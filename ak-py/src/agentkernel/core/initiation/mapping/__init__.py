"""
Session ID Mapping store for agent-initiated conversations.

The store backend follows the configured session store type (``session.type``);
connection settings are read from the corresponding ``session.<backend>`` block
and namespace settings (table/collection name, key prefix, TTL) from the
``mapping_table`` block.
"""

import logging

from ...config import AKConfig
from ...util.factory import require_extra
from .base import SessionIdMappingStore


class SessionIdMappingStoreBuilder:
    """
    Builder class for creating SessionIdMappingStore instances based on configuration.

    The backend selection follows ``session.type`` exactly as SessionStoreBuilder
    does — the mapping store always lives next to the session store.
    """

    _log = logging.getLogger("ak.initiation.mapping")

    @staticmethod
    def build() -> SessionIdMappingStore:
        """
        Build and return a SessionIdMappingStore instance for the configured
        session store type.

        :return: The SessionIdMappingStore implementation matching ``session.type``,
            falling back to the in-memory store for unknown types.
        :raises ImportError: If the backend's extra (e.g. ``valkey`` for session.type:
            valkey) is not installed.
        """
        store_type = AKConfig.get().session.type.lower()
        SessionIdMappingStoreBuilder._log.info(f"Building '{store_type}' session id mapping store")
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

        from .in_memory import InMemorySessionIdMappingStore

        return InMemorySessionIdMappingStore()
