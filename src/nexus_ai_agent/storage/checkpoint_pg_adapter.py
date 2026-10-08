"""Read-only PostgreSQL checkpoint adapter (PR3).

Mirrors the public surface of :class:`SQLiteCheckpointAdapter` over the
LangGraph Postgres schema (``langgraph-checkpoint-postgres``:
``checkpoints``, ``checkpoint_blobs``, ``checkpoint_writes``,
``checkpoint_migrations``).

Read-only is enforced by the *database*, not by code review: the
connection is opened with ``default_transaction_read_only=on``, so Postgres
itself refuses any DML (SQLSTATE 25006) even if a future caller forgets.

The connection-error contract matches the SQLite adapter: a dead
connection is never interpreted as schema drift (``core_head`` degrades to
``None``); other operations propagate and are treated as scan errors by
the reconciler's health gate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from nexus_ai_agent.optional_deps import require
from nexus_ai_agent.storage.checkpoint_adapter import (
    POST_V1_DELETE_MARKER,
    CheckpointInfo,
    CleanupDisabled,
)

# PG-only module: fail closed with the install command instead of a bare
# ModuleNotFoundError when the [postgres] extra is not installed.
psycopg: Any = require("psycopg")

# Fingerprint algorithm for the Postgres langgraph schema (distinct from the
# SQLite one: different engine, tables, and types).
FINGERPRINT_ALGORITHM: Final[str] = "schema-v1-pg"

# The exact LangGraph-owned tables the fingerprint covers.  The Postgres
# database also holds the application schema (Alembic), which must never
# leak into the langgraph-scope fingerprint.
LANGGRAPH_TABLES: Final[tuple[str, ...]] = (
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
)

#: Default golden for the Postgres langgraph schema.
DEFAULT_PG_GOLDEN = Path(__file__).parent / "golden" / "postgres.langgraph.json"

_COLUMNS_QUERY = """
    SELECT column_name::text, data_type::text, udt_name::text,
           is_nullable::text, column_default::text
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = %s
    ORDER BY ordinal_position
"""

_PK_QUERY = """
    SELECT kcu.column_name::text
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
     AND tc.table_schema = kcu.table_schema
    WHERE tc.table_schema = 'public' AND tc.table_name = %s
      AND tc.constraint_type = 'PRIMARY KEY'
    ORDER BY kcu.ordinal_position
"""

_INDEXES_QUERY = """
    SELECT indexname::text FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = %s
    ORDER BY indexname
"""


class PostgresCheckpointAdapter:
    """Read-only adapter; destructive operations are intentionally absent.

    Satisfies the same structural contract as ``SQLiteCheckpointAdapter``
    (see ``CheckpointReadAdapter``) so the reconciler and inspect CLI are
    backend-agnostic.
    """

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._connection = psycopg.connect(
            database_url,
            autocommit=True,
            options="-c default_transaction_read_only=on",
        )

    def close(self) -> None:
        try:
            self._connection.close()
        except psycopg.Error:
            # Closing an already-broken connection must not mask errors.
            pass

    # ── read-only queries (same surface as the SQLite adapter) ──────────

    def _has_table(self, name: str) -> bool:
        row = self._connection.execute(
            "SELECT to_regclass('public.' || %s) IS NOT NULL", (name,)
        ).fetchone()
        return row is not None and bool(row[0])

    def list_threads(self) -> list[str]:
        if not self._has_table("checkpoints"):
            return []
        rows = self._connection.execute(
            "SELECT thread_id FROM checkpoints GROUP BY thread_id ORDER BY thread_id"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def list_checkpoints(self, thread_id: str) -> list[CheckpointInfo]:
        if not self._has_table("checkpoints"):
            return []
        rows = self._connection.execute(
            "SELECT thread_id, checkpoint_id, parent_checkpoint_id "
            "FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id",
            (thread_id,),
        ).fetchall()
        return [CheckpointInfo(row[0], row[1], row[2]) for row in rows]

    def exists_checkpoint(self, thread_id: str, checkpoint_id: str) -> bool:
        """Read-only existence check (re-verify orphans right before a purge)."""
        if not self._has_table("checkpoints"):
            return False
        row = self._connection.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = %s AND checkpoint_id = %s LIMIT 1",
            (thread_id, checkpoint_id),
        ).fetchone()
        return row is not None

    def estimate_thread_bytes(self, thread_id: str) -> int:
        """Read-only size estimate for a thread's checkpoint data.

        Sums ``pg_column_size`` over the stored payloads across the
        LangGraph tables that exist.  Callers must label the result with
        the ``_estimate`` suffix; it is an approximation, not an
        authoritative disk accounting.
        """
        total = 0
        if self._has_table("checkpoints"):
            row = self._connection.execute(
                "SELECT COALESCE(SUM(pg_column_size(checkpoint) "
                "+ pg_column_size(metadata)), 0) "
                "FROM checkpoints WHERE thread_id = %s",
                (thread_id,),
            ).fetchone()
            if row is not None:
                total += int(row[0])
        if self._has_table("checkpoint_writes"):
            row = self._connection.execute(
                "SELECT COALESCE(SUM(pg_column_size(blob)), 0) "
                "FROM checkpoint_writes WHERE thread_id = %s",
                (thread_id,),
            ).fetchone()
            if row is not None:
                total += int(row[0])
        if self._has_table("checkpoint_blobs"):
            row = self._connection.execute(
                "SELECT COALESCE(SUM(pg_column_size(blob)), 0) "
                "FROM checkpoint_blobs WHERE thread_id = %s",
                (thread_id,),
            ).fetchone()
            if row is not None:
                total += int(row[0])
        return total

    def get_blob_refs(self, thread_id: str) -> list[tuple[str, str]]:
        if not self._has_table("checkpoint_blobs"):
            return []
        rows = self._connection.execute(
            "SELECT channel, version FROM checkpoint_blobs "
            "WHERE thread_id = %s ORDER BY channel, version",
            (thread_id,),
        ).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    def verify_lineage(self, thread_id: str) -> bool:
        """Verify that every parent is in the same thread and acyclic."""
        checkpoints = self.list_checkpoints(thread_id)
        by_id = {item.checkpoint_id: item for item in checkpoints}
        for item in checkpoints:
            seen: set[str] = set()
            parent = item.parent_checkpoint_id
            while parent is not None:
                if parent in seen or parent not in by_id:
                    return False
                seen.add(parent)
                parent = by_id[parent].parent_checkpoint_id
        return True

    # ── POST_V1 guards (identical contract to the SQLite adapter) ──────

    def delete_checkpoint(self, checkpoint_id: str) -> None:
        """POST_V1: delta-chain surgery is forbidden."""
        raise NotImplementedError(f"{POST_V1_DELETE_MARKER}: checkpoint deletion is disabled")

    def delete_thread(self, thread_id: str) -> None:
        """POST_V1: mutation is deferred until the destructive PR."""
        raise NotImplementedError(f"{POST_V1_DELETE_MARKER}: thread deletion is not in PR1")

    # ── schema safety ──────────────────────────────────────────────────

    def schema_fingerprint(self) -> str:
        payload = _schema_payload(self._connection)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def assert_golden(self, golden_path: str | Path) -> None:
        expected = json.loads(Path(golden_path).read_text(encoding="utf-8"))
        if expected.get("algorithm") != FINGERPRINT_ALGORITHM:
            raise CleanupDisabled("unsupported schema fingerprint algorithm")
        actual = self.schema_fingerprint()
        if actual != expected.get("fingerprint"):
            raise CleanupDisabled(
                "checkpoint cleanup disabled: schema fingerprint mismatch "
                f"expected={expected.get('fingerprint')} actual={actual}"
            )

    def core_head(self) -> str | None:
        """Core migration head from ``alembic_version`` (read-only, R7).

        A dead or unreachable connection degrades to ``None``: a
        connection error is *not* schema drift (I12).  Only a reachable
        database that lacks the table yields the same ``None``, which the
        reconciler reports as ``warning:db_head_missing``.
        """
        try:
            if not self._has_table("alembic_version"):
                return None
            row = self._connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            ).fetchone()
            return None if row is None else str(row[0])
        except psycopg.Error:
            return None


def _schema_payload(connection: psycopg.Connection) -> dict[str, object]:
    """Deterministic catalog payload over the LangGraph tables only."""
    table_payload: list[dict[str, object]] = []
    for name in LANGGRAPH_TABLES:
        if not _table_exists(connection, name):
            continue
        columns = [list(row) for row in connection.execute(_COLUMNS_QUERY, (name,)).fetchall()]
        primary_key = [row[0] for row in connection.execute(_PK_QUERY, (name,))]
        indexes = [row[0] for row in connection.execute(_INDEXES_QUERY, (name,))]
        table_payload.append(
            {
                "name": name,
                "columns": columns,
                "primary_key": primary_key,
                "indexes": indexes,
            }
        )
    result: dict[str, object] = {
        "algorithm": FINGERPRINT_ALGORITHM,
        "tables": table_payload,
        "migration_head": _migration_head(connection),
        "capabilities": {"postgres": True},
    }
    return result


def _table_exists(connection: psycopg.Connection, name: str) -> bool:
    row = connection.execute("SELECT to_regclass('public.' || %s) IS NOT NULL", (name,)).fetchone()
    return row is not None and bool(row[0])


def _migration_head(connection: psycopg.Connection) -> str | None:
    if not _table_exists(connection, "alembic_version"):
        return None
    row = connection.execute(
        "SELECT version_num FROM alembic_version ORDER BY version_num"
    ).fetchone()
    return None if row is None else str(row[0])
