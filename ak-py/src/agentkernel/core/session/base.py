from abc import ABC, abstractmethod
from collections import OrderedDict
from threading import RLock
from typing import ClassVar, Optional

from ..base import Session


class MappingStore(ABC):
    """
    MappingStore is the base class for the Session ID Mapping table that allows storage
    and bidirectional retrieval of the ``session_id <-> messaging_integration_thread_id``
    association used by agent-initiated conversations.

    The mapping associates the session id created by the Agent Runner with the messaging
    platform's thread identifier (``thread_ts`` for Slack, chat id for Telegram, ...)
    obtained when the Response Handler sends the initiation message. Both lookup directions
    must be O(1) on key-value backends, so every mapping is persisted as two records:

      - ``thread#<messaging_integration_thread_id>`` -> ``session_id``
        (Request Handler: routes inbound replies to the initiated session)
      - ``session#<session_id>`` -> ``messaging_integration_thread_id``
        (Response Handler: threads later agent replies under the platform thread)

    ``save()`` is last-writer-wins and idempotent; there is no transactional coupling
    between the two records (a torn write is repaired by the next bind, which re-saves when
    the forward lookup misses).

    Implementations live in ``core/session/mapping/`` and are paired with a session store
    backend, which hands its own out via :meth:`SessionStore.get_mapping_store`.
    """

    THREAD_RECORD_PREFIX: ClassVar[str] = "thread#"
    SESSION_RECORD_PREFIX: ClassVar[str] = "session#"

    @staticmethod
    def thread_record_key(messaging_integration_thread_id: str) -> str:
        """
        Composes the record key for the forward (thread id -> session id) direction.

        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        :return: The record key.
        """
        return f"{MappingStore.THREAD_RECORD_PREFIX}{messaging_integration_thread_id}"

    @staticmethod
    def session_record_key(session_id: str) -> str:
        """
        Composes the record key for the reverse (session id -> thread id) direction.

        :param session_id: The Agent Kernel session id.
        :return: The record key.
        """
        return f"{MappingStore.SESSION_RECORD_PREFIX}{session_id}"

    @abstractmethod
    def get_session_id(self, messaging_integration_thread_id: str) -> Optional[str]:
        """
        Resolves a messaging platform thread id to the session id it was bound to.

        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        :return: The mapped session id, or None if no mapping exists.
        """
        pass

    @abstractmethod
    def get_messaging_integration_thread_id(self, session_id: str) -> Optional[str]:
        """
        Resolves a session id to the messaging platform thread id it was bound to.

        :param session_id: The Agent Kernel session id.
        :return: The mapped messaging platform thread id, or None if no mapping exists.
        """
        pass

    @abstractmethod
    def save(self, session_id: str, messaging_integration_thread_id: str) -> None:
        """
        Persists the mapping in both directions. Last-writer-wins and idempotent.

        :param session_id: The Agent Kernel session id.
        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        """
        pass

    @abstractmethod
    def clear(self) -> None:
        """
        Clears all stored mappings.
        """
        pass


class SessionStore(ABC):
    """
    SessionStore is the base class for session storage that allows storage and retrieval of session
    data.
    """

    @abstractmethod
    def new(self, session_id: str) -> Session:
        """
        Initialize a session for a given session id.
        :param session_id: Unique identifier for the session.
        :return: The session associated with the identifier, or a new session if it does not exist.
        """
        pass

    @abstractmethod
    def load(self, session_id: str, strict: bool = False) -> Session:
        """
        Loads a session by its unique identifier.
        :param session_id: Unique identifier for the session.
        :param strict: If True, raises an exception if the session is not found.
        :return: The session associated with the identifier, or a new session if it does not exist
        in storage.
        """
        pass

    @abstractmethod
    def store(self, session: Session) -> None:
        """
        Stores a session or update it if it already exists in the storage.
        :param session: The session to store.
        """
        pass

    @abstractmethod
    def clear(self) -> None:
        """
        Clears all stored sessions.
        """
        pass

    @abstractmethod
    def get_mapping_store(self) -> MappingStore:
        """
        Returns the Session ID Mapping store paired with this session store, used by
        agent-initiated conversations to bind an initiated session to a messaging platform
        thread id.

        Abstract on purpose: every session store must supply one, so adding a backend
        without its mapping counterpart fails at class instantiation rather than silently
        leaving agent-initiated conversations broken for whoever enables them. Built-in
        backends construct theirs with ``build_mapping_store()`` so the two share one
        connection, namespace and TTL; a bring-your-own store must do the same, and this
        is a breaking change for any that predates the method.

        :return: The MappingStore paired with this session store.
        """
        pass


class SessionCache:
    """
    SessionCache is an in-memory cache for Session objects, with a maximum size limit.
    When the cache exceeds the maximum size, the least recently used session is removed.
    """

    def __init__(self, capacity: int = 256):
        """
        Initialize the session cache with a specified capacity.
        :param capacity (int, optional): The maximum number of sessions the cache can hold (default is 256).
        """
        super().__init__()
        self._lock: RLock = RLock()
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._capacity = capacity

    def capacity(self) -> int:
        """
        Get the maximum capacity of the session cache.
        :return int: The maximum number of items the session can hold.
        """
        return self._capacity

    def size(self) -> int:
        """
        Get the current size of the session cache.
        :return int: The current number of items in the session cache.
        """
        with self._lock:
            return len(self._cache)

    def set(self, session: Session) -> None:
        """
        Store a session in the cache with the given key.

        If the session already exists, it is replaced. Otherwise, if the cache
        is at capacity, the least recently used session is removed before adding
        the new session. In either case the session is marked as most recently used.

        :param session: The session object to be stored in the cache.
        """
        with self._lock:
            if session.id in self._cache:
                del self._cache[session.id]
            elif len(self._cache) >= self._capacity:
                self._cache.popitem(last=False)
            self._cache.__setitem__(session.id, session)

    def get(self, id: str) -> Session | None:
        """
        Retrieve a session by key and update its access order.

        The retrieved session is marked as most recently used.

        :param id (str): The unique identifier for the session to retrieve.
        :return Session | None: The session object if found, None otherwise.
        """
        with self._lock:
            if id in self._cache:
                self._cache.move_to_end(id)
                return self._cache[id]
            return None

    def clear(self) -> None:
        """
        Clear all sessions from the cache.
        """
        with self._lock:
            self._cache.clear()
