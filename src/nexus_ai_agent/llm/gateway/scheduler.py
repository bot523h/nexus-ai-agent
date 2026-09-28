"""Bounded admission and concurrency control (W2, LAW 6 — bounded everything).

The scheduler answers one question: *may this request talk to this provider
now?* It owns the global bulkhead, the per-provider bulkhead, the per-tenant
cap, the bounded wait queue, and the load-shedding behaviour when the queue is
full. It owns nothing else — no retries, no timeouts beyond its own queue-wait
budget, no provider knowledge.

Design decisions, each with the reason it is not something else
----------------------------------------------------------------

**No task is spawned per request.** The provider call runs *inside the caller's
own task*, after ``acquire()`` returns. This is the single most important
property here: cancellation of the caller cancels the request immediately and
completely, there is no orphan task to reap, no future to settle, and no
"worker died and stranded the waiter" failure class at all. The legacy
:class:`~nexus_ai_agent.features.request_queue.GeminiRequestQueue` needs a
processor task and a careful settlement protocol precisely because it *does*
move work into its own task; the gateway avoids the whole category by not moving
work. (The one task created here is the optional external-cancel watcher, which
is always cancelled and awaited in a ``finally``.)

**Work-conserving priority scan.** On every release the waiter deque is scanned
in ``(priority, arrival)`` order and the *first satisfiable* waiter is granted,
rather than only the head. A head-only queue lets one waiter for a saturated
provider block every waiter for a healthy one (head-of-line blocking). The deque
is bounded by ``max_queued``, so the scan is O(bounded).

**Bounded in both overload modes.** ``REJECT`` sheds immediately with a typed
``OVERLOADED`` error — real backpressure, per AWS's load-shedding guidance
(https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/).
``WAIT`` blocks for a *queue slot* (not for capacity) up to the queue-wait
budget and then also raises ``OVERLOADED``. Neither mode can grow memory without
bound, which is the failure mode an unbounded ``asyncio.Queue`` produces under a
slow provider.

**Counters are released exactly once.** :class:`Admission` is a token with a
``released`` flag; ``release()`` is idempotent, so an engine bug that releases
twice in a ``finally`` cannot inflate capacity — and a missing release cannot
silently leak either, because ``snapshot()`` exposes the live counts and
``tests/unit/test_llm_gateway_scheduler.py`` asserts they return to zero after
adversarial cancellation storms.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Protocol

from nexus_ai_agent.llm.errors import CancelledByCallerError, GatewayClosedError, OverloadedError
from nexus_ai_agent.llm.gateway.contract import MONOTONIC, Clock, LLMPriority
from nexus_ai_agent.llm.gateway.policy import ConcurrencyPolicy, OverloadBehavior

__all__ = [
    "Admission",
    "BoundedScheduler",
    "SchedulerPort",
]


@dataclass
class Admission:
    """A held capacity slot. Must be released exactly once (release is idempotent)."""

    provider: str
    tenant_id: int
    priority: LLMPriority
    queued_seconds: float = 0.0
    released: bool = field(default=False, repr=False)
    sequence: int = 0


class SchedulerPort(Protocol):
    """The seam the engine depends on. One method to enter, one to leave."""

    async def acquire(
        self,
        *,
        provider: str,
        tenant_id: int,
        priority: LLMPriority,
        queue_budget_seconds: float,
        cancellation: Any | None = None,
    ) -> Admission: ...

    def release(self, admission: Admission) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


@dataclass
class _Waiter:
    sequence: int
    priority: LLMPriority
    provider: str
    tenant_id: int
    future: asyncio.Future[None]
    granted: bool = False
    enqueued_at: float = 0.0


class BoundedScheduler:
    """The default :class:`SchedulerPort`: bulkheads + bounded queue + shedding."""

    def __init__(
        self,
        policy: ConcurrencyPolicy,
        *,
        clock: Clock = MONOTONIC,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._global_inflight = 0
        self._provider_inflight: dict[str, int] = {}
        self._tenant_inflight: dict[int, int] = {}
        self._waiters: deque[_Waiter] = deque()
        self._sequence = count(1)
        self._closed = False
        self._slot_available = asyncio.Event()
        self._slot_available.set()
        # Counters: bounded-cardinality, monotonic, and the only way an operator
        # can see shedding happen.
        self._granted = 0
        self._rejected = 0
        self._timed_out = 0
        self._cancelled = 0
        self._total_queued_seconds = 0.0

    # ── public API ───────────────────────────────────────────────────
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def policy(self) -> ConcurrencyPolicy:
        return self._policy

    async def acquire(
        self,
        *,
        provider: str,
        tenant_id: int = 0,
        priority: LLMPriority = LLMPriority.NORMAL,
        queue_budget_seconds: float | None = None,
        cancellation: Any | None = None,
    ) -> Admission:
        """Take a capacity slot, waiting at most ``queue_budget_seconds``.

        Raises:
            GatewayClosedError: the authority is shutting down.
            OverloadedError: the bounded queue is full (REJECT), or no queue slot
                appeared inside the budget (WAIT), or the budget expired while
                waiting for capacity.
            asyncio.CancelledError: the caller's task was cancelled; any grant
                won in the meantime is released before the exception escapes, so
                cancellation can never leak a slot.
        """

        if self._closed:
            raise GatewayClosedError("LLM gateway scheduler is closed")

        started = self._clock()
        # Fast path: capacity now and nobody waiting ahead (no queue jumping).
        if not self._waiters and self._can_admit(provider, tenant_id):
            self._take(provider, tenant_id)
            self._granted += 1
            return Admission(
                provider=provider,
                tenant_id=tenant_id,
                priority=priority,
                queued_seconds=0.0,
                sequence=next(self._sequence),
            )

        remaining = queue_budget_seconds
        if remaining is not None and remaining <= 0:
            # No budget to wait: this is a shed, not a failure of the provider.
            self._rejected += 1
            raise OverloadedError(
                "LLM gateway is saturated and the queue-wait budget is already exhausted",
                provider=provider,
                detail=(
                    f"inflight={self._global_inflight}/{self._policy.max_inflight_global} "
                    f"queued={len(self._waiters)}/{self._policy.max_queued}"
                ),
            )

        try:
            return await self._wait_for_admission(
                provider=provider,
                tenant_id=tenant_id,
                priority=priority,
                budget=remaining,
                cancellation=cancellation,
                started=started,
            )
        except _CancelledByToken:
            # The caller withdrew the request without cancelling its own task.
            # That is a *typed* cancellation, not an ``asyncio.CancelledError``:
            # the caller's task is still alive and must be able to catch this,
            # report it, and carry on. The internal marker never escapes.
            self._cancelled += 1
            raise CancelledByCallerError(
                "LLM request withdrawn by caller while waiting for capacity",
                provider=provider,
                detail=f"queued={len(self._waiters)}/{self._policy.max_queued}",
            ) from None
        except asyncio.CancelledError:
            # The caller's *task* went away while queued. Count it here too: an
            # operator reading ``cancelled`` must see every abandonment, whether
            # it was signalled through a token or through the event loop. The
            # slot bookkeeping already happened in ``_wait_for_admission``.
            self._cancelled += 1
            raise

    async def _wait_for_admission(
        self,
        *,
        provider: str,
        tenant_id: int,
        priority: LLMPriority,
        budget: float | None,
        cancellation: Any | None,
        started: float,
    ) -> Admission:
        """Block until a slot is granted, the budget expires, or the caller withdraws."""

        remaining = budget
        # WAIT mode: block for a *queue slot* first, so the deque stays bounded.
        if len(self._waiters) >= self._policy.max_queued:
            if self._policy.overload_behavior is OverloadBehavior.REJECT:
                self._rejected += 1
                raise OverloadedError(
                    "LLM gateway queue is full; request shed to protect the process",
                    provider=provider,
                    detail=(
                        f"queued={len(self._waiters)}/{self._policy.max_queued} "
                        f"inflight={self._global_inflight}/{self._policy.max_inflight_global}"
                    ),
                )
            # One freed slot wakes *every* waiter blocked on the event, and only
            # one of them can take it. Giving up after a single wake would shed
            # load that the remaining budget could still have served, so this
            # keeps waiting until it holds a slot or the budget really is gone
            # (``_await_queue_slot`` raises the typed error in that case).
            while len(self._waiters) >= self._policy.max_queued:
                await self._await_queue_slot(remaining, cancellation)
                if self._closed:
                    raise GatewayClosedError("LLM gateway scheduler is closed")
                remaining = self._remaining(remaining, started)
                if remaining is not None and remaining <= 0:
                    self._timed_out += 1
                    raise OverloadedError(
                        "LLM gateway queue stayed full for the whole queue-wait budget",
                        provider=provider,
                        detail=f"queued={len(self._waiters)}/{self._policy.max_queued}",
                    )

        loop = asyncio.get_running_loop()
        waiter = _Waiter(
            sequence=next(self._sequence),
            priority=priority,
            provider=provider,
            tenant_id=tenant_id,
            future=loop.create_future(),
            enqueued_at=started,
        )
        self._waiters.append(waiter)
        self._slot_available.clear()
        # Capacity can free *while* this caller is waiting for a queue slot, and
        # ``_pump`` only runs on release: the release that freed the slot found an
        # empty deque (this waiter was not in it yet) and granted nobody. Without
        # this pump the caller would sit in a queue next to idle capacity until
        # its budget expired — a stranded caller and a fleet that looks
        # saturated while doing nothing (LAW 6 is a bound, not a stall).
        # ``_pump`` grants in (priority, arrival) order, so joining the queue
        # this way cannot jump ahead of anybody already waiting in it.
        self._pump()
        try:
            await self._await_grant(waiter, remaining, cancellation)
        except BaseException:
            self._withdraw(waiter)
            raise
        if not waiter.granted:
            # Defensive: _await_grant either grants or raises.
            self._withdraw(waiter)  # pragma: no cover
            raise OverloadedError("LLM gateway admission ended without a grant", provider=provider)
        queued_for = max(0.0, self._clock() - started)
        self._total_queued_seconds += queued_for
        self._granted += 1
        return Admission(
            provider=provider,
            tenant_id=tenant_id,
            priority=priority,
            queued_seconds=queued_for,
            sequence=waiter.sequence,
        )

    def release(self, admission: Admission | None) -> None:
        """Give a slot back and wake the next satisfiable waiter. Idempotent."""

        if admission is None or admission.released:
            return
        admission.released = True
        provider = admission.provider
        if self._global_inflight > 0:
            self._global_inflight -= 1
        current = self._provider_inflight.get(provider, 0)
        if current > 1:
            self._provider_inflight[provider] = current - 1
        else:
            self._provider_inflight.pop(provider, None)
        tenant_current = self._tenant_inflight.get(admission.tenant_id, 0)
        if tenant_current > 1:
            self._tenant_inflight[admission.tenant_id] = tenant_current - 1
        else:
            self._tenant_inflight.pop(admission.tenant_id, None)
        self._pump()

    async def aclose(self) -> None:
        """Stop admitting and fail every waiter with a typed closed error.

        In-flight work is *not* cancelled here: the engine owns the caller-facing
        cancellation contract, and a scheduler that cancelled other people's
        tasks would be a second authority. Idempotent.
        """

        if self._closed:
            return
        self._closed = True
        waiters = list(self._waiters)
        self._waiters.clear()
        self._slot_available.set()
        for waiter in waiters:
            if not waiter.future.done():
                waiter.future.set_exception(
                    GatewayClosedError("LLM gateway scheduler closed while queued")
                )

    def snapshot(self) -> dict[str, Any]:
        return {
            "closed": self._closed,
            "inflight_global": self._global_inflight,
            "max_inflight_global": self._policy.max_inflight_global,
            "inflight_per_provider": dict(self._provider_inflight),
            "inflight_per_tenant": dict(self._tenant_inflight),
            "queued": len(self._waiters),
            "max_queued": self._policy.max_queued,
            "overload_behavior": self._policy.overload_behavior.value,
            "granted": self._granted,
            "rejected": self._rejected,
            "timed_out": self._timed_out,
            "cancelled": self._cancelled,
            "total_queued_seconds": round(self._total_queued_seconds, 4),
            "avg_queued_seconds": (
                round(self._total_queued_seconds / self._granted, 4) if self._granted else 0.0
            ),
        }

    # ── internals ────────────────────────────────────────────────────
    def _can_admit(self, provider: str, tenant_id: int) -> bool:
        if self._global_inflight >= self._policy.max_inflight_global:
            return False
        if self._provider_inflight.get(provider, 0) >= self._policy.max_inflight_per_provider:
            return False
        return self._tenant_inflight.get(tenant_id, 0) < self._policy.max_inflight_per_tenant

    def _take(self, provider: str, tenant_id: int) -> None:
        self._global_inflight += 1
        self._provider_inflight[provider] = self._provider_inflight.get(provider, 0) + 1
        self._tenant_inflight[tenant_id] = self._tenant_inflight.get(tenant_id, 0) + 1

    def _pump(self) -> None:
        """Grant slots to waiters in (priority, arrival) order, skipping blocked ones."""

        if self._closed:
            self._slot_available.set()
            return
        # Iterate over a snapshot: granting removes entries from the deque.
        for waiter in sorted(self._waiters, key=lambda w: (int(w.priority), w.sequence)):
            if self._global_inflight >= self._policy.max_inflight_global:
                break
            if not self._can_admit(waiter.provider, waiter.tenant_id):
                continue  # work-conserving: try the next satisfiable waiter
            self._remove_waiter(waiter)
            self._take(waiter.provider, waiter.tenant_id)
            waiter.granted = True
            if not waiter.future.done():
                waiter.future.set_result(None)
        if len(self._waiters) < self._policy.max_queued:
            self._slot_available.set()

    def _remove_waiter(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            pass
        if len(self._waiters) < self._policy.max_queued:
            self._slot_available.set()

    def _withdraw(self, waiter: _Waiter) -> None:
        """Remove a waiter and give back a grant it may have already won."""

        self._remove_waiter(waiter)
        if waiter.granted:
            # The pump granted a slot in the same tick the waiter gave up. Hand
            # it straight back so the slot cannot leak, then wake the next.
            waiter.granted = False
            self.release(
                Admission(
                    provider=waiter.provider, tenant_id=waiter.tenant_id, priority=waiter.priority
                )
            )
        if not waiter.future.done():
            waiter.future.cancel()

    def _remaining(self, budget: float | None, started: float) -> float | None:
        if budget is None:
            return None
        return max(0.0, budget - (self._clock() - started))

    async def _await_queue_slot(self, budget: float | None, cancellation: Any | None) -> None:
        """WAIT mode: block until the deque has room, the budget ends, or cancel."""

        watch: asyncio.Future[Any] = asyncio.ensure_future(self._slot_available.wait())
        pending: set[asyncio.Future[Any]] = {watch}
        cancel_watch = _cancel_watcher(cancellation)
        if cancel_watch is not None:
            pending.add(cancel_watch)
        try:
            done, _ = await asyncio.wait(
                pending,
                timeout=budget,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_watch is not None and cancel_watch in done:
                raise _CancelledByToken()
            if not done:
                self._timed_out += 1
                raise OverloadedError(
                    "LLM gateway queue stayed full for the whole queue-wait budget",
                    detail=f"queued={len(self._waiters)}/{self._policy.max_queued}",
                )
        finally:
            watch.cancel()
            cleanup: list[asyncio.Future[Any]] = [watch]
            if cancel_watch is not None:
                cancel_watch.cancel()
                cleanup.append(cancel_watch)
            # Every future this method created is cancelled AND awaited: a
            # pending task left behind is an orphan (LAW 5).
            await asyncio.gather(*cleanup, return_exceptions=True)

    async def _await_grant(
        self, waiter: _Waiter, budget: float | None, cancellation: Any | None
    ) -> None:
        """Wait for this waiter's grant, honouring budget and external cancel."""

        pending: set[asyncio.Future[Any]] = {waiter.future}
        cancel_watch = _cancel_watcher(cancellation)
        if cancel_watch is not None:
            pending.add(cancel_watch)
        try:
            done, _ = await asyncio.wait(
                pending,
                timeout=budget,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # The cancel watcher is ours; it must never outlive the wait, or
            # every cancelled request leaves a task behind (LAW 5 / no orphans).
            if cancel_watch is not None:
                cancel_watch.cancel()
                await asyncio.gather(cancel_watch, return_exceptions=True)

        if waiter.future in done:
            exc = waiter.future.exception()
            if exc is not None:
                waiter.granted = False
                raise exc
            if not waiter.granted:  # pragma: no cover — defensive
                raise OverloadedError("LLM gateway grant lost", provider=waiter.provider)
            return
        if cancel_watch is not None and cancel_watch in done:
            raise _CancelledByToken()
        self._timed_out += 1
        raise OverloadedError(
            "LLM gateway queue-wait budget expired before a capacity slot freed",
            provider=waiter.provider,
            detail=(
                f"inflight={self._global_inflight}/{self._policy.max_inflight_global} "
                f"queued={len(self._waiters)}/{self._policy.max_queued}"
            ),
        )


class _CancelledByToken(Exception):
    """Internal: the caller's external cancellation event fired while queued."""


def _cancel_watcher(cancellation: Any | None) -> asyncio.Future[Any] | None:
    """Wrap an external cancel signal as an awaitable, or ``None`` when absent.

    Accepts anything with an awaitable ``wait()`` (``asyncio.Event``), an already
    awaitable object, or ``None``. A non-awaitable truthy value is a caller bug:
    it is ignored deliberately rather than guessed at, and the gateway logs the
    mismatch through its internal-error channel.
    """

    if cancellation is None:
        return None
    wait = getattr(cancellation, "wait", None)
    if callable(wait):
        result = wait()
        if asyncio.isfuture(result):
            return asyncio.ensure_future(result)
        if isinstance(result, Awaitable):
            return asyncio.ensure_future(_await(result))
        return None
    if isinstance(cancellation, Awaitable):
        return asyncio.ensure_future(_await(cancellation))
    return None


async def _await(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


#: Convenience alias for callers that build a scheduler from a plain callable.
SchedulerFactory = Callable[[ConcurrencyPolicy], SchedulerPort]
