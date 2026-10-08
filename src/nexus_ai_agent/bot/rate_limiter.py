"""Process-local rate limiting for the Modular Monolith."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class InMemoryRateLimiter:
    """Bounded sliding-window limiter with no external service dependency."""

    #: Hard ceiling on simultaneously tracked users; the oldest-tracked
    #: windows are dropped past this, so the limiter cannot grow without bound
    #: on a bot with a large or hostile audience.
    MAX_TRACKED_USERS = 10_000

    def __init__(self, *, limit: int = 5, period: float = 60.0) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if period <= 0:
            raise ValueError("period must be positive")
        self.limit = limit
        self.period = period
        self._events: dict[int, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def is_allowed(self, user_id: int) -> bool:
        now = time.monotonic()
        cutoff = now - self.period
        with self._lock:
            while len(self._events) >= self.MAX_TRACKED_USERS:
                # dict keeps insertion order → drop the oldest-tracked user.
                self._events.pop(next(iter(self._events)))
            events = self._events[user_id]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                logger.warning(
                    "user rate limited: user_id=%s count=%s period=%s",
                    user_id,
                    len(events),
                    self.period,
                )
                return False
            events.append(now)
            return True

    def clear(self, user_id: int | None = None) -> None:
        """Clear one user's events or all process-local state."""
        with self._lock:
            if user_id is None:
                self._events.clear()
            else:
                self._events.pop(user_id, None)
