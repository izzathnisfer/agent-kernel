import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class LocalQueue:
    """
    SQLite-backed FIFO queue doing for ``queue.Queue`` what SQS does for a real broker:
    durable, cross-process, and tracks which messages are in-flight (handed to a consumer,
    not yet deleted) so two consumer threads/processes never receive the same message at once.

    Storage-engine primitive: only ``LocalQueueHandler`` is expected to call this directly.
    ``enqueue``/``receive``/``delete_by_id`` are not the public queue API.

    There is no separate ``in_flight`` boolean column — ``visible_at`` is the in-flight flag,
    encoded as a timestamp ("in-flight until this instant") rather than a plain bit:

    - Available (safe to hand to a consumer): ``visible_at <= now``.
    - In-flight (already handed to a consumer, not yet acknowledged): ``visible_at > now``.

    A message that is never acknowledged (consumer crashes/raises) needs no reaper: its
    ``visible_at`` was already pushed into the future on receive, so it becomes available
    again by itself once wall-clock time passes it — the same self-expiry SQS's
    visibility-timeout redelivery provides.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: manage transactions explicitly (BEGIN IMMEDIATE/COMMIT/ROLLBACK)
        # instead of the sqlite3 module's implicit transaction handling.
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  queue_name TEXT NOT NULL,
                  body TEXT NOT NULL,
                  attributes TEXT NOT NULL,
                  message_group_id TEXT,
                  message_deduplication_id TEXT,
                  receive_count INTEGER NOT NULL DEFAULT 0,
                  visible_at REAL NOT NULL,
                  created_at REAL NOT NULL
                )
                """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_queue_visible ON messages(queue_name, visible_at)")
        finally:
            conn.close()

    def enqueue(
        self,
        queue_name: str,
        body: Dict[str, Any],
        attributes: Optional[Dict[str, Any]] = None,
        message_group_id: Optional[str] = None,
        message_deduplication_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a new message, immediately available (``visible_at = created_at``)."""
        now = time.time()
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO messages "
                "(queue_name, body, attributes, message_group_id, message_deduplication_id, receive_count, visible_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (queue_name, json.dumps(body), json.dumps(attributes or {}), message_group_id, message_deduplication_id, now, now),
            )
            return {"MessageId": str(cursor.lastrowid)}
        finally:
            conn.close()

    def receive(self, queue_name: str, batch_size: int, visibility_timeout: float) -> List[Dict[str, Any]]:
        """
        Atomically select up to ``batch_size`` available messages and move them in-flight.

        Shaped like a boto3 ``receive_message`` record (``MessageId``, ``Body``,
        ``Attributes: {ApproximateReceiveCount, MessageGroupId, MessageDeduplicationId}``,
        ``MessageAttributes``, ``ReceiptHandle``) so consumers can reuse the same
        attribute-extraction helpers as the AWS containerized deployment almost verbatim.
        """
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, body, attributes, message_group_id, message_deduplication_id, receive_count "
                "FROM messages WHERE queue_name = ? AND visible_at <= ? ORDER BY id LIMIT ?",
                (queue_name, now, batch_size),
            ).fetchall()
            if rows:
                new_visible_at = now + visibility_timeout
                conn.executemany(
                    "UPDATE messages SET receive_count = receive_count + 1, visible_at = ? WHERE id = ?",
                    [(new_visible_at, row[0]) for row in rows],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        messages = []
        for row_id, body, attributes, message_group_id, message_deduplication_id, receive_count in rows:
            messages.append(
                {
                    "MessageId": str(row_id),
                    "Body": body,
                    "Attributes": {
                        "ApproximateReceiveCount": str(receive_count + 1),
                        "MessageGroupId": message_group_id,
                        "MessageDeduplicationId": message_deduplication_id,
                    },
                    "MessageAttributes": json.loads(attributes),
                    "ReceiptHandle": str(row_id),
                }
            )
        return messages

    def delete_by_id(self, message_id: int) -> None:
        """Acknowledge a message. A deleted row can never come back, in-flight or otherwise."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        finally:
            conn.close()
