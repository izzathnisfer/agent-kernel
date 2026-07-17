"""Redis-backed Session ID Mapping store. Requires the ``redis`` dependency."""

from ...config import AKConfig
from ...util.driver.redis import RedisDriver
from .redis_like import _RedisLikeSessionIdMappingStore


class RedisSessionIdMappingStore(_RedisLikeSessionIdMappingStore):
    """
    Redis-backed implementation of the SessionIdMappingStore interface.

    Connection settings come from ``session.redis``; the key prefix and TTL come
    from ``mapping_table``.
    """

    def __init__(self):
        cfg = AKConfig.get()
        mapping_cfg = cfg.mapping_table
        if mapping_cfg is None:
            raise ValueError("mapping_table config block is required to use RedisSessionIdMappingStore")
        conn = cfg.session.redis
        if conn is None:
            raise ValueError("session.redis config block is required to use RedisSessionIdMappingStore")
        driver = RedisDriver(url=conn.url, prefix=mapping_cfg.prefix, ttl=int(mapping_cfg.ttl), decode_responses=True)
        super().__init__(driver, "ak.initiation.mapping.redis")
