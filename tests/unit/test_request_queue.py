"""Lifecycle, fairness, quota, and retry contracts for GeminiRequestQueue."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from nexus_ai_agent.features.request_queue import (
    GeminiRequestQueue,
    Priority,
    RequestQueueClosedError,
)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before deadline")
        await asyncio.sleep(0)


class _StatusError(RuntimeError):
    def __init__(self, status_code: int, message: str = "provider rejected request") -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.asyncio
async def test_same_user_timeout_releases_exactly_one_pending_slot() -> None:
    queue = GeminiRequestQueue(max_rpm=10, max_daily=10)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first() -> str:
        first_started.set()
        await release_first.wait()
        return "first"

    async def second() -> str:
        second_started.set()
        return "second"

    first_task = asyncio.create_task(queue.submit(first, user_id=7, timeout=2))
    try:
        await first_started.wait()
        second_task = asyncio.create_task(queue.submit(second, user_id=7, timeout=0.02))
        await _wait_until(lambda: queue.queue_position(7) == 2)

        with pytest.raises(TimeoutError, match="timed out"):
            await second_task

        # The first provider call is still active; timing out the second request
        # must not erase the first request from per-user accounting.
        assert queue.queue_position(7) == 1
        assert not second_started.is_set()
        assert queue.get_status()["queue_size"] == 0
        assert queue.get_status()["pending_requests"] == 1
        assert queue.get_status()["logical_timed_out"] == 1

        release_first.set()
        assert await first_task == "first"
        assert queue.queue_position(7) == 0
        assert queue.get_status()["pending_requests"] == 0
    finally:
        release_first.set()
        await queue.close()
        await asyncio.gather(first_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_withdrawn_queued_requests_never_run_after_backlog_clears() -> None:
    queue = GeminiRequestQueue(max_rpm=10, max_daily=10)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    abandoned_called = asyncio.Event()
    cancelled_called = asyncio.Event()

    async def first() -> str:
        first_started.set()
        await release_first.wait()
        return "first"

    async def abandoned() -> str:
        abandoned_called.set()
        return "must-not-run"

    async def cancelled() -> str:
        cancelled_called.set()
        return "must-not-run"

    first_task = asyncio.create_task(queue.submit(first, user_id=1, timeout=2))
    abandoned_task: asyncio.Task[str] | None = None
    cancelled_task: asyncio.Task[str] | None = None
    try:
        await first_started.wait()
        abandoned_task = asyncio.create_task(queue.submit(abandoned, user_id=2, timeout=0.02))
        with pytest.raises(TimeoutError, match="timed out"):
            await abandoned_task

        cancelled_task = asyncio.create_task(queue.submit(cancelled, user_id=3, timeout=2))
        await _wait_until(lambda: queue.get_status()["queue_size"] == 1)
        cancelled_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_task
        assert queue.get_status()["queue_size"] == 0

        release_first.set()
        assert await first_task == "first"
        await asyncio.sleep(0)

        assert not abandoned_called.is_set()
        assert not cancelled_called.is_set()
        status = queue.get_status()
        assert status["provider_attempts"] == 1
        assert status["pending_requests"] == 0
        assert status["logical_timed_out"] == 1
        assert status["logical_cancelled"] == 1
    finally:
        release_first.set()
        await queue.close()
        await asyncio.gather(first_task, return_exceptions=True)
        if abandoned_task is not None:
            await asyncio.gather(abandoned_task, return_exceptions=True)
        if cancelled_task is not None:
            await asyncio.gather(cancelled_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_timeout_during_rate_wait_wakes_worker_without_provider_invocation() -> None:
    queue = GeminiRequestQueue(max_rpm=1, max_daily=10)
    rate_wait_provider_called = asyncio.Event()
    try:
        assert await queue.submit(lambda: _result("first"), user_id=1) == "first"
        waiting = asyncio.create_task(
            queue.submit(lambda: _mark_event(rate_wait_provider_called), user_id=2, timeout=0.03)
        )
        await _wait_until(lambda: queue.get_status()["rate_waiting_requests"] == 1)

        with pytest.raises(TimeoutError, match="timed out"):
            await waiting
        await _wait_until(lambda: queue.get_status()["rate_waiting_requests"] == 0)

        status = queue.get_status()
        assert not rate_wait_provider_called.is_set()
        assert status["provider_attempts"] == 1
        assert status["pending_requests"] == 0
        assert status["active_requests"] == 0
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_to_running_provider_task() -> None:
    queue = GeminiRequestQueue(max_rpm=10, max_daily=10)
    provider_started = asyncio.Event()
    provider_cancelled = asyncio.Event()

    async def provider() -> str:
        provider_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()
            raise
        return "unreachable"

    caller = asyncio.create_task(queue.submit(provider, user_id=4, timeout=5))
    try:
        await provider_started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await asyncio.wait_for(provider_cancelled.wait(), timeout=1)
        await _wait_until(lambda: queue.get_status()["pending_requests"] == 0)
        assert queue.queue_position(4) == 0
        assert queue.get_status()["logical_cancelled"] == 1
        assert queue.get_status()["provider_attempts"] == 1
    finally:
        processor = queue._processor_task
        if not provider_cancelled.is_set() and processor is not None and not processor.done():
            # A broken cancellation path can detach its active request from
            # close(); cancel the worker directly so this negative test itself
            # does not strand an async task after it has exposed that defect.
            processor.cancel()
            await asyncio.wait_for(provider_cancelled.wait(), timeout=1)
            # The cancellation mutation can leave the worker's cancellation
            # request translated to a caller withdrawal; wake that worker only
            # for deterministic test cleanup after the assertion fails.
            queue._closed = True
            queue._ready_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(processor), timeout=1)
            except asyncio.CancelledError:
                if not processor.cancelled():
                    raise
        else:
            await queue.close()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_settles_active_and_queued_waiters_and_rejects_new_work() -> None:
    queue = GeminiRequestQueue(max_rpm=10, max_daily=10)
    provider_started = asyncio.Event()
    provider_cancelled = asyncio.Event()

    async def active_provider() -> str:
        provider_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()
            raise
        return "unreachable"

    async def queued_provider() -> str:
        pytest.fail("queued provider ran after queue close")

    active = asyncio.create_task(queue.submit(active_provider, user_id=10, timeout=5))
    queued: asyncio.Task[str] | None = None
    try:
        await provider_started.wait()
        queued = asyncio.create_task(queue.submit(queued_provider, user_id=11, timeout=5))
        await _wait_until(lambda: queue.get_status()["pending_requests"] == 2)

        await queue.close()
        outcomes = await asyncio.gather(active, queued, return_exceptions=True)
        assert all(isinstance(outcome, RequestQueueClosedError) for outcome in outcomes)
        assert provider_cancelled.is_set()
        status = queue.get_status()
        assert status["closed"] is True
        assert status["queue_size"] == 0
        assert status["active_requests"] == 0
        assert status["pending_requests"] == 0
        assert status["logical_closed"] == 2
        assert queue.queue_position(10) == 0
        assert queue.queue_position(11) == 0

        with pytest.raises(RequestQueueClosedError):
            await queue.submit(queued_provider, user_id=12)
        await queue.close()
    finally:
        await queue.close()
        await asyncio.gather(active, return_exceptions=True)
        if queued is not None:
            await asyncio.gather(queued, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_cancellation_settles_active_work_and_close_is_idempotent() -> None:
    queue = GeminiRequestQueue(max_rpm=10, max_daily=10)
    provider_started = asyncio.Event()
    provider_cancelled = asyncio.Event()

    async def provider() -> str:
        provider_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            provider_cancelled.set()
            raise
        return "unreachable"

    caller = asyncio.create_task(queue.submit(provider, user_id=8, timeout=5))
    try:
        await provider_started.wait()
        processor = queue._processor_task
        assert processor is not None
        processor.cancel()

        outcome = await asyncio.gather(caller, return_exceptions=True)
        assert isinstance(outcome[0], RequestQueueClosedError)
        assert provider_cancelled.is_set()
        assert queue.get_status()["pending_requests"] == 0
        assert queue.get_status()["logical_closed"] == 1

        # close() remains safe when the worker has already been cancelled by
        # application shutdown rather than by the queue's own close path.
        await queue.close()
        assert queue.get_status()["closed"] is True
    finally:
        await queue.close()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_round_robin_rotates_users_within_a_priority_tier() -> None:
    queue = GeminiRequestQueue(max_rpm=20, max_daily=20)
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()
    order: list[str] = []

    async def blocker() -> str:
        blocker_started.set()
        await release_blocker.wait()
        return "released"

    async def record(label: str) -> str:
        order.append(label)
        return label

    blocking = asyncio.create_task(queue.submit(blocker, user_id=99, timeout=3))
    queued: list[asyncio.Task[str]] = []
    try:
        await blocker_started.wait()
        queued = [
            asyncio.create_task(queue.submit(lambda: record("user-1-a"), user_id=1, timeout=1)),
            asyncio.create_task(queue.submit(lambda: record("user-1-b"), user_id=1, timeout=1)),
            asyncio.create_task(queue.submit(lambda: record("user-1-c"), user_id=1, timeout=1)),
            asyncio.create_task(queue.submit(lambda: record("user-2-a"), user_id=2, timeout=1)),
        ]
        await _wait_until(lambda: queue.get_status()["queue_size"] == 4)
        release_blocker.set()
        assert await blocking == "released"
        assert await asyncio.gather(*queued) == [
            "user-1-a",
            "user-1-b",
            "user-1-c",
            "user-2-a",
        ]
        assert order == ["user-1-a", "user-2-a", "user-1-b", "user-1-c"]
    finally:
        release_blocker.set()
        await queue.close()
        await asyncio.gather(blocking, *queued, return_exceptions=True)


@pytest.mark.asyncio
async def test_priority_order_is_preserved_across_user_lanes() -> None:
    queue = GeminiRequestQueue(max_rpm=20, max_daily=20)
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()
    order: list[str] = []

    async def blocker() -> str:
        blocker_started.set()
        await release_blocker.wait()
        return "released"

    async def record(label: str) -> str:
        order.append(label)
        return label

    blocking = asyncio.create_task(queue.submit(blocker, user_id=9, timeout=3))
    queued: list[asyncio.Task[str]] = []
    try:
        await blocker_started.wait()
        queued = [
            asyncio.create_task(
                queue.submit(lambda: record("low"), user_id=1, priority=Priority.LOW)
            ),
            asyncio.create_task(
                queue.submit(lambda: record("normal"), user_id=2, priority=Priority.NORMAL)
            ),
            asyncio.create_task(
                queue.submit(lambda: record("owner"), user_id=3, priority=Priority.OWNER)
            ),
        ]
        await _wait_until(lambda: queue.get_status()["queue_size"] == 3)
        release_blocker.set()
        await blocking
        assert await asyncio.gather(*queued) == ["low", "normal", "owner"]
        assert order == ["owner", "normal", "low"]
    finally:
        release_blocker.set()
        await queue.close()
        await asyncio.gather(blocking, *queued, return_exceptions=True)


@pytest.mark.asyncio
async def test_continuous_higher_priority_work_can_starve_lower_priority_lane() -> None:
    queue = GeminiRequestQueue(max_rpm=20, max_daily=20)
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()
    low_started = asyncio.Event()
    high_started = [asyncio.Event() for _ in range(3)]
    high_releases = [asyncio.Event() for _ in range(3)]
    order: list[str] = []

    async def blocker() -> str:
        blocker_started.set()
        await release_blocker.wait()
        return "blocker"

    async def low() -> str:
        low_started.set()
        order.append("low")
        return "low"

    async def high(index: int) -> str:
        high_started[index].set()
        order.append(f"high-{index}")
        await high_releases[index].wait()
        return f"high-{index}"

    blocker_task = asyncio.create_task(
        queue.submit(blocker, user_id=90, priority=Priority.LOW, timeout=3)
    )
    low_task: asyncio.Task[str] | None = None
    high_tasks: list[asyncio.Task[str]] = []
    try:
        await asyncio.wait_for(blocker_started.wait(), timeout=1)
        low_task = asyncio.create_task(
            queue.submit(low, user_id=1, priority=Priority.LOW, timeout=3)
        )
        high_tasks.append(
            asyncio.create_task(
                queue.submit(lambda: high(0), user_id=2, priority=Priority.OWNER, timeout=3)
            )
        )
        await _wait_until(lambda: queue.get_status()["queue_size"] == 2)

        release_blocker.set()
        assert await blocker_task == "blocker"
        await asyncio.wait_for(high_started[0].wait(), timeout=1)

        high_tasks.append(
            asyncio.create_task(
                queue.submit(lambda: high(1), user_id=3, priority=Priority.OWNER, timeout=3)
            )
        )
        await _wait_until(lambda: queue.get_status()["queue_size"] == 2)
        high_releases[0].set()
        await asyncio.wait_for(high_started[1].wait(), timeout=1)
        assert not low_started.is_set()

        high_tasks.append(
            asyncio.create_task(
                queue.submit(lambda: high(2), user_id=4, priority=Priority.OWNER, timeout=3)
            )
        )
        await _wait_until(lambda: queue.get_status()["queue_size"] == 2)
        high_releases[1].set()
        await asyncio.wait_for(high_started[2].wait(), timeout=1)
        assert not low_started.is_set()

        high_releases[2].set()
        await asyncio.wait_for(low_started.wait(), timeout=1)
        assert await low_task == "low"
        assert await asyncio.gather(*high_tasks) == ["high-0", "high-1", "high-2"]
        assert order == ["high-0", "high-1", "high-2", "low"]
    finally:
        release_blocker.set()
        for release in high_releases:
            release.set()
        await queue.close()
        await asyncio.gather(blocker_task, *high_tasks, return_exceptions=True)
        if low_task is not None:
            await asyncio.gather(low_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_each_typed_retry_consumes_a_separate_provider_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = GeminiRequestQueue(max_rpm=10, max_daily=2, max_retries=1)
    attempts = 0

    async def provider() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _StatusError(503)
        return "ok"

    monkeypatch.setattr(queue, "_retry_delay_seconds", lambda _exc, _number: 0.0)
    try:
        assert await queue.submit(provider, user_id=1) == "ok"
        status = queue.get_status()
        assert attempts == 2
        assert status["provider_attempts"] == 2
        assert status["daily_used"] == 2
        assert status["logical_submitted"] == 1
        assert status["logical_completed"] == 1
        assert status["logical_succeeded"] == 1
        assert status["logical_failed"] == 0
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_typed_retries_stop_at_configured_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = GeminiRequestQueue(max_rpm=10, max_daily=10, max_retries=2)
    attempts = 0

    async def always_transiently_fails() -> str:
        nonlocal attempts
        attempts += 1
        raise _StatusError(503)

    monkeypatch.setattr(queue, "_retry_delay_seconds", lambda _exc, _number: 0.0)
    try:
        assert await queue.submit(always_transiently_fails, user_id=1) == (
            "❌ AI request failed. Please try again later."
        )
        status = queue.get_status()
        assert attempts == 3  # initial attempt plus two configured retries
        assert status["provider_attempts"] == 3
        assert status["daily_used"] == 3
        assert status["logical_failed"] == 1
        assert status["logical_completed"] == 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_retry_requires_structured_transient_status_and_honors_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_status_queue = GeminiRequestQueue(max_rpm=10, max_daily=10, max_retries=2)
    no_retry_queue = GeminiRequestQueue(max_rpm=10, max_daily=10, retry_on_429=False, max_retries=2)
    no_status_attempts = 0
    no_retry_attempts = 0

    async def text_only_error() -> str:
        nonlocal no_status_attempts
        no_status_attempts += 1
        raise RuntimeError("429 temporarily unavailable")

    async def typed_but_disabled() -> str:
        nonlocal no_retry_attempts
        no_retry_attempts += 1
        raise _StatusError(503)

    monkeypatch.setattr(no_status_queue, "_retry_delay_seconds", lambda _exc, _number: 0.0)
    monkeypatch.setattr(no_retry_queue, "_retry_delay_seconds", lambda _exc, _number: 0.0)
    try:
        assert await no_status_queue.submit(text_only_error, user_id=1) == (
            "❌ AI request failed. Please try again later."
        )
        assert await no_retry_queue.submit(typed_but_disabled, user_id=2) == (
            "❌ AI request failed. Please try again later."
        )
        assert no_status_attempts == 1
        assert no_retry_attempts == 1
        assert no_status_queue.get_status()["provider_attempts"] == 1
        assert no_retry_queue.get_status()["provider_attempts"] == 1
        assert no_status_queue.get_status()["logical_failed"] == 1
        assert no_retry_queue.get_status()["logical_failed"] == 1
    finally:
        await no_status_queue.close()
        await no_retry_queue.close()


@pytest.mark.asyncio
async def test_retry_after_is_honored_and_excessive_wait_is_not_shortened() -> None:
    queue = GeminiRequestQueue(max_rpm=10, max_daily=10, max_retries=1)
    error = _StatusError(429)
    error.response = SimpleNamespace(headers={"Retry-After": "5"})
    delay = queue._retry_delay_seconds(error, 0)
    assert delay is not None and 5.0 <= delay <= 6.0

    date_error = _StatusError(429)
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=20)
    date_error.response = SimpleNamespace(
        headers={"Retry-After": format_datetime(retry_at, usegmt=True)}
    )
    date_delay = queue._retry_delay_seconds(date_error, 0)
    assert date_delay is not None and 19.0 <= date_delay <= 21.0

    excessive = _StatusError(429)
    excessive.response = SimpleNamespace(headers={"Retry-After": "31"})
    assert queue._retry_delay_seconds(excessive, 0) is None
    attempts = 0

    async def provider() -> str:
        nonlocal attempts
        attempts += 1
        raise excessive

    try:
        assert await queue.submit(provider, user_id=1) == (
            "❌ AI request failed. Please try again later."
        )
        assert attempts == 1
        assert queue.get_status()["provider_attempts"] == 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_daily_quota_rollover_rechecks_capacity_while_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = GeminiRequestQueue(max_rpm=10, max_daily=1)
    try:
        assert await queue.submit(lambda: _result("first"), user_id=1) == "first"
        initial_day = queue._day
        current_day = [initial_day]
        monkeypatch.setattr(queue, "_utc_day_key", lambda: current_day[0])
        monkeypatch.setattr(queue, "_seconds_until_utc_day_rollover", lambda: 0.01)
        original_wait = queue._wait_for_cancellation

        async def cross_midnight(req: object, _delay: float) -> bool:
            current_day[0] = (initial_day[0], initial_day[1] + 1)
            return False

        monkeypatch.setattr(queue, "_wait_for_cancellation", cross_midnight)
        assert await queue.submit(lambda: _result("next-day"), user_id=2, timeout=1) == ("next-day")
        status = queue.get_status()
        assert status["provider_attempts"] == 2
        assert status["daily_used"] == 1
        assert status["daily_remaining"] == 0
        monkeypatch.setattr(queue, "_wait_for_cancellation", original_wait)
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_invalid_limits_and_negative_timeout_fail_before_acceptance() -> None:
    with pytest.raises(ValueError, match="max_rpm"):
        GeminiRequestQueue(max_rpm=0)
    with pytest.raises(ValueError, match="max_daily"):
        GeminiRequestQueue(max_daily=0)
    with pytest.raises(ValueError, match="max_retries"):
        GeminiRequestQueue(max_retries=-1)

    queue = GeminiRequestQueue()
    try:
        with pytest.raises(ValueError, match="timeout"):
            await queue.submit(lambda: _result("unused"), timeout=-0.1)
        assert queue.get_status()["logical_submitted"] == 0
    finally:
        await queue.close()


async def _result(value: str) -> str:
    return value


async def _mark_event(event: asyncio.Event) -> str:
    event.set()
    return "unexpected"
