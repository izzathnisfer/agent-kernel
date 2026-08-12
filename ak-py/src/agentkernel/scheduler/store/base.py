"""The ``ScheduledTaskStore`` ABC and the builder that resolves its backend.

The store is a private collaborator of the ``Scheduler``, not a public seam. It is pluggable
only so that three storage backends can sit behind one timer implementation.
"""

import base64
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from ...core.config import AKConfig
from ...core.util.factory import AKConfigError, require_extra
from ..errors import ScheduleValidationError
from ..model import ScheduledTask, ScheduledTaskPage

# Session store types durable enough to share scheduled tasks across replicas. Anything
# else fails the enablement check at initialization.
DURABLE_SESSION_TYPES = ("dynamodb", "redis", "valkey")


class TaskSerializer:
    """JSON-safe conversion between ``ScheduledTask`` rows and backend records.

    Shared by all three backends so one row shape is stored everywhere: DynamoDB keeps it
    as an item, Redis and Valkey as a JSON string.
    """

    @staticmethod
    def to_record(task: ScheduledTask) -> dict[str, Any]:
        """Convert a row to a JSON-safe record.

        :param task: The scheduled task to convert.
        :return: A record whose values are JSON scalars, maps and lists.
        """
        return task.model_dump(mode="json")

    @staticmethod
    def from_record(record: dict[str, Any]) -> ScheduledTask:
        """Rebuild a row from a stored record, ignoring backend-added attributes.

        :param record: The stored record.
        :return: The parsed scheduled task.
        """
        return ScheduledTask.model_validate(record)

    @staticmethod
    def encode_value(value: Any) -> Any:
        """Convert one attribute value to its JSON-safe form.

        :param value: The value to convert.
        :return: An ISO string for a datetime, the member value for an enum, else the value.
        """
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        return value

    @staticmethod
    def encode_fields(fields: dict[str, Any]) -> dict[str, Any]:
        """Convert a partial-update field map to its JSON-safe form.

        :param fields: Attribute names mapped to their new values.
        :return: The same map with JSON-safe values.
        """
        return {name: TaskSerializer.encode_value(value) for name, value in fields.items()}


class PageCursor:
    """Opaque, backend-neutral pagination cursor.

    A cursor is a backend's own continuation state (a DynamoDB ``LastEvaluatedKey``, an
    offset on Redis), base64-encoded so callers cannot construct or interpret one.
    """

    @staticmethod
    def encode(state: Any) -> str:
        """Encode backend continuation state into an opaque cursor.

        :param state: JSON-serializable continuation state.
        :return: The opaque cursor string.
        """
        return base64.urlsafe_b64encode(json.dumps(state).encode("utf-8")).decode("ascii")

    @staticmethod
    def decode(cursor: Optional[str], expected_type: Optional[type] = None) -> Any:
        """Decode an opaque cursor back into backend continuation state.

        The decoded shape is checked as well as the encoding: a well-formed cursor carrying the
        wrong type is still a client error, and left unchecked it surfaces further down as a
        ``TypeError`` or a provider-side rejection rather than as a rejected cursor.

        :param cursor: The cursor from a previous page, or None for the first page.
        :param expected_type: The continuation state the calling backend paginates on; None
            accepts any shape.
        :return: The decoded state, or None when no cursor was supplied.
        :raises ScheduleValidationError: The cursor is not one this class produced for this
            backend.
        """
        if not cursor:
            return None
        try:
            state = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        except Exception as exc:
            raise ScheduleValidationError(f"invalid pagination cursor: {cursor}") from exc
        if expected_type is not None and not isinstance(state, expected_type):
            raise ScheduleValidationError(f"invalid pagination cursor: {cursor}")
        return state


class ScheduledTaskStore(ABC):
    """Persistence interface for scheduled tasks.

    Implementations never read ``AKConfig``: all connection and layout parameters are
    explicit constructor arguments, and config reading lives in
    :class:`ScheduledTaskStoreBuilder`.
    """

    _log = logging.getLogger("ak.scheduler.store")

    @abstractmethod
    def put(self, task: ScheduledTask) -> None:
        """Write the whole row. Used for creation only.

        :param task: The scheduled task to write.
        """

    @abstractmethod
    def update_fields(self, scheduled_task_id: str, fields: dict[str, Any], *, expected_version: Optional[str] = None) -> bool:
        """Write a subset of attributes, leaving every other attribute untouched.

        Lets a management ``PUT`` and an outcome write interleave without clobbering each
        other's fields, which a whole-row ``put`` cannot express.

        :param scheduled_task_id: Identity of the row to update.
        :param fields: Attribute names mapped to their new values.
        :param expected_version: When given, the write applies only if the stored
            ``scheduled_task_version`` matches, folding the incarnation guard into the
            write rather than leaving a check-then-act window.
        :return: False when ``expected_version`` does not match; True when written.
        """

    @abstractmethod
    def get(self, scheduled_task_id: str) -> Optional[ScheduledTask]:
        """Read one row, including soft-deleted ones — filtering is the ``Scheduler``'s job.

        :param scheduled_task_id: Identity of the row to read.
        :return: The scheduled task, or None when absent.
        """

    @abstractmethod
    def list_by_owner(self, owner_id: str, *, limit: Optional[int] = None, cursor: Optional[str] = None) -> ScheduledTaskPage:
        """Return one page of an owner's live rows.

        Each backend excludes soft-deleted rows itself, so the page size reflects live rows
        and is never short because tombstones were filtered out.

        :param owner_id: The owner whose rows to list.
        :param limit: Maximum number of rows in the page.
        :param cursor: Opaque continuation token from a previous page.
        :return: One page of scheduled tasks.
        """

    @abstractmethod
    def remove(self, scheduled_task_id: str) -> None:
        """Physically remove a row, leaving no tombstone. Idempotent.

        Used only to roll back a creation whose timer registration failed, where a tombstone
        would block retrying the create at the same id.

        :param scheduled_task_id: Identity of the row to remove.
        """

    @abstractmethod
    def soft_delete(self, scheduled_task_id: str, deleted_at: datetime, ttl_seconds: int) -> None:
        """Mark the row deleted and set it to expire after the grace window.

        The row stays readable by primary key throughout the window, which is what the
        outcome-write guards need.

        :param scheduled_task_id: Identity of the row to soft-delete.
        :param deleted_at: Timestamp recorded on the tombstone.
        :param ttl_seconds: Grace window before the backend expires the row.
        """


class ScheduledTaskStoreBuilder:
    """Resolves the scheduled-task store from ``session.type``.

    The backend is not configured separately. DynamoDB sessions get a dedicated table;
    Redis and Valkey sessions reuse their cluster under a separate keyspace, so those two
    need no new infrastructure.
    """

    _log = logging.getLogger("ak.scheduler.store.builder")

    @staticmethod
    def build() -> ScheduledTaskStore:
        """Build the store matching the configured session backend.

        :return: The resolved scheduled-task store.
        :raises AKConfigError: When ``session.type`` is not a durable backend.
        """
        config = AKConfig.get()
        store_type = config.session.type.lower()
        scheduler_config = config.scheduler
        # Defaulted when the deployment did not declare it — the block only ever parameterizes
        # the store that session.type already selected.
        store_block = scheduler_config.store_block(store_type)
        ScheduledTaskStoreBuilder._log.info("Building '%s' scheduled task store", store_type)

        if store_type == "dynamodb":
            with require_extra("aws", "scheduler with session.type: dynamodb"):
                from .dynamodb import DynamoDBScheduledTaskStore

            return DynamoDBScheduledTaskStore(table_name=store_block.table_name, region=scheduler_config.region)
        if store_type == "redis":
            with require_extra("redis", "scheduler with session.type: redis"):
                from .redis import RedisScheduledTaskStore

            return RedisScheduledTaskStore(url=config.session.redis.url, prefix=store_block.prefix)
        if store_type == "valkey":
            with require_extra("valkey", "scheduler with session.type: valkey"):
                from .valkey import ValkeyScheduledTaskStore

            return ValkeyScheduledTaskStore(url=config.session.valkey.url, prefix=store_block.prefix)

        raise AKConfigError(
            f"scheduler requires a durable session store; session.type '{config.session.type}' is not one of {list(DURABLE_SESSION_TYPES)}"
        )
