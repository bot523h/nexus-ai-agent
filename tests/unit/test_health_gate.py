"""Health gate: 10% failure rate latches the lifecycle off (fail-safe)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from nexus_ai_agent.adapters.langgraph.lifecycle_recording import (
    LifecycleRecordingSaver,
    TouchCoalescer,
)
from nexus_ai_agent.domain.policies.lifecycle_health import (
    HEALTH_GATE_THRESHOLD,
    health_gate_ok,
)


class _FailingLifecycle:
    def __init__(self) -> None:
        self.record_attempts = 0

    async def record_checkpoint(self, *args: object, **kwargs: object) -> None:
        self.record_attempts += 1
        raise RuntimeError("store unavailable")

    async def touch_thread(self, thread_id: str, *, accessed_at: datetime) -> None:
        raise RuntimeError("store unavailable")


class _FakeSaver:
    def __init__(self) -> None:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        self.put_count = 0
        self.serde = JsonPlusSerializer()

    async def aput(self, *args: object, **kwargs: object) -> dict[str, object]:
        # Mirrors LangGraph's positional call: (config, checkpoint, metadata, new_versions)
        self.put_count += 1
        return {"configurable": {"thread_id": "t", "checkpoint_id": f"cp{self.put_count}"}}


def test_gate_allows_below_and_at_threshold() -> None:
    assert health_gate_ok(failed=0, total=0)
    assert health_gate_ok(failed=9, total=100)
    assert health_gate_ok(failed=10, total=100)  # exactly 10% is still allowed
    assert not health_gate_ok(failed=11, total=100)
    assert health_gate_ok(failed=1, total=10, threshold=0.5)
    assert HEALTH_GATE_THRESHOLD == 0.10


@pytest.mark.asyncio
async def test_saver_latches_off_after_gate_trips() -> None:
    lifecycle = _FailingLifecycle()
    saver = LifecycleRecordingSaver(_FakeSaver(), lifecycle)
    config = {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}

    for _ in range(20):
        await saver.aput(config, {"id": "cp"}, {}, {})
        await asyncio.sleep(0)  # let the scheduled op run

    # First op fails (1/1 > 10%) -> latched off; later ops are never attempted.
    assert saver.enabled is False
    assert lifecycle.record_attempts == 1
    # The wrapped saver itself is unaffected: every aput still completed.
    assert saver.put_count == 20


@pytest.mark.asyncio
async def test_gate_never_reenables() -> None:
    lifecycle = _FailingLifecycle()
    saver = LifecycleRecordingSaver(_FakeSaver(), lifecycle, enabled=True)
    config = {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}
    await saver.aput(config, {"id": "cp"}, {}, {})
    await asyncio.sleep(0)
    assert saver.enabled is False
    saver.enabled = True  # external code cannot revive it mid-process
    assert not health_gate_ok(failed=saver._failures, total=saver._ops)


@pytest.mark.asyncio
async def test_drop_window_drops_stale_pending_touches() -> None:
    touched: list[str] = []

    class _Lifecycle:
        async def touch_thread(self, thread_id: str, *, accessed_at: datetime) -> None:
            touched.append(thread_id)

    now = datetime.now(timezone.utc)
    coalescer = TouchCoalescer(drop_window=timedelta(hours=24))
    coalescer.add("stale", now - timedelta(hours=48))
    coalescer.add("fresh", now - timedelta(minutes=5))
    await coalescer.flush(_Lifecycle())
    assert touched == ["fresh"]
