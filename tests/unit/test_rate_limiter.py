from nexus_ai_agent.bot.rate_limiter import InMemoryRateLimiter


def test_in_memory_rate_limiter_is_process_local() -> None:
    limiter = InMemoryRateLimiter(limit=2, period=60)

    assert limiter.is_allowed(7) is True
    assert limiter.is_allowed(7) is True
    assert limiter.is_allowed(7) is False
    assert limiter.is_allowed(8) is True

    limiter.clear(7)
    assert limiter.is_allowed(7) is True
