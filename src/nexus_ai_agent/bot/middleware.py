from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

# ── distributed rate-limit backend (task-163) ───────────────────────────
# The composition root (bot/app.py) installs a RateLimitBackend here when
# NEXUS_REDIS_URL is set (stateful.build_rate_limit_backend).  Until then —
# and if the backend is cleared — RateLimiter runs its legacy in-process
# sliding window, byte-for-byte the old behaviour.
#
# Fail-closed at decision time: a backend that raises mid-request counts the
# request as *not allowed* (a security surface must not open a hole because
# the counter is unreachable).  The composition-time fallback (no backend
# installed at all) is the availability path.
_RATE_LIMIT_BACKEND: Any | None = None
_BACKEND_LOCK = threading.Lock()


def install_rate_limit_backend(backend: Any | None) -> None:
    """Process-wide backend registration (see module note)."""
    global _RATE_LIMIT_BACKEND
    with _BACKEND_LOCK:
        _RATE_LIMIT_BACKEND = backend


def get_rate_limit_backend() -> Any | None:
    with _BACKEND_LOCK:
        return _RATE_LIMIT_BACKEND


def clear_rate_limit_backend() -> None:
    """Restore the legacy in-process limiter (tests / shutdown)."""
    install_rate_limit_backend(None)


class RateLimiter:
    def __init__(self, max_messages: int = 10, window_seconds: int = 60):
        self.max_messages = max_messages
        self.window_seconds = window_seconds
        self._events: dict[int, deque[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        backend = get_rate_limit_backend()
        if backend is not None:
            try:
                return bool(
                    backend.is_allowed(
                        user_id=user_id,
                        limit=self.max_messages,
                        period_seconds=float(self.window_seconds),
                    )
                )
            except Exception:  # noqa: BLE001 — fail-closed, by design
                logger.error("rate_limit_backend_failed fail_closed=True", exc_info=True)
                return False
        # Legacy in-process sliding window (unchanged behaviour).
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
