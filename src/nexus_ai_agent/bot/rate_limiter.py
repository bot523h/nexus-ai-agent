"""Process-local rate limiting for the Modular Monolith."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class InMemoryRateLimiter:
    """Bounded sliding-window limiter with no external service dependency."""

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
