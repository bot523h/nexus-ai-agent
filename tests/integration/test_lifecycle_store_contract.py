"""PR3 C1 — shared contract for the lifecycle index stores (option A).

The same behavioural contract must hold for
``SQLiteCheckpointLifecycleStore`` and ``PostgresCheckpointLifecycleStore``:
same interface, same semantics (upsert idempotency per key, touch updates
but never creates, scoped deletion, deterministic fingerprint).  The PG
leg runs against a real PostgreSQL instance (``NEXUS_DATABASE_URL``),
which CI's ``migrate-postgres`` job provides.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_store import (
    SQLiteCheckpointLifecycleStore,
)

PG_URL = os.getenv("NEXUS_DATABASE_URL")

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        s = SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.lifecycle"))
    else:
        from nexus_ai_agent.storage.checkpoint_lifecycle_pg_store import (
            PostgresCheckpointLifecycleStore,
        )
        from nexus_ai_agent.storage.migrations import run_migrations

        run_migrations()  # ensures nexus_checkpoint_lifecycle exists
        s = PostgresCheckpointLifecycleStore(PG_URL)  # type: ignore[arg-type]
    yield s
    s.close()


def _row_for(store, thread_id: str) -> CheckpointRecord | None:
    rows = [r for r in store.records() if r.thread_id == thread_id]
    return rows[0] if rows else None


def test_upsert_and_roundtrip(store) -> None:
    thread_id = uuid.uuid4().hex
    record = CheckpointRecord(thread_id, "cp1", NOW)
    store.upsert(record)
    row = _row_for(store, thread_id)
    assert row is not None
    assert row.checkpoint_id == "cp1"
    assert row.created_at == NOW
    assert row.last_accessed_at is None
    assert row.active_until is None


def test_upsert_is_idempotent_per_key(store) -> None:
    thread_id = uuid.uuid4().hex
    store.upsert(CheckpointRecord(thread_id, "cp1", NOW))
    store.upsert(CheckpointRecord(thread_id, "cp1", NOW, last_accessed_at=LATER))
    rows = [r for r in store.records() if r.thread_id == thread_id]
    assert len(rows) == 1
    # upsert refreshes the mutable fields, keeps created_at
    assert rows[0].created_at == NOW
    assert rows[0].last_accessed_at == LATER


def test_touch_updates_known_and_never_creates(store) -> None:
    thread_id = uuid.uuid4().hex
    store.upsert(CheckpointRecord(thread_id, "cp1", NOW))
    assert store.touch_thread(thread_id, LATER) is True
    assert _row_for(store, thread_id).last_accessed_at == LATER
    # unknown thread: no update AND no creation (backfill is the
    # reconciler's explicit job, with a protected estimated age)
    unknown = uuid.uuid4().hex
    assert store.touch_thread(unknown, LATER) is False
    assert _row_for(store, unknown) is None


def test_delete_index_is_scoped(store) -> None:
    keep = uuid.uuid4().hex
    drop = uuid.uuid4().hex
    store.upsert(CheckpointRecord(keep, "cp1", NOW))
    store.upsert(CheckpointRecord(drop, "cp1", NOW))
    store.delete_index(CheckpointRecord(drop, "cp1", NOW))
    assert _row_for(store, drop) is None
    assert _row_for(store, keep) is not None


def test_schema_fingerprint_is_deterministic(store, tmp_path: Path) -> None:
    first = store.schema_fingerprint()
    assert len(first) == 64
    assert store.schema_fingerprint() == first


@pytest.mark.skipif(not PG_URL, reason="requires PostgreSQL")
def test_postgres_read_only_store_is_server_enforced() -> None:
    """read_only=True is enforced by the server (SQLSTATE 25006)."""
    import psycopg

    from nexus_ai_agent.storage.checkpoint_lifecycle_pg_store import (
        PostgresCheckpointLifecycleStore,
    )
    from nexus_ai_agent.storage.migrations import run_migrations

    run_migrations()
    ro = PostgresCheckpointLifecycleStore(PG_URL, read_only=True)
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro.upsert(CheckpointRecord("ro-test", "cp1", NOW))
        # reads keep working
        assert isinstance(ro.records(), list)
    finally:
        ro.close()


def test_postgres_store_never_issues_ddl() -> None:
    """The store assumes the migration created the table; it never DDLs.

    Every string literal that could be a SQL statement must be DML.
    """
    import ast
    import importlib

    module = importlib.import_module("nexus_ai_agent.storage.checkpoint_lifecycle_pg_store")
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            head = node.value.strip().split(None, 1)[0].upper()
            assert head not in {"CREATE", "DROP", "ALTER", "TRUNCATE"}, node.value
