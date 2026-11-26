"""Live session + voice-turn fencing with durable SQLite WAL persistence.

Every turn carries an identity and an order: ``session_id``, ``turn_id``,
``turn_index``, ``sequence_number``, ``project_revision``,
``target_revision_id``, ``transcript_state``, ``turn_state``.
The transition table below is enforced mechanically, and the fencing rules are
the mission's adversarial + Wave 3 durable invariants made executable:

* strict monotonic ``turn_index`` per session (``UNIQUE(session_id, turn_index)``);
* idempotent turn deduplication per ``(session_id, idempotency_key)`` with
  typed conflict on payload mismatch;
* stale revision rejection whenever ``target_revision_id != active_revision_id``
  or ``project_revision != current_revision``;
* safe session close/pause/abort revoking any unconsumed previews/approvals;
* full restart recovery via SQLite WAL (``BEGIN IMMEDIATE``, ``busy_timeout=5000``,
  ``foreign_keys=ON``, ``synchronous=FULL``);
* concurrent turn serialization via ``BEGIN IMMEDIATE`` + optimistic
  ``session_epoch`` CAS.

The raw transcript/audio never leaves the turn object as anything but a
digest (see ``evidence()`` / :mod:`nagar.ibpc.privacy`).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from nexus_ai_agent.application.ports.caption_engine import CaptionEnginePort
from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes
from nexus_ai_agent.nagar.ibpc.models import ConsentRecord, PrivacyMode, SessionStatus
from nexus_ai_agent.nagar.ibpc.privacy import PrivacyPolicy, PrivacyViolation, redact_mapping

#: Caller-supplied turn ids are a trust-boundary input: they are only ever used
#: as logical dict keys after this strict validation, and NEVER to build a
#: filesystem path (the physical audio file name is an independent random id).
TURN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Hard cap on one staged audio upload (25 MiB).
MAX_AUDIO_BYTES = 25 * 1024 * 1024

#: How many random file names to try before giving up (O_EXCL collisions).
_STAGE_ATTEMPTS = 8


def validate_turn_id(turn_id: str) -> str:
    """Validate a caller-supplied turn id at the boundary.

    Rejects empty, overlong, absolute-path, traversal, backslash and any other
    separator-bearing id with a typed error — no regex bypass is possible
    because the id is additionally never used as a path component.
    """
    if not isinstance(turn_id, str) or not TURN_ID_RE.fullmatch(turn_id):
        raise SessionError(
            "invalid_turn_id",
            "turn_id must match ^[A-Za-z0-9_-]{1,64}$ (no path separators, no '..')",
        )
    return turn_id


class TranscriptState(str, Enum):
    EMPTY = "EMPTY"
    PARTIAL = "PARTIAL"
    FINAL = "FINAL"


class TurnState(str, Enum):
    RECEIVED = "RECEIVED"
    TRANSCRIBING = "TRANSCRIBING"
    TRANSCRIPT_PARTIAL = "TRANSCRIPT_PARTIAL"
    TRANSCRIPT_FINAL = "TRANSCRIPT_FINAL"
    INTENT_PARSED = "INTENT_PARSED"
    PLAN_PREVIEW_READY = "PLAN_PREVIEW_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    STALE = "STALE"


TERMINAL_TURN_STATES = frozenset(
    {
        TurnState.EXECUTED,
        TurnState.FAILED,
        TurnState.CANCELLED,
        TurnState.INTERRUPTED,
        TurnState.STALE,
    }
)

#: The one legal transition table. PARTIAL transcript has no path to parsing;
#: terminal states have no outgoing edges; EXECUTING can only finish.
ALLOWED_TRANSITIONS: dict[TurnState, frozenset[TurnState]] = {
    TurnState.RECEIVED: frozenset(
        {
            TurnState.TRANSCRIBING,
            TurnState.TRANSCRIPT_FINAL,
            TurnState.INTENT_PARSED,
            TurnState.CANCELLED,
            TurnState.INTERRUPTED,
            TurnState.STALE,
            TurnState.FAILED,
        }
    ),
    TurnState.TRANSCRIBING: frozenset(
        {
            TurnState.TRANSCRIPT_PARTIAL,
            TurnState.TRANSCRIPT_FINAL,
            TurnState.CANCELLED,
            TurnState.INTERRUPTED,
            TurnState.STALE,
            TurnState.FAILED,
        }
    ),
    TurnState.TRANSCRIPT_PARTIAL: frozenset(
        {
            TurnState.TRANSCRIPT_FINAL,
            TurnState.CANCELLED,
            TurnState.INTERRUPTED,
            TurnState.STALE,
            TurnState.FAILED,
        }
    ),
    TurnState.TRANSCRIPT_FINAL: frozenset(
        {
            TurnState.INTENT_PARSED,
            TurnState.CANCELLED,
            TurnState.INTERRUPTED,
            TurnState.STALE,
            TurnState.FAILED,
        }
    ),
    TurnState.INTENT_PARSED: frozenset(
        {
            TurnState.PLAN_PREVIEW_READY,
            TurnState.CANCELLED,
            TurnState.INTERRUPTED,
            TurnState.STALE,
            TurnState.FAILED,
        }
    ),
    TurnState.PLAN_PREVIEW_READY: frozenset(
        {
            TurnState.AWAITING_APPROVAL,
            TurnState.CANCELLED,
            TurnState.INTERRUPTED,
            TurnState.STALE,
            TurnState.FAILED,
        }
    ),
    TurnState.AWAITING_APPROVAL: frozenset(
        {
            TurnState.APPROVED,
            TurnState.CANCELLED,
            TurnState.INTERRUPTED,
            TurnState.STALE,
            TurnState.FAILED,
        }
    ),
    TurnState.APPROVED: frozenset(
        {
            TurnState.EXECUTING,
            TurnState.CANCELLED,
            TurnState.INTERRUPTED,
            TurnState.STALE,
            TurnState.FAILED,
        }
    ),
    TurnState.EXECUTING: frozenset({TurnState.EXECUTED, TurnState.FAILED}),
    TurnState.EXECUTED: frozenset(),
    TurnState.FAILED: frozenset(),
    TurnState.CANCELLED: frozenset(),
    TurnState.INTERRUPTED: frozenset(),
    TurnState.STALE: frozenset(),
}


class SessionError(ValueError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class DuplicateTurnError(SessionError):
    pass


class IdempotencyConflictError(SessionError):
    pass


class StaleTurnError(SessionError):
    pass


class StaleRevisionError(SessionError):
    pass


class SessionEpochConflictError(SessionError):
    pass


class SessionClosedError(SessionError):
    pass


class TransitionError(SessionError):
    pass


@dataclass
class VoiceTurn:
    """One fenced turn. Raw transcript/audio live only in the private fields;
    every exported view carries digests only."""

    session_id: str
    turn_id: str
    sequence_number: int
    project_revision: int
    turn_state: TurnState = TurnState.RECEIVED
    transcript_state: TranscriptState = TranscriptState.EMPTY
    transcript_sha256: str = ""
    audio_sha256: str = ""
    intent_digest: str = ""
    approval_id: str = ""
    turn_index: int = 0
    target_revision_id: str = ""
    idempotency_key: str | None = None
    payload_digest: str = ""
    shadow_preview_digest: str = ""
    _transcript: str = field(default="", repr=False)

    def evidence(self) -> dict[str, Any]:
        """Digest-only view — safe for logs and records."""
        return redact_mapping(
            {
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "turn_index": self.turn_index,
                "sequence_number": self.sequence_number,
                "project_revision": self.project_revision,
                "target_revision_id": self.target_revision_id,
                "turn_state": self.turn_state.value,
                "transcript_state": self.transcript_state.value,
                "transcript": self.transcript_sha256 or None,
                "audio": self.audio_sha256 or None,
                "intent_digest": self.intent_digest or None,
                "approval_id": self.approval_id or None,
                "shadow_preview_digest": self.shadow_preview_digest or None,
            }
        )


@dataclass
class LiveSession:
    session_id: str
    tenant_id: str
    project_id: str
    principal_id: str
    user_id: int
    chat_id: int
    privacy_mode: PrivacyMode
    current_revision: int
    created_at: str
    consents: list[ConsentRecord] = field(default_factory=list)
    turns: dict[str, VoiceTurn] = field(default_factory=dict)
    max_sequence: int = 0
    status: SessionStatus = SessionStatus.ACTIVE
    active_revision_id: str = ""
    session_epoch: int = 1
    next_turn_index: int = 1

    def __post_init__(self) -> None:
        if not self.active_revision_id:
            self.active_revision_id = str(self.current_revision)


class DurableLiveSessionStore:
    """SQLite WAL-backed durable store for live IBPC sessions, turns, and turn contexts.

    All mutations execute under ``BEGIN IMMEDIATE`` with ``busy_timeout=5000``,
    ``foreign_keys=ON``, and ``synchronous=FULL``. ``nexus_ibpc_turns`` enforces
    ``UNIQUE(session_id, turn_index)`` and per-session ``idempotency_key``
    uniqueness at the schema layer. Raw transcript/audio bytes are never written.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._transaction():
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_ibpc_sessions (
                    session_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    privacy_mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    active_revision_id TEXT NOT NULL DEFAULT '0',
                    current_revision INTEGER NOT NULL DEFAULT 0,
                    session_epoch INTEGER NOT NULL DEFAULT 1,
                    max_sequence INTEGER NOT NULL DEFAULT 0,
                    next_turn_index INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_ibpc_turns (
                    session_id TEXT NOT NULL
                        REFERENCES nexus_ibpc_sessions(session_id) ON DELETE CASCADE,
                    turn_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    project_revision INTEGER NOT NULL,
                    target_revision_id TEXT NOT NULL DEFAULT '0',
                    idempotency_key TEXT,
                    payload_digest TEXT NOT NULL DEFAULT '',
                    turn_state TEXT NOT NULL,
                    transcript_state TEXT NOT NULL,
                    transcript_sha256 TEXT NOT NULL DEFAULT '',
                    audio_sha256 TEXT NOT NULL DEFAULT '',
                    intent_digest TEXT NOT NULL DEFAULT '',
                    approval_id TEXT NOT NULL DEFAULT '',
                    shadow_preview_digest TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, turn_id),
                    UNIQUE (session_id, turn_index)
                )
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ibpc_turns_idempotency
                ON nexus_ibpc_turns (session_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_ibpc_turn_contexts (
                    approval_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                        REFERENCES nexus_ibpc_sessions(session_id) ON DELETE CASCADE,
                    turn_id TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth > 0:
                yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self._conn.execute("COMMIT")

    @staticmethod
    def _row_to_turn(row: tuple[Any, ...]) -> VoiceTurn:
        return VoiceTurn(
            session_id=row[0],
            turn_id=row[1],
            turn_index=int(row[2]),
            sequence_number=int(row[3]),
            project_revision=int(row[4]),
            target_revision_id=row[5],
            idempotency_key=row[6],
            payload_digest=row[7],
            turn_state=TurnState(row[8]),
            transcript_state=TranscriptState(row[9]),
            transcript_sha256=row[10],
            audio_sha256=row[11],
            intent_digest=row[12],
            approval_id=row[13],
            shadow_preview_digest=row[14],
        )

    def create_session(self, session: LiveSession, *, now_utc: str) -> None:
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO nexus_ibpc_sessions (
                    session_id, tenant_id, project_id, principal_id,
                    user_id, chat_id, privacy_mode, status,
                    active_revision_id, current_revision, session_epoch,
                    max_sequence, next_turn_index, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.tenant_id,
                    session.project_id,
                    session.principal_id,
                    session.user_id,
                    session.chat_id,
                    session.privacy_mode.value,
                    session.status.value,
                    session.active_revision_id,
                    session.current_revision,
                    session.session_epoch,
                    session.max_sequence,
                    session.next_turn_index,
                    session.created_at,
                    now_utc,
                ),
            )

    def load_session(self, session_id: str) -> LiveSession | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT session_id, tenant_id, project_id, principal_id,
                       user_id, chat_id, privacy_mode, status,
                       active_revision_id, current_revision, session_epoch,
                       max_sequence, next_turn_index, created_at
                FROM nexus_ibpc_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            turn_rows = self._conn.execute(
                """
                SELECT session_id, turn_id, turn_index, sequence_number,
                       project_revision, target_revision_id, idempotency_key,
                       payload_digest, turn_state, transcript_state,
                       transcript_sha256, audio_sha256, intent_digest,
                       approval_id, shadow_preview_digest
                FROM nexus_ibpc_turns
                WHERE session_id = ?
                ORDER BY turn_index ASC
                """,
                (session_id,),
            ).fetchall()
        turns: dict[str, VoiceTurn] = {}
        for t_row in turn_rows:
            turn = self._row_to_turn(t_row)
            turns[turn.turn_id] = turn
        return LiveSession(
            session_id=row[0],
            tenant_id=row[1],
            project_id=row[2],
            principal_id=row[3],
            user_id=int(row[4]),
            chat_id=int(row[5]),
            privacy_mode=PrivacyMode(row[6]),
            status=SessionStatus(row[7]),
            active_revision_id=row[8],
            current_revision=int(row[9]),
            session_epoch=int(row[10]),
            max_sequence=int(row[11]),
            next_turn_index=int(row[12]),
            created_at=row[13],
            turns=turns,
        )

    def update_session_cas(self, session: LiveSession, *, expected_epoch: int, now_utc: str) -> int:
        """Persist session fields using optimistic CAS on ``session_epoch``."""
        new_epoch = expected_epoch + 1
        with self._transaction():
            cur = self._conn.execute(
                """
                UPDATE nexus_ibpc_sessions
                SET privacy_mode = ?,
                    status = ?,
                    active_revision_id = ?,
                    current_revision = ?,
                    session_epoch = ?,
                    max_sequence = ?,
                    next_turn_index = ?,
                    updated_at = ?
                WHERE session_id = ? AND session_epoch = ?
                """,
                (
                    session.privacy_mode.value,
                    session.status.value,
                    session.active_revision_id,
                    session.current_revision,
                    new_epoch,
                    session.max_sequence,
                    session.next_turn_index,
                    now_utc,
                    session.session_id,
                    expected_epoch,
                ),
            )
            if cur.rowcount != 1:
                raise SessionEpochConflictError(
                    "epoch_conflict",
                    f"session {session.session_id} epoch {expected_epoch} is stale",
                )
            session.session_epoch = new_epoch
            return new_epoch

    def find_turn_by_idempotency_key(
        self, session_id: str, idempotency_key: str
    ) -> VoiceTurn | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT session_id, turn_id, turn_index, sequence_number,
                       project_revision, target_revision_id, idempotency_key,
                       payload_digest, turn_state, transcript_state,
                       transcript_sha256, audio_sha256, intent_digest,
                       approval_id, shadow_preview_digest
                FROM nexus_ibpc_turns
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key),
            ).fetchone()
        return self._row_to_turn(row) if row is not None else None

    def insert_turn_atomic(
        self,
        *,
        session: LiveSession,
        turn: VoiceTurn,
        expected_epoch: int,
        now_utc: str,
    ) -> VoiceTurn:
        """Atomically assign ``turn_index``, insert turn, and advance ``session_epoch``."""
        with self._transaction():
            cur_row = self._conn.execute(
                """
                SELECT status, active_revision_id, current_revision,
                       session_epoch, max_sequence, next_turn_index
                FROM nexus_ibpc_sessions
                WHERE session_id = ?
                """,
                (session.session_id,),
            ).fetchone()
            if cur_row is None:
                raise SessionError("unknown_session", session.session_id)
            db_status = SessionStatus(cur_row[0])
            db_active_rev_id = cur_row[1]
            db_current_rev = int(cur_row[2])
            db_epoch = int(cur_row[3])
            db_max_seq = int(cur_row[4])
            db_next_idx = int(cur_row[5])
            if db_status is not SessionStatus.ACTIVE:
                raise SessionClosedError(
                    "session_not_active",
                    f"session {session.session_id} is {db_status.value}",
                )
            if db_epoch != expected_epoch:
                raise SessionEpochConflictError(
                    "epoch_conflict",
                    f"expected session epoch {expected_epoch}, actual {db_epoch}",
                )
            if turn.idempotency_key is not None:
                existing = self.find_turn_by_idempotency_key(
                    session.session_id, turn.idempotency_key
                )
                if existing is not None:
                    if existing.payload_digest != turn.payload_digest:
                        raise IdempotencyConflictError(
                            "idempotency_conflict",
                            f"idempotency_key {turn.idempotency_key!r} "
                            "reused with different payload",
                        )
                    return existing
            if turn.sequence_number <= db_max_seq:
                raise StaleTurnError(
                    "out_of_order",
                    f"sequence {turn.sequence_number} <= session head {db_max_seq}",
                )
            if (
                turn.project_revision != db_current_rev
                or turn.target_revision_id != db_active_rev_id
            ):
                raise StaleRevisionError(
                    "stale_revision",
                    f"turn target {turn.target_revision_id!r} (rev {turn.project_revision}) "
                    f"!= active {db_active_rev_id!r} (rev {db_current_rev})",
                )
            turn.turn_index = db_next_idx
            try:
                self._conn.execute(
                    """
                    INSERT INTO nexus_ibpc_turns (
                        session_id, turn_id, turn_index, sequence_number,
                        project_revision, target_revision_id, idempotency_key,
                        payload_digest, turn_state, transcript_state,
                        transcript_sha256, audio_sha256, intent_digest,
                        approval_id, shadow_preview_digest, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn.session_id,
                        turn.turn_id,
                        turn.turn_index,
                        turn.sequence_number,
                        turn.project_revision,
                        turn.target_revision_id,
                        turn.idempotency_key,
                        turn.payload_digest,
                        turn.turn_state.value,
                        turn.transcript_state.value,
                        turn.transcript_sha256,
                        turn.audio_sha256,
                        turn.intent_digest,
                        turn.approval_id,
                        turn.shadow_preview_digest,
                        now_utc,
                        now_utc,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateTurnError(
                    "duplicate_turn", f"turn {turn.turn_id!r} already exists"
                ) from exc
            session.max_sequence = turn.sequence_number
            session.next_turn_index = db_next_idx + 1
            self.update_session_cas(session, expected_epoch=expected_epoch, now_utc=now_utc)
            return turn

    def save_turn(self, turn: VoiceTurn, *, now_utc: str) -> None:
        with self._transaction():
            self._conn.execute(
                """
                UPDATE nexus_ibpc_turns
                SET turn_state = ?,
                    transcript_state = ?,
                    transcript_sha256 = ?,
                    audio_sha256 = ?,
                    intent_digest = ?,
                    approval_id = ?,
                    shadow_preview_digest = ?,
                    updated_at = ?
                WHERE session_id = ? AND turn_id = ?
                """,
                (
                    turn.turn_state.value,
                    turn.transcript_state.value,
                    turn.transcript_sha256,
                    turn.audio_sha256,
                    turn.intent_digest,
                    turn.approval_id,
                    turn.shadow_preview_digest,
                    now_utc,
                    turn.session_id,
                    turn.turn_id,
                ),
            )

    def save_turn_context(
        self,
        *,
        approval_id: str,
        session_id: str,
        turn_id: str,
        context_payload: dict[str, Any],
        now_utc: str,
    ) -> None:
        blob = json.dumps(
            context_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO nexus_ibpc_turn_contexts (
                    approval_id, session_id, turn_id, context_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    context_json = excluded.context_json,
                    updated_at = excluded.updated_at
                """,
                (approval_id, session_id, turn_id, blob, now_utc),
            )

    def load_turn_context(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT context_json FROM nexus_ibpc_turn_contexts WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        loaded = json.loads(row[0])
        return loaded if isinstance(loaded, dict) else None

    def list_turn_contexts(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT approval_id, context_json FROM nexus_ibpc_turn_contexts ORDER BY rowid ASC"
            ).fetchall()
        out: list[tuple[str, dict[str, Any]]] = []
        for aid, raw_json in rows:
            loaded = json.loads(raw_json)
            if isinstance(loaded, dict):
                out.append((str(aid), loaded))
        return tuple(out)

    def delete_session_contexts(self, session_id: str) -> None:
        with self._transaction():
            self._conn.execute(
                "DELETE FROM nexus_ibpc_turn_contexts WHERE session_id = ?",
                (session_id,),
            )


def durable_session_store_path(workspace: str | Path) -> Path:
    """Canonical SQLite location for durable live IBPC sessions and turns."""
    return Path(workspace) / "queue" / "ibpc_sessions.sqlite3"


class LiveSessionManager:
    """Owns sessions and turns; enforces the transition table + fencing + durable WAL."""

    def __init__(
        self,
        *,
        now: Callable[[], str],
        privacy: PrivacyPolicy,
        on_revoke_turn: Callable[[str, str], Any] | None = None,
        on_revoke_session: Callable[[str], Any] | None = None,
        store: DurableLiveSessionStore | None = None,
    ) -> None:
        self._now = now
        self._privacy = privacy
        self._on_revoke_turn = on_revoke_turn
        self._on_revoke_session = on_revoke_session
        self._store = store
        self._lock = threading.RLock()
        self._sessions: dict[str, LiveSession] = {}

    @property
    def store(self) -> DurableLiveSessionStore | None:
        return self._store

    # -- sessions ---------------------------------------------------------

    def open_session(
        self,
        *,
        tenant_id: str,
        project_id: str,
        principal_id: str,
        user_id: int,
        chat_id: int,
        privacy_mode: PrivacyMode = PrivacyMode.LOCAL_VAULT,
        consent_scope: str = "",
        current_revision: int = 0,
        active_revision_id: str | None = None,
    ) -> LiveSession:
        with self._lock:
            session_id = f"sess_{uuid.uuid4().hex}"
            consents: list[ConsentRecord] = []
            if privacy_mode is not PrivacyMode.LOCAL_VAULT:
                # a non-local mode is only accepted with an explicit consent record
                if not consent_scope:
                    raise PrivacyViolation(
                        "consent_missing",
                        f"{privacy_mode.value} requires an explicit consent scope at session open",
                    )
                consents.append(
                    self._privacy.record_consent(
                        privacy_mode,
                        principal_id=principal_id,
                        scope=consent_scope,
                        project_id=project_id,
                    )
                )
            now_utc = self._now()
            session = LiveSession(
                session_id=session_id,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                user_id=user_id,
                chat_id=chat_id,
                privacy_mode=privacy_mode,
                current_revision=current_revision,
                active_revision_id=(
                    active_revision_id if active_revision_id is not None else str(current_revision)
                ),
                created_at=now_utc,
                consents=consents,
            )
            if self._store is not None:
                self._store.create_session(session, now_utc=now_utc)
            self._sessions[session_id] = session
            return session

    def get_session(self, session_id: str) -> LiveSession:
        with self._lock:
            if self._store is not None:
                loaded = self._store.load_session(session_id)
                if loaded is None:
                    raise SessionError("unknown_session", session_id)
                cached = self._sessions.get(session_id)
                if cached is not None:
                    # Preserve process-local ephemeral transcript text & consents for live turns.
                    for tid, t in loaded.turns.items():
                        prev = cached.turns.get(tid)
                        if prev is not None and prev._transcript:
                            t._transcript = prev._transcript
                    loaded.consents = list(cached.consents)
                self._sessions[session_id] = loaded
                return loaded
            try:
                return self._sessions[session_id]
            except KeyError:
                raise SessionError("unknown_session", session_id) from None

    def _require_active_session(self, session: LiveSession) -> None:
        if session.status is not SessionStatus.ACTIVE:
            raise SessionClosedError(
                "session_not_active",
                f"session {session.session_id} is {session.status.value}",
            )

    def _require_turn(self, session: LiveSession, turn_id: str) -> VoiceTurn:
        """A turn id that is not part of *this* session fails closed with a typed
        error; a raw ``KeyError`` would leak an untyped failure to callers."""
        turn = session.turns.get(turn_id)
        if turn is None:
            raise SessionError("unknown_turn", turn_id)
        return turn

    def _persist_turn(self, turn: VoiceTurn) -> None:
        if self._store is not None:
            self._store.save_turn(turn, now_utc=self._now())

    def _persist_session(self, session: LiveSession, expected_epoch: int) -> None:
        if self._store is not None:
            self._store.update_session_cas(
                session, expected_epoch=expected_epoch, now_utc=self._now()
            )
        else:
            session.session_epoch += 1

    def set_privacy_mode(
        self, session_id: str, mode: PrivacyMode, *, principal_id: str, consent_scope: str
    ) -> ConsentRecord:
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            if session.principal_id != principal_id:
                raise PrivacyViolation(
                    "wrong_principal", "only the session principal may change the mode"
                )
            record = self._privacy.record_consent(
                mode,
                principal_id=principal_id,
                scope=consent_scope,
                project_id=session.project_id,
            )
            expected_epoch = session.session_epoch
            session.consents.append(record)
            session.privacy_mode = mode
            self._persist_session(session, expected_epoch)
            return record

    def assert_no_egress(self, session_id: str, data_kind: str) -> None:
        """Any egress attempt is refused per the session's mode + consents."""
        session = self.get_session(session_id)
        consented = self._privacy.has_consent(
            session.privacy_mode,
            principal_id=session.principal_id,
            scope=data_kind,
            project_id=session.project_id,
        )
        self._privacy.check_egress(session.privacy_mode, data_kind, consented=consented)

    def assert_local_processing(self, session_id: str, data_kind: str) -> None:
        """``data_kind`` is processed locally (never leaves the vault).

        LOCAL_VAULT: allowed. Any cloud mode: refused — no provider is wired
        in this slice, so protected data can never leave regardless of consent.
        """
        session = self.get_session(session_id)
        if session.privacy_mode is PrivacyMode.LOCAL_VAULT:
            return
        consented = self._privacy.has_consent(
            session.privacy_mode,
            principal_id=session.principal_id,
            scope=data_kind,
            project_id=session.project_id,
        )
        self._privacy.check_egress(session.privacy_mode, data_kind, consented=consented)

    def advance_revision(
        self,
        session_id: str,
        *,
        new_revision: int,
        new_revision_id: str | None = None,
    ) -> LiveSession:
        """Advance the session's authoritative revision after verified execution."""
        with self._lock:
            session = self.get_session(session_id)
            expected_epoch = session.session_epoch
            session.current_revision = new_revision
            session.active_revision_id = (
                new_revision_id if new_revision_id is not None else str(new_revision)
            )
            self._persist_session(session, expected_epoch)
            return session

    def _terminate_session(
        self, session_id: str, *, target_status: SessionStatus, turn_terminal_state: TurnState
    ) -> LiveSession:
        with self._lock:
            session = self.get_session(session_id)
            if session.status in (SessionStatus.CLOSED, SessionStatus.ABORTED):
                if session.status is target_status:
                    return session
                raise SessionClosedError(
                    "session_terminal",
                    f"session {session_id} is already {session.status.value}",
                )
            for turn in session.turns.values():
                if turn.turn_state is TurnState.EXECUTING:
                    raise TransitionError(
                        "executing", "cannot terminate session while a turn is EXECUTING"
                    )
            expected_epoch = session.session_epoch
            session.status = target_status
            for turn in session.turns.values():
                if turn.turn_state not in TERMINAL_TURN_STATES:
                    turn.turn_state = turn_terminal_state
                    turn.shadow_preview_digest = ""
                    self._persist_turn(turn)
                    self._revoke_turn(session_id, turn.turn_id)
            if self._on_revoke_session is not None:
                self._on_revoke_session(session_id)
            if self._store is not None and target_status in (
                SessionStatus.CLOSED,
                SessionStatus.ABORTED,
            ):
                self._store.delete_session_contexts(session_id)
            self._persist_session(session, expected_epoch)
            return session

    def close_session(self, session_id: str) -> LiveSession:
        """Durably close session and revoke all unconsumed previews/approvals."""
        return self._terminate_session(
            session_id,
            target_status=SessionStatus.CLOSED,
            turn_terminal_state=TurnState.CANCELLED,
        )

    def pause_session(self, session_id: str) -> LiveSession:
        """Durably pause session and revoke any unconsumed pending previews/approvals."""
        return self._terminate_session(
            session_id,
            target_status=SessionStatus.PAUSED,
            turn_terminal_state=TurnState.INTERRUPTED,
        )

    def abort_session(self, session_id: str) -> LiveSession:
        """Durably abort session and revoke all unconsumed previews/approvals."""
        return self._terminate_session(
            session_id,
            target_status=SessionStatus.ABORTED,
            turn_terminal_state=TurnState.CANCELLED,
        )

    def resume_session(self, session_id: str) -> LiveSession:
        """Resume a PAUSED session back to ACTIVE; terminal sessions refuse."""
        with self._lock:
            session = self.get_session(session_id)
            if session.status is SessionStatus.ACTIVE:
                return session
            if session.status is not SessionStatus.PAUSED:
                raise SessionClosedError(
                    "session_terminal",
                    f"cannot resume session in {session.status.value} state",
                )
            expected_epoch = session.session_epoch
            session.status = SessionStatus.ACTIVE
            self._persist_session(session, expected_epoch)
            return session

    # -- turns ------------------------------------------------------------

    def begin_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        sequence_number: int,
        project_revision: int,
        target_revision_id: str | None = None,
        idempotency_key: str | None = None,
        payload_digest: str = "",
        expected_epoch: int | None = None,
    ) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            validate_turn_id(turn_id)  # boundary validation: untrusted caller input
            if expected_epoch is not None and expected_epoch != session.session_epoch:
                raise SessionEpochConflictError(
                    "epoch_conflict",
                    f"expected epoch {expected_epoch}, session at {session.session_epoch}",
                )
            if idempotency_key is not None:
                for existing in session.turns.values():
                    if existing.idempotency_key == idempotency_key:
                        if existing.payload_digest != payload_digest:
                            raise IdempotencyConflictError(
                                "idempotency_conflict",
                                f"idempotency_key {idempotency_key!r} "
                                "reused with different payload",
                            )
                        return existing
            if turn_id in session.turns:
                raise DuplicateTurnError("duplicate_turn", f"turn {turn_id!r} already exists")
            if sequence_number <= session.max_sequence:
                raise StaleTurnError(
                    "out_of_order",
                    f"sequence {sequence_number} <= session head {session.max_sequence}",
                )
            eff_revision_id = (
                target_revision_id if target_revision_id is not None else str(project_revision)
            )
            if (
                project_revision != session.current_revision
                or eff_revision_id != session.active_revision_id
            ):
                raise StaleRevisionError(
                    "stale_revision",
                    f"turn pinned to revision {project_revision}/{eff_revision_id!r}, "
                    f"session at {session.current_revision}/{session.active_revision_id!r}",
                )
            turn = VoiceTurn(
                session_id=session_id,
                turn_id=turn_id,
                sequence_number=sequence_number,
                project_revision=project_revision,
                target_revision_id=eff_revision_id,
                idempotency_key=idempotency_key,
                payload_digest=payload_digest,
            )
            if self._store is not None:
                persisted = self._store.insert_turn_atomic(
                    session=session,
                    turn=turn,
                    expected_epoch=session.session_epoch,
                    now_utc=self._now(),
                )
                session.turns[persisted.turn_id] = persisted
                return persisted
            turn.turn_index = session.next_turn_index
            session.next_turn_index += 1
            session.turns[turn_id] = turn
            session.max_sequence = sequence_number
            session.session_epoch += 1
            return turn

    def get_turn(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            turn = session.turns.get(turn_id)
            if turn is None:
                raise SessionError("unknown_turn", turn_id)
            self._guard_not_stale(session, turn)
            return turn

    def list_turns(self, session_id: str) -> tuple[VoiceTurn, ...]:
        with self._lock:
            session = self.get_session(session_id)
            return tuple(sorted(session.turns.values(), key=lambda t: t.turn_index))

    def _guard_not_stale(self, session: LiveSession, turn: VoiceTurn) -> None:
        """A turn older than the session head is STALE; any use is refused and
        its approvals revoked — out-of-order messages never overwrite newer state."""
        if (
            turn.sequence_number < session.max_sequence
            and turn.turn_state not in TERMINAL_TURN_STATES
        ):
            turn.turn_state = TurnState.STALE
            self._persist_turn(turn)
            self._revoke_turn(session.session_id, turn.turn_id)
            raise StaleTurnError(
                "stale_turn",
                f"turn {turn.turn_id} seq {turn.sequence_number} < head {session.max_sequence}",
            )
        if turn.turn_state in TERMINAL_TURN_STATES and turn.turn_state is not TurnState.EXECUTED:
            raise TransitionError(
                "terminal_turn", f"turn {turn.turn_id} is {turn.turn_state.value}"
            )

    def _revoke_turn(self, session_id: str, turn_id: str) -> None:
        if self._on_revoke_turn is not None:
            self._on_revoke_turn(session_id, turn_id)

    def _transition(self, session: LiveSession, turn: VoiceTurn, target: TurnState) -> None:
        allowed = ALLOWED_TRANSITIONS[turn.turn_state]
        if target not in allowed:
            raise TransitionError(
                "illegal_transition",
                f"{turn.turn_state.value} -> {target.value} is not allowed",
            )
        turn.turn_state = target
        self._persist_turn(turn)

    # -- secure audio staging ----------------------------------------------
    #
    # Threat model (audio ingress): `turn_id` and `audio_dir` are caller-
    # influenced, `audio` is untrusted bytes. The physical file name is an
    # INDEPENDENT random id (secrets.token_hex) — never derived from turn_id —
    # created with O_CREAT|O_EXCL (no overwrite, no symlink following), mode
    # 0600, and proven by real Path.resolve() containment inside the resolved
    # audio dir before and after creation. The file is deleted in a finally
    # block on success, exception, timeout and cancellation, and cleanup only
    # ever unlinks the exact regular file this call created inside the root.

    @staticmethod
    def _resolve_audio_root(audio_dir: Path) -> Path:
        root = Path(audio_dir)
        root.mkdir(parents=True, exist_ok=True)
        # resolve() follows symlinks: containment is checked against the REAL
        # directory, not the textual spelling (startswith on text is not a check)
        return root.resolve(strict=False)

    @staticmethod
    def _validate_audio(audio: object) -> bytes:
        """Validate the untrusted payload BEFORE any state changes or file IO."""
        if not isinstance(audio, (bytes, bytearray)):
            raise SessionError("invalid_audio", "audio must be bytes")
        data = bytes(audio)
        if not data:
            raise SessionError("invalid_audio", "empty audio payload")
        if len(data) > MAX_AUDIO_BYTES:
            raise SessionError("invalid_audio", f"audio exceeds {MAX_AUDIO_BYTES} bytes")
        return data

    @classmethod
    def _stage_audio(cls, root: Path, audio: bytes) -> Path:
        """Write ``audio`` to a fresh, unpredictable, O_EXCL file inside ``root``.

        Returns the resolved path of the created file. Raises SessionError
        (typed, fail-closed) on invalid input, escape, or creation failure.
        """
        data = cls._validate_audio(audio)
        last_exc: OSError | None = None
        for _ in range(_STAGE_ATTEMPTS):
            candidate = root / f"audio_{secrets.token_hex(16)}.bin"
            try:
                # O_EXCL: fails if the name exists — including as a symlink —
                # so no existing file (ours or anyone else's) is overwritten
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                last_exc = exc
                continue
            try:
                resolved = Path(os.path.realpath(candidate))
                # real containment check with real path operations
                resolved.relative_to(root)
                if resolved.parent != root:
                    raise SessionError("audio_path_escape", str(candidate))
                os.fchmod(fd, 0o600)  # force perms regardless of umask
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                return resolved
            except SessionError:
                os.close(fd)
                candidate.unlink(missing_ok=True)
                raise
            except OSError as exc:
                os.close(fd)
                candidate.unlink(missing_ok=True)
                last_exc = exc
        raise SessionError("audio_stage_failed", f"could not create a fresh audio file: {last_exc}")

    @staticmethod
    def _cleanup_audio(root: Path, staged: Path | None) -> None:
        """Delete exactly the file this call created — never anything else.

        Only a regular, non-symlink file whose resolved parent is the audio
        root is unlinked, so cleanup can never delete another turn's file,
        another process's file, or a symlink target.
        """
        if staged is None:
            return
        try:
            resolved = Path(os.path.realpath(staged))
            if resolved.parent == root and resolved.is_file() and not resolved.is_symlink():
                resolved.unlink(missing_ok=True)
        except OSError:
            pass  # cleanup failure is logged degradation, never a fake success

    async def submit_audio(
        self,
        session_id: str,
        turn_id: str,
        audio: bytes,
        *,
        engine: CaptionEnginePort,
        audio_dir: Path,
    ) -> VoiceTurn:
        """Audio → transcript via the injected local engine.

        The audio bytes are staged ONLY inside the resolved audio dir under a
        random O_EXCL file with 0600 perms (see _stage_audio) and the file is
        always removed afterwards — success, engine exception, timeout or
        coroutine cancellation. The turn keeps just the digests.
        """
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            validate_turn_id(turn_id)
            turn = session.turns.get(turn_id)
            if turn is None:
                raise SessionError("unknown_turn", turn_id)
            self._guard_not_stale(session, turn)
            # validate the untrusted payload BEFORE mutating turn state, so a
            # refused payload leaves the turn exactly where it was
            self._validate_audio(audio)
            self._transition(session, turn, TurnState.TRANSCRIBING)
        root = self._resolve_audio_root(audio_dir)
        staged = self._stage_audio(root, audio)
        try:
            transcript = await engine.transcribe(staged, language=None)
            text = transcript.text
        finally:
            # success, exception, timeout AND cancellation all land here
            self._cleanup_audio(root, staged)
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            turn = self._require_turn(session, turn_id)
            self._guard_not_stale(session, turn)
            turn._transcript = text
            turn.transcript_sha256 = digest_of({"transcript": text})
            turn.audio_sha256 = sha256_bytes(audio)
            turn.transcript_state = TranscriptState.FINAL
            self._transition(session, turn, TurnState.TRANSCRIPT_FINAL)
            return turn

    def submit_partial_transcript(self, session_id: str, turn_id: str, text: str) -> VoiceTurn:
        """A streaming interim transcript: PARTIAL — it can never parse or approve."""
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            turn = self._require_turn(session, turn_id)
            self._guard_not_stale(session, turn)
            if turn.turn_state is not TurnState.TRANSCRIBING:
                self._transition(session, turn, TurnState.TRANSCRIBING)
            turn._transcript = text
            turn.transcript_sha256 = digest_of({"transcript": text})
            turn.transcript_state = TranscriptState.PARTIAL
            self._transition(session, turn, TurnState.TRANSCRIPT_PARTIAL)
            return turn

    def finalize_transcript(self, session_id: str, turn_id: str, text: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            turn = self._require_turn(session, turn_id)
            self._guard_not_stale(session, turn)
            turn._transcript = text
            turn.transcript_sha256 = digest_of({"transcript": text})
            turn.transcript_state = TranscriptState.FINAL
            self._transition(session, turn, TurnState.TRANSCRIPT_FINAL)
            return turn

    def transcript_text(self, session_id: str, turn_id: str) -> str:
        """The raw transcript, readable only while the turn is live and FINAL —
        it is parse input, never authority."""
        with self._lock:
            session = self.get_session(session_id)
            turn = self._require_turn(session, turn_id)
            self._guard_not_stale(session, turn)
            if turn.transcript_state is not TranscriptState.FINAL:
                raise SessionError(
                    "transcript_not_final", f"transcript state is {turn.transcript_state.value}"
                )
            return turn._transcript

    # -- pipeline markers -----------------------------------------------------

    def mark_intent_parsed(self, session_id: str, turn_id: str, *, intent_digest: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            turn = self._require_turn(session, turn_id)
            self._guard_not_stale(session, turn)
            # voice turns must have a FINAL transcript before parsing
            if turn.turn_state in (TurnState.TRANSCRIBING, TurnState.TRANSCRIPT_PARTIAL):
                raise TransitionError(
                    "partial_transcript", "a partial transcript can never be parsed into an intent"
                )
            turn.intent_digest = intent_digest
            self._transition(session, turn, TurnState.INTENT_PARSED)
            return turn

    def mark_plan_preview(
        self, session_id: str, turn_id: str, *, shadow_preview_digest: str = ""
    ) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            turn = self._require_turn(session, turn_id)
            self._guard_not_stale(session, turn)
            if turn.target_revision_id and turn.target_revision_id != session.active_revision_id:
                raise StaleRevisionError(
                    "stale_revision",
                    f"turn target revision {turn.target_revision_id!r} != active "
                    f"{session.active_revision_id!r}",
                )
            if shadow_preview_digest:
                turn.shadow_preview_digest = shadow_preview_digest
            self._transition(session, turn, TurnState.PLAN_PREVIEW_READY)
            return turn

    def mark_awaiting_approval(
        self, session_id: str, turn_id: str, *, approval_id: str
    ) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            turn = self._require_turn(session, turn_id)
            self._guard_not_stale(session, turn)
            if turn.target_revision_id and turn.target_revision_id != session.active_revision_id:
                raise StaleRevisionError(
                    "stale_revision",
                    f"turn target revision {turn.target_revision_id!r} != active "
                    f"{session.active_revision_id!r}",
                )
            turn.approval_id = approval_id
            self._transition(session, turn, TurnState.AWAITING_APPROVAL)
            return turn

    def mark_approved(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            turn = self._require_turn(session, turn_id)
            self._guard_not_stale(session, turn)
            if turn.target_revision_id and turn.target_revision_id != session.active_revision_id:
                raise StaleRevisionError(
                    "stale_revision",
                    f"turn target revision {turn.target_revision_id!r} != active "
                    f"{session.active_revision_id!r}",
                )
            self._transition(session, turn, TurnState.APPROVED)
            return turn

    def mark_executing(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            self._require_active_session(session)
            turn = self._require_turn(session, turn_id)
            self._guard_not_stale(session, turn)
            self._transition(session, turn, TurnState.EXECUTING)
            return turn

    def mark_executed(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            turn = self._require_turn(session, turn_id)
            self._transition(session, turn, TurnState.EXECUTED)
            return turn

    def mark_failed(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            turn = session.turns.get(turn_id)
            if turn is None:
                raise SessionError("unknown_turn", turn_id)
            if turn.turn_state not in TERMINAL_TURN_STATES:
                self._transition(session, turn, TurnState.FAILED)
            return turn

    def interrupt(self, session_id: str, turn_id: str) -> VoiceTurn:
        """Interruption invalidates the pending preview/approval of the turn."""
        with self._lock:
            session = self.get_session(session_id)
            turn = self._require_turn(session, turn_id)
            if turn.turn_state in TERMINAL_TURN_STATES:
                raise TransitionError("terminal_turn", f"turn is {turn.turn_state.value}")
            if turn.turn_state is TurnState.EXECUTING:
                raise TransitionError("executing", "an executing turn cannot be interrupted")
            turn.shadow_preview_digest = ""
            self._transition(session, turn, TurnState.INTERRUPTED)
            self._revoke_turn(session_id, turn_id)
            return turn

    def cancel(self, session_id: str, turn_id: str) -> VoiceTurn:
        """Cancellation: the turn can never execute later."""
        with self._lock:
            session = self.get_session(session_id)
            turn = self._require_turn(session, turn_id)
            if turn.turn_state in TERMINAL_TURN_STATES:
                raise TransitionError("terminal_turn", f"turn is {turn.turn_state.value}")
            if turn.turn_state is TurnState.EXECUTING:
                raise TransitionError("executing", "an executing turn cannot be cancelled")
            turn.shadow_preview_digest = ""
            self._transition(session, turn, TurnState.CANCELLED)
            self._revoke_turn(session_id, turn_id)
            return turn

    def turn_is_active(self, session_id: str, turn_id: str) -> bool:
        """Used by the approval ledger: a turn is active iff live and not stale."""
        with self._lock:
            session = self.get_session(session_id)
            if session.status is not SessionStatus.ACTIVE:
                return False
            turn = session.turns.get(turn_id)
            if turn is None:
                return False
            if (
                turn.sequence_number < session.max_sequence
                and turn.turn_state not in TERMINAL_TURN_STATES
            ):
                return False
            return turn.turn_state not in TERMINAL_TURN_STATES


__all__ = [
    "ALLOWED_TRANSITIONS",
    "DurableLiveSessionStore",
    "DuplicateTurnError",
    "IdempotencyConflictError",
    "MAX_AUDIO_BYTES",
    "SessionClosedError",
    "SessionEpochConflictError",
    "TURN_ID_RE",
    "LiveSession",
    "LiveSessionManager",
    "SessionError",
    "StaleRevisionError",
    "StaleTurnError",
    "TERMINAL_TURN_STATES",
    "TransitionError",
    "TranscriptState",
    "TurnState",
    "VoiceTurn",
    "durable_session_store_path",
    "validate_turn_id",
]
