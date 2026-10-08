from nexus_ai_agent.bot.rate_limiter import InMemoryRateLimiter


def test_in_memory_rate_limiter_is_process_local() -> None:
    limiter = InMemoryRateLimiter(limit=2, period=60)

    assert limiter.is_allowed(7) is True
    assert limiter.is_allowed(7) is True
    assert limiter.is_allowed(7) is False
    assert limiter.is_allowed(8) is True

    limiter.clear(7)
    assert limiter.is_allowed(7) is True


def test_tracked_users_are_bounded_under_a_distinct_user_flood() -> None:
    """RED on main@440d290: the class says "bounded" but ``_events`` grew forever.

    Every distinct hostile ``user_id`` added a permanent deque, so the limiter's
    memory was a function of audience size. The cap evicts the oldest-tracked
    window instead; the flood must not break the limiter for a fresh user.
    """
    limiter = InMemoryRateLimiter(limit=2, period=60)
    cap = InMemoryRateLimiter.MAX_TRACKED_USERS

    for user_id in range(cap + 25):
        assert limiter.is_allowed(user_id) is True

    assert len(limiter._events) <= cap
    # The eviction is a bound, not a bypass: the current user is still limited.
    fresh = cap + 25
    assert limiter.is_allowed(fresh) is True
    assert limiter.is_allowed(fresh) is True
    assert limiter.is_allowed(fresh) is False
