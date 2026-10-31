"""Unit tests: live session + voice-turn fencing (M4).

The adversarial list made executable: partial transcripts never parse,
cancelled/interrupted/stale turns never execute, duplicate turns refused,
out-of-order sequences are stale and revoke their approvals, transcripts are
exposed only as digests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import PrivacyMode
from nexus_ai_agent.nagar.ibpc.privacy import PrivacyPolicy, PrivacyViolation
from nexus_ai_agent.nagar.ibpc.session import (
    ALLOWED_TRANSITIONS,
    DuplicateTurnError,
    LiveSessionManager,
    SessionError,
    StaleRevisionError,
    StaleTurnError,
    TranscriptState,
    TransitionError,
    TurnState,
)

NOW = lambda: "2026-10-31T00:00:00Z"  # noqa: E731


def _manager(revoked: list[tuple[str, str]] | None = None) -> LiveSessionManager:
    return LiveSessionManager(
        now=NOW,
        privacy=PrivacyPolicy(now=NOW),
        on_revoke_turn=(lambda s, t: revoked.append((s, t))) if revoked is not None else None,
    )


def _open(mgr: LiveSessionManager, **kw) -> str:
    base = dict(
        tenant_id="ten",
        project_id="p1",
        principal_id="user-1",
        user_id=42,
        chat_id=4242,
        current_revision=7,
    )
    base.update(kw)
    return mgr.open_session(**base).session_id


class TestSessions:
    def test_local_vault_is_the_default_mode(self) -> None:
        mgr = _manager()
        sid = _open(mgr)
        session = mgr.get_session(sid)
        assert session.privacy_mode is PrivacyMode.LOCAL_VAULT
        assert session.consents == []

    def test_cloud_mode_without_consent_is_refused(self) -> None:
        mgr = _manager()
        with pytest.raises(PrivacyViolation) as exc:
            _open(mgr, privacy_mode=PrivacyMode.TRUSTED_CLOUD)
        assert exc.value.reason == "consent_missing"

    def test_cloud_mode_with_explicit_consent_is_recorded(self) -> None:
        mgr = _manager()
        sid = _open(
            mgr, privacy_mode=PrivacyMode.TRUSTED_CLOUD, consent_scope="transcript:local-engine"
        )
        session = mgr.get_session(sid)
        assert session.privacy_mode is PrivacyMode.TRUSTED_CLOUD
        assert len(session.consents) == 1
        assert session.consents[0].consent_digest.startswith("sha256:")

    def test_egress_is_always_refused_in_this_slice(self) -> None:
        mgr = _manager()
        sid = _open(mgr)
        with pytest.raises(PrivacyViolation):
            mgr.assert_no_egress(sid, "audio")


class TestTurnFencing:
    def test_duplicate_turn_id_refused(self) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        with pytest.raises(DuplicateTurnError):
            mgr.begin_turn(sid, "t1", sequence_number=2, project_revision=7)

    def test_out_of_order_sequence_is_stale(self) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t2", sequence_number=5, project_revision=7)
        with pytest.raises(StaleTurnError):
            mgr.begin_turn(sid, "t1", sequence_number=3, project_revision=7)

    def test_stale_project_revision_refused(self) -> None:
        mgr = _manager()
        sid = _open(mgr)
        with pytest.raises(StaleRevisionError):
            mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=6)

    def test_older_turn_becomes_stale_and_is_revoked_on_use(self) -> None:
        revoked: list[tuple[str, str]] = []
        mgr = _manager(revoked)
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        mgr.begin_turn(sid, "t2", sequence_number=2, project_revision=7)
        with pytest.raises(StaleTurnError):
            mgr.get_turn(sid, "t1")
        turn = mgr.get_session(sid).turns["t1"]
        assert turn.turn_state is TurnState.STALE
        assert (sid, "t1") in revoked
        assert mgr.turn_is_active(sid, "t1") is False
        assert mgr.turn_is_active(sid, "t2") is True

    def test_partial_transcript_can_never_parse(self) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        mgr.submit_partial_transcript(sid, "t1", "trim from…")
        turn = mgr.get_session(sid).turns["t1"]
        assert turn.transcript_state is TranscriptState.PARTIAL
        with pytest.raises(TransitionError) as exc:
            mgr.mark_intent_parsed(sid, "t1", intent_digest=digest_of({"i": 1}))
        assert exc.value.reason == "partial_transcript"
        with pytest.raises(SessionError):
            mgr.transcript_text(sid, "t1")  # not FINAL
        # finalizing makes it parseable — the transcript is input, not authority
        mgr.finalize_transcript(sid, "t1", "trim from 0.5 to 1.5 seconds")
        mgr.mark_intent_parsed(sid, "t1", intent_digest=digest_of({"i": 1}))
        assert mgr.get_session(sid).turns["t1"].turn_state is TurnState.INTENT_PARSED

    def test_interrupt_revokes_and_blocks_execution(self) -> None:
        revoked: list[tuple[str, str]] = []
        mgr = _manager(revoked)
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        mgr.mark_intent_parsed(sid, "t1", intent_digest=digest_of({"i": 1}))
        mgr.mark_plan_preview(sid, "t1")
        mgr.mark_awaiting_approval(sid, "t1", approval_id="appr_1")
        mgr.interrupt(sid, "t1")
        assert (sid, "t1") in revoked
        turn = mgr.get_session(sid).turns["t1"]
        assert turn.turn_state is TurnState.INTERRUPTED
        with pytest.raises(TransitionError):
            mgr.mark_executing(sid, "t1")

    def test_cancel_blocks_execution(self) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        mgr.cancel(sid, "t1")
        with pytest.raises(TransitionError):
            mgr.mark_executing(sid, "t1")
        with pytest.raises(TransitionError):
            mgr.cancel(sid, "t1")  # terminal

    def test_executing_turn_cannot_be_interrupted(self) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        mgr.mark_intent_parsed(sid, "t1", intent_digest=digest_of({"i": 1}))
        mgr.mark_plan_preview(sid, "t1")
        mgr.mark_awaiting_approval(sid, "t1", approval_id="appr_1")
        mgr.mark_approved(sid, "t1")
        mgr.mark_executing(sid, "t1")
        with pytest.raises(TransitionError) as exc:
            mgr.interrupt(sid, "t1")
        assert exc.value.reason == "executing"

    def test_transition_table_is_closed(self) -> None:
        # every terminal state has no outgoing edges
        for state in (
            TurnState.EXECUTED,
            TurnState.FAILED,
            TurnState.CANCELLED,
            TurnState.INTERRUPTED,
            TurnState.STALE,
        ):
            assert ALLOWED_TRANSITIONS[state] == frozenset()
        # PARTIAL transcript has no path to parsing
        assert TurnState.INTENT_PARSED not in ALLOWED_TRANSITIONS[TurnState.TRANSCRIPT_PARTIAL]

    def test_evidence_carries_digests_only(self) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        mgr.finalize_transcript(sid, "t1", "super secret transcript words")
        turn = mgr.get_session(sid).turns["t1"]
        evidence = turn.evidence()
        assert "super secret transcript words" not in str(evidence)
        assert evidence["transcript"].startswith("sha256:")
        assert evidence["turn_state"] == "TRANSCRIPT_FINAL"


class _FakeEngine:
    """A local, declared test double for the caption port (no cloud, no mock of
    the execution path — transcription is the only faked seam, as declared)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def is_available(self) -> bool:
        return True

    async def transcribe(self, audio_path, *, language=None):  # noqa: ANN001, ANN202
        from nexus_ai_agent.creative.packs.caption.models import TranscriptRef, TranscriptSegment

        return TranscriptRef(
            transcript_id="tr_1",
            segments=(TranscriptSegment(start_us=0, end_us=1_000_000, text=self._text),),
        )


class TestVoiceTurns:
    @pytest.mark.asyncio
    async def test_audio_to_final_transcript_via_local_engine(self, tmp_path: Path) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        turn = await mgr.submit_audio(
            sid,
            "t1",
            b"fake-audio-bytes",
            engine=_FakeEngine("trim from 0.5 to 1.5 seconds"),
            audio_dir=tmp_path / "audio",
        )
        assert turn.transcript_state is TranscriptState.FINAL
        assert turn.turn_state is TurnState.TRANSCRIPT_FINAL
        assert turn.audio_sha256.startswith("sha256:")
        assert "fake-audio-bytes" not in str(turn.evidence())
        assert mgr.transcript_text(sid, "t1") == "trim from 0.5 to 1.5 seconds"
        # audio file staged inside the guarded dir only
        assert (tmp_path / "audio" / "t1.bin").read_bytes() == b"fake-audio-bytes"
