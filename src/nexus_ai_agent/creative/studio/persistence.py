"""Durable persistence for CreativeGraph and studio transaction history.

Provides a minimal, robust SQLite-backed transaction log and graph store to guarantee
crash safety and deterministic recovery across restart boundaries (CRASH A, B, C).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.studio.models import EditTransaction, Project


class DurableStore:
    """SQLite-backed transaction log and project graph persistence."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    state_revision INTEGER NOT NULL,
                    state_hash TEXT NOT NULL,
                    json_data TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS transaction_log (
                    transaction_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    permission_level TEXT NOT NULL,
                    parent_revision INTEGER NOT NULL,
                    previous_state_hash TEXT NOT NULL,
                    new_state_hash TEXT NOT NULL,
                    plan_id TEXT,
                    state_before TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS artifact_passports (
                    passport_hash TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    passport_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS creative_graph_nodes (
                    node_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    transaction_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );
                """
            )

    def save_project_state(
        self,
        project: Project,
        new_transactions: list[EditTransaction] | tuple[EditTransaction, ...] = (),
    ) -> None:
        """Atomically persist central project state and transaction delta."""
        project_json = json.dumps(project.model_dump(mode="json"), ensure_ascii=False)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO projects (
                    project_id, name, state_revision, state_hash, json_data, updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(project_id) DO UPDATE SET
                    name = excluded.name,
                    state_revision = excluded.state_revision,
                    state_hash = excluded.state_hash,
                    json_data = excluded.json_data,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    project.project_id,
                    project.name,
                    project.state_revision,
                    project.state_hash,
                    project_json,
                ),
            )
            for tx in new_transactions:
                state_before_json = json.dumps(tx.state_before, ensure_ascii=False)
                self._conn.execute(
                    """
                    INSERT INTO transaction_log (
                        transaction_id, project_id, command_id, operation,
                        permission_level, parent_revision, previous_state_hash,
                        new_state_hash, plan_id, state_before
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(transaction_id) DO NOTHING
                    """,
                    (
                        tx.transaction_id,
                        project.project_id,
                        tx.command_id,
                        tx.operation,
                        tx.permission_level.value,
                        tx.parent_revision,
                        tx.previous_state_hash,
                        tx.new_state_hash,
                        tx.plan_id,
                        state_before_json,
                    ),
                )

    def load_project_state(self, project_id: str) -> Project | None:
        row = self._conn.execute(
            "SELECT json_data FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["json_data"])
        return Project.model_validate(data)

    def load_history(self, project_id: str) -> list[EditTransaction]:
        rows = self._conn.execute(
            """
            SELECT transaction_id, command_id, operation, permission_level,
                   parent_revision, previous_state_hash, new_state_hash,
                   plan_id, state_before
            FROM transaction_log
            WHERE project_id = ?
            ORDER BY parent_revision ASC
            """,
            (project_id,),
        ).fetchall()
        txs: list[EditTransaction] = []
        for r in rows:
            txs.append(
                EditTransaction(
                    transaction_id=r["transaction_id"],
                    command_id=r["command_id"],
                    operation=r["operation"],
                    permission_level=r["permission_level"],
                    parent_revision=r["parent_revision"],
                    previous_state_hash=r["previous_state_hash"],
                    new_state_hash=r["new_state_hash"],
                    plan_id=r["plan_id"],
                    state_before=json.loads(r["state_before"]),
                )
            )
        return txs

    def save_artifact_passport(self, passport: Any) -> None:
        """Atomically persist a proof-carrying artifact passport."""
        passport_json = json.dumps(passport.model_dump(mode="json"), ensure_ascii=False)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO artifact_passports (
                    passport_hash, artifact_id, project_id,
                    transaction_id, command_id, passport_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(passport_hash) DO NOTHING
                """,
                (
                    passport.passport_hash,
                    passport.artifact_id,
                    passport.causal_chain.project_id,
                    passport.causal_chain.transaction_id,
                    passport.causal_chain.command_id,
                    passport_json,
                ),
            )

    def load_artifact_passport(self, passport_hash: str) -> Any | None:
        from nexus_ai_agent.creative.studio.passport import ArtifactPassport

        row = self._conn.execute(
            "SELECT passport_json FROM artifact_passports WHERE passport_hash = ?",
            (passport_hash,),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["passport_json"])
        return ArtifactPassport.model_validate(data)

    def register_graph_node(
        self,
        node_id: str,
        project_id: str,
        node_type: str,
        transaction_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO creative_graph_nodes (
                    node_id, project_id, node_type, transaction_id, payload_json
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (node_id, project_id, node_type, transaction_id, payload_json),
            )

    def close(self) -> None:
        self._conn.close()
