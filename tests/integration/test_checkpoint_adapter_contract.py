"""PR3 C1 — shared contract for the read-only checkpoint adapters.

The same behavioural contract must hold for the SQLite adapter and the
Postgres adapter (PR3): same public surface, same read-only guarantees,
same fail-safe semantics.  The PG leg runs against a real PostgreSQL
instance (``NEXUS_DATABASE_URL``), which CI's ``migrate-postgres`` job
provides via the pgvector:pg16 service container.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid

import psycopg
import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from nexus_ai_agent.storage.checkpoint_adapter import (
    FINGERPRINT_ALGORITHM,
    POST_V1_DELETE_MARKER,
    CheckpointInfo,
    CheckpointReadAdapter,
    CleanupDisabled,
    SQLiteCheckpointAdapter,
)
from nexus_ai_agent.storage.checkpoint_pg_adapter import (
    DEFAULT_PG_GOLDEN,
    PostgresCheckpointAdapter,
)
from nexus_ai_agent.storage.checkpoint_pg_adapter import (
    FINGERPRINT_ALGORITHM as PG_FINGERPRINT_ALGORITHM,
)

PG_URL = os.getenv("NEXUS_DATABASE_URL")
requires_pg = pytest.mark.skipif(not PG_URL, reason="requires PostgreSQL")

# One id namespace per test session so re-runs against a persistent PG
# database never collide with earlier seeds.
_SESSION = uuid.uuid4().hex[:8]
THREAD_A = f"contract-a-{_SESSION}"  # good 3-checkpoint chain
THREAD_B = f"contract-b-{_SESSION}"  # dangling parent (broken lineage)


def _seed_sqlite(path) -> None:
    conn = sqlite3.connect(path)
    SqliteSaver(conn).setup()
    for thread, parent in ((THREAD_A, None), (THREAD_B, "ghost-parent")):
        rows = (
            [("cp1", parent), ("cp2", "cp1"), ("cp3", "cp2")]
            if thread == THREAD_A
            else [("cp1", parent)]
        )
        for checkpoint_id, par in rows:
            conn.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                "type, checkpoint, metadata) VALUES (?, '', ?, ?, 'json', '{}', '{}')",
                (thread, checkpoint_id, par),
            )
    conn.execute(
        "INSERT INTO writes "
        "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
        "VALUES (?, '', 'cp1', 'task-1', 0, 'channel_a', 'json', X'0001')",
        (THREAD_A,),
    )
    conn.commit()
    conn.close()


def _seed_pg(url: str) -> None:
    setup = psycopg.connect(url, autocommit=True)
    from langgraph.checkpoint.postgres import PostgresSaver

    PostgresSaver(setup).setup()  # CREATE TABLE IF NOT EXISTS — idempotent
    rows = [
        (THREAD_A, "cp1", None),
        (THREAD_A, "cp2", "cp1"),
        (THREAD_A, "cp3", "cp2"),
        (THREAD_B, "cp1", "ghost-parent"),
    ]
    for thread, checkpoint_id, parent in rows:
        setup.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
            "type, checkpoint, metadata) "
            "VALUES (%s, '', %s, %s, 'json', '{}'::jsonb, '{}'::jsonb)",
            (thread, checkpoint_id, parent),
        )
    setup.execute(
        "INSERT INTO checkpoint_writes "
        "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob) "
        "VALUES (%s, '', 'cp1', 'task-1', 0, 'channel_a', 'json', '\\x0001'::bytea)",
        (THREAD_A,),
    )
    setup.close()


# ── fixtures ──────────────────────────────────────────────────────────


_PG_SEEDED = False


def _ensure_pg_seeded() -> None:
    """Seed the shared PG test database exactly once per session."""
    global _PG_SEEDED
    if not _PG_SEEDED:
        _seed_pg(PG_URL)  # type: ignore[arg-type]
        _PG_SEEDED = True


@pytest.fixture(params=["sqlite", "pg"])
def adapter(request, tmp_path) -> CheckpointReadAdapter:
    """One contract, two backends (the PG leg skips without a database)."""
    if request.param == "sqlite":
        path = tmp_path / "contract.sqlite"
        _seed_sqlite(path)
        adapter: CheckpointReadAdapter = SQLiteCheckpointAdapter(str(path))
    else:
        if not PG_URL:
            pytest.skip("requires PostgreSQL (NEXUS_DATABASE_URL)")
        _ensure_pg_seeded()
        adapter = PostgresCheckpointAdapter(PG_URL)  # type: ignore[arg-type]
    try:
        yield adapter
    finally:
        adapter.close()


# ── shared contract ─────────────────────────────────────────────────────


def test_is_read_adapter_protocol(adapter: CheckpointReadAdapter) -> None:
    assert isinstance(adapter, CheckpointReadAdapter)


def test_list_threads_sorted(adapter: CheckpointReadAdapter) -> None:
    threads = adapter.list_threads()
    assert THREAD_A in threads and THREAD_B in threads
    assert threads == sorted(threads)


def test_list_checkpoints_shape_and_order(adapter: CheckpointReadAdapter) -> None:
    chain = adapter.list_checkpoints(THREAD_A)
    assert [item.checkpoint_id for item in chain] == ["cp1", "cp2", "cp3"]
    assert all(isinstance(item, CheckpointInfo) for item in chain)
    assert chain[0].parent_checkpoint_id is None
    assert chain[1].parent_checkpoint_id == "cp1"
    assert chain[2].parent_checkpoint_id == "cp2"

    broken = adapter.list_checkpoints(THREAD_B)
    assert [item.checkpoint_id for item in broken] == ["cp1"]
    assert broken[0].parent_checkpoint_id == "ghost-parent"


def test_exists_checkpoint(adapter: CheckpointReadAdapter) -> None:
    assert adapter.exists_checkpoint(THREAD_A, "cp1")
    assert not adapter.exists_checkpoint(THREAD_A, "nope")
    assert not adapter.exists_checkpoint("no-thread", "cp1")


def test_estimate_thread_bytes_positive_int(adapter: CheckpointReadAdapter) -> None:
    # The value is an estimate (labelled _estimate at the call site); the
    # contract is: int, positive for a seeded thread, zero for a ghost.
    estimate = adapter.estimate_thread_bytes(THREAD_A)
    assert isinstance(estimate, int)
    assert estimate > 0
    assert adapter.estimate_thread_bytes("no-thread") == 0


def test_verify_lineage_good_and_broken(adapter: CheckpointReadAdapter) -> None:
    assert adapter.verify_lineage(THREAD_A) is True
    assert adapter.verify_lineage(THREAD_B) is False


def test_post_v1_delete_guards_raise(adapter: CheckpointReadAdapter) -> None:
    with pytest.raises(NotImplementedError, match=POST_V1_DELETE_MARKER):
        adapter.delete_checkpoint("cp1")
    with pytest.raises(NotImplementedError, match=POST_V1_DELETE_MARKER):
        adapter.delete_thread(THREAD_A)


def test_schema_fingerprint_deterministic(adapter: CheckpointReadAdapter) -> None:
    first = adapter.schema_fingerprint()
    assert first == adapter.schema_fingerprint()
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first)


def test_assert_golden_semantics(tmp_path, adapter: CheckpointReadAdapter) -> None:
    algorithm = (
        PG_FINGERPRINT_ALGORITHM
        if isinstance(adapter, PostgresCheckpointAdapter)
        else FINGERPRINT_ALGORITHM
    )
    actual = adapter.schema_fingerprint()

    wrong_fp = tmp_path / "wrong.json"
    wrong_fp.write_text(
        json.dumps({"algorithm": algorithm, "fingerprint": "0" * 64}), encoding="utf-8"
    )
    with pytest.raises(CleanupDisabled, match="fingerprint mismatch"):
        adapter.assert_golden(wrong_fp)

    wrong_algo = tmp_path / "algo.json"
    wrong_algo.write_text(
        json.dumps({"algorithm": "not-a-real-algorithm", "fingerprint": actual}),
        encoding="utf-8",
    )
    with pytest.raises(CleanupDisabled, match="unsupported"):
        adapter.assert_golden(wrong_algo)

    with pytest.raises(FileNotFoundError):
        adapter.assert_golden(tmp_path / "missing.json")

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"algorithm": algorithm, "fingerprint": actual}), encoding="utf-8")
    adapter.assert_golden(good)  # no raise


def test_core_head_agrees_with_source_of_truth(adapter: CheckpointReadAdapter) -> None:
    """The adapter must report exactly what ``alembic_version`` holds (or None)."""
    if isinstance(adapter, PostgresCheckpointAdapter):
        conn = psycopg.connect(adapter.database_url, autocommit=True)
        try:
            exists = conn.execute(
                "SELECT to_regclass('public.alembic_version') IS NOT NULL"
            ).fetchone()[0]
            expected: str | None = None
            if exists:
                row = conn.execute(
                    "SELECT version_num FROM alembic_version ORDER BY version_num"
                ).fetchone()
                expected = str(row[0]) if row else None
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(adapter.path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='alembic_version'"
            ).fetchone()
            expected = None
            if exists is not None:
                row = conn.execute(
                    "SELECT version_num FROM alembic_version ORDER BY version_num"
                ).fetchone()
                expected = str(row[0]) if row else None
        finally:
            conn.close()
    assert adapter.core_head() == expected


def test_sqlite_core_head_absent_and_stamped(tmp_path) -> None:
    """SQLite leg can control the alembic state deterministically."""
    path = tmp_path / "core.sqlite"
    _seed_sqlite(path)
    adapter = SQLiteCheckpointAdapter(str(path))
    try:
        assert adapter.core_head() is None  # no alembic_version yet
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version (version_num) VALUES ('abcd1234')")
        conn.commit()
        conn.close()
        assert adapter.core_head() == "abcd1234"
    finally:
        adapter.close()


# ── PG-specific guarantees (server-enforced read-only) ──────────────────


@requires_pg
def test_pg_connection_refuses_dml() -> None:
    adapter = PostgresCheckpointAdapter(PG_URL)  # type: ignore[arg-type]
    try:
        with pytest.raises(psycopg.Error) as excinfo:
            adapter._connection.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                "VALUES ('rogue', '', 'cp', '{}'::jsonb, '{}'::jsonb)"
            )
        assert excinfo.value.sqlstate == "25006"  # readonly_sql_transaction
    finally:
        adapter.close()


@requires_pg
def test_pg_dead_connection_fails_safe_at_construction() -> None:
    """I12 for Postgres: a dead connection is a construction-time error.

    The adapter connects eagerly (like the SQLite adapter opening its
    file): an unreachable database raises at construction, before any
    query and before any mutation — it is never interpreted as drift.
    """
    with pytest.raises(psycopg.OperationalError):
        PostgresCheckpointAdapter("postgresql://nexus:nexus@127.0.0.1:59999/unreachable")


@requires_pg
def test_committed_postgres_golden_matches_pg16_in_ci() -> None:
    """The committed postgres.langgraph.json must match the running engine.

    Generated on PostgreSQL 18; this test proves the fingerprint payload is
    stable across engine versions (CI runs pgvector:pg16).
    """
    adapter = PostgresCheckpointAdapter(PG_URL)  # type: ignore[arg-type]
    try:
        adapter.assert_golden(DEFAULT_PG_GOLDEN)
    finally:
        adapter.close()


# ── PR3 option A: the lifecycle table + PG store contract ───────────────


@requires_pg
def test_postgres_lifecycle_table_created_by_migration() -> None:
    """f4a9c2e71b08 creates exactly nexus_checkpoint_lifecycle, exact shape."""
    from nexus_ai_agent.storage.migrations import run_migrations

    run_migrations()  # idempotent: already at head in CI
    conn = psycopg.connect(PG_URL, autocommit=True)
    try:
        cols = conn.execute(
            "SELECT column_name::text, is_nullable::text "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "  AND table_name = 'nexus_checkpoint_lifecycle' "
            "ORDER BY ordinal_position"
        ).fetchall()
        assert [c[0] for c in cols] == [
            "thread_id",
            "checkpoint_id",
            "created_at",
            "last_accessed_at",
            "active_until",
        ]
        assert [c[1] for c in cols] == ["NO", "NO", "NO", "YES", "YES"]
        pk = conn.execute(
            "SELECT kcu.column_name::text "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            " AND tc.table_schema = kcu.table_schema "
            "WHERE tc.table_schema = 'public' "
            "  AND tc.table_name = 'nexus_checkpoint_lifecycle' "
            "  AND tc.constraint_type = 'PRIMARY KEY' "
            "ORDER BY kcu.ordinal_position"
        ).fetchall()
        assert [r[0] for r in pk] == ["thread_id", "checkpoint_id"]
    finally:
        conn.close()


@requires_pg
def test_postgres_lifecycle_store_roundtrip_and_read_only() -> None:
    """Same store contract as SQLite: roundtrip, touch, read-only server-side."""
    from datetime import datetime, timezone

    from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
    from nexus_ai_agent.storage.checkpoint_lifecycle_pg_store import (
        PostgresCheckpointLifecycleStore,
    )
    from nexus_ai_agent.storage.migrations import run_migrations

    run_migrations()
    thread_id = f"lifecycle-{_SESSION}"
    record = CheckpointRecord(thread_id, "cp1", datetime(2026, 1, 1, tzinfo=timezone.utc))

    store = PostgresCheckpointLifecycleStore(PG_URL)
    try:
        store.upsert(record)
        rows = [r for r in store.records() if r.thread_id == thread_id]
        assert [(r.thread_id, r.checkpoint_id) for r in rows] == [(thread_id, "cp1")]
        assert rows[0].created_at == record.created_at
        assert rows[0].last_accessed_at is None

        touched_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
        assert store.touch_thread(thread_id, touched_at) is True
        rows = [r for r in store.records() if r.thread_id == thread_id]
        assert rows[0].last_accessed_at == touched_at
        # touch never creates: unknown thread → False, no row
        assert store.touch_thread("no-such-thread", touched_at) is False

        # server-enforced read-only (SQLSTATE 25006)
        ro = PostgresCheckpointLifecycleStore(PG_URL, read_only=True)
        try:
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                ro.upsert(record)
            assert ro.records()  # reads still work
        finally:
            ro.close()

        again = PostgresCheckpointLifecycleStore(PG_URL)
        try:
            assert again.schema_fingerprint() == store.schema_fingerprint()
        finally:
            again.close()

        store.delete_index(record)
        assert [r for r in store.records() if r.thread_id == thread_id] == []
    finally:
        store.close()
