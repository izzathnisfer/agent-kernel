"""
DynamoDB-backed Session ID Mapping store.

Layout: one item per mapping direction in a hash-only table —
``{"map_key": <record key>, "value": <mapped id>}``. When a TTL is configured,
the driver attaches an ``expiry_time`` attribute (UNIX epoch seconds) on put.
"""

import logging
from typing import Optional

from ...config import AKConfig
from ...util.driver.dynamodb import DynamoDBDriver
from ..base import MappingStore

PARTITION_KEY = "map_key"
VALUE_ATTRIBUTE = "value"


class DynamoDBMappingStore(MappingStore):
    """
    DynamoDB-backed implementation of the MappingStore interface.

    The table name is derived by suffixing the session store's table name with
    ``-id-mapping``; TTL and the AWS connection follow ``session.dynamodb``, as
    the DynamoDB session store does.
    """

    def __init__(self):
        self._log = logging.getLogger("ak.core.session.mapping.dynamodb")
        conn = AKConfig.get().session.dynamodb
        if conn is None:
            raise ValueError("session.dynamodb config block is required to use DynamoDBMappingStore")
        table_name = f"{conn.table_name}-id-mapping"
        self._driver = DynamoDBDriver(table_name=table_name, partition_key=PARTITION_KEY, ttl=int(conn.ttl))

    def _get_value(self, record_key: str) -> Optional[str]:
        """
        Reads a single record's mapped value by its record key.

        :param record_key: The record key (``thread#...`` or ``session#...``).
        :return: The mapped id, or None if the record does not exist.
        """
        item = self._driver.get(record_key)
        return item.get(VALUE_ATTRIBUTE) if item else None

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
        self._driver.put({PARTITION_KEY: MappingStore.thread_record_key(messaging_integration_thread_id), VALUE_ATTRIBUTE: session_id})
        self._driver.put({PARTITION_KEY: MappingStore.session_record_key(session_id), VALUE_ATTRIBUTE: messaging_integration_thread_id})

    def clear(self) -> None:
        """
        Clears all stored mappings by scanning and deleting every item in the table.
        """
        self._driver.clear_all()
