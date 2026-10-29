"""Effect dedupe (P4): same key × N attempts → one logical success, distinct
effects not collapsed, and the missing-adapter fail-closed behaviour.

Everything runs against the reference store's UNIQUE key, which is the
database-level dedupe (mutation B targets it).
"""

from __future__ import annotations

import uuid

from nexus_ai_agent.adapters.effect.outbox_dispatcher import OutboxDispatcher, OutboxDispatcherStore


class CounterAdapter:
    def __init__(self) -> None:
        self.deliveries = 0

    async def deliver(
        self, *, effect_key: str, payload: dict[str, object], destination: str
    ) -> dict:
        self.deliveries += 1
        return {"ok": True, "effect_key": effect_key}


class FailingAdapter:
    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable
        self.deliveries = 0

    async def deliver(
        self, *, effect_key: str, payload: dict[str, object], destination: str
    ) -> dict:
        from nexus_ai_agent.application.ports.outbox_port import DeliveryError

        self.deliveries += 1
        raise DeliveryError("boom", retryable=self.retryable, effect_key=effect_key)


def _record(dispatcher: OutboxDispatcher, *, logical_slot: str, now: int) -> dict:
    return dispatcher.record_intent(
        operation_type="telegram.send",
        logical_entity="post-7",
        logical_revision=3,
        destination="555",
        logical_slot=logical_slot,
        payload={"text": "hi"},
        attempt_id=uuid.uuid4().hex,
        now=now,
    )


async def test_same_effect_key_n_attempts_one_logical_success() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = CounterAdapter()
    dispatcher = OutboxDispatcher(store, {"telegram.send": adapter})

    row = _record(dispatcher, logical_slot="20:30", now=100)
    for i in range(4):  # 4 attempts, one logical intent
        outcome = await dispatcher.dispatch(
            operation_type="telegram.send",
            effect_key=row["effect_key"],
            attempt_id=uuid.uuid4().hex,
            now=101 + i,
        )
    assert outcome.action == "dedupe"
    assert adapter.deliveries == 1
    final = store.snapshot("telegram.send", row["effect_key"])
    assert final is not None and final["status"] == "succeeded"


async def test_distinct_logical_effects_are_not_collapsed() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = CounterAdapter()
    dispatcher = OutboxDispatcher(store, {"telegram.send": adapter})

    row_a = _record(dispatcher, logical_slot="20:30", now=100)
    row_b = _record(dispatcher, logical_slot="20:31", now=100)
    assert row_a["effect_key"] != row_b["effect_key"]
    await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=row_a["effect_key"], attempt_id="x1", now=101
    )
    await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=row_b["effect_key"], attempt_id="x2", now=102
    )
    assert adapter.deliveries == 2


async def test_missing_adapter_is_fail_closed_not_silent_success() -> None:
    store = OutboxDispatcherStore(":memory:")
    dispatcher = OutboxDispatcher(store, {})  # no adapters at all
    row = _record(dispatcher, logical_slot="20:30", now=100)
    outcome = await dispatcher.dispatch(
        operation_type="telegram.send",
        effect_key=row["effect_key"],
        attempt_id="x",
        now=101,
    )
    assert outcome.action == "permanent"
    assert outcome.status == "failed_permanent"
    final = store.snapshot("telegram.send", row["effect_key"])
    assert final is not None and final["status"] == "failed_permanent"


async def test_non_retryable_failure_is_permanent_immediately() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = FailingAdapter(retryable=False)
    dispatcher = OutboxDispatcher(store, {"telegram.send": adapter})
    row = _record(dispatcher, logical_slot="20:30", now=100)
    outcome = await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=row["effect_key"], attempt_id="x", now=101
    )
    assert outcome.status == "failed_permanent"
    # even a fresh attempt sees "dedupe" because the row is terminal.
    again = await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=row["effect_key"], attempt_id="y", now=102
    )
    assert again.action == "dedupe"
    assert adapter.deliveries == 1


async def test_retryable_failure_retries_with_the_same_key() -> None:
    store = OutboxDispatcherStore(":memory:")
    adapter = FailingAdapter(retryable=True)
    dispatcher = OutboxDispatcher(store, {"telegram.send": adapter})
    row = _record(dispatcher, logical_slot="20:30", now=100)
    first = await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=row["effect_key"], attempt_id="x", now=101
    )
    assert first.status == "failed_retryable"
    # the same key, a new attempt, after the backoff window.
    snap = store.snapshot("telegram.send", row["effect_key"])
    assert snap is not None
    due = int(snap["next_retry_at"])
    second = await dispatcher.dispatch(
        operation_type="telegram.send", effect_key=row["effect_key"], attempt_id="y", now=due
    )
    assert second.status == "failed_retryable"
    assert adapter.deliveries == 2


def test_unique_constraint_is_the_cross_process_dedupe_backstop(tmp_path) -> None:  # noqa: ANN001
    """Mutation B targets this test: without UNIQUE(operation_type, effect_key)
    a second connection inserts a duplicate row and this test goes red."""
    import uuid as _uuid

    db = str(tmp_path / "effects.sqlite3")
    store_a = OutboxDispatcherStore(db)
    store_b = OutboxDispatcherStore(db)  # a different connection = another process

    key = "telegram.send.deadbeef"
    row_a, created_a = store_a.append_or_get(
        operation_type="telegram.send",
        effect_key=key,
        attempt_id=_uuid.uuid4().hex,
        destination="555",
        logical_entity="post-7",
        payload={"text": "hi"},
        now=100,
    )
    assert created_a is True
    row_b, created_b = store_b.append_or_get(
        operation_type="telegram.send",
        effect_key=key,
        attempt_id=_uuid.uuid4().hex,
        destination="555",
        logical_entity="post-7",
        payload={"text": "hi"},
        now=100,
    )
    assert created_b is False
    assert row_b["seq"] == row_a["seq"]  # the SAME stored row, not a second one
    assert len(store_a.all_rows()) == 1
