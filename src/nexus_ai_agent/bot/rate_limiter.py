"""Per-user sliding-window rate limiter, in-process (R-001 / R-026).

Replaces the Redis-backed limiter.  The bot is a single-process modular
monolith, so process memory *is* the shared state; there is no network hop
and therefore no "fail open" path.  The policy is unchanged: at most
``limit`` accepted messages per ``period`` seconds per user.  Rejected
attempts are not recorded, so a burst does not extend its own penalty.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)


class InMemoryRateLimiter:
    """Sliding-window limiter keyed by Telegram user id."""

    def __init__(
        self,
        limit: int = 5,
        period: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if period <= 0:
            raise ValueError("period must be > 0")
        self.limit = limit
        self.period = period
        self._clock = clock
        self._hits: dict[int, deque[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        """Record and accept the request if the user is under the limit."""
        now = self._clock()
        window_start = now - self.period
        hits = self._hits.get(user_id)
        if hits is None:
            hits = deque()
            self._hits[user_id] = hits
        while hits and hits[0] <= window_start:
            hits.popleft()
        if len(hits) >= self.limit:
            log.warning("rate_limited", user_id=user_id, hits=len(hits), period=self.period)
            return False
        hits.append(now)
        return True
