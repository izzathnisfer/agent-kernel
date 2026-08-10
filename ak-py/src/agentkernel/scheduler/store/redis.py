import logging

from ...core.util.driver.redis import RedisDriver
from .redis_like import _RedisLikeScheduledTaskStore


class RedisScheduledTaskStore(_RedisLikeScheduledTaskStore):
    """Scheduled-task rows in a separate keyspace on the session Redis cluster."""

    def __init__(self, url: str, prefix: str):
        """
        :param url: The session cluster's URL — no new infrastructure is provisioned.
        :param prefix: Key prefix dedicating a keyspace to scheduled tasks.
        """
        self._log = logging.getLogger("ak.scheduler.store.redis")
        # ttl=0 deliberately: the driver would otherwise apply its TTL to every write,
        # expiring live rows. soft_delete applies the derived window per call instead.
        self._driver = RedisDriver(url=url, prefix=prefix, ttl=0, decode_responses=True)
