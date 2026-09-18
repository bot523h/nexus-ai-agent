"""Durable lifecycle index for safe checkpoint retention decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord

_TABLE = "nexus_checkpoint_lifecycle"


class SQLiteCheckpointLifecycleStore:
    """Small independent index; it never deletes LangGraph rows by itself."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Cross-thread access is required: the async adapter runs store
        # operations in worker threads (same pattern as the LangGraph saver).
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                thread_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_accessed_at TEXT,
                active_until TEXT,
                PRIMARY KEY (thread_id, checkpoint_id)
            )"""
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def upsert(self, record: CheckpointRecord) -> None:
        values = (_key(record.created_at), _key(record.last_accessed_at), _key(record.active_until))
        self._connection.execute(
            f"""INSERT INTO {_TABLE}
            (thread_id, checkpoint_id, created_at, last_accessed_at, active_until)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(thread_id, checkpoint_id) DO UPDATE SET
              last_accessed_at=excluded.last_accessed_at,
              active_until=excluded.active_until""",
            (record.thread_id, record.checkpoint_id, *values),
        )
        self._connection.commit()

    def records(self) -> list[CheckpointRecord]:
        rows = self._connection.execute(
            f"SELECT thread_id, checkpoint_id, created_at, last_accessed_at, "
            f"active_until FROM {_TABLE}"
        ).fetchall()
        return [
            CheckpointRecord(
                thread_id=row[0],
                checkpoint_id=row[1],
                created_at=_parse_required(row[2]),
                last_accessed_at=_parse(row[3]),
                active_until=_parse(row[4]),
            )
            for row in rows
        ]

    def touch_thread(self, thread_id: str, accessed_at: datetime) -> bool:
        """Update ``last_accessed_at`` on existing rows of one thread.

        Returns ``False`` when the thread is unknown to the index.  Touching
        never creates a record: unknown checkpoints are backfilled by the
        reconciler only, with an explicitly estimated age.
        """
        cursor = self._connection.execute(
            f"UPDATE {_TABLE} SET last_accessed_at = ? WHERE thread_id = ?",
            (_key(accessed_at), thread_id),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def delete_index(self, record: CheckpointRecord) -> None:
        """Remove only the lifecycle row, after an adapter deletes checkpoint data."""
        self._connection.execute(
            f"DELETE FROM {_TABLE} WHERE thread_id = ? AND checkpoint_id = ?",
            (record.thread_id, record.checkpoint_id),
        )
        self._connection.commit()

    def schema_fingerprint(self) -> str:
        """Return a deterministic fingerprint of the lifecycle schema only."""
        tables = self._connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        payload = json.dumps(tables, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@contextmanager
def cleanup_lock(path: str) -> Iterator[None]:
    """Serialize cleanup processes; failure to acquire is fail-safe."""
    import fcntl

    lock_path = Path(path).with_suffix(Path(path).suffix + ".cleanup.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("checkpoint cleanup is already in progress") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _key(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _parse_required(value: str) -> datetime:
    return datetime.fromisoformat(value)
