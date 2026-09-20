"""In-memory sliding-window rate limiter (replaces the Redis-backed one)."""

from __future__ import annotations

from nexus_ai_agent.bot import rate_limiter as rl_module
from nexus_ai_agent.bot.rate_limiter import InMemoryRateLimiter


class _Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_limit_then_blocks() -> None:
    clock = _Clock()
    limiter = InMemoryRateLimiter(limit=3, period=60.0, clock=clock)
    assert [limiter.is_allowed(1) for _ in range(3)] == [True, True, True]
    assert limiter.is_allowed(1) is False


def test_users_are_isolated() -> None:
    clock = _Clock()
    limiter = InMemoryRateLimiter(limit=1, period=60.0, clock=clock)
    assert limiter.is_allowed(1) is True
    assert limiter.is_allowed(1) is False
    assert limiter.is_allowed(2) is True


def test_window_slides() -> None:
    clock = _Clock()
    limiter = InMemoryRateLimiter(limit=2, period=60.0, clock=clock)
    assert limiter.is_allowed(1) is True
    clock.now += 30
    assert limiter.is_allowed(1) is True
    assert limiter.is_allowed(1) is False
    clock.now += 31  # the first request (t=1000) has aged out of the 60s window
    assert limiter.is_allowed(1) is True
    assert limiter.is_allowed(1) is False


def test_rejected_requests_do_not_extend_the_penalty() -> None:
    """Blocked attempts are not recorded, so a spammer is not locked out forever."""
    clock = _Clock()
    limiter = InMemoryRateLimiter(limit=1, period=10.0, clock=clock)
    assert limiter.is_allowed(1) is True
    for _ in range(50):
        clock.now += 0.1
        assert limiter.is_allowed(1) is False
    clock.now += 10
    assert limiter.is_allowed(1) is True


def test_defaults_match_the_previous_policy() -> None:
    limiter = InMemoryRateLimiter()
    assert limiter.limit == 5
    assert limiter.period == 60.0


def test_module_has_no_redis_symbols() -> None:
    assert not hasattr(rl_module, "RedisRateLimiter")
    assert not hasattr(rl_module, "redis")
