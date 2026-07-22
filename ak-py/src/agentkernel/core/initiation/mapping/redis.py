"""Redis-backed Session ID Mapping store. Requires the ``redis`` dependency."""

from ...config import AKConfig
from ...util.driver.redis import RedisDriver
from .redis_like import _RedisLikeSessionIdMappingStore


class RedisSessionIdMappingStore(_RedisLikeSessionIdMappingStore):
    """
    Redis-backed implementation of the SessionIdMappingStore interface.

    Connection settings and TTL come from ``session.redis``; the key prefix is
    derived by suffixing the session store's prefix with ``id-mapping:``.
    """

    def __init__(self):
        conn = AKConfig.get().session.redis
        if conn is None:
            raise ValueError("session.redis config block is required to use RedisSessionIdMappingStore")
        prefix = f"{conn.prefix}id-mapping:"
        driver = RedisDriver(url=conn.url, prefix=prefix, ttl=int(conn.ttl), decode_responses=True)
        super().__init__(driver, "ak.initiation.mapping.redis")
