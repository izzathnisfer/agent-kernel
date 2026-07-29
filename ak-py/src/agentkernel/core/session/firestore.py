import logging
from typing import Optional

from ..base import Session
from ..config import AKConfig
from ..util.driver.firestore import FirestoreDriver
from .base import MappingStore, SessionCache, SessionStore, build_mapping_store
from .serde import BinarySerde

MAPPING_VALUE_FIELD = "value"


class FirestoreMappingStore(MappingStore):
    """
    Firestore-backed implementation of the MappingStore interface. Requires the
    ``gcp`` extra.

    Layout: one document per mapping direction (document ID = record key) with the
    mapped id stored as bytes under a single ``value`` field. When a TTL is
    configured, the driver sets an ``expiry_time`` field a Firestore TTL policy can
    use to auto-delete expired documents.

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
        value = self._driver.get(record_key, MAPPING_VALUE_FIELD)
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
        self._driver.put(MappingStore.thread_record_key(messaging_integration_thread_id), MAPPING_VALUE_FIELD, session_id.encode("utf-8"))
        self._driver.put(MappingStore.session_record_key(session_id), MAPPING_VALUE_FIELD, messaging_integration_thread_id.encode("utf-8"))

    def clear(self) -> None:
        """
        Clears all stored mappings by deleting every document in the collection.
        """
        self._driver.delete_all()


class FirestoreSessionStore(SessionStore):
    """
    Firestore-backed implementation of SessionStore.

    Document schema (one document per session):
      - Document ID : session_id
      - Fields      : {key: bytes, ...}  (one field per session key)
      - Optional    : expiry_time (datetime) for Firestore TTL auto-deletion

    To enable automatic TTL deletion, configure a TTL policy on the Firestore
    collection pointing to the ``expiry_time`` field in the GCP Console or via
    ``gcloud firestore fields ttls update``.
    """

    def __init__(self, cache: Optional[SessionCache] = None) -> None:
        """
        Initialize the Firestore-backed SessionStore.

        :param cache: An optional SessionCache instance for in-memory caching of sessions.
        """
        self._log = logging.getLogger("ak.core.session.firestore")
        self._serde = BinarySerde()
        cfg = AKConfig.get().session.firestore
        if cfg is None or not cfg.collection_name:
            raise ValueError("AKConfig.session.firestore.collection_name must be set to use FirestoreSessionStore")
        self._driver = FirestoreDriver(
            collection_name=cfg.collection_name,
            project_id=cfg.project_id,
            database_id=cfg.database_id,
            ttl=cfg.ttl,
        )
        self._cache = cache
        self._mapping = build_mapping_store(FirestoreMappingStore)

    def get_mapping_store(self) -> MappingStore:
        """
        Returns the Session ID Mapping store paired with this session store.

        :return: The FirestoreMappingStore sharing this store's connection settings.
        """
        return self._mapping

    def load(self, session_id: str, strict: bool = False) -> Session:
        """
        Load a session by its unique identifier.

        Reads all keys from the Firestore document and reconstructs a Session
        by deserializing each field via BinarySerde.

        :param session_id: Unique identifier for the session.
        :param strict: If True, raises a KeyError if the session is not found.
        :return: The populated Session, or a new Session if not found and strict is False.
        """
        self._log.debug("Loading Firestore session with ID %s", session_id)
        if self._cache:
            session = self._cache.get(session_id)
            if session:
                self._log.debug("Session %s found in cache", session_id)
                return session

        keys = self._driver.get_all_keys(session_id)
        if not keys:
            if strict:
                raise KeyError(f"Session {session_id} not found")
            self._log.warning("Session %s not found, creating new session", session_id)
            return self.new(session_id)

        session = Session(session_id)
        for k in keys:
            payload = self._driver.get(session_id, k)
            if payload is None:
                continue
            session.set(k, self._serde.loads(payload))
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
        if self._cache:
            self._cache.set(session)
        return session

    def store(self, session: Session) -> None:
        """
        Persist all session key/value pairs as fields on the Firestore document.

        :param session: The session to persist.
        """
        for key, value in session.get_all(volatile=False):
            payload = self._serde.dumps(value)
            self._driver.put(session.id, key, payload)
        if self._cache:
            self._cache.set(session)

    def clear(self) -> None:
        """
        Delete all documents from the configured Firestore collection.

        This is a destructive operation intended for development/testing only.
        """
        self._driver.delete_all()
        if self._cache:
            self._cache.clear()
