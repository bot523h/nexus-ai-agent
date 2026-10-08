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
    """Allow-list auth with deny-by-default.

    - The owner (``owner_telegram_id`` != 0) is always allowed.
    - ``allowed_user_ids`` adds extra allowed users on top of the owner.
    - Empty list + owner configured → only the owner is allowed.
    - Empty list + no owner configured → nobody is allowed.
    """

    def __init__(self, allowed_user_ids: list[int], owner_telegram_id: int = 0):
        self.allowed_user_ids = list(allowed_user_ids)
        self.owner_telegram_id = owner_telegram_id

    def is_allowed(self, user_id: int) -> bool:
        if self.owner_telegram_id and user_id == self.owner_telegram_id:
            return True
        return user_id in self.allowed_user_ids
