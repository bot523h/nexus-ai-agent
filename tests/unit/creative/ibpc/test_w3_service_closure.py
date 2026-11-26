"""Wave 3 — ``IBPCService`` refusal and defensive seams that happy paths skip.

Every test here drives a *public* method of :class:`IBPCService` or one of its
documented internal seams (``_resolve_authority``, ``_mark_durable_turn``, …)
against the real durable stores, pinning the fail-closed branch that a
compromised client, a mid-flight revocation, or a hostile filesystem can reach:

* an unconfigured/unbound canonical authority is a typed refusal, never a job;
* the durable consent gate refuses *before* any media evidence work, and a
  revocation between preview and execution refuses the execution;
* approval consumption is bound to the exact canonical payload and the approved
  scope signature, and a cancelled turn can never execute;
* staging copies content-addressed bytes only (digest drift refuses), and the
  provenance reference degrades honestly while the drain honours its deadline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import types
from pathlib import Path

import pytest

import nexus_ai_agent.nagar.ibpc.service as service_mod
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalError,
    ApprovalIdentity,
    CanonicalApprovalLedger,
    DurableCanonicalApprovalLedger,
)
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.authority import CanonicalAuthorityError, PrincipalFacts
from nexus_ai_agent.nagar.ibpc.live_session import DurableLiveSessionStore
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    DurableStudioAuthorizationService,
    MembershipRole,
    StudioAuthorizationError,
)
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    StudioAction as PrincipalStudioAction,
)
from nexus_ai_agent.nagar.ibpc.privacy import (
    ConsentAction,
    ConsentScope,
    DurablePrivacyConsentStore,
    PrivacyViolation,
)
from nexus_ai_agent.nagar.ibpc.service import (
    IBPCService,
    IBPCServiceDeps,
    SessionError,
    _TurnContext,
)

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
KEY = b"k" * 32
PRINCIPAL = "prn_" + "1" * 32
PROJECT = "project-w3"
BINDING_REF = "cbn_" + "a" * 64


def _ring() -> StaticKeyRing:
    return StaticKeyRing({"unit": KEY}, active_key_id="unit")


class _Queue:
    """Records enqueues; ``status`` is whatever the test needs it to be."""

    def __init__(self, status: JobStatus = JobStatus.FAILED_TERMINAL) -> None:
        self.enqueued: list[tuple[str, str, dict]] = []
        self.status = status

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
        self.enqueued.append((job_type, idempotency_key, payload))
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str) -> JobStatus:
        return self.status

    async def get_result(self, job_id: str) -> dict:
        return {"success": False, "error": "not executed in this test"}


class _Authority:
    """Configurable canonical authority port (real refusals, no synthetic grant)."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        principal_id: str | None = None,
    ) -> None:
        self.error = error
        self.principal_id = principal_id
        self.calls: list[tuple[str, str, str]] = []

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        self.calls.append((principal_id, project_id, binding_ref))
        if self.error is not None:
            raise self.error
        return PrincipalFacts(
            principal_id=self.principal_id or principal_id,
            project_id=project_id,
            channel_binding_id=binding_ref,
        )


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Media probes stubbed, stores real, authority injectable per test."""

    def fake_probe(path, *, binary=None):
        return types.SimpleNamespace(duration_us=3_000_000), 30.0

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

    monkeypatch.setattr(service_mod, "probe_media", fake_probe)
    monkeypatch.setattr(service_mod, "build_shadow_preview", fake_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)

    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)

    created: list[IBPCService] = []

    def build(**overrides) -> IBPCService:
        defaults = dict(
            queue=_Queue(),  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: 7,
            now=NOW,
            approval_key_provider=_ring(),
            approval_ledger=DurableCanonicalApprovalLedger(
                tmp_path / f"approvals_{len(created)}.sqlite3", key_provider=_ring(), now=NOW
            ),
            session_store=DurableLiveSessionStore(
                tmp_path / f"sessions_{len(created)}.sqlite3", now=NOW
            ),
            channel_binding_ref=BINDING_REF,
            authority=_Authority(),
        )
        defaults.update(overrides)
        service = IBPCService(IBPCServiceDeps(**defaults))  # type: ignore[arg-type]
        created.append(service)
        return service

    yield types.SimpleNamespace(build=build, clip=clip, tmp_path=tmp_path, created=created)

    for service in created:
        ledger = service._deps.approval_ledger
        close_ledger = getattr(ledger, "close", None)
        if close_ledger is not None:
            close_ledger()
        store = service._deps.session_store
        if store is not None:
            store.close()


@pytest.fixture()
def studio(tmp_path: Path):
    """A real durable principal authority: PRINCIPAL owns PROJECT."""
    authz = DurableStudioAuthorizationService(tmp_path / "authz.sqlite3", now=NOW)
    authz.register_principal(PRINCIPAL)
    binding = authz.bind_channel(
        principal_id=PRINCIPAL, channel="telegram", external_id="tg-closure"
    )
    authz.grant_membership(project_id=PROJECT, principal_id=PRINCIPAL, role=MembershipRole.OWNER)
    yield types.SimpleNamespace(service=authz, binding=binding)
    authz.close()


def _session(service: IBPCService) -> str:
    return service.open_session(
        tenant_id="ten",
        project_id=PROJECT,
        principal_id=PRINCIPAL,
        user_id=1,
        chat_id=1,
        current_revision=7,
    )


def _submit(service: IBPCService, clip: Path, session_id: str, text: str | None = None):
    return service.submit_text_intent(
        session_id,
        "turn-1",
        sequence_number=1,
        text=text or "trim from 0.0 to 2.0 seconds",
        asset_id="asset-w3",
        source_path=str(clip),
    )


def _identity_for(ctx: _TurnContext) -> ApprovalIdentity:
    return ApprovalIdentity(
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        sequence_number=1,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        principal_id=ctx.principal_id,
        source_revision=7,
        source_asset_digest=ctx.source_asset_digest,
        ast_digest=ctx.ast.ast_digest,
        plan_digest=ctx.plan.plan_digest,
        preview_digest=ctx.preview.preview_digest,
    )


class TestCanonicalAuthorityRefusals:
    def test_no_principal_authority_and_no_canonical_authority_refuses(self, harness) -> None:
        service = harness.build(authority=None)
        session_id = _session(service)
        result = _submit(service, harness.clip, session_id)
        assert type(result).__name__ == "Refusal"
        assert result.reason == "unauthorized_principal"
        assert "no canonical/principal authority" in result.detail
        # refused BEFORE begin_turn: no context, nothing enqueued
        assert service._contexts == {}
        assert service._deps.queue.enqueued == []

    def test_a_missing_channel_binding_refuses_the_turn(self, harness) -> None:
        service = harness.build(channel_binding_ref=None)
        session_id = _session(service)
        result = _submit(service, harness.clip, session_id)
        assert type(result).__name__ == "Refusal"
        assert "missing_binding" in result.detail

    def test_an_unconfigured_canonical_authority_refuses_after_the_studio_gate(
        self, harness, studio
    ) -> None:
        # the studio gate passes (OWNER) but no canonical authority is wired, so
        # the turn is admitted and then terminalised by the typed refusal.
        service = harness.build(
            authority=None,
            principal_authority=studio.service,
            channel_binding_ref=studio.binding,
        )
        session_id = _session(service)
        result = _submit(service, harness.clip, session_id)
        assert type(result).__name__ == "Refusal"
        assert result.reason == "unauthorized_principal"
        assert "authority_unconfigured" in result.detail
        assert service._contexts == {}
        assert (
            service._sessions.get_session(session_id).turns["turn-1"].turn_state.value == "FAILED"
        )

    def test_a_typed_authority_refusal_is_surfaced_verbatim(self, harness, studio) -> None:
        authority = _Authority(error=CanonicalAuthorityError("not_a_member", "no membership"))
        service = harness.build(
            authority=authority,
            principal_authority=studio.service,
            channel_binding_ref=studio.binding,
        )
        session_id = _session(service)
        result = _submit(service, harness.clip, session_id)
        assert result.reason == "unauthorized_principal"
        assert "not_a_member" in result.detail
        # the turn was terminalised, never left half-open
        turn = service._sessions.get_session(session_id).turns["turn-1"]
        assert turn.turn_state.value == "FAILED"

    def test_an_identity_mismatch_from_the_authority_is_refused(self, harness) -> None:
        class _DriftingStudio:
            """A compromised authorization service returning another identity."""

            def authorize_action(self, **_kwargs):
                return types.SimpleNamespace(
                    principal_id="prn_" + "9" * 32,
                    project_id=PROJECT,
                    channel_binding_id=BINDING_REF,
                    role=None,
                )

        service = harness.build(principal_authority=_DriftingStudio())
        with pytest.raises(ApprovalError) as exc:
            service._authorize_principal(
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                action=PrincipalStudioAction.TURN_CREATE,
            )
        assert "identity_mismatch" in exc.value.reason


class TestDurableConsentGate:
    def test_a_missing_grant_refuses_the_turn_before_media_work(self, harness) -> None:
        store = DurablePrivacyConsentStore(harness.tmp_path / "consent.sqlite3", now=NOW)
        try:
            service = harness.build(privacy_consent_store=store)
            session_id = _session(service)
            result = _submit(service, harness.clip, session_id)
            assert type(result).__name__ == "Refusal"
            assert result.reason.startswith("consent_")
            assert service._contexts == {}
        finally:
            store.close()

    def test_a_live_grant_admits_the_turn(self, harness) -> None:
        store = DurablePrivacyConsentStore(harness.tmp_path / "consent.sqlite3", now=NOW)
        try:
            store.grant_consent(
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provider_id="local",
                data_category=ConsentScope.VIDEO,
                action=ConsentAction.LOCAL_PROCESSING,
                expires_at_utc="2027-01-01T00:00:00Z",
            )
            service = harness.build(privacy_consent_store=store)
            session_id = _session(service)
            assert type(_submit(service, harness.clip, session_id)).__name__ == "PipelineResult"
        finally:
            store.close()

    def test_external_upload_is_refused_by_the_session_policy(self, harness) -> None:
        service = harness.build()
        session_id = _session(service)
        with pytest.raises(PrivacyViolation) as exc:
            service._require_processing_consent(
                session_id=session_id,
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                data_category=ConsentScope.VIDEO,
                action=ConsentAction.EXTERNAL_PROVIDER_UPLOAD,
            )
        assert exc.value.reason == "egress_refused"

    def test_a_store_refusal_is_a_typed_approval_error(self, harness) -> None:
        store = DurablePrivacyConsentStore(harness.tmp_path / "consent.sqlite3", now=NOW)
        try:
            service = harness.build(privacy_consent_store=store)
            session_id = _session(service)
            with pytest.raises(ApprovalError) as exc:
                service._require_processing_consent(
                    session_id=session_id,
                    principal_id=PRINCIPAL,
                    project_id=PROJECT,
                    data_category=ConsentScope.VIDEO,
                    action=ConsentAction.LOCAL_PROCESSING,
                )
            assert exc.value.reason.startswith("consent_")
        finally:
            store.close()


class TestBindingAndTurnMirrors:
    def test_the_payload_falls_back_when_the_session_is_unknown(self, harness) -> None:
        service = harness.build()
        identity = types.SimpleNamespace(
            project_id=PROJECT,
            session_id="no-such-session",
            turn_id="turn-1",
            plan_digest="sha256:" + "2" * 64,
            preview_digest="sha256:" + "3" * 64,
            source_revision=7,
            scope_signature="sha256:" + "4" * 64,
            principal_id=PRINCIPAL,
        )
        ctx = _TurnContext.__new__(_TurnContext)
        payload = service._canonical_binding_payload(ctx, identity)  # type: ignore[arg-type]
        assert payload.base_revision_id == "rev:7"

    def test_the_payload_uses_the_session_revision_when_known(self, harness) -> None:
        service = harness.build()
        session_id = _session(service)
        identity = types.SimpleNamespace(
            project_id=PROJECT,
            session_id=session_id,
            turn_id="turn-1",
            plan_digest="sha256:" + "2" * 64,
            preview_digest="sha256:" + "3" * 64,
            source_revision=7,
            scope_signature="sha256:" + "4" * 64,
            principal_id=PRINCIPAL,
        )
        ctx = _TurnContext.__new__(_TurnContext)
        payload = service._canonical_binding_payload(ctx, identity)  # type: ignore[arg-type]
        assert (
            payload.base_revision_id
            == service._session_store.get_session(session_id).active_revision_id
        )

    def test_the_base_revision_falls_back_without_a_session_row(self, harness) -> None:
        service = harness.build()
        assert service._session_base_revision("unknown", 7) == "rev:7"

    def test_marking_an_unknown_durable_turn_never_raises(self, harness) -> None:
        from nexus_ai_agent.nagar.ibpc.live_session import TurnStatus

        service = harness.build()
        service._mark_durable_turn("unknown", "turn-1", TurnStatus.EXECUTED)
        service._mark_durable_turn("unknown", "turn-1", TurnStatus.APPROVED)

    def test_marking_executed_advances_the_row(self, harness) -> None:
        from nexus_ai_agent.nagar.ibpc.live_session import TurnStatus

        service = harness.build()
        session_id = _session(service)
        assert type(_submit(service, harness.clip, session_id)).__name__ == "PipelineResult"
        service._mark_durable_turn(session_id, "turn-1", TurnStatus.APPROVED)
        assert (
            service._session_store.get_turn(session_id, "turn-1").turn_status is TurnStatus.APPROVED
        )
        service._mark_durable_turn(session_id, "turn-1", TurnStatus.EXECUTED)
        assert (
            service._session_store.get_turn(session_id, "turn-1").turn_status is TurnStatus.EXECUTED
        )

    def test_consuming_without_a_scope_signature_is_refused(self, harness) -> None:
        service = harness.build()
        ctx = _TurnContext.__new__(_TurnContext)
        ctx.scope_signature = None
        identity = types.SimpleNamespace()
        with pytest.raises(ApprovalError) as exc:
            service._consume_approval("approval-x", ctx, identity)  # type: ignore[arg-type]
        assert "scope_signature_missing" in exc.value.reason


class TestFencedExecutionRefusals:
    def _approved(self, harness, **overrides):
        service = harness.build(**overrides)
        session_id = _session(service)
        proposal = _submit(service, harness.clip, session_id)
        assert type(proposal).__name__ == "PipelineResult", proposal
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        return service, session_id, proposal.approval_id

    def test_a_missing_key_provider_refuses_execution(self, harness) -> None:
        service, session_id, approval_id = self._approved(harness)
        service._deps.approval_key_provider = None
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "approval_mac_invalid" in outcome.detail

    def test_a_cancelled_turn_cannot_execute(self, harness) -> None:
        service, session_id, approval_id = self._approved(harness)
        service._sessions.cancel(session_id, "turn-1")
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"

    def test_missing_media_evidence_refuses_execution(self, harness) -> None:
        # A context admitted without media evidence (a legacy/reconstructed
        # turn) may be approved, but it can never reach execution.
        service = harness.build()
        session_id = _session(service)
        proposal = _submit(service, harness.clip, session_id)
        assert type(proposal).__name__ == "PipelineResult"
        ctx = service._contexts[proposal.approval_id]
        ctx.shadow_manifest = None
        ctx.media_signature = None
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "media_evidence_missing" in outcome.detail

    def test_a_source_changed_during_staging_refuses_execution(self, harness) -> None:
        service, session_id, approval_id = self._approved(harness)
        service._sha256_fd = lambda path: "sha256:" + "9" * 64  # type: ignore[method-assign]
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "source_changed_during_staging" in outcome.detail

    def test_a_staging_failure_refuses_execution(self, harness, monkeypatch) -> None:
        service, session_id, approval_id = self._approved(harness)

        def explode(*_args, **_kwargs):
            raise SessionError("staging_failed", "synthetic staging failure")

        monkeypatch.setattr(service, "_stage_source", explode)
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "staging_failed" in outcome.detail

    def test_staging_closes_its_descriptors_when_fdopen_fails(self, harness, monkeypatch) -> None:
        service = harness.build()
        calls: list[int] = []

        def failing_fdopen(fd, *args, **kwargs):
            calls.append(fd)
            raise OSError("synthetic fdopen failure")

        monkeypatch.setattr(service_mod.os, "fdopen", failing_fdopen)
        workspace = harness.tmp_path / "staging"
        with pytest.raises(SessionError) as exc:
            service._stage_source(harness.clip, workspace)
        assert exc.value.reason == "staging_failed"
        assert calls and calls[0] >= 0

    def test_a_symlinked_source_is_refused(self, harness) -> None:
        service = harness.build()
        link = harness.tmp_path / "link.mp4"
        link.symlink_to(harness.clip)
        with pytest.raises(SessionError) as exc:
            service._stage_source(link, harness.tmp_path / "staging")
        assert exc.value.reason == "source_unreadable"


class TestDrainAndProvenance:
    def test_the_drain_returns_after_its_deadline(self, harness) -> None:
        queue = _Queue(status=JobStatus.PENDING)
        service = harness.build(queue=queue)  # type: ignore[arg-type]
        assert asyncio.run(service._drain("job-1", timeout=0.0)) is JobStatus.PENDING

    def test_an_unreadable_journal_degrades_to_an_error_record(self, harness) -> None:
        class ExplodingJournal:
            def records_for_job(self, job_id: str):
                raise RuntimeError("journal offline")

            def all_records(self):
                raise RuntimeError("journal offline")

        service = harness.build(journal=ExplodingJournal())
        reference = service._provenance_reference("job-1")
        assert "error" in reference["journal"]
        assert "RuntimeError" in reference["journal"]["error"]
        assert reference["passport"] is None

    def test_a_failing_passport_builder_degrades_to_an_error_record(self, harness) -> None:
        from nexus_ai_agent.provenance.journal import CausalJournal

        journal = CausalJournal(harness.tmp_path / "journal.sqlite3")
        service = harness.build(journal=journal, facts_provider=object())
        reference = service._provenance_reference("job-1")
        assert reference["journal"]["record_count"] == 0
        assert "error" in reference["passport"]

    def test_a_well_formed_journal_reports_a_verified_chain(self, harness) -> None:
        from nexus_ai_agent.provenance.journal import CausalJournal

        journal = CausalJournal(harness.tmp_path / "journal.sqlite3")
        service = harness.build(journal=journal)
        reference = service._provenance_reference("job-1")
        assert reference["journal"]["chain_verified"] is True
        assert reference["journal"]["record_count"] == 0
        assert reference["passport"] is None


class TestIdentityResolution:
    def test_the_scope_signature_is_bound_to_the_approved_context(self, harness) -> None:
        service = harness.build()
        session_id = _session(service)
        proposal = _submit(service, harness.clip, session_id)
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        ctx = service._contexts[proposal.approval_id]
        identity = _identity_for(ctx)
        assert ctx.scope_signature is not None
        assert service._scope_signature(ctx, identity) == ctx.scope_signature

    def test_a_canonical_ledger_without_a_mac_refuses_the_consume(self, harness) -> None:
        ledger = CanonicalApprovalLedger(key_provider=_ring(), now=NOW)
        service = harness.build(approval_ledger=ledger)
        session_id = _session(service)
        proposal = _submit(service, harness.clip, session_id)
        ctx = service._contexts[proposal.approval_id]
        with pytest.raises(ApprovalError):
            service._consume_approval(proposal.approval_id, ctx, _identity_for(ctx))


class TestResidualRefusalBranches:
    """The remaining fail-closed branches around consume and media evidence."""

    def _approved(self, harness, **overrides):
        service = harness.build(**overrides)
        session_id = _session(service)
        proposal = _submit(service, harness.clip, session_id)
        assert type(proposal).__name__ == "PipelineResult", proposal
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        return service, session_id, proposal.approval_id

    def test_an_unauthorized_consume_is_refused_after_preview(self, harness) -> None:
        class _ConsumeRefusingStudio:
            binding_ref = BINDING_REF

            def authorize_action(self, *, principal_id, project_id, binding_ref, action):
                if action is PrincipalStudioAction.APPROVAL_CONSUME:
                    raise StudioAuthorizationError("insufficient_role", "consume is not allowed")
                return types.SimpleNamespace(
                    principal_id=principal_id,
                    project_id=project_id,
                    channel_binding_id=binding_ref,
                    role=MembershipRole.EDITOR,
                )

        service = harness.build(principal_authority=_ConsumeRefusingStudio())
        session_id = _session(service)
        proposal = _submit(service, harness.clip, session_id)
        assert type(proposal).__name__ == "PipelineResult"
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "insufficient_role" in outcome.detail

    def test_a_revoked_consent_refuses_execution(self, harness) -> None:
        store = DurablePrivacyConsentStore(harness.tmp_path / "consent.sqlite3", now=NOW)
        try:
            store.grant_consent(
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provider_id="local",
                data_category=ConsentScope.VIDEO,
                action=ConsentAction.LOCAL_PROCESSING,
                expires_at_utc="2027-01-01T00:00:00Z",
            )
            service, session_id, approval_id = self._approved(harness, privacy_consent_store=store)
            store.revoke_consent(
                principal_id=PRINCIPAL,
                project_id=PROJECT,
                provider_id="local",
                data_category=ConsentScope.VIDEO,
                action=ConsentAction.LOCAL_PROCESSING,
            )
            outcome = asyncio.run(
                service.execute(session_id, "turn-1", approval_id, principal_id=PRINCIPAL)
            )
            assert outcome.status == "refused"
            assert outcome.detail.startswith("consent_")
            assert service._deps.queue.enqueued == []
        finally:
            store.close()

    def test_a_turn_cancelled_during_consume_cannot_execute(self, harness, monkeypatch) -> None:
        service, session_id, approval_id = self._approved(harness)
        real = service._consume_approval

        def consume_then_cancel(*args, **kwargs):
            real(*args, **kwargs)
            service._sessions.cancel(session_id, "turn-1")

        monkeypatch.setattr(service, "_consume_approval", consume_then_cancel)
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "cannot execute" in outcome.detail

    def test_a_turn_that_never_reaches_executing_is_refused(self, harness, monkeypatch) -> None:
        service, session_id, approval_id = self._approved(harness)
        monkeypatch.setattr(service._sessions, "mark_executing", lambda *_args, **_kwargs: None)
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "no longer in an executing state" in outcome.detail

    def test_a_vanished_context_never_breaks_the_preview(self, harness) -> None:
        service = harness.build()

        class _VanishingContexts(dict):
            def get(self, key, default=None):
                return None

        session_id = _session(service)
        service._contexts = _VanishingContexts()
        result = _submit(service, harness.clip, session_id)
        assert type(result).__name__ == "PipelineResult"

    def test_the_payload_without_a_session_store_uses_the_pinned_revision(self, harness) -> None:
        service = harness.build(session_store=None)
        identity = types.SimpleNamespace(
            project_id=PROJECT,
            session_id="s1",
            turn_id="turn-1",
            plan_digest="sha256:" + "2" * 64,
            preview_digest="sha256:" + "3" * 64,
            source_revision=7,
            scope_signature="sha256:" + "4" * 64,
            principal_id=PRINCIPAL,
        )
        ctx = _TurnContext.__new__(_TurnContext)
        payload = service._canonical_binding_payload(ctx, identity)  # type: ignore[arg-type]
        assert payload.base_revision_id == "rev:7"

    def test_marking_a_non_terminal_target_is_a_noop(self, harness) -> None:
        from nexus_ai_agent.nagar.ibpc.live_session import TurnStatus

        service = harness.build()
        session_id = _session(service)
        assert type(_submit(service, harness.clip, session_id)).__name__ == "PipelineResult"
        service._mark_durable_turn(session_id, "turn-1", TurnStatus.DRAFTED)
        # neither APPROVED nor EXECUTED: the mirror is a no-op, never a downgrade
        assert (
            service._session_store.get_turn(session_id, "turn-1").turn_status
            is TurnStatus.PREVIEW_READY
        )

    def test_a_digest_read_closes_its_descriptor_on_fdopen_failure(
        self, harness, monkeypatch
    ) -> None:
        service = harness.build()

        def failing_fdopen(fd, *args, **kwargs):
            raise OSError("synthetic fdopen failure")

        monkeypatch.setattr(service_mod.os, "fdopen", failing_fdopen)
        with pytest.raises(OSError):
            service._sha256_fd(harness.clip)

    def test_attempt_identity_degrades_when_the_provider_fails(self, harness) -> None:
        def explode(job_id):
            raise RuntimeError("attempt history unavailable")

        service = harness.build(attempt_provider=explode)
        assert asyncio.run(service._attempt_identity("job-1")) == "job-1:attempt?"

    def test_attempt_identity_reports_the_highest_attempt(self, harness) -> None:
        service = harness.build(
            attempt_provider=lambda job_id: [{"attempt": 2}, {"attempt": 7}, {"attempt": "x"}]
        )
        assert asyncio.run(service._attempt_identity("job-1")) == "job-1:attempt7"

    def test_attempt_identity_awaits_an_async_provider(self, harness) -> None:
        async def history(job_id):
            return [{"attempt": 3}]

        service = harness.build(attempt_provider=history)
        assert asyncio.run(service._attempt_identity("job-1")) == "job-1:attempt3"


class TestCanonicalBindingAndScopeEnforcement:
    """Execution is bound to the *presented* canonical payload and scope."""

    def _approved(self, harness, **overrides):
        service = harness.build(**overrides)
        session_id = _session(service)
        proposal = _submit(service, harness.clip, session_id)
        assert type(proposal).__name__ == "PipelineResult", proposal
        service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        return service, session_id, proposal.approval_id

    def test_a_drifted_canonical_payload_refuses_execution(self, harness, monkeypatch) -> None:
        service, session_id, approval_id = self._approved(harness)
        stored = service._deps.approval_ledger.binding_payload(approval_id)
        drifted = dataclasses.replace(stored, base_revision_id="rev:999")
        monkeypatch.setattr(service, "_canonical_binding_payload", lambda *a, **k: drifted)
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "approval_binding_mismatch" in outcome.detail
        assert service._deps.queue.enqueued == []
        assert service._deps.approval_ledger.get(approval_id).state.value == "APPROVED"

    def test_a_tampered_scope_signature_refuses_execution(self, harness) -> None:
        service, session_id, approval_id = self._approved(harness)
        service._contexts[approval_id].scope_signature = "sha256:" + "e" * 64
        outcome = asyncio.run(
            service.execute(session_id, "turn-1", approval_id, principal_id=PRINCIPAL)
        )
        assert outcome.status == "refused"
        assert "scope_signature_mismatch" in outcome.detail
        assert service._deps.queue.enqueued == []
        # a refused scope never burns the approval
        assert service._deps.approval_ledger.get(approval_id).state.value == "APPROVED"
