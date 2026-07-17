"""
Session ID Mapping store for agent-initiated conversations.

The store backend follows the configured session store type (``session.type``);
connection settings are read from the corresponding ``session.<backend>`` block
and namespace settings (table/collection name, key prefix, TTL) from the
``mapping_table`` block.
"""

import logging

from ...builder import SessionStoreBuilder
from ...config import AKConfig
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
        :raises ImportError: If session.type is valkey and the ``valkey`` extra is
            not installed.
        """
        store_type = SessionStoreBuilder.Types.from_str(AKConfig.get().session.type)
        SessionIdMappingStoreBuilder._log.info(f"Building {store_type} session id mapping store")
        if store_type == SessionStoreBuilder.Types.REDIS:
            from .redis import RedisSessionIdMappingStore

            return RedisSessionIdMappingStore()
        elif store_type == SessionStoreBuilder.Types.VALKEY:
            try:
                from .valkey import ValkeySessionIdMappingStore
            except ImportError as e:
                raise ImportError(
                    "The 'valkey' package is required for session.type: valkey. Install it with: pip install agentkernel[valkey]"
                ) from e

            return ValkeySessionIdMappingStore()
        elif store_type == SessionStoreBuilder.Types.DYNAMODB:
            from .dynamodb import DynamoDBSessionIdMappingStore

            return DynamoDBSessionIdMappingStore()
        elif store_type == SessionStoreBuilder.Types.COSMOSDB:
            from .cosmosdb import CosmosDBSessionIdMappingStore

            return CosmosDBSessionIdMappingStore()
        elif store_type == SessionStoreBuilder.Types.FIRESTORE:
            from .firestore import FirestoreSessionIdMappingStore

            return FirestoreSessionIdMappingStore()
        else:
            from .in_memory import InMemorySessionIdMappingStore

            return InMemorySessionIdMappingStore()
