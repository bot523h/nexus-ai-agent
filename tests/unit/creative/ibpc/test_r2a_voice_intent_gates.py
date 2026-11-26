"""Wave 3-R2a — the voice boundary runs the same gates as the text boundary.

The audit found ``submit_voice_intent`` bypassing every Wave 3 gate that
``submit_text_intent`` enforces: it consulted only the legacy in-memory policy,
never the durable ``AUDIO`` consent, never authorized ``TURN_CREATE``, and
``_continue_with_text`` never persisted the voice turn durably.

These tests pin the closed boundary end to end:

* no durable ``AUDIO`` grant → the transcription engine is **never called**
  (0 calls), and a revoked grant refuses the same way;
* ``VIEWER`` / ``AUDITOR`` are refused before the turn fence opens or a byte of
  audio is transcribed;
* a revoked durable ``VIDEO`` grant blocks the preview/media evidence of a
  voice-initiated turn even though the text was authorized;
* successful voice turns are durable (monotonic ``turn_index``, idempotent
  replay, digest-only utterance) and failed ones are too (REJECTED with a typed
  refusal reason, never raw audio or transcript).
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

import nexus_ai_agent.nagar.ibpc.service as service_mod
from nexus_ai_agent.nagar.ibpc.approval import DurableCanonicalApprovalLedger
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.authority import PrincipalFacts
from nexus_ai_agent.nagar.ibpc.live_session import DurableLiveSessionStore, TurnStatus
from nexus_ai_agent.nagar.ibpc.models import (
    CapabilityIdentity,
    CompiledPlan,
    PlannedStep,
)
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    DurableStudioAuthorizationService,
    MembershipRole,
)
from nexus_ai_agent.nagar.ibpc.privacy import (
    ConsentAction,
    ConsentScope,
    DurablePrivacyConsentStore,
)
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps
from nexus_ai_agent.nagar.ibpc.session import TurnState

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
#: Every grant is time-bounded; this is the consent window under test.
CONSENT_EXPIRY = "2026-12-31T00:00:00Z"
KEY = b"v" * 32
PRINCIPAL = "prn_" + "5" * 32
PROJECT = "project-r2a"
BINDING_REF = "cbn_" + "c" * 64
TRANSCRIPT = "trim from 0.2 to 0.6 seconds"


class _Engine:
    """Caption port double that counts how many times it was consulted."""

    def __init__(self, text: str = TRANSCRIPT, *, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0

    def is_available(self) -> bool:
        return True

    async def transcribe(self, audio_path, *, language=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        # The caption port's contract for this boundary is ``transcript.text``
        # (session.submit_audio reads nothing else), so the double stays a plain
        # object: this module must not import a creative pack at all — an
        # import here would drag the voice boundary into the pack-coverage
        # contract and tempt a HOST_LAYER_PACK_IMPORTERS exemption.
        return types.SimpleNamespace(
            text=self.text,
            segments=(types.SimpleNamespace(start_us=0, end_us=1_000_000, text=self.text),),
        )


def _stub_compile(ast, *, registry):
    """A deterministic, pack-free stand-in for the registry-validated compiler."""
    return CompiledPlan(
        ast_digest=ast.ast_digest,
        capability=CapabilityIdentity(
            operation_id=ast.operation,
            schema_version=1,
            permission_level="read",
            execution_modes=("local",),
            required_packs=(),
            required_permissions=(),
            schema_digest="sha256:" + "e" * 64,
        ),
        steps=(
            PlannedStep(
                operation=ast.operation,
                input=dict(ast.params),
                target={"project_id": ast.project_id, "asset_id": ast.asset_id},
            ),
        ),
    )


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
        self.enqueued.append(payload)
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str):
        from nexus_ai_agent.application.ports.job_queue import JobStatus

        return JobStatus.FAILED_TERMINAL

    async def get_result(self, job_id: str):
        return {"success": False, "error": "not executed in this test"}


class _Authority:
    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        return PrincipalFacts(
            principal_id=principal_id,
            project_id=project_id,
            channel_binding_id=binding_ref,
        )


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        service_mod,
        "probe_media",
        lambda path, *, binary=None: (types.SimpleNamespace(duration_us=3_000_000), 30.0),
    )

    def fake_shadow(path, scope, plan_digest, **kwargs):
        payload = {
            "plan_digest": plan_digest,
            "scope_digest": scope.scope_digest,
            "source_duration_us": 3_000_000,
            "scope_start_us": scope.start_us,
            "scope_end_us": scope.end_us,
            "keyframes": [],
            "composite_digest": "sha256:" + "0" * 64,
        }
        return types.SimpleNamespace(
            plan_digest=plan_digest,
            scope_digest=scope.scope_digest,
            keyframes=(),
            model_dump=lambda **_kw: payload,
        )

    def fake_signature(path, scope, **kwargs):
        payload = {
            "scope_digest": scope.scope_digest,
            "asset_id": scope.asset_id,
            "start_us": scope.start_us,
            "end_us": scope.end_us,
            "color_histogram": [0.0] * 64,
            "motion_magnitude": 0.0,
            "sample_frames": 1,
            "computed_at": NOW(),
            "signature_digest": "sha256:" + "1" * 64,
        }
        return types.SimpleNamespace(
            signature_digest=payload["signature_digest"],
            scope_digest=scope.scope_digest,
            model_dump=lambda **_kw: payload,
        )

    shadow_calls: list[str] = []

    def counting_shadow(path, scope, plan_digest, **kwargs):
        shadow_calls.append(plan_digest)
        return fake_shadow(path, scope, plan_digest, **kwargs)

    monkeypatch.setattr(service_mod, "build_shadow_preview", counting_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)
    # The registry-validated compiler is exercised by its own suite
    # (tests/unit/test_ibpc_intent_compiler.py) against the real composition.
    # Here the voice *boundary* is under test, so the compile step is stubbed:
    # that keeps this module free of any creative-pack import.
    monkeypatch.setattr(service_mod, "compile_ast", _stub_compile)

    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)

    authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
    authz.register_principal(PRINCIPAL)
    binding = authz.bind_channel(principal_id=PRINCIPAL, channel="telegram", external_id="tg-r2a")
    authz.grant_membership(project_id=PROJECT, principal_id=PRINCIPAL, role=MembershipRole.OWNER)
    consent = DurablePrivacyConsentStore(tmp_path / "consent.sqlite3", now=NOW)
    sessions = DurableLiveSessionStore(tmp_path / "sessions.sqlite3", now=NOW)
    ledger = DurableCanonicalApprovalLedger(
        tmp_path / "approvals.sqlite3",
        key_provider=StaticKeyRing({"unit": KEY}, active_key_id="unit"),
        now=NOW,
    )
    queue = _Queue()
    engine = _Engine()
    service = IBPCService(
        IBPCServiceDeps(
            queue=queue,  # type: ignore[arg-type]
            registry=None,  # type: ignore[arg-type]  # stub compiler, no pack composition
            workspace_root=tmp_path,
            revision_provider=lambda: 7,
            now=NOW,
            caption_engine=engine,  # type: ignore[arg-type]
            approval_key_provider=StaticKeyRing({"unit": KEY}, active_key_id="unit"),
            approval_ledger=ledger,
            session_store=sessions,
            principal_authority=authz,
            privacy_consent_store=consent,
            channel_binding_ref=binding,
            authority=_Authority(),
        )
    )

    def open_session() -> str:
        return service.open_session(
            tenant_id="ten",
            project_id=PROJECT,
            principal_id=PRINCIPAL,
            user_id=1,
            chat_id=1,
            current_revision=7,
        )

    def voice(sid: str, turn_id: str = "voice-1", sequence: int = 1):
        return asyncio.run(
            service.submit_voice_intent(
                sid,
                turn_id,
                sequence_number=sequence,
                audio=b"audio-bytes",
                asset_id="src",
                source_path=str(clip),
                audio_dir=tmp_path / f"audio-{turn_id}",
            )
        )

    yield types.SimpleNamespace(
        service=service,
        queue=queue,
        engine=engine,
        clip=clip,
        consent=consent,
        sessions=sessions,
        ledger=ledger,
        authz=authz,
        binding=binding,
        tmp_path=tmp_path,
        open_session=open_session,
        voice=voice,
        shadow_calls=shadow_calls,
        set_role=lambda role: authz.grant_membership(
            project_id=PROJECT, principal_id=PRINCIPAL, role=role
        ),
    )
    ledger.close()
    sessions.close()
    consent.close()
    authz.close()


def _grant_audio(env) -> None:
    env.consent.grant_consent(
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        provider_id="local",
        data_category=ConsentScope.AUDIO,
        action=ConsentAction.LOCAL_PROCESSING,
        expires_at_utc=CONSENT_EXPIRY,
    )


def _grant_video(env) -> None:
    env.consent.grant_consent(
        principal_id=PRINCIPAL,
        project_id=PROJECT,
        provider_id="local",
        data_category=ConsentScope.VIDEO,
        action=ConsentAction.LOCAL_PROCESSING,
        expires_at_utc=CONSENT_EXPIRY,
    )


class TestTheDurableAudioConsentGate:
    def test_without_an_audio_grant_the_engine_is_never_called(self, env) -> None:
        sid = env.open_session()
        result = env.voice(sid)
        assert type(result).__name__ == "Refusal", result
        assert "consent" in result.reason
        assert env.engine.calls == 0
        # nothing was fenced and nothing was persisted
        assert env.service.sessions.get_session(sid).turns == {}
        assert env.sessions.list_turns(sid) == ()

    def test_a_revoked_audio_grant_refuses_before_transcription(self, env) -> None:
        _grant_audio(env)
        env.consent.revoke_consent(
            principal_id=PRINCIPAL,
            project_id=PROJECT,
            provider_id="local",
            data_category=ConsentScope.AUDIO,
            action=ConsentAction.LOCAL_PROCESSING,
        )
        sid = env.open_session()
        result = env.voice(sid)
        assert type(result).__name__ == "Refusal", result
        assert env.engine.calls == 0

    def test_a_live_audio_grant_lets_the_turn_proceed(self, env) -> None:
        _grant_audio(env)
        _grant_video(env)
        sid = env.open_session()
        proposal = env.voice(sid)
        assert type(proposal).__name__ == "PipelineResult", proposal
        assert env.engine.calls == 1


class TestTurnCreateAuthorization:
    @pytest.mark.parametrize("role", [MembershipRole.VIEWER, MembershipRole.AUDITOR])
    def test_read_only_roles_never_transcribe(self, env, role) -> None:
        _grant_audio(env)
        _grant_video(env)
        # the session is opened while the principal is still an OWNER, then the
        # role is downgraded: TURN_CREATE must be re-verified on EVERY turn, not
        # inherited from the session-start grant.
        sid = env.open_session()
        env.set_role(role)
        result = env.voice(sid)
        assert type(result).__name__ == "Refusal", result
        assert result.reason == "unauthorized_principal"
        assert "insufficient_role" in result.detail
        assert env.engine.calls == 0
        assert env.service.sessions.get_session(sid).turns == {}
        assert env.sessions.list_turns(sid) == ()

    def test_an_editor_may_create_a_voice_turn(self, env) -> None:
        _grant_audio(env)
        _grant_video(env)
        env.set_role(MembershipRole.EDITOR)
        sid = env.open_session()
        proposal = env.voice(sid)
        assert type(proposal).__name__ == "PipelineResult", proposal


class TestTheDurableVideoConsentGate:
    def test_a_revoked_video_grant_blocks_the_voice_preview(self, env) -> None:
        _grant_audio(env)  # audio is fine; the target-media evidence is not
        sid = env.open_session()
        result = env.voice(sid)
        assert type(result).__name__ == "Refusal", result
        assert "consent" in result.reason
        # the transcript happened, but no media evidence was built and no
        # approval exists to consume
        assert env.engine.calls == 1
        assert env.shadow_calls == []
        assert env.service.approvals._all() == ()
        assert env.queue.enqueued == []

    def test_the_video_grant_is_required_for_the_preview_not_the_audio(self, env) -> None:
        _grant_audio(env)
        _grant_video(env)
        sid = env.open_session()
        proposal = env.voice(sid)
        assert type(proposal).__name__ == "PipelineResult", proposal
        assert len(env.shadow_calls) == 1


class TestVoiceTurnsAreDurable:
    def test_a_successful_voice_turn_is_persisted_with_a_monotonic_index(self, env) -> None:
        _grant_audio(env)
        _grant_video(env)
        sid = env.open_session()
        first = env.voice(sid, "voice-1", sequence=1)
        assert type(first).__name__ == "PipelineResult", first
        second = env.voice(sid, "voice-2", sequence=2)
        assert type(second).__name__ == "PipelineResult", second
        turns = env.sessions.list_turns(sid)
        assert [turn.turn_index for turn in turns] == [1, 2]
        assert turns[0].turn_status is TurnStatus.PREVIEW_READY
        assert turns[0].approval_id == first.approval_id
        # the utterance is stored as a digest, never as text
        assert turns[0].user_utterance.startswith("sha256:")
        assert TRANSCRIPT not in turns[0].user_utterance

    def test_a_replayed_voice_turn_is_idempotent(self, env) -> None:
        _grant_audio(env)
        _grant_video(env)
        sid = env.open_session()
        first = env.voice(sid, "voice-1", sequence=1)
        assert type(first).__name__ == "PipelineResult", first
        # a second submission of the same (session, turn) reuses the durable row
        replay = env.sessions.submit_turn(
            session_id=sid,
            turn_id="voice-1",
            idempotency_key="ibpc-turn:" + sid + ":voice-1",
            user_utterance=TRANSCRIPT,
            base_revision_id=env.sessions.get_session(sid).active_revision_id,
            scope_signature="sha256:" + "0" * 64,
            plan=first.plan,
            preview=first.preview,
            approval_id=first.approval_id,
            turn_status=TurnStatus.PREVIEW_READY,
        )
        assert replay.turn_index == 1
        assert len(env.sessions.list_turns(sid)) == 1

    def test_a_failed_transcription_is_recorded_as_a_typed_rejection(self, env) -> None:
        _grant_audio(env)
        _grant_video(env)
        failing = _Engine(error=RuntimeError("decoder failure"))
        env.service._deps.caption_engine = failing  # type: ignore[assignment]
        sid = env.open_session()
        result = env.voice(sid, "voice-fail")
        assert type(result).__name__ == "Refusal", result
        assert result.reason == "transcription_failed"
        turns = env.sessions.list_turns(sid)
        assert len(turns) == 1
        assert turns[0].turn_status is TurnStatus.REJECTED
        assert turns[0].refusal_reason is not None
        assert turns[0].refusal_reason.startswith("transcription_failed")
        # no plan was invented and no raw audio/transcript was written
        with pytest.raises(Exception) as exc:  # noqa: B017 - typed TurnStoreError
            turns[0].compiled_plan()
        assert getattr(exc.value, "reason", "") == "no_plan"
        in_memory = env.service.sessions.get_session(sid).turns["voice-fail"]
        assert in_memory.turn_state is TurnState.FAILED

    def test_a_refused_preview_is_recorded_durably(self, env) -> None:
        _grant_audio(env)
        sid = env.open_session()
        result = env.voice(sid, "voice-refused")
        assert type(result).__name__ == "Refusal", result
        turns = env.sessions.list_turns(sid)
        assert len(turns) == 1
        assert turns[0].turn_status is TurnStatus.REJECTED
        assert "voice_preview_refused" in (turns[0].refusal_reason or "")


class TestNoRawMediaIsPersisted:
    def test_the_durable_rows_never_contain_audio_or_transcript_bytes(self, env) -> None:
        _grant_audio(env)
        _grant_video(env)
        sid = env.open_session()
        env.voice(sid, "voice-1")
        rows = env.sessions.path.read_bytes()
        assert b"audio-bytes" not in rows
        assert TRANSCRIPT.encode() not in rows
        assert b"transcript" not in rows.lower()
