"""Unit tests for the distributed rate-limit backend seam (task-163).

Covers the install/delegate/clear contract in ``bot/middleware.py``:

* with **no backend installed** (the default and the availability path),
  ``RateLimiter`` runs the legacy in-process sliding window — the behaviour
  must be byte-for-byte the old one;
* with a backend **installed**, every decision delegates to
  ``backend.is_allowed(user_id, limit, period_seconds)`` and the backend's
  verdict (True or False) is returned unchanged;
* a backend that **raises** mid-request fails *closed* (deny, never crash,
  never silently fall back to the permissive in-process window);
* ``clear_rate_limit_backend()`` restores the legacy path.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from nexus_ai_agent.bot import middleware as mw


@pytest.fixture(autouse=True)
def _no_backend_by_default() -> Iterator[None]:
    """The seam is process-wide global state — never leak a backend."""
    mw.clear_rate_limit_backend()
    yield
    mw.clear_rate_limit_backend()


class _RecordingBackend:
    def __init__(self, verdict: bool = True) -> None:
        self.verdict = verdict
        self.calls: list[dict[str, Any]] = []

    def is_allowed(self, *, user_id: int, limit: int, period_seconds: float) -> bool:
        self.calls.append({"user_id": user_id, "limit": limit, "period_seconds": period_seconds})
        return self.verdict


class _RaisingBackend:
    def __init__(self, times: int = 1) -> None:
        self.times = times
        self.attempts = 0

    def is_allowed(self, **kwargs: Any) -> bool:
        self.attempts += 1
        if self.attempts <= self.times:
            raise RuntimeError("redis connection reset")
        return True


class TestNoBackendLegacyPath:
    def test_default_has_no_backend(self) -> None:
        assert mw.get_rate_limit_backend() is None

    def test_legacy_sliding_window_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        limiter = mw.RateLimiter(max_messages=2, window_seconds=60)
        clock = {"t": 1000.0}
        monkeypatch.setattr(mw.time, "time", lambda: clock["t"])

        assert limiter.is_allowed(7) is True
        assert limiter.is_allowed(7) is True
        # Third message inside the same window: denied.
        assert limiter.is_allowed(7) is False
        # Other users are tracked independently.
        assert limiter.is_allowed(8) is True
        # After the window slides past both events, the user is admitted again.
        clock["t"] = 1000.0 + 60.0 + 1
        assert limiter.is_allowed(7) is True

    def test_legacy_events_expire_before_denial(self, monkeypatch: pytest.MonkeyPatch) -> None:
        limiter = mw.RateLimiter(max_messages=1, window_seconds=60)
        clock = {"t": 0.0}
        monkeypatch.setattr(mw.time, "time", lambda: clock["t"])

        assert limiter.is_allowed(1) is True
        clock["t"] = 59.0  # still inside the window
        assert limiter.is_allowed(1) is False
        clock["t"] = 61.0  # the first event has expired
        assert limiter.is_allowed(1) is True


class TestInstallAndDelegate:
    def test_install_is_visible_via_getter(self) -> None:
        backend = _RecordingBackend()
        mw.install_rate_limit_backend(backend)
        assert mw.get_rate_limit_backend() is backend

    def test_delegates_with_typed_kwargs_and_returns_backend_true(self) -> None:
        backend = _RecordingBackend(verdict=True)
        mw.install_rate_limit_backend(backend)
        limiter = mw.RateLimiter(max_messages=3, window_seconds=45)

        assert limiter.is_allowed(42) is True

        assert backend.calls == [
            {"user_id": 42, "limit": 3, "period_seconds": 45.0},
        ]
        # The legacy window must stay untouched while a backend is installed:
        # the same user may be admitted by the backend on every call.
        assert limiter.is_allowed(42) is True
        assert len(backend.calls) == 2

    def test_backend_false_verdict_is_denied(self) -> None:
        backend = _RecordingBackend(verdict=False)
        mw.install_rate_limit_backend(backend)
        limiter = mw.RateLimiter(max_messages=100, window_seconds=60)
        assert limiter.is_allowed(1) is False
        assert len(backend.calls) == 1


class TestFailClosed:
    def test_raising_backend_denies_instead_of_crashing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = _RaisingBackend(times=1)
        mw.install_rate_limit_backend(backend)
        limiter = mw.RateLimiter(max_messages=100, window_seconds=60)

        with caplog.at_level("ERROR", logger="nexus_ai_agent.bot.middleware"):
            assert limiter.is_allowed(1) is False

        assert "rate_limit_backend_failed" in caplog.text
        assert "fail_closed=True" in caplog.text

    def test_raising_backend_never_falls_back_to_permissive_window(self) -> None:
        backend = _RaisingBackend(times=10)
        mw.install_rate_limit_backend(backend)
        limiter = mw.RateLimiter(max_messages=100, window_seconds=60)

        # First call: the legacy window is empty, yet the decision must be
        # a deny (fail-closed) — never the in-process fallback.
        assert limiter.is_allowed(1) is False
        # And the backend is still the authority on every subsequent call.
        assert limiter.is_allowed(1) is False
        assert backend.attempts == 2

    def test_backend_recovered_after_transient_failure(self) -> None:
        backend = _RaisingBackend(times=1)
        mw.install_rate_limit_backend(backend)
        limiter = mw.RateLimiter(max_messages=100, window_seconds=60)

        assert limiter.is_allowed(1) is False  # transient failure → deny
        assert limiter.is_allowed(1) is True  # recovered → delegate again
        assert backend.attempts == 2


class TestClear:
    def test_clear_restores_legacy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _RecordingBackend(verdict=False)
        mw.install_rate_limit_backend(backend)
        limiter = mw.RateLimiter(max_messages=5, window_seconds=60)
        assert limiter.is_allowed(1) is False  # backend denies
        assert len(backend.calls) == 1

        mw.clear_rate_limit_backend()
        assert mw.get_rate_limit_backend() is None
        assert len(backend.calls) == 1  # no further delegation

        # The legacy window now decides — and it allows.
        assert limiter.is_allowed(1) is True
        assert len(backend.calls) == 1

    def test_install_none_is_the_availability_path_not_deny(self) -> None:
        # bot/app.py installs build_rate_limit_backend(settings), which
        # returns None when NEXUS_REDIS_URL is unset/unreachable.  A None
        # install must mean "legacy limiter", never "deny everything".
        mw.install_rate_limit_backend(None)
        limiter = mw.RateLimiter(max_messages=10, window_seconds=60)
        assert limiter.is_allowed(1) is True
