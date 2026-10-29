"""Reference dispatcher contract (P3): claim fencing, lease ownership, and the
observer side (effect events + low-cardinality counters, never secrets).
"""

from __future__ import annotations

from nexus_ai_agent.adapters.effect.outbox_dispatcher import OutboxDispatcher, OutboxDispatcherStore


class EchoAdapter:
    def __init__(self) -> None:
        self.deliveries = 0

    def deliver(self, *, effect_key: str, payload: dict[str, object], destination: str) -> dict:
        self.deliveries += 1
        return {"echo": destination}


def test_only_the_claim_winner_reaches_the_effect() -> None:
    """T3: two dispatchers racing on one effect → exactly one delivery.

    The store's guarded UPDATE makes the second claim a no-op; the loser sees
    the row claimed by the winner, not a second live claim.
    """
    store = OutboxDispatcherStore(":memory:")
    adapter = EchoAdapter()
    d1 = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="worker-1")
    d2 = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="worker-2")

    row = d1.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="seed",
        now=100,
    )
    result = d1.store.claim(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        dispatcher_id="worker-1",
        attempt_id="attempt-1",
        now=101,
    )
    assert result is True
    # worker-2 cannot claim the row already claimed by worker-1 (live lease).
    second = d2.store.claim(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        dispatcher_id="worker-2",
        attempt_id="attempt-2",
        now=101,
    )
    assert second is False
    snap = store.snapshot("telegram.send", row["effect_key"])
    assert snap is not None and snap["claimed_by"] == "worker-1"


def test_stale_claim_is_taken_over_after_lease_expiry() -> None:
    """T7/C5: stale lease → safe recovery by another worker."""
    store = OutboxDispatcherStore(":memory:")
    adapter = EchoAdapter()
    d1 = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="worker-1")

    row = d1.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="seed",
        now=100,
    )
    assert store.claim(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        dispatcher_id="worker-1",
        attempt_id="attempt-1",
        now=101,
    )
    # 61 seconds later the lease is stale: worker-2 recovers it.
    taken = store.claim(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        dispatcher_id="worker-2",
        attempt_id="attempt-2",
        now=101 + 61,
    )
    assert taken is True
    snap = store.snapshot("telegram.send", row["effect_key"])
    assert snap is not None and snap["claimed_by"] == "worker-2"
    assert snap["attempt"] == 2


def test_stale_claimant_cannot_overwrite_a_newer_attempt() -> None:
    """C5 fencing: after takeover, the old worker's outcome write is a no-op."""
    store = OutboxDispatcherStore(":memory:")
    adapter = EchoAdapter()
    d1 = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="worker-1")

    row = d1.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        payload={"text": "hi"},
        attempt_id="seed",
        now=100,
    )
    assert store.claim(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        dispatcher_id="worker-1",
        attempt_id="attempt-1",
        now=101,
    )
    assert store.claim(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        dispatcher_id="worker-2",
        attempt_id="attempt-2",
        now=162,
    )
    # the stale worker-1 tries to write SUCCEEDED with its superseded attempt id.
    wrote = store.mark_succeeded(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        attempt_id="attempt-1",
        outcome={"echo": "555"},
        now=163,
    )
    assert wrote is False  # fenced
    snap = store.snapshot("telegram.send", row["effect_key"])
    assert snap is not None and snap["attempt_id"] == "attempt-2"


def test_schema_is_isolated_to_the_outbox_table() -> None:
    import sqlite3

    store = OutboxDispatcherStore(":memory:")
    rows = store._mem_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert [r[0] for r in rows] == ["nexus_effect_outbox"]
    del sqlite3


def test_intent_lifecycle_events_distinguish_created_from_duplicate(caplog) -> None:
    """The producer side emits ``effect_created`` then ``effect_duplicate``.

    This is the behaviour ``adapters/effect/observability_backend.py`` exists
    for: re-submitting the same logical intent is observable, never silent.
    """
    import logging

    store = OutboxDispatcherStore(":memory:")
    d1 = OutboxDispatcher(store, {"telegram.send": EchoAdapter()}, dispatcher_id="worker-1")
    with caplog.at_level(logging.DEBUG, logger="nexus_ai_agent.lifecycle"):
        for _ in range(2):
            d1.record_intent(
                operation_type="telegram.send",
                logical_entity="post-9",
                logical_revision=1,
                destination="555",
                payload={"text": "hi"},
                attempt_id="seed",
                now=100,
            )
    events = [getattr(r, "event", None) for r in caplog.records]
    assert "effect_created" in events
    assert "effect_duplicate" in events
    # the raw effect key never travels into an event: only a hashed prefix does.
    assert all(not hasattr(r, "effect_key") for r in caplog.records)


async def _drain(dispatcher, operation_type, effect_key, attempt_id, now):
    return await dispatcher.dispatch(
        operation_type=operation_type,
        effect_key=effect_key,
        attempt_id=attempt_id,
        now=now,
    )


def test_successful_delivery_emits_claimed_and_succeeded_and_counters(caplog) -> None:
    """A delivery runs through ``effect_delivery`` and the observer sees it."""
    import asyncio
    import logging

    from nexus_ai_agent.infrastructure.observability.metrics import get_metrics_registry

    store = OutboxDispatcherStore(":memory:")
    adapter = EchoAdapter()
    d1 = OutboxDispatcher(store, {"telegram.send": adapter}, dispatcher_id="worker-1")
    row = d1.record_intent(
        operation_type="telegram.send",
        logical_entity="post-11",
        logical_revision=1,
        destination="555",
        payload={"text": "hi"},
        attempt_id="seed",
        now=100,
    )
    before = dict(get_metrics_registry().snapshot())
    with caplog.at_level(logging.DEBUG, logger="nexus_ai_agent.lifecycle"):
        outcome = asyncio.run(_drain(d1, "telegram.send", row["effect_key"], "attempt-1", 101))
    assert outcome.status == "succeeded"
    assert adapter.deliveries == 1
    events = [getattr(r, "event", None) for r in caplog.records]
    assert "effect_claimed" in events and "effect_succeeded" in events
    after = get_metrics_registry().snapshot()
    assert (
        after.get("nexus_effect_succeeded_total", 0)
        >= before.get("nexus_effect_succeeded_total", 0) + 1
    )


def test_failing_delivery_emits_failed_event_and_retryable_counter(caplog) -> None:
    """A refused effect is a typed, observable failure — never a fake SUCCEEDED."""
    import asyncio
    import logging

    from nexus_ai_agent.application.ports.outbox_port import DeliveryError
    from nexus_ai_agent.infrastructure.observability.metrics import get_metrics_registry

    class RefusingAdapter:
        async def deliver(self, *, effect_key, payload, destination):
            raise DeliveryError("destination refused", retryable=True)

    store = OutboxDispatcherStore(":memory:")
    d1 = OutboxDispatcher(store, {"telegram.send": RefusingAdapter()}, dispatcher_id="worker-1")
    row = d1.record_intent(
        operation_type="telegram.send",
        logical_entity="post-12",
        logical_revision=1,
        destination="555",
        payload={"text": "hi"},
        attempt_id="seed",
        now=100,
    )
    before = dict(get_metrics_registry().snapshot())
    with caplog.at_level(logging.DEBUG, logger="nexus_ai_agent.lifecycle"):
        outcome = asyncio.run(_drain(d1, "telegram.send", row["effect_key"], "attempt-1", 101))
    assert outcome.status == "failed_retryable"
    events = [getattr(r, "event", None) for r in caplog.records]
    assert "effect_failed" in events
    after = get_metrics_registry().snapshot()
    assert (
        after.get("nexus_effect_failed_retryable_total", 0)
        >= before.get("nexus_effect_failed_retryable_total", 0) + 1
    )


def test_missing_adapter_resolves_to_a_fail_closed_handler() -> None:
    """``_as_effect_handler`` on a non-conforming adapter never fakes success."""
    import asyncio

    from nexus_ai_agent.adapters.effect.outbox_dispatcher import _as_effect_handler
    from nexus_ai_agent.application.ports.outbox_port import DeliveryError

    handler = _as_effect_handler(object())  # no deliver()
    try:
        asyncio.run(handler(effect_key="ek", destination="d", payload={}))
    except DeliveryError as exc:
        assert exc.retryable is False
    else:  # pragma: no cover - a fabricated success would fail the guard's purpose
        raise AssertionError("a missing effect adapter must not fabricate success")
