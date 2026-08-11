"""Store-level tests for the scheduled-task backends (mocked shared drivers)."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from conftest_scheduler import enable_scheduler_config, reset_scheduler_config

from agentkernel.core.util.factory import AKConfigError
from agentkernel.scheduler.model import RunStatus, ScheduleSpec, TaskStatus
from agentkernel.scheduler.store.base import PageCursor, ScheduledTaskStoreBuilder, TaskSerializer
from agentkernel.scheduler.store.dynamodb import OWNER_INDEX_KEY, OWNER_INDEX_NAME, TTL_ATTRIBUTE, DynamoDBScheduledTaskStore
from agentkernel.scheduler.store.redis import RedisScheduledTaskStore
from agentkernel.scheduler.testing import InMemoryScheduledTaskStore, build_task

PREFIX = "ak:scheduled_tasks:"


# --------------------------------------------------------------------- contract


@pytest.fixture(params=["in_memory", "dynamodb", "redis"])
def store(request):
    """Every backend, behind the one ScheduledTaskStore contract."""
    if request.param == "in_memory":
        return InMemoryScheduledTaskStore()
    if request.param == "dynamodb":
        return _fake_dynamodb_store()
    return _fake_redis_store()[0]


class TestStoreContract:
    def test_put_and_get_round_trip(self, store):
        task = build_task("a")
        store.put(task)
        loaded = store.get("a")
        assert loaded.owner_id == task.owner_id
        assert loaded.schedule.rate == task.schedule.rate
        assert loaded.message == task.message

    def test_get_returns_none_for_an_unknown_id(self, store):
        assert store.get("missing") is None

    def test_list_by_owner_is_scoped(self, store):
        store.put(build_task("mine", owner_id="u1"))
        store.put(build_task("theirs", owner_id="u2"))
        assert [task.scheduled_task_id for task in store.list_by_owner("u1").items] == ["mine"]

    def test_list_by_owner_paginates(self, store):
        for index in range(3):
            store.put(build_task(f"task-{index}", owner_id="u1"))

        first = store.list_by_owner("u1", limit=2)
        assert len(first.items) == 2
        assert first.next_cursor is not None

        second = store.list_by_owner("u1", limit=2, cursor=first.next_cursor)
        assert len(second.items) == 1

    def test_soft_deleted_rows_stay_readable_but_leave_the_listing(self, store):
        store.put(build_task("a", owner_id="u1"))
        store.soft_delete("a", datetime.now(timezone.utc), 900)

        assert store.get("a").deleted is True
        assert store.list_by_owner("u1").items == []

    def test_a_page_is_not_short_because_a_tombstone_was_filtered(self, store):
        for index in range(3):
            store.put(build_task(f"task-{index}", owner_id="u1"))
        store.soft_delete("task-0", datetime.now(timezone.utc), 900)

        page = store.list_by_owner("u1", limit=2)
        assert len(page.items) == 2

    def test_update_fields_touches_only_the_named_attributes(self, store):
        task = build_task("a")
        store.put(task)

        assert store.update_fields("a", {"last_run_status": RunStatus.COMPLETED, "last_error": "boom"}) is True

        loaded = store.get("a")
        assert loaded.last_run_status == RunStatus.COMPLETED
        assert loaded.last_error == "boom"
        # The definition is untouched, so an outcome write cannot clobber a concurrent PUT.
        assert loaded.schedule.rate == task.schedule.rate
        assert loaded.message == task.message
        assert loaded.status == TaskStatus.ACTIVE

    def test_update_fields_leaves_run_state_alone_when_writing_the_definition(self, store):
        store.put(build_task("a"))
        store.update_fields("a", {"last_run_status": RunStatus.COMPLETED})
        store.update_fields("a", {"schedule": ScheduleSpec(rate="2 hours").model_dump(mode="json")})

        loaded = store.get("a")
        assert loaded.schedule.rate == "2 hours"
        assert loaded.last_run_status == RunStatus.COMPLETED

    def test_update_fields_rejects_a_version_mismatch_without_writing(self, store):
        task = build_task("a", version="v-current")
        store.put(task)

        assert store.update_fields("a", {"last_error": "boom"}, expected_version="v-previous") is False
        assert store.get("a").last_error is None

    def test_remove_leaves_no_tombstone(self, store):
        store.put(build_task("a", owner_id="u1"))
        store.remove("a")
        assert store.get("a") is None
        assert store.list_by_owner("u1").items == []


# --------------------------------------------------------------------- DynamoDB


class _FakeTable:
    """Minimal in-memory stand-in for the boto3 Table surface the store uses."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.queries: list[dict] = []

    def put_item(self, Item):
        self.items[Item["scheduled_task_id"]] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key["scheduled_task_id"])
        return {"Item": dict(item)} if item is not None else {}

    def delete_item(self, Key):
        self.items.pop(Key["scheduled_task_id"], None)

    def update_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["scheduled_task_id"])
        if item is None:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

        condition = kwargs.get("ConditionExpression")
        if condition is not None and item.get("scheduled_task_version") != condition._values[1]:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        expression = kwargs["UpdateExpression"]
        for clause in _split_update_expression(expression):
            if clause.startswith("SET "):
                for assignment in clause[4:].split(", "):
                    placeholder, value_ref = [part.strip() for part in assignment.split("=")]
                    item[names[placeholder]] = values[value_ref]
            elif clause.startswith("REMOVE "):
                for placeholder in clause[7:].split(", "):
                    item.pop(names[placeholder.strip()], None)

    def query(self, **kwargs):
        self.queries.append(kwargs)
        owner = kwargs["KeyConditionExpression"]._values[1]
        matching = sorted(
            (item for item in self.items.values() if item.get(OWNER_INDEX_KEY) == owner),
            key=lambda item: item["scheduled_task_id"],
        )
        start = json.loads(kwargs["ExclusiveStartKey"]["offset"]) if "ExclusiveStartKey" in kwargs else 0
        limit = kwargs.get("Limit", len(matching))
        window = matching[start : start + limit]
        response = {"Items": [dict(item) for item in window]}
        if start + len(window) < len(matching):
            response["LastEvaluatedKey"] = {"offset": json.dumps(start + len(window))}
        return response


def _split_update_expression(expression: str) -> list[str]:
    """Split a 'SET ... REMOVE ...' expression into its clauses."""
    if " REMOVE " in expression:
        set_clause, remove_clause = expression.split(" REMOVE ", 1)
        return [set_clause, f"REMOVE {remove_clause}"]
    return [expression]


def _fake_dynamodb_store() -> DynamoDBScheduledTaskStore:
    store = DynamoDBScheduledTaskStore(table_name="ak-scheduled-tasks")
    table = _FakeTable()
    driver = MagicMock()
    driver.table = table
    driver.put.side_effect = lambda item: table.put_item(Item=item)
    driver.get.side_effect = lambda pk: table.get_item(Key={"scheduled_task_id": pk}).get("Item")
    driver.delete.side_effect = lambda pk: table.delete_item(Key={"scheduled_task_id": pk})
    store._driver = driver
    return store


class TestDynamoDBLayout:
    def test_driver_is_built_without_a_ttl(self, monkeypatch):
        """A configured TTL would stamp expiry_time on every put and expire live rows."""
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("agentkernel.scheduler.store.dynamodb.DynamoDBDriver", capture)
        DynamoDBScheduledTaskStore(table_name="ak-scheduled-tasks", region="us-east-1")

        assert captured["ttl"] == 0
        assert captured["partition_key"] == "scheduled_task_id"

    def test_expiry_time_is_written_only_by_soft_delete(self):
        store = _fake_dynamodb_store()
        store.put(build_task("a"))
        assert TTL_ATTRIBUTE not in store._driver.table.items["a"]

        store.soft_delete("a", datetime.now(timezone.utc), 900)
        assert TTL_ATTRIBUTE in store._driver.table.items["a"]

    def test_soft_delete_drops_the_index_key_but_keeps_the_row_complete(self):
        store = _fake_dynamodb_store()
        store.put(build_task("a", owner_id="u1"))
        store.soft_delete("a", datetime.now(timezone.utc), 900)

        item = store._driver.table.items["a"]
        assert OWNER_INDEX_KEY not in item
        # owner_id itself stays, so the tombstone remains a readable row for the
        # outcome-write guards during the grace window.
        assert item["owner_id"] == "u1"

    def test_list_queries_the_sparse_index_with_no_filter(self):
        store = _fake_dynamodb_store()
        store.put(build_task("a", owner_id="u1"))
        store.list_by_owner("u1")

        query = store._driver.table.queries[-1]
        assert query["IndexName"] == OWNER_INDEX_NAME
        assert "FilterExpression" not in query


# ------------------------------------------------------------------ Redis-like


class _FakeRedisClient:
    """Minimal stand-in for the redis client surface the store reaches for directly."""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.expirations: dict[str, int] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    def delete(self, *keys):
        for key in keys:
            self.strings.pop(key, None)

    def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    def expire(self, name, time):
        self.expirations[name] = time


def _fake_redis_store() -> tuple[RedisScheduledTaskStore, _FakeRedisClient]:
    store = RedisScheduledTaskStore(url="redis://localhost:6379", prefix=PREFIX)
    client = _FakeRedisClient()
    driver = MagicMock()
    driver.client = client
    driver.key.side_effect = lambda suffix: f"{PREFIX}{suffix}"
    driver.set.side_effect = lambda key, value: client.set(key, value)
    driver.get.side_effect = lambda key: client.strings.get(key)
    driver.delete.side_effect = lambda *keys: client.delete(*keys)
    driver.sadd.side_effect = lambda key, member: client.sets.setdefault(key, set()).add(member)
    driver.smembers.side_effect = lambda key: set(client.sets.get(key, set()))
    store._driver = driver
    return store, client


class TestRedisLayout:
    def test_driver_is_built_without_a_ttl(self, monkeypatch):
        """A configured TTL would apply to every write and expire live rows."""
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("agentkernel.scheduler.store.redis.RedisDriver", capture)
        RedisScheduledTaskStore(url="redis://localhost:6379", prefix=PREFIX)

        assert captured["ttl"] == 0
        assert captured["prefix"] == PREFIX

    def test_soft_delete_applies_the_derived_window_per_call(self):
        store, client = _fake_redis_store()
        store.put(build_task("a", owner_id="u1"))
        store.soft_delete("a", datetime.now(timezone.utc), 1234)

        assert client.expirations[f"{PREFIX}a"] == 1234

    def test_a_tombstone_stays_in_the_owner_index_while_it_is_readable(self):
        store, client = _fake_redis_store()
        store.put(build_task("a", owner_id="u1"))
        store.soft_delete("a", datetime.now(timezone.utc), 900)

        store.list_by_owner("u1")
        assert "a" in client.sets[f"{PREFIX}owner:u1"]

    def test_expired_rows_are_pruned_from_the_owner_index(self):
        """A set member does not disappear when its row key expires."""
        store, client = _fake_redis_store()
        store.put(build_task("a", owner_id="u1"))
        client.strings.pop(f"{PREFIX}a")

        store.list_by_owner("u1")
        assert client.sets[f"{PREFIX}owner:u1"] == set()

    def test_update_takes_and_releases_the_row_lock(self):
        store, client = _fake_redis_store()
        store.put(build_task("a"))
        store.update_fields("a", {"last_error": "boom"})

        assert f"{PREFIX}lock:a" not in client.strings


# ---------------------------------------------------------------------- builder


class TestStoreBuilder:
    @pytest.fixture(autouse=True)
    def _clean_config(self):
        yield
        reset_scheduler_config()

    def test_a_non_durable_session_store_is_rejected(self):
        enable_scheduler_config(session_type="in_memory")
        with pytest.raises(AKConfigError, match="durable"):
            ScheduledTaskStoreBuilder.build()

    def test_dynamodb_sessions_resolve_a_dedicated_table(self, monkeypatch):
        enable_scheduler_config()
        monkeypatch.setattr("agentkernel.scheduler.store.dynamodb.DynamoDBDriver", lambda **kwargs: MagicMock())
        assert isinstance(ScheduledTaskStoreBuilder.build(), DynamoDBScheduledTaskStore)


class TestPageCursor:
    def test_a_cursor_round_trips(self):
        assert PageCursor.decode(PageCursor.encode({"offset": 3})) == {"offset": 3}

    def test_no_cursor_decodes_to_none(self):
        assert PageCursor.decode(None) is None

    def test_a_malformed_cursor_is_rejected(self):
        with pytest.raises(ValueError, match="invalid pagination cursor"):
            PageCursor.decode("not-a-cursor")


class TestTaskSerializer:
    def test_records_are_json_safe(self):
        record = TaskSerializer.to_record(build_task("a"))
        json.dumps(record)  # must not raise

    def test_backend_added_attributes_are_ignored_on_read(self):
        record = TaskSerializer.to_record(build_task("a"))
        record[TTL_ATTRIBUTE] = 12345
        assert TaskSerializer.from_record(record).scheduled_task_id == "a"
