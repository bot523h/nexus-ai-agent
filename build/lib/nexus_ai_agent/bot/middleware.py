from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(self, max_messages: int = 10, window_seconds: int = 60):
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        self._events: dict[int, deque[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        q = self._events.setdefault(user_id, deque())
        while q and (now - q[0]) > self.window_seconds:
            q.popleft()
        if len(q) >= self.max_messages:
            return False
        q.append(now)
        return True


class AuthMiddleware:
    def __init__(self, allowed_user_ids: list[int]):
        self.allowed_user_ids = allowed_user_ids

    def is_allowed(self, user_id: int) -> bool:
        if not self.allowed_user_ids:
            return True
        return user_id in self.allowed_user_ids
