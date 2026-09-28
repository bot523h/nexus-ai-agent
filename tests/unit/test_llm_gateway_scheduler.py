"""W2 — the bounded scheduler: bulkheads, priority, shedding, cancellation.

LAW 6 ("bounded everything") and LAW 5 ("cancellation is sacred") are the two
laws this module can violate silently, so every test here ends by asserting the
scheduler's own accounting returned to zero. A leaked slot is not a theoretical
problem: it permanently shrinks the fleet's capacity, one request at a time,
until the gateway refuses everything.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus_ai_agent.llm.errors import GatewayClosedError, LLMErrorKind, OverloadedError
from nexus_ai_agent.llm.gateway.contract import LLMPriority
from nexus_ai_agent.llm.gateway.policy import ConcurrencyPolicy, OverloadBehavior
from nexus_ai_agent.llm.gateway.scheduler import Admission, BoundedScheduler


def _policy(**overrides: object) -> ConcurrencyPolicy:
    """A valid policy for one test.

    Inner bounds are clamped to the global one, because ``ConcurrencyPolicy``
    (correctly) refuses a per-provider bound looser than the global bound — a
    test helper that produced an invalid policy would fail for the wrong reason.
    """

    base: dict[str, object] = {
        "max_inflight_global": 2,
        "max_inflight_per_provider": 2,
        "max_inflight_per_tenant": 2,
        "max_queued": 2,
        "overload_behavior": OverloadBehavior.REJECT,
    }
    base.update(overrides)
    ceiling = int(base["max_inflight_global"])  # type: ignore[arg-type]
    base["max_inflight_per_provider"] = min(
        int(base["max_inflight_per_provider"]),  # type: ignore[arg-type]
        ceiling,
    )
    base["max_inflight_per_tenant"] = min(
        int(base["max_inflight_per_tenant"]),  # type: ignore[arg-type]
        ceiling,
    )
    return ConcurrencyPolicy(**base)  # type: ignore[arg-type]


async def _acquire(
    scheduler: BoundedScheduler,
    *,
    provider: str = "gemini",
    tenant_id: int = 0,
    priority: LLMPriority = LLMPriority.NORMAL,
    budget: float = 0.05,
    cancellation: asyncio.Event | None = None,
) -> Admission:
    return await scheduler.acquire(
        provider=provider,
        tenant_id=tenant_id,
        priority=priority,
        queue_budget_seconds=budget,
        cancellation=cancellation,
    )


def _is_idle(scheduler: BoundedScheduler) -> bool:
    snapshot = scheduler.snapshot()
    return (
        snapshot["inflight_global"] == 0
        and snapshot["queued"] == 0
        and not snapshot["inflight_per_provider"]
        and not snapshot["inflight_per_tenant"]
    )


# ═══════════════════════════════════════════════════════════════════════════
# Bulkheads
# ═══════════════════════════════════════════════════════════════════════════


async def test_capacity_is_granted_immediately_when_the_fleet_is_idle() -> None:
    scheduler = BoundedScheduler(_policy())
    admission = await _acquire(scheduler)
    assert admission.provider == "gemini"
    assert admission.released is False
    assert scheduler.snapshot()["inflight_global"] == 1
    scheduler.release(admission)
    assert _is_idle(scheduler)


async def test_the_global_bulkhead_caps_total_inflight() -> None:
    scheduler = BoundedScheduler(_policy(max_inflight_global=2, max_queued=0))
    first = await _acquire(scheduler)
    second = await _acquire(scheduler)
    assert scheduler.snapshot()["inflight_global"] == 2
    with pytest.raises(OverloadedError) as excinfo:
        await _acquire(scheduler)
    assert excinfo.value.kind is LLMErrorKind.OVERLOADED
    scheduler.release(first)
    scheduler.release(second)
    assert _is_idle(scheduler)


async def test_the_per_provider_bulkhead_isolates_one_sick_provider() -> None:
    """A bulkhead: one provider hanging must not consume the whole fleet."""

    scheduler = BoundedScheduler(
        _policy(max_inflight_global=4, max_inflight_per_provider=1, max_queued=0)
    )
    sick = await _acquire(scheduler, provider="gemini")
    with pytest.raises(OverloadedError):
        await _acquire(scheduler, provider="gemini")
    # The other provider is unaffected — that is the entire point.
    healthy = await _acquire(scheduler, provider="ollama")
    assert scheduler.snapshot()["inflight_per_provider"] == {"gemini": 1, "ollama": 1}
    scheduler.release(sick)
    scheduler.release(healthy)
    assert _is_idle(scheduler)


async def test_the_per_tenant_bulkhead_stops_one_tenant_starving_the_others() -> None:
    scheduler = BoundedScheduler(
        _policy(
            max_inflight_global=4,
            max_inflight_per_provider=4,
            max_inflight_per_tenant=1,
            max_queued=0,
        )
    )
    greedy = await _acquire(scheduler, tenant_id=1)
    with pytest.raises(OverloadedError):
        await _acquire(scheduler, tenant_id=1)
    other = await _acquire(scheduler, tenant_id=2)
    assert scheduler.snapshot()["inflight_per_tenant"] == {1: 1, 2: 1}
    scheduler.release(greedy)
    scheduler.release(other)
    assert _is_idle(scheduler)


async def test_policy_rejects_a_per_provider_bound_looser_than_the_global_one() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        ConcurrencyPolicy(max_inflight_global=1, max_inflight_per_provider=4)


async def test_policy_rejects_zero_or_negative_bounds() -> None:
    with pytest.raises(ValueError):
        ConcurrencyPolicy(max_inflight_global=0)
    with pytest.raises(ValueError):
        ConcurrencyPolicy(max_queued=-1)


# ═══════════════════════════════════════════════════════════════════════════
# Shedding: REJECT vs WAIT
# ═══════════════════════════════════════════════════════════════════════════


async def test_reject_mode_sheds_immediately_instead_of_growing_the_queue() -> None:
    scheduler = BoundedScheduler(_policy(max_inflight_global=1, max_queued=1))
    held = await _acquire(scheduler)
    queued = asyncio.create_task(_acquire(scheduler, budget=5.0))
    await asyncio.sleep(0)  # let the waiter enqueue
    assert scheduler.snapshot()["queued"] == 1
    # The queue is at its bound: the next caller is refused, not enqueued.
    with pytest.raises(OverloadedError):
        await _acquire(scheduler, budget=5.0)
    assert scheduler.snapshot()["queued"] == 1
    scheduler.release(held)
    admission = await asyncio.wait_for(queued, timeout=1.0)
    scheduler.release(admission)
    assert _is_idle(scheduler)


async def test_wait_mode_blocks_until_capacity_appears() -> None:
    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=4, overload_behavior=OverloadBehavior.WAIT)
    )
    held = await _acquire(scheduler)
    waiter = asyncio.create_task(_acquire(scheduler, budget=5.0))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    scheduler.release(held)
    admission = await asyncio.wait_for(waiter, timeout=1.0)
    assert admission.provider == "gemini"
    scheduler.release(admission)
    assert _is_idle(scheduler)


async def test_wait_mode_gives_up_with_a_typed_overload_when_the_budget_expires() -> None:
    """The caller's queue budget is a real deadline, not a suggestion."""

    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=4, overload_behavior=OverloadBehavior.WAIT)
    )
    held = await _acquire(scheduler)
    with pytest.raises(OverloadedError) as excinfo:
        await _acquire(scheduler, budget=0.05)
    assert excinfo.value.kind is LLMErrorKind.OVERLOADED
    snapshot = scheduler.snapshot()
    assert snapshot["timed_out"] == 1
    assert snapshot["queued"] == 0
    scheduler.release(held)
    assert _is_idle(scheduler)


async def test_rejected_callers_are_counted_so_shedding_is_visible() -> None:
    scheduler = BoundedScheduler(_policy(max_inflight_global=1, max_queued=0))
    held = await _acquire(scheduler)
    for _ in range(3):
        with pytest.raises(OverloadedError):
            await _acquire(scheduler)
    snapshot = scheduler.snapshot()
    assert snapshot["rejected"] == 3
    assert snapshot["granted"] == 1
    scheduler.release(held)
    assert _is_idle(scheduler)


async def test_average_queue_wait_is_reported() -> None:
    scheduler = BoundedScheduler(_policy(max_inflight_global=1, max_queued=1))
    held = await _acquire(scheduler)
    waiter = asyncio.create_task(_acquire(scheduler, budget=5.0))
    await asyncio.sleep(0.02)
    scheduler.release(held)
    admission = await asyncio.wait_for(waiter, timeout=1.0)
    assert admission.queued_seconds > 0.0
    assert scheduler.snapshot()["avg_queued_seconds"] > 0.0
    scheduler.release(admission)
    assert _is_idle(scheduler)


# ═══════════════════════════════════════════════════════════════════════════
# Priority
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_higher_priority_waiter_is_served_before_an_earlier_normal_one() -> None:
    scheduler = BoundedScheduler(
        _policy(
            max_inflight_global=1,
            max_queued=4,
            overload_behavior=OverloadBehavior.WAIT,
        )
    )
    held = await _acquire(scheduler)
    normal = asyncio.create_task(_acquire(scheduler, tenant_id=1, budget=5.0))
    await asyncio.sleep(0.01)
    owner = asyncio.create_task(
        _acquire(scheduler, tenant_id=2, priority=LLMPriority.OWNER, budget=5.0)
    )
    await asyncio.sleep(0.01)
    assert scheduler.snapshot()["queued"] == 2

    scheduler.release(held)
    first = await asyncio.wait_for(owner, timeout=1.0)
    assert first.priority is LLMPriority.OWNER
    scheduler.release(first)
    second = await asyncio.wait_for(normal, timeout=1.0)
    scheduler.release(second)
    assert _is_idle(scheduler)


async def test_equal_priority_is_served_in_arrival_order() -> None:
    """FIFO within a tier: no waiter may be starved by a later arrival."""

    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=4, overload_behavior=OverloadBehavior.WAIT)
    )
    held = await _acquire(scheduler)
    order: list[int] = []

    async def waiter(tenant_id: int) -> None:
        admission = await _acquire(scheduler, tenant_id=tenant_id, budget=5.0)
        order.append(tenant_id)
        scheduler.release(admission)

    tasks = [asyncio.create_task(waiter(index)) for index in range(3)]
    await asyncio.sleep(0.02)
    scheduler.release(held)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
    assert order == [0, 1, 2]
    assert _is_idle(scheduler)


async def test_priority_is_work_conserving_no_capacity_is_left_idle() -> None:
    """A priority queue that idles while work waits is a bug, not a policy."""

    scheduler = BoundedScheduler(
        _policy(
            max_inflight_global=3,
            max_inflight_per_provider=3,
            max_inflight_per_tenant=3,
            max_queued=8,
            overload_behavior=OverloadBehavior.WAIT,
        )
    )
    held = [await _acquire(scheduler) for _ in range(3)]
    tasks = [
        asyncio.create_task(_acquire(scheduler, tenant_id=index, budget=5.0)) for index in range(3)
    ]
    await asyncio.sleep(0.01)
    for admission in held:
        scheduler.release(admission)
    granted = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
    assert len(granted) == 3
    assert scheduler.snapshot()["inflight_global"] == 3
    for admission in granted:
        scheduler.release(admission)
    assert _is_idle(scheduler)


# ═══════════════════════════════════════════════════════════════════════════
# Cancellation (LAW 5)
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_task_cancelled_while_queued_is_counted_as_a_cancellation() -> None:
    """``cancelled`` must count every abandonment, token-signalled or not.

    An operator reading the scheduler snapshot uses this number to tell "the
    fleet is being asked to do less" from "the fleet is shedding"; counting only
    one of the two cancellation mechanisms makes the metric lie by omission
    (LAW 10). A task cancellation is also the mechanism the engine itself uses
    when a deadline expires, so under-counting it hides timeout storms too.
    """

    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=4, overload_behavior=OverloadBehavior.WAIT)
    )
    held = await _acquire(scheduler)
    waiter = asyncio.create_task(_acquire(scheduler, budget=5.0))
    await asyncio.sleep(0.01)
    assert scheduler.snapshot()["queued"] == 1
    assert scheduler.snapshot()["cancelled"] == 0
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    snapshot = scheduler.snapshot()
    assert snapshot["cancelled"] == 1
    assert snapshot["queued"] == 0
    scheduler.release(held)
    assert _is_idle(scheduler)


async def test_cancelling_a_queued_caller_removes_it_from_the_queue() -> None:
    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=4, overload_behavior=OverloadBehavior.WAIT)
    )
    held = await _acquire(scheduler)
    waiter = asyncio.create_task(_acquire(scheduler, budget=5.0))
    await asyncio.sleep(0.01)
    assert scheduler.snapshot()["queued"] == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert scheduler.snapshot()["queued"] == 0
    scheduler.release(held)
    assert _is_idle(scheduler)


async def test_a_thundering_herd_wakeup_never_overfills_the_bounded_queue() -> None:
    """One freed slot wakes everybody; the bound must still hold (LAW 6).

    ``max_queued`` is a promise about memory and latency, so the queue-slot wait
    is re-checked in a loop: a caller that wakes up to a queue that filled again
    must go back to waiting. Checking once would let N woken callers append
    themselves to a queue of size 1, and the "bounded" queue would grow with
    every burst — the exact failure mode a bound exists to prevent.
    """

    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=1, overload_behavior=OverloadBehavior.WAIT)
    )
    holder = await _acquire(scheduler)
    queued = asyncio.create_task(_acquire(scheduler, budget=5.0))
    await asyncio.sleep(0.01)
    assert scheduler.snapshot()["queued"] == 1  # the one permitted queue slot is taken

    herd = [asyncio.create_task(_acquire(scheduler, budget=5.0)) for _ in range(2)]
    await asyncio.sleep(0.01)
    assert scheduler.snapshot()["queued"] == 1  # the herd is waiting for a slot, not in it

    scheduler.release(holder)  # frees capacity AND wakes every blocked caller at once
    await asyncio.sleep(0.05)
    assert scheduler.snapshot()["queued"] <= 1  # exactly one of them may enter

    async def take_and_release(task: asyncio.Task[Admission]) -> None:
        admission = await asyncio.wait_for(task, timeout=2.0)
        scheduler.release(admission)

    first = await asyncio.wait_for(queued, timeout=2.0)
    scheduler.release(first)
    await asyncio.gather(*(take_and_release(task) for task in herd))
    assert _is_idle(scheduler)


async def test_a_waiter_joining_an_empty_queue_is_not_stranded_beside_idle_capacity() -> None:
    """Joining the queue must itself try for a grant.

    ``_pump`` runs on release. A caller that is still waiting for a *queue slot*
    is not in the deque when that release happens, so the release grants nobody
    — and the caller then joins a queue next to idle capacity, where nothing
    will ever wake it again. It sits until its budget expires while the fleet
    does nothing, which reads as saturation in the snapshot and feels like an
    outage to the caller (LAW 6 is a bound, not a stall).
    """

    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=1, overload_behavior=OverloadBehavior.WAIT)
    )
    holder = await _acquire(scheduler)
    queued = asyncio.create_task(_acquire(scheduler, budget=5.0))
    await asyncio.sleep(0.01)
    assert scheduler.snapshot()["queued"] == 1
    blocked = asyncio.create_task(_acquire(scheduler, budget=5.0))  # wants the queue slot
    await asyncio.sleep(0.01)

    scheduler.release(holder)  # grants `queued`, wakes `blocked`
    first = await asyncio.wait_for(queued, timeout=1.0)
    scheduler.release(first)  # capacity is free and the deque is empty again
    second = await asyncio.wait_for(blocked, timeout=1.0)  # ... so `blocked` must not strand
    assert scheduler.snapshot()["inflight_global"] == 1
    scheduler.release(second)
    assert _is_idle(scheduler)


async def test_an_external_cancellation_token_withdraws_a_queued_request() -> None:
    """A caller can withdraw without cancelling its own task.

    The withdrawal surfaces as a typed ``CANCELLED`` error, *not* as
    ``asyncio.CancelledError``: the caller's task is still alive and must be able
    to catch the withdrawal, report it, and keep running. Raising
    ``CancelledError`` here would tell the event loop the task itself was
    cancelled, which is a different (and false) statement.
    """

    from nexus_ai_agent.llm.errors import CancelledByCallerError

    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=4, overload_behavior=OverloadBehavior.WAIT)
    )
    held = await _acquire(scheduler)
    token = asyncio.Event()
    waiter = asyncio.create_task(_acquire(scheduler, budget=5.0, cancellation=token))
    await asyncio.sleep(0.01)
    token.set()
    with pytest.raises(CancelledByCallerError) as excinfo:
        await asyncio.wait_for(waiter, timeout=1.0)
    assert excinfo.value.kind is LLMErrorKind.CANCELLED
    assert excinfo.value.retryable is False
    assert excinfo.value.fallback_eligible is False
    assert scheduler.snapshot()["queued"] == 0
    assert scheduler.snapshot()["cancelled"] == 1
    scheduler.release(held)
    assert _is_idle(scheduler)


async def test_a_grant_that_lands_during_cancellation_is_released_not_leaked() -> None:
    """The race that leaks capacity: slot granted and task cancelled together."""

    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=4, overload_behavior=OverloadBehavior.WAIT)
    )
    held = await _acquire(scheduler)
    waiter = asyncio.create_task(_acquire(scheduler, budget=5.0))
    await asyncio.sleep(0.01)
    # Release and cancel in the same tick: the pump may grant before the
    # cancellation is observed.
    scheduler.release(held)
    waiter.cancel()
    try:
        await waiter
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.01)
    assert _is_idle(scheduler), f"leaked slot: {scheduler.snapshot()}"


async def test_release_is_idempotent_so_a_finally_block_cannot_double_free() -> None:
    scheduler = BoundedScheduler(_policy(max_inflight_global=2))
    admission = await _acquire(scheduler)
    scheduler.release(admission)
    scheduler.release(admission)
    scheduler.release(admission)
    assert scheduler.snapshot()["inflight_global"] == 0
    assert admission.released is True


async def test_releasing_none_is_safe() -> None:
    scheduler = BoundedScheduler(_policy())
    scheduler.release(None)
    assert _is_idle(scheduler)


async def test_a_cancellation_storm_leaves_the_accounting_at_zero() -> None:
    """Adversarial: 60 callers, most cancelled mid-queue, none may leak."""

    scheduler = BoundedScheduler(
        _policy(
            max_inflight_global=2,
            max_inflight_per_provider=2,
            max_inflight_per_tenant=2,
            max_queued=8,
            overload_behavior=OverloadBehavior.WAIT,
        )
    )
    held = [await _acquire(scheduler) for _ in range(2)]
    tasks = [
        asyncio.create_task(_acquire(scheduler, tenant_id=index % 3, budget=5.0))
        for index in range(60)
    ]
    await asyncio.sleep(0.02)
    for task in tasks[::2]:
        task.cancel()
    for admission in held:
        scheduler.release(admission)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Admission):
            scheduler.release(result)
    await asyncio.sleep(0.02)
    assert _is_idle(scheduler), f"leaked after storm: {scheduler.snapshot()}"
    assert any(isinstance(result, Admission) for result in results)
    assert any(isinstance(result, (OverloadedError, asyncio.CancelledError)) for result in results)


# ═══════════════════════════════════════════════════════════════════════════
# Shutdown
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_closed_scheduler_refuses_new_work_with_a_typed_error() -> None:
    scheduler = BoundedScheduler(_policy())
    await scheduler.aclose()
    assert scheduler.closed is True
    with pytest.raises(GatewayClosedError) as excinfo:
        await _acquire(scheduler)
    assert excinfo.value.kind is LLMErrorKind.GATEWAY_CLOSED


async def test_closing_releases_every_waiter_instead_of_hanging_them() -> None:
    scheduler = BoundedScheduler(
        _policy(max_inflight_global=1, max_queued=4, overload_behavior=OverloadBehavior.WAIT)
    )
    held = await _acquire(scheduler)
    waiters = [asyncio.create_task(_acquire(scheduler, budget=30.0)) for _ in range(3)]
    await asyncio.sleep(0.01)
    await scheduler.aclose()
    results = await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), timeout=2.0)
    assert all(isinstance(result, GatewayClosedError) for result in results)
    assert scheduler.snapshot()["queued"] == 0
    scheduler.release(held)


async def test_snapshot_shape_is_bounded_and_json_safe() -> None:
    scheduler = BoundedScheduler(_policy())
    admission = await _acquire(scheduler, provider="gemini", tenant_id=4)
    snapshot = scheduler.snapshot()
    assert snapshot["max_inflight_global"] == 2
    assert snapshot["max_queued"] == 2
    assert snapshot["overload_behavior"] == "reject"
    assert snapshot["inflight_per_provider"] == {"gemini": 1}
    assert snapshot["inflight_per_tenant"] == {4: 1}
    scheduler.release(admission)
    import json

    json.dumps(scheduler.snapshot())  # must not raise
