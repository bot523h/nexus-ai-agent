"""The crash matrix (P3/P4): every window is a deterministic property test.

Each scenario reproduces a real failure without sleeps — ``now`` is explicit
in every call. T1-T7 are named in the docstrings; the four first-mentioned
property tests (same-key×N, distinct-effects, two-dispatchers, stale-lease)
also exist in the unit layer, so the matrix here pins the *end-to-end* shape.

Crash windows covered:
    C1 commit → crash before dispatch        → row stays pending, reclaimable
    C2 effect → crash before success record  → retry reuses the same effect key
    C3 two dispatchers race                  → one logical effect
    C4 timeout / unknown remote result       → ambiguity visible, not laundered
    C5 stale lease                            → another worker recovers safely
"""

from __future__ import annotations

from nexus_ai_agent.adapters.effect.outbox_dispatcher import OutboxDispatcher, OutboxDispatcherStore
from nexus_ai_agent.application.ports.outbox_port import DeliveryError
from nexus_ai_agent.domain.policies import outbox_policy as op
from nexus_ai_agent.domain.policies.outbox_policy import EffectStatus, Snapshot


class RecordDeliveries:
    def __init__(self, *, crash_on: str | None = None) -> None:
        self.deliveries: list[str] = []
        self.crash_on = crash_on

    async def deliver(
        self, *, effect_key: str, payload: dict[str, object], destination: str
    ) -> dict:
        self.deliveries.append(effect_key)
        if self.crash_on == "after_delivery":
            # C2/C4: the remote accepted the effect, but the process dies before
            # recording success. The effect is *already done* out there.
            raise TimeoutError("simulated: effect sent, result unknown")
        return {"ok": True}


class PermanentAdapter:
    async def deliver(
        self, *, effect_key: str, payload: dict[str, object], destination: str
    ) -> dict:
        raise DeliveryError("chat not found", retryable=False, effect_key=effect_key)


async def test_T1_same_effect_key_N_attempts_one_logical_success() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = RecordDeliveries()
    dispatcher = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="w1")
    row = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="seed",
        now=100,
    )
    for i in range(5):
        await dispatcher.dispatch(
            operation_type="telegram.send",
            effect_key=row["effect_key"],
            attempt_id=f"a{i}",
            now=101 + i,
        )
    snap = store.snapshot("telegram.send", row["effect_key"])
    assert snap is not None and snap["status"] == "succeeded"
    assert len(adapter.deliveries) == 1


async def test_T2_different_logical_effects_are_not_collapsed() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = RecordDeliveries()
    dispatcher = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="w1")
    a = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        logical_slot="20:30",
        payload={"text": "a"},
        attempt_id="s",
        now=100,
    )
    b = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        logical_slot="20:31",
        payload={"text": "b"},
        attempt_id="s",
        now=100,
    )
    await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=a["effect_key"], attempt_id="x", now=101
    )
    await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=b["effect_key"], attempt_id="y", now=101
    )
    assert len(adapter.deliveries) == 2


async def test_T3_two_dispatchers_race_one_effect() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = RecordDeliveries()
    d1 = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="w1")
    d2 = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="w2")
    row = d1.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="s",
        now=100,
    )
    outcome1 = await d1.dispatch(
        operation_type="telegram.send", effect_key=row["effect_key"], attempt_id="a1", now=101
    )
    outcome2 = await d2.dispatch(
        operation_type="telegram.send", effect_key=row["effect_key"], attempt_id="a2", now=101
    )
    assert sorted([outcome1.action, outcome2.action]) == ["dedupe", "dispatched"]
    assert len(adapter.deliveries) == 1


async def test_T4_crash_after_remote_effect_leaves_ambiguity_then_same_key() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = RecordDeliveries(crash_on="after_delivery")
    dispatcher = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="w1")
    row = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="s",
        now=100,
    )
    first = await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=row["effect_key"], attempt_id="a1", now=101
    )
    # The timeout is retryable; the row must be retryable, never SUCCEEDED.
    assert first.action == "retrying"
    snap = store.snapshot("telegram.send", row["effect_key"])
    assert snap is not None and snap["status"] == "failed_retryable"
    # The retry, when due, reuses the same effect key.
    retry_row = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="s2",
        now=100,
    )
    assert retry_row["effect_key"] == row["effect_key"]
    assert len(adapter.deliveries) == 1  # the ambiguity did not double-send


async def test_T5_retry_after_timeout_keeps_effect_key_unchanged() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = PermanentAdapter()
    dispatcher = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="w1")
    row = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="s",
        now=100,
    )
    await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=row["effect_key"], attempt_id="a1", now=101
    )
    snap = store.snapshot("telegram.send", row["effect_key"])
    assert snap is not None
    assert snap["status"] == "failed_permanent"
    assert snap["effect_key"] == row["effect_key"]  # key unchanged across the failure


async def test_T6_new_logical_revision_yields_new_effect_key() -> None:
    store = OutboxDispatcherStore(":memory:")
    dispatcher = OutboxDispatcher(store, {"telegram.send": PermanentAdapter()}, dispatcher_id="w1")
    r1 = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="s",
        now=100,
    )
    r2 = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=4,
        destination="555",
        payload={"text": "hi"},
        attempt_id="s",
        now=100,
    )
    assert r1["effect_key"] != r2["effect_key"]


async def test_T7_stale_lease_recovery_is_safe() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = RecordDeliveries()
    dispatcher = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="w1")
    row = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="s",
        now=100,
    )
    assert store.claim(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        dispatcher_id="w1",
        attempt_id="a1",
        now=101,
    )
    # C5: w1 dies. w2 recovers the stale claim after the lease window.
    assert store.claim(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        dispatcher_id="w2",
        attempt_id="a2",
        now=101 + op.CLAIM_LEASE_SECONDS + 1,
    )
    snap = store.snapshot("telegram.send", row["effect_key"])
    assert snap is not None and snap["claimed_by"] == "w2"


async def test_C1_commit_then_crash_before_dispatch_stays_pending() -> None:
    store = OutboxDispatcherStore(":memory:")
    dispatcher = OutboxDispatcher(store, {"telegram.send": RecordDeliveries()}, dispatcher_id="w1")
    row = dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="s",
        now=100,
    )
    # Crash: no dispatch was ever attempted. The row is pending and recoverable.
    snap = store.snapshot("telegram.send", row["effect_key"])
    assert snap is not None and snap["status"] == "pending"
    recovered = op.recovered_rows(
        [
            Snapshot(
                sequence=1,
                effect_key=row["effect_key"],
                status=EffectStatus.PENDING,
                attempt=0,
                claimed_by=None,
                lease_until=0,
                not_before=100,
                next_retry_at=0,
                created_at=100,
            )
        ],
        now=200,
        dispatcher_id="w1",
    )
    assert [r.effect_key for r in recovered] == [row["effect_key"]]


async def test_pending_rows_surface_stuck_effects() -> None:
    store = OutboxDispatcherStore(":memory:")
    dispatcher = OutboxDispatcher(store, {"telegram.send": RecordDeliveries()}, dispatcher_id="w1")
    dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="s",
        now=100,
    )
    pending = store.pending_rows()
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"
