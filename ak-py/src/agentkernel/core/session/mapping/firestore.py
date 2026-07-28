"""
Firestore-backed Session ID Mapping store. Requires the ``gcp`` extra.

Layout: one document per mapping direction (document ID = record key) with the
mapped id stored as bytes under a single ``value`` field. When a TTL is
configured, the driver sets an ``expiry_time`` field a Firestore TTL policy can
use to auto-delete expired documents.

The collection name is derived by suffixing the session store's collection name
with ``-id-mapping``; ``project_id``, ``database_id``, and TTL follow
``session.firestore``.
"""

import logging
from typing import Optional

from ...config import AKConfig
from ...util.driver.firestore import FirestoreDriver
from ..base import MappingStore

VALUE_FIELD = "value"


class FirestoreMappingStore(MappingStore):
    """
    Firestore-backed implementation of the MappingStore interface.

    The collection name is derived from the session store's collection name;
    ``project_id``, ``database_id``, and TTL come from ``session.firestore``.
    """

    def __init__(self):
        self._log = logging.getLogger("ak.core.session.mapping.firestore")
        conn = AKConfig.get().session.firestore
        if conn is None:
            raise ValueError("session.firestore config block is required to use FirestoreMappingStore")
        collection_name = f"{conn.collection_name}-id-mapping"
        self._driver = FirestoreDriver(
            collection_name=collection_name,
            project_id=conn.project_id,
            database_id=conn.database_id,
            ttl=int(conn.ttl),
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
        return self._get_value(MappingStore.thread_record_key(messaging_integration_thread_id))

    def get_messaging_integration_thread_id(self, session_id: str) -> Optional[str]:
        """
        Resolves a session id to the messaging platform thread id it was bound to.

        :param session_id: The Agent Kernel session id.
        :return: The mapped messaging platform thread id, or None if no mapping exists.
        """
        return self._get_value(MappingStore.session_record_key(session_id))

    def save(self, session_id: str, messaging_integration_thread_id: str) -> None:
        """
        Persists the mapping in both directions. Last-writer-wins and idempotent.

        :param session_id: The Agent Kernel session id.
        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        """
        self._log.debug(f"Saving mapping {session_id} <-> {messaging_integration_thread_id}")
        self._driver.put(MappingStore.thread_record_key(messaging_integration_thread_id), VALUE_FIELD, session_id.encode("utf-8"))
        self._driver.put(MappingStore.session_record_key(session_id), VALUE_FIELD, messaging_integration_thread_id.encode("utf-8"))

    def clear(self) -> None:
        """
        Clears all stored mappings by deleting every document in the collection.
        """
        self._driver.delete_all()
