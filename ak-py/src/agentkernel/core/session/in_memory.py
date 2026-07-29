import logging
from typing import ClassVar, Optional

from ..base import Session
from .base import MappingStore, SessionStore, build_mapping_store


class InMemoryMappingStore(MappingStore):
    """
    InMemoryMappingStore provides an in-memory implementation of the
    MappingStore interface.

    Storage is shared across all instances via ClassVar so that mappings persist
    for the lifetime of the process.
    """

    _records: ClassVar[dict[str, str]] = {}
    _log = logging.getLogger("ak.core.session.mapping.inmemory")

    def get_session_id(self, messaging_integration_thread_id: str) -> Optional[str]:
        """
        Resolves a messaging platform thread id to the session id it was bound to.

        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        :return: The mapped session id, or None if no mapping exists.
        """
        return self._records.get(MappingStore.thread_record_key(messaging_integration_thread_id))

    def get_messaging_integration_thread_id(self, session_id: str) -> Optional[str]:
        """
        Resolves a session id to the messaging platform thread id it was bound to.

        :param session_id: The Agent Kernel session id.
        :return: The mapped messaging platform thread id, or None if no mapping exists.
        """
        return self._records.get(MappingStore.session_record_key(session_id))

    def save(self, session_id: str, messaging_integration_thread_id: str) -> None:
        """
        Persists the mapping in both directions. Last-writer-wins and idempotent.

        :param session_id: The Agent Kernel session id.
        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        """
        self._log.debug(f"Saving mapping {session_id} <-> {messaging_integration_thread_id}")
        self._records[MappingStore.thread_record_key(messaging_integration_thread_id)] = session_id
        self._records[MappingStore.session_record_key(session_id)] = messaging_integration_thread_id

    def clear(self) -> None:
        """
        Clears all stored mappings.
        """
        self._records.clear()


class InMemorySessionStore(SessionStore):
    """
    InMemorySessionStore class provides an in-memory implementation of the SessionStore interface.
    """

    def __init__(self):
        """
        Initializes an InMemorySessionStore instance.
        """
        self._sessions = {}
        self._log = logging.getLogger("ak.core.session.inmemory")
        self._mapping = build_mapping_store(InMemoryMappingStore)

    def get_mapping_store(self) -> MappingStore:
        """
        Returns the Session ID Mapping store paired with this session store.

        :return: The InMemoryMappingStore, whose records share this process like the sessions do.
        """
        return self._mapping

    def load(self, session_id: str, strict: bool = False) -> Session:
        """
        Loads a session by its unique identifier.
        :param session_id: Unique identifier for the session.
        :param strict: If True, raises an exception if the session is not found.
        :return: The session associated with the identifier, or a new session if it does not exist.
        """
        self._log.debug(f"Loading in-memory session with ID {session_id}")
        session = self._sessions.get(session_id)
        if session is None:
            if strict:
                raise KeyError(f"Session {session_id} not found")
            else:
                self._log.warning(f"Session {session_id} not found, creating new session")
                session = self.new(session_id)
        return session

    def new(self, session_id: str) -> Session:
        """
        Initialize a session for a given session id.
        :param session_id: Unique identifier for the session.
        :return: The session associated with the identifier, or a new session if it does not exist.
        """
        self._log.debug(f"Creating new session with ID {session_id} ")
        session = Session(session_id)
        self.store(session)

        return session

    def store(self, session: Session) -> None:
        """
        Stores a session or updates it if it already exists in the storage.
        :param session: The session to store.
        """
        self._log.debug(f"Storing session with ID {session.id}")
        self._sessions[session.id] = session

    def clear(self) -> None:
        """
        Clears all stored sessions.
        """
        self._log.debug("Clearing all stored sessions")
        self._sessions.clear()
