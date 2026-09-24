"""Integration: the scale-to-zero kill proof for the Redis rate limiter.

The whole point of the distributed tier: a container can be SIGKILLed at
any moment, and the counter must survive.  ``os.fork`` + ``os._exit(9)``
reproduces the kill faithfully — no close(), no cleanup, no chance to
flush.  A fresh instance (the "woken" container) must read the same
window's count.

Skipped unless ``NEXUS_REDIS_URL`` is set (CI: the ``distributed-state``
job provides a local redis:7-alpine service container — zero cloud
credentials).
"""

from __future__ import annotations

import os

import pytest

from nexus_ai_agent.stateful import RedisRateLimiter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("NEXUS_REDIS_URL"), reason="NEXUS_REDIS_URL not set"),
]

_URL = os.environ.get("NEXUS_REDIS_URL", "")
_LIMIT = 5
_PERIOD = 60.0


def _limiter() -> RedisRateLimiter:
    return RedisRateLimiter(_URL)


def test_kill_does_not_reset_the_counter() -> None:
    """3 counts here + 2 counts in a SIGKILLed child ⇒ the 6th is denied."""
    uid = 777
    _limiter().reset(uid)

    for _ in range(3):
        assert _limiter().is_allowed(user_id=uid, limit=_LIMIT, period_seconds=_PERIOD)

    pid = os.fork()
    if pid == 0:  # child: consume, then die violently (no cleanup)
        child = _limiter()
        child.is_allowed(user_id=uid, limit=_LIMIT, period_seconds=_PERIOD)
        child.is_allowed(user_id=uid, limit=_LIMIT, period_seconds=_PERIOD)
        os._exit(9)  # SIGKILL semantics: no __del__, no close()
    _, status = os.waitpid(pid, 0)
    assert status == 9 or (status >> 8) == 0  # killed or exited; either way no cleanup ran

    # A brand-new instance (a woken container) must see all five counts.
    woken = _limiter()
    assert woken.is_allowed(user_id=uid, limit=_LIMIT, period_seconds=_PERIOD) is False

    _limiter().reset(uid)


def test_unaffected_users_are_independent() -> None:
    """The killed user's window must not leak onto another user."""
    blocked, other = 777, 888
    _limiter().reset(blocked)
    _limiter().reset(other)

    for _ in range(_LIMIT):
        _limiter().is_allowed(user_id=blocked, limit=_LIMIT, period_seconds=_PERIOD)
    assert _limiter().is_allowed(user_id=blocked, limit=_LIMIT, period_seconds=_PERIOD) is False

    limiter = _limiter()
    assert limiter.is_allowed(user_id=other, limit=_LIMIT, period_seconds=_PERIOD) is True

    limiter.reset(blocked)
    limiter.reset(other)
