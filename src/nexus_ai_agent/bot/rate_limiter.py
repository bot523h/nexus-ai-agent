from __future__ import annotations

import logging
import time

import redis

from nexus_ai_agent.config.settings import get_settings

logger = logging.getLogger(__name__)


class RedisRateLimiter:
    """Rate limiter using Redis for spam prevention."""

    def __init__(self) -> None:
        settings = get_settings()
        self.redis = redis.from_url(settings.redis_url)
        self.limit = 5  # requests
        self.period = 60  # seconds

    def is_allowed(self, user_id: int) -> bool:
        """Check if user is within rate limits."""
        key = f"rate_limit:{user_id}"
        now = time.time()

        try:
            # Using Redis pipeline for atomicity
            pipe = self.redis.pipeline()
            # Remove timestamps older than the period
            pipe.zremrangebyscore(key, 0, now - self.period)
            # Add current timestamp
            pipe.zadd(key, {str(now): now})
            # Count remaining timestamps
            pipe.zcard(key)
            # Set expiration to cleanup old keys
            pipe.expire(key, self.period)

            _, _, count, _ = pipe.execute()

            if count > self.limit:
                logger.warning(f"User {user_id} rate limited: {count} requests in {self.period}s")
                return False

            return True
        except Exception as e:
            logger.error(f"Rate limiter error: {e}")
            return True  # Fail open
