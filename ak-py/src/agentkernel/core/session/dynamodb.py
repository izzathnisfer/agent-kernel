import logging
from typing import Optional

from boto3.dynamodb.types import Binary

from ..base import Session
from ..config import AKConfig
from ..util.driver.dynamodb import DynamoDBDriver
from .base import MappingStore, SessionCache, SessionStore
from .serde import BinarySerde

MAPPING_PARTITION_KEY = "map_key"
MAPPING_VALUE_ATTRIBUTE = "value"


class DynamoDBMappingStore(MappingStore):
    """
    DynamoDB-backed implementation of the MappingStore interface.

    Layout: one item per mapping direction in a hash-only table —
    ``{"map_key": <record key>, "value": <mapped id>}``. When a TTL is configured,
    the driver attaches an ``expiry_time`` attribute (UNIX epoch seconds) on put.

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
        self._driver = DynamoDBDriver(table_name=table_name, partition_key=MAPPING_PARTITION_KEY, ttl=int(conn.ttl))

    def _get_value(self, record_key: str) -> Optional[str]:
        """
        Reads a single record's mapped value by its record key.

        :param record_key: The record key (``thread#...`` or ``session#...``).
        :return: The mapped id, or None if the record does not exist.
        """
        item = self._driver.get(record_key)
        return item.get(MAPPING_VALUE_ATTRIBUTE) if item else None

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
        self._driver.put(
            {MAPPING_PARTITION_KEY: MappingStore.thread_record_key(messaging_integration_thread_id), MAPPING_VALUE_ATTRIBUTE: session_id}
        )
        self._driver.put(
            {MAPPING_PARTITION_KEY: MappingStore.session_record_key(session_id), MAPPING_VALUE_ATTRIBUTE: messaging_integration_thread_id}
        )

    def clear(self) -> None:
        """
        Clears all stored mappings by scanning and deleting every item in the table.
        """
        self._driver.clear_all()


class DynamoDBSessionStore(SessionStore):
    """
    DynamoDB-backed implementation of SessionStore.
    Table schema uses:
      - session_id: partition key (string)
      - key: sort key (string)
      - value: binary attribute (serialized using BinarySerde)
    """

    def __init__(self, cache: SessionCache = None):
        """
        Initialize the DynamoDB-backed SessionStore.

        Prepares the serializer and a DynamoDB driver that encapsulates access
        to the configured table.

        :param cache: An optional SessionCache instance for in-memory caching of sessions.
        """
        self._log = logging.getLogger("ak.core.session.dynamodb")
        self._serde = BinarySerde()
        cfg = AKConfig.get().session.dynamodb
        if cfg is None or not cfg.table_name:
            raise ValueError("AKConfig.session.dynamodb.table_name must be set to use DynamoDBSessionStore")
        self._driver = DynamoDBDriver(table_name=cfg.table_name, partition_key="session_id", sort_key="key", ttl=cfg.ttl)
        self._cache = cache
        self._mapping = DynamoDBMappingStore()

    def get_mapping_store(self) -> MappingStore:
        """
        Returns the Session ID Mapping store paired with this session store.

        :return: The DynamoDBMappingStore sharing this store's connection settings.
        """
        return self._mapping

    def load(self, session_id: str, strict: bool = False) -> Session:
        """
        Load a session by its unique identifier.

        Reads all keys for the session from DynamoDB and reconstructs a Session
        by deserializing each value via BinarySerde.

        :param session_id: Unique identifier for the session.
        :param strict: If True, raises a KeyError if the session is not found.
        :return: The populated Session, or a new Session if not found and strict is False.
        """
        self._log.debug(f"Loading dynamodb session with ID {session_id}")
        if self._cache:
            session = self._cache.get(session_id)
            if session:
                self._log.debug(f"Session {session_id} found in cache")
                return session
        keys = self._driver.query_sort_keys(session_id)
        if not keys:
            if strict:
                raise KeyError(f"Session {session_id} not found")
            self._log.warning("Session %s not found, creating new session", session_id)
            return self.new(session_id)

        session = Session(session_id)
        for k in keys:
            item = self._driver.get(session_id, k)
            payload = self._unwrap(item)
            if payload is None:
                continue
            session.set(k, self._serde.loads(payload))
        if self._cache:
            self._cache.set(session)
        return session

    @staticmethod
    def _unwrap(item) -> bytes:
        """Extract the raw bytes payload from an item's value attribute, unwrapping
        boto3 Binary objects."""
        if not item:
            return None
        val = item.get("value")
        # boto3 Binary objects expose .value or are bytes-like
        if hasattr(val, "value"):
            return val.value
        return val

    def new(self, session_id: str) -> Session:
        """
        Initialize a new, empty Session instance

        :param session_id: Unique identifier for the session.
        :return: A new Session instance for the provided identifier.
        """
        self._log.debug("Creating new session with ID %s", session_id)
        session = Session(session_id)
        if self._cache:
            self._cache.set(session)
        return session

    def store(self, session: Session) -> None:
        """
        Persist all session key/value pairs as individual DynamoDB items.
        :param session: The session to persist.
        """
        for key, value in session.get_all(volatile=False):
            payload = self._serde.dumps(value)
            self._driver.put({"session_id": session.id, "key": key, "value": Binary(payload)})
        if self._cache:
            self._cache.set(session)

    def clear(self) -> None:
        """
        Clear all items from the configured DynamoDB table.

        This is a destructive operation intended for development/testing only.
        """
        self._driver.clear_all()
        if self._cache:
            self._cache.clear()
