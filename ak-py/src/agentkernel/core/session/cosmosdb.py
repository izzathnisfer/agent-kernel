import logging
from typing import Optional

from ..base import Session
from ..config import AKConfig
from ..util.driver.cosmosdb import CosmosDBDriver
from .base import MappingStore, SessionCache, SessionStore
from .serde import BinarySerde

MAPPING_ROW_KEY = "value"


class CosmosDBMappingStore(MappingStore):
    """
    Azure Cosmos DB (Table API)-backed implementation of the MappingStore
    interface. Requires the ``azure`` extra.

    Layout: one entity per mapping direction — PartitionKey = record key, a fixed
    RowKey, and the mapped id stored as bytes in the ``value`` property. TTL is not
    supported on this backend (matching the Cosmos DB thread store).

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
        value = self._driver.get(record_key, MAPPING_ROW_KEY)
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
        self._driver.put(MappingStore.thread_record_key(messaging_integration_thread_id), MAPPING_ROW_KEY, session_id.encode("utf-8"))
        self._driver.put(MappingStore.session_record_key(session_id), MAPPING_ROW_KEY, messaging_integration_thread_id.encode("utf-8"))

    def clear(self) -> None:
        """
        Clears all stored mappings by deleting every entity in the table.
        """
        self._driver.clear_all()


class CosmosDBSessionStore(SessionStore):
    """
    Cosmos DB Table API-backed implementation of SessionStore.
    Table schema uses:
      - PartitionKey: session_id (string)
      - RowKey: key (string)
      - value: binary attribute (serialized using BinarySerde)
      - CreatedAt: optional timestamp for TTL management (UNIX epoch seconds)
      - ExpiresIn: optional TTL value in seconds

    Note: Property names 'Timestamp' and 'TTL' are reserved in Cosmos DB Table API.
    """

    def __init__(self, cache: SessionCache = None):
        """
        Initialize the Cosmos DB-backed SessionStore.

        Prepares the serializer and a Cosmos DB driver that encapsulates access
        to the configured table.

        :param cache: An optional SessionCache instance for in-memory caching of sessions.
        """
        self._log = logging.getLogger("ak.core.session.cosmosdb")
        self._serde = BinarySerde()
        cfg = AKConfig.get().session.cosmosdb
        if cfg is None or not cfg.connection_string:
            raise ValueError("AKConfig.session.cosmosdb.connection_string must be set to use CosmosDBSessionStore")
        if not cfg.table_name:
            raise ValueError("AKConfig.session.cosmosdb.table_name must be set to use CosmosDBSessionStore")
        self._driver = CosmosDBDriver(connection_string=cfg.connection_string, table_name=cfg.table_name, ttl=cfg.ttl)
        self._cache = cache
        self._mapping = CosmosDBMappingStore()

    def get_mapping_store(self) -> MappingStore:
        """
        Returns the Session ID Mapping store paired with this session store.

        :return: The CosmosDBMappingStore sharing this store's connection settings.
        """
        return self._mapping

    def load(self, session_id: str, strict: bool = False) -> Session:
        """
        Load a session by its unique identifier.

        Reads all keys for the session from Cosmos DB and reconstructs a Session
        by deserializing each value via BinarySerde.

        :param session_id: Unique identifier for the session.
        :param strict: If True, raises a KeyError if the session is not found.
        :return: The populated Session, or a new Session if not found and strict is False.
        """
        self._log.debug(f"Loading Cosmos DB session with ID {session_id}")

        # Check cache first
        if self._cache:
            session = self._cache.get(session_id)
            if session:
                self._log.debug(f"Session {session_id} found in cache")
                return session

        # Query all keys for this session
        keys = self._driver.query_sort_keys(session_id)

        if not keys:
            if strict:
                raise KeyError(f"Session {session_id} not found")
            self._log.warning("Session %s not found, creating new session", session_id)
            return self.new(session_id)

        # Reconstruct session from stored data
        session = Session(session_id)
        for k in keys:
            payload = self._driver.get(session_id, k)
            if payload is None:
                continue
            session.set(k, self._serde.loads(payload))

        # Update cache
        if self._cache:
            self._cache.set(session)

        return session

    def new(self, session_id: str) -> Session:
        """
        Initialize a new, empty Session instance.

        :param session_id: Unique identifier for the session.
        :return: A new Session instance for the provided identifier.
        """
        self._log.debug("Creating new session with ID %s", session_id)
        session = Session(session_id)

        # Update cache
        if self._cache:
            self._cache.set(session)

        return session

    def store(self, session: Session) -> None:
        """
        Persist all session key/value pairs as individual Cosmos DB entities.

        :param session: The session to persist.
        """
        for key, value in session.get_all(volatile=False):
            payload = self._serde.dumps(value)
            self._driver.put(session.id, key, payload)

        # Update cache
        if self._cache:
            self._cache.set(session)

    def clear(self) -> None:
        """
        Clear all entities from the configured Cosmos DB table.

        This is a destructive operation intended for development/testing only.
        """
        self._driver.clear_all()

        # Clear cache
        if self._cache:
            self._cache.clear()
