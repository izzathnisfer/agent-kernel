"""
Session ID Mapping store for agent-initiated conversations.

The store backend follows the configured session store type (``session.type``) and
shares its connection settings; namespace (table/collection name, key prefix) is
derived from the session store's own and TTL is reused from it — see each backend
module for the exact derivation. Set ``conversation_initiation.store`` to a dotted path to bring
your own ``SessionIdMappingStore`` subclass instead.
"""

import logging

from ...config import AKConfig
from ...util.factory import AKConfigError, require_extra, resolve_dotted
from .base import SessionIdMappingStore

_BUILTIN_SESSION_ID_MAPPING_STORES = ["in_memory", "redis", "valkey", "dynamodb", "cosmosdb", "firestore"]


class SessionIdMappingStoreBuilder:
    """
    Builder class for creating SessionIdMappingStore instances based on configuration.

    The backend selection follows ``session.type`` exactly as SessionStoreBuilder
    does — the mapping store always lives next to the session store — unless
    ``conversation_initiation.store`` names a bring-your-own ``SessionIdMappingStore`` subclass.
    """

    _log = logging.getLogger("ak.initiation.mapping")

    @staticmethod
    def build() -> SessionIdMappingStore:
        """
        Build and return a SessionIdMappingStore instance.

        :return: A bring-your-own store when ``conversation_initiation.store`` is set, otherwise the
            built-in SessionIdMappingStore implementation matching ``session.type``.
        :raises ImportError: If the backend's extra (e.g. ``valkey`` for session.type:
            valkey) is not installed.
        :raises AKConfigError: If ``session.type`` is neither a built-in short name nor
            resolvable via ``conversation_initiation.store``.
        """
        config = AKConfig.get()
        store_path = config.conversation_initiation.store
        if store_path:
            SessionIdMappingStoreBuilder._log.info(f"Building session id mapping store from dotted path '{store_path}'")
            return resolve_dotted(store_path, base=SessionIdMappingStore)()

        store_type = config.session.type.lower()
        SessionIdMappingStoreBuilder._log.info(f"Building '{store_type}' session id mapping store")
        if store_type == "in_memory":
            from .in_memory import InMemorySessionIdMappingStore

            return InMemorySessionIdMappingStore()
        if store_type == "redis":
            with require_extra("redis", "session.type: redis"):
                from .redis import RedisSessionIdMappingStore

            return RedisSessionIdMappingStore()
        if store_type == "valkey":
            with require_extra("valkey", "session.type: valkey"):
                from .valkey import ValkeySessionIdMappingStore

            return ValkeySessionIdMappingStore()
        if store_type == "dynamodb":
            with require_extra("aws", "session.type: dynamodb"):
                from .dynamodb import DynamoDBSessionIdMappingStore

            return DynamoDBSessionIdMappingStore()
        if store_type == "cosmosdb":
            with require_extra("azure", "session.type: cosmosdb"):
                from .cosmosdb import CosmosDBSessionIdMappingStore

            return CosmosDBSessionIdMappingStore()
        if store_type == "firestore":
            with require_extra("gcp", "session.type: firestore"):
                from .firestore import FirestoreSessionIdMappingStore

            return FirestoreSessionIdMappingStore()

        raise AKConfigError(
            f"unknown session store type '{store_type}' for the Session ID Mapping store; expected one of "
            f"{_BUILTIN_SESSION_ID_MAPPING_STORES} or set conversation_initiation.store to a dotted path to a SessionIdMappingStore subclass"
        )
