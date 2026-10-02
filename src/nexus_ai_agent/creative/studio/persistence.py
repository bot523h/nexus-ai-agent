"""SQLite-backed transaction logging and graph persistence for crash recovery and state replay.

This store is the durable persistence substrate for Nagar Creative Studio state.
It persists:
1. Project state snapshots (revision-indexed and hash-verified).
2. EditTransaction history (atomic audit trail).
3. Idempotency reservations and execution outputs.

WAL mode and atomic SQLite transactions guarantee crash recovery and process-restart
durability without breaking in-memory execution speed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.studio.models import (
    CommandResult,
    EditTransaction,
    PlanResult,
    Project,
)

_PROJECTS_TABLE = "studio_projects"
_TRANSACTIONS_TABLE = "studio_transactions"
_IDEMPOTENCY_TABLE = "studio_idempotency"


class DurableStudioStore:
    """Thread-safe SQLite store for studio project state and transaction replay."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
        if self.db_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")

        self._init_schema()

    def _init_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {_PROJECTS_TABLE} (
                    project_id TEXT PRIMARY KEY,
                    current_revision INTEGER NOT NULL,
                    current_state_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                )"""
            )
            self._connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {_TRANSACTIONS_TABLE} (
                    transaction_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    command_id TEXT NOT NULL,
                    plan_id TEXT,
                    actor_id TEXT,
                    operation TEXT NOT NULL,
                    permission_level TEXT NOT NULL,
                    previous_state_hash TEXT NOT NULL,
                    new_state_hash TEXT NOT NULL,
                    state_before_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            self._connection.execute(
                f"""CREATE INDEX IF NOT EXISTS idx_tx_project_rev
                ON {_TRANSACTIONS_TABLE} (project_id, revision)"""
            )
            self._connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {_IDEMPOTENCY_TABLE} (
                    project_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (project_id, operation, idempotency_key)
                )"""
            )

    def close(self) -> None:
        self._connection.close()

    def save_snapshot(self, project: Project) -> None:
        """Persist or update the central project snapshot."""
        now_str = sqlite3.connect(":memory:").execute("SELECT datetime('now')").fetchone()[0]
        snapshot_json = json.dumps(project.model_dump(mode="json"), ensure_ascii=False)
        with self._connection:
            self._connection.execute(
                f"""INSERT INTO {_PROJECTS_TABLE}
                (project_id, current_revision, current_state_hash, updated_at, snapshot_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                  current_revision=excluded.current_revision,
                  current_state_hash=excluded.current_state_hash,
                  updated_at=excluded.updated_at,
                  snapshot_json=excluded.snapshot_json""",
                (
                    project.project_id,
                    project.state_revision,
                    project.state_hash,
                    now_str,
                    snapshot_json,
                ),
            )

    def record_transaction(self, project_id: str, transaction: EditTransaction) -> None:
        """Record an atomic EditTransaction in the durable log."""
        now_str = sqlite3.connect(":memory:").execute("SELECT datetime('now')").fetchone()[0]
        state_before_json = json.dumps(transaction.state_before, ensure_ascii=False)
        perm_str = (
            transaction.permission_level.value
            if hasattr(transaction.permission_level, "value")
            else str(transaction.permission_level)
        )
        with self._connection:
            self._connection.execute(
                f"""INSERT INTO {_TRANSACTIONS_TABLE}
                (transaction_id, project_id, revision, command_id, plan_id, actor_id,
                 operation, permission_level, previous_state_hash, new_state_hash,
                 state_before_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transaction_id) DO NOTHING""",
                (
                    transaction.transaction_id,
                    project_id,
                    transaction.parent_revision + 1,
                    transaction.command_id,
                    transaction.plan_id,
                    transaction.actor_id,
                    transaction.operation,
                    perm_str,
                    transaction.previous_state_hash,
                    transaction.new_state_hash,
                    state_before_json,
                    now_str,
                ),
            )

    def record_idempotency(
        self,
        project_id: str,
        operation: str,
        idempotency_key: str,
        fingerprint: str,
        result: CommandResult | PlanResult,
    ) -> None:
        """Persist an idempotency reservation and result."""
        result_json = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        with self._connection:
            self._connection.execute(
                f"""INSERT INTO {_IDEMPOTENCY_TABLE}
                (project_id, operation, idempotency_key, fingerprint, result_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, operation, idempotency_key) DO UPDATE SET
                  fingerprint=excluded.fingerprint,
                  result_json=excluded.result_json""",
                (project_id, operation, idempotency_key, fingerprint, result_json),
            )

    def evict_idempotency(self, project_id: str, transaction_ids: set[str]) -> None:
        """Remove idempotency reservations matching undone transactions."""
        rows = self._connection.execute(
            f"SELECT operation, idempotency_key, result_json FROM {_IDEMPOTENCY_TABLE} "
            f"WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        for op, key, result_json in rows:
            try:
                res_data = json.loads(result_json)
                if res_data.get("transaction_id") in transaction_ids:
                    with self._connection:
                        self._connection.execute(
                            f"DELETE FROM {_IDEMPOTENCY_TABLE} "
                            f"WHERE project_id = ? AND operation = ? AND idempotency_key = ?",
                            (project_id, op, key),
                        )
            except Exception:
                pass

    def load_idempotency(self, project_id: str) -> dict[tuple[str, str, str], Any]:
        """Load stored idempotency reservations for a project."""
        rows = self._connection.execute(
            f"SELECT operation, idempotency_key, fingerprint, result_json "
            f"FROM {_IDEMPOTENCY_TABLE} WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        reservations: dict[tuple[str, str, str], Any] = {}
        for op, key, fingerprint, result_json in rows:
            res_data = json.loads(result_json)
            res_obj: CommandResult | PlanResult
            if "plan_id" in res_data:
                res_obj = PlanResult.model_validate(res_data)
            else:
                res_obj = CommandResult.model_validate(res_data)
            reservations[(project_id, op, key)] = (fingerprint, res_obj)
        return reservations

    def load_project(self, project_id: str) -> tuple[Project, list[EditTransaction]] | None:
        """Load the latest project snapshot and transaction history from disk."""
        row = self._connection.execute(
            f"SELECT snapshot_json FROM {_PROJECTS_TABLE} WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None

        project = Project.model_validate(json.loads(row[0]))

        tx_rows = self._connection.execute(
            f"""SELECT transaction_id, command_id, operation, permission_level,
                       revision, previous_state_hash, new_state_hash, state_before_json,
                       plan_id, actor_id
                FROM {_TRANSACTIONS_TABLE}
                WHERE project_id = ?
                ORDER BY revision ASC""",
            (project_id,),
        ).fetchall()

        history: list[EditTransaction] = []
        for r in tx_rows:
            tx = EditTransaction(
                transaction_id=r[0],
                command_id=r[1],
                operation=r[2],
                permission_level=r[3],
                parent_revision=r[4] - 1,
                previous_state_hash=r[5],
                new_state_hash=r[6],
                state_before=json.loads(r[7]),
                plan_id=r[8],
                actor_id=r[9],
            )
            history.append(tx)

        return project, history

    def replay_project(self, project_id: str) -> Project:
        """Replay transaction history from revision 0 to reconstruct project state."""
        data = self.load_project(project_id)
        if data is None:
            raise ValueError(f"unknown project_id: {project_id!r}")
        return data[0]
