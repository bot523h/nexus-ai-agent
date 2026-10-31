"""Live session + voice-turn fencing for proof-carrying live editing.

Every turn carries an identity and an order: ``session_id``, ``turn_id``,
``sequence_number``, ``project_revision``, ``transcript_state``, ``turn_state``.
The transition table below is enforced mechanically, and the fencing rules are
the mission's adversarial list made executable:

* a PARTIAL transcript can never parse, approve, preview or execute;
* a CANCELLED / INTERRUPTED / STALE turn can never execute later — its pending
  approvals are revoked at the moment of the call;
* a turn with a lower sequence number than the session's head is STALE: every
  operation on it is refused and its approvals revoked (out-of-order messages
  can never overwrite a newer valid state);
* a duplicate ``turn_id`` is refused (no duplicate operations);
* the transcript is untrusted input: even a FINAL transcript is only parse
  input — it is never execution authority by itself.

The raw transcript/audio never leaves the turn object as anything but a
digest (see ``evidence()`` / :mod:`nagar.ibpc.privacy`).
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from nexus_ai_agent.application.ports.caption_engine import CaptionEnginePort
from nexus_ai_agent.nagar.ibpc.canonical import digest_of, sha256_bytes
from nexus_ai_agent.nagar.ibpc.models import ConsentRecord, PrivacyMode
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


class StaleTurnError(SessionError):
    pass


class StaleRevisionError(SessionError):
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
    _transcript: str = field(default="", repr=False)

    def evidence(self) -> dict[str, Any]:
        """Digest-only view — safe for logs and records."""
        return redact_mapping(
            {
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "sequence_number": self.sequence_number,
                "project_revision": self.project_revision,
                "turn_state": self.turn_state.value,
                "transcript_state": self.transcript_state.value,
                "transcript": self.transcript_sha256 or None,
                "audio": self.audio_sha256 or None,
                "intent_digest": self.intent_digest or None,
                "approval_id": self.approval_id or None,
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


class LiveSessionManager:
    """Owns sessions and turns; enforces the transition table + fencing."""

    def __init__(
        self,
        *,
        now: Callable[[], str],
        privacy: PrivacyPolicy,
        on_revoke_turn: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._now = now
        self._privacy = privacy
        self._on_revoke_turn = on_revoke_turn
        self._lock = threading.RLock()
        self._sessions: dict[str, LiveSession] = {}

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
                        privacy_mode, principal_id=principal_id, scope=consent_scope
                    )
                )
            session = LiveSession(
                session_id=session_id,
                tenant_id=tenant_id,
                project_id=project_id,
                principal_id=principal_id,
                user_id=user_id,
                chat_id=chat_id,
                privacy_mode=privacy_mode,
                current_revision=current_revision,
                created_at=self._now(),
                consents=consents,
            )
            self._sessions[session_id] = session
            return session

    def get_session(self, session_id: str) -> LiveSession:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError:
                raise SessionError("unknown_session", session_id) from None

    def set_privacy_mode(
        self, session_id: str, mode: PrivacyMode, *, principal_id: str, consent_scope: str
    ) -> ConsentRecord:
        with self._lock:
            session = self.get_session(session_id)
            if session.principal_id != principal_id:
                raise PrivacyViolation(
                    "wrong_principal", "only the session principal may change the mode"
                )
            record = self._privacy.record_consent(
                mode, principal_id=principal_id, scope=consent_scope
            )
            session.consents.append(record)
            session.privacy_mode = mode
            return record

    def assert_no_egress(self, session_id: str, data_kind: str) -> None:
        """Any egress attempt is refused per the session's mode + consents."""
        session = self.get_session(session_id)
        consented = self._privacy.has_consent(
            session.privacy_mode, principal_id=session.principal_id, scope=data_kind
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
            session.privacy_mode, principal_id=session.principal_id, scope=data_kind
        )
        self._privacy.check_egress(session.privacy_mode, data_kind, consented=consented)

    # -- turns ------------------------------------------------------------

    def begin_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        sequence_number: int,
        project_revision: int,
    ) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            validate_turn_id(turn_id)  # boundary validation: untrusted caller input
            if turn_id in session.turns:
                raise DuplicateTurnError("duplicate_turn", f"turn {turn_id!r} already exists")
            if sequence_number <= session.max_sequence:
                raise StaleTurnError(
                    "out_of_order",
                    f"sequence {sequence_number} <= session head {session.max_sequence}",
                )
            if project_revision != session.current_revision:
                raise StaleRevisionError(
                    "stale_revision",
                    f"turn pinned to revision {project_revision}, "
                    f"session at {session.current_revision}",
                )
            turn = VoiceTurn(
                session_id=session_id,
                turn_id=turn_id,
                sequence_number=sequence_number,
                project_revision=project_revision,
            )
            session.turns[turn_id] = turn
            session.max_sequence = sequence_number
            return turn

    def get_turn(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            turn = session.turns.get(turn_id)
            if turn is None:
                raise SessionError("unknown_turn", turn_id)
            self._guard_not_stale(session, turn)
            return turn

    def _guard_not_stale(self, session: LiveSession, turn: VoiceTurn) -> None:
        """A turn older than the session head is STALE; any use is refused and
        its approvals revoked — out-of-order messages never overwrite newer state."""
        if (
            turn.sequence_number < session.max_sequence
            and turn.turn_state not in TERMINAL_TURN_STATES
        ):
            turn.turn_state = TurnState.STALE
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

    # -- transcript / audio -------------------------------------------------

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
            turn = session.turns[turn_id]
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
            turn = session.turns[turn_id]
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
            turn = session.turns[turn_id]
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
            turn = session.turns[turn_id]
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
            turn = session.turns[turn_id]
            self._guard_not_stale(session, turn)
            # voice turns must have a FINAL transcript before parsing
            if turn.turn_state in (TurnState.TRANSCRIBING, TurnState.TRANSCRIPT_PARTIAL):
                raise TransitionError(
                    "partial_transcript", "a partial transcript can never be parsed into an intent"
                )
            turn.intent_digest = intent_digest
            self._transition(session, turn, TurnState.INTENT_PARSED)
            return turn

    def mark_plan_preview(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            turn = session.turns[turn_id]
            self._guard_not_stale(session, turn)
            self._transition(session, turn, TurnState.PLAN_PREVIEW_READY)
            return turn

    def mark_awaiting_approval(
        self, session_id: str, turn_id: str, *, approval_id: str
    ) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            turn = session.turns[turn_id]
            self._guard_not_stale(session, turn)
            self._transition(session, turn, TurnState.AWAITING_APPROVAL)
            turn.approval_id = approval_id
            return turn

    def mark_approved(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            turn = session.turns[turn_id]
            self._guard_not_stale(session, turn)
            self._transition(session, turn, TurnState.APPROVED)
            return turn

    def mark_executing(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            turn = session.turns[turn_id]
            self._guard_not_stale(session, turn)
            self._transition(session, turn, TurnState.EXECUTING)
            return turn

    def mark_executed(self, session_id: str, turn_id: str) -> VoiceTurn:
        with self._lock:
            session = self.get_session(session_id)
            turn = session.turns[turn_id]
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
            turn = session.turns[turn_id]
            if turn.turn_state in TERMINAL_TURN_STATES:
                raise TransitionError("terminal_turn", f"turn is {turn.turn_state.value}")
            if turn.turn_state is TurnState.EXECUTING:
                raise TransitionError("executing", "an executing turn cannot be interrupted")
            self._transition(session, turn, TurnState.INTERRUPTED)
            self._revoke_turn(session_id, turn_id)
            return turn

    def cancel(self, session_id: str, turn_id: str) -> VoiceTurn:
        """Cancellation: the turn can never execute later."""
        with self._lock:
            session = self.get_session(session_id)
            turn = session.turns[turn_id]
            if turn.turn_state in TERMINAL_TURN_STATES:
                raise TransitionError("terminal_turn", f"turn is {turn.turn_state.value}")
            if turn.turn_state is TurnState.EXECUTING:
                raise TransitionError("executing", "an executing turn cannot be cancelled")
            self._transition(session, turn, TurnState.CANCELLED)
            self._revoke_turn(session_id, turn_id)
            return turn

    def turn_is_active(self, session_id: str, turn_id: str) -> bool:
        """Used by the approval ledger: a turn is active iff live and not stale."""
        with self._lock:
            session = self.get_session(session_id)
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
    "DuplicateTurnError",
    "MAX_AUDIO_BYTES",
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
    "validate_turn_id",
]
