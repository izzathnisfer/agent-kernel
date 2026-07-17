"""
Firestore-backed Session ID Mapping store. Requires the ``gcp`` extra.

Layout: one document per mapping direction (document ID = record key) with the
mapped id stored as bytes under a single ``value`` field. When a TTL is
configured, the driver sets an ``expiry_time`` field a Firestore TTL policy can
use to auto-delete expired documents.
"""

import logging
from typing import Optional

from ...config import AKConfig
from ...util.driver.firestore import FirestoreDriver
from .base import SessionIdMappingStore, session_record_key, thread_record_key

VALUE_FIELD = "value"


class FirestoreSessionIdMappingStore(SessionIdMappingStore):
    """
    Firestore-backed implementation of the SessionIdMappingStore interface.

    The collection name and TTL come from ``mapping_table``; ``project_id`` and
    ``database_id`` come from ``session.firestore`` when present, otherwise the
    Application Default Credentials / default database are used.
    """

    def __init__(self):
        self._log = logging.getLogger("ak.initiation.mapping.firestore")
        cfg = AKConfig.get()
        mapping_cfg = cfg.mapping_table
        if mapping_cfg is None:
            raise ValueError("mapping_table config block is required to use FirestoreSessionIdMappingStore")
        conn = cfg.session.firestore
        self._driver = FirestoreDriver(
            collection_name=mapping_cfg.collection_name,
            project_id=conn.project_id if conn else None,
            database_id=conn.database_id if conn else None,
            ttl=int(mapping_cfg.ttl),
        )

    def _get_value(self, record_key: str) -> Optional[str]:
        """
        Reads a single record's mapped value by its record key.

        :param record_key: The record key (``thread#...`` or ``session#...``).
        :return: The mapped id, or None if the record does not exist.
        """
        value = self._driver.get(record_key, VALUE_FIELD)
        return value.decode("utf-8") if value is not None else None

    def get_session_id(self, messaging_integration_thread_id: str) -> Optional[str]:
        """
        Resolves a messaging platform thread id to the session id it was bound to.

        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        :return: The mapped session id, or None if no mapping exists.
        """
        return self._get_value(thread_record_key(messaging_integration_thread_id))

    def get_messaging_integration_thread_id(self, session_id: str) -> Optional[str]:
        """
        Resolves a session id to the messaging platform thread id it was bound to.

        :param session_id: The Agent Kernel session id.
        :return: The mapped messaging platform thread id, or None if no mapping exists.
        """
        return self._get_value(session_record_key(session_id))

    def save(self, session_id: str, messaging_integration_thread_id: str) -> None:
        """
        Persists the mapping in both directions. Last-writer-wins and idempotent.

        :param session_id: The Agent Kernel session id.
        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        """
        self._log.debug(f"Saving mapping {session_id} <-> {messaging_integration_thread_id}")
        self._driver.put(thread_record_key(messaging_integration_thread_id), VALUE_FIELD, session_id.encode("utf-8"))
        self._driver.put(session_record_key(session_id), VALUE_FIELD, messaging_integration_thread_id.encode("utf-8"))

    def clear(self) -> None:
        """
        Clears all stored mappings by deleting every document in the collection.
        """
        self._driver.delete_all()
