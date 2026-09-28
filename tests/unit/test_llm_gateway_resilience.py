"""W2 — resilience primitives: backoff, Retry-After, circuit breaker, rate window.

Each primitive here is a direct implementation of published guidance rather than
an invention:

* backoff/jitter — AWS Architecture Blog, "Exponential Backoff And Jitter"
  (Brooker, 2015): full jitter minimises both total work and peak server load;
* ``Retry-After`` — RFC 9110 §10.2.3: a delay in seconds *or* an HTTP-date,
  honoured exactly when present;
* circuit breaker — open after N consecutive health failures, fail fast during
  the cool-down, then admit a bounded half-open probe;
* rate window — Gemini's limits are RPM/TPM/RPD and exceeding *any* of them
  yields 429 RESOURCE_EXHAUSTED, with RPD resetting on a fixed daily boundary
  (https://ai.google.dev/gemini-api/docs/rate-limits).

The clock is always injected, so every assertion here is about arithmetic, not
about how fast the test machine is.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from nexus_ai_agent.llm.errors import LLMErrorKind
from nexus_ai_agent.llm.gateway.policy import CircuitPolicy, JitterMode, RetryPolicy
from nexus_ai_agent.llm.gateway.resilience import (
    CircuitBreaker,
    CircuitState,
    WindowRateLimiter,
    compute_backoff,
    parse_retry_after,
)


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ═══════════════════════════════════════════════════════════════════════════
# Backoff and jitter
# ═══════════════════════════════════════════════════════════════════════════


def test_no_jitter_is_exact_exponential_growth() -> None:
    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=8.0, jitter=JitterMode.NONE)
    rng = random.Random(0)
    assert compute_backoff(0, policy, rng=rng) == 1.0
    assert compute_backoff(1, policy, rng=rng) == 2.0
    assert compute_backoff(2, policy, rng=rng) == 4.0
    assert compute_backoff(3, policy, rng=rng) == 8.0


def test_the_cap_bound_the_wait_no_matter_how_many_attempts() -> None:
    """A retry loop must not grow an unbounded sleep out of a long outage."""

    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=8.0, jitter=JitterMode.NONE)
    rng = random.Random(0)
    for attempt in (10, 30, 100, 10_000):
        assert compute_backoff(attempt, policy, rng=rng) == 8.0


def test_full_jitter_stays_inside_zero_to_exponential() -> None:
    """Full jitter is ``uniform(0, min(cap, base * 2**attempt))`` — AWS's recipe."""

    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=64.0, jitter=JitterMode.FULL)
    for seed in range(50):
        rng = random.Random(seed)
        for attempt in range(8):
            delay = compute_backoff(attempt, policy, rng=rng)
            ceiling = min(64.0, 1.0 * (2.0**attempt))
            assert 0.0 <= delay <= ceiling


def test_full_jitter_actually_decorrelates_concurrent_callers() -> None:
    """The point of jitter: a burst must not retry in lockstep (thundering herd)."""

    policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=8.0, jitter=JitterMode.FULL)
    delays = {round(compute_backoff(3, policy, rng=random.Random(seed)), 6) for seed in range(200)}
    assert len(delays) > 150


def test_equal_jitter_keeps_at_least_half_the_exponential_delay() -> None:
    policy = RetryPolicy(base_delay_seconds=2.0, max_delay_seconds=32.0, jitter=JitterMode.EQUAL)
    for seed in range(50):
        rng = random.Random(seed)
        delay = compute_backoff(2, policy, rng=rng)
        assert 4.0 <= delay <= 8.0


def test_decorrelated_jitter_grows_from_the_previous_sleep() -> None:
    policy = RetryPolicy(
        base_delay_seconds=1.0, max_delay_seconds=20.0, jitter=JitterMode.DECORRELATED
    )
    rng = random.Random(3)
    previous = None
    for _ in range(10):
        delay = compute_backoff(0, policy, rng=rng, previous_delay=previous)
        assert 1.0 <= delay <= 20.0
        previous = delay


def test_zero_base_delay_never_produces_a_negative_or_nan_sleep() -> None:
    policy = RetryPolicy(base_delay_seconds=0.0, max_delay_seconds=0.0, jitter=JitterMode.FULL)
    rng = random.Random(1)
    for attempt in range(5):
        delay = compute_backoff(attempt, policy, rng=rng)
        assert delay == 0.0


def test_retry_policy_rejects_nonsense_at_construction() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_seconds=-1.0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_seconds=10.0, max_delay_seconds=1.0)
    with pytest.raises(ValueError):
        RetryPolicy(multiplier=0.5)


def test_retry_policy_allows_only_the_retryable_kinds() -> None:
    policy = RetryPolicy()
    assert policy.allows(LLMErrorKind.RATE_LIMITED) is True
    assert policy.allows(LLMErrorKind.TRANSIENT_PROVIDER) is True
    assert policy.allows(LLMErrorKind.UPSTREAM_TIMEOUT) is True
    assert policy.allows(LLMErrorKind.NETWORK) is True
    assert policy.allows(LLMErrorKind.AUTHENTICATION) is False
    assert policy.allows(LLMErrorKind.INVALID_REQUEST) is False
    assert policy.allows(LLMErrorKind.CONTENT_BLOCKED) is False
    assert policy.allows(LLMErrorKind.CANCELLED) is False


# ═══════════════════════════════════════════════════════════════════════════
# Retry-After (RFC 9110)
# ═══════════════════════════════════════════════════════════════════════════


def test_retry_after_accepts_delta_seconds() -> None:
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(7) == 7.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("1.5") == 1.5


def test_retry_after_accepts_an_http_date_relative_to_now() -> None:
    now_wall = 1_700_000_000.0
    moment = datetime.fromtimestamp(now_wall, tz=timezone.utc) + timedelta(seconds=30)
    value = moment.strftime("%a, %d %b %Y %H:%M:%S GMT")
    parsed = parse_retry_after(value, now_wall=now_wall)
    assert parsed is not None
    assert 29.0 <= parsed <= 31.0


def test_retry_after_in_the_past_is_zero_not_negative() -> None:
    now_wall = 1_700_000_000.0
    moment = datetime.fromtimestamp(now_wall, tz=timezone.utc) - timedelta(seconds=120)
    value = moment.strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(value, now_wall=now_wall) == 0.0


@pytest.mark.parametrize("value", ["", "   ", "soon", "-3", "nan", None, object()])
def test_retry_after_returns_none_for_anything_unusable(value: object) -> None:
    """``None`` means "the provider gave us nothing"; guessing would be worse."""

    assert parse_retry_after(value) is None


def test_retry_after_never_returns_infinity() -> None:
    assert parse_retry_after("inf") is None
    assert parse_retry_after(float("inf")) is None


# ═══════════════════════════════════════════════════════════════════════════
# Circuit breaker
# ═══════════════════════════════════════════════════════════════════════════


def _breaker(**overrides: object) -> tuple[CircuitBreaker, FakeClock]:
    clock = FakeClock()
    policy = CircuitPolicy(
        failure_threshold=int(overrides.get("failure_threshold", 3)),
        recovery_seconds=float(overrides.get("recovery_seconds", 30.0)),
        half_open_max_probes=int(overrides.get("half_open_max_probes", 1)),
        success_threshold=int(overrides.get("success_threshold", 1)),
        enabled=bool(overrides.get("enabled", True)),
    )
    return CircuitBreaker(key="gemini/m", policy=policy, clock=clock), clock


def test_breaker_starts_closed_and_opens_after_the_threshold() -> None:
    breaker, clock = _breaker(failure_threshold=3)
    assert breaker.state(clock()) is CircuitState.CLOSED
    breaker.record_failure(LLMErrorKind.TRANSIENT_PROVIDER, clock())
    breaker.record_failure(LLMErrorKind.TRANSIENT_PROVIDER, clock())
    assert breaker.state(clock()) is CircuitState.CLOSED
    breaker.record_failure(LLMErrorKind.TRANSIENT_PROVIDER, clock())
    assert breaker.state(clock()) is CircuitState.OPEN
    assert breaker.is_open(clock()) is True
    assert breaker.allow(clock()) is False


def test_breaker_ignores_failures_that_say_nothing_about_provider_health() -> None:
    """Our own bug must not shed healthy traffic (LAW 3 + the counted-kinds set)."""

    breaker, clock = _breaker(failure_threshold=2)
    for _ in range(5):
        breaker.record_failure(LLMErrorKind.INVALID_REQUEST, clock())
        breaker.record_failure(LLMErrorKind.MALFORMED_RESPONSE, clock())
        breaker.record_failure(LLMErrorKind.CONTENT_BLOCKED, clock())
        breaker.record_failure(LLMErrorKind.STRUCTURED_OUTPUT_INVALID, clock())
        breaker.record_failure(LLMErrorKind.CANCELLED, clock())
    assert breaker.state(clock()) is CircuitState.CLOSED
    assert breaker.allow(clock()) is True


def test_a_success_resets_the_consecutive_failure_run() -> None:
    breaker, clock = _breaker(failure_threshold=3)
    breaker.record_failure(LLMErrorKind.NETWORK, clock())
    breaker.record_failure(LLMErrorKind.NETWORK, clock())
    breaker.record_success(clock())
    breaker.record_failure(LLMErrorKind.NETWORK, clock())
    breaker.record_failure(LLMErrorKind.NETWORK, clock())
    assert breaker.state(clock()) is CircuitState.CLOSED


def test_breaker_goes_half_open_after_the_cool_down_and_admits_one_probe() -> None:
    breaker, clock = _breaker(failure_threshold=1, recovery_seconds=10.0, half_open_max_probes=1)
    breaker.record_failure(LLMErrorKind.TRANSIENT_PROVIDER, clock())
    assert breaker.allow(clock()) is False
    clock.advance(10.5)
    assert breaker.state(clock()) is CircuitState.HALF_OPEN
    assert breaker.allow(clock()) is True
    # A second concurrent caller must not also probe: the point of half-open is
    # to test the provider with one request, not to re-open the floodgate.
    assert breaker.allow(clock()) is False


def test_a_failed_probe_reopens_the_circuit_and_restarts_the_cool_down() -> None:
    breaker, clock = _breaker(failure_threshold=1, recovery_seconds=10.0)
    breaker.record_failure(LLMErrorKind.TRANSIENT_PROVIDER, clock())
    clock.advance(10.5)
    permit = breaker.acquire(clock())
    assert permit is not None
    permit.failure(LLMErrorKind.TRANSIENT_PROVIDER, clock())
    assert breaker.state(clock()) is CircuitState.OPEN
    assert breaker.allow(clock()) is False
    clock.advance(5.0)
    assert breaker.allow(clock()) is False
    clock.advance(6.0)
    assert breaker.allow(clock()) is True


def test_a_successful_probe_closes_the_circuit() -> None:
    breaker, clock = _breaker(failure_threshold=1, recovery_seconds=10.0, success_threshold=1)
    breaker.record_failure(LLMErrorKind.TRANSIENT_PROVIDER, clock())
    clock.advance(10.5)
    breaker.release_probe()
    assert breaker.allow(clock()) is True
    breaker.record_success(clock())
    assert breaker.state(clock()) is CircuitState.CLOSED
    assert breaker.allow(clock()) is True


def test_release_probe_frees_a_slot_for_the_next_probe() -> None:
    breaker, clock = _breaker(failure_threshold=1, recovery_seconds=5.0, half_open_max_probes=1)
    breaker.record_failure(LLMErrorKind.NETWORK, clock())
    clock.advance(5.5)
    assert breaker.allow(clock()) is True
    assert breaker.allow(clock()) is False
    breaker.release_probe()
    assert breaker.allow(clock()) is True


def test_a_disabled_breaker_never_sheds_traffic() -> None:
    breaker, clock = _breaker(enabled=False, failure_threshold=1)
    for _ in range(10):
        breaker.record_failure(LLMErrorKind.TRANSIENT_PROVIDER, clock())
    assert breaker.state(clock()) is CircuitState.CLOSED
    assert breaker.allow(clock()) is True


def test_breaker_snapshot_is_bounded_and_secret_free() -> None:
    breaker, clock = _breaker(failure_threshold=2)
    breaker.record_failure(LLMErrorKind.RATE_LIMITED, clock())
    snapshot = breaker.snapshot(clock())
    assert snapshot["state"] == "closed"
    assert snapshot["consecutive_failures"] == 1
    assert snapshot["trips"] == 0
    assert isinstance(snapshot, dict)
    assert all(
        isinstance(value, (str, int, float, bool, type(None))) for value in snapshot.values()
    )


def test_breaker_policy_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        CircuitPolicy(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitPolicy(recovery_seconds=0.0)
    with pytest.raises(ValueError):
        CircuitPolicy(half_open_max_probes=0)
    with pytest.raises(ValueError):
        CircuitPolicy(success_threshold=0)


# ═══════════════════════════════════════════════════════════════════════════
# Rolling-minute + UTC-day rate window
# ═══════════════════════════════════════════════════════════════════════════


def test_minute_window_admits_up_to_the_limit_then_throttles() -> None:
    clock = FakeClock()
    limiter = WindowRateLimiter(key="gemini", requests_per_minute=3, clock=clock)
    assert limiter.charge(clock()) is True
    assert limiter.charge(clock()) is True
    assert limiter.charge(clock()) is True
    assert limiter.charge(clock()) is False
    assert limiter.seconds_until_capacity(clock()) > 0.0


def test_the_window_slides_so_capacity_returns_after_sixty_seconds() -> None:
    clock = FakeClock()
    limiter = WindowRateLimiter(key="gemini", requests_per_minute=2, clock=clock)
    limiter.charge(clock())
    clock.advance(10.0)
    limiter.charge(clock())
    assert limiter.charge(clock()) is False
    clock.advance(50.5)  # the first charge has now aged out of the minute
    assert limiter.charge(clock()) is True


def test_seconds_until_capacity_reports_the_oldest_entrys_age() -> None:
    clock = FakeClock()
    limiter = WindowRateLimiter(key="gemini", requests_per_minute=1, clock=clock)
    limiter.charge(clock())
    clock.advance(20.0)
    wait = limiter.seconds_until_capacity(clock())
    assert 39.0 <= wait <= 41.0


def test_unlimited_axes_report_zero_wait() -> None:
    clock = FakeClock()
    limiter = WindowRateLimiter(key="local", clock=clock)
    assert limiter.seconds_until_capacity(clock()) == 0.0
    for _ in range(1000):
        assert limiter.charge(clock()) is True


def test_daily_budget_is_exhaustible_and_reports_infinite_wait() -> None:
    """Gemini's RPD is a hard daily cap: no amount of waiting inside a request
    lifts it, so the wait is ``inf`` and the caller is told the truth."""

    clock = FakeClock()
    wall = FakeClock(1_700_000_000.0)
    limiter = WindowRateLimiter(key="gemini", requests_per_day=2, clock=clock, wall_clock=wall)
    assert limiter.charge(clock()) is True
    assert limiter.charge(clock()) is True
    assert limiter.charge(clock()) is False
    assert limiter.seconds_until_capacity(clock()) == float("inf")


def test_daily_budget_resets_on_the_utc_day_boundary_using_the_wall_clock() -> None:
    """The regression this pins: ``time.monotonic()`` has no calendar meaning.

    Deriving "which UTC day is this" from a monotonic clock would reset the daily
    budget at an arbitrary moment — silently granting a fresh 1500 requests in the
    middle of a Gemini day and turning the cap into decoration.
    """

    clock = FakeClock()
    midnight = datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc)
    wall = FakeClock((midnight - timedelta(seconds=5)).timestamp())
    limiter = WindowRateLimiter(key="gemini", requests_per_day=1, clock=clock, wall_clock=wall)

    assert limiter.charge(clock()) is True
    assert limiter.charge(clock()) is False
    # 5 seconds later the UTC day rolls over; monotonic time advanced the same.
    wall.advance(6.0)
    clock.advance(6.0)
    assert limiter.charge(clock()) is True


def test_monotonic_movement_alone_never_resets_the_daily_budget() -> None:
    clock = FakeClock()
    wall = FakeClock(1_700_000_000.0)
    limiter = WindowRateLimiter(key="gemini", requests_per_day=1, clock=clock, wall_clock=wall)
    assert limiter.charge(clock()) is True
    clock.advance(3600.0 * 5)  # five hours of monotonic time, same UTC day
    assert limiter.charge(clock()) is False


def test_minute_window_memory_is_bounded_by_the_limit_itself() -> None:
    """A tight loop must not grow the window past ``maxlen`` (LAW 6)."""

    clock = FakeClock()
    limiter = WindowRateLimiter(key="gemini", requests_per_minute=5, clock=clock)
    for _ in range(10_000):
        limiter.charge(clock())
    assert len(limiter._minute_window) <= 5  # noqa: SLF001 — bounded-memory assertion
    snapshot = limiter.snapshot(clock())
    assert snapshot["requests_per_minute"] == 5
    assert snapshot["throttled"] >= 9_995


def test_limiter_rejects_a_zero_or_negative_limit() -> None:
    with pytest.raises(ValueError):
        WindowRateLimiter(key="gemini", requests_per_minute=0)
    with pytest.raises(ValueError):
        WindowRateLimiter(key="gemini", requests_per_day=-1)


def test_snapshot_reports_charged_and_throttled_counts() -> None:
    clock = FakeClock()
    limiter = WindowRateLimiter(key="gemini", requests_per_minute=1, clock=clock)
    limiter.charge(clock())
    limiter.charge(clock())
    snapshot = limiter.snapshot(clock())
    assert snapshot["charged"] == 1
    assert snapshot["throttled"] == 1
    assert snapshot["minute_used"] == 1
    assert snapshot["minute_remaining"] == 0
