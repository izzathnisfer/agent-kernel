import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

from agentkernel.deployment.common.response_store import ResponseStore


class LocalResponseStore(ResponseStore):
    """
    SQLite-backed ResponseStore — same file LocalQueue uses, a separate table.

    add_message/get_message/delete_message are implemented directly;
    get_message_with_retry is inherited from the ResponseStore ABC for free.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS responses (
                  request_id TEXT PRIMARY KEY,
                  session_id TEXT,
                  body TEXT,
                  created_at REAL NOT NULL
                )
                """)
        finally:
            conn.close()

    def add_message(self, message: Dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO responses (request_id, session_id, body, created_at) VALUES (?, ?, ?, ?)",
                (message["request_id"], message.get("session_id"), json.dumps(message.get("body")), time.time()),
            )
        finally:
            conn.close()

    def get_message(self, request_id: str, get_and_delete: bool = False) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT request_id, session_id, body FROM responses WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                return None
            if get_and_delete:
                conn.execute("DELETE FROM responses WHERE request_id = ?", (request_id,))
            return {"request_id": row[0], "session_id": row[1], "body": json.loads(row[2]) if row[2] is not None else None}
        finally:
            conn.close()

    def delete_message(self, request_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM responses WHERE request_id = ?", (request_id,))
        finally:
            conn.close()
