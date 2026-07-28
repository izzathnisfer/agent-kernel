"""
Azure Cosmos DB (Table API)-backed Session ID Mapping store. Requires the
``azure`` extra.

Layout: one entity per mapping direction — PartitionKey = record key, a fixed
RowKey, and the mapped id stored as bytes in the ``value`` property. TTL is not
supported on this backend (matching the Cosmos DB thread store).
"""

import logging
from typing import Optional

from ...config import AKConfig
from ...util.driver.cosmosdb import CosmosDBDriver
from ..base import MappingStore

ROW_KEY = "value"


class CosmosDBMappingStore(MappingStore):
    """
    Cosmos DB Table API-backed implementation of the MappingStore interface.

    The table name is derived by suffixing the session store's table name with
    ``-id-mapping``; the connection string comes from ``session.cosmosdb``.
    """

    def __init__(self):
        self._log = logging.getLogger("ak.core.session.mapping.cosmosdb")
        conn = AKConfig.get().session.cosmosdb
        if conn is None:
            raise ValueError("session.cosmosdb config block is required to use CosmosDBMappingStore")
        table_name = f"{conn.table_name}-id-mapping"
        self._driver = CosmosDBDriver(connection_string=conn.connection_string, table_name=table_name)

    def _get_value(self, record_key: str) -> Optional[str]:
        """
        Reads a single record's mapped value by its record key.

        :param record_key: The record key (``thread#...`` or ``session#...``).
        :return: The mapped id, or None if the record does not exist.
        """
        value = self._driver.get(record_key, ROW_KEY)
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
        self._driver.put(MappingStore.thread_record_key(messaging_integration_thread_id), ROW_KEY, session_id.encode("utf-8"))
        self._driver.put(MappingStore.session_record_key(session_id), ROW_KEY, messaging_integration_thread_id.encode("utf-8"))

    def clear(self) -> None:
        """
        Clears all stored mappings by deleting every entity in the table.
        """
        self._driver.clear_all()
