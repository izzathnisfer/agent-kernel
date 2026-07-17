"""
In-memory Session ID Mapping store for local development and testing.
"""

import logging
from typing import ClassVar, Optional

from .base import SessionIdMappingStore, session_record_key, thread_record_key


class InMemorySessionIdMappingStore(SessionIdMappingStore):
    """
    InMemorySessionIdMappingStore provides an in-memory implementation of the
    SessionIdMappingStore interface.

    Storage is shared across all instances via ClassVar so that mappings persist
    for the lifetime of the process.
    """

    _records: ClassVar[dict[str, str]] = {}
    _log = logging.getLogger("ak.initiation.mapping.inmemory")

    def get_session_id(self, messaging_integration_thread_id: str) -> Optional[str]:
        """
        Resolves a messaging platform thread id to the session id it was bound to.

        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        :return: The mapped session id, or None if no mapping exists.
        """
        return self._records.get(thread_record_key(messaging_integration_thread_id))

    def get_messaging_integration_thread_id(self, session_id: str) -> Optional[str]:
        """
        Resolves a session id to the messaging platform thread id it was bound to.

        :param session_id: The Agent Kernel session id.
        :return: The mapped messaging platform thread id, or None if no mapping exists.
        """
        return self._records.get(session_record_key(session_id))

    def save(self, session_id: str, messaging_integration_thread_id: str) -> None:
        """
        Persists the mapping in both directions. Last-writer-wins and idempotent.

        :param session_id: The Agent Kernel session id.
        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        """
        self._log.debug(f"Saving mapping {session_id} <-> {messaging_integration_thread_id}")
        self._records[thread_record_key(messaging_integration_thread_id)] = session_id
        self._records[session_record_key(session_id)] = messaging_integration_thread_id

    def clear(self) -> None:
        """
        Clears all stored mappings.
        """
        self._records.clear()
