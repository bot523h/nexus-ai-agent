"""Queue-to-Provenance Transactional Outbox and Idempotent Reconciler (Wave 2).

Two separate SQLite databases (the execution queue DB and the durable
``CreativeGraph`` DB) cannot share an atomic native cross-database transaction.
To eliminate split-brain without pretending otherwise, queue milestone
transitions atomically insert events into ``provenance_outbox`` inside the
**same** ``BEGIN IMMEDIATE`` SQLite transaction as ``nexus_job_queue`` and
``nexus_attempt_leases``.

:class:`OutboxReconciler` then drains ``PENDING`` outbox records into the
durable :class:`~nexus_ai_agent.provenance.graph.CreativeGraph` idempotently.
A crash after queue commit and before reconciliation leaves the record safely
``PENDING`` for deterministic replay on recovery.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus_ai_agent.provenance.graph import (
    TERMINAL_STATUSES,
    CreativeGraph,
    EpistemicProof,
    EpistemicStatus,
    canonical_graph_digest,
    canonical_graph_json,
    node_identity,
)

OUTBOX_EVENT_ATTEMPT_RESERVED = "ATTEMPT_RESERVED"
OUTBOX_EVENT_ATTEMPT_SUPERSEDED = "ATTEMPT_SUPERSEDED"
OUTBOX_EVENT_ATTEMPT_VERIFIED = "ATTEMPT_VERIFIED"
OUTBOX_EVENT_JOB_COMPLETED = "JOB_COMPLETED"
OUTBOX_EVENT_JOB_FAILED = "JOB_FAILED"

PROVENANCE_OUTBOX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS provenance_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    job_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    attempt_epoch INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(status IN ('PENDING', 'APPLIED', 'FAILED_DEAD')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    applied_at TEXT
)
"""

PROVENANCE_OUTBOX_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_provenance_outbox_status_created
ON provenance_outbox(status, created_at, outbox_id)
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_outbox_id(job_id: str, attempt_epoch: int, event_type: str) -> str:
    """Deterministic content-keyed outbox ID for idempotent insertion."""
    material = f"{job_id}\0{attempt_epoch}\0{event_type}".encode()
    return f"outbox_{hashlib.sha256(material).hexdigest()[:32]}"


def ensure_outbox_schema(connection: sqlite3.Connection) -> None:
    """Create the ``provenance_outbox`` table and index if absent."""
    connection.execute(PROVENANCE_OUTBOX_TABLE_SQL)
    connection.execute(PROVENANCE_OUTBOX_INDEX_SQL)


def insert_outbox_event_tx(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    job_id: str,
    attempt_id: str,
    attempt_epoch: int,
    payload: dict[str, Any],
    created_at: str | None = None,
) -> str:
    """Insert an outbox event inside the caller's open SQLite transaction."""
    if not event_type or not job_id or not attempt_id:
        raise ValueError("outbox event requires event_type, job_id, and attempt_id")
    if isinstance(attempt_epoch, bool) or attempt_epoch < 0:
        raise ValueError("attempt_epoch must be a non-negative integer")
    outbox_id = make_outbox_id(job_id, attempt_epoch, event_type)
    payload_json = canonical_graph_json(payload)
    payload_digest = canonical_graph_digest(payload)
    ts = created_at or _utc_iso()
    connection.execute(
        """
        INSERT INTO provenance_outbox (
            outbox_id, event_type, job_id, attempt_id, attempt_epoch,
            payload_json, payload_digest, status, attempts, last_error,
            created_at, applied_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, NULL, ?, NULL)
        ON CONFLICT(outbox_id) DO NOTHING
        """,
        (
            outbox_id,
            event_type,
            job_id,
            attempt_id,
            attempt_epoch,
            payload_json,
            payload_digest,
            ts,
        ),
    )
    return outbox_id


@dataclass(frozen=True)
class OutboxEvent:
    """Immutable view of one ``provenance_outbox`` row."""

    outbox_id: str
    event_type: str
    job_id: str
    attempt_id: str
    attempt_epoch: int
    payload_json: str
    payload_digest: str
    status: str
    attempts: int
    last_error: str | None
    created_at: str
    applied_at: str | None


@dataclass(frozen=True)
class OutboxReconcileReport:
    """Summary returned by :meth:`OutboxReconciler.reconcile_pending`."""

    scanned: int
    applied: int
    already_applied: int
    failed: int
    errors: tuple[str, ...] = ()


class ProvenanceOutboxStore:
    """SQLite store for ``provenance_outbox`` (co-located with the job queue DB)."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        memory_connection: sqlite3.Connection | None = None,
    ) -> None:
        if db_path is None and memory_connection is None:
            raise ValueError("ProvenanceOutboxStore requires db_path or memory_connection")
        self._db_path = Path(db_path) if db_path is not None else None
        self._memory_connection = memory_connection
        self.ensure_schema()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            try:
                yield self._memory_connection
            except Exception:
                self._memory_connection.rollback()
                raise
            else:
                self._memory_connection.commit()
            return
        assert self._db_path is not None
        conn = sqlite3.connect(str(self._db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
        except Exception:
            if write:
                conn.execute("ROLLBACK")
            raise
        else:
            if write:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self._connection(write=True) as conn:
            ensure_outbox_schema(conn)

    def enqueue(
        self,
        *,
        event_type: str,
        job_id: str,
        attempt_id: str,
        attempt_epoch: int,
        payload: dict[str, Any],
        created_at: str | None = None,
    ) -> str:
        with self._connection(write=True) as conn:
            return insert_outbox_event_tx(
                conn,
                event_type=event_type,
                job_id=job_id,
                attempt_id=attempt_id,
                attempt_epoch=attempt_epoch,
                payload=payload,
                created_at=created_at,
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(
            outbox_id=str(row["outbox_id"]),
            event_type=str(row["event_type"]),
            job_id=str(row["job_id"]),
            attempt_id=str(row["attempt_id"]),
            attempt_epoch=int(row["attempt_epoch"]),
            payload_json=str(row["payload_json"]),
            payload_digest=str(row["payload_digest"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            created_at=str(row["created_at"]),
            applied_at=str(row["applied_at"]) if row["applied_at"] is not None else None,
        )

    def list_pending(self, *, limit: int = 100) -> list[OutboxEvent]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        with self._connection(write=False) as conn:
            rows = conn.execute(
                """
                SELECT * FROM provenance_outbox
                WHERE status = 'PENDING'
                ORDER BY created_at ASC, outbox_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_all(self) -> list[OutboxEvent]:
        with self._connection(write=False) as conn:
            rows = conn.execute(
                "SELECT * FROM provenance_outbox ORDER BY created_at ASC, outbox_id ASC"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def mark_applied(self, outbox_id: str, *, applied_at: str | None = None) -> bool:
        ts = applied_at or _utc_iso()
        with self._connection(write=True) as conn:
            cur = conn.execute(
                """
                UPDATE provenance_outbox
                SET status = 'APPLIED', attempts = attempts + 1,
                    applied_at = ?, last_error = NULL
                WHERE outbox_id = ? AND status = 'PENDING'
                """,
                (ts, outbox_id),
            )
            return cur.rowcount == 1

    def record_failure(self, outbox_id: str, *, error: str, dead_letter: bool = False) -> bool:
        next_status = "FAILED_DEAD" if dead_letter else "PENDING"
        with self._connection(write=True) as conn:
            cur = conn.execute(
                """
                UPDATE provenance_outbox
                SET status = ?, attempts = attempts + 1, last_error = ?
                WHERE outbox_id = ? AND status = 'PENDING'
                """,
                (next_status, error[:1000], outbox_id),
            )
            return cur.rowcount == 1


class OutboxReconciler:
    """Idempotent reconciler draining ``provenance_outbox`` into ``CreativeGraph``."""

    def __init__(self, outbox_store: ProvenanceOutboxStore, graph: CreativeGraph) -> None:
        self._outbox = outbox_store
        self._graph = graph

    def reconcile_pending(self, *, limit: int = 100) -> OutboxReconcileReport:
        pending = self._outbox.list_pending(limit=limit)
        applied = 0
        already_applied = 0
        failed = 0
        errors: list[str] = []

        for event in pending:
            try:
                payload = json.loads(event.payload_json)
                if not isinstance(payload, dict):
                    raise ValueError("outbox payload_json is not a JSON object")
                if canonical_graph_digest(payload) != event.payload_digest:
                    raise ValueError("outbox payload_digest mismatch (tampered payload)")
                self._apply_event(event, payload)
                if self._outbox.mark_applied(event.outbox_id):
                    applied += 1
                else:
                    already_applied += 1
            except Exception as exc:  # noqa: BLE001 - fail closed per record
                failed += 1
                msg = f"{event.outbox_id}: {type(exc).__name__}: {exc}"
                errors.append(msg)
                dead = isinstance(exc, (ValueError, json.JSONDecodeError))
                self._outbox.record_failure(event.outbox_id, error=str(exc), dead_letter=dead)

        return OutboxReconcileReport(
            scanned=len(pending),
            applied=applied,
            already_applied=already_applied,
            failed=failed,
            errors=tuple(errors),
        )

    def _apply_event(self, event: OutboxEvent, payload: dict[str, Any]) -> None:
        job_identity = {"job_id": event.job_id}
        job_payload: dict[str, Any] = {
            "job_id": event.job_id,
            "job_type": str(payload.get("job_type") or "creative_render"),
        }
        if payload.get("project_id") is not None:
            job_payload["project_id"] = str(payload["project_id"])
        base_rev = payload.get("base_revision_id") or payload.get("revision_id")
        if base_rev is not None:
            job_payload["base_revision_id"] = str(base_rev)

        job_node_id = node_identity("COMMAND", job_identity)
        existing_job = self._graph.get_node(job_node_id)
        if existing_job is not None and existing_job.status in TERMINAL_STATUSES:
            job_res_id = existing_job.node_id
        else:
            job_res = self._graph.put_node(
                kind="COMMAND",
                identity=job_identity,
                payload=job_payload,
                logical_entity_id=f"job:{event.job_id}",
                revision_id=f"job:{event.job_id}",
                source="outbox_reconciler",
            )
            assert job_res.node is not None
            job_res_id = job_res.node.node_id

        attempt_identity = {
            "job_id": event.job_id,
            "attempt_epoch": event.attempt_epoch,
        }
        attempt_payload: dict[str, Any] = {
            "job_id": event.job_id,
            "attempt_id": event.attempt_id,
            "attempt_epoch": event.attempt_epoch,
            "event_type": event.event_type,
        }
        for key in ("worker_id", "project_id", "revision_id", "error"):
            if payload.get(key) is not None:
                attempt_payload[key] = payload[key]

        attempt_status_tier = EpistemicStatus.HYPOTHESIS
        evidence_digest: str | None = None
        raw_ev = payload.get("evidence_digest")
        if isinstance(raw_ev, str) and raw_ev.startswith("sha256:"):
            attempt_status_tier = EpistemicStatus.MEASURED_EVIDENCE
            evidence_digest = raw_ev

        attempt_node_id = node_identity("EXECUTION", attempt_identity)
        existing_attempt = self._graph.get_node(attempt_node_id)
        if existing_attempt is not None and existing_attempt.status in TERMINAL_STATUSES:
            attempt_res_id = existing_attempt.node_id
        else:
            if (
                existing_attempt is not None
                and existing_attempt.epistemic_status is not EpistemicStatus.HYPOTHESIS
                and attempt_status_tier is EpistemicStatus.HYPOTHESIS
            ):
                attempt_status_tier = existing_attempt.epistemic_status
                evidence_digest = evidence_digest or existing_attempt.evidence_digest
            attempt_res = self._graph.put_node(
                kind="EXECUTION",
                identity=attempt_identity,
                payload=attempt_payload,
                logical_entity_id=f"attempt:{event.attempt_id}",
                revision_id=f"epoch:{event.attempt_epoch}",
                epistemic_status=attempt_status_tier,
                evidence_digest=evidence_digest,
                source="outbox_reconciler",
            )
            assert attempt_res.node is not None
            attempt_res_id = attempt_res.node.node_id
        self._graph.put_edge(
            relation="COMMAND_EXECUTED_AS",
            src=job_res_id,
            dst=attempt_res_id,
            payload={"attempt_epoch": event.attempt_epoch},
            source="outbox_reconciler",
        )

        if event.event_type == OUTBOX_EVENT_ATTEMPT_SUPERSEDED:
            cur = self._graph.get_node(attempt_res_id)
            if cur is not None and cur.status not in TERMINAL_STATUSES:
                self._graph.set_status(
                    attempt_res_id,
                    "superseded",
                    superseded_by=str(payload.get("superseded_by") or "") or None,
                )
            return

        if event.event_type == OUTBOX_EVENT_JOB_FAILED:
            cur = self._graph.get_node(attempt_res_id)
            if cur is not None and cur.status not in TERMINAL_STATUSES:
                self._graph.set_status(attempt_res_id, "invalidated")
            return

        if event.event_type in (OUTBOX_EVENT_ATTEMPT_VERIFIED, OUTBOX_EVENT_JOB_COMPLETED):
            artifact_digest = payload.get("artifact_digest") or payload.get("content_digest")
            if isinstance(artifact_digest, str) and artifact_digest.startswith("sha256:"):
                art_payload: dict[str, Any] = {
                    "job_id": event.job_id,
                    "attempt_id": event.attempt_id,
                    "attempt_epoch": event.attempt_epoch,
                    "content_digest": artifact_digest,
                }
                for key in (
                    "project_id",
                    "base_revision_id",
                    "accepted_revision_id",
                    "input_asset_version_ids",
                    "output_asset_version_id",
                    "verification_proof_digest",
                    "artifact_path",
                ):
                    if payload.get(key) is not None:
                        art_payload[key] = payload[key]
                ev_digest = (
                    str(payload["verification_proof_digest"])
                    if isinstance(payload.get("verification_proof_digest"), str)
                    and str(payload["verification_proof_digest"]).startswith("sha256:")
                    else artifact_digest
                )
                art_identity = {
                    "job_id": event.job_id,
                    "attempt_epoch": event.attempt_epoch,
                    "content_digest": artifact_digest,
                }
                art_node_id = node_identity("ARTIFACT", art_identity)
                existing_art = self._graph.get_node(art_node_id)
                raw_proof = payload.get("verification_proof")
                parsed_proof = (
                    EpistemicProof.from_dict(raw_proof) if isinstance(raw_proof, dict) else None
                )
                if existing_art is not None and (
                    existing_art.status in TERMINAL_STATUSES
                    or existing_art.epistemic_status is EpistemicStatus.VERIFIED_COMMIT
                ):
                    art_res_id = existing_art.node_id
                else:
                    art_res = self._graph.put_node(
                        kind="ARTIFACT",
                        identity=art_identity,
                        payload=art_payload,
                        logical_entity_id=f"artifact:{event.job_id}:{artifact_digest}",
                        revision_id=str(
                            payload.get("accepted_revision_id")
                            or payload.get("base_revision_id")
                            or f"epoch:{event.attempt_epoch}"
                        ),
                        content_digest=artifact_digest,
                        epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
                        evidence_digest=ev_digest,
                        source="outbox_reconciler",
                    )
                    assert art_res.node is not None
                    art_res_id = art_res.node.node_id
                self._graph.put_edge(
                    relation="EXECUTION_PRODUCED_ARTIFACT",
                    src=attempt_res_id,
                    dst=art_res_id,
                    payload={"attempt_epoch": event.attempt_epoch},
                    epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
                    evidence_digest=ev_digest,
                    source="outbox_reconciler",
                )
                if parsed_proof is not None and (
                    existing_art is None
                    or existing_art.epistemic_status is not EpistemicStatus.VERIFIED_COMMIT
                ):
                    self._graph.commit_verified_revision(
                        node_id=art_res_id,
                        verification_proof=parsed_proof,
                        evidence_digest=ev_digest,
                    )


__all__ = [
    "OUTBOX_EVENT_ATTEMPT_RESERVED",
    "OUTBOX_EVENT_ATTEMPT_SUPERSEDED",
    "OUTBOX_EVENT_ATTEMPT_VERIFIED",
    "OUTBOX_EVENT_JOB_COMPLETED",
    "OUTBOX_EVENT_JOB_FAILED",
    "PROVENANCE_OUTBOX_INDEX_SQL",
    "PROVENANCE_OUTBOX_TABLE_SQL",
    "OutboxEvent",
    "OutboxReconcileReport",
    "OutboxReconciler",
    "ProvenanceOutboxStore",
    "ensure_outbox_schema",
    "insert_outbox_event_tx",
    "make_outbox_id",
]
