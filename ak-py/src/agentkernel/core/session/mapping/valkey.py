"""Valkey-backed Session ID Mapping store. Requires the ``valkey`` extra."""

from ...config import AKConfig
from ...util.driver.valkey import ValkeyDriver
from .redis_like import _RedisLikeMappingStore


class ValkeyMappingStore(_RedisLikeMappingStore):
    """
    Valkey-backed implementation of the MappingStore interface.

    Connection settings and TTL come from ``session.valkey``; the key prefix is
    derived by suffixing the session store's prefix with ``id-mapping:``.
    """

    def __init__(self):
        conn = AKConfig.get().session.valkey
        if conn is None:
            raise ValueError("session.valkey config block is required to use ValkeyMappingStore")
        prefix = f"{conn.prefix}id-mapping:"
        driver = ValkeyDriver(url=conn.url, prefix=prefix, ttl=int(conn.ttl), decode_responses=True)
        super().__init__(driver, "ak.core.session.mapping.valkey")
