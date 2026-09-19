"""core_schema_fingerprint (C3/S6/R7): Alembic head vs local manifest.

Contract: the core head is read from ``alembic_version`` with a plain SELECT
(no migration run), and a core mismatch is *warning only* — it never disables
the lifecycle or the reconciler (langgraph mismatch, by contrast, disables).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from nexus_ai_agent.storage.checkpoint_adapter import SQLiteCheckpointAdapter
from nexus_ai_agent.storage.checkpoint_fingerprint import (
    compare_core_schema,
    core_manifest_head,
    core_schema_fingerprint,
)
from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_store import SQLiteCheckpointLifecycleStore
from nexus_ai_agent.storage.checkpoint_reconciler import CheckpointReconciler

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
CURRENT_MANIFEST_HEAD = "f4a9c2e71b08"  # single head of migrations/versions


def _langgraph_db(tmp_path: Path) -> Path:
    path = tmp_path / "lg.sqlite"
    conn = sqlite3.connect(path)
    SqliteSaver(conn).setup()
    conn.execute(
        "INSERT INTO checkpoints "
        "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
        "type, checkpoint, metadata) VALUES ('t1', '', 'cp1', NULL, 'json', '{}', '{}')"
    )
    conn.commit()
    conn.close()
    return path


def _stamp_core_head(path: Path, revision: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    conn.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (revision,))
    conn.commit()
    conn.close()


def _golden_for(db_path: Path, golden_path: Path) -> Path:
    """Golden matching *this exact database* (stamped or not).

    Stamping ``alembic_version`` changes the schema-v1 fingerprint, so the
    core-schema tests pin a DB-specific golden; this isolates the *core*
    (warning-only) behaviour from the *langgraph* (disabling) behaviour,
    which is covered separately in test_reconciler.py.
    """
    adapter = SQLiteCheckpointAdapter(str(db_path))
    try:
        fingerprint = adapter.schema_fingerprint()
    finally:
        adapter.close()
    golden_path.write_text(
        json.dumps({"algorithm": "schema-v1", "fingerprint": fingerprint}),
        encoding="utf-8",
    )
    return golden_path


def test_core_head_read_from_alembic_version(tmp_path: Path) -> None:
    db = _langgraph_db(tmp_path)
    _stamp_core_head(db, CURRENT_MANIFEST_HEAD)
    adapter = SQLiteCheckpointAdapter(str(db))
    try:
        assert adapter.core_head() == CURRENT_MANIFEST_HEAD
        assert core_schema_fingerprint(adapter._connection) == CURRENT_MANIFEST_HEAD
    finally:
        adapter.close()


def test_core_head_absent_returns_none(tmp_path: Path) -> None:
    db = _langgraph_db(tmp_path)
    adapter = SQLiteCheckpointAdapter(str(db))
    try:
        assert adapter.core_head() is None
    finally:
        adapter.close()


def test_connection_error_is_not_core_drift(tmp_path: Path) -> None:
    db = _langgraph_db(tmp_path)
    _stamp_core_head(db, CURRENT_MANIFEST_HEAD)
    adapter = SQLiteCheckpointAdapter(str(db))
    adapter.close()  # broken connection: must read as "no head", not drift
    assert core_schema_fingerprint(adapter._connection) is None
    assert compare_core_schema(None, CURRENT_MANIFEST_HEAD) == "warning:db_head_missing"


def test_core_manifest_head_reads_local_chain() -> None:
    # Read-only walk of the repo's migrations/versions (no migration run).
    assert core_manifest_head() == CURRENT_MANIFEST_HEAD


def test_core_mismatch_is_warning_only_and_never_disables(tmp_path: Path) -> None:
    db = _langgraph_db(tmp_path)
    _stamp_core_head(db, "some-other-revision")
    golden = _golden_for(db, tmp_path / "golden.json")
    adapter = SQLiteCheckpointAdapter(str(db))
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))
    reconciler = CheckpointReconciler(
        adapter,
        store,
        golden_path=golden,
        core_manifest_head=CURRENT_MANIFEST_HEAD,
    )

    report = reconciler.run(apply=True, now=NOW)

    assert report.core_schema == (
        "warning:db_head_mismatch:db=some-other-revision,manifest=f4a9c2e71b08"
    )
    # Warning only: the lifecycle still applies (t1/cp1 was backfilled).
    assert report.applied is True
    assert report.backfilled == 1
    adapter.close()
    store.close()


def test_core_head_missing_is_warning_only(tmp_path: Path) -> None:
    db = _langgraph_db(tmp_path)  # no alembic_version at all
    golden = _golden_for(db, tmp_path / "golden.json")
    adapter = SQLiteCheckpointAdapter(str(db))
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))
    reconciler = CheckpointReconciler(
        adapter, store, golden_path=golden, core_manifest_head=CURRENT_MANIFEST_HEAD
    )

    report = reconciler.run(apply=True, now=NOW)

    assert report.core_schema == "warning:db_head_missing"
    assert report.applied is True
    assert report.backfilled == 1
    adapter.close()
    store.close()


def test_manifest_unavailable_is_warning_only(tmp_path: Path) -> None:
    db = _langgraph_db(tmp_path)
    _stamp_core_head(db, CURRENT_MANIFEST_HEAD)
    golden = _golden_for(db, tmp_path / "golden.json")
    adapter = SQLiteCheckpointAdapter(str(db))
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))
    reconciler = CheckpointReconciler(adapter, store, golden_path=golden, core_manifest_head=None)

    report = reconciler.run(apply=True, now=NOW)

    assert report.core_schema == "warning:manifest_unavailable"
    assert report.applied is True
    adapter.close()
    store.close()


def test_matching_heads_report_match(tmp_path: Path) -> None:
    db = _langgraph_db(tmp_path)
    _stamp_core_head(db, CURRENT_MANIFEST_HEAD)
    golden = _golden_for(db, tmp_path / "golden.json")
    adapter = SQLiteCheckpointAdapter(str(db))
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))
    reconciler = CheckpointReconciler(
        adapter, store, golden_path=golden, core_manifest_head=CURRENT_MANIFEST_HEAD
    )

    report = reconciler.run(apply=True, now=NOW)

    assert report.core_schema == "match"
    adapter.close()
    store.close()


def test_orphan_purge_still_blocked_by_rate_with_core_warning(tmp_path: Path) -> None:
    """The core warning must not loosen any purge guard."""
    db = _langgraph_db(tmp_path)  # t1/cp1 already present
    _stamp_core_head(db, "some-other-revision")
    # 90 covered checkpoints (t1..t90) + 2 orphans -> ~1.1% anomaly rate.
    conn = sqlite3.connect(db)
    for t in range(2, 91):
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
            "type, checkpoint, metadata) VALUES (?, '', 'cp1', NULL, 'json', '{}', '{}')",
            (f"t{t}",),
        )
    conn.commit()
    conn.close()
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))
    for t in range(1, 91):
        store.upsert(CheckpointRecord(f"t{t}", "cp1", NOW - timedelta(days=30)))
    store.upsert(CheckpointRecord("tx", "cx", NOW - timedelta(hours=48)))
    store.upsert(CheckpointRecord("ty", "cy", NOW - timedelta(hours=48)))
    golden = _golden_for(db, tmp_path / "golden.json")
    adapter = SQLiteCheckpointAdapter(str(db))
    reconciler = CheckpointReconciler(
        adapter, store, golden_path=golden, core_manifest_head=CURRENT_MANIFEST_HEAD
    )

    report = reconciler.run(apply=True, now=NOW)

    assert report.core_schema.startswith("warning:db_head_mismatch")
    assert report.purged == 0
    adapter.close()
    store.close()
