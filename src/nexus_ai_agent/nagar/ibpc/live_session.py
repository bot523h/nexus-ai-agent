"""Durable live session + turn persistence for proof-carrying live editing.

The in-process :class:`~nexus_ai_agent.nagar.ibpc.session.LiveSessionManager`
owns the *fencing rules* (which transitions are legal, what a partial transcript
may do, when a turn is stale).  This module owns **durability** for the same
fencing model, backed by one SQLite file opened in WAL mode:

* ``ibpc_sessions`` — one row per live session with an optimistic
  ``version_epoch`` compare-and-swap counter, a monotonic ``last_turn_index``
  and a lifecycle ``status`` (``ACTIVE``/``PAUSED``/``CLOSED``/``ABORTED``).
* ``ibpc_turns`` — one row per submitted turn, carrying the utterance digest
  (never the raw transcript), the compiled plan + its SHA-256, the preview
  manifest + its SHA-256, the approval id and the turn status ladder.

Six invariants are enforced mechanically, not by convention:

1. **Strict monotonic turn ordering.** ``turn_index`` is allocated as
   ``last_turn_index + 1`` inside the write transaction; ``UNIQUE`` on
   ``(session_id, turn_index)`` and on ``idempotency_key`` makes a duplicate
   or reordered allocation impossible rather than merely unlikely.
2. **Idempotent submission** (``f(f(x)) == f(x)``). A repeat of the same
   ``idempotency_key`` — even from a concurrent process — returns the *stored*
   :class:`TurnRecord` and performs no side effect.  The first winner is the
   row the database committed; losers read it back inside their transaction.
3. **Stale-revision protection.** A turn may only be submitted or edited when
   its ``base_revision_id`` equals the session's ``active_revision_id``;
   anything else raises :class:`StaleSessionRevisionError` *before* a row is
   written.
4. **Safe deterministic termination.**
   :meth:`DurableLiveSessionStore.close_session` /
   :meth:`~DurableLiveSessionStore.abort_session` are terminal, idempotent and
   revoke every unconsumed approval of the session through the injected revoker
   inside the same transaction — a preview approval can never outlive its
   session, and every open turn is superseded.
5. **Full state reconstruction.** Every read path goes through the file, so a
   fresh store over the same ``db_path`` observes exactly the state the
   previous process committed (nothing is cached in Python).
6. **Concurrent serialization.** Every mutation runs in ``BEGIN IMMEDIATE``
   (a cross-process write lock) and commits through an optimistic
   ``version_epoch`` compare-and-swap; a lock that cannot be obtained is
   surfaced as :class:`ConcurrentSessionMutationError`, never swallowed.

Storage contains no sensitive payload: the utterance is stored as a
``sha256:`` digest, and the compiled plan / preview are digest-addressed JSON.
Raw audio and raw transcripts never reach this file.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from nexus_ai_agent.nagar.ibpc.canonical import sha256_bytes
from nexus_ai_agent.nagar.ibpc.models import CompiledPlan, PreviewManifest

__all__ = [
    "ConcurrentSessionMutationError",
    "DurableLiveSessionStore",
    "SessionRecord",
    "SessionStatus",
    "StaleSessionError",
    "StaleSessionRevisionError",
    "TurnRecord",
    "TurnStatus",
    "TurnStoreError",
    "durable_live_session_path",
]

#: How many times a busy write lock is re-attempted before a typed failure.
_BUSY_RETRIES = 5

#: ``busy_timeout`` for every connection opened by this store (milliseconds).
_BUSY_TIMEOUT_MS = 5000


class TurnStoreError(ValueError):
    """Typed refusal from the durable session store. Never a bare ``KeyError``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class StaleSessionRevisionError(TurnStoreError):
    """The turn was compiled against a revision the session has since left."""


class StaleSessionError(TurnStoreError):
    """The turn was fenced (a newer turn exists, or the session was terminated)."""


class ConcurrentSessionMutationError(TurnStoreError):
    """A conflicting parallel write won, or the write lock was unobtainable."""


class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"


class TurnStatus(str, Enum):
    DRAFTED = "DRAFTED"
    PREVIEW_READY = "PREVIEW_READY"
    APPROVED = "APPROVED"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


#: The one legal turn-status transition table (terminal states have no exit).
TURN_TRANSITIONS: dict[TurnStatus, frozenset[TurnStatus]] = {
    TurnStatus.DRAFTED: frozenset(
        {TurnStatus.PREVIEW_READY, TurnStatus.REJECTED, TurnStatus.SUPERSEDED}
    ),
    TurnStatus.PREVIEW_READY: frozenset(
        {TurnStatus.APPROVED, TurnStatus.REJECTED, TurnStatus.SUPERSEDED}
    ),
    TurnStatus.APPROVED: frozenset({TurnStatus.EXECUTED, TurnStatus.SUPERSEDED}),
    TurnStatus.EXECUTED: frozenset(),
    TurnStatus.REJECTED: frozenset(),
    TurnStatus.SUPERSEDED: frozenset(),
}

#: Session states that accept no further mutation.
TERMINAL_SESSION_STATES = frozenset({SessionStatus.CLOSED, SessionStatus.ABORTED})

#: Turn states that a session termination supersedes.
_OPEN_TURN_STATES = (
    TurnStatus.DRAFTED.value,
    TurnStatus.PREVIEW_READY.value,
    TurnStatus.APPROVED.value,
)

_SESSION_COLUMNS = (
    "session_id, project_id, principal_id, base_revision_id, active_revision_id, status,"
    " version_epoch, last_turn_index, created_at_utc, updated_at_utc"
)

_TURN_COLUMNS = (
    "turn_id, session_id, turn_index, idempotency_key, user_utterance, base_revision_id,"
    " scope_signature, compiled_plan_json, compiled_plan_sha256, preview_manifest_json,"
    " preview_sha256, approval_id, turn_status, created_at_utc, refusal_reason"
)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ibpc_sessions ("
    " session_id TEXT PRIMARY KEY,"
    " project_id TEXT NOT NULL,"
    " principal_id TEXT NOT NULL,"
    " base_revision_id TEXT NOT NULL,"
    " active_revision_id TEXT NOT NULL,"
    " status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'PAUSED', 'CLOSED', 'ABORTED')),"
    " version_epoch INTEGER NOT NULL DEFAULT 1,"
    " last_turn_index INTEGER NOT NULL DEFAULT 0,"
    " created_at_utc TEXT NOT NULL,"
    " updated_at_utc TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ibpc_turns ("
    " turn_id TEXT PRIMARY KEY,"
    " session_id TEXT NOT NULL REFERENCES ibpc_sessions(session_id),"
    " turn_index INTEGER NOT NULL,"
    " idempotency_key TEXT NOT NULL UNIQUE,"
    " user_utterance TEXT NOT NULL,"
    " base_revision_id TEXT NOT NULL,"
    " scope_signature TEXT NOT NULL,"
    " compiled_plan_json TEXT NOT NULL,"
    " compiled_plan_sha256 TEXT NOT NULL,"
    " preview_manifest_json TEXT,"
    " preview_sha256 TEXT,"
    " approval_id TEXT,"
    " turn_status TEXT NOT NULL CHECK(turn_status IN"
    " ('DRAFTED', 'PREVIEW_READY', 'APPROVED', 'EXECUTED', 'REJECTED', 'SUPERSEDED')),"
    " created_at_utc TEXT NOT NULL,"
    # a turn that failed BEFORE a plan existed keeps an empty plan document and
    # names its typed refusal here (Wave 3-R2a: failed voice turns are durable)
    " refusal_reason TEXT,"
    " UNIQUE(session_id, turn_index))",
    "CREATE INDEX IF NOT EXISTS idx_ibpc_turns_session ON ibpc_turns(session_id, turn_index)",
)


def durable_live_session_path(workspace: str | Path) -> Path:
    """Canonical SQLite location for the durable live-session store."""
    return Path(workspace) / "queue" / "ibpc_live_session.sqlite3"


def utterance_digest(text: str) -> str:
    """Digest-only spelling persisted for one utterance (never the raw text)."""
    return sha256_bytes(text.encode("utf-8"))


def _json_digest(payload: dict[str, Any]) -> str:
    """``sha256:<hex>`` of the RFC 8785 canonical bytes of *payload*."""
    from nexus_ai_agent.provenance.canonicalization import (
        CanonicalizationScheme,
        canonical_digest,
    )

    return canonical_digest(payload, CanonicalizationScheme.JCS_RFC8785_V2)


def _encode(payload: dict[str, Any]) -> tuple[str, str]:
    """Compact, deterministic JSON spelling plus its JCS digest."""
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return text, _json_digest(payload)


@dataclass(frozen=True)
class SessionRecord:
    """One durable live session (immutable snapshot of a table row)."""

    session_id: str
    project_id: str
    principal_id: str
    base_revision_id: str
    active_revision_id: str
    status: SessionStatus
    version_epoch: int
    last_turn_index: int
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True)
class TurnRecord:
    """One durable turn.

    ``user_utterance`` is the *digest-only* spelling the row carries
    (``sha256:`` + 64 hex) — the raw utterance is never persisted.
    """

    turn_id: str
    session_id: str
    turn_index: int
    idempotency_key: str
    user_utterance: str
    base_revision_id: str
    scope_signature: str
    compiled_plan_json: str
    compiled_plan_sha256: str
    preview_manifest_json: str | None
    preview_sha256: str | None
    approval_id: str | None
    turn_status: TurnStatus
    created_at_utc: str
    #: Present only for a plan-less row: why the turn failed before a plan existed.
    refusal_reason: str | None = None

    def compiled_plan(self) -> CompiledPlan:
        if self.refusal_reason is not None:
            raise TurnStoreError("no_plan", f"turn {self.turn_id} failed before a plan existed")
        return CompiledPlan.model_validate_json(self.compiled_plan_json)

    def preview_manifest(self) -> PreviewManifest | None:
        if self.preview_manifest_json is None:
            return None
        return PreviewManifest.model_validate_json(self.preview_manifest_json)


class DurableLiveSessionStore:
    """SQLite-WAL durable store for IBPC live sessions and turns."""

    def __init__(self, db_path: str | Path, *, now: Callable[[], str]) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._transaction():
            for statement in _SCHEMA:
                self._conn.execute(statement)
            turn_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(ibpc_turns)")}
            if "refusal_reason" not in turn_columns:
                # Additive, backwards-compatible: every existing row keeps NULL,
                # which means "this turn reached a compiled plan".
                self._conn.execute("ALTER TABLE ibpc_turns ADD COLUMN refusal_reason TEXT")
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    # --- lifecycle ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def busy_timeout_ms(self) -> int:
        """The ``busy_timeout`` this store sets on every connection (ms)."""
        return _BUSY_TIMEOUT_MS

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- transaction seam ---------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """One ``BEGIN IMMEDIATE`` write unit; re-entrant within a thread."""
        with self._lock:
            if self._depth > 0:
                yield
                return
            attempts = 0
            while True:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    attempts += 1
                    if attempts >= _BUSY_RETRIES:
                        raise ConcurrentSessionMutationError(
                            "write_lock_unavailable",
                            f"could not open a write transaction: {exc}",
                        ) from exc
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            self._depth = 0
            try:
                self._conn.execute("COMMIT")
            except sqlite3.OperationalError as exc:
                raise ConcurrentSessionMutationError(
                    "commit_conflict", f"a concurrent writer won the commit: {exc}"
                ) from exc

    # --- sessions -----------------------------------------------------------

    def open_session(
        self,
        *,
        session_id: str,
        project_id: str,
        principal_id: str,
        base_revision_id: str,
        active_revision_id: str | None = None,
    ) -> SessionRecord:
        """Create one ACTIVE session. Idempotent for the identical request."""
        if not session_id or not project_id or not principal_id or not base_revision_id:
            raise TurnStoreError(
                "invalid_session", "session/project/principal/revision are required"
            )
        active = base_revision_id if active_revision_id is None else active_revision_id
        with self._transaction():
            existing = self._fetch_session(session_id)
            if existing is not None:
                identical = (
                    existing.project_id == project_id
                    and existing.principal_id == principal_id
                    and existing.base_revision_id == base_revision_id
                    and existing.active_revision_id == active
                )
                if identical:
                    return existing
                raise TurnStoreError("duplicate_session", session_id)
            clash = self._conn.execute(
                "SELECT session_id FROM ibpc_sessions WHERE project_id = ? AND principal_id = ?"
                " AND status NOT IN ('CLOSED', 'ABORTED')",
                (project_id, principal_id),
            ).fetchone()
            if clash is not None:
                raise TurnStoreError(
                    "duplicate_open_session",
                    "an ACTIVE/PAUSED session already exists for this project+principal",
                )
            stamp = self._now()
            self._conn.execute(
                "INSERT INTO ibpc_sessions (session_id, project_id, principal_id, base_revision_id,"
                " active_revision_id, status, version_epoch, last_turn_index, created_at_utc,"
                " updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)",
                (
                    session_id,
                    project_id,
                    principal_id,
                    base_revision_id,
                    active,
                    SessionStatus.ACTIVE.value,
                    stamp,
                    stamp,
                ),
            )
            record = self._fetch_session(session_id)
        assert record is not None
        return record

    def get_session(self, session_id: str) -> SessionRecord:
        record = self._fetch_session(session_id)
        if record is None:
            raise TurnStoreError("unknown_session", session_id)
        return record

    def list_sessions(self, *statuses: SessionStatus) -> tuple[SessionRecord, ...]:
        """Every session (optionally filtered by status), oldest first.

        The recovery seam: a new process enumerates what it must reconstruct
        instead of trusting its own empty in-memory maps.
        """
        allowed = [status.value for status in statuses]
        query = f"SELECT {_SESSION_COLUMNS} FROM ibpc_sessions"
        params: tuple[Any, ...] = ()
        if allowed:
            query += f" WHERE status IN ({', '.join('?' for _ in allowed)})"
            params = tuple(allowed)
        rows = self._conn.execute(query + " ORDER BY created_at_utc, rowid", params).fetchall()
        return tuple(_session_record(row) for row in rows)

    def _fetch_session(self, session_id: str) -> SessionRecord | None:
        row = self._conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM ibpc_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return _session_record(row)

    def advance_revision(self, session_id: str, *, revision_id: str) -> SessionRecord:
        """Move the session to a new active revision (epoch compare-and-swap)."""
        if not revision_id:
            raise TurnStoreError("invalid_revision", "a revision id is required")
        with self._transaction():
            session = self.get_session(session_id)
            self._require_open(session)
            self._cas_session(
                session, "active_revision_id = ?", (revision_id,), "revision_conflict"
            )
            return self.get_session(session_id)

    def pause_session(self, session_id: str) -> SessionRecord:
        with self._transaction():
            session = self.get_session(session_id)
            self._require_open(session)
            return self._write_status(session, SessionStatus.PAUSED)

    def resume_session(self, session_id: str) -> SessionRecord:
        with self._transaction():
            session = self.get_session(session_id)
            if session.status is not SessionStatus.PAUSED:
                raise TurnStoreError("invalid_status", f"cannot resume from {session.status.value}")
            return self._write_status(session, SessionStatus.ACTIVE)

    def close_session(
        self, session_id: str, *, revoke_approvals: Callable[[str], Any] | None = None
    ) -> SessionRecord:
        """Terminate as CLOSED; revoke every unconsumed approval of the session."""
        return self._terminate(session_id, SessionStatus.CLOSED, revoke_approvals)

    def abort_session(
        self, session_id: str, *, revoke_approvals: Callable[[str], Any] | None = None
    ) -> SessionRecord:
        """Terminate as ABORTED (interrupt/crash path); same revocation duty."""
        return self._terminate(session_id, SessionStatus.ABORTED, revoke_approvals)

    def _terminate(
        self,
        session_id: str,
        status: SessionStatus,
        revoke_approvals: Callable[[str], Any] | None,
    ) -> SessionRecord:
        with self._transaction():
            session = self.get_session(session_id)
            if session.status is status:
                return session
            if session.status in TERMINAL_SESSION_STATES:
                raise TurnStoreError(
                    "session_terminated", f"{session_id!r} is already {session.status.value}"
                )
            if revoke_approvals is not None:
                revoke_approvals(session_id)
            self._conn.execute(
                "UPDATE ibpc_turns SET turn_status = ? WHERE session_id = ? AND turn_status IN"
                " (?, ?, ?)",
                (TurnStatus.SUPERSEDED.value, session_id, *_OPEN_TURN_STATES),
            )
            self._write_status(session, status)
            return self.get_session(session_id)

    def _require_open(self, session: SessionRecord) -> None:
        if session.status in TERMINAL_SESSION_STATES:
            raise TurnStoreError(
                "session_terminated", f"{session.session_id!r} is {session.status.value}"
            )

    def _write_status(self, session: SessionRecord, status: SessionStatus) -> SessionRecord:
        self._cas_session(session, "status = ?", (status.value,), "status_conflict")
        return self.get_session(session.session_id)

    def _cas_session(
        self,
        session: SessionRecord,
        assignments: str | None,
        params: tuple[Any, ...],
        conflict_reason: str,
    ) -> None:
        """Optimistic ``version_epoch`` compare-and-swap for one session row.

        ``assignments=None`` performs a pure epoch bump (used to record that a
        turn transition touched this session without changing session columns).
        """
        clause = "" if assignments is None else f"{assignments}, "
        cursor = self._conn.execute(
            f"UPDATE ibpc_sessions SET {clause}version_epoch = version_epoch + 1,"
            " updated_at_utc = ? WHERE session_id = ? AND version_epoch = ?",
            (*params, self._now(), session.session_id, session.version_epoch),
        )
        if cursor.rowcount != 1:
            raise ConcurrentSessionMutationError(
                conflict_reason, f"{session.session_id!r} was mutated concurrently"
            )

    # --- turns --------------------------------------------------------------

    def submit_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        idempotency_key: str,
        user_utterance: str,
        base_revision_id: str,
        scope_signature: str,
        plan: CompiledPlan,
        preview: PreviewManifest | None = None,
        approval_id: str | None = None,
        turn_status: TurnStatus = TurnStatus.DRAFTED,
    ) -> TurnRecord:
        """Allocate the next ``turn_index`` and persist the turn exactly once."""
        if not idempotency_key:
            raise TurnStoreError("invalid_idempotency_key", "an idempotency key is required")
        if not isinstance(turn_status, TurnStatus):
            raise TurnStoreError("invalid_turn_status", f"{turn_status!r} is not a TurnStatus")
        plan_json, plan_sha = _encode(plan.model_dump(mode="json"))
        if preview is None:
            preview_json, preview_sha = None, None
        else:
            preview_json, preview_sha = _encode(preview.model_dump(mode="json"))
        with self._transaction():
            # 1) idempotency: a repeat returns the stored row with no side effect.
            prior = self._fetch_turn_by_key(idempotency_key)
            if prior is not None:
                if prior.session_id != session_id or prior.turn_id != turn_id:
                    raise TurnStoreError(
                        "idempotency_conflict",
                        "the idempotency key is already bound to another turn",
                    )
                if (
                    prior.compiled_plan_sha256 != plan_sha
                    or prior.base_revision_id != base_revision_id
                ):
                    raise TurnStoreError(
                        "idempotency_conflict", "the same key presented a different payload"
                    )
                return prior
            # 2) replay of a turn id inside the session returns that row.
            same_turn = self._fetch_turn(session_id, turn_id)
            if same_turn is not None:
                return same_turn
            session = self.get_session(session_id)
            self._require_open(session)
            # 3) stale-revision protection: the turn must be compiled against the
            #    revision the session is actually on.
            if base_revision_id != session.active_revision_id:
                raise StaleSessionRevisionError(
                    "stale_revision",
                    f"turn base {base_revision_id!r} != active {session.active_revision_id!r}",
                )
            index = session.last_turn_index + 1
            stamp = self._now()
            self._cas_session(session, "last_turn_index = ?", (index,), "turn_alloc_conflict")
            try:
                self._conn.execute(
                    "INSERT INTO ibpc_turns (turn_id, session_id, turn_index, idempotency_key,"
                    " user_utterance, base_revision_id, scope_signature, compiled_plan_json,"
                    " compiled_plan_sha256, preview_manifest_json, preview_sha256, approval_id,"
                    " turn_status, created_at_utc)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        turn_id,
                        session_id,
                        index,
                        idempotency_key,
                        utterance_digest(user_utterance),
                        base_revision_id,
                        scope_signature,
                        plan_json,
                        plan_sha,
                        preview_json,
                        preview_sha,
                        approval_id,
                        turn_status.value,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # UNIQUE(session_id, turn_index) lost a cross-process race.
                raise ConcurrentSessionMutationError(
                    "turn_index_conflict", "another writer allocated this turn index"
                ) from exc
            record = self._fetch_turn(session_id, turn_id)
        assert record is not None
        return record

    def get_turn(self, session_id: str, turn_id: str) -> TurnRecord:
        record = self._fetch_turn(session_id, turn_id)
        if record is None:
            raise TurnStoreError("unknown_turn", f"{session_id}/{turn_id}")
        return record

    def get_turn_by_idempotency_key(self, idempotency_key: str) -> TurnRecord:
        record = self._fetch_turn_by_key(idempotency_key)
        if record is None:
            raise TurnStoreError("unknown_idempotency_key", idempotency_key)
        return record

    def latest_turn(self, session_id: str) -> TurnRecord:
        """The turn with the highest index (the fencing head)."""
        row = self._conn.execute(
            f"SELECT {_TURN_COLUMNS} FROM ibpc_turns WHERE session_id = ?"
            " ORDER BY turn_index DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            raise StaleSessionError("no_turns", session_id)
        return _turn_record(row)

    def list_turns(self, session_id: str) -> tuple[TurnRecord, ...]:
        rows = self._conn.execute(
            f"SELECT {_TURN_COLUMNS} FROM ibpc_turns WHERE session_id = ? ORDER BY turn_index",
            (session_id,),
        ).fetchall()
        return tuple(_turn_record(row) for row in rows)

    def _fetch_turn(self, session_id: str, turn_id: str) -> TurnRecord | None:
        row = self._conn.execute(
            f"SELECT {_TURN_COLUMNS} FROM ibpc_turns WHERE session_id = ? AND turn_id = ?",
            (session_id, turn_id),
        ).fetchone()
        return None if row is None else _turn_record(row)

    def _fetch_turn_by_key(self, idempotency_key: str) -> TurnRecord | None:
        row = self._conn.execute(
            f"SELECT {_TURN_COLUMNS} FROM ibpc_turns WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return None if row is None else _turn_record(row)

    def set_turn_preview(
        self,
        session_id: str,
        turn_id: str,
        *,
        preview: PreviewManifest,
        approval_id: str,
        base_revision_id: str,
    ) -> TurnRecord:
        """Attach the plan-derived preview + approval id to a DRAFTED turn."""
        preview_json, preview_sha = _encode(preview.model_dump(mode="json"))
        return self._transition_turn(
            session_id,
            turn_id,
            TurnStatus.PREVIEW_READY,
            base_revision_id=base_revision_id,
            preview_json=preview_json,
            preview_sha=preview_sha,
            approval_id=approval_id,
        )

    def mark_turn_approved(
        self, session_id: str, turn_id: str, *, base_revision_id: str
    ) -> TurnRecord:
        return self._transition_turn(
            session_id, turn_id, TurnStatus.APPROVED, base_revision_id=base_revision_id
        )

    def mark_turn_executed(
        self, session_id: str, turn_id: str, *, base_revision_id: str
    ) -> TurnRecord:
        return self._transition_turn(
            session_id, turn_id, TurnStatus.EXECUTED, base_revision_id=base_revision_id
        )

    def record_failed_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        idempotency_key: str,
        user_utterance: str,
        base_revision_id: str,
        refusal_reason: str,
        scope_signature: str = "",
    ) -> TurnRecord:
        """Record a turn that failed **before** a compiled plan existed.

        Voice turns are transcribed before they are planned, so a transcription
        or media refusal has no ``CompiledPlan`` to store. The row is still a
        first-class, monotonic, idempotent turn: ``turn_status = REJECTED`` with
        an empty plan document and the typed ``refusal_reason`` beside it
        (``TurnRecord.compiled_plan()`` refuses to invent one). Raw audio and the
        raw transcript are never written — only the utterance digest.
        """
        if not idempotency_key:
            raise TurnStoreError("invalid_idempotency_key", "an idempotency key is required")
        if not refusal_reason:
            raise TurnStoreError("invalid_refusal", "a typed refusal reason is required")
        plan_json, plan_sha = _encode({})
        signature = scope_signature or _json_digest({"refused": refusal_reason})
        with self._transaction():
            prior = self._fetch_turn_by_key(idempotency_key)
            if prior is not None:
                if prior.session_id != session_id or prior.turn_id != turn_id:
                    raise TurnStoreError(
                        "idempotency_conflict",
                        "the idempotency key is already bound to another turn",
                    )
                return prior
            same_turn = self._fetch_turn(session_id, turn_id)
            if same_turn is not None:
                return same_turn
            session = self.get_session(session_id)
            self._require_open(session)
            if base_revision_id != session.active_revision_id:
                raise StaleSessionRevisionError(
                    "stale_revision",
                    f"turn base {base_revision_id!r} != active {session.active_revision_id!r}",
                )
            index = session.last_turn_index + 1
            stamp = self._now()
            self._cas_session(session, "last_turn_index = ?", (index,), "turn_alloc_conflict")
            try:
                self._conn.execute(
                    "INSERT INTO ibpc_turns (turn_id, session_id, turn_index, idempotency_key,"
                    " user_utterance, base_revision_id, scope_signature, compiled_plan_json,"
                    " compiled_plan_sha256, preview_manifest_json, preview_sha256, approval_id,"
                    " turn_status, created_at_utc, refusal_reason)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)",
                    (
                        turn_id,
                        session_id,
                        index,
                        idempotency_key,
                        utterance_digest(user_utterance),
                        base_revision_id,
                        signature,
                        plan_json,
                        plan_sha,
                        TurnStatus.REJECTED.value,
                        stamp,
                        refusal_reason,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrentSessionMutationError(
                    "turn_index_conflict", "another writer allocated this turn index"
                ) from exc
            record = self._fetch_turn(session_id, turn_id)
        assert record is not None
        return record

    def supersede_turn(self, session_id: str, turn_id: str, *, base_revision_id: str) -> TurnRecord:
        """APPROVED → SUPERSEDED: the human approved, but it never executed."""
        return self._transition_turn(
            session_id, turn_id, TurnStatus.SUPERSEDED, base_revision_id=base_revision_id
        )

    def reject_turn(self, session_id: str, turn_id: str, *, base_revision_id: str) -> TurnRecord:
        return self._transition_turn(
            session_id, turn_id, TurnStatus.REJECTED, base_revision_id=base_revision_id
        )

    def _transition_turn(
        self,
        session_id: str,
        turn_id: str,
        target: TurnStatus,
        *,
        base_revision_id: str,
        preview_json: str | None = None,
        preview_sha: str | None = None,
        approval_id: str | None = None,
    ) -> TurnRecord:
        with self._transaction():
            session = self.get_session(session_id)
            self._require_open(session)
            if base_revision_id != session.active_revision_id:
                raise StaleSessionRevisionError(
                    "stale_revision",
                    f"edit base {base_revision_id!r} != active {session.active_revision_id!r}",
                )
            turn = self.get_turn(session_id, turn_id)
            if target not in TURN_TRANSITIONS[turn.turn_status]:
                raise TurnStoreError(
                    "invalid_transition", f"{turn.turn_status.value} -> {target.value}"
                )
            cursor = self._conn.execute(
                "UPDATE ibpc_turns SET turn_status = ?,"
                " preview_manifest_json = COALESCE(?, preview_manifest_json),"
                " preview_sha256 = COALESCE(?, preview_sha256),"
                " approval_id = COALESCE(?, approval_id)"
                " WHERE session_id = ? AND turn_id = ? AND turn_status = ?",
                (
                    target.value,
                    preview_json,
                    preview_sha,
                    approval_id,
                    session_id,
                    turn_id,
                    turn.turn_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentSessionMutationError(
                    "turn_conflict", f"{session_id}/{turn_id} was mutated concurrently"
                )
            self._cas_session(session, None, (), "epoch_conflict")
            record = self._fetch_turn(session_id, turn_id)
        assert record is not None
        return record


def _turn_record(row: tuple[Any, ...]) -> TurnRecord:
    return TurnRecord(
        row[0],
        row[1],
        int(row[2]),
        row[3],
        row[4],
        row[5],
        row[6],
        row[7],
        row[8],
        row[9],
        row[10],
        row[11],
        TurnStatus(row[12]),
        row[13],
        row[14],
    )


def _session_record(row: tuple[Any, ...]) -> SessionRecord:
    return SessionRecord(
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        SessionStatus(row[5]),
        int(row[6]),
        int(row[7]),
        row[8],
        row[9],
    )
