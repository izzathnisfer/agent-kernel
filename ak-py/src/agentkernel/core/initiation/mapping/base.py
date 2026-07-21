"""
Session ID Mapping store base for agent-initiated conversations.

The mapping associates the session id created by the Agent Runner with the
messaging platform's thread identifier (``thread_ts`` for Slack, chat id for
Telegram, ...) obtained when the Response Handler sends the initiation message.
Both lookup directions must be O(1) on key-value backends, so every mapping is
persisted as two records:

  - ``thread#<messaging_integration_thread_id>`` -> ``session_id``
    (Request Handler: routes inbound replies to the initiated session)
  - ``session#<session_id>`` -> ``messaging_integration_thread_id``
    (Response Handler: threads later agent replies under the platform thread)

``save()`` is last-writer-wins and idempotent; there is no transactional
coupling between the two records (a torn write is repaired by the next bind,
which re-saves when the forward lookup misses).
"""

from abc import ABC, abstractmethod
from typing import Optional

THREAD_RECORD_PREFIX = "thread#"
SESSION_RECORD_PREFIX = "session#"


def thread_record_key(messaging_integration_thread_id: str) -> str:
    """
    Composes the record key for the forward (thread id -> session id) direction.

    :param messaging_integration_thread_id: The messaging platform's thread identifier.
    :return: The record key.
    """
    return f"{THREAD_RECORD_PREFIX}{messaging_integration_thread_id}"


def session_record_key(session_id: str) -> str:
    """
    Composes the record key for the reverse (session id -> thread id) direction.

    :param session_id: The Agent Kernel session id.
    :return: The record key.
    """
    return f"{SESSION_RECORD_PREFIX}{session_id}"


class SessionIdMappingStore(ABC):
    """
    SessionIdMappingStore is the base class for the Session ID Mapping table that
    allows storage and bidirectional retrieval of the
    ``session_id <-> messaging_integration_thread_id`` association.
    """

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
