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
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from nexus_ai_agent.creative.studio.durable_dedupe import (
    CompensationJournalEntry,
    DurablePlanRecord,
    ExecutionLease,
    ExternalEffectObservation,
    PlanExecutionInProgressError,
    PlanFencingTokenMismatchError,
    PlanInDoubtQuarantineError,
    PlanReplayConflictError,
    PlanStoreCapacityError,
    PlanStoreStateError,
    RecoveryEvidence,
    RecoveryRoute,
    VerifiedEffectRecoveryEvidence,
)
from nexus_ai_agent.creative.studio.plan_identifiers import validate_plan_identity

MAX_RECORDS = 4096
MAX_EVIDENCE_BYTES = 65536
MAX_COMPENSATION_ENTRIES = 131072
MAX_APPROVAL_NONCES = 128
MAX_CONSUMED_NONCES = MAX_RECORDS * MAX_APPROVAL_NONCES
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PROCESS_CLOCK_DOMAIN = "process-" + secrets.token_hex(16)


def _monotonic_clock_domain() -> str:
    """Bind leases to the kernel boot, or conservatively to this process."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open("/proc/sys/kernel/random/boot_id", flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PlanStoreStateError("invalid kernel boot identity")
            boot_id = os.read(fd, 65).decode("ascii").strip()
        finally:
            os.close(fd)
    except FileNotFoundError:
        # On systems without /proc, a restart quarantines active leases rather
        # than comparing incompatible monotonic clocks across processes.
        return _PROCESS_CLOCK_DOMAIN
    except (OSError, UnicodeError) as exc:
        raise PlanStoreStateError("kernel boot identity is unavailable") from exc
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id) is None:
        raise PlanStoreStateError("invalid kernel boot identity")
    return "monotonic-ns-v1:" + boot_id


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


def _encode_evidence(evidence: Any) -> str:
    """Encode bounded JSON without materializing an unbounded input."""
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
    return "".join(pieces)


class DurablePlanDedupeStore:
    """One SQLite connection per operation, with atomic state transitions.

    Records and consumed nonces are never evicted: exhaustion fails closed.
    Live leases refuse duplicates without mutating the owner. Expired or legacy
    executions are quarantined. Fencing protects database transitions only; it
    cannot fence arbitrary external effects, which need their own authority.
    Injected clocks are for controlled environments: every instance sharing a
    database must use the same monotonic clock domain. Without a Linux boot ID,
    separate processes use different domains and quarantine each other's leases;
    sharing active work across those processes is therefore unsupported.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] | None = None,
        repair_authorizer: Callable[[str, str, str, int], bool] | None = None,
        recovery_routes: Mapping[tuple[str, str], RecoveryRoute] | None = None,
    ) -> None:
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if repair_authorizer is not None and not callable(repair_authorizer):
            raise ValueError("repair_authorizer must be callable")
        self._clock: Callable[[], float] = (
            clock if clock is not None else lambda: time.monotonic_ns() / 1e9
        )
        self._initialized = False
        self._repair_authorizer = repair_authorizer
        self._recovery_routes = dict(recovery_routes or {})
        for (project_id, step_id), route in self._recovery_routes.items():
            validate_plan_identity(project_id, name="project_id")
            validate_plan_identity(step_id, name="step_id")
            if not isinstance(route, RecoveryRoute):
                raise ValueError("recovery route must be configured by trusted composition")
            validate_plan_identity(route.verifier_id, name="verifier_id")
            if cast(object, route.destination) is cast(object, route.verifier):
                raise ValueError("recovery verifier must be separate from destination")
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
        self._clock_domain = "injected" if clock is not None else _monotonic_clock_domain()
        with self._transaction() as conn:
            self._ensure_schema(conn)
            self._ensure_clock_domain(conn)
        self._initialized = True

    def _ensure_clock_domain(self, conn: sqlite3.Connection) -> None:
        """Never interpret an active lease from an incompatible clock domain."""
        conn.execute("CREATE TABLE IF NOT EXISTS plan_clock_domain (domain TEXT NOT NULL)")
        domains = conn.execute("SELECT domain FROM plan_clock_domain").fetchall()
        if len(domains) > 1:
            raise PlanStoreStateError("invalid lease clock domain metadata")
        if not domains or domains[0][0] != self._clock_domain:
            # Includes databases created by older versions using epoch clocks.
            conn.execute(
                "UPDATE plans SET status='IN_DOUBT' WHERE status IN ('EXECUTING','COMPENSATING') "
                "AND lease_expires_at IS NOT NULL"
            )
            conn.execute("DELETE FROM plan_clock_domain")
            conn.execute("INSERT INTO plan_clock_domain(domain) VALUES (?)", (self._clock_domain,))

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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS nonce_plan_bindings ("
            "project_id TEXT NOT NULL, nonce TEXT NOT NULL, plan_id TEXT NOT NULL, "
            "plan_hash TEXT NOT NULL, PRIMARY KEY(project_id, nonce))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plan_repair_events ("
            "event_id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, plan_id TEXT NOT NULL, "
            "plan_hash TEXT NOT NULL, evidence_json TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS plan_recovery_inspections ("
            "nonce_hash TEXT PRIMARY KEY, evidence_json TEXT NOT NULL, "
            "journal_digest TEXT NOT NULL, clock_domain TEXT NOT NULL, "
            "expires_monotonic REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS compensation_journal ("
            "entry_id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, plan_id TEXT NOT NULL, "
            "plan_hash TEXT NOT NULL, attempt INTEGER NOT NULL, step_id TEXT NOT NULL, "
            "effect_key TEXT NOT NULL, status TEXT NOT NULL, evidence_json TEXT, "
            "UNIQUE(project_id, plan_id, step_id), "
            "UNIQUE(project_id, plan_id, effect_key))"
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plans)")}
        for name, definition in (
            ("attempt", "INTEGER NOT NULL DEFAULT 0"),
            ("owner_token", "TEXT"),
            ("lease_expires_at", "REAL"),
            ("compensation_status", "TEXT"),
            ("compensation_evidence_json", "TEXT"),
            ("repair_evidence_json", "TEXT"),
            ("takeover_ready", "INTEGER NOT NULL DEFAULT 0"),
            ("takeover_epoch_reserved", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE plans ADD COLUMN {name} {definition}")

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
                    getattr(exc, "sqlite_errorcode", None)
                    == getattr(sqlite3, "SQLITE_PROTOCOL", 15)
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
                    if self._initialized:
                        domains = conn.execute("SELECT domain FROM plan_clock_domain").fetchall()
                        if domains != [(self._clock_domain,)]:
                            raise PlanStoreStateError("lease clock domain changed")
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
    def monotonic_ns() -> int:
        """Return a monotonic deadline sample in nanoseconds."""
        return time.monotonic_ns()

    @staticmethod
    def utc_timestamp() -> float:
        """Return a UTC epoch sample for independently verified approvals."""
        return datetime.now(timezone.utc).timestamp()

    @staticmethod
    def _validate_identity(plan_id: str, plan_hash: str, project_id: str) -> None:
        validate_plan_identity(plan_id, name="plan_id")
        validate_plan_identity(project_id, name="project_id")
        if not isinstance(plan_hash, str) or _HASH.fullmatch(plan_hash) is None:
            raise ValueError("plan_hash must be lowercase SHA-256 hex")

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise PlanStoreStateError("clock must return a finite epoch value")
        try:
            result = float(value)
        except OverflowError as exc:
            raise PlanStoreStateError("clock must return a finite epoch value") from exc
        if not math.isfinite(result):
            raise PlanStoreStateError("clock must return a finite epoch value")
        return result

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
                "SELECT plan_hash, status, external_effects, evidence_json, lease_expires_at "
                "FROM plans WHERE project_id=? AND plan_id=?",
                (project_id, plan_id),
            ).fetchone()
            if row is None:
                count = conn.execute("SELECT count(*) FROM plans").fetchone()[0]
                if count >= MAX_RECORDS:
                    raise PlanStoreCapacityError("plan record capacity reached")
                conn.execute(
                    "INSERT INTO plans (project_id, plan_id, plan_hash, status, external_effects) "
                    "VALUES (?, ?, ?, 'RESERVED', ?)",
                    (project_id, plan_id, plan_hash, int(external_effects)),
                )
                result = DurablePlanRecord(plan_id, plan_hash, "RESERVED", external_effects, None)
            else:
                stored_hash, status, effects, evidence, expiry = row
                if stored_hash != plan_hash or bool(effects) != external_effects:
                    raise PlanReplayConflictError("plan identity differs from stored identity")
                if status in ("EXECUTING", "COMPENSATING"):
                    if expiry is not None and self._now() < expiry:
                        raise PlanExecutionInProgressError("plan execution is still live")
                    conn.execute(
                        "UPDATE plans SET status='IN_DOUBT' WHERE project_id=? AND plan_id=? "
                        "AND plan_hash=? AND status=? AND lease_expires_at IS ?",
                        (project_id, plan_id, plan_hash, status, expiry),
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

    @staticmethod
    def _expiry(now: float, ttl: float) -> float:
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= 86400:
            raise ValueError("lease_ttl_seconds must be finite and within (0, 86400]")
        expires = now + ttl
        if not math.isfinite(expires) or expires <= now:
            raise ValueError("lease expiry is not finite and increasing")
        return expires

    @staticmethod
    def _validate_nonces(nonce: str, additional_nonces: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not isinstance(additional_nonces, tuple)
            or len(additional_nonces) >= MAX_APPROVAL_NONCES
        ):
            raise ValueError("additional_nonces must be a bounded tuple")
        nonces = (nonce, *additional_nonces)
        for value in nonces:
            if not isinstance(value, str) or not 1 <= len(value) <= 256:
                raise ValueError("nonce must contain 1 to 256 UTF-8 bytes")
            if len(value.encode("utf-8")) > 256:
                raise ValueError("nonce must contain 1 to 256 UTF-8 bytes")
        if len(set(nonces)) != len(nonces):
            raise PlanStoreStateError("nonce repeated within approval set")
        return nonces

    @staticmethod
    def _consume_nonces(
        conn: sqlite3.Connection,
        project_id: str,
        nonces: tuple[str, ...],
        plan_id: str,
        plan_hash: str,
    ) -> None:
        count = conn.execute("SELECT count(*) FROM consumed_nonces").fetchone()[0]
        if count + len(nonces) > MAX_CONSUMED_NONCES:
            raise PlanStoreCapacityError("nonce capacity reached")
        try:
            conn.executemany(
                "INSERT INTO consumed_nonces VALUES (?, ?)",
                ((project_id, value) for value in nonces),
            )
            conn.executemany(
                "INSERT INTO nonce_plan_bindings VALUES (?, ?, ?, ?)",
                ((project_id, value, plan_id, plan_hash) for value in nonces),
            )
        except sqlite3.IntegrityError as exc:
            raise PlanStoreStateError("nonce already consumed") from exc

    def reserve_and_start(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        external_effects: bool,
        nonce: str,
        project_id: str = "default",
        lease_ttl_seconds: float = 30.0,
        additional_nonces: tuple[str, ...] = (),
    ) -> ExecutionLease:
        """Atomically reserve a new identity and consume its execution nonces.

        Existing reservations are not stolen. An authorized operator may mark a
        quarantined record takeover-ready; only that state can be restarted.
        This API never returns previously committed evidence or authorizes an
        actor; authentication and approval must precede calling it.
        """
        self._validate_identity(plan_id, plan_hash, project_id)
        if not isinstance(external_effects, bool):
            raise ValueError("external_effects must be boolean")
        nonces = self._validate_nonces(nonce, additional_nonces)
        self._expiry(0.0, lease_ttl_seconds)
        quarantined = False
        lease: ExecutionLease | None = None
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT plan_hash, status, external_effects, attempt, lease_expires_at, "
                "takeover_ready, takeover_epoch_reserved FROM plans "
                "WHERE project_id=? AND plan_id=?",
                (project_id, plan_id),
            ).fetchone()
            now = self._now()
            if row is not None and (row[0] != plan_hash or bool(row[2]) != external_effects):
                raise PlanReplayConflictError("plan identity differs from stored identity")
            if row is not None and row[1] in ("EXECUTING", "COMPENSATING"):
                if row[4] is not None and now < row[4]:
                    raise PlanExecutionInProgressError("plan execution is still live")
                conn.execute(
                    "UPDATE plans SET status='IN_DOUBT' WHERE project_id=? AND plan_id=? "
                    "AND plan_hash=? AND status=? AND attempt=?",
                    (project_id, plan_id, plan_hash, row[1], row[3]),
                )
                quarantined = True
            elif row is not None and not (row[1] == "RESERVED" and row[5] == 1):
                raise PlanInDoubtQuarantineError("plan identity cannot be automatically replayed")
            else:
                if row is None:
                    if conn.execute("SELECT count(*) FROM plans").fetchone()[0] >= MAX_RECORDS:
                        raise PlanStoreCapacityError("plan record capacity reached")
                    attempt = 1
                else:
                    if row[6] != 1:
                        raise PlanStoreStateError("legacy takeover lacks recovery evidence")
                    if row[3] < 1 or row[3] >= 2**63 - 1:
                        raise PlanStoreStateError("execution attempt counter exhausted")
                    attempt = row[3]
                expires = self._expiry(now, lease_ttl_seconds)
                self._consume_nonces(conn, project_id, nonces, plan_id, plan_hash)
                owner = secrets.token_hex(32)
                if row is None:
                    conn.execute(
                        "INSERT INTO plans (project_id, plan_id, plan_hash, status, "
                        "external_effects, attempt, owner_token, lease_expires_at) "
                        "VALUES (?, ?, ?, 'EXECUTING', ?, ?, ?, ?)",
                        (
                            project_id,
                            plan_id,
                            plan_hash,
                            int(external_effects),
                            attempt,
                            owner,
                            expires,
                        ),
                    )
                else:
                    changed = conn.execute(
                        "UPDATE plans SET status='EXECUTING', attempt=?, owner_token=?, "
                        "lease_expires_at=?, takeover_ready=0, takeover_epoch_reserved=0 "
                        "WHERE project_id=? AND plan_id=? AND plan_hash=? AND status='RESERVED' "
                        "AND takeover_ready=1 AND takeover_epoch_reserved=1 AND attempt=?",
                        (attempt, owner, expires, project_id, plan_id, plan_hash, row[3]),
                    ).rowcount
                    if changed != 1:
                        raise PlanStoreStateError("takeover changed while starting")
                lease = ExecutionLease(project_id, plan_id, plan_hash, attempt, owner, expires)
        if quarantined:
            raise PlanInDoubtQuarantineError("expired execution is quarantined")
        assert lease is not None
        return lease

    def find_committed(
        self, plan_id: str, plan_hash: str, *, project_id: str
    ) -> DurablePlanRecord | None:
        """Look up a receipt without mutation; caller must authorize disclosure."""
        self._validate_identity(plan_id, plan_hash, project_id)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT plan_hash, status, external_effects, evidence_json FROM plans "
                "WHERE project_id=? AND plan_id=?",
                (project_id, plan_id),
            ).fetchone()
            if row is None or row[1] != "COMMITTED":
                return None
            if row[0] != plan_hash:
                raise PlanReplayConflictError("plan identity differs from stored identity")
            if row[3] is None:
                raise PlanStoreStateError("committed receipt is missing")
            return DurablePlanRecord(plan_id, row[0], row[1], bool(row[2]), row[3])

    def replay_committed(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        external_effects: bool,
        nonce: str,
        project_id: str = "default",
        additional_nonces: tuple[str, ...] = (),
    ) -> DurablePlanRecord:
        """Return a committed receipt only for fresh or same-plan approval nonces.

        The caller must independently authenticate actor and approvals. Legacy
        consumed nonces without a recorded plan binding cannot be replayed.
        """
        self._validate_identity(plan_id, plan_hash, project_id)
        if not isinstance(external_effects, bool):
            raise ValueError("external_effects must be boolean")
        nonces = self._validate_nonces(nonce, additional_nonces)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT plan_hash, status, external_effects, evidence_json "
                "FROM plans WHERE project_id=? AND plan_id=?",
                (project_id, plan_id),
            ).fetchone()
            if row is None:
                raise PlanStoreStateError("committed plan not found")
            if row[0] != plan_hash or bool(row[2]) != external_effects:
                raise PlanReplayConflictError("plan identity differs from stored identity")
            if row[1] != "COMMITTED" or row[3] is None:
                raise PlanStoreStateError("plan has no committed receipt")
            fresh: list[str] = []
            for value in nonces:
                consumed = conn.execute(
                    "SELECT 1 FROM consumed_nonces WHERE project_id=? AND nonce=?",
                    (project_id, value),
                ).fetchone()
                if consumed is None:
                    fresh.append(value)
                    continue
                binding = conn.execute(
                    "SELECT plan_id, plan_hash FROM nonce_plan_bindings "
                    "WHERE project_id=? AND nonce=?",
                    (project_id, value),
                ).fetchone()
                if binding != (plan_id, plan_hash):
                    raise PlanStoreStateError("nonce already consumed by another or unknown plan")
            self._consume_nonces(conn, project_id, tuple(fresh), plan_id, plan_hash)
            return DurablePlanRecord(plan_id, plan_hash, row[1], bool(row[2]), row[3])

    def start(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        nonce: str,
        project_id: str = "default",
        lease_ttl_seconds: float = 30.0,
    ) -> ExecutionLease:
        """Consume a nonce and issue a fenced, persistent execution lease."""
        self._validate_identity(plan_id, plan_hash, project_id)
        if not isinstance(nonce, str) or not 1 <= len(nonce.encode("utf-8")) <= 256:
            raise ValueError("nonce must contain 1 to 256 UTF-8 bytes")
        # Validate before taking the database lock, without consulting the clock.
        self._expiry(0.0, lease_ttl_seconds)
        expired = False
        lease: ExecutionLease | None = None
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT plan_hash, status, attempt, lease_expires_at, takeover_ready, "
                "takeover_epoch_reserved FROM plans WHERE project_id=? AND plan_id=?",
                (project_id, plan_id),
            ).fetchone()
            if row is None or row[0] != plan_hash:
                raise PlanStoreStateError("plan is not reserved for this fingerprint")
            now = self._now()
            if row[1] in ("EXECUTING", "COMPENSATING"):
                if row[3] is not None and now < row[3]:
                    raise PlanExecutionInProgressError("plan execution is still live")
                conn.execute(
                    "UPDATE plans SET status='IN_DOUBT' WHERE project_id=? AND plan_id=? "
                    "AND plan_hash=? AND status=? AND attempt=?",
                    (project_id, plan_id, plan_hash, row[1], row[2]),
                )
                expired = True
            elif row[1] != "RESERVED":
                raise PlanStoreStateError("plan is not reserved for this fingerprint")
            else:
                if row[2] < 0 or row[2] >= 2**63 - 1:
                    raise PlanStoreStateError("execution attempt counter exhausted")
                if row[4] and row[5] != 1:
                    raise PlanStoreStateError("legacy takeover lacks recovery evidence")
                expires = self._expiry(now, lease_ttl_seconds)
                self._consume_nonces(conn, project_id, (nonce,), plan_id, plan_hash)
                attempt = row[2] if row[4] else row[2] + 1
                owner = secrets.token_hex(32)
                changed = conn.execute(
                    "UPDATE plans SET status='EXECUTING', attempt=?, owner_token=?, "
                    "lease_expires_at=?, takeover_ready=0, takeover_epoch_reserved=0 "
                    "WHERE project_id=? AND plan_id=? AND plan_hash=? "
                    "AND status='RESERVED' AND attempt=?",
                    (attempt, owner, expires, project_id, plan_id, plan_hash, row[2]),
                ).rowcount
                if changed != 1:
                    raise PlanStoreStateError("reservation changed while starting")
                lease = ExecutionLease(project_id, plan_id, plan_hash, attempt, owner, expires)
        if expired:
            raise PlanInDoubtQuarantineError("expired execution is quarantined")
        assert lease is not None
        return lease

    @staticmethod
    def _validate_fence(attempt: int, owner_token: str) -> None:
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= 2**63 - 1
            or not isinstance(owner_token, str)
            or _HASH.fullmatch(owner_token) is None
        ):
            raise PlanFencingTokenMismatchError("invalid execution fence")

    def heartbeat(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        project_id: str = "default",
        lease_ttl_seconds: float = 30.0,
    ) -> ExecutionLease:
        """Check ownership and extend a live execution without reviving an expired lease."""
        self._validate_identity(plan_id, plan_hash, project_id)
        self._validate_fence(attempt, owner_token)
        self._expiry(0.0, lease_ttl_seconds)
        with self._transaction() as conn:
            now = self._now()
            expiry = self._expiry(now, lease_ttl_seconds)
            changed = conn.execute(
                "UPDATE plans SET lease_expires_at=MAX(lease_expires_at, ?) "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? "
                "AND status IN ('EXECUTING', 'COMPENSATING') "
                "AND attempt=? AND owner_token=? AND lease_expires_at>?",
                (expiry, project_id, plan_id, plan_hash, attempt, owner_token, now),
            ).rowcount
            if changed != 1:
                raise PlanFencingTokenMismatchError(
                    "execution is stale or its owner does not match"
                )
            row = conn.execute(
                "SELECT lease_expires_at FROM plans WHERE project_id=? AND plan_id=?",
                (project_id, plan_id),
            ).fetchone()
            assert row is not None
            return ExecutionLease(project_id, plan_id, plan_hash, attempt, owner_token, row[0])

    def finish(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        evidence: Any,
        project_id: str = "default",
        deadline_monotonic_ns: int | None = None,
    ) -> None:
        """Record bounded evidence only for the current, unexpired owner."""
        self._validate_identity(plan_id, plan_hash, project_id)
        self._validate_fence(attempt, owner_token)
        if deadline_monotonic_ns is not None and (
            isinstance(deadline_monotonic_ns, bool) or not isinstance(deadline_monotonic_ns, int)
        ):
            raise ValueError("deadline_monotonic_ns must be an integer")
        body = _encode_evidence(evidence)
        unresolved = False
        with self._transaction() as conn:
            # A pending or failed journal entry remains unresolved, never a receipt.
            # Check and transition under the same transaction and live fence.
            now = self._now()
            live = conn.execute(
                "SELECT 1 FROM plans WHERE project_id=? AND plan_id=? AND plan_hash=? "
                "AND status='EXECUTING' AND attempt=? AND owner_token=? AND lease_expires_at>?",
                (project_id, plan_id, plan_hash, attempt, owner_token, now),
            ).fetchone()
            if live is None:
                raise PlanFencingTokenMismatchError(
                    "execution is stale or its owner does not match"
                )
            unresolved = (
                conn.execute(
                    "SELECT 1 FROM compensation_journal WHERE project_id=? AND plan_id=? "
                    "AND plan_hash=? AND ((attempt=? AND status!='VERIFIED') "
                    "OR (attempt!=? AND status NOT IN ('COMPENSATED','NOT_APPLIED'))) LIMIT 1",
                    (project_id, plan_id, plan_hash, attempt, attempt),
                ).fetchone()
                is not None
            )
            if deadline_monotonic_ns is not None and time.monotonic_ns() >= deadline_monotonic_ns:
                raise TimeoutError("durable plan deadline expired")
            changed = conn.execute(
                "UPDATE plans SET status=?, evidence_json=? "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? AND status='EXECUTING' "
                "AND attempt=? AND owner_token=? AND lease_expires_at>?",
                (
                    "IN_DOUBT" if unresolved else "COMMITTED",
                    None if unresolved else body,
                    project_id,
                    plan_id,
                    plan_hash,
                    attempt,
                    owner_token,
                    self._now(),
                ),
            ).rowcount
            if changed != 1:
                raise PlanFencingTokenMismatchError(
                    "execution is stale or its owner does not match"
                )
        if unresolved:
            raise PlanInDoubtQuarantineError("cannot finish with an unresolved effect intent")

    def quarantine(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        project_id: str = "default",
    ) -> None:
        """Persist a tombstone only for the matching execution owner."""
        self._validate_identity(plan_id, plan_hash, project_id)
        self._validate_fence(attempt, owner_token)
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE plans SET status='IN_DOUBT' "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? "
                "AND status IN ('EXECUTING', 'COMPENSATING') "
                "AND attempt=? AND owner_token=? AND lease_expires_at>?",
                (project_id, plan_id, plan_hash, attempt, owner_token, self._now()),
            ).rowcount
            if changed != 1:
                raise PlanFencingTokenMismatchError(
                    "execution is stale or its owner does not match"
                )

    def mark_compensating(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        project_id: str = "default",
    ) -> None:
        """Persist the start of compensation under the current live fence."""
        self._validate_identity(plan_id, plan_hash, project_id)
        self._validate_fence(attempt, owner_token)
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE plans SET status='COMPENSATING', compensation_status='STARTED' "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? AND status='EXECUTING' "
                "AND attempt=? AND owner_token=? AND lease_expires_at>?",
                (project_id, plan_id, plan_hash, attempt, owner_token, self._now()),
            ).rowcount
            if changed != 1:
                raise PlanFencingTokenMismatchError(
                    "execution is stale or its owner does not match"
                )

    def record_compensation_intent(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        step_id: str,
        effect_key: str,
        project_id: str = "default",
    ) -> None:
        """Durably record intent before a forward or compensation effect.

        Reusing a step or effect key is refused, including after a crash. A
        PENDING entry is uncertain, not permission to repeat an effect.
        """
        self._validate_identity(plan_id, plan_hash, project_id)
        self._validate_fence(attempt, owner_token)
        validate_plan_identity(step_id, name="step_id")
        if not isinstance(effect_key, str) or _HASH.fullmatch(effect_key) is None:
            raise ValueError("effect_key must be lowercase SHA-256 hex")
        with self._transaction() as conn:
            reused = conn.execute(
                "UPDATE compensation_journal SET attempt=?,status='PENDING',evidence_json=NULL "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? AND step_id=? AND effect_key=? "
                "AND status='NOT_APPLIED' AND attempt<? AND EXISTS (SELECT 1 FROM plans "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? "
                "AND status IN ('EXECUTING','COMPENSATING') AND attempt=? AND owner_token=? "
                "AND lease_expires_at>?)",
                (
                    attempt,
                    project_id,
                    plan_id,
                    plan_hash,
                    step_id,
                    effect_key,
                    attempt,
                    project_id,
                    plan_id,
                    plan_hash,
                    attempt,
                    owner_token,
                    self._now(),
                ),
            ).rowcount
            if reused == 1:
                return
            if (
                conn.execute("SELECT count(*) FROM compensation_journal").fetchone()[0]
                >= MAX_COMPENSATION_ENTRIES
            ):
                raise PlanStoreCapacityError("compensation journal capacity reached")
            try:
                changed = conn.execute(
                    "INSERT INTO compensation_journal "
                    "(project_id, plan_id, plan_hash, attempt, step_id, effect_key, status) "
                    "SELECT project_id, plan_id, plan_hash, attempt, ?, ?, 'PENDING' FROM plans "
                    "WHERE project_id=? AND plan_id=? AND plan_hash=? "
                    "AND status IN ('EXECUTING', 'COMPENSATING') "
                    "AND attempt=? AND owner_token=? AND lease_expires_at>?",
                    (
                        step_id,
                        effect_key,
                        project_id,
                        plan_id,
                        plan_hash,
                        attempt,
                        owner_token,
                        self._now(),
                    ),
                ).rowcount
            except sqlite3.IntegrityError as exc:
                raise PlanStoreStateError("compensation intent already exists") from exc
            if changed != 1:
                raise PlanFencingTokenMismatchError(
                    "compensation is stale or its owner does not match"
                )

    def verify_effect_intent(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        step_id: str,
        effect_key: str,
        verification_digest: str,
        project_id: str = "default",
        deadline_monotonic_ns: int | None = None,
    ) -> None:
        """Persist an independently verified forward effect behind a live fence.

        The caller must actually verify the effect; the digest by itself is not
        proof of verification. A pending intent is never retried automatically.
        """
        self._validate_identity(plan_id, plan_hash, project_id)
        self._validate_fence(attempt, owner_token)
        validate_plan_identity(step_id, name="step_id")
        if not isinstance(effect_key, str) or _HASH.fullmatch(effect_key) is None:
            raise ValueError("effect_key must be lowercase SHA-256 hex")
        if not isinstance(verification_digest, str) or _HASH.fullmatch(verification_digest) is None:
            raise ValueError("verification_digest must be lowercase SHA-256 hex")
        if deadline_monotonic_ns is not None and (
            isinstance(deadline_monotonic_ns, bool) or not isinstance(deadline_monotonic_ns, int)
        ):
            raise ValueError("deadline_monotonic_ns must be an integer")
        body = _encode_evidence(
            {
                "verification_digest": verification_digest,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        with self._transaction() as conn:
            if deadline_monotonic_ns is not None and time.monotonic_ns() >= deadline_monotonic_ns:
                raise TimeoutError("durable plan deadline expired")
            changed = conn.execute(
                "UPDATE compensation_journal SET status='VERIFIED', evidence_json=? "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? AND attempt=? "
                "AND step_id=? AND effect_key=? AND status='PENDING' AND EXISTS ("
                "SELECT 1 FROM plans WHERE plans.project_id=compensation_journal.project_id "
                "AND plans.plan_id=compensation_journal.plan_id "
                "AND plans.plan_hash=compensation_journal.plan_hash AND plans.status='EXECUTING' "
                "AND plans.attempt=? AND plans.owner_token=? AND plans.lease_expires_at>?)",
                (
                    body,
                    project_id,
                    plan_id,
                    plan_hash,
                    attempt,
                    step_id,
                    effect_key,
                    attempt,
                    owner_token,
                    self._now(),
                ),
            ).rowcount
            if changed != 1:
                raise PlanFencingTokenMismatchError("pending intent or live fence does not match")

    def record_compensation_step(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        step_id: str,
        effect_key: str,
        succeeded: bool,
        evidence: Any,
        project_id: str = "default",
    ) -> None:
        """Finish one pending intent only while the matching fence is live."""
        self._validate_identity(plan_id, plan_hash, project_id)
        self._validate_fence(attempt, owner_token)
        validate_plan_identity(step_id, name="step_id")
        if not isinstance(effect_key, str) or _HASH.fullmatch(effect_key) is None:
            raise ValueError("effect_key must be lowercase SHA-256 hex")
        if not isinstance(succeeded, bool):
            raise ValueError("succeeded must be boolean")
        body = _encode_evidence(evidence)
        status = "COMPENSATED" if succeeded else "FAILED"
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE compensation_journal SET status=?, evidence_json=? "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? AND attempt=? "
                "AND step_id=? AND effect_key=? AND status IN ('PENDING','VERIFIED') AND EXISTS ("
                "SELECT 1 FROM plans WHERE plans.project_id=compensation_journal.project_id "
                "AND plans.plan_id=compensation_journal.plan_id "
                "AND plans.plan_hash=compensation_journal.plan_hash "
                "AND plans.status='COMPENSATING' AND plans.attempt=? "
                "AND plans.owner_token=? AND plans.lease_expires_at>?)",
                (
                    status,
                    body,
                    project_id,
                    plan_id,
                    plan_hash,
                    attempt,
                    step_id,
                    effect_key,
                    attempt,
                    owner_token,
                    self._now(),
                ),
            ).rowcount
            if changed != 1:
                raise PlanFencingTokenMismatchError(
                    "pending compensation or live fence does not match"
                )

    def compensation_journal(
        self, plan_id: str, plan_hash: str, *, project_id: str = "default"
    ) -> tuple[CompensationJournalEntry, ...]:
        """Return ordered evidence; the caller must authorize its disclosure."""
        self._validate_identity(plan_id, plan_hash, project_id)
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT step_id, effect_key, attempt, status, evidence_json "
                "FROM compensation_journal WHERE project_id=? AND plan_id=? AND plan_hash=? "
                "ORDER BY entry_id",
                (project_id, plan_id, plan_hash),
            ).fetchall()
        return tuple(CompensationJournalEntry(*row) for row in rows)

    def quarantined_journal(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        expected_status: str,
        expected_attempt: int,
        project_id: str = "default",
    ) -> tuple[CompensationJournalEntry, ...]:
        """Read effects only for the expected quarantined execution epoch."""
        self._validate_identity(plan_id, plan_hash, project_id)
        if expected_status not in ("IN_DOUBT", "ROLLBACK_FAILED"):
            raise ValueError("unsupported expected status")
        if (
            isinstance(expected_attempt, bool)
            or not isinstance(expected_attempt, int)
            or not 0 <= expected_attempt <= 2**63 - 1
        ):
            raise ValueError("expected_attempt must be a nonnegative bounded integer")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status, attempt FROM plans WHERE project_id=? AND plan_id=? "
                "AND plan_hash=?",
                (project_id, plan_id, plan_hash),
            ).fetchone()
            if row != (expected_status, expected_attempt):
                raise PlanStoreStateError("plan is not quarantined at expected status/attempt")
            rows = conn.execute(
                "SELECT step_id, effect_key, attempt, status, evidence_json "
                "FROM compensation_journal WHERE project_id=? AND plan_id=? AND plan_hash=? "
                "ORDER BY entry_id",
                (project_id, plan_id, plan_hash),
            ).fetchall()
            return tuple(CompensationJournalEntry(*entry) for entry in rows)

    @staticmethod
    def _journal_digest(entries: tuple[CompensationJournalEntry, ...]) -> str:
        encoded = _encode_evidence([asdict(entry) for entry in entries]).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _recovery_json(evidence: RecoveryEvidence[datetime]) -> str:
        body = asdict(evidence)
        body["issued_at"] = evidence.issued_at.isoformat()
        body["expires_at"] = evidence.expires_at.isoformat()
        return _encode_evidence(body)

    def _authorize_recovery(
        self, project_id: str, operator_id: str, expected_status: str, expected_attempt: int
    ) -> None:
        if (
            self._repair_authorizer is None
            or self._repair_authorizer(project_id, operator_id, expected_status, expected_attempt)
            is not True
        ):
            raise PlanStoreStateError("repair authorization denied")

    def inspect_recovery(
        self,
        project_id: str,
        plan_id: str,
        plan_hash: str,
        *,
        expected_status: str,
        expected_attempt: int,
        operator_id: str,
    ) -> RecoveryEvidence[datetime]:
        """Issue a short-lived, one-use inspection from trusted configured routes.

        No caller-supplied destination, verifier, observation or nonce is trusted.
        The journal is rechecked atomically when recording the issued snapshot.
        Independent authentication and project access remain composition duties.
        """
        self._validate_identity(plan_id, plan_hash, project_id)
        validate_plan_identity(operator_id, name="operator_id")
        self._authorize_recovery(project_id, operator_id, expected_status, expected_attempt)
        if expected_status not in ("IN_DOUBT", "ROLLBACK_FAILED"):
            raise ValueError("unsupported expected status")
        if type(expected_attempt) is not int or not 0 <= expected_attempt <= 2**63 - 1:
            raise ValueError("expected_attempt must be a nonnegative bounded integer")
        # A dead worker cannot quarantine itself. Authenticated inspection may
        # quarantine only its matching expired lease, never a live worker.
        with self._transaction() as conn:
            conn.execute(
                "UPDATE plans SET status='IN_DOUBT' WHERE project_id=? AND plan_id=? "
                "AND plan_hash=? AND attempt=? AND status IN ('EXECUTING','COMPENSATING') "
                "AND lease_expires_at<=? AND ?='IN_DOUBT'",
                (project_id, plan_id, plan_hash, expected_attempt, self._now(), expected_status),
            )
        journal = self.quarantined_journal(
            plan_id,
            plan_hash,
            project_id=project_id,
            expected_status=expected_status,
            expected_attempt=expected_attempt,
        )
        digest = self._journal_digest(journal)
        verified: list[VerifiedEffectRecoveryEvidence] = []
        for entry in journal:
            if entry.status in ("COMPENSATED", "NOT_APPLIED"):
                continue
            route = self._recovery_routes.get((project_id, entry.step_id))
            if route is None:
                raise PlanStoreStateError("trusted recovery destination is unavailable")
            observation = route.destination.inspect(
                effect_key=entry.effect_key, fencing_epoch=entry.attempt
            )
            if (
                not isinstance(observation, ExternalEffectObservation)
                or observation.effect_key != entry.effect_key
                or observation.fencing_epoch != entry.attempt
                or observation.status == "UNKNOWN"
            ):
                raise PlanStoreStateError("recovery observation is unknown or mismatched")
            if route.verifier.verify(observation) is not True:
                raise PlanStoreStateError("recovery evidence was not independently verified")
            verified.append(
                VerifiedEffectRecoveryEvidence(
                    step_id=entry.step_id,
                    effect_key=entry.effect_key,
                    fencing_epoch=entry.attempt,
                    observation_status=observation.status,
                    observation_digest=observation.evidence_digest,
                    verifier_id=route.verifier_id,
                    destination_fenced=observation.status == "ABSENT",
                )
            )
        issued = datetime.now(timezone.utc)
        evidence = RecoveryEvidence(
            project_id=project_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            expected_status=expected_status,
            expected_attempt=expected_attempt,
            operator_id=operator_id,
            recovery_nonce=secrets.token_hex(32),
            issued_at=issued,
            expires_at=issued + timedelta(seconds=60),
            journal_digest=digest,
            effect_evidences=tuple(verified),
        )
        body = self._recovery_json(evidence)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status, attempt FROM plans WHERE project_id=? AND plan_id=? "
                "AND plan_hash=?",
                (project_id, plan_id, plan_hash),
            ).fetchone()
            rows = conn.execute(
                "SELECT step_id, effect_key, attempt, status, evidence_json "
                "FROM compensation_journal WHERE project_id=? AND plan_id=? AND plan_hash=? "
                "ORDER BY entry_id",
                (project_id, plan_id, plan_hash),
            ).fetchall()
            if (
                row != (expected_status, expected_attempt)
                or self._journal_digest(tuple(CompensationJournalEntry(*item) for item in rows))
                != digest
            ):
                raise PlanStoreStateError("plan or journal changed during recovery inspection")
            self._authorize_recovery(project_id, operator_id, expected_status, expected_attempt)
            now = self._now()
            conn.execute(
                "DELETE FROM plan_recovery_inspections WHERE consumed=1 OR expires_monotonic<=? "
                "OR clock_domain!=?",
                (now, self._clock_domain),
            )
            if (
                conn.execute("SELECT count(*) FROM plan_recovery_inspections").fetchone()[0]
                >= MAX_RECORDS
            ):
                raise PlanStoreCapacityError("recovery inspection capacity reached")
            conn.execute(
                "INSERT INTO plan_recovery_inspections "
                "(nonce_hash,evidence_json,journal_digest,clock_domain,expires_monotonic) "
                "VALUES (?,?,?,?,?)",
                (
                    hashlib.sha256(evidence.recovery_nonce.encode("ascii")).hexdigest(),
                    body,
                    digest,
                    self._clock_domain,
                    now + 60.0,
                ),
            )
        return evidence

    def record_compensation(
        self,
        plan_id: str,
        plan_hash: str,
        *,
        attempt: int,
        owner_token: str,
        succeeded: bool,
        evidence: Any,
        project_id: str = "default",
    ) -> None:
        """Record the outcome of a matching live compensation attempt."""
        self._validate_identity(plan_id, plan_hash, project_id)
        self._validate_fence(attempt, owner_token)
        if not isinstance(succeeded, bool):
            raise ValueError("succeeded must be boolean")
        body = _encode_evidence(evidence)
        result = "ROLLED_BACK" if succeeded else "ROLLBACK_FAILED"
        with self._transaction() as conn:
            now = self._now()
            if (
                conn.execute(
                    "SELECT 1 FROM plans WHERE project_id=? AND plan_id=? AND plan_hash=? "
                    "AND status='COMPENSATING' AND attempt=? AND owner_token=? "
                    "AND lease_expires_at>?",
                    (project_id, plan_id, plan_hash, attempt, owner_token, now),
                ).fetchone()
                is None
            ):
                raise PlanFencingTokenMismatchError(
                    "compensation is stale or its owner does not match"
                )
            if (
                succeeded
                and conn.execute(
                    "SELECT 1 FROM compensation_journal WHERE project_id=? AND plan_id=? "
                    "AND plan_hash=? AND status NOT IN ('COMPENSATED','NOT_APPLIED') LIMIT 1",
                    (project_id, plan_id, plan_hash),
                ).fetchone()
                is not None
            ):
                raise PlanStoreStateError("compensation journal contains unresolved steps")
            changed = conn.execute(
                "UPDATE plans SET status=?, compensation_status=?, compensation_evidence_json=? "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? AND status='COMPENSATING' "
                "AND attempt=? AND owner_token=? AND lease_expires_at>?",
                (
                    result,
                    result,
                    body,
                    project_id,
                    plan_id,
                    plan_hash,
                    attempt,
                    owner_token,
                    self._now(),
                ),
            ).rowcount
            if changed != 1:
                raise PlanFencingTokenMismatchError(
                    "compensation is stale or its owner does not match"
                )

    def repair_in_doubt(
        self,
        project_id: str,
        plan_id: str,
        plan_hash: str,
        *,
        expected_status: str,
        expected_attempt: int,
        operator_id: str,
        resolution_action: str,
        reason: str,
        recovery_evidence: RecoveryEvidence[Any] | None = None,
    ) -> None:
        """Consume trusted, current evidence and atomically resolve quarantine.

        Takeover requires independently verified, fenced ABSENT effects. The
        acknowledge_present policy records observed PRESENT effects as VERIFIED
        but retains quarantine: it never repeats them or claims compensation.
        Operator authentication remains external.
        """
        self._validate_identity(plan_id, plan_hash, project_id)
        validate_plan_identity(operator_id, name="operator_id")
        if expected_status not in ("IN_DOUBT", "ROLLBACK_FAILED"):
            raise ValueError("unsupported expected status")
        if (
            isinstance(expected_attempt, bool)
            or not isinstance(expected_attempt, int)
            or not 0 <= expected_attempt <= 2**63 - 1
        ):
            raise ValueError("expected_attempt must be a nonnegative bounded integer")
        if resolution_action not in (
            "confirm_rolled_back",
            "authorize_takeover",
            "acknowledge_present",
            "compensate_present",
        ):
            raise ValueError("unsupported repair action")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2048:
            raise ValueError("repair reason must contain 1 to 2048 characters")
        with self._transaction() as conn:
            self._authorize_recovery(project_id, operator_id, expected_status, expected_attempt)
            row = conn.execute(
                "SELECT status, attempt FROM plans WHERE project_id=? AND plan_id=? "
                "AND plan_hash=?",
                (project_id, plan_id, plan_hash),
            ).fetchone()
            if row is None or row[0] != expected_status or row[1] != expected_attempt:
                raise PlanStoreStateError(
                    "plan is not eligible for repair at expected status/attempt"
                )
            if not isinstance(recovery_evidence, RecoveryEvidence):
                raise PlanStoreStateError("verified recovery evidence is required")
            if (
                recovery_evidence.project_id != project_id
                or recovery_evidence.plan_id != plan_id
                or recovery_evidence.plan_hash != plan_hash
                or recovery_evidence.expected_status != expected_status
                or recovery_evidence.expected_attempt != expected_attempt
                or recovery_evidence.operator_id != operator_id
                or not isinstance(recovery_evidence.issued_at, datetime)
                or not isinstance(recovery_evidence.expires_at, datetime)
                or recovery_evidence.issued_at.tzinfo is None
                or recovery_evidence.expires_at.tzinfo is None
                or not recovery_evidence.issued_at
                <= datetime.now(timezone.utc)
                < recovery_evidence.expires_at
                or not isinstance(recovery_evidence.recovery_nonce, str)
                or re.fullmatch(r"[0-9a-f]{64}", recovery_evidence.recovery_nonce) is None
            ):
                raise PlanStoreStateError("recovery evidence is expired or mismatched")
            body = self._recovery_json(recovery_evidence)
            nonce_hash = hashlib.sha256(
                recovery_evidence.recovery_nonce.encode("ascii")
            ).hexdigest()
            inspection = conn.execute(
                "SELECT evidence_json,journal_digest,clock_domain,expires_monotonic,consumed "
                "FROM plan_recovery_inspections WHERE nonce_hash=?",
                (nonce_hash,),
            ).fetchone()
            now = self._now()
            if (
                inspection is None
                or inspection[0] != body
                or inspection[1] != recovery_evidence.journal_digest
                or inspection[2] != self._clock_domain
                or inspection[3] <= now
                or inspection[4] != 0
            ):
                raise PlanStoreStateError("recovery nonce is invalid, expired or consumed")
            rows = conn.execute(
                "SELECT step_id, effect_key, attempt, status, evidence_json "
                "FROM compensation_journal WHERE project_id=? AND plan_id=? AND plan_hash=? "
                "ORDER BY entry_id",
                (project_id, plan_id, plan_hash),
            ).fetchall()
            journal = tuple(CompensationJournalEntry(*item) for item in rows)
            if self._journal_digest(journal) != recovery_evidence.journal_digest:
                raise PlanStoreStateError("recovery journal changed after inspection")
            unresolved = tuple(
                item for item in journal if item.status not in ("COMPENSATED", "NOT_APPLIED")
            )
            if len(unresolved) != len(recovery_evidence.effect_evidences):
                raise PlanStoreStateError("recovery evidence does not cover every effect")
            for entry, effect in zip(unresolved, recovery_evidence.effect_evidences, strict=True):
                if (
                    effect.step_id != entry.step_id
                    or effect.effect_key != entry.effect_key
                    or effect.fencing_epoch != entry.attempt
                    or effect.observation_status not in ("PRESENT", "ABSENT")
                    or (
                        effect.observation_status == "PRESENT"
                        and resolution_action not in ("acknowledge_present", "compensate_present")
                    )
                    or (
                        effect.observation_status == "ABSENT"
                        and effect.destination_fenced is not True
                    )
                ):
                    raise PlanStoreStateError("effect is not verified fenced ABSENT")
            if conn.execute("SELECT count(*) FROM plan_repair_events").fetchone()[0] >= MAX_RECORDS:
                raise PlanStoreCapacityError("repair audit capacity reached")
            if (
                resolution_action in ("authorize_takeover", "compensate_present")
                and expected_attempt >= 2**63 - 1
            ):
                raise PlanStoreStateError("execution attempt counter exhausted")
            remediations: dict[str, ExternalEffectObservation] = {}
            if resolution_action == "compensate_present":
                # Validate every route before invoking any external remediation.
                for effect in recovery_evidence.effect_evidences:
                    if effect.observation_status == "PRESENT":
                        route = self._recovery_routes.get((project_id, effect.step_id))
                        if (
                            route is None
                            or route.compensator is None
                            or route.verifier_id != effect.verifier_id
                        ):
                            raise PlanStoreStateError("trusted compensation route is unavailable")
                for effect in recovery_evidence.effect_evidences:
                    if effect.observation_status != "PRESENT":
                        continue
                    route = self._recovery_routes[(project_id, effect.step_id)]
                    assert route.compensator is not None
                    before = ExternalEffectObservation(
                        effect.effect_key,
                        effect.fencing_epoch,
                        "PRESENT",
                        effect.observation_digest,
                    )
                    if route.verifier.verify(before) is not True:
                        raise PlanStoreStateError("PRESENT observation changed before compensation")
                    after = route.compensator.compensate(before)
                    if (
                        not isinstance(after, ExternalEffectObservation)
                        or after.status != "ABSENT"
                        or after.effect_key != effect.effect_key
                        or after.fencing_epoch != effect.fencing_epoch
                        or route.verifier.verify(after) is not True
                    ):
                        raise PlanStoreStateError(
                            "compensation absence was not independently verified"
                        )
                    remediations[effect.step_id] = after
            audit = _encode_evidence(
                {
                    "operator_id": operator_id,
                    "action": resolution_action,
                    "reason": reason,
                    "previous_status": row[0],
                    "previous_attempt": row[1],
                    "resolved_at": now,
                    "recovery_evidence": json.loads(body),
                    **(
                        {
                            "remediations": {
                                key: asdict(value) for key, value in remediations.items()
                            }
                        }
                        if remediations
                        else {}
                    ),
                }
            )
            for entry, effect in zip(unresolved, recovery_evidence.effect_evidences, strict=True):
                changed = conn.execute(
                    "UPDATE compensation_journal SET status=?,evidence_json=? "
                    "WHERE project_id=? AND plan_id=? AND plan_hash=? AND step_id=? "
                    "AND effect_key=? AND attempt=? AND status=?",
                    (
                        (
                            "COMPENSATED"
                            if effect.step_id in remediations
                            else "VERIFIED"
                            if effect.observation_status == "PRESENT"
                            else "NOT_APPLIED"
                        ),
                        _encode_evidence(asdict(effect)),
                        project_id,
                        plan_id,
                        plan_hash,
                        entry.step_id,
                        entry.effect_key,
                        entry.attempt,
                        entry.status,
                    ),
                ).rowcount
                if changed != 1:
                    raise PlanStoreStateError("recovery journal changed while repairing")
            target = (
                "ROLLED_BACK"
                if resolution_action in ("confirm_rolled_back", "compensate_present")
                else "RESERVED"
            )
            if resolution_action == "acknowledge_present":
                target = expected_status
            takeover = resolution_action == "authorize_takeover"
            changed = conn.execute(
                "UPDATE plans SET status=?, repair_evidence_json=?, owner_token=NULL, "
                "lease_expires_at=NULL, takeover_ready=?, takeover_epoch_reserved=?, attempt=? "
                "WHERE project_id=? AND plan_id=? AND plan_hash=? AND status=? AND attempt=?",
                (
                    target,
                    audit,
                    int(takeover),
                    int(takeover),
                    expected_attempt + int(takeover or resolution_action == "compensate_present"),
                    project_id,
                    plan_id,
                    plan_hash,
                    expected_status,
                    expected_attempt,
                ),
            ).rowcount
            if changed != 1:
                raise PlanStoreStateError("plan changed while repairing")
            changed = conn.execute(
                "UPDATE plan_recovery_inspections SET consumed=1 WHERE nonce_hash=? AND consumed=0 "
                "AND clock_domain=? AND expires_monotonic>?",
                (nonce_hash, self._clock_domain, self._now()),
            ).rowcount
            if changed != 1:
                raise PlanStoreStateError("recovery nonce changed while repairing")
            conn.execute(
                "INSERT INTO plan_repair_events (project_id, plan_id, plan_hash, evidence_json) "
                "VALUES (?, ?, ?, ?)",
                (project_id, plan_id, plan_hash, audit),
            )
