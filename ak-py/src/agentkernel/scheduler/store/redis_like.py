"""Shared scheduled-task store body for the Redis-protocol backends.

Layout (keys under the configured prefix):
  - Row:        ``{prefix}{scheduled_task_id}``      -> the JSON-serialized ScheduledTask
  - Owner index: ``{prefix}owner:{owner_id}``        -> set of scheduled_task_ids
  - Update lock: ``{prefix}lock:{scheduled_task_id}`` -> short-lived SET NX guard

The owner set is the only index available, so unlike DynamoDB there is no sparse index to
lean on and ``list_by_owner`` filters tombstones out on read. Its pages are ordered by id and
its cursor is the last id read, so paging is stable against tasks added or removed in between.
"""

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from ...core.util.driver.redis_like import _RedisLikeDriver
from ..errors import SchedulerConflictError
from ..model import ScheduledTask, ScheduledTaskPage
from .base import PageCursor, ScheduledTaskStore, TaskSerializer

# A row is one JSON string, so a partial update is a read-merge-write. The lock closes that
# window and is short-lived because the critical section is only two round trips.
UPDATE_LOCK_TTL_SECONDS = 5
UPDATE_LOCK_ATTEMPTS = 20
UPDATE_LOCK_RETRY_DELAY_SECONDS = 0.05


class _RedisLikeScheduledTaskStore(ScheduledTaskStore):
    """Scheduled-task rows on a Redis-protocol cluster shared with the session store.

    Concrete subclasses implement only ``__init__``, where they must set both attributes
    below — this class reads them but never assigns them.
    """

    _driver: _RedisLikeDriver
    _log: logging.Logger

    def _row_key(self, scheduled_task_id: str) -> str:
        """Key holding one scheduled task's JSON row."""
        return self._driver.key(scheduled_task_id)

    def _owner_key(self, owner_id: str) -> str:
        """Key holding the set of an owner's scheduled task ids."""
        return self._driver.key(f"owner:{owner_id}")

    def _lock_key(self, scheduled_task_id: str) -> str:
        """Key guarding a read-merge-write on one row."""
        return self._driver.key(f"lock:{scheduled_task_id}")

    def put(self, task: ScheduledTask) -> None:
        self._log.debug("Putting scheduled task %s", task.scheduled_task_id)
        self._driver.set(self._row_key(task.scheduled_task_id), json.dumps(TaskSerializer.to_record(task)))
        self._driver.sadd(self._owner_key(task.owner_id), task.scheduled_task_id)

    def update_fields(self, scheduled_task_id: str, fields: dict[str, Any], *, expected_version: Optional[str] = None) -> bool:
        if not fields:
            return True

        with self._row_lock(scheduled_task_id):
            return self._merge_locked(scheduled_task_id, fields, expected_version)

    def _merge_locked(self, scheduled_task_id: str, fields: dict[str, Any], expected_version: Optional[str]) -> bool:
        """Read, merge and write one row. **The caller must already hold the row lock.**

        Split out of ``update_fields`` so ``soft_delete`` can merge, then set expiry, without
        releasing the non-reentrant lock in between.

        :param scheduled_task_id: Identity of the row to merge into.
        :param fields: Attribute names mapped to their new values.
        :param expected_version: When given, the write applies only if the stored version matches.
        :return: False when the row is absent or the version does not match; True when written.
        """
        record = self._read_record(scheduled_task_id)
        if record is None:
            return False
        if expected_version is not None and record.get("scheduled_task_version") != expected_version:
            self._log.warning("Rejected update of %s: scheduled_task_version is not %s", scheduled_task_id, expected_version)
            return False
        record.update(TaskSerializer.encode_fields(fields))
        self._driver.set(self._row_key(scheduled_task_id), json.dumps(record))
        return True

    def get(self, scheduled_task_id: str) -> Optional[ScheduledTask]:
        record = self._read_record(scheduled_task_id)
        return TaskSerializer.from_record(record) if record is not None else None

    def list_by_owner(self, owner_id: str, *, limit: Optional[int] = None, cursor: Optional[str] = None) -> ScheduledTaskPage:
        # Resume after the previous page's last id rather than by offset: the set shifts when a
        # task is created, deleted or pruned between pages, and an offset then skips or repeats rows.
        owner_key = self._owner_key(owner_id)
        task_ids = sorted(self._driver.smembers(owner_key))
        # str: this backend resumes after an id, and comparing a non-string against one would
        # raise a TypeError below rather than reject the cursor.
        after = PageCursor.decode(cursor, expected_type=str)
        pending = [task_id for task_id in task_ids if after is None or task_id > after]

        items: list[ScheduledTask] = []
        last_read: Optional[str] = None
        more_remain = False
        for task_id in pending:
            if limit is not None and len(items) >= limit:
                more_remain = True
                break
            last_read = task_id
            record = self._read_record(task_id)
            if record is None:
                # The row key expired but its set member did not. Prune it, or the index grows
                # without bound after every TTL expiry.
                self._driver.client.srem(owner_key, task_id)
                continue
            # A tombstone is hidden from the listing but kept in the index, since the row must
            # stay readable during the grace window.
            if not record.get("deleted"):
                items.append(TaskSerializer.from_record(record))

        # last_read, not the last item, so a page whose tail is all tombstones still advances.
        next_cursor = PageCursor.encode(last_read) if more_remain and last_read is not None else None
        return ScheduledTaskPage(items=items, next_cursor=next_cursor)

    def remove(self, scheduled_task_id: str) -> None:
        self._log.debug("Removing scheduled task %s", scheduled_task_id)
        record = self._read_record(scheduled_task_id)
        self._driver.delete(self._row_key(scheduled_task_id))
        if record is not None and record.get("owner_id"):
            self._driver.client.srem(self._owner_key(record["owner_id"]), scheduled_task_id)

    def soft_delete(self, scheduled_task_id: str, deleted_at: datetime, ttl_seconds: int) -> None:
        self._log.debug("Soft-deleting scheduled task %s with a %ss grace window", scheduled_task_id, ttl_seconds)
        # One lock across both steps: releasing before setting expiry would let a concurrent
        # put land in between and leave a tombstone that never expires.
        with self._row_lock(scheduled_task_id):
            if not self._merge_locked(scheduled_task_id, {"deleted": True, "deleted_at": deleted_at}, None):
                # Idempotent, like the rest of the delete path: no row left to tombstone.
                self._log.info("Nothing to soft-delete at %s; the row is already gone", scheduled_task_id)
                return
            # The native handle, because the driver's expire() can only apply its own configured
            # TTL while the soft-delete window is derived per call.
            self._driver.client.expire(name=self._row_key(scheduled_task_id), time=int(ttl_seconds))

    def _read_record(self, scheduled_task_id: str) -> Optional[dict[str, Any]]:
        """Read one row's raw record.

        :param scheduled_task_id: Identity of the row to read.
        :return: The decoded record, or None when the key does not exist.
        """
        raw = self._driver.get(self._row_key(scheduled_task_id))
        if raw is None:
            return None
        return json.loads(raw)

    def _row_lock(self, scheduled_task_id: str) -> "_RowLock":
        """Acquire the read-merge-write guard for one row.

        :param scheduled_task_id: Identity of the row to guard.
        :return: A context manager holding the lock.
        """
        return _RowLock(self._driver, self._lock_key(scheduled_task_id))


# Releases the lock only if this holder still owns it. An unconditional DELETE lets a writer
# whose read-merge-write outlived the lock TTL (a driver reconnect can stall one) delete the
# lock a second writer has since taken, after which two merges interleave.
_RELEASE_IF_MINE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class _RowLock:
    """Short-lived ``SET NX`` guard around a row's read-merge-write.

    Fenced with a per-acquisition token so a release can only ever drop this holder's own lock.
    """

    _log = logging.getLogger("ak.scheduler.store.lock")

    def __init__(self, driver: _RedisLikeDriver, key: str):
        """
        :param driver: The Redis-protocol driver owning the connection.
        :param key: The lock key to claim.
        """
        self._driver = driver
        self._key = key
        # Unique per lock object, so two writers can never hold the same token.
        self._token = uuid.uuid4().hex

    def __enter__(self) -> "_RowLock":
        for _ in range(UPDATE_LOCK_ATTEMPTS):
            if self._driver.client.set(self._key, self._token, nx=True, ex=UPDATE_LOCK_TTL_SECONDS):
                return self
            time.sleep(UPDATE_LOCK_RETRY_DELAY_SECONDS)
        raise SchedulerConflictError(f"another writer holds {self._key}; retry the request")

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            # Compare-and-delete in one round trip, so there is no window between the check and
            # the delete for the lock to change hands in.
            released = self._driver.client.eval(_RELEASE_IF_MINE, 1, self._key, self._token)
        except Exception:
            # The lock's own TTL releases it; failing to clean up must not mask the
            # caller's outcome.
            self._log.warning("Failed to release scheduled-task lock %s; it expires in %ss", self._key, UPDATE_LOCK_TTL_SECONDS)
            return
        if not released:
            # The lock expired mid-write and someone else may have taken it, so this write raced
            # another. Worth knowing about: it means a merge took longer than the lock TTL.
            self._log.warning("Scheduled-task lock %s was no longer held at release; it expired mid-write", self._key)
