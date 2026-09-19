"""Two-way reconciler: dry-run by default, mutation only via apply.

Uses the real LangGraph SQLite schema (via ``SqliteSaver.setup``) so the
golden-fingerprint gate is exercised for real; checkpoint rows are inserted
directly for determinism.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from typer.testing import CliRunner

from nexus_ai_agent.storage.checkpoint_adapter import SQLiteCheckpointAdapter
from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_store import SQLiteCheckpointLifecycleStore
from nexus_ai_agent.storage.checkpoint_reconciler import CheckpointReconciler

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _make_db(tmp_path: Path, threads: int = 1, checkpoints: int = 2) -> Path:
    path = tmp_path / "lg.sqlite"
    conn = sqlite3.connect(path)
    SqliteSaver(conn).setup()
    for t in range(1, threads + 1):
        for i in range(1, checkpoints + 1):
            conn.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                "type, checkpoint, metadata) VALUES (?, '', ?, ?, 'json', '{}', '{}')",
                (f"t{t}", f"cp{i}", None if i == 1 else f"cp{i - 1}"),
            )
    conn.commit()
    conn.close()
    return path


def _make_store(tmp_path: Path) -> SQLiteCheckpointLifecycleStore:
    return SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))


def _seed_lifecycle(
    store: SQLiteCheckpointLifecycleStore, threads: int = 1, checkpoints: int = 2
) -> None:
    for t in range(1, threads + 1):
        for i in range(1, checkpoints + 1):
            store.upsert(CheckpointRecord(f"t{t}", f"cp{i}", NOW - timedelta(days=30)))


def _add_orphan(store: SQLiteCheckpointLifecycleStore, age: timedelta) -> None:
    store.upsert(CheckpointRecord("t9", "c9", NOW - age, last_accessed_at=NOW - age))


def _open(db: Path) -> tuple[SQLiteCheckpointAdapter, SQLiteCheckpointLifecycleStore]:
    return SQLiteCheckpointAdapter(str(db)), _make_store(db.parent)


def _count_rows(db: Path, table: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ── dry-run semantics ───────────────────────────────────────────────────


def test_dry_run_reports_without_any_mutation(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    store = _make_store(tmp_path)
    _seed_lifecycle(store)  # covers t1/cp1 and t1/cp2
    _add_orphan(store, timedelta(hours=48))  # t9/c9 orphan
    reconciler = CheckpointReconciler(*_open(db))

    report = reconciler.run(apply=False, now=NOW)

    assert report.applied is False
    assert report.backfilled == 0
    assert report.purged == 0
    assert report.checkpoints == 2
    assert report.lifecycle_rows == 3
    assert report.rows_scanned == 5
    kinds = sorted(a.kind for a in report.anomalies)
    assert kinds == ["orphan_lifecycle"]
    # Dry-run must not have written anything at all.
    assert len(store.records()) == 3
    assert _count_rows(db, "checkpoints") == 2


def test_dry_run_missing_lifecycle_is_a_protected_backfill_plan(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    store = _make_store(tmp_path)
    _seed_lifecycle(store, checkpoints=1)  # t1/cp1 only; t1/cp2 is missing
    reconciler = CheckpointReconciler(*_open(db))

    report = reconciler.run(apply=False, now=NOW)
    missing = [a for a in report.anomalies if a.kind == "missing_lifecycle"]
    assert [a.checkpoint_id for a in missing] == ["cp2"]


def test_broken_lineage_is_reported(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE checkpoints SET parent_checkpoint_id = 'ghost' "
        "WHERE thread_id = 't1' AND checkpoint_id = 'cp2'"
    )
    conn.commit()
    conn.close()
    store = _make_store(tmp_path)
    _seed_lifecycle(store)
    reconciler = CheckpointReconciler(*_open(db))

    report = reconciler.run(apply=False, now=NOW)
    assert any(a.kind == "broken_lineage" for a in report.anomalies)


# ── backfill (direction: checkpoints -> lifecycle) ──────────────────────


def test_apply_backfills_missing_with_protected_age(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    store = _make_store(tmp_path)
    _seed_lifecycle(store, checkpoints=1)  # t1/cp2 missing
    reconciler = CheckpointReconciler(*_open(db))

    report = reconciler.run(apply=True, now=NOW)

    assert report.applied is True
    assert report.backfilled == 1
    backfilled = [r for r in store.records() if r.checkpoint_id == "cp2"]
    assert len(backfilled) == 1
    # Unknown age is protected: recorded as created *now*, never as old.
    assert backfilled[0].created_at == NOW
    assert backfilled[0].last_accessed_at is None
    # Idempotent: the same run again backfills nothing.
    second = reconciler.run(apply=True, now=NOW)
    assert second.backfilled == 0
    assert len(store.records()) == 2


# ── purge (direction: lifecycle -> checkpoints) ─────────────────────────


def test_orphan_blocked_until_is_plus_24h(tmp_path: Path) -> None:
    db = _make_db(tmp_path, threads=200, checkpoints=1)
    store = _make_store(tmp_path)
    _seed_lifecycle(store, threads=200, checkpoints=1)
    _add_orphan(store, timedelta(hours=48))
    reconciler = CheckpointReconciler(*_open(db))

    report = reconciler.run(apply=False, now=NOW)
    assert len(report.orphan_decisions) == 1
    orphan = report.orphan_decisions[0]
    assert orphan.blocked_until == (NOW - timedelta(hours=48)) + timedelta(hours=24)
    assert orphan.would_purge is False  # dry-run never purges


def test_purge_eligible_orphan_is_applied(tmp_path: Path) -> None:
    db = _make_db(tmp_path, threads=200, checkpoints=1)
    store = _make_store(tmp_path)
    _seed_lifecycle(store, threads=200, checkpoints=1)
    _add_orphan(store, timedelta(hours=48))
    reconciler = CheckpointReconciler(*_open(db))

    report = reconciler.run(apply=True, now=NOW)

    assert report.purged == 1
    assert report.backfilled == 0
    # Only the lifecycle index row was removed; checkpoints are untouched.
    assert len(store.records()) == 200
    assert _count_rows(db, "checkpoints") == 200
    assert not any(r.checkpoint_id == "c9" for r in store.records())


def test_purge_blocked_by_anomaly_rate(tmp_path: Path) -> None:
    # 90 covered checkpoints + 2 orphans -> 2/182 ≈ 1.1% > 1%: no purge.
    db = _make_db(tmp_path, threads=90, checkpoints=1)
    store = _make_store(tmp_path)
    _seed_lifecycle(store, threads=90, checkpoints=1)
    _add_orphan(store, timedelta(hours=48))
    store.upsert(CheckpointRecord("t8", "c8", NOW - timedelta(hours=48)))
    reconciler = CheckpointReconciler(*_open(db))

    report = reconciler.run(apply=True, now=NOW)

    assert report.anomaly_rate > 0.01
    assert report.purged == 0
    assert len(store.records()) == 92


def test_purge_blocked_by_young_age_and_block(tmp_path: Path) -> None:
    db = _make_db(tmp_path, threads=200, checkpoints=1)
    store = _make_store(tmp_path)
    _seed_lifecycle(store, threads=200, checkpoints=1)
    _add_orphan(store, timedelta(hours=10))  # 10h old: blocked until +14h
    reconciler = CheckpointReconciler(*_open(db))

    report = reconciler.run(apply=True, now=NOW)

    assert report.purged == 0
    orphan = report.orphan_decisions[0]
    assert orphan.would_purge is False
    assert any("blocked until" in reason for reason in orphan.reasons)


def test_purge_reverifies_orphan_status_before_delete(tmp_path: Path, monkeypatch) -> None:
    """If the checkpoint reappears between scan and apply, nothing is purged."""
    db = _make_db(tmp_path, threads=200, checkpoints=1)
    store = _make_store(tmp_path)
    _seed_lifecycle(store, threads=200, checkpoints=1)
    _add_orphan(store, timedelta(hours=48))
    adapter = SQLiteCheckpointAdapter(str(db))
    store_for_reconciler = _make_store(tmp_path)
    reconciler = CheckpointReconciler(adapter, store_for_reconciler)
    # Simulate the checkpoint reappearing right before the mutation phase.
    monkeypatch.setattr(adapter, "exists_checkpoint", lambda *a: True)

    report = reconciler.run(apply=True, now=NOW)

    assert report.purged == 0
    assert any(r.checkpoint_id == "c9" for r in store_for_reconciler.records())
    adapter.close()


# ── fail-safe gates ─────────────────────────────────────────────────────


def test_kill_switch_blocks_apply(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    store = _make_store(tmp_path)
    _add_orphan(store, timedelta(hours=48))
    reconciler = CheckpointReconciler(*_open(db), enabled=False)

    report = reconciler.run(apply=True, now=NOW)

    assert report.applied is False
    assert report.health_gate == "disabled:kill_switch"
    assert report.backfilled == 0
    assert report.purged == 0


def test_scan_error_disables_apply(tmp_path: Path, monkeypatch) -> None:
    db = _make_db(tmp_path)
    store = _make_store(tmp_path)
    adapter = SQLiteCheckpointAdapter(str(db))
    reconciler = CheckpointReconciler(adapter, store)

    def _boom(thread_id: str) -> list:
        raise sqlite3.OperationalError("simulated connection error")

    monkeypatch.setattr(adapter, "list_checkpoints", _boom)
    report = reconciler.run(apply=True, now=NOW)

    assert report.applied is False
    assert report.health_gate.startswith("disabled:scan_errors")
    # A connection error is NOT schema drift.
    assert report.langgraph_schema == "match"


def test_missing_golden_disables_apply_and_alerts(tmp_path: Path, caplog) -> None:
    db = _make_db(tmp_path)
    reconciler = CheckpointReconciler(*_open(db), golden_path=tmp_path / "does-not-exist.json")

    with caplog.at_level("WARNING", logger="nexus_ai_agent.lifecycle"):
        report = reconciler.run(apply=True, now=NOW)

    assert report.langgraph_schema == "disabled:missing_golden"
    assert report.applied is False
    # The alert is a structured event on the lifecycle logger.
    assert any("missing" in record.message for record in caplog.records)
    # No golden was auto-generated.
    assert not (tmp_path / "does-not-exist.json").exists()


def test_schema_mismatch_disables_apply(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE checkpoints ADD COLUMN synthetic_drift TEXT")
    conn.commit()
    conn.close()
    reconciler = CheckpointReconciler(*_open(db))

    report = reconciler.run(apply=True, now=NOW)

    assert report.langgraph_schema == "disabled:fingerprint_mismatch"
    assert report.applied is False


# ── CLI ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch) -> Path:
    from nexus_ai_agent.config.settings import get_settings

    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "lg.sqlite"))
    # Hermetic backend: pin the SQLite path even if a PG URL is present in
    # the environment (PR3 backend branching in the CLI).
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUS_LIFECYCLE_HOOKS_ENABLED", "true")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_cli_reconcile_dry_run_default_and_json(cli_env: Path) -> None:
    from nexus_ai_agent.cli import app

    _make_db(cli_env)
    store = _make_store(cli_env)
    _seed_lifecycle(store)
    _add_orphan(store, timedelta(hours=48))

    result = CliRunner().invoke(app, ["checkpoints", "reconcile", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is False
    assert payload["backfilled"] == 0
    assert payload["purged"] == 0
    assert payload["anomalies"]
    assert "would_free_bytes_estimate" in payload
    # The default (no --apply) must not have mutated anything.
    assert len(store.records()) == 3  # 2 seeded + 1 orphan, untouched
    store.close()


def test_cli_reconcile_apply_requires_flag(cli_env: Path) -> None:
    from nexus_ai_agent.cli import app

    _make_db(cli_env, threads=200, checkpoints=1)
    store = _make_store(cli_env)
    _seed_lifecycle(store, threads=200, checkpoints=1)
    _add_orphan(store, timedelta(hours=48))

    result = CliRunner().invoke(app, ["checkpoints", "reconcile", "--apply", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is True
    assert payload["purged"] == 1
    assert len(store.records()) == 200
    store.close()
