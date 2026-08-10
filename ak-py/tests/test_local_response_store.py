from unittest.mock import Mock

import pytest

from testharness.core.response_store import LocalResponseStore


@pytest.fixture
def store(tmp_path):
    return LocalResponseStore(str(tmp_path / "responses.db"))


class TestAddGetMessage:
    def test_get_missing_message_returns_none(self, store):
        assert store.get_message("missing") is None

    def test_add_then_get_round_trips_body(self, store):
        store.add_message({"request_id": "r1", "session_id": "s1", "body": {"result": "ok"}})

        message = store.get_message("r1")

        assert message == {"request_id": "r1", "session_id": "s1", "body": {"result": "ok"}}

    def test_get_without_delete_keeps_message(self, store):
        store.add_message({"request_id": "r1", "session_id": "s1", "body": {"result": "ok"}})

        store.get_message("r1", get_and_delete=False)

        assert store.get_message("r1") is not None

    def test_get_and_delete_removes_message(self, store):
        store.add_message({"request_id": "r1", "session_id": "s1", "body": {"result": "ok"}})

        first = store.get_message("r1", get_and_delete=True)
        second = store.get_message("r1")

        assert first is not None
        assert second is None

    def test_add_message_overwrites_existing_request_id(self, store):
        store.add_message({"request_id": "r1", "session_id": "s1", "body": {"result": "first"}})
        store.add_message({"request_id": "r1", "session_id": "s1", "body": {"result": "second"}})

        assert store.get_message("r1")["body"] == {"result": "second"}


class TestDeleteMessage:
    def test_delete_message_removes_it(self, store):
        store.add_message({"request_id": "r1", "session_id": "s1", "body": {}})

        store.delete_message("r1")

        assert store.get_message("r1") is None

    def test_delete_missing_message_does_not_raise(self, store):
        store.delete_message("missing")


class TestPersistence:
    def test_second_instance_same_file_sees_stored_message(self, tmp_path):
        db_path = str(tmp_path / "shared.db")
        writer = LocalResponseStore(db_path)
        writer.add_message({"request_id": "r1", "session_id": "s1", "body": {"result": "ok"}})

        reader = LocalResponseStore(db_path)
        assert reader.get_message("r1") is not None


class TestGetMessageWithRetry:
    def test_retries_until_message_appears(self, store, monkeypatch):
        config = Mock()
        config.execution.response_store.retry_count = 3
        config.execution.response_store.delay = 0
        monkeypatch.setattr("agentkernel.deployment.common.response_store.AKConfig.get", lambda: config)

        calls = {"n": 0}
        original_get = store.get_message

        def flaky_get(request_id, get_and_delete=False):
            calls["n"] += 1
            if calls["n"] < 2:
                return None
            return original_get(request_id, get_and_delete=get_and_delete)

        store.add_message({"request_id": "r1", "session_id": "s1", "body": {"result": "ok"}})
        monkeypatch.setattr(store, "get_message", flaky_get)

        assert store.get_message_with_retry("r1") is not None
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_async_mode_returns_awaitable(self, store, monkeypatch):
        config = Mock()
        config.execution.response_store = None
        monkeypatch.setattr("agentkernel.deployment.common.response_store.AKConfig.get", lambda: config)

        store.add_message({"request_id": "r1", "session_id": "s1", "body": {"result": "ok"}})

        result = await store.get_message_with_retry("r1", async_mode=True)

        assert result["body"] == {"result": "ok"}
