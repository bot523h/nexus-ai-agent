"""Distributed (Redis-backed) rate-limit backend — task-163, D-0010 / ADR 0005.

The access-guard denial limiter must survive a scale-to-zero kill: a spammer
who paces one request per container-lifetime window must still be counted.
The counter therefore lives in Redis, in a *fixed* window key whose TTL is
set atomically with the increment:

    key   = ``{prefix}:{user_id}:{floor(now / period)}``
    step  = Lua:  count = INCR key; if count == 1: PEXPIRE key ttl

One atomic Lua step means there is no crash window in which the key exists
without a TTL, and a new window is simply a *new key* (the old one expires
on its own) — no sliding-window bookkeeping, no per-request ``ZREMRANGE*``
race.

Availability contract (two distinct failures, two distinct behaviours):

* **composition time** — Redis unreachable at bot start:
  :func:`build_rate_limit_backend` returns ``None`` (the in-process limiter
  keeps serving; loud error log).  Availability wins at boot: a security
  gate must not take the bot down with it.
* **decision time** — Redis unreachable mid-request:
  :meth:`RedisRateLimiter.is_allowed` returns ``False`` (fail-closed; loud
  error log).  A security surface never opens a hole because the counter is
  unreachable.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol, cast, runtime_checkable

import redis

logger = logging.getLogger(__name__)

__all__ = [
    "RateLimitBackend",
    "RedisRateLimitUnavailable",
    "RedisRateLimiter",
    "build_rate_limit_backend",
]

#: One atomic fixed-window step: INCR + PEXPIRE (TTL only when the key is
#: created).  No crash window in which a counter exists without an expiry.
_FIXED_WINDOW_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return count
"""


class RedisRateLimitUnavailable(Exception):
    """Redis unreachable at composition time (fail-soft path)."""


@runtime_checkable
class RateLimitBackend(Protocol):
    """The seam installed at the composition root (``bot/middleware.py``)."""

    def is_allowed(self, *, user_id: int, limit: int, period_seconds: float) -> bool: ...


@runtime_checkable
class _RedisScript(Protocol):
    def __call__(self, keys: list[str], args: list[str], **kwargs: Any) -> Any: ...


@runtime_checkable
class _RedisLikeClient(Protocol):
    """The subset of the Redis client the limiter actually uses.

    Typed as a protocol (not ``redis.Redis``) so tests can inject a fake
    without a network, and mypy stays exact about what this class may call.
    """

    def ping(self) -> bool: ...

    def register_script(self, source: str) -> Any: ...

    def scan_iter(self, match: str, count: int | None = None) -> Any: ...

    def delete(self, *keys: str) -> int: ...

    def close(self) -> None: ...


class RedisRateLimiter:
    """Fixed-window rate limiter on a Redis key namespace (fail-closed)."""

    def __init__(
        self,
        url: str,
        *,
        client: _RedisLikeClient | None = None,
        prefix: str = "nexus:rl",
    ) -> None:
        self._prefix = prefix
        if client is not None:
            self._client = client
        else:
            self._client = cast("_RedisLikeClient", redis.Redis.from_url(url))
        try:
            self._client.ping()
        except Exception as exc:
            # Composition-time failure: surface it to the builder, which
            # decides the fail-soft fallback (in-process limiter).
            raise RedisRateLimitUnavailable(f"redis ping failed: {exc}") from exc
        self._script: _RedisScript | None = cast(
            "_RedisScript", self._client.register_script(_FIXED_WINDOW_LUA)
        )

    def is_allowed(self, *, user_id: int, limit: int, period_seconds: float) -> bool:
        """Consume one count for ``user_id``; ``False`` = deny.

        Decision-time failures fail *closed* (deny) by design: the limiter
        is a security surface, and an unreachable counter must never read
        as "everything is allowed".
        """
        try:
            now = time.time()
            window = int(now // period_seconds)
            key = f"{self._prefix}:{user_id}:{window}"
            ttl_ms = int(period_seconds * 1000) + 1000
            count = int(self._script_call(key, str(ttl_ms)))
            return count <= limit
        except Exception:  # noqa: BLE001 — fail-closed, by design
            logger.error("redis_rate_limit_failed fail_closed=True", exc_info=True)
            return False

    def reset(self, user_id: int) -> int:
        """Delete every window key for ``user_id`` (operator action)."""
        deleted = 0
        pattern = f"{self._prefix}:{user_id}:*"
        for key in self._client.scan_iter(match=pattern):
            deleted += int(self._client.delete(str(key)))
        return deleted

    def close(self) -> None:
        self._client.close()

    def _script_call(self, key: str, ttl_ms: str) -> Any:
        script = self._script
        if script is None:  # pragma: no cover — defensive (always set in __init__)
            raise RedisRateLimitUnavailable("lua script not registered")
        return script(keys=[key], args=[ttl_ms])


def build_rate_limit_backend(settings: Any) -> RedisRateLimiter | None:
    """Composition-time builder (fail-soft).

    ``None`` when ``NEXUS_REDIS_URL`` is unset **or** Redis is unreachable —
    the caller then keeps the legacy in-process limiter.  Never raises:
    a state tier must not take the bot down at boot.
    """
    url = getattr(settings, "redis_url", None)
    if not url:
        return None
    try:
        return RedisRateLimiter(str(url))
    except RedisRateLimitUnavailable:
        logger.error(
            "redis_rate_limit_unavailable fail_soft=true — in-process limiter stays active",
            exc_info=True,
        )
        return None
