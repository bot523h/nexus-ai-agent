"""PostgreSQL lifecycle index store (PR3 option A).

The same lifecycle index as :class:`SQLiteCheckpointLifecycleStore`,
materialized in PostgreSQL by the explicit, isolated Alembic revision
``f4a9c2e71b08`` (table ``nexus_checkpoint_lifecycle``).

Design notes:

* **No DDL here.**  The table is owned by the migration; this store never
  issues ``CREATE TABLE`` (no implicit repair).  A database that has not
  been migrated simply fails its lifecycle operations, which the
  recording saver contains (best-effort) and the CLI surfaces as a
  health-gate error.
* **Lazy connection.**  Construction never touches the network, so a
  PostgreSQL outage cannot break process boot — the same posture as the
  lazy :class:`PostgresCheckpointer`.
* **Read-only mode (CLI).**  ``read_only=True`` opens the connection with
  ``default_transaction_read_only=on``: the *server* enforces it
  (SQLSTATE 25006), the same guarantee as the read-only checkpoint
  adapter.  The runtime saver uses the writable default.
* **Serialized access.**  A psycopg connection is not safe for concurrent
  cursor use from the ``asyncio.to_thread`` workers, so operations are
  serialized with a lock (the SQLite store gets the same serialization
  from the sqlite3 module's GIL-bound behaviour).
* ``path`` is a deterministic temp-dir anchor for the reconciler cleanup
  lock, derived from the (full) URL so two processes on one host sharing
  a database serialize against each other.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from nexus_ai_agent.optional_deps import require
from nexus_ai_agent.storage.checkpoint_lifecycle import (
    LIFECYCLE_TABLE_NAME,
    CheckpointRecord,
)
from nexus_ai_agent.storage.checkpoint_lifecycle_store import (
    _key,
    _parse,
    _parse_required,
)

# PG-only module: fail closed with the install command instead of a bare
# ModuleNotFoundError when the [postgres] extra is not installed.
psycopg: Any = require("psycopg")

_TABLE = LIFECYCLE_TABLE_NAME  # "nexus_checkpoint_lifecycle"

_UPSERT = f"""
    INSERT INTO {_TABLE}
    (thread_id, checkpoint_id, created_at, last_accessed_at, active_until)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (thread_id, checkpoint_id) DO UPDATE SET
      last_accessed_at = EXCLUDED.last_accessed_at,
      active_until = EXCLUDED.active_until
"""


class PostgresCheckpointLifecycleStore:
    """Lifecycle index in PostgreSQL; satisfies :class:`LifecycleStore`."""

    #: Local cleanup-lock anchor (temp dir); ``str | Path`` for
    #: LifecycleStore invariance.  Never the database itself.
    path: str | Path

    def __init__(self, database_url: str, *, read_only: bool = False) -> None:
        self.database_url = database_url
        self._read_only = read_only
        self._connection: psycopg.Connection | None = None
        self._lock = threading.Lock()
        # Host-scoped cleanup-lock anchor (never the database itself).
        digest = hashlib.sha256(database_url.encode("utf-8")).hexdigest()[:16]
        self.path = Path(tempfile.gettempdir()) / f"nexus-lifecycle-pg-{digest}"

    # ── connection ──────────────────────────────────────────────────────

    def _connect(self) -> psycopg.Connection:
        if self._connection is None:
            options = "-c default_transaction_read_only=on" if self._read_only else None
            self._connection = psycopg.connect(self.database_url, autocommit=True, options=options)
        return self._connection

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._connect().execute(sql, params)

    def _fetchall(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            return self._connect().execute(sql, params).fetchall()

    def _rowcount(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cursor = self._connect().execute(sql, params)
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            try:
                if self._connection is not None:
                    self._connection.close()
            except psycopg.Error:
                # Closing an already-broken connection must not mask errors.
                pass
            self._connection = None

    # ── LifecycleStore ──────────────────────────────────────────────────

    def upsert(self, record: CheckpointRecord) -> None:
        values = (_key(record.created_at), _key(record.last_accessed_at), _key(record.active_until))
        self._execute(_UPSERT, (record.thread_id, record.checkpoint_id, *values))

    def records(self) -> list[CheckpointRecord]:
        rows = self._fetchall(
            f"SELECT thread_id, checkpoint_id, created_at, last_accessed_at, "
            f"active_until FROM {_TABLE}"
        )
        return [_record(row) for row in rows]

    def touch_thread(self, thread_id: str, accessed_at: datetime) -> bool:
        rowcount = self._rowcount(
            f"UPDATE {_TABLE} SET last_accessed_at = %s WHERE thread_id = %s",
            (_key(accessed_at), thread_id),
        )
        return rowcount > 0

    def delete_index(self, record: CheckpointRecord) -> None:
        self._execute(
            f"DELETE FROM {_TABLE} WHERE thread_id = %s AND checkpoint_id = %s",
            (record.thread_id, record.checkpoint_id),
        )

    def schema_fingerprint(self) -> str:
        """Deterministic fingerprint of the lifecycle table definition."""
        columns = self._fetchall(
            "SELECT column_name::text, data_type::text, is_nullable::text, "
            "column_default::text FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "ORDER BY ordinal_position",
            (LIFECYCLE_TABLE_NAME,),
        )
        primary_key = self._fetchall(
            "SELECT kcu.column_name::text "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            " AND tc.table_schema = kcu.table_schema "
            "WHERE tc.table_schema = 'public' AND tc.table_name = %s "
            "  AND tc.constraint_type = 'PRIMARY KEY' "
            "ORDER BY kcu.ordinal_position",
            (LIFECYCLE_TABLE_NAME,),
        )
        payload = json.dumps(
            {
                "columns": [tuple(row) for row in columns],
                "primary_key": [row[0] for row in primary_key],
            },
            separators=(",", ":"),
            ensure_ascii=True,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record(row: tuple) -> CheckpointRecord:
    return CheckpointRecord(
        thread_id=row[0],
        checkpoint_id=row[1],
        created_at=_parse_required(row[2]),
        last_accessed_at=_parse(row[3]),
        active_until=_parse(row[4]),
    )
