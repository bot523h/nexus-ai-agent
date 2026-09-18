"""PR2 C4 — the remaining eight adversarial tests.

Each test pins one invariant that a "happy path" suite would miss:
policy boundaries, error containment, context loss, shutdown flush,
golden safety, full delegation, CLI kill-switch, and lock contention.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from typer.testing import CliRunner

from nexus_ai_agent.adapters.langgraph.lifecycle_recording import (
    LifecycleRecordingSaver,
    access_context,
    nexus_access_context,
)
from nexus_ai_agent.domain.policies.reconciler_policy import purge_allowed
from nexus_ai_agent.storage.checkpoint_adapter import SQLiteCheckpointAdapter
from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_adapter import (
    SQLiteCheckpointLifecycleAdapter,
)
from nexus_ai_agent.storage.checkpoint_lifecycle_store import (
    SQLiteCheckpointLifecycleStore,
    cleanup_lock,
)
from nexus_ai_agent.storage.checkpoint_reconciler import CheckpointReconciler
from nexus_ai_agent.storage.langgraph_checkpoint import AsyncCompatibleSqliteSaver

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


class _FailingLifecycle:
    def __init__(self) -> None:
        self.record_calls = 0
        self.touch_calls = 0

    async def record_checkpoint(self, *args: object, **kwargs: object) -> None:
        self.record_calls += 1
        raise RuntimeError("password=boom123")

    async def touch_thread(self, thread_id: str, *, accessed_at: datetime) -> None:
        self.touch_calls += 1
        raise RuntimeError("token=boom456")


class _FakeSaver:
    def __init__(self) -> None:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        self.serde = JsonPlusSerializer()

    def get_tuple(self, config: object) -> dict[str, object]:
        return {"checkpoint": {"v": 1}, "config": config}

    async def aget_tuple(self, config: object) -> dict[str, object]:
        return self.get_tuple(config)

    async def aput(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"configurable": {"thread_id": "t", "checkpoint_id": "cp1"}}


def _langgraph_db(path: Path, thread: str = "t1", checkpoints: int = 1) -> None:
    conn = sqlite3.connect(path)
    SqliteSaver(conn).setup()
    for i in range(1, checkpoints + 1):
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
            "type, checkpoint, metadata) VALUES (?, '', ?, ?, 'json', '{}', '{}')",
            (thread, f"cp{i}", None if i == 1 else f"cp{i - 1}"),
        )
    conn.commit()
    conn.close()


# ── 1. policy boundary values (S2) ─────────────────────────────────────


def test_purge_policy_boundary_values() -> None:
    # Base guards are clean: 25h-old record, an already-elapsed block.
    def _decision(**overrides: object) -> tuple[bool, tuple[str, ...]]:
        args: dict[str, object] = {
            "rows_scanned": 100000,
            "anomalies": 0,
            "record_age": timedelta(hours=25),
            "blocked_until": NOW - timedelta(hours=1),
            "now": NOW,
        }
        args.update(overrides)
        decision = purge_allowed(**args)  # type: ignore[arg-type]
        return decision.allowed, decision.reasons

    # Exactly 1.0% anomaly rate is allowed (strictly above is not).
    assert _decision(rows_scanned=1000, anomalies=10)[0]
    over_allowed, over_reasons = _decision(rows_scanned=1000, anomalies=11)
    assert not over_allowed
    assert any("anomaly_rate" in r for r in over_reasons)

    # Exactly 500 anomalies is allowed; 501 blocks even at a clean rate.
    assert _decision(rows_scanned=100000, anomalies=500)[0]
    over_abs_allowed, over_abs_reasons = _decision(rows_scanned=100000, anomalies=501)
    assert not over_abs_allowed
    assert any("501 > 500" in r for r in over_abs_reasons)

    # Age exactly 24h blocks; just above allows.
    young_allowed, young_reasons = _decision(record_age=timedelta(hours=24))
    assert not young_allowed
    assert any("age" in r for r in young_reasons)
    assert _decision(record_age=timedelta(hours=24, seconds=1))[0]

    # An unknown block state always blocks, even with clean guards.
    unknown_allowed, unknown_reasons = _decision(blocked_until=None)
    assert not unknown_allowed
    assert any("fail-safe" in r for r in unknown_reasons)

    # A still-active block always blocks.
    active_allowed, active_reasons = _decision(blocked_until=NOW + timedelta(hours=1))
    assert not active_allowed
    assert any("blocked until" in r for r in active_reasons)


# ── 2. lifecycle errors never propagate (I5) ───────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_errors_never_propagate() -> None:
    config = {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}

    # Record path (upsert) — failure stays internal, result intact.
    record_lifecycle = _FailingLifecycle()
    saver = LifecycleRecordingSaver(_FakeSaver(), record_lifecycle)
    result = await saver.aput(config, {"id": "cp1"}, {}, {})
    assert result["configurable"]["checkpoint_id"] == "cp1"
    await asyncio.sleep(0)
    assert record_lifecycle.record_calls == 1
    # Latched off by the health gate: the runtime still works, it just
    # stops writing lifecycle data.
    assert saver.enabled is False

    # Touch path — the flush failure stays internal, result intact.
    touch_lifecycle = _FailingLifecycle()
    touch_saver = LifecycleRecordingSaver(_FakeSaver(), touch_lifecycle)
    async with access_context("user"):
        got = await touch_saver.aget_tuple(config)
    assert got is not None
    assert got["checkpoint"] == {"v": 1}
    await touch_saver.flush()  # would raise if the failure propagated
    assert touch_lifecycle.touch_calls == 1


# ── 3. lost context means no touch (R3) ────────────────────────────────


@pytest.mark.asyncio
async def test_lost_context_means_no_touch() -> None:
    touches: list[str] = []

    class _Lifecycle:
        async def touch_thread(self, thread_id: str, *, accessed_at: datetime) -> None:
            touches.append(thread_id)

        async def record_checkpoint(self, *args: object, **kwargs: object) -> None:
            pass

    saver = LifecycleRecordingSaver(_FakeSaver(), _Lifecycle())
    config = {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}

    # Default context is "system": reads are invisible to the lifecycle.
    assert nexus_access_context.get() == "system"
    saver.get_tuple(config)
    await saver.flush()
    assert touches == []

    # A context that is set and then lost (reset) means no touch either.
    token = nexus_access_context.set("user")
    nexus_access_context.reset(token)
    saver.get_tuple(config)
    await saver.flush()
    assert touches == []

    # Only an explicit, live user context touches — exactly once (coalesced).
    async with access_context("user"):
        saver.get_tuple(config)
        saver.get_tuple(config)
    await saver.flush()
    assert touches == ["t"]


# ── 4. shutdown flush (atexit) persists pending touches (R4) ───────────


def test_shutdown_flush_persists_pending_touches(tmp_path: Path) -> None:
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))
    store.upsert(CheckpointRecord("t1", "cp1", NOW - timedelta(days=1)))
    adapter = SQLiteCheckpointLifecycleAdapter(store)
    saver = LifecycleRecordingSaver(_FakeSaver(), adapter)

    async def _user_read() -> None:
        async with access_context("user"):
            saver.get_tuple({"configurable": {"thread_id": "t1", "checkpoint_ns": ""}})

    asyncio.run(_user_read())
    assert store.records()[0].last_accessed_at is None  # still pending

    # What atexit calls, at process exit with no running loop.
    atexit.register(saver.flush_sync)
    saver.flush_sync()

    assert store.records()[0].last_accessed_at is not None
    # A second shutdown flush is a no-op (nothing pending).
    saver.flush_sync()
    assert len(store.records()) == 1
    store.close()


# ── 5. missing golden never generates files (I11) ──────────────────────


def test_missing_golden_never_generates_files(tmp_path: Path) -> None:
    from nexus_ai_agent.storage.checkpoint_reconciler import DEFAULT_GOLDEN

    db = tmp_path / "lg.sqlite"
    _langgraph_db(db)
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))
    # Aim at the REAL golden directory with a missing filename: no file in
    # that directory may appear as a side effect of a reconcile run.
    golden_dir = DEFAULT_GOLDEN.parent
    missing = golden_dir / "definitely-missing-xyz.json"
    before = sorted(p.name for p in golden_dir.iterdir())
    reconciler = CheckpointReconciler(SQLiteCheckpointAdapter(str(db)), store, golden_path=missing)

    report = reconciler.run(apply=True, now=NOW)
    report = reconciler.run(apply=True, now=NOW)  # refusal is stable

    assert report.langgraph_schema == "disabled:missing_golden"
    assert report.applied is False
    after = sorted(p.name for p in golden_dir.iterdir())
    assert before == after  # nothing was auto-generated
    store.close()


# ── 6. full delegate — introspective (DoD #4, future-proof) ────────────


def test_wrapper_exposes_full_saver_surface(tmp_path: Path) -> None:
    db = tmp_path / "lg.sqlite"
    conn = sqlite3.connect(db)
    raw = AsyncCompatibleSqliteSaver(conn)
    raw.setup()
    raw.put(
        {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}},
        {
            "v": 1,
            "id": "cp1",
            "ts": "2026-01-01T00:00:00Z",
            "channel_values": {},
            "channel_versions": {},
            "versions_seen": {},
            "pending_sends": [],
        },
        {},
        {},
    )
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))
    wrapped = LifecycleRecordingSaver(raw, SQLiteCheckpointLifecycleAdapter(store))

    # Every public name on the raw saver must resolve on the wrapper —
    # this is the future-proof contract for upstream API growth.
    for name in dir(raw):
        if name.startswith("_"):
            continue
        assert hasattr(wrapped, name), f"wrapper lost attribute: {name}"

    # Behavioural parity on reads.
    config = {"configurable": {"thread_id": "nope", "checkpoint_ns": ""}}
    assert wrapped.get_tuple(config) == raw.get_tuple(config)
    assert list(wrapped.list(config)) == list(raw.list(config))
    assert wrapped.config_specs == raw.config_specs
    assert wrapped.serde is raw.serde
    conn.close()
    store.close()


# ── 7. CLI kill-switch blocks apply (I4, product level) ────────────────


def test_cli_kill_switch_blocks_apply(tmp_path: Path, monkeypatch) -> None:
    from nexus_ai_agent.cli import app
    from nexus_ai_agent.config.settings import get_settings

    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "lg.sqlite"))
    monkeypatch.setenv("NEXUS_LIFECYCLE_HOOKS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        _langgraph_db(tmp_path / "lg.sqlite")
        result = CliRunner().invoke(app, ["checkpoints", "reconcile", "--apply", "--json"])
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kill_switch"] is False
    assert payload["applied"] is False
    assert payload["health_gate"] == "disabled:kill_switch"
    assert payload["backfilled"] == 0
    # No records were written by the refused apply (the index file itself
    # may be opened/initialized by the store — that is not a lifecycle write).
    conn = sqlite3.connect(tmp_path / "lg.sqlite.lifecycle")
    if conn.execute("SELECT name FROM sqlite_master WHERE name='checkpoint_lifecycle'").fetchone():
        count = conn.execute("SELECT COUNT(*) FROM checkpoint_lifecycle").fetchone()[0]
        assert count == 0
    conn.close()


# ── 8. apply under lock contention fails safe (no mutation) ────────────


def test_apply_under_lock_contention_fails_safe(tmp_path: Path) -> None:
    db = tmp_path / "lg.sqlite"
    _langgraph_db(db)
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))
    store.upsert(CheckpointRecord("t9", "c9", NOW - timedelta(hours=48)))
    adapter = SQLiteCheckpointAdapter(str(db))

    with cleanup_lock(str(tmp_path / "lg.sqlite.lifecycle")):
        with pytest.raises(RuntimeError, match="already in progress"):
            CheckpointReconciler(adapter, store, core_manifest_head=None).run(apply=True, now=NOW)

    # Nothing was mutated while the lock was held: no purge, no backfill.
    assert len(store.records()) == 1
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    conn.close()
    assert count == 1
    adapter.close()
    store.close()
