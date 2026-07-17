"""Valkey-backed Session ID Mapping store. Requires the ``valkey`` extra."""

from ...config import AKConfig
from ...util.driver.valkey import ValkeyDriver
from .redis_like import _RedisLikeSessionIdMappingStore


class ValkeySessionIdMappingStore(_RedisLikeSessionIdMappingStore):
    """
    Valkey-backed implementation of the SessionIdMappingStore interface.

    Connection settings come from ``session.valkey``; the key prefix and TTL come
    from ``mapping_table``.
    """

    def __init__(self):
        cfg = AKConfig.get()
        mapping_cfg = cfg.mapping_table
        if mapping_cfg is None:
            raise ValueError("mapping_table config block is required to use ValkeySessionIdMappingStore")
        conn = cfg.session.valkey
        if conn is None:
            raise ValueError("session.valkey config block is required to use ValkeySessionIdMappingStore")
        driver = ValkeyDriver(url=conn.url, prefix=mapping_cfg.prefix, ttl=int(mapping_cfg.ttl), decode_responses=True)
        super().__init__(driver, "ak.initiation.mapping.valkey")
