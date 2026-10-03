"""Canonical Durable Persistence, Worker Lease, Monotonic Fencing & Idempotency Store.

Provides atomic SQLite-backed persistence for Nagar Creative Studio:
- Project snapshots, revision numbers, and content-addressed state hashes
- EditTransaction append-only log + replay verification from genesis
- Explicit aborted/rolled-back plan & command transaction records
- Durable idempotency reservations with payload fingerprint & undo eviction
- Proof-carrying ArtifactPassports, execution receipts, and lineage edges
- Worker job attempt leases with strictly monotonic fencing tokens and
  fail-closed rejection of stale workers or expired leases
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.studio.models import (
    CommandExecutionError,
    CommandResult,
    EditTransaction,
    PlanExecutionResult,
    Project,
    compute_state_hash,
)
from nexus_ai_agent.creative.studio.passport import ArtifactPassport


class StaleFencingTokenError(CommandExecutionError):
    """Raised fail-closed when a worker presents a stale or superseded fencing token."""


class LeaseExpiredError(CommandExecutionError):
    """Raised fail-closed when a worker attempt lease has expired or is no longer active."""


@dataclass(frozen=True)
class JobAttemptLease:
    """Canonical worker lease and monotonic fencing token for a job attempt."""

    job_id: str
    attempt_id: str
    worker_id: str
    fencing_token: int
    lease_expires_at: float
    status: str = "active"


class DurableStudioStore:
    """Thread-safe SQLite store for Studio state, transactions, idempotency, and leases."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            isolation_level="DEFERRED",
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute("PRAGMA busy_timeout=30000;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS studio_projects (
                    project_id TEXT PRIMARY KEY,
                    state_revision INTEGER NOT NULL,
                    state_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS studio_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_revision INTEGER NOT NULL,
                    command_id TEXT NOT NULL,
                    plan_id TEXT,
                    actor_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    permission_level TEXT NOT NULL,
                    previous_state_hash TEXT NOT NULL,
                    new_state_hash TEXT NOT NULL,
                    state_before_json TEXT NOT NULL,
                    tx_json TEXT NOT NULL,
                    UNIQUE(project_id, revision)
                );

                CREATE TABLE IF NOT EXISTS aborted_transactions (
                    abort_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    plan_id TEXT,
                    command_id TEXT,
                    reason TEXT NOT NULL,
                    state_revision INTEGER NOT NULL,
                    state_hash TEXT NOT NULL,
                    aborted_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS studio_idempotency (
                    project_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (project_id, operation, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS artifact_passports (
                    passport_hash TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    passport_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    plan_id TEXT,
                    job_id TEXT,
                    attempt_id TEXT,
                    fencing_token INTEGER,
                    artifact_id TEXT,
                    receipt_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS creative_graph_nodes (
                    node_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lineage_edges (
                    edge_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    parent_id TEXT NOT NULL,
                    child_id TEXT NOT NULL,
                    edge_kind TEXT NOT NULL,
                    transaction_id TEXT
                );

                CREATE TABLE IF NOT EXISTS job_leases (
                    job_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    PRIMARY KEY (job_id, attempt_id),
                    UNIQUE (job_id, fencing_token)
                );
                """
            )

    def save_snapshot(self, project: Project) -> None:
        """Persist or update a project's current state snapshot."""
        payload = json.dumps(project.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO studio_projects (
                    project_id, state_revision, state_hash, snapshot_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    state_revision = excluded.state_revision,
                    state_hash = excluded.state_hash,
                    snapshot_json = excluded.snapshot_json,
                    updated_at = excluded.updated_at
                """,
                (
                    project.project_id,
                    project.state_revision,
                    project.state_hash,
                    payload,
                    time.time(),
                ),
            )

    def record_transaction(self, project_id: str, tx: EditTransaction) -> None:
        """Append a committed EditTransaction to the durable transaction log."""
        tx_payload = json.dumps(tx.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        state_before_json = json.dumps(tx.state_before, sort_keys=True, ensure_ascii=False)
        revision = tx.parent_revision + 1
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO studio_transactions (
                    transaction_id, project_id, revision, parent_revision,
                    command_id, plan_id, actor_id, operation, permission_level,
                    previous_state_hash, new_state_hash, state_before_json, tx_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx.transaction_id,
                    project_id,
                    revision,
                    tx.parent_revision,
                    tx.command_id,
                    tx.plan_id,
                    tx.actor_id or "unknown",
                    str(tx.operation),
                    str(tx.permission_level.value),
                    tx.previous_state_hash,
                    tx.new_state_hash,
                    state_before_json,
                    tx_payload,
                ),
            )

    def save_project_state(
        self,
        project: Project,
        new_transactions: Sequence[EditTransaction] = (),
    ) -> None:
        """Atomically persist project snapshot and any new transactions."""
        with self._lock, self._conn:
            self.save_snapshot(project)
            for tx in new_transactions:
                self.record_transaction(project.project_id, tx)

    def record_aborted_transaction(
        self,
        project_id: str,
        *,
        plan_id: str | None = None,
        command_id: str | None = None,
        reason: str,
        state_revision: int,
        state_hash: str,
    ) -> str:
        """Record an explicit aborted plan or command while canonical state stays unchanged."""
        abort_id = f"abort_{uuid.uuid4().hex[:12]}"
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO aborted_transactions (
                    abort_id, project_id, plan_id, command_id, reason,
                    state_revision, state_hash, aborted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    abort_id,
                    project_id,
                    plan_id,
                    command_id,
                    reason,
                    state_revision,
                    state_hash,
                    time.time(),
                ),
            )
        return abort_id

    def list_aborted_transactions(self, project_id: str) -> list[dict[str, Any]]:
        """List all recorded aborted/rolled-back transactions for a project."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    abort_id, project_id, plan_id, command_id,
                    reason, state_revision, state_hash, aborted_at
                FROM aborted_transactions
                WHERE project_id = ?
                ORDER BY aborted_at ASC
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_idempotency(
        self,
        project_id: str,
        operation: str,
        idempotency_key: str,
        fingerprint: str,
        result: CommandResult | PlanExecutionResult,
    ) -> None:
        """Persist an idempotency reservation for a command or plan."""
        result_payload = json.dumps(
            result.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO studio_idempotency
                (project_id, operation, idempotency_key, fingerprint, result_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, operation, idempotency_key, fingerprint, result_payload),
            )

    def evict_idempotency(
        self,
        project_id: str,
        transaction_ids: set[str],
        plan_ids: set[str] | Sequence[str] = (),
    ) -> None:
        """Evict idempotency reservations belonging to undone transactions or plans."""
        plan_id_set = set(plan_ids)
        if not transaction_ids and not plan_id_set:
            return
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                SELECT operation, idempotency_key, result_json
                FROM studio_idempotency
                WHERE project_id = ?
                """,
                (project_id,),
            )
            to_delete: list[tuple[str, str]] = []
            for row in cur.fetchall():
                data = json.loads(row["result_json"])
                tx_id = data.get("transaction_id")
                plan_id = data.get("plan_id")
                step_results = data.get("step_results") or data.get("results") or []
                step_tx_ids = {
                    step.get("transaction_id")
                    for step in step_results
                    if isinstance(step, dict) and step.get("transaction_id")
                }
                if (
                    (tx_id and tx_id in transaction_ids)
                    or (plan_id and plan_id in plan_id_set)
                    or bool(step_tx_ids & transaction_ids)
                ):
                    to_delete.append((row["operation"], row["idempotency_key"]))
            for op, key in to_delete:
                self._conn.execute(
                    """
                    DELETE FROM studio_idempotency
                    WHERE project_id = ? AND operation = ? AND idempotency_key = ?
                    """,
                    (project_id, op, key),
                )

    def load_idempotency(
        self, project_id: str
    ) -> dict[tuple[str, str], tuple[str, CommandResult | PlanExecutionResult]]:
        """Load persisted idempotency reservations for a project."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT operation, idempotency_key, fingerprint, result_json
                FROM studio_idempotency
                WHERE project_id = ?
                """,
                (project_id,),
            )
            rows = cur.fetchall()
        out: dict[tuple[str, str], tuple[str, CommandResult | PlanExecutionResult]] = {}
        for row in rows:
            data = json.loads(row["result_json"])
            parsed: CommandResult | PlanExecutionResult
            if "plan_id" in data and ("results" in data or "step_results" in data):
                parsed = PlanExecutionResult.model_validate(data)
            else:
                parsed = CommandResult.model_validate(data)
            out[(row["operation"], row["idempotency_key"])] = (row["fingerprint"], parsed)
        return out

    def load_project_state(self, project_id: str) -> Project | None:
        """Load the latest persisted Project state from SQLite."""
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_json FROM studio_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return Project.model_validate(json.loads(row["snapshot_json"]))

    def load_history(self, project_id: str) -> list[EditTransaction]:
        """Load ordered transaction history for a project."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT tx_json FROM studio_transactions
                WHERE project_id = ?
                ORDER BY revision ASC
                """,
                (project_id,),
            ).fetchall()
        return [EditTransaction.model_validate(json.loads(r["tx_json"])) for r in rows]

    def load_project(self, project_id: str) -> tuple[Project, list[EditTransaction]] | None:
        """Load project state snapshot and ordered transaction history."""
        project = self.load_project_state(project_id)
        if project is None:
            return None
        history = self.load_history(project_id)
        return project, history

    def replay_project(self, project_id: str) -> Project:
        """Verify transaction chain continuity from genesis and return verified latest project."""
        loaded = self.load_project(project_id)
        if loaded is None:
            raise ValueError(f"Project {project_id!r} not found in DurableStudioStore")
        snapshot, history = loaded
        if not history:
            return snapshot

        genesis_state = Project.model_validate(history[0].state_before)
        expected_hash = compute_state_hash(genesis_state)
        if genesis_state.state_hash != expected_hash:
            raise ValueError("Genesis state_before hash mismatch during replay")

        prev_hash = genesis_state.state_hash
        for tx in history:
            revision = tx.parent_revision + 1
            if tx.previous_state_hash != prev_hash:
                raise ValueError(
                    f"Transaction chain broken at revision {revision}: "
                    f"expected previous_state_hash {prev_hash}, got {tx.previous_state_hash}"
                )
            prev_hash = tx.new_state_hash

        if snapshot.state_hash != prev_hash:
            raise ValueError(
                f"Snapshot state_hash {snapshot.state_hash} does not match "
                f"final transaction hash {prev_hash}"
            )
        return snapshot

    def save_artifact_passport(self, passport: ArtifactPassport) -> None:
        """Persist a verified content-addressed ArtifactPassport."""
        if not passport.verify_integrity():
            raise ValueError(
                f"Cannot persist ArtifactPassport {passport.artifact_id!r}: "
                "integrity verification failed"
            )
        payload = json.dumps(passport.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO artifact_passports (
                    passport_hash, artifact_id, project_id,
                    transaction_id, command_id, passport_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    passport.passport_hash,
                    passport.artifact_id,
                    passport.causal_chain.project_id,
                    passport.causal_chain.transaction_id,
                    passport.causal_chain.command_id,
                    payload,
                ),
            )

    def load_artifact_passport(self, passport_hash: str) -> ArtifactPassport | None:
        """Load and verify an ArtifactPassport by its content-addressed hash."""
        with self._lock:
            row = self._conn.execute(
                "SELECT passport_json FROM artifact_passports WHERE passport_hash = ?",
                (passport_hash,),
            ).fetchone()
        if row is None:
            return None
        passport = ArtifactPassport.model_validate(json.loads(row["passport_json"]))
        if not passport.verify_integrity():
            raise ValueError(f"Corrupted ArtifactPassport in database: {passport_hash}")
        return passport

    def list_artifact_passports(self, project_id: str) -> list[ArtifactPassport]:
        """Return all verified ArtifactPassports for a project."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT passport_json FROM artifact_passports
                WHERE project_id = ?
                ORDER BY rowid ASC
                """,
                (project_id,),
            ).fetchall()
        out: list[ArtifactPassport] = []
        for row in rows:
            passport = ArtifactPassport.model_validate(json.loads(row["passport_json"]))
            if not passport.verify_integrity():
                raise ValueError(
                    f"Corrupted ArtifactPassport in database: {passport.passport_hash}"
                )
            out.append(passport)
        return out

    def record_execution_receipt(
        self,
        receipt_id: str,
        project_id: str,
        transaction_id: str,
        command_id: str,
        *,
        plan_id: str | None = None,
        job_id: str | None = None,
        attempt_id: str | None = None,
        fencing_token: int | None = None,
        artifact_id: str | None = None,
        payload: dict[str, Any],
    ) -> None:
        """Persist a machine-verifiable execution receipt."""
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO execution_receipts (
                    receipt_id, project_id, transaction_id, command_id,
                    plan_id, job_id, attempt_id, fencing_token, artifact_id, receipt_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    project_id,
                    transaction_id,
                    command_id,
                    plan_id,
                    job_id,
                    attempt_id,
                    fencing_token,
                    artifact_id,
                    raw,
                ),
            )

    def list_execution_receipts(self, project_id: str) -> list[dict[str, Any]]:
        """List all persisted execution receipts for a project."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT receipt_json FROM execution_receipts
                WHERE project_id = ?
                ORDER BY rowid ASC
                """,
                (project_id,),
            ).fetchall()
        return [json.loads(row["receipt_json"]) for row in rows]

    def register_graph_node(
        self,
        node_id: str,
        project_id: str,
        node_type: str,
        transaction_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist a creative lineage graph node."""
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO creative_graph_nodes (
                    node_id, project_id, node_type, transaction_id, payload_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (node_id, project_id, node_type, transaction_id, raw),
            )

    def record_lineage_edge(
        self,
        project_id: str,
        parent_id: str,
        child_id: str,
        edge_kind: str,
        transaction_id: str | None = None,
    ) -> str:
        """Persist a directed lineage edge between two provenance entities."""
        edge_id = f"edge_{parent_id}_{child_id}_{edge_kind}"
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO lineage_edges (
                    edge_id, project_id, parent_id, child_id, edge_kind, transaction_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (edge_id, project_id, parent_id, child_id, edge_kind, transaction_id),
            )
        return edge_id

    def list_lineage_edges(self, project_id: str) -> list[dict[str, Any]]:
        """List all persisted lineage edges for a project."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT edge_id, project_id, parent_id, child_id, edge_kind, transaction_id
                FROM lineage_edges
                WHERE project_id = ?
                ORDER BY rowid ASC
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def acquire_lease(
        self,
        job_id: str,
        attempt_id: str,
        worker_id: str,
        *,
        ttl_seconds: float = 30.0,
        now: float | None = None,
    ) -> JobAttemptLease:
        """Acquire a worker lease and strictly monotonic fencing token for ``job_id``."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current_time = time.time() if now is None else now
        expires_at = current_time + ttl_seconds
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT COALESCE(MAX(fencing_token), 0) AS max_token
                FROM job_leases
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            next_token = int(row["max_token"]) + 1
            # Mark any prior active leases for this job as superseded
            self._conn.execute(
                """
                UPDATE job_leases
                SET status = 'superseded'
                WHERE job_id = ? AND status = 'active'
                """,
                (job_id,),
            )
            self._conn.execute(
                """
                INSERT INTO job_leases (
                    job_id, attempt_id, worker_id,
                    fencing_token, lease_expires_at, status, result_json
                )
                VALUES (?, ?, ?, ?, ?, 'active', NULL)
                """,
                (job_id, attempt_id, worker_id, next_token, expires_at),
            )
        return JobAttemptLease(
            job_id=job_id,
            attempt_id=attempt_id,
            worker_id=worker_id,
            fencing_token=next_token,
            lease_expires_at=expires_at,
            status="active",
        )

    def verify_fence(
        self,
        job_id: str,
        attempt_id: str,
        fencing_token: int,
        *,
        now: float | None = None,
    ) -> JobAttemptLease:
        """Verify ``fencing_token`` is the current monotonic token and lease is active."""
        current_time = time.time() if now is None else now
        with self._lock:
            max_row = self._conn.execute(
                """
                SELECT COALESCE(MAX(fencing_token), 0) AS max_token
                FROM job_leases
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            current_max = int(max_row["max_token"])
            row = self._conn.execute(
                """
                SELECT job_id, attempt_id, worker_id, fencing_token, lease_expires_at, status
                FROM job_leases
                WHERE job_id = ? AND attempt_id = ?
                """,
                (job_id, attempt_id),
            ).fetchone()
            if (
                row is None
                or int(row["fencing_token"]) != fencing_token
                or fencing_token < current_max
            ):
                raise StaleFencingTokenError(
                    f"Stale fencing token {fencing_token} for job {job_id!r} "
                    f"(current_fencing_token={current_max})"
                )
            if row["status"] != "active" or current_time >= float(row["lease_expires_at"]):
                raise LeaseExpiredError(
                    f"Lease for job {job_id!r} attempt {attempt_id!r} is expired or inactive "
                    f"(status={row['status']!r})"
                )
            return JobAttemptLease(
                job_id=row["job_id"],
                attempt_id=row["attempt_id"],
                worker_id=row["worker_id"],
                fencing_token=int(row["fencing_token"]),
                lease_expires_at=float(row["lease_expires_at"]),
                status=row["status"],
            )

    def commit_fenced_side_effect(
        self,
        job_id: str,
        attempt_id: str,
        fencing_token: int,
        *,
        project_id: str,
        transaction_id: str,
        command_id: str,
        artifact_id: str | None = None,
        result_payload: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> JobAttemptLease:
        """Atomically verify the fencing token & lease validity and commit the job result."""
        payload = dict(result_payload or {})
        raw_result = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self._lock, self._conn:
            lease = self.verify_fence(job_id, attempt_id, fencing_token, now=now)
            self._conn.execute(
                """
                UPDATE job_leases
                SET status = 'completed', result_json = ?
                WHERE job_id = ? AND attempt_id = ? AND fencing_token = ?
                """,
                (raw_result, job_id, attempt_id, fencing_token),
            )
            receipt_id = f"rcpt_job_{job_id}_{attempt_id}_{fencing_token}"
            self.record_execution_receipt(
                receipt_id=receipt_id,
                project_id=project_id,
                transaction_id=transaction_id,
                command_id=command_id,
                job_id=job_id,
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                artifact_id=artifact_id,
                payload={
                    "receipt_id": receipt_id,
                    "job_id": job_id,
                    "attempt_id": attempt_id,
                    "worker_id": lease.worker_id,
                    "fencing_token": fencing_token,
                    "artifact_id": artifact_id,
                    "result": payload,
                },
            )
            return JobAttemptLease(
                job_id=lease.job_id,
                attempt_id=lease.attempt_id,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                lease_expires_at=lease.lease_expires_at,
                status="completed",
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# Canonical alias so DurableStudioStore and DurableStore imports resolve to the single authority
DurableStore = DurableStudioStore

__all__ = [
    "DurableStore",
    "DurableStudioStore",
    "JobAttemptLease",
    "LeaseExpiredError",
    "StaleFencingTokenError",
]
