"""
InitiationManager — service façade for agent-initiated conversations (AK-134).

Owns the Session ID Mapping lifecycle: resolving inbound platform thread ids to
session ids (Request Handler direction), binding new mappings and initializing
the AK conversation thread after a successful send (Response Handler direction),
and dispatching InitiationMessages out of the Agent Runner. A single shared
instance is used by all handlers; ``get()`` returns None when the feature is
disabled (no ``mapping_table`` config block).
"""

import logging
from abc import ABC, abstractmethod
from threading import RLock
from typing import Callable, Optional

from ..config import AKConfig
from ..thread import ConversationThreadManager
from .mapping import SessionIdMappingStoreBuilder
from .mapping.base import SessionIdMappingStore
from .model import InitiationMessage


class InitiationSender(ABC):
    """
    Single-process REST sender contract: the overridable send method for
    initiation messages. Queue deployments deliver through the response
    handler's ``process_message`` override instead — there is no response
    handler process in single-process REST.
    """

    @abstractmethod
    def send_initiation_message(self, target: str, message: str, target_details: Optional[dict] = None) -> str:
        """
        Sends the initiation message to the user on the messaging platform.

        :param target: Opaque recipient address from the initiation request.
        :param message: The outbound message text.
        :param target_details: Opaque platform extras from the initiation request.
        :return: The messaging_integration_thread_id obtained from the platform send.
        """
        pass


class InitiationManager:
    """
    Service façade owning the Session ID Mapping and initiation dispatch.
    """

    _instance: Optional["InitiationManager"] = None
    _dispatcher: Optional[Callable[[InitiationMessage], None]] = None
    _lock: RLock = RLock()
    _log = logging.getLogger("ak.initiation.manager")

    def __init__(self, store: SessionIdMappingStore):
        """
        Initializes an InitiationManager instance.
        :param store: The SessionIdMappingStore backend to persist mappings in.
        """
        self._store = store

    @classmethod
    def get(cls) -> Optional["InitiationManager"]:
        """
        Return the shared InitiationManager instance, or None when agent-initiated
        conversations are not configured (no 'mapping_table' block). Callers use
        the None check as the feature-enabled check.
        :return: The shared instance, or None if the feature is disabled.
        """
        if AKConfig.get().mapping_table is None:
            return None
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(store=SessionIdMappingStoreBuilder.build())
            return cls._instance

    @classmethod
    def register_dispatcher(cls, dispatcher: Callable[[InitiationMessage], None]) -> None:
        """
        Register the dispatcher that carries InitiationMessages out of the Agent
        Runner (queue deployments enqueue to the output queue; single-process REST
        sends and binds in-process). Call once at startup; safe to call whether or
        not the feature is enabled.
        :param dispatcher: Callable invoked with each InitiationMessage.
        """
        with cls._lock:
            cls._dispatcher = dispatcher

    @classmethod
    def reset(cls) -> None:
        """
        Drop the shared instance and any registered dispatcher so the next get()
        rebuilds from config. Intended for testing.
        """
        with cls._lock:
            cls._instance = None
            cls._dispatcher = None

    def resolve_session_id(self, messaging_integration_thread_id: str) -> str:
        """
        Resolves an inbound messaging platform thread id to its mapped session id.
        On a mapping miss — or any mapping store error — the given id is returned
        unchanged, preserving today's platform-derived session behavior
        (availability over continuity).

        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        :return: The mapped session id, or the given id when no mapping applies.
        """
        try:
            mapped = self._store.get_session_id(messaging_integration_thread_id)
        except Exception:
            self._log.exception(f"Session ID Mapping lookup failed for thread id {messaging_integration_thread_id}; using the id as-is")
            return messaging_integration_thread_id
        return mapped if mapped is not None else messaging_integration_thread_id

    def get_messaging_integration_thread_id(self, session_id: str) -> Optional[str]:
        """
        Resolves a session id to the messaging platform thread id it was bound to.
        Used by reply-delivery overrides to thread later agent replies of an
        initiated conversation. Returns None on a miss — or any mapping store
        error — which callers treat as "not an initiated conversation".

        :param session_id: The Agent Kernel session id.
        :return: The mapped messaging platform thread id, or None.
        """
        try:
            return self._store.get_messaging_integration_thread_id(session_id)
        except Exception:
            self._log.exception(f"Session ID Mapping reverse lookup failed for session {session_id}")
            return None

    def bind(self, session_id: str, messaging_integration_thread_id: str) -> None:
        """
        Persists the mapping if one does not already exist for the thread id.
        Store errors propagate — complete() is the never-raising wrapper.

        :param session_id: The Agent Kernel session id created at initiation.
        :param messaging_integration_thread_id: The thread id obtained from the platform send.
        """
        if self._store.get_session_id(messaging_integration_thread_id) is not None:
            self._log.debug(f"Mapping for thread id {messaging_integration_thread_id} already exists, skipping save")
            return
        self._store.save(session_id, messaging_integration_thread_id)

    def complete(self, initiation: InitiationMessage, messaging_integration_thread_id: str) -> None:
        """
        Post-send completion: bind the mapping (if absent) and initialize the AK
        conversation thread when thread support is enabled — the thread is owned
        by the recipient (initiation.user_id), named by the configured naming
        strategy from the outbound message, and seeded with the outbound message
        as its first assistant message.

        NEVER raises: internal failures are caught and logged so a caller's queue
        message is not redelivered after a successful platform send (redelivery
        would message the user twice). A lost bind degrades to platform-derived
        session ids on replies.

        :param initiation: The InitiationMessage that was sent.
        :param messaging_integration_thread_id: The thread id obtained from the platform send.
        """
        try:
            self.bind(initiation.session_id, messaging_integration_thread_id)
        except Exception:
            self._log.exception(f"Failed to bind mapping for initiated session {initiation.session_id}; replies will not resolve to this session")
        try:
            thread_manager = ConversationThreadManager.get()
            if thread_manager is not None:
                thread_manager.get_or_create_thread(
                    session_id=initiation.session_id,
                    user_id=initiation.user_id,
                    first_prompt=initiation.message,
                )
                thread_manager.append_message(initiation.session_id, "assistant", initiation.message)
        except Exception:
            self._log.exception(f"Failed to initialize conversation thread for initiated session {initiation.session_id}")

    def dispatch(self, initiation: InitiationMessage) -> None:
        """
        Hands an InitiationMessage to the registered dispatcher.

        :param initiation: The InitiationMessage to dispatch.
        :raises ValueError: If no dispatcher has been registered in this process.
        """
        dispatcher = InitiationManager._dispatcher
        if dispatcher is None:
            raise ValueError("No initiation dispatcher is registered — conversation initiation is not available in this process")
        self._log.debug(f"Dispatching initiation for session {initiation.session_id}")
        dispatcher(initiation)


class SessionIdResolver:
    """
    Mixin providing the special overridable Request Handler method that maps an
    inbound messaging_integration_thread_id to its session id. The default
    implementation consults the Session ID Mapping and falls back to the
    platform-derived id (today's behavior); override to customize the mapping logic.
    """

    def resolve_session_id(self, messaging_integration_thread_id: str) -> str:
        """
        Resolves an inbound messaging platform thread id to the session id to run under.

        :param messaging_integration_thread_id: The platform-derived conversation identifier.
        :return: The mapped session id, or the given id when the feature is disabled
            or no mapping exists.
        """
        manager = InitiationManager.get()
        if manager is None:
            return messaging_integration_thread_id
        return manager.resolve_session_id(messaging_integration_thread_id)
