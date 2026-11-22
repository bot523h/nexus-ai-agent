"""Bounded SQLite plan reservations; not an authorization or execution service.

A caller must independently authorize both first execution and redelivery. A
committed record proves only that its caller recorded a result, not that an
external effect and the SQLite commit were atomic. An uncertain execution is
quarantined rather than retried. This store is not yet wired to CommandBus.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_RECORDS = 4096
MAX_EVIDENCE_BYTES = 65536
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class PlanReplayConflictError(ValueError):
    """An existing plan identity has a different fingerprint."""


class PlanInDoubtQuarantineError(RuntimeError):
    """An existing plan may have performed effects and must not be replayed."""


class PlanStoreCapacityError(RuntimeError):
    """The bounded store cannot accept another durable identity."""


class PlanStoreStateError(RuntimeError):
    """A requested transition is not permitted."""


@dataclass(frozen=True)
class DurablePlanRecord:
    plan_id: str
    plan_hash: str
    status: str
    external_effects: bool
    evidence_json: str | None


class DurablePlanDedupeStore:
    """One SQLite connection per operation, with atomic state transitions.

    Records and consumed nonces are never evicted: exhaustion fails closed.
    An ``EXECUTING`` identity observed by a competing/restarted reservation is
    changed to ``IN_DOUBT``. This may also quarantine a still-running executor;
    that conservative result is intentional because this store has no lease or
    external-effect fencing authority. Quarantine resolution is not provided.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) == ":memory:":
            raise ValueError("a persistent SQLite path is required")
        with self._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plans ("
                "plan_id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, "
                "status TEXT NOT NULL, external_effects INTEGER NOT NULL, evidence_json TEXT)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS consumed_nonces (nonce TEXT PRIMARY KEY)")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or mode[0] != "wal":
                raise PlanStoreStateError("SQLite WAL is required")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @staticmethod
    def _validate_identity(plan_id: str, plan_hash: str) -> None:
        if not isinstance(plan_id, str) or not 1 <= len(plan_id.encode("utf-8")) <= 256:
            raise ValueError("plan_id must contain 1 to 256 UTF-8 bytes")
        if not isinstance(plan_hash, str) or _HASH.fullmatch(plan_hash) is None:
            raise ValueError("plan_hash must be lowercase SHA-256 hex")

    def reserve(self, plan_id: str, plan_hash: str, *, external_effects: bool) -> DurablePlanRecord:
        """Reserve an identity or return a previously committed record.

        Redelivery is not authorized by this method. The caller must check the
        current actor and policy before returning any stored evidence.
        """
        self._validate_identity(plan_id, plan_hash)
        if not isinstance(external_effects, bool):
            raise ValueError("external_effects must be boolean")
        quarantine = False
        result: DurablePlanRecord | None = None
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT plan_hash, status, external_effects, evidence_json "
                "FROM plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                count = conn.execute("SELECT count(*) FROM plans").fetchone()[0]
                if count >= MAX_RECORDS:
                    raise PlanStoreCapacityError("plan record capacity reached")
                conn.execute(
                    "INSERT INTO plans VALUES (?, ?, 'RESERVED', ?, NULL)",
                    (plan_id, plan_hash, int(external_effects)),
                )
                result = DurablePlanRecord(plan_id, plan_hash, "RESERVED", external_effects, None)
            else:
                stored_hash, status, effects, evidence = row
                if stored_hash != plan_hash or bool(effects) != external_effects:
                    raise PlanReplayConflictError("plan identity differs from stored identity")
                if status == "EXECUTING":
                    conn.execute("UPDATE plans SET status='IN_DOUBT' WHERE plan_id=?", (plan_id,))
                    quarantine = True
                elif status == "COMMITTED":
                    result = DurablePlanRecord(
                        plan_id, stored_hash, status, bool(effects), evidence
                    )
                else:
                    quarantine = True
        if quarantine:
            raise PlanInDoubtQuarantineError("plan is not safely replayable")
        assert result is not None
        return result

    def start(self, plan_id: str, plan_hash: str, *, nonce: str) -> None:
        """Atomically consume a nonce and transition RESERVED to EXECUTING."""
        self._validate_identity(plan_id, plan_hash)
        if not isinstance(nonce, str) or not 1 <= len(nonce.encode("utf-8")) <= 256:
            raise ValueError("nonce must contain 1 to 256 UTF-8 bytes")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT plan_hash, status FROM plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if row is None or row[0] != plan_hash or row[1] != "RESERVED":
                raise PlanStoreStateError("plan is not reserved for this fingerprint")
            try:
                conn.execute("INSERT INTO consumed_nonces VALUES (?)", (nonce,))
            except sqlite3.IntegrityError as exc:
                raise PlanStoreStateError("nonce already consumed") from exc
            conn.execute("UPDATE plans SET status='EXECUTING' WHERE plan_id=?", (plan_id,))

    def finish(self, plan_id: str, plan_hash: str, *, evidence: Any) -> None:
        """Record canonical bounded JSON only for the executing identity."""
        self._validate_identity(plan_id, plan_hash)
        body = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(body.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise ValueError("evidence exceeds the byte limit")
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE plans SET status='COMMITTED', evidence_json=? "
                "WHERE plan_id=? AND plan_hash=? AND status='EXECUTING'",
                (body, plan_id, plan_hash),
            ).rowcount
            if changed != 1:
                raise PlanStoreStateError("plan is not executing for this fingerprint")

    def quarantine(self, plan_id: str, plan_hash: str) -> None:
        """Persist a tombstone for a failed or uncertain executing plan."""
        self._validate_identity(plan_id, plan_hash)
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE plans SET status='IN_DOUBT' "
                "WHERE plan_id=? AND plan_hash=? AND status='EXECUTING'",
                (plan_id, plan_hash),
            ).rowcount
            if changed != 1:
                raise PlanStoreStateError("plan is not executing for this fingerprint")
