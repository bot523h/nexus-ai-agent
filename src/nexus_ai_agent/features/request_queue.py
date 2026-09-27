"""In-process async request queue for Gemini calls.

The queue serializes provider invocations within one process, applies request
and daily attempt limits, and rotates users within each priority tier. It is
not a durable or cross-process queue. Strict priority is retained across tiers,
so a continuously busy higher-priority tier can starve lower-priority work.

A caller timeout/cancellation withdraws work that has not started and requests
cancellation of the local provider coroutine if it is already running. Once a
request has been sent to a remote provider, cancellation cannot prove that the
remote service stopped processing it; the remote outcome may be unknown.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum, IntEnum
from typing import Any

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

_FAILURE_MESSAGE = "❌ AI request failed. Please try again later."
_TIMEOUT_MESSAGE = "⏳ Your request timed out in the AI queue. Please try again later."
_CLOSED_MESSAGE = "AI request queue is closed."
_MAX_RETRY_BACKOFF_SECONDS = 30.0


class Priority(IntEnum):
    """Request priority — lower value is selected first."""

    OWNER = 0
    REFERRAL_BONUS = 1
    NORMAL = 2
    LOW = 3


class RequestQueueClosedError(RuntimeError):
    """Raised when work is submitted after queue shutdown."""


class _RequestState(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CLOSED = "closed"


class _RequestCancelled(Exception):
    """Internal signal: a caller withdrew this request or the queue closed."""


class _RequestTimedOut(Exception):
    """Internal signal: the absolute caller deadline elapsed before completion."""


@dataclass(eq=False)
class _Request:
    """One logical request, tracked independently from provider attempts."""

    request_id: int
    priority: Priority
    future: asyncio.Future[str]
    coro_factory: Callable[[], Coroutine[Any, Any, str]]
    user_id: int
    deadline: float | None
    cancel_event: asyncio.Event
    state: _RequestState = field(default=_RequestState.QUEUED, init=False)
    enqueued: bool = field(default=False, init=False)
    accounted: bool = field(default=True, init=False)
    execution_task: asyncio.Task[str] | None = field(default=None, init=False)
    provider_attempts: int = field(default=0, init=False)
    failed: bool = field(default=False, init=False)


class GeminiRequestQueue:
    """Serial in-process queue for Gemini API calls.

    ``max_retries`` is the maximum number of retries *after* the initial
    provider attempt. Only exceptions carrying a structured HTTP status code
    of 429 or 5xx are retryable. Error-message text and successful string
    results are never parsed to infer a retry.

    ``retry_on_429`` remains the public compatibility switch for this queue's
    transient-status retry policy (429 and 5xx). Each retry returns to the
    shared capacity gate and consumes a separate attempt from RPM/daily quota.
    """

    def __init__(
        self,
        max_rpm: int = 15,
        max_daily: int = 1500,
        retry_on_429: bool = True,
        max_retries: int = 2,
    ) -> None:
        if max_rpm <= 0:
            raise ValueError("max_rpm must be positive")
        if max_daily <= 0:
            raise ValueError("max_daily must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")

        self._max_rpm = max_rpm
        self._max_daily = max_daily
        self._retry_on_429 = retry_on_429
        self._max_retries = max_retries

        # Per-priority tenant lanes plus a round-robin user ring. The same
        # user's requests stay FIFO within a tier; each turn serves one item.
        self._user_queues: dict[Priority, dict[int, deque[_Request]]] = {
            priority: {} for priority in Priority
        }
        self._user_rotation: dict[Priority, deque[int]] = {
            priority: deque() for priority in Priority
        }
        self._queued_count = 0
        self._pending_count = 0
        self._pending_per_user: dict[int, int] = {}

        self._minute_timestamps: list[float] = []
        self._daily_count = 0
        self._day = self._utc_day_key()

        self._lock = asyncio.Lock()
        self._ready_event = asyncio.Event()
        self._processor_task: asyncio.Task[None] | None = None
        self._active_request: _Request | None = None
        self._rate_waiting_request_id: int | None = None
        self._request_id = 0
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

        self._logical_submitted = 0
        self._logical_completed = 0
        self._logical_succeeded = 0
        self._logical_failed = 0
        self._logical_cancelled = 0
        self._logical_timed_out = 0
        self._logical_closed = 0
        self._provider_attempts = 0

    # ── Public API ──────────────────────────────────────────────────

    async def submit(
        self,
        coro_factory: Callable[[], Coroutine[Any, Any, str]],
        *,
        user_id: int = 0,
        priority: Priority | int = Priority.NORMAL,
        timeout: float | None = 120.0,
    ) -> str:
        """Queue a provider coroutine and wait for its result.

        A timeout includes time spent queued, waiting for rate capacity, and
        executing. If it expires before provider start, the factory is never
        invoked. During a provider call, cancellation is best-effort locally;
        a remote provider may already have received and processed the request.

        Raises:
            TimeoutError: The caller's deadline expired.
            RequestQueueClosedError: The queue is closing or already closed.
            TypeError or ValueError: The factory, priority, or timeout is invalid.
        """
        if not callable(coro_factory):
            raise TypeError("coro_factory must be callable")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout cannot be negative")
        if isinstance(priority, bool):
            raise ValueError("priority must be a Priority value")
        try:
            request_priority = Priority(priority)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid request priority: {priority!r}") from exc

        loop = asyncio.get_running_loop()
        self._bind_loop(loop)
        deadline = loop.time() + timeout if timeout is not None else None

        async with self._lock:
            if self._closed:
                raise RequestQueueClosedError(_CLOSED_MESSAGE)
            if deadline is not None and loop.time() >= deadline:
                raise TimeoutError(_TIMEOUT_MESSAGE)

            self._request_id += 1
            future: asyncio.Future[str] = loop.create_future()
            req = _Request(
                request_id=self._request_id,
                priority=request_priority,
                future=future,
                coro_factory=coro_factory,
                user_id=user_id,
                deadline=deadline,
                cancel_event=asyncio.Event(),
            )
            self._enqueue_locked(req)
            self._pending_count += 1
            self._pending_per_user[user_id] = self._pending_per_user.get(user_id, 0) + 1
            self._logical_submitted += 1
            self._ensure_processor()
            self._ready_event.set()

        remaining = max(0.0, deadline - loop.time()) if deadline is not None else None
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
        except TimeoutError:
            cancelled = await self._cancel_request(req, timed_out=True)
            if not cancelled and future.done() and not future.cancelled():
                # Completion/shutdown may have won the deadline race. Preserve
                # the already-settled outcome instead of reporting a false timeout.
                return future.result()
            raise TimeoutError(_TIMEOUT_MESSAGE) from None
        except asyncio.CancelledError:
            await asyncio.shield(self._cancel_request(req, timed_out=False))
            if future.done() and not future.cancelled():
                # Consume a concurrently settled exception; cancellation remains
                # the caller-visible outcome and must not leave a warning behind.
                future.exception()
            raise

    def queue_position(self, user_id: int) -> int:
        """Return the approximate queued-plus-running count for a user."""
        return self._pending_per_user.get(user_id, 0)

    def get_status(self) -> dict[str, Any]:
        """Return process-local queue depth, quota, and lifecycle counters."""
        self._reset_day_if_needed()
        self._clean_minute_timestamps()
        return {
            # Preserve the existing meaning: items waiting for the worker,
            # excluding the one currently executing or rate-waiting.
            "queue_size": self._queued_count,
            "pending_requests": self._pending_count,
            "active_requests": int(self._active_request is not None),
            "rate_waiting_requests": int(self._rate_waiting_request_id is not None),
            "max_rpm": self._max_rpm,
            "max_daily": self._max_daily,
            "daily_used": self._daily_count,
            "rpm_remaining": max(0, self._max_rpm - len(self._minute_timestamps)),
            "daily_remaining": max(0, self._max_daily - self._daily_count),
            "logical_submitted": self._logical_submitted,
            "logical_completed": self._logical_completed,
            "logical_succeeded": self._logical_succeeded,
            "logical_failed": self._logical_failed,
            "logical_cancelled": self._logical_cancelled,
            "logical_timed_out": self._logical_timed_out,
            "logical_closed": self._logical_closed,
            "provider_attempts": self._provider_attempts,
            "closed": self._closed,
        }

    async def close(self) -> None:
        """Immediately stop accepting work and settle all queued/in-flight calls.

        Queued waiters receive ``RequestQueueClosedError``. The active local
        provider coroutine receives cancellation; this method awaits the worker
        so its local cleanup can finish. A provider coroutine that deliberately
        suppresses cancellation can therefore delay close, and no local queue
        can prove that a remote HTTP server stopped after receiving a request.
        Repeated calls are safe.
        """
        loop = asyncio.get_running_loop()
        self._bind_loop(loop)

        async with self._lock:
            self._closed = True
            queued = [
                req
                for lanes in self._user_queues.values()
                for lane in lanes.values()
                for req in tuple(lane)
            ]
            for req in queued:
                self._settle_closed_locked(req)

            active = self._active_request
            if active is not None:
                self._settle_closed_locked(active)
                if active.execution_task is not None and not active.execution_task.done():
                    active.execution_task.cancel()

            self._ready_event.set()
            processor = self._processor_task

        if processor is not None and processor is not asyncio.current_task():
            try:
                await asyncio.shield(processor)
            except asyncio.CancelledError:
                if not processor.cancelled():
                    raise

    # ── Internal lifecycle ──────────────────────────────────────────

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("GeminiRequestQueue instances are bound to one asyncio event loop")

    def _ensure_processor(self) -> None:
        if self._processor_task is None or self._processor_task.done():
            self._processor_task = asyncio.create_task(
                self._process_loop(), name="gemini-request-queue"
            )

    def _enqueue_locked(self, req: _Request) -> None:
        lanes = self._user_queues[req.priority]
        lane = lanes.get(req.user_id)
        if lane is None:
            lane = deque()
            lanes[req.user_id] = lane
            self._user_rotation[req.priority].append(req.user_id)
        lane.append(req)
        req.enqueued = True
        self._queued_count += 1

    def _remove_queued_locked(self, req: _Request) -> None:
        if not req.enqueued:
            return
        lanes = self._user_queues[req.priority]
        lane = lanes.get(req.user_id)
        if lane is not None:
            lane.remove(req)
            if not lane:
                del lanes[req.user_id]
                try:
                    self._user_rotation[req.priority].remove(req.user_id)
                except ValueError:
                    # The processor can only remove the user while holding the
                    # same lock; tolerate an already-drained empty lane.
                    pass
        req.enqueued = False
        self._queued_count -= 1

    def _next_request_locked(self) -> _Request | None:
        for priority in Priority:
            rotation = self._user_rotation[priority]
            lanes = self._user_queues[priority]
            while rotation:
                user_id = rotation.popleft()
                lane = lanes.get(user_id)
                if not lane:
                    lanes.pop(user_id, None)
                    continue

                req = lane.popleft()
                if lane:
                    rotation.append(user_id)
                else:
                    del lanes[user_id]

                req.enqueued = False
                self._queued_count -= 1
                if req.state is not _RequestState.QUEUED:
                    # Normally cancellation physically removes a queued item;
                    # this is a defensive guard against a future state change.
                    continue
                req.state = _RequestState.RUNNING
                self._active_request = req
                return req
        return None

    async def _take_next_request(self) -> _Request | None:
        while True:
            await self._ready_event.wait()
            async with self._lock:
                req = self._next_request_locked()
                if req is not None:
                    if self._queued_count == 0:
                        self._ready_event.clear()
                    return req
                self._ready_event.clear()
                if self._closed:
                    return None

    async def _process_loop(self) -> None:
        while True:
            req = await self._take_next_request()
            if req is None:
                return
            try:
                result = await self._execute(req)
            except _RequestTimedOut:
                await self._settle_timed_out(req)
            except _RequestCancelled:
                # Caller cancellation or close has already settled the request.
                await self._settle_cancelled_if_needed(req)
            except asyncio.CancelledError:
                # Keep the caller's Future and per-user accounting terminal if
                # the worker itself is cancelled during application shutdown.
                await asyncio.shield(self._settle_closed(req))
                raise
            except Exception as exc:
                # An internal queue failure is visible as a safe terminal result,
                # counted, and logged by type; it cannot strand later waiters.
                log.error(
                    "queue_processor_error",
                    request_id=req.request_id,
                    error_type=type(exc).__name__,
                )
                req.failed = True
                await self._settle_result(req, _FAILURE_MESSAGE)
            else:
                await self._settle_result(req, result)

    async def _execute(self, req: _Request) -> str:
        retry_number = 0
        while True:
            self._check_request(req)
            if not await self._wait_for_capacity(req):
                raise _RequestCancelled
            self._check_request(req)

            task = asyncio.create_task(self._invoke_provider(req))
            req.execution_task = task
            try:
                # Await through a shield so that the three cancellation sources
                # stay distinguishable without Python-version-specific task
                # introspection (requires-python >= 3.10; Task.cancelling() is
                # 3.11+): an external cancellation of this worker leaves the
                # shielded provider task RUNNING, while any provider-task-side
                # cancellation (request withdrawal, close, or a provider
                # cancelling itself) completes the task before we observe it.
                return await asyncio.shield(task)
            except _RequestCancelled:
                raise
            except _RequestTimedOut:
                raise
            except asyncio.CancelledError:
                if not task.done():
                    # The shield was interrupted: this worker task itself was
                    # cancelled from outside (shutdown-level cancellation; both
                    # in-contract paths — close() and _cancel_request — instead
                    # cancel the provider task after recording their state).
                    # The shield does not propagate cancellation into the
                    # provider task, so do it explicitly, then propagate.
                    task.cancel()
                    raise
                if req.cancel_event.is_set() or self._closed:
                    raise _RequestCancelled from None
                # A provider coroutine may cancel itself. Do not let that kill
                # the queue worker or silently retry an unknown outcome. An
                # out-of-contract direct worker cancel racing provider
                # completion on the same loop tick settles here too.
                req.failed = True
                log.error(
                    "queue_provider_cancelled",
                    request_id=req.request_id,
                    attempt=req.provider_attempts,
                )
                return _FAILURE_MESSAGE
            except Exception as exc:
                status_code = self._status_code(exc)
                is_retryable = self._retry_on_429 and self._is_retryable_status(status_code)
                if is_retryable and retry_number < self._max_retries:
                    delay = self._retry_delay_seconds(exc, retry_number)
                    if delay is not None:
                        retry_number += 1
                        log.warning(
                            "queue_retry",
                            request_id=req.request_id,
                            retry=retry_number,
                            status_code=status_code,
                            backoff=delay,
                        )
                        if await self._wait_for_cancellation(req, delay):
                            raise _RequestCancelled from None
                        continue

                req.failed = True
                log.error(
                    "queue_provider_error",
                    request_id=req.request_id,
                    attempt=req.provider_attempts,
                    status_code=status_code,
                    error_type=type(exc).__name__,
                )
                return _FAILURE_MESSAGE
            finally:
                if req.execution_task is task:
                    req.execution_task = None

    async def _invoke_provider(self, req: _Request) -> str:
        self._check_request(req)
        self._record_attempt(req)
        return await req.coro_factory()

    async def _wait_for_capacity(self, req: _Request) -> bool:
        while True:
            self._check_request(req)
            self._reset_day_if_needed()
            self._clean_minute_timestamps()
            if len(self._minute_timestamps) < self._max_rpm and self._daily_count < self._max_daily:
                return True

            if self._daily_count >= self._max_daily:
                delay = self._seconds_until_utc_day_rollover()
            else:
                oldest = self._minute_timestamps[0]
                delay = max(0.01, 60.0 - (time.monotonic() - oldest) + 0.05)

            if req.deadline is not None:
                remaining = req.deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise _RequestTimedOut
                delay = min(delay, remaining)

            self._rate_waiting_request_id = req.request_id
            try:
                withdrawn = await self._wait_for_cancellation(req, delay)
            finally:
                if self._rate_waiting_request_id == req.request_id:
                    self._rate_waiting_request_id = None
            if withdrawn:
                return False
            self._check_request(req)

    async def _wait_for_cancellation(self, req: _Request, delay: float) -> bool:
        """Wait at most ``delay`` seconds; return True if work was withdrawn."""
        self._check_request(req)
        if delay <= 0:
            return req.cancel_event.is_set() or self._closed
        try:
            await asyncio.wait_for(req.cancel_event.wait(), timeout=delay)
        except TimeoutError:
            return req.cancel_event.is_set() or self._closed
        return True

    def _check_request(self, req: _Request) -> None:
        if req.cancel_event.is_set() or self._closed:
            raise _RequestCancelled
        if req.deadline is not None and asyncio.get_running_loop().time() >= req.deadline:
            raise _RequestTimedOut

    def _record_attempt(self, req: _Request) -> None:
        """Reserve one unit of quota immediately before invoking the provider."""
        self._reset_day_if_needed()
        self._clean_minute_timestamps()
        if len(self._minute_timestamps) >= self._max_rpm or self._daily_count >= self._max_daily:
            # The single worker normally makes this unreachable; fail closed if
            # the capacity invariant changes rather than exceeding the limit.
            raise RuntimeError("provider attempt reached without available queue capacity")
        now = time.monotonic()
        self._minute_timestamps.append(now)
        self._daily_count += 1
        self._provider_attempts += 1
        req.provider_attempts += 1

    async def _settle_result(self, req: _Request, result: str) -> None:
        async with self._lock:
            if req.state is not _RequestState.RUNNING:
                return
            req.state = _RequestState.FAILED if req.failed else _RequestState.SUCCEEDED
            self._active_request = None
            self._release_pending_locked(req)
            self._logical_completed += 1
            if req.failed:
                self._logical_failed += 1
            else:
                self._logical_succeeded += 1
            if not req.future.done():
                req.future.set_result(result)

    async def _settle_timed_out(self, req: _Request) -> None:
        async with self._lock:
            if req.state not in {_RequestState.QUEUED, _RequestState.RUNNING}:
                return
            self._remove_queued_locked(req)
            req.state = _RequestState.TIMED_OUT
            req.cancel_event.set()
            self._clear_active_locked(req)
            self._release_pending_locked(req)
            self._logical_timed_out += 1
            if not req.future.done():
                req.future.set_exception(TimeoutError(_TIMEOUT_MESSAGE))
            self._ready_event.set()

    async def _settle_cancelled_if_needed(self, req: _Request) -> None:
        async with self._lock:
            if req.state not in {_RequestState.QUEUED, _RequestState.RUNNING}:
                return
            self._remove_queued_locked(req)
            req.state = _RequestState.CANCELLED
            req.cancel_event.set()
            self._clear_active_locked(req)
            self._release_pending_locked(req)
            self._logical_cancelled += 1
            if not req.future.done():
                req.future.cancel()
            self._ready_event.set()

    async def _settle_closed(self, req: _Request) -> None:
        async with self._lock:
            self._settle_closed_locked(req)
            self._ready_event.set()

    def _settle_closed_locked(self, req: _Request) -> None:
        if req.state not in {_RequestState.QUEUED, _RequestState.RUNNING}:
            return
        self._remove_queued_locked(req)
        req.state = _RequestState.CLOSED
        req.cancel_event.set()
        self._clear_active_locked(req)
        self._release_pending_locked(req)
        self._logical_closed += 1
        if not req.future.done():
            req.future.set_exception(RequestQueueClosedError(_CLOSED_MESSAGE))

    def _clear_active_locked(self, req: _Request) -> None:
        if self._active_request is req:
            self._active_request = None

    def _release_pending_locked(self, req: _Request) -> None:
        if not req.accounted:
            return
        req.accounted = False
        self._pending_count -= 1
        user_count = self._pending_per_user.get(req.user_id, 0) - 1
        if user_count <= 0:
            self._pending_per_user.pop(req.user_id, None)
        else:
            self._pending_per_user[req.user_id] = user_count

    async def _cancel_request(self, req: _Request, *, timed_out: bool) -> bool:
        async with self._lock:
            if req.state not in {_RequestState.QUEUED, _RequestState.RUNNING}:
                return False
            self._remove_queued_locked(req)
            req.state = _RequestState.TIMED_OUT if timed_out else _RequestState.CANCELLED
            req.cancel_event.set()
            self._clear_active_locked(req)
            self._release_pending_locked(req)
            if timed_out:
                self._logical_timed_out += 1
            else:
                self._logical_cancelled += 1
            if not req.future.done():
                req.future.cancel()
            if req.execution_task is not None and not req.execution_task.done():
                req.execution_task.cancel()
            self._ready_event.set()
            return True

    # ── Rate/retry helpers ──────────────────────────────────────────

    @staticmethod
    def _utc_day_key() -> tuple[int, int]:
        now = time.gmtime()
        return now.tm_year, now.tm_yday

    def _reset_day_if_needed(self) -> None:
        today = self._utc_day_key()
        if today != self._day:
            self._daily_count = 0
            self._day = today

    @staticmethod
    def _seconds_until_utc_day_rollover() -> float:
        seconds_since_midnight = time.time() % 86_400
        return max(0.05, 86_400 - seconds_since_midnight + 0.05)

    def _clean_minute_timestamps(self) -> None:
        now = time.monotonic()
        self._minute_timestamps = [t for t in self._minute_timestamps if now - t < 60.0]

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        """Read an HTTP status only from structured exception attributes."""
        response = getattr(exc, "response", None)
        candidates = (
            getattr(exc, "status_code", None),
            getattr(response, "status_code", None),
            getattr(exc, "code", None),
        )
        for candidate in candidates:
            if isinstance(candidate, bool):
                continue
            if isinstance(candidate, int):
                return candidate
            if isinstance(candidate, str) and candidate.strip().isdigit():
                return int(candidate.strip())
        return None

    @staticmethod
    def _is_retryable_status(status_code: int | None) -> bool:
        return status_code == 429 or (status_code is not None and 500 <= status_code <= 599)

    @staticmethod
    def _retry_delay_seconds(exc: Exception, retry_number: int) -> float | None:
        """Return bounded jittered delay, or decline an excessive Retry-After."""
        base = min(float(2 ** (retry_number + 1)), _MAX_RETRY_BACKOFF_SECONDS)
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            retry_after = headers.get("Retry-After")
            if retry_after is not None:
                provider_delay: float | None
                try:
                    provider_delay = float(retry_after)
                except (TypeError, ValueError):
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                    except (TypeError, ValueError, OverflowError):
                        provider_delay = None
                    else:
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        provider_delay = max(
                            0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()
                        )
                if provider_delay is not None:
                    if provider_delay > _MAX_RETRY_BACKOFF_SECONDS:
                        # Do not violate an upstream minimum wait merely to
                        # keep this local retry window short; fail this call.
                        return None
                    base = max(base, provider_delay)
        jitter = random.uniform(0.0, min(1.0, base * 0.25))
        return min(_MAX_RETRY_BACKOFF_SECONDS + 1.0, base + jitter)
