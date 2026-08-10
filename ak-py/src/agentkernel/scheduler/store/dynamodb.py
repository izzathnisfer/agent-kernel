"""DynamoDB-backed scheduled-task store.

Layout: partition key ``scheduled_task_id``, no sort key; a sparse global secondary index
``owner-index`` on ``owner_index_key`` / ``created_at`` serving ``list_by_owner``; TTL
attribute ``expiry_time`` written only by ``soft_delete``.

``owner_index_key`` mirrors ``owner_id`` while the row is live and is removed by
``soft_delete``, which is how ``list_by_owner``'s live-rows-only contract is met here. The
alternative — keeping tombstones in the index and filtering them out with a
``FilterExpression`` — filters *after* the read, so a page of ``limit`` items can come back
with fewer (or zero) live rows while ``LastEvaluatedKey`` is still set. Dropping the index
key instead takes tombstones out of the index entirely: no filter expression, no short
pages, and no read capacity spent on rows nobody can see. The index key is a separate
attribute rather than ``owner_id`` itself so the row stays complete and readable by primary
key throughout the grace window, which is what the outcome-write guards need.
"""

import time
from datetime import datetime
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr as DDBAttr
from boto3.dynamodb.conditions import Key as DDBKey
from botocore.exceptions import ClientError

from ...core.util.driver.dynamodb import DynamoDBDriver
from ..model import ScheduledTask, ScheduledTaskPage
from .base import PageCursor, ScheduledTaskStore, TaskSerializer

OWNER_INDEX_NAME = "owner-index"
OWNER_INDEX_KEY = "owner_index_key"
TTL_ATTRIBUTE = "expiry_time"


class DynamoDBScheduledTaskStore(ScheduledTaskStore):
    """Scheduled-task rows in a dedicated DynamoDB table."""

    def __init__(self, table_name: str, region: Optional[str] = None):
        """
        :param table_name: The dedicated scheduled-task table; never a partition of the
            session or response-store table.
        :param region: AWS region; defaults to the boto3 environment default.
        """
        self._log.debug("Initializing DynamoDBScheduledTaskStore with table_name=%s region=%s", table_name, region)
        # ttl=0 deliberately: DynamoDBDriver.put stamps expiry_time on every put when a TTL
        # is configured, which would expire live rows. soft_delete writes it explicitly.
        self._driver = DynamoDBDriver(table_name=table_name, partition_key="scheduled_task_id", region=region, ttl=0)

    def put(self, task: ScheduledTask) -> None:
        self._log.debug("Putting scheduled task %s", task.scheduled_task_id)
        record = TaskSerializer.to_record(task)
        record[OWNER_INDEX_KEY] = task.owner_id
        self._driver.put(record)

    def update_fields(self, scheduled_task_id: str, fields: dict[str, Any], *, expected_version: Optional[str] = None) -> bool:
        if not fields:
            return True

        encoded = TaskSerializer.encode_fields(fields)
        set_names, remove_names = self._split_by_removal(encoded)

        expression_names = {f"#{index}": name for index, name in enumerate(encoded)}
        expression_values = {f":{index}": encoded[name] for index, name in enumerate(encoded) if name in set_names}
        clauses = []
        if set_names:
            clauses.append("SET " + ", ".join(f"#{index} = :{index}" for index, name in enumerate(encoded) if name in set_names))
        if remove_names:
            clauses.append("REMOVE " + ", ".join(f"#{index}" for index, name in enumerate(encoded) if name in remove_names))

        kwargs: dict[str, Any] = {
            "Key": {"scheduled_task_id": scheduled_task_id},
            "UpdateExpression": " ".join(clauses),
            "ExpressionAttributeNames": expression_names,
        }
        if expression_values:
            kwargs["ExpressionAttributeValues"] = expression_values
        if expected_version is not None:
            kwargs["ConditionExpression"] = DDBAttr("scheduled_task_version").eq(expected_version)

        try:
            self._driver.table.update_item(**kwargs)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            self._log.warning("Rejected update of %s: scheduled_task_version is not %s", scheduled_task_id, expected_version)
            return False

    def get(self, scheduled_task_id: str) -> Optional[ScheduledTask]:
        item = self._driver.get(scheduled_task_id)
        if item is None:
            return None
        return TaskSerializer.from_record(item)

    def list_by_owner(self, owner_id: str, *, limit: Optional[int] = None, cursor: Optional[str] = None) -> ScheduledTaskPage:
        kwargs: dict[str, Any] = {
            "IndexName": OWNER_INDEX_NAME,
            "KeyConditionExpression": DDBKey(OWNER_INDEX_KEY).eq(owner_id),
        }
        if limit is not None:
            kwargs["Limit"] = limit
        start_key = PageCursor.decode(cursor)
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key

        # No FilterExpression: the index is sparse (soft_delete removes owner_id), so
        # tombstones are not in it and a page is never short because one was filtered out.
        response = self._driver.table.query(**kwargs)
        items = [TaskSerializer.from_record(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        return ScheduledTaskPage(items=items, next_cursor=PageCursor.encode(last_key) if last_key else None)

    def remove(self, scheduled_task_id: str) -> None:
        self._log.debug("Removing scheduled task %s", scheduled_task_id)
        self._driver.delete(scheduled_task_id)

    def soft_delete(self, scheduled_task_id: str, deleted_at: datetime, ttl_seconds: int) -> None:
        self._log.debug("Soft-deleting scheduled task %s with a %ss grace window", scheduled_task_id, ttl_seconds)
        self._driver.table.update_item(
            Key={"scheduled_task_id": scheduled_task_id},
            # Dropping the index key keeps the GSI sparse, so the tombstone leaves the
            # listing index entirely while the row itself stays get-able by primary key.
            UpdateExpression="SET #deleted = :deleted, #deleted_at = :deleted_at, #ttl = :ttl REMOVE #owner_index",
            ExpressionAttributeNames={
                "#deleted": "deleted",
                "#deleted_at": "deleted_at",
                "#ttl": TTL_ATTRIBUTE,
                "#owner_index": OWNER_INDEX_KEY,
            },
            ExpressionAttributeValues={
                ":deleted": True,
                ":deleted_at": deleted_at.isoformat(),
                ":ttl": int(time.time()) + int(ttl_seconds),
            },
        )

    @staticmethod
    def _split_by_removal(fields: dict[str, Any]) -> tuple[set[str], set[str]]:
        """Partition an update into attributes to set and attributes to remove.

        A None clears the attribute rather than storing a null, so a cleared
        ``completed_at`` reads back as absent.

        :param fields: JSON-safe attribute names mapped to their new values.
        :return: The names to SET and the names to REMOVE.
        """
        remove_names = {name for name, value in fields.items() if value is None}
        return set(fields) - remove_names, remove_names
