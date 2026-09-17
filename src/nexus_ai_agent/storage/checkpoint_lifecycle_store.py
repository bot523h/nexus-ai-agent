"""Durable lifecycle index for safe checkpoint retention decisions."""

from __future__ import annotations

import sqlite3
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
        self._connection = sqlite3.connect(path)
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

    def delete_index(self, record: CheckpointRecord) -> None:
        """Remove only the lifecycle row, after an adapter deletes checkpoint data."""
        self._connection.execute(
            f"DELETE FROM {_TABLE} WHERE thread_id = ? AND checkpoint_id = ?",
            (record.thread_id, record.checkpoint_id),
        )
        self._connection.commit()


def _key(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _parse_required(value: str) -> datetime:
    return datetime.fromisoformat(value)
