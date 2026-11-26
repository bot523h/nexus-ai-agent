"""Wave 3-R1a — the approval token's expiry is minted once and never drifts.

The audit found that ``_canonical_binding_payload`` recomputed ``expires_at_utc``
from ``now() + TTL`` on *every* call.  A frozen unit clock hid it completely: an
issuance at ``T0`` and a consumption at ``T0`` produced identical payloads.  In
production a human decision taken at ``T0`` and executed 30 seconds later
produced a *different* payload, so every honest approval was refused as
``approval_binding_mismatch``.

These tests drive a **moving** clock through the real durable stores:

* issue at ``T0``, execute at ``T0 + 30s`` → the approval verifies and is
  consumed exactly once (the drift regression);
* issue at ``T0``, execute past ``T0 + APPROVAL_TTL_MINUTES + 1s`` → typed
  ``approval_expired`` refusal, and the token is *not* burned;
* a refusal for an unrelated reason (a different principal) never consumes an
  otherwise valid approval, so the rightful principal can still execute;
* the persisted canonical payload is byte-identical to the one minted at
  issuance (no recomputation anywhere on the consume path).
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import nexus_ai_agent.nagar.ibpc.service as service_mod
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalBindingPayload,
    DurableCanonicalApprovalLedger,
)
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.authority import PrincipalFacts
from nexus_ai_agent.nagar.ibpc.live_session import DurableLiveSessionStore
from nexus_ai_agent.nagar.ibpc.models import CheckVerdict
from nexus_ai_agent.nagar.ibpc.principal_authority import MembershipRole
from nexus_ai_agent.nagar.ibpc.principal_authority import PrincipalFacts as RoleFacts
from nexus_ai_agent.nagar.ibpc.service import (
    APPROVAL_TTL_MINUTES,
    IBPCService,
    IBPCServiceDeps,
)

T0 = "2026-11-25T00:00:00Z"
KEY = b"r" * 32
PRINCIPAL = "prn_" + "2" * 32
OTHER_PRINCIPAL = "prn_" + "3" * 32
PROJECT = "project-r1a"
BINDING_REF = "cbn_" + "b" * 64


class _Clock:
    """A monotonic test clock the test advances explicitly."""

    def __init__(self, start: str = T0) -> None:
        self._instant = _parse(start)

    def __call__(self) -> str:
        return self._instant.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, **delta: float) -> None:
        self._instant = self._instant + timedelta(**delta)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class _Queue:
    """Queue port double: reports a completed render with a real artifact file."""

    def __init__(self, artifact: Path) -> None:
        self.artifact = artifact
        self.enqueued: list[tuple[str, str, dict]] = []

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
        self.enqueued.append((job_type, idempotency_key, payload))
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str) -> JobStatus:
        return JobStatus.COMPLETED

    async def get_result(self, job_id: str) -> dict:
        return {"success": True, "artifact_path": str(self.artifact)}


class _Authority:
    """Canonical authority double: verifies identity, never grants a role."""

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        return PrincipalFacts(
            principal_id=principal_id,
            project_id=project_id,
            channel_binding_id=binding_ref,
        )


class _OwnerRoleAuthority:
    """Role-authority double: verifies the requested principal as OWNER.

    Wave 3-R7: approving and consuming require a *verified Studio role*, so a
    full-pipeline test must compose a role source.  The canonical port still
    answers independently — the 3-way consistency check cross-examines both.
    """

    def __init__(self, role: MembershipRole = MembershipRole.OWNER) -> None:
        self.role = role

    def authorize_action(
        self, *, principal_id: str, project_id: str, binding_ref: str, action: object
    ) -> RoleFacts:
        return RoleFacts(
            principal_id=principal_id,
            project_id=project_id,
            channel_binding_id=binding_ref,
            role=self.role,
        )


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = _Clock()
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
            "computed_at": clock(),
            "signature_digest": "sha256:" + "1" * 64,
        }
        return types.SimpleNamespace(
            signature_digest=payload["signature_digest"],
            scope_digest=scope.scope_digest,
            model_dump=lambda **_kw: payload,
        )

    monkeypatch.setattr(service_mod, "build_shadow_preview", fake_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)

    # Re-deriving the keyframe evidence needs real JPEG bytes from ffmpeg; this
    # module is about token lifetime, so the manifest is shape-checked only. The
    # service's own plan/scope agreement check still runs right after it.
    def fake_verify_shadow(manifest):
        assert manifest.plan_digest and manifest.scope_digest

    monkeypatch.setattr(service_mod, "verify_shadow_manifest", fake_verify_shadow)

    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    artifact = tmp_path / "artifact.mp4"
    artifact.write_bytes(b"artifact-bytes")

    ledger = DurableCanonicalApprovalLedger(
        tmp_path / "approvals.sqlite3",
        key_provider=StaticKeyRing({"unit": KEY}, active_key_id="unit"),
        now=clock,
    )
    sessions = DurableLiveSessionStore(tmp_path / "sessions.sqlite3", now=clock)
    queue = _Queue(artifact)
    service = IBPCService(
        IBPCServiceDeps(
            queue=queue,  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: 7,
            now=clock,
            approval_key_provider=StaticKeyRing({"unit": KEY}, active_key_id="unit"),
            approval_ledger=ledger,
            session_store=sessions,
            channel_binding_ref=BINDING_REF,
            authority=_Authority(),
            principal_authority=_OwnerRoleAuthority(),
        )
    )

    def fake_report(ctx, artifact_path, input_path):
        return "sha256:" + "9" * 64, types.SimpleNamespace(
            verdict=CheckVerdict.PASS, report_digest="sha256:" + "8" * 64
        )

    monkeypatch.setattr(service, "_verify_artifact", fake_report)

    sid = service.open_session(
        tenant_id="ten",
        project_id=PROJECT,
        principal_id=PRINCIPAL,
        user_id=1,
        chat_id=1,
        current_revision=7,
    )

    def submit(turn_id: str = "turn-1", sequence: int = 1):
        return service.submit_text_intent(
            sid,
            turn_id,
            sequence_number=sequence,
            text="trim from 0.2 to 0.6 seconds",
            asset_id="src",
            source_path=str(clip),
        )

    yield types.SimpleNamespace(
        service=service,
        clock=clock,
        ledger=ledger,
        sessions=sessions,
        queue=queue,
        clip=clip,
        sid=sid,
        submit=submit,
    )
    ledger.close()
    sessions.close()


def _execute(env, approval_id: str, *, principal_id: str = PRINCIPAL, turn: str = "turn-1"):
    return asyncio.run(env.service.execute(env.sid, turn, approval_id, principal_id=principal_id))


class TestIssuanceMintsTheExpiryExactlyOnce:
    def test_the_signed_payload_carries_the_issuance_instant(self, env) -> None:
        proposal = env.submit()
        assert type(proposal).__name__ == "PipelineResult", proposal
        env.service.approve(env.sid, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        stored = env.ledger.binding_payload(proposal.approval_id)
        assert stored.expires_at_utc == "2026-11-25T00:10:00Z"
        ctx = env.service._contexts[proposal.approval_id]
        assert ctx.issued_expires_at_utc == stored.expires_at_utc
        assert ctx.canonical_payload is not None
        assert ctx.canonical_payload.as_dict() == stored.as_dict()

    def test_the_payload_does_not_move_as_the_clock_advances(self, env) -> None:
        proposal = env.submit()
        env.service.approve(env.sid, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        before = env.ledger.binding_payload(proposal.approval_id)
        env.clock.advance(seconds=30)
        ctx = env.service._contexts[proposal.approval_id]
        # wave 3-R1a: the consume-time payload is the issued one, byte for byte.
        assert env.service._issued_binding_payload(ctx).as_dict() == before.as_dict()
        assert env.ledger.binding_payload(proposal.approval_id).as_dict() == before.as_dict()

    def test_the_ttl_is_the_shipped_constant(self) -> None:
        assert APPROVAL_TTL_MINUTES == 10


class TestExecutionAfterTheClockAdvances:
    def test_execution_thirty_seconds_after_issuance_succeeds(self, env) -> None:
        proposal = env.submit()
        assert type(proposal).__name__ == "PipelineResult", proposal
        env.service.approve(env.sid, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        env.clock.advance(seconds=30)
        outcome = _execute(env, proposal.approval_id)
        assert outcome.status != "refused", outcome.detail
        assert outcome.job_id == "job_1"
        assert len(env.queue.enqueued) == 1
        assert env.ledger.get(proposal.approval_id).state.value == "CONSUMED"

    def test_execution_at_the_exact_expiry_instant_is_refused(self, env) -> None:
        proposal = env.submit()
        env.service.approve(env.sid, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        env.clock.advance(minutes=APPROVAL_TTL_MINUTES)
        outcome = _execute(env, proposal.approval_id)
        assert outcome.status == "refused"
        assert "approval_expired" in outcome.detail
        assert env.queue.enqueued == []
        # an expired token is never burned: the row stays APPROVED
        assert env.ledger.get(proposal.approval_id).state.value == "APPROVED"

    def test_execution_past_the_ttl_is_a_typed_refusal(self, env) -> None:
        proposal = env.submit()
        env.service.approve(env.sid, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        env.clock.advance(minutes=APPROVAL_TTL_MINUTES, seconds=1)
        outcome = _execute(env, proposal.approval_id)
        assert outcome.status == "refused"
        assert "approval_expired" in outcome.detail
        assert env.queue.enqueued == []

    def test_a_refused_request_never_burns_the_approval(self, env) -> None:
        proposal = env.submit()
        env.service.approve(env.sid, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        env.clock.advance(seconds=30)
        wrong = _execute(env, proposal.approval_id, principal_id=OTHER_PRINCIPAL)
        assert wrong.status == "refused"
        assert "wrong_principal" in wrong.detail
        assert env.ledger.get(proposal.approval_id).state.value == "APPROVED"
        assert env.queue.enqueued == []
        # the rightful principal can still execute the untouched approval
        honest = _execute(env, proposal.approval_id)
        assert honest.status != "refused", honest.detail
        assert len(env.queue.enqueued) == 1

    def test_a_second_execution_of_the_same_approval_is_a_replay_refusal(self, env) -> None:
        proposal = env.submit()
        env.service.approve(env.sid, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        env.clock.advance(seconds=30)
        first = _execute(env, proposal.approval_id)
        assert first.status != "refused", first.detail
        replay = _execute(env, proposal.approval_id)
        assert replay.status == "refused"
        assert len(env.queue.enqueued) == 1


class TestTheIssuedPayloadIsTheOnlyPayload:
    def test_a_context_without_an_issued_payload_fails_closed(self, env) -> None:
        proposal = env.submit()
        assert type(proposal).__name__ == "PipelineResult", proposal
        ctx = env.service._contexts[proposal.approval_id]
        ctx.canonical_payload = None
        with pytest.raises(Exception) as exc:  # noqa: B017 - typed ApprovalError
            env.service._issued_binding_payload(ctx)
        assert getattr(exc.value, "reason", "") == "approval_payload_missing"

    def test_the_issuance_signature_requires_an_explicit_instant(self, env) -> None:
        proposal = env.submit()
        assert type(proposal).__name__ == "PipelineResult", proposal
        ctx = env.service._contexts[proposal.approval_id]
        identity = types.SimpleNamespace(
            project_id=PROJECT,
            session_id=env.sid,
            turn_id="turn-1",
            plan_digest=ctx.plan.plan_digest,
            preview_digest=ctx.preview.preview_digest,
            source_revision=7,
            principal_id=PRINCIPAL,
        )
        with pytest.raises(TypeError):
            env.service._canonical_binding_payload(ctx, identity)  # type: ignore[call-arg]
        payload = env.service._canonical_binding_payload(
            ctx,
            identity,
            expires_at_utc="2030-01-01T00:00:00Z",  # type: ignore[arg-type]
        )
        assert isinstance(payload, ApprovalBindingPayload)
        assert payload.expires_at_utc == "2030-01-01T00:00:00Z"

    def test_a_naive_clock_is_a_typed_refusal(self, env, monkeypatch) -> None:
        proposal = env.submit()
        assert type(proposal).__name__ == "PipelineResult", proposal
        naive_service = env.service
        monkeypatch.setattr(naive_service._deps, "now", lambda: "2026-11-25T00:00:00")
        with pytest.raises(Exception) as exc:  # noqa: B017 - typed ApprovalError
            naive_service.approve(env.sid, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        assert getattr(exc.value, "reason", "") == "invalid_instant"
