"""Unit tests for the Redis-backed rate limiter (task-163, ADR 0005).

Exercises the real ``RedisRateLimiter`` code path (fixed-window key + the
atomic INCR+PEXPIRE Lua semantics) against an injected fake client — no
network. The kill-survival proof (fork → SIGKILL → counter survives) is
an integration test (``tests/integration/test_rate_limiter_redis.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nexus_ai_agent.stateful import rate_limit as rl


class _FakeScript:
    """Emulates the INCR + PEXPIRE-when-new Lua step exactly."""

    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def __call__(self, keys: list[str], args: list[str], **kwargs: Any) -> int:
        (key,) = keys
        (ttl_ms,) = args
        count = self._client.counters.get(key, 0) + 1
        self._client.counters[key] = count
        if count == 1:
            self._client.ttls[key] = int(ttl_ms)
        return count


class _FakeClient:
    def __init__(
        self, ping_error: Exception | None = None, call_error: Exception | None = None
    ) -> None:
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.deleted: list[str] = []
        self.ping_error = ping_error
        self.call_error = call_error
        self.closed = False

    def ping(self) -> bool:
        if self.ping_error is not None:
            raise self.ping_error
        return True

    def register_script(self, source: str) -> _FakeScript:
        assert "INCR" in source and "PEXPIRE" in source
        return _FakeScript(self)

    def scan_iter(self, match: str, count: int | None = None) -> list[str]:
        prefix = match.rstrip("*")
        return [key for key in self.counters if key.startswith(prefix)]

    def delete(self, *keys: str) -> int:
        self.deleted.extend(keys)
        for key in keys:
            self.counters.pop(key, None)
            self.ttls.pop(key, None)
        return len(keys)

    def close(self) -> None:
        self.closed = True


class _BrokenScript:
    def __call__(self, *args: Any, **kwargs: Any) -> int:
        raise RuntimeError("connection reset by peer")


def test_fixed_window_counts_up_to_the_limit() -> None:
    client = _FakeClient()
    limiter = rl.RedisRateLimiter("redis://x", client=client, prefix="nexus:rl")

    for _ in range(3):
        assert limiter.is_allowed(user_id=7, limit=3, period_seconds=60) is True
    # The 4th message in the same window is denied.
    assert limiter.is_allowed(user_id=7, limit=3, period_seconds=60) is False
    # The counter exists exactly once (fixed window) and got a TTL.
    assert len(client.counters) == 1
    (key,) = tuple(client.counters)
    assert client.ttls[key] == 60 * 1000 + 1000


def test_key_is_user_and_window_scoped() -> None:
    client = _FakeClient()
    limiter = rl.RedisRateLimiter("redis://x", client=client, prefix="nexus:rl")

    limiter.is_allowed(user_id=7, limit=10, period_seconds=60)
    limiter.is_allowed(user_id=8, limit=10, period_seconds=60)

    keys = sorted(client.counters)
    assert all(key.startswith("nexus:rl:") for key in keys)
    assert any(key.startswith("nexus:rl:7:") for key in keys)
    assert any(key.startswith("nexus:rl:8:") for key in keys)
    # Different users, different keys — no cross-user counting.
    assert len(keys) == 2


def test_new_window_is_a_new_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Production keys are ``nexus:rl:{uid}:{window}``: a new window is a
    # NEW key (the old one expires on its own TTL).
    client = _FakeClient()
    limiter = rl.RedisRateLimiter("redis://x", client=client, prefix="nexus:rl")

    clock = {"t": 1_000_000.0}  # pinned: window = 16666666
    monkeypatch.setattr(rl.time, "time", lambda: clock["t"])
    assert limiter.is_allowed(user_id=7, limit=1, period_seconds=60) is True
    assert limiter.is_allowed(user_id=7, limit=1, period_seconds=60) is False
    old_keys = set(client.counters)

    clock["t"] += 60.0  # next window
    assert limiter.is_allowed(user_id=7, limit=1, period_seconds=60) is True
    assert set(client.counters) - old_keys, "new window must be a new key"


def test_ttl_is_window_plus_one_second() -> None:
    client = _FakeClient()
    limiter = rl.RedisRateLimiter("redis://x", client=client)
    limiter.is_allowed(user_id=1, limit=5, period_seconds=45)
    assert list(client.ttls.values()) == [45_000 + 1000]


def test_ping_failure_at_construction_raises_unavailable() -> None:
    client = _FakeClient(ping_error=ConnectionRefusedError("no redis"))
    with pytest.raises(rl.RedisRateLimitUnavailable):
        rl.RedisRateLimiter("redis://x", client=client)


def test_mid_request_failure_fails_closed() -> None:
    client = _FakeClient()
    limiter = rl.RedisRateLimiter("redis://x", client=client)
    limiter._script = _BrokenScript()  # type: ignore[assignment]
    # A security surface never opens a hole because the counter is unreachable.
    assert limiter.is_allowed(user_id=7, limit=100, period_seconds=60) is False


def test_reset_deletes_all_window_keys_for_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    limiter = rl.RedisRateLimiter("redis://x", client=client)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(rl.time, "time", lambda: clock["t"])
    # Two windows for user 7, one for user 8 (inside window 1).
    for window_start in (1_000_000.0, 1_000_060.0):
        clock["t"] = window_start
        limiter.is_allowed(user_id=7, limit=10, period_seconds=60)
    clock["t"] = 1_000_000.0
    limiter.is_allowed(user_id=8, limit=10, period_seconds=60)

    deleted = limiter.reset(7)
    assert deleted == 2
    assert all(key.startswith("nexus:rl:7:") for key in client.deleted)
    assert any(key.startswith("nexus:rl:8:") for key in client.counters)


def test_build_rate_limit_backend_returns_none_when_unset() -> None:
    assert rl.build_rate_limit_backend(SimpleNamespace(redis_url=None)) is None
    assert rl.build_rate_limit_backend(SimpleNamespace(redis_url="")) is None


def test_build_rate_limit_backend_returns_none_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRedisClass:
        @staticmethod
        def from_url(url: str) -> _FakeClient:
            return _FakeClient(ping_error=ConnectionRefusedError("no redis"))

    monkeypatch.setattr(rl.redis, "Redis", _FakeRedisClass)
    # Fail-soft at composition: the bot keeps its in-process limiter.
    assert rl.build_rate_limit_backend(SimpleNamespace(redis_url="redis://nowhere:6379/0")) is None
