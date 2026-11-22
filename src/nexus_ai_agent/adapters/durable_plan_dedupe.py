"""Bounded SQLite plan reservations; not an authorization or execution service.

A caller must independently authorize both first execution and redelivery. A
committed record proves only that its caller recorded a result, not that an
external effect and the SQLite commit were atomic. An uncertain execution is
quarantined rather than retried. This adapter is not yet wired to CommandBus.
Cooperating processes use a POSIX advisory lock; other SQLite clients do not
participate in that lock, and filesystem paths still require a trusted parent.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.studio.durable_dedupe import (
    DurablePlanRecord,
    PlanInDoubtQuarantineError,
    PlanReplayConflictError,
    PlanStoreCapacityError,
    PlanStoreStateError,
)
from nexus_ai_agent.creative.studio.plan_identifiers import validate_plan_identity

MAX_RECORDS = 4096
MAX_EVIDENCE_BYTES = 65536
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _precheck_evidence_bounds(
    obj: Any, *, max_bytes: int = MAX_EVIDENCE_BYTES, max_depth: int = 32
) -> None:
    """Bound encoder input before it can allocate a large escaped string.

    This walks existing JSON data without copying it. A length check rejects an
    oversized string before iteration; UTF-8 length is then counted per code
    point without allocating the encoded string. Container depth and the number
    of visited nodes are bounded too, including for cyclic data. Boolean values
    remain valid JSON booleans; no numeric schema is inferred at this layer.
    """
    remaining = max_bytes

    def charge(size: int) -> None:
        nonlocal remaining
        remaining -= size
        if remaining < 0:
            raise ValueError("evidence exceeds the byte limit")

    def visit(value: Any, depth: int) -> None:
        if depth > max_depth:
            raise ValueError("evidence exceeds the depth limit")
        charge(1)
        if type(value) is str:
            if len(value) > max_bytes:
                raise ValueError("evidence exceeds the byte limit")
            for char in value:
                code = ord(char)
                if code < 0x80:
                    charge(1)
                elif code < 0x800:
                    charge(2)
                elif 0xD800 <= code <= 0xDFFF:
                    raise ValueError("evidence contains invalid Unicode")
                elif code < 0x10000:
                    charge(3)
                else:
                    charge(4)
        elif value is None or type(value) is bool:
            pass
        elif type(value) is int:
            if value.bit_length() > max_bytes * 4:
                raise ValueError("evidence exceeds the byte limit")
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError("evidence must contain finite numbers")
        elif type(value) in (list, tuple):
            for item in value:
                visit(item, depth + 1)
        elif type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("evidence keys must be strings")
                visit(key, depth + 1)
                visit(item, depth + 1)
        else:
            raise ValueError("evidence must contain JSON data")

    visit(obj, 0)


class DurablePlanDedupeStore:
    """One SQLite connection per operation, with atomic state transitions.

    Records and consumed nonces are never evicted: exhaustion fails closed.
    An ``EXECUTING`` identity observed by a competing/restarted reservation is
    changed to ``IN_DOUBT``. This may also quarantine a still-running executor;
    that conservative result is intentional because this store has no lease or
    external-effect fencing authority. Quarantine resolution is not provided.
    """

    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("a persistent SQLite path is required")
        candidate = Path(path).expanduser().absolute()
        if candidate.is_symlink():
            raise ValueError("SQLite path must not be a symlink")
        self._check_ancestors(candidate.parent)
        self.path = candidate.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._check_ancestors(candidate.parent)
        info = self.path.parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(
                "SQLite parent must be owned by this user and not group/world writable"
            )
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("SQLite path must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._transaction() as conn:
            self._ensure_schema(conn)

    @staticmethod
    def _check_ancestors(parent: Path) -> None:
        for ancestor in (parent, *parent.parents):
            if ancestor.is_symlink():
                raise ValueError("SQLite parent path must not contain a symlink")

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        # Upgrade the provisional pre-tenant schema atomically. Existing records
        # and nonces belong to the legacy default project, never to every tenant.
        plans = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
        nonces = {row[1] for row in conn.execute("PRAGMA table_info(consumed_nonces)")}
        if plans and "project_id" not in plans:
            conn.execute("ALTER TABLE plans RENAME TO plans_legacy")
        if nonces and "project_id" not in nonces:
            conn.execute("ALTER TABLE consumed_nonces RENAME TO consumed_nonces_legacy")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plans ("
            "project_id TEXT NOT NULL, plan_id TEXT NOT NULL, plan_hash TEXT NOT NULL, "
            "status TEXT NOT NULL, external_effects INTEGER NOT NULL, evidence_json TEXT, "
            "PRIMARY KEY(project_id, plan_id))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS consumed_nonces ("
            "project_id TEXT NOT NULL, nonce TEXT NOT NULL, PRIMARY KEY(project_id, nonce))"
        )
        if plans and "project_id" not in plans:
            conn.execute(
                "INSERT INTO plans SELECT 'default', plan_id, plan_hash, status, "
                "external_effects, evidence_json FROM plans_legacy"
            )
            conn.execute("DROP TABLE plans_legacy")
        if nonces and "project_id" not in nonces:
            conn.execute(
                "INSERT INTO consumed_nonces SELECT 'default', nonce FROM consumed_nonces_legacy"
            )
            conn.execute("DROP TABLE consumed_nonces_legacy")

    def _protect_sidecars(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        for suffix in ("-wal", "-shm"):
            try:
                fd = os.open(f"{self.path}{suffix}", flags)
            except FileNotFoundError:
                continue
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("SQLite sidecar must be a regular file")
                os.fchmod(fd, 0o600)
            finally:
                os.close(fd)

    def _open_transaction(self) -> sqlite3.Connection:
        # journal_mode can raise SQLITE_BUSY even with a busy handler during
        # competing first opens. Only retry setup, never retry a caller's work.
        deadline = time.monotonic() + 2.0
        while True:
            conn: sqlite3.Connection | None = None
            try:
                conn = sqlite3.connect(str(self.path), timeout=0.1, isolation_level=None)
                conn.execute("PRAGMA busy_timeout=100")
                mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                if mode is None or mode[0] != "wal":
                    raise PlanStoreStateError("SQLite WAL is required")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("PRAGMA busy_timeout=5000")
                return conn
            except sqlite3.OperationalError as exc:
                if conn is not None:
                    conn.close()
                remaining = deadline - time.monotonic()
                retryable = any(word in str(exc).lower() for word in ("locked", "busy")) or (
                    getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_PROTOCOL
                )
                if remaining <= 0 or not retryable:
                    raise
                time.sleep(min(0.01, remaining))
            except BaseException:
                if conn is not None:
                    conn.close()
                raise

    @contextmanager
    def _store_lock(self) -> Iterator[None]:
        # Keep a stable, private inode across restarts. Unlinking a lock file
        # would let two processes acquire locks on different inodes.
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(f"{self.path}.lock", flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("SQLite lock must be a regular file")
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PlanStoreStateError("SQLite store lock timed out") from None
                    time.sleep(min(0.01, remaining))
            yield
        finally:
            os.close(fd)  # closes and releases the advisory lock, even on control errors

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # In addition to SQLite's transaction, coordinate cooperating openers
        # so the first WAL initialization cannot race another connection.
        with self._store_lock():
            conn = self._open_transaction()
            try:
                try:
                    self._protect_sidecars()
                    yield conn
                    self._protect_sidecars()
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()

    @staticmethod
    def _validate_identity(plan_id: str, plan_hash: str, project_id: str) -> None:
        validate_plan_identity(plan_id, name="plan_id")
        validate_plan_identity(project_id, name="project_id")
        if not isinstance(plan_hash, str) or _HASH.fullmatch(plan_hash) is None:
            raise ValueError("plan_hash must be lowercase SHA-256 hex")

    def reserve(
        self, plan_id: str, plan_hash: str, *, external_effects: bool, project_id: str = "default"
    ) -> DurablePlanRecord:
        """Reserve an identity or return a previously committed record.

        Redelivery is not authorized by this method. The caller must check the
        current actor and policy before returning any stored evidence.
        """
        self._validate_identity(plan_id, plan_hash, project_id)
        if not isinstance(external_effects, bool):
            raise ValueError("external_effects must be boolean")
        quarantine = False
        result: DurablePlanRecord | None = None
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT plan_hash, status, external_effects, evidence_json "
                "FROM plans WHERE project_id=? AND plan_id=?",
                (project_id, plan_id),
            ).fetchone()
            if row is None:
                count = conn.execute("SELECT count(*) FROM plans").fetchone()[0]
                if count >= MAX_RECORDS:
                    raise PlanStoreCapacityError("plan record capacity reached")
                conn.execute(
                    "INSERT INTO plans VALUES (?, ?, ?, 'RESERVED', ?, NULL)",
                    (project_id, plan_id, plan_hash, int(external_effects)),
                )
                result = DurablePlanRecord(plan_id, plan_hash, "RESERVED", external_effects, None)
            else:
                stored_hash, status, effects, evidence = row
                if stored_hash != plan_hash or bool(effects) != external_effects:
                    raise PlanReplayConflictError("plan identity differs from stored identity")
                if status == "EXECUTING":
                    conn.execute(
                        "UPDATE plans SET status='IN_DOUBT' WHERE project_id=? AND plan_id=?",
                        (project_id, plan_id),
                    )
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

    def start(
        self, plan_id: str, plan_hash: str, *, nonce: str, project_id: str = "default"
    ) -> None:
        """Atomically consume a nonce and transition RESERVED to EXECUTING."""
        self._validate_identity(plan_id, plan_hash, project_id)
        if not isinstance(nonce, str) or not 1 <= len(nonce.encode("utf-8")) <= 256:
            raise ValueError("nonce must contain 1 to 256 UTF-8 bytes")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT plan_hash, status FROM plans WHERE project_id=? AND plan_id=?",
                (project_id, plan_id),
            ).fetchone()
            if row is None or row[0] != plan_hash or row[1] != "RESERVED":
                raise PlanStoreStateError("plan is not reserved for this fingerprint")
            try:
                conn.execute("INSERT INTO consumed_nonces VALUES (?, ?)", (project_id, nonce))
            except sqlite3.IntegrityError as exc:
                raise PlanStoreStateError("nonce already consumed") from exc
            conn.execute(
                "UPDATE plans SET status='EXECUTING' WHERE project_id=? AND plan_id=?",
                (project_id, plan_id),
            )

    def finish(
        self, plan_id: str, plan_hash: str, *, evidence: Any, project_id: str = "default"
    ) -> None:
        """Record canonical bounded JSON only for the executing identity."""
        self._validate_identity(plan_id, plan_hash, project_id)
        _precheck_evidence_bounds(evidence, max_bytes=MAX_EVIDENCE_BYTES)
        encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
        pieces: list[str] = []
        size = 0
        for piece in encoder.iterencode(evidence):
            if len(piece) > MAX_EVIDENCE_BYTES:
                raise ValueError("evidence exceeds the byte limit")
            size += len(piece.encode("utf-8"))
            if size > MAX_EVIDENCE_BYTES:
                raise ValueError("evidence exceeds the byte limit")
            pieces.append(piece)
        body = "".join(pieces)
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE plans SET status='COMMITTED', evidence_json=? "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? AND status='EXECUTING'",
                (body, project_id, plan_id, plan_hash),
            ).rowcount
            if changed != 1:
                raise PlanStoreStateError("plan is not executing for this fingerprint")

    def quarantine(self, plan_id: str, plan_hash: str, *, project_id: str = "default") -> None:
        """Persist a tombstone for a failed or uncertain executing plan."""
        self._validate_identity(plan_id, plan_hash, project_id)
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE plans SET status='IN_DOUBT' "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? AND status='EXECUTING'",
                (project_id, plan_id, plan_hash),
            ).rowcount
            if changed != 1:
                raise PlanStoreStateError("plan is not executing for this fingerprint")
