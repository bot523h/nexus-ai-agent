"""Read-only SQLite checkpoint adapter for Stage 1.

This adapter deliberately stops before mutation.  It understands enough of
LangGraph's SQLite schema to inspect lineage and references, while keeping all
cleanup decisions behind a versioned schema fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FINGERPRINT_ALGORITHM: Final[str] = "schema-v1"
POST_V1_DELETE_MARKER: Final[str] = "POST_V1"


class CleanupDisabled(RuntimeError):
    """Raised when schema safety cannot be proven."""


@dataclass(frozen=True)
class CheckpointInfo:
    thread_id: str
    checkpoint_id: str
    parent_checkpoint_id: str | None


class SQLiteCheckpointAdapter:
    """Read-only adapter; destructive operations are intentionally absent."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self._connection.close()

    def list_threads(self) -> list[str]:
        if not self._has_table("checkpoints"):
            return []
        rows = self._connection.execute(
            "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def list_checkpoints(self, thread_id: str) -> list[CheckpointInfo]:
        if not self._has_table("checkpoints"):
            return []
        rows = self._connection.execute(
            "SELECT thread_id, checkpoint_id, parent_checkpoint_id "
            "FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id",
            (thread_id,),
        ).fetchall()
        return [CheckpointInfo(row[0], row[1], row[2]) for row in rows]

    def get_blob_refs(self, thread_id: str) -> list[tuple[str, str]]:
        if not self._has_table("checkpoint_blobs"):
            return []
        rows = self._connection.execute(
            "SELECT channel, version FROM checkpoint_blobs "
            "WHERE thread_id = ? ORDER BY channel, version",
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

    def delete_checkpoint(self, checkpoint_id: str) -> None:
        """POST_V1: delta-chain surgery is forbidden."""
        raise NotImplementedError(f"{POST_V1_DELETE_MARKER}: checkpoint deletion is disabled")

    def delete_thread(self, thread_id: str) -> None:
        """POST_V1: mutation is deferred until the destructive PR."""
        raise NotImplementedError(f"{POST_V1_DELETE_MARKER}: thread deletion is not in PR1")

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
                f"checkpoint cleanup disabled: schema fingerprint mismatch "
                f"expected={expected.get('fingerprint')} actual={actual}"
            )

    def _has_table(self, name: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )


def _schema_payload(connection: sqlite3.Connection) -> dict[str, object]:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    result: dict[str, object] = {"algorithm": FINGERPRINT_ALGORITHM, "tables": []}
    table_payload: list[dict[str, object]] = []
    for (name,) in tables:
        columns = [dict(row) for row in connection.execute(f'PRAGMA table_info("{name}")')]
        foreign_keys = [
            dict(row) for row in connection.execute(f'PRAGMA foreign_key_list("{name}")')
        ]
        indexes = [dict(row) for row in connection.execute(f'PRAGMA index_list("{name}")')]
        table_payload.append(
            {
                "name": name,
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": indexes,
            }
        )
    result["tables"] = table_payload
    result["migration_head"] = _migration_head(connection)
    result["capabilities"] = {"sqlite": True, "extensions": []}
    return result


def _migration_head(connection: sqlite3.Connection) -> str | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if exists is None:
        return None
    row = connection.execute(
        "SELECT version_num FROM alembic_version ORDER BY version_num"
    ).fetchone()
    return None if row is None else str(row[0])
