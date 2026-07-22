"""
Client-library-agnostic Session ID Mapping store shared by the Redis and Valkey
backends. This module must not import ``redis`` or ``valkey``: concrete
subclasses construct the driver and pass it in.
"""

import logging
from typing import Optional

from ...util.driver.redis_like import _RedisLikeDriver
from .base import SessionIdMappingStore


class _RedisLikeSessionIdMappingStore(SessionIdMappingStore):
    """
    Redis-compatible implementation of the SessionIdMappingStore interface.

    Each mapping direction is a plain string key under the configured prefix;
    the driver applies the configured TTL atomically on every SET.
    """

    def __init__(self, driver: _RedisLikeDriver, logger_name: str):
        """
        Initializes the store with a connected-on-demand driver.

        :param driver: The Redis-compatible driver (constructed with ``decode_responses=True``).
        :param logger_name: Logger name for the concrete backend.
        """
        self._log = logging.getLogger(logger_name)
        self._driver = driver

    def get_session_id(self, messaging_integration_thread_id: str) -> Optional[str]:
        """
        Resolves a messaging platform thread id to the session id it was bound to.

        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        :return: The mapped session id, or None if no mapping exists.
        """
        return self._driver.get(self._driver.key(SessionIdMappingStore.thread_record_key(messaging_integration_thread_id)))

    def get_messaging_integration_thread_id(self, session_id: str) -> Optional[str]:
        """
        Resolves a session id to the messaging platform thread id it was bound to.

        :param session_id: The Agent Kernel session id.
        :return: The mapped messaging platform thread id, or None if no mapping exists.
        """
        return self._driver.get(self._driver.key(SessionIdMappingStore.session_record_key(session_id)))

    def save(self, session_id: str, messaging_integration_thread_id: str) -> None:
        """
        Persists the mapping in both directions. Last-writer-wins and idempotent.

        :param session_id: The Agent Kernel session id.
        :param messaging_integration_thread_id: The messaging platform's thread identifier.
        """
        self._log.debug(f"Saving mapping {session_id} <-> {messaging_integration_thread_id}")
        self._driver.set(self._driver.key(SessionIdMappingStore.thread_record_key(messaging_integration_thread_id)), session_id)
        self._driver.set(self._driver.key(SessionIdMappingStore.session_record_key(session_id)), messaging_integration_thread_id)

    def clear(self) -> None:
        """
        Clears all stored mappings under the configured prefix.
        """
        self._driver.clear_prefix()
