"""Smart async request queue for Gemini API — fair scheduling under rate limits.

Problem: Gemini free tier = 15 RPM shared across ALL users.
Without a queue, concurrent requests either fail silently (429) or get
rejected by the in-process rate limiter with a confusing error.

Solution: A per-process async priority queue that:
  - Serialises outbound Gemini requests so they never exceed RPM
  - Gives each user a fair share (round-robin within priority tier)
  - Supports priority levels (owner > referral_bonus > normal > low)
  - Returns a friendly "waiting in queue" position while pending
  - Auto-retries on transient 429 / 5xx responses
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)


class Priority(IntEnum):
    """Request priority — lower value = higher priority."""

    OWNER = 0
    REFERRAL_BONUS = 1
    NORMAL = 2
    LOW = 3


@dataclass(order=True)
class _Request:
    """Internal queued request."""

    sort_key: tuple[int, float] = field(compare=True)
    future: asyncio.Future[str] = field(compare=False)
    coro_factory: Callable[[], Coroutine[Any, Any, str]] = field(compare=False)
    user_id: int = field(compare=False, default=0)
    created_at: float = field(compare=False, default=0.0)


class GeminiRequestQueue:
    """Fair async request queue for Gemini API calls.

    Usage::

        queue = GeminiRequestQueue(max_rpm=15, max_daily=1500)
        result = await queue.submit(
            lambda: gemini_engine.chat(text, conv_id=cid, user_id=uid),
            user_id=uid,
            priority=Priority.NORMAL,
        )
    """

    def __init__(
        self,
        max_rpm: int = 15,
        max_daily: int = 1500,
        retry_on_429: bool = True,
        max_retries: int = 2,
    ) -> None:
        self._max_rpm = max_rpm
        self._max_daily = max_daily
        self._retry_on_429 = retry_on_429
        self._max_retries = max_retries

        self._queue: asyncio.PriorityQueue[_Request] = asyncio.PriorityQueue()
        self._minute_timestamps: list[float] = []
        self._daily_count = 0
        self._day = time.gmtime().tm_yday

        self._processing = False
        self._processor_task: asyncio.Task[None] | None = None

        # Per-user waiting count (for position info)
        self._pending_per_user: dict[int, int] = defaultdict(int)

    # ── Public API ──────────────────────────────────────────────────

    async def submit(
        self,
        coro_factory: Callable[[], Coroutine[Any, Any, str]],
        *,
        user_id: int = 0,
        priority: Priority = Priority.NORMAL,
        timeout: float = 120.0,
    ) -> str:
        """Submit a request to the queue and wait for the result.

        Args:
            coro_factory: Zero-arg async callable that performs the Gemini call.
            user_id: Telegram user ID (for fair scheduling).
            priority: Request priority.
            timeout: Max seconds to wait before raising TimeoutError.

        Returns:
            The string result from the Gemini call.

        Raises:
            TimeoutError: If the request isn't processed within *timeout*.
        """
        self._ensure_processor()
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        now = time.monotonic()
        req = _Request(
            sort_key=(priority, now),
            future=future,
            coro_factory=coro_factory,
            user_id=user_id,
            created_at=now,
        )
        self._pending_per_user[user_id] += 1
        await self._queue.put(req)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_per_user[user_id] = max(0, self._pending_per_user[user_id] - 1)
            raise TimeoutError(
                "⏳ Your request timed out in the AI queue. Please try again later."
            ) from None
        finally:
            self._pending_per_user[user_id] = max(0, self._pending_per_user[user_id] - 1)

    def queue_position(self, user_id: int) -> int:
        """Approximate queue position for a user."""
        return self._pending_per_user.get(user_id, 0)

    def get_status(self) -> dict[str, Any]:
        """Get queue status info."""
        return {
            "queue_size": self._queue.qsize(),
            "max_rpm": self._max_rpm,
            "max_daily": self._max_daily,
            "daily_used": self._daily_count,
            "rpm_remaining": max(0, self._max_rpm - len(self._minute_timestamps)),
            "daily_remaining": max(0, self._max_daily - self._daily_count),
        }

    async def close(self) -> None:
        """Stop the background processor."""
        if self._processor_task and not self._processor_task.done():
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

    # ── Internal ────────────────────────────────────────────────────

    def _ensure_processor(self) -> None:
        """Start the background processor if not running."""
        if self._processor_task is None or self._processor_task.done():
            self._processor_task = asyncio.create_task(self._process_loop())

    async def _process_loop(self) -> None:
        """Background loop that processes queued requests at safe rate."""
        while True:
            try:
                req = await self._queue.get()
                # Wait until we have rate capacity
                await self._wait_for_capacity()
                # Execute the request
                result = await self._execute(req)
                if not req.future.done():
                    req.future.set_result(result)
                self._record_request()
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("queue_processor_error", error=str(exc))
                await asyncio.sleep(1)

    async def _wait_for_capacity(self) -> None:
        """Block until we have RPM capacity."""
        while True:
            self._clean_minute_timestamps()
            if len(self._minute_timestamps) < self._max_rpm and self._daily_count < self._max_daily:
                return
            # Sleep until the oldest request in the current minute window expires
            if self._minute_timestamps:
                wait = 60.0 - (time.monotonic() - self._minute_timestamps[0]) + 0.1
                if wait > 0:
                    await asyncio.sleep(wait)
            else:
                await asyncio.sleep(1.0)

    async def _execute(self, req: _Request) -> str:
        """Execute a queued request with retry logic."""
        last_error: str = ""
        for attempt in range(self._max_retries + 1):
            try:
                result = await req.coro_factory()
                return result
            except Exception as exc:
                last_error = str(exc)
                error_str = last_error.lower()
                # Retry on 429 or 5xx
                is_retryable = "429" in error_str or "5" in error_str[:1]
                if is_retryable and attempt < self._max_retries:
                    backoff = 2 ** (attempt + 1)
                    log.warning(
                        "queue_retry",
                        attempt=attempt,
                        backoff=backoff,
                        error=last_error,
                    )
                    await asyncio.sleep(backoff)
                    continue
                break
        return f"❌ AI request failed: {last_error}"

    def _record_request(self) -> None:
        """Record that a request was made."""
        now = time.monotonic()
        self._minute_timestamps.append(now)
        self._reset_day_if_needed()
        self._daily_count += 1

    def _clean_minute_timestamps(self) -> None:
        """Remove timestamps older than 60 seconds."""
        now = time.monotonic()
        self._minute_timestamps = [t for t in self._minute_timestamps if now - t < 60]

    def _reset_day_if_needed(self) -> None:
        """Reset daily counter on a new day."""
        today = time.gmtime().tm_yday
        if today != self._day:
            self._daily_count = 0
            self._day = today
