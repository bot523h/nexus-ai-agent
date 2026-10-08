"""Two-way, CLI-only checkpoint lifecycle reconciler.

Dry-run by default; mutation happens only via ``apply=True`` and then only
under the cleanup lock, after measurement.  The reconciler NEVER deletes
LangGraph rows — the only mutable target is the independent lifecycle index
(``nexus_checkpoint_lifecycle``), and even that only through rows whose
purge decision passed every guard.

Directions:

* checkpoints -> lifecycle: checkpoints missing from the index are backfilled
  with a *protected* estimated age (created now), never treated as old.
* lifecycle -> checkpoints: orphaned index rows are blocked for 24h from
  their last known activity and purged only while every anomaly guard holds.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus_ai_agent.domain.policies.reconciler_policy import (
    anomaly_rate,
    orphan_block_until,
    purge_allowed,
)
from nexus_ai_agent.infrastructure.observability.structured import (
    error_field,
    log_lifecycle_event,
)
from nexus_ai_agent.storage.checkpoint_adapter import (
    CheckpointReadAdapter,
    CleanupDisabled,
)
from nexus_ai_agent.storage.checkpoint_fingerprint import (
    core_manifest_head as compute_core_manifest_head,
)
from nexus_ai_agent.storage.checkpoint_fingerprint import (
    evaluate_core_head,
)
from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord, LifecycleStore
from nexus_ai_agent.storage.checkpoint_lifecycle_store import (
    cleanup_lock,
)

# Driver exceptions from either backend; a DB error during a scan is a
# health-gate signal, never an unhandled crash (backend-agnostic since PR3).
# psycopg is an optional extra ([postgres]); a SQLite-only install must still be
# able to import this module, so the tuple degrades to the SQLite errors.
try:
    import psycopg

    _DB_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error, psycopg.Error)
except ModuleNotFoundError:
    _DB_ERRORS = (sqlite3.Error,)

ORPHAN = "orphan_lifecycle"
MISSING = "missing_lifecycle"
BROKEN_LINEAGE = "broken_lineage"

#: Default golden (schema-v1 fingerprint of the LangGraph SQLite schema).
DEFAULT_GOLDEN = Path(__file__).parent / "golden" / "sqlite.langgraph.json"

#: Sentinel: compute the core manifest head from the local migrations chain.
AUTO = "auto"


@dataclass(frozen=True)
class Anomaly:
    kind: str
    thread_id: str
    checkpoint_id: str | None
    detail: str = ""


@dataclass(frozen=True)
class OrphanDecision:
    thread_id: str
    checkpoint_id: str
    reference_time: datetime | None
    blocked_until: datetime | None
    would_purge: bool
    reasons: tuple[str, ...]


@dataclass
class ReconcileReport:
    rows_scanned: int
    checkpoints: int
    lifecycle_rows: int
    anomalies: list[Anomaly] = field(default_factory=list)
    orphan_decisions: list[OrphanDecision] = field(default_factory=list)
    health_gate: str = "ok"
    langgraph_schema: str = "not_evaluated"
    core_schema: str = "not_evaluated"
    kill_switch: bool = True
    applied: bool = False
    backfilled: int = 0
    purged: int = 0
    bytes_freed_estimate: int = 0

    @property
    def anomaly_rate(self) -> float:
        return anomaly_rate(rows_scanned=self.rows_scanned, anomalies=len(self.anomalies))

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_scanned": self.rows_scanned,
            "checkpoints": self.checkpoints,
            "lifecycle_rows": self.lifecycle_rows,
            "anomalies": [dataclasses.asdict(item) for item in self.anomalies],
            "anomaly_rate": self.anomaly_rate,
            "health_gate": self.health_gate,
            "langgraph_schema": self.langgraph_schema,
            "core_schema": self.core_schema,
            "kill_switch": self.kill_switch,
            "applied": self.applied,
            "backfilled": self.backfilled,
            "purged": self.purged,
            "would_free_bytes_estimate": self.bytes_freed_estimate,
            "orphans": [dataclasses.asdict(item) for item in self.orphan_decisions],
        }


def render_report(report: ReconcileReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str)


class CheckpointReconciler:
    """Measure, decide, then (only when asked) apply — in that order."""

    def __init__(
        self,
        adapter: CheckpointReadAdapter,
        store: LifecycleStore,
        *,
        golden_path: Path | str = DEFAULT_GOLDEN,
        enabled: bool = True,
        core_manifest_head: str | None = AUTO,
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._golden_path = Path(golden_path)
        self.enabled = enabled
        # ``AUTO`` (default) => compute from the local migrations manifest
        # (read-only); an explicit ``None`` means "manifest unavailable".
        self._core_manifest_head = (
            compute_core_manifest_head() if core_manifest_head == AUTO else core_manifest_head
        )

    # ── measurement ─────────────────────────────────────────────────────

    def _check_langgraph_schema(self) -> str:
        try:
            self._adapter.assert_golden(self._golden_path)
        except FileNotFoundError:
            # Missing golden: alert loudly, never auto-generate.
            log_lifecycle_event(
                logging.WARNING,
                "checkpoint cleanup disabled: golden fingerprint missing",
                operation="reconcile",
                golden=str(self._golden_path),
            )
            return "disabled:missing_golden"
        except CleanupDisabled as exc:
            log_lifecycle_event(
                logging.WARNING,
                "checkpoint cleanup disabled: schema fingerprint mismatch",
                operation="reconcile",
                reason=error_field(exc),
            )
            return "disabled:fingerprint_mismatch"
        except _DB_ERRORS as exc:
            # A connection error is NOT schema drift.
            return f"error:{type(exc).__name__}"
        return "match"

    def _scan(self, now: datetime) -> tuple[int, int, int, list[Anomaly], list[Anomaly], list, int]:
        """Read-only pass over checkpoints and the lifecycle index.

        Returns (rows_scanned, checkpoints, lifecycle_rows, anomalies,
        missing_anomalies, orphan_records, scan_errors).
        """
        threads: list[str] = []
        checkpoints_total = 0
        checkpoint_keys: set[tuple[str, str]] = set()
        anomalies: list[Anomaly] = []
        scan_errors = 0
        try:
            threads = self._adapter.list_threads()
        except _DB_ERRORS as exc:
            scan_errors += 1
            log_lifecycle_event(
                logging.WARNING,
                "reconcile scan failed to list threads",
                error=error_field(exc),
            )
        for thread_id in threads:
            try:
                checkpoints = self._adapter.list_checkpoints(thread_id)
                checkpoints_total += len(checkpoints)
                for item in checkpoints:
                    checkpoint_keys.add((thread_id, item.checkpoint_id))
                if not self._adapter.verify_lineage(thread_id):
                    anomalies.append(
                        Anomaly(BROKEN_LINEAGE, thread_id, None, "lineage check failed")
                    )
            except _DB_ERRORS as exc:
                scan_errors += 1
                log_lifecycle_event(
                    logging.WARNING,
                    "reconcile scan failed for thread",
                    thread_id=thread_id,
                    error=error_field(exc),
                )
        try:
            records = self._store.records()
        except _DB_ERRORS as exc:
            # e.g. the PG lifecycle table is missing (migration not run):
            # a measurement error, surfaced via the health gate — never a
            # crash, and never reported as schema drift.
            scan_errors += 1
            log_lifecycle_event(
                logging.WARNING,
                "reconcile scan failed to read the lifecycle index",
                error=error_field(exc),
            )
            records = []
        lifecycle_keys: set[tuple[str, str]] = set()
        orphan_records: list = []
        missing: list[Anomaly] = []
        for record in records:
            key = (record.thread_id, record.checkpoint_id)
            if key in checkpoint_keys:
                lifecycle_keys.add(key)
            else:
                orphan_records.append(record)
                anomalies.append(
                    Anomaly(ORPHAN, record.thread_id, record.checkpoint_id, "no checkpoint row")
                )
        for thread_id, checkpoint_id in sorted(checkpoint_keys):
            if (thread_id, checkpoint_id) not in lifecycle_keys:
                missing.append(Anomaly(MISSING, thread_id, checkpoint_id, "no lifecycle row"))
                anomalies.append(missing[-1])
        rows_scanned = checkpoints_total + len(records)
        return (
            rows_scanned,
            checkpoints_total,
            len(records),
            anomalies,
            missing,
            orphan_records,
            scan_errors,
        )

    # ── decision ────────────────────────────────────────────────────────

    def run(self, *, apply: bool = False, now: datetime | None = None) -> ReconcileReport:
        now = now or datetime.now(timezone.utc)
        report = ReconcileReport(
            rows_scanned=0, checkpoints=0, lifecycle_rows=0, kill_switch=self.enabled
        )
        # Core head is read from alembic_version (no migration run, R7) and
        # compared to the local manifest. Mismatch is warning-only (S6).
        report.core_schema = evaluate_core_head(self._adapter.core_head(), self._core_manifest_head)
        report.langgraph_schema = self._check_langgraph_schema()

        (
            report.rows_scanned,
            report.checkpoints,
            report.lifecycle_rows,
            anomalies,
            missing,
            orphan_records,
            scan_errors,
        ) = self._scan(now)
        report.anomalies = anomalies

        # Health gate: measurement from this execution only.  Any scan error
        # means the measurement is incomplete, so mutation is withheld
        # (fail-safe); the 10% rate is the explicit disable threshold.
        if scan_errors and report.rows_scanned > 0:
            rate = scan_errors / report.rows_scanned
            if rate > 0.10:
                report.health_gate = f"disabled:scan_error_rate {scan_errors}/{report.rows_scanned}"
            else:
                report.health_gate = f"degraded:scan_errors {scan_errors}"
        elif scan_errors:
            report.health_gate = f"disabled:scan_errors {scan_errors}"

        # Per-orphan decisions (pure policy, fail-safe).
        for record in orphan_records:
            reference = record.last_accessed_at or record.created_at
            blocked_until = orphan_block_until(reference)
            record_age = now - (reference or now)
            decision = purge_allowed(
                rows_scanned=report.rows_scanned,
                anomalies=len(anomalies),
                record_age=record_age,
                blocked_until=blocked_until,
                now=now,
            )
            report.orphan_decisions.append(
                OrphanDecision(
                    thread_id=record.thread_id,
                    checkpoint_id=record.checkpoint_id,
                    reference_time=reference,
                    blocked_until=blocked_until,
                    would_purge=decision.allowed and apply,
                    reasons=decision.reasons,
                )
            )

        report.applied = False
        if not apply:
            return report
        if not self.enabled:
            report.health_gate = "disabled:kill_switch"
            return report
        if report.langgraph_schema != "match":
            return report
        if report.health_gate != "ok":
            return report

        # ── mutation phase: lifecycle index only, under the cleanup lock ──
        with cleanup_lock(self._store.path):
            try:
                self._adapter.assert_golden(self._golden_path)
            except (CleanupDisabled, FileNotFoundError):
                log_lifecycle_event(
                    logging.WARNING, "reconcile apply aborted: schema no longer provable"
                )
                return report
            report.backfilled = self._backfill(missing, now)
            report.purged, report.bytes_freed_estimate = self._purge(
                [item for item in report.orphan_decisions if item.would_purge],
                now,
                len(anomalies),
                report.rows_scanned,
            )
        report.applied = True
        return report

    def _backfill(self, missing: list[Anomaly], now: datetime) -> int:
        count = 0
        for item in missing:
            try:
                # Unknown age is protected: recorded as created *now*.
                self._store.upsert(CheckpointRecord(item.thread_id, item.checkpoint_id or "", now))
                count += 1
            except _DB_ERRORS as exc:
                log_lifecycle_event(
                    logging.WARNING,
                    "reconcile backfill failed",
                    thread_id=item.thread_id,
                    error=error_field(exc),
                )
        return count

    def _purge(
        self,
        targets: list[OrphanDecision],
        now: datetime,
        anomalies: int,
        rows_scanned: int,
    ) -> tuple[int, int]:
        purged = 0
        freed = 0
        for item in targets:
            # Re-verify right before the DELETE: the checkpoint may have
            # reappeared since the scan (eventual consistency, both ways).
            if self._adapter.exists_checkpoint(item.thread_id, item.checkpoint_id):
                continue
            if not purge_allowed(
                rows_scanned=rows_scanned,
                anomalies=anomalies,
                record_age=now - (item.reference_time or now),
                blocked_until=item.blocked_until,
                now=now,
            ).allowed:
                continue
            record = self._find_record(item.thread_id, item.checkpoint_id)
            if record is None:
                continue
            try:
                self._store.delete_index(record)
                purged += 1
                # Only the index row is removed; estimate its on-disk size.
                freed += len(item.thread_id) + len(item.checkpoint_id) + 64
            except _DB_ERRORS as exc:
                log_lifecycle_event(
                    logging.WARNING,
                    "reconcile purge failed",
                    thread_id=item.thread_id,
                    error=error_field(exc),
                )
        return purged, freed

    def _find_record(self, thread_id: str, checkpoint_id: str) -> Any:
        for record in self._store.records():
            if record.thread_id == thread_id and record.checkpoint_id == checkpoint_id:
                return record
        return None


def lifecycle_db_path(checkpoint_path: str) -> str:
    """File location of the lifecycle index next to the checkpoint DB."""
    if checkpoint_path == ":memory:":
        return ":memory:"
    return checkpoint_path + ".lifecycle"
