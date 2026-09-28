"""Resilience primitives: backoff/jitter, circuit breaker, window rate limiter.

Three small, independent, single-event-loop-safe mechanisms. None of them spawns
a task, holds a lock across an ``await``, or keeps unbounded state — each is a
pure-ish state machine over a caller-supplied monotonic clock, which is what
makes them testable without sleeping in real time.

Safety argument for the missing locks: every method here runs to completion
without awaiting, so on a single-threaded asyncio event loop the mutations are
atomic with respect to other coroutines. The gateway never calls these from a
thread; ``tests/unit/test_llm_gateway_ratelimit.py`` pins that assumption by
driving hundreds of concurrent coroutines through one limiter and asserting the
window bound is never exceeded.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

from nexus_ai_agent.llm.errors import LLMErrorKind
from nexus_ai_agent.llm.gateway.contract import MONOTONIC, Clock
from nexus_ai_agent.llm.gateway.policy import CircuitPolicy, JitterMode, RetryPolicy

__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "WindowRateLimiter",
    "compute_backoff",
    "parse_retry_after",
]


# ═══════════════════════════════════════════════════════════════════════════
# Backoff + jitter
# ═══════════════════════════════════════════════════════════════════════════


def compute_backoff(
    attempt_index: int,
    policy: RetryPolicy,
    *,
    rng: random.Random,
    previous_delay: float | None = None,
) -> float:
    """Delay before retry number *attempt_index* (0-based: first retry = 0).

    Implements the three AWS jitter strategies plus a deterministic mode
    (https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/).
    ``FULL`` is the default: it minimises total work and peak server load, which
    is what a free-tier provider quota actually cares about.
    """

    cap = policy.max_delay_seconds
    base = policy.base_delay_seconds
    exponent = min(attempt_index, 30)  # bound the shift; 2**30 already exceeds any cap
    exponential = min(cap, base * (policy.multiplier**exponent))

    if policy.jitter is JitterMode.NONE:
        return max(0.0, exponential)
    if policy.jitter is JitterMode.FULL:
        draw = rng.uniform(0.0, exponential) if exponential > 0 else 0.0
    elif policy.jitter is JitterMode.EQUAL:
        half = exponential / 2.0
        draw = half + rng.uniform(0.0, half)
    else:
        # DECORRELATED: grows from the previous sleep, not from the attempt number.
        previous = base if previous_delay is None else max(base, previous_delay)
        draw = min(cap, rng.uniform(base, previous * 3.0))
    # An injected rng is a seam (tests, future pluggable strategies) and a draw
    # outside [0, 1] would produce a negative sleep. A delay is a duration: it
    # is clamped here so no caller can ever schedule one in the past.
    return max(0.0, draw)


def _usable_delay(seconds: float) -> float | None:
    """A delay we are willing to sleep for: finite, and not in the past.

    ``None`` means "the provider gave us nothing usable" and the caller falls
    back to computed backoff — which is bounded by policy.
    """

    if not math.isfinite(seconds) or seconds < 0.0:
        return None
    return seconds


def parse_retry_after(value: Any, *, now_wall: float | None = None) -> float | None:
    """Parse an RFC 9110 ``Retry-After`` into seconds, or ``None`` if unusable.

    Accepts both legal forms — delta-seconds (``"120"``) and an HTTP-date
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``) — plus an already-numeric value from
    a structured provider error. Malformed input yields ``None`` so the caller
    falls back to computed backoff rather than guessing.

    Two different "past" cases are handled differently, and the distinction is
    the whole point:

    * an **HTTP-date** that has already passed is a legal value with a legal
      meaning — the wait is over — so it yields ``0.0``;
    * a **negative delta-seconds** is malformed (RFC 9110 defines it as a
      non-negative integer), so it yields ``None``. Clamping it to ``0.0`` would
      mean "retry immediately", which is how a provider that emits a bogus
      ``Retry-After: -1`` turns into a hot retry loop against a service that is
      already refusing us.
    * ``nan``/``inf`` in either form yield ``None``: ``asyncio.sleep(nan)`` is
      undefined and ``asyncio.sleep(inf)`` holds the caller's deadline hostage.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _usable_delay(float(value))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        pass
    else:
        return _usable_delay(parsed)
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    reference = (
        datetime.now(timezone.utc)
        if now_wall is None
        else datetime.fromtimestamp(now_wall, tz=timezone.utc)
    )
    return max(0.0, (retry_at - reference).total_seconds())


# ═══════════════════════════════════════════════════════════════════════════
# Circuit breaker
# ═══════════════════════════════════════════════════════════════════════════


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Per-route breaker: closed → open (after N health failures) → half-open probe.

    Only kinds in ``policy.counted_kinds`` move the breaker. A malformed request
    we built, a content block, or a validation failure says nothing about whether
    the provider is alive, and opening the circuit on those would shed healthy
    traffic because of our own bug.
    """

    key: str
    policy: CircuitPolicy
    clock: Clock = MONOTONIC
    _consecutive_failures: int = 0
    _consecutive_successes: int = 0
    _opened_at: float | None = None
    _probes_in_flight: int = 0
    _trip_count: int = 0

    def state(self, now: float | None = None) -> CircuitState:
        if not self.policy.enabled:
            return CircuitState.CLOSED
        moment = self.clock() if now is None else now
        if self._opened_at is None:
            return CircuitState.CLOSED
        if moment - self._opened_at < self.policy.recovery_seconds:
            return CircuitState.OPEN
        return CircuitState.HALF_OPEN

    def is_open(self, now: float | None = None) -> bool:
        """True when the route must not be used (OPEN, or HALF_OPEN with no probe slot)."""

        state = self.state(now)
        if state is CircuitState.OPEN:
            return True
        if state is CircuitState.HALF_OPEN:
            return self._probes_in_flight >= self.policy.half_open_max_probes
        return False

    def allow(self, now: float | None = None) -> bool:
        """Reserve permission to try this route. Claims a half-open probe slot."""

        if not self.policy.enabled:
            return True
        state = self.state(now)
        if state is CircuitState.OPEN:
            return False
        if state is CircuitState.HALF_OPEN:
            if self._probes_in_flight >= self.policy.half_open_max_probes:
                return False
            self._probes_in_flight += 1
        return True

    def release_probe(self) -> None:
        """Give back a probe slot without recording an outcome (e.g. cancellation)."""

        if self._probes_in_flight > 0:
            self._probes_in_flight -= 1

    def record_success(self, now: float | None = None) -> None:
        _ = now  # success closes the circuit immediately; no timing decision
        self._consecutive_failures = 0
        if self._probes_in_flight > 0:
            self._probes_in_flight -= 1
        if self._opened_at is None:
            self._consecutive_successes = 0
            return
        self._consecutive_successes += 1
        if self._consecutive_successes >= self.policy.success_threshold:
            self._opened_at = None
            self._consecutive_successes = 0

    def record_failure(self, kind: LLMErrorKind, now: float | None = None) -> None:
        if not self.policy.enabled or not self.policy.counts(kind):
            return
        moment = self.clock() if now is None else now
        if self._probes_in_flight > 0:
            self._probes_in_flight -= 1
        self._consecutive_successes = 0
        self._consecutive_failures += 1
        if self._opened_at is None:
            if self._consecutive_failures >= self.policy.failure_threshold:
                self._opened_at = moment
                self._trip_count += 1
                self._consecutive_failures = 0
            return
        # A failed half-open probe re-opens for another full recovery window.
        self._opened_at = moment
        self._trip_count += 1

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state(now).value,
            "consecutive_failures": self._consecutive_failures,
            "open": self.is_open(now),
            "trips": self._trip_count,
            "probes_in_flight": self._probes_in_flight,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Window rate limiter
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class WindowRateLimiter:
    """Rolling-minute + UTC-day limiter for one provider.

    Memory is bounded by construction: the minute window is a ``deque`` with
    ``maxlen=requests_per_minute``, so it can never grow past the limit it
    enforces even if :meth:`charge` is called in a tight loop. The daily counter
    is one integer that resets on the UTC day boundary (Gemini's RPD resets on a
    fixed daily boundary — https://ai.google.dev/gemini-api/docs/rate-limits).

    ``None`` limits mean "unbounded on that axis", which is what a local model
    needs.
    """

    key: str
    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    #: Monotonic clock for window arithmetic — durations must not jump when the
    #: wall clock is stepped by NTP or an operator.
    clock: Clock = MONOTONIC
    #: Wall clock, used *only* to find the UTC day boundary. The two clocks are
    #: deliberately separate: ``time.monotonic()`` has no calendar meaning, so
    #: deriving "which UTC day is this" from it would silently reset the daily
    #: budget at an arbitrary moment.
    wall_clock: Callable[[], float] = time.time
    _minute_window: deque[float] = field(default_factory=deque)
    _day_count: int = 0
    _day_stamp: tuple[int, int] = field(init=False, default=(0, 0))
    _charged: int = 0
    _throttled: int = 0

    def __post_init__(self) -> None:
        if self.requests_per_minute is not None and self.requests_per_minute < 1:
            raise ValueError("requests_per_minute must be >= 1 or None")
        if self.requests_per_day is not None and self.requests_per_day < 1:
            raise ValueError("requests_per_day must be >= 1 or None")
        if self.requests_per_minute is not None:
            self._minute_window = deque(maxlen=self.requests_per_minute)
        self._day_stamp = _utc_day_stamp_from(self.wall_clock())

    def _roll_day(self) -> None:
        stamp = _utc_day_stamp_from(self.wall_clock())
        if stamp != self._day_stamp:
            self._day_stamp = stamp
            self._day_count = 0

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        while self._minute_window and self._minute_window[0] <= cutoff:
            self._minute_window.popleft()

    def seconds_until_capacity(self, now: float | None = None) -> float:
        """0.0 when a unit is available now; otherwise the wait needed.

        ``float("inf")`` means the daily budget is gone and no amount of waiting
        inside this process will recover it — the caller must fall back or fail,
        not hang.
        """

        moment = self.clock() if now is None else now
        self._roll_day()
        if self.requests_per_day is not None and self._day_count >= self.requests_per_day:
            return float("inf")
        if self.requests_per_minute is None:
            return 0.0
        self._prune(moment)
        if len(self._minute_window) < self.requests_per_minute:
            return 0.0
        # The window is full: capacity returns when the oldest entry ages out.
        return max(0.0, 60.0 - (moment - self._minute_window[0]))

    def charge(self, now: float | None = None) -> bool:
        """Consume one unit. ``False`` when the window is full (nothing consumed)."""

        moment = self.clock() if now is None else now
        self._roll_day()
        if self.requests_per_day is not None and self._day_count >= self.requests_per_day:
            self._throttled += 1
            return False
        if self.requests_per_minute is not None:
            self._prune(moment)
            if len(self._minute_window) >= self.requests_per_minute:
                self._throttled += 1
                return False
            self._minute_window.append(moment)
        self._day_count += 1
        self._charged += 1
        return True

    def record_throttled(self) -> None:
        """Count a refusal the *gateway's wait policy* made.

        :meth:`charge` counts the refusals the limiter makes itself (a full
        window, a spent daily budget). The engine also refuses when the wait a
        unit would need exceeds ``RateLimitPolicy.max_wait_seconds`` or the
        caller's remaining deadline — and unless that is counted here too, an
        operator reading ``throttled`` sees quota shedding as zero while callers
        are being turned away (LAW 10: the numbers describe what happened).
        """

        self._throttled += 1

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        moment = self.clock() if now is None else now
        self._roll_day()
        self._prune(moment)
        return {
            "key": self.key,
            "requests_per_minute": self.requests_per_minute,
            "requests_per_day": self.requests_per_day,
            "minute_used": len(self._minute_window),
            "minute_remaining": (
                None
                if self.requests_per_minute is None
                else max(0, self.requests_per_minute - len(self._minute_window))
            ),
            "day_used": self._day_count,
            "day_remaining": (
                None
                if self.requests_per_day is None
                else max(0, self.requests_per_day - self._day_count)
            ),
            "charged": self._charged,
            "throttled": self._throttled,
        }


def _utc_day_stamp_from(moment: float) -> tuple[int, int]:
    stamp = datetime.fromtimestamp(moment, tz=timezone.utc)
    return (stamp.year, stamp.timetuple().tm_yday)
