"""Wave 3-R3/R4 — durable recovery, termination and the narrowed turn mirror.

The in-memory session manager and the context map are *caches*. Everything an
approval needs in order to be executed — canonical plan/preview, media evidence,
scope signature, binding, principal/project, the issuance instant — is persisted
with the turn, so a second process can validate and execute it (or refuse it)
without the first process. These tests pin that contract, the close/abort
fencing, the crash reconciler, and the fact that ``_mark_durable_turn`` no longer
swallows every ``ValueError``.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

import nexus_ai_agent.nagar.ibpc.service as service_mod
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval import DurableCanonicalApprovalLedger
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.authority import PrincipalFacts
from nexus_ai_agent.nagar.ibpc.live_session import (
    DurableLiveSessionStore,
    SessionStatus,
    TurnStatus,
)
from nexus_ai_agent.nagar.ibpc.models import CheckVerdict, ShadowPreviewManifest
from nexus_ai_agent.nagar.ibpc.service import (
    ApprovalError,
    IBPCService,
    IBPCServiceDeps,
    RecoveryError,
)
from nexus_ai_agent.nagar.ibpc.session import TurnState
from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
KEY = b"r" * 32
PRINCIPAL = "prn_" + "7" * 32
PROJECT = "project-r3"
BINDING_REF = "cbn_" + "a" * 64
SHADOW_DIGEST = "sha256:" + "2" * 64


class _Queue:
    """Records enqueues; ``status`` is terminal so a single execute publishes once."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, dict]] = []
        self.status = JobStatus.COMPLETED
        self.artifact_path = ""

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
        self.enqueued.append((job_type, idempotency_key, payload))
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str) -> JobStatus:
        return self.status

    async def get_result(self, job_id: str) -> dict:
        return {"success": True, "output": "out.mp4", "artifact_path": self.artifact_path}


class _Authority:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        self.calls.append((principal_id, project_id, binding_ref))
        return PrincipalFacts(
            principal_id=principal_id,
            project_id=project_id,
            channel_binding_id=binding_ref,
        )


def _shadow_manifest(plan_digest: str, scope) -> ShadowPreviewManifest:
    """Deterministic evidence: a fixed id and no keyframes.

    Determinism matters here — the canonical scope signature binds
    ``shadow_manifest.model_dump()``, so recovery can only reproduce the
    approval's signature if the persisted manifest dumps byte-for-byte.
    """
    return ShadowPreviewManifest(
        shadow_id="shad_r3recovery",
        plan_digest=plan_digest,
        scope_digest=scope.scope_digest,
        source_duration_us=3_000_000,
        scope_start_us=scope.start_us,
        scope_end_us=scope.end_us,
        keyframes=(),
        composite_digest="sha256:" + "0" * 64,
    )


def _media_signature(scope) -> ScopeSignature:
    return ScopeSignature(
        scope_digest=scope.scope_digest,
        asset_id=scope.asset_id,
        start_us=scope.start_us,
        end_us=scope.end_us,
        color_histogram=(0.0,) * 64,
        motion_magnitude=0.0,
        sample_frames=1,
        computed_at=NOW(),
        signature_digest="sha256:" + "1" * 64,
    )


@pytest.fixture()
def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Media probes stubbed; the evidence models are REAL so they round-trip
    through the durable JSON byte-for-byte (that is what recovery replays)."""

    def fake_probe(path, *, binary=None):
        return types.SimpleNamespace(duration_us=3_000_000), 30.0

    def fake_shadow(path, scope, plan_digest, **kwargs):
        return _shadow_manifest(plan_digest, scope)

    def fake_signature(path, scope, **kwargs):
        return _media_signature(scope)

    monkeypatch.setattr(service_mod, "probe_media", fake_probe)
    monkeypatch.setattr(service_mod, "build_shadow_preview", fake_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)
    monkeypatch.setattr(service_mod, "verify_shadow_manifest", lambda manifest: None)
    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    return types.SimpleNamespace(clip=clip, root=tmp_path)


def _service(root: Path, *, clock=NOW, **overrides) -> IBPCService:
    """A service over the *fixed* store paths under ``root`` (restartable)."""
    ring = StaticKeyRing({"unit": KEY}, active_key_id="unit")
    deps = dict(
        queue=_Queue(),
        registry=build_runtime_registry(),
        workspace_root=root,
        revision_provider=lambda: 7,
        now=clock,
        channel_binding_ref=BINDING_REF,
        authority=_Authority(),
        approval_key_provider=ring,
        approval_ledger=DurableCanonicalApprovalLedger(
            root / "approvals.sqlite3", key_provider=ring, now=clock
        ),
        session_store=DurableLiveSessionStore(root / "sessions.sqlite3", now=clock),
    )
    deps.update(overrides)
    return IBPCService(IBPCServiceDeps(**deps))  # type: ignore[arg-type]


def _shutdown(service: IBPCService) -> None:
    """Simulate process death: the stores are flushed and the caches are gone."""
    service._deps.approval_ledger.close()
    store = service._deps.session_store
    if store is not None:
        store.close()


def _open(service: IBPCService) -> str:
    return service.open_session(
        tenant_id="ten",
        project_id=PROJECT,
        principal_id=PRINCIPAL,
        user_id=1,
        chat_id=1,
        current_revision=7,
    )


def _preview(service: IBPCService, clip: Path, session_id: str):
    return service.submit_text_intent(
        session_id,
        "turn-1",
        sequence_number=1,
        text="trim from 0.0 to 2.0 seconds",
        asset_id="asset-r3",
        source_path=str(clip),
    )


class TestTheDurableContextRoundTrips:
    def test_a_serialized_context_rebuilds_into_an_executable_one(self, media) -> None:
        service = _service(media.root)
        try:
            session_id = _open(service)
            proposal = _preview(service, media.clip, session_id)
            service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            ctx = service._contexts[proposal.approval_id]
            rebuilt = service._deserialize_context(service._serialize_context(ctx))
            assert rebuilt.plan.plan_digest == ctx.plan.plan_digest
            assert rebuilt.preview.preview_digest == ctx.preview.preview_digest
            assert rebuilt.scope_signature == ctx.scope_signature
            assert rebuilt.issued_expires_at_utc == ctx.issued_expires_at_utc
            assert rebuilt.canonical_payload == ctx.canonical_payload
            assert rebuilt.approval_envelope == ctx.approval_envelope
            # the re-validated evidence is byte-identical, so the approval scope
            # signature the new process recomputes still matches
            identity = service._resolve_identity(
                session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL
            )[3]
            assert service._scope_signature(rebuilt, identity) == ctx.scope_signature
        finally:
            _shutdown(service)

    def test_the_durable_context_carries_no_media_or_transcript(self, media) -> None:
        service = _service(media.root)
        try:
            session_id = _open(service)
            proposal = _preview(service, media.clip, session_id)
            service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            raw = service._session_store.get_turn(session_id, "turn-1").execution_context_json
            assert raw is not None
            payload = json.loads(raw)
            assert set(payload) >= {
                "ast",
                "plan",
                "preview",
                "media_signature",
                "shadow_manifest",
                "scope_signature",
                "source_asset_digest",
                "binding_ref",
                "principal_id",
                "project_id",
                "issued_expires_at_utc",
                "canonical_payload",
                "approval_envelope",
            }
            # no raw media, no audio, no transcript text anywhere in the row
            blob = raw.encode()
            assert b"trim from 0.0" not in blob
            assert media.clip.read_bytes()[:8] not in blob
            assert payload["source_asset_digest"].startswith("sha256:")
        finally:
            _shutdown(service)

    def test_a_tampered_context_is_a_typed_recovery_error(self, media) -> None:
        service = _service(media.root)
        try:
            session_id = _open(service)
            proposal = _preview(service, media.clip, session_id)
            raw = json.loads(service._serialize_context(service._contexts[proposal.approval_id]))
            raw["plan"] = {"not": "a plan"}
            with pytest.raises(RecoveryError) as exc:
                service._deserialize_context(json.dumps(raw))
            assert exc.value.reason == "context_unrecoverable"
        finally:
            _shutdown(service)

    def test_a_wrong_clock_spelling_is_not_a_crash(self) -> None:
        assert service_mod._revision_from_id("rev:12") == 12
        assert service_mod._revision_from_id("rev:oops") == 0
        assert service_mod._revision_from_id("7") == 0


def _stub_artifact(service: IBPCService, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the publication lane finish without shelling out to ffmpeg.

    The artifact *verification* is exercised by its own suite; what R3 pins is
    that exactly one job is published and the durable turn records EXECUTED.
    """
    artifact = root / "out.mp4"
    artifact.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 512)
    service._deps.queue.artifact_path = str(artifact)
    report = types.SimpleNamespace(
        verdict=CheckVerdict.INCONCLUSIVE, report_digest="sha256:" + "4" * 64
    )
    monkeypatch.setattr(
        service,
        "_verify_artifact",
        lambda ctx, path, source: ("sha256:" + "3" * 64, report),
        raising=False,
    )


def _executed(outcome) -> bool:
    return outcome.status in ("executed", "executed_unverified")


class TestASecondProcessRecoversTheLifecycle:
    def test_sessions_turns_and_the_active_revision_are_restored(self, media) -> None:
        first = _service(media.root)
        session_id = _open(first)
        proposal = _preview(first, media.clip, session_id)
        first.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        _shutdown(first)

        second = _service(media.root)
        try:
            assert second._sessions._sessions == {}  # a cold cache
            report = second.recover_from_durable_state()
            assert report["sessions"] == 1
            assert report["turns"] >= 1
            assert report["contexts"] >= 1
            recovered = second._sessions.get_session(session_id)
            assert recovered.project_id == PROJECT
            assert recovered.principal_id == PRINCIPAL
            assert recovered.current_revision == 7
            assert recovered.max_sequence == 1
            turn = second._sessions.get_turn(session_id, "turn-1")
            assert turn.turn_state is TurnState.APPROVED
            assert turn.approval_id == proposal.approval_id
            # the approval is executable here, with the context rebuilt from SQLite
            assert proposal.approval_id in second._contexts
        finally:
            _shutdown(second)

    def test_the_recovered_approval_executes_exactly_once(self, media, monkeypatch) -> None:
        first = _service(media.root)
        session_id = _open(first)
        proposal = _preview(first, media.clip, session_id)
        first.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        _shutdown(first)

        second = _service(media.root)
        try:
            _stub_artifact(second, media.root, monkeypatch)
            outcome = asyncio.run(
                second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            )
            assert _executed(outcome), outcome.detail
            enqueued = second._deps.queue.enqueued
            assert len(enqueued) == 1
            # the durable turn is EXECUTED and the job carries the canonical key
            assert (
                second._session_store.get_turn(session_id, "turn-1").turn_status
                is TurnStatus.EXECUTED
            )
            replay = asyncio.run(
                second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            )
            assert replay.status == "refused"
            assert "already_consumed" in replay.detail
            assert len(second._deps.queue.enqueued) == 1
        finally:
            _shutdown(second)

    def test_a_third_process_never_republishes(self, media, monkeypatch) -> None:
        first = _service(media.root)
        session_id = _open(first)
        proposal = _preview(first, media.clip, session_id)
        first.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        _shutdown(first)
        second = _service(media.root)
        _stub_artifact(second, media.root, monkeypatch)
        assert _executed(
            asyncio.run(
                second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            )
        )
        _shutdown(second)

        third = _service(media.root)
        try:
            outcome = asyncio.run(
                third.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            )
            assert outcome.status == "refused"
            assert third._deps.queue.enqueued == []
            assert third._session_store.get_session(session_id).last_turn_index == 1
        finally:
            _shutdown(third)

    def test_an_approval_that_expired_while_we_were_down_is_refused(self, media) -> None:
        first = _service(media.root)
        session_id = _open(first)
        proposal = _preview(first, media.clip, session_id)
        first.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        issued_expires = first._contexts[proposal.approval_id].issued_expires_at_utc
        _shutdown(first)

        late = _service(media.root, clock=lambda: "2027-06-01T00:00:00Z")
        try:
            outcome = asyncio.run(
                late.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            )
            assert outcome.status == "refused"
            assert "approval_expired" in outcome.detail
            assert late._deps.queue.enqueued == []
            # the stored instant is the issuance instant, not a recomputation
            assert (
                late._session_store.get_turn(session_id, "turn-1").issued_expires_at_utc
                == issued_expires
            )
        finally:
            _shutdown(late)

    def test_recovery_is_idempotent_and_does_not_clobber_a_live_context(self, media) -> None:
        service = _service(media.root)
        try:
            session_id = _open(service)
            proposal = _preview(service, media.clip, session_id)
            service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            live = service._contexts[proposal.approval_id]
            report = service.recover_from_durable_state(force=True)
            assert report["contexts_kept"] >= 1
            assert service._contexts[proposal.approval_id] is live
            assert service.recover_from_durable_state() == report
            assert service._sessions.get_session(session_id).max_sequence == 1
        finally:
            _shutdown(service)

    def test_recovery_without_a_store_is_a_no_op(self) -> None:
        service = _service(Path("/tmp"), session_store=None)  # type: ignore[arg-type]
        assert service.recover_from_durable_state()["reason"] == "no_store"
        assert service.recover_from_durable_state()["reason"] == "no_store"

    def test_an_unusable_context_is_reported_and_never_half_restored(self, media) -> None:
        first = _service(media.root)
        session_id = _open(first)
        proposal = _preview(first, media.clip, session_id)
        first.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
        store = first._session_store
        assert store is not None
        # corrupt the durable context as a torn write would
        with store._transaction():
            store._conn.execute(
                "UPDATE ibpc_turns SET execution_context_json = ?"
                " WHERE session_id = ? AND turn_id = ?",
                ('{"ast": null}', session_id, "turn-1"),
            )
        _shutdown(first)

        second = _service(media.root)
        try:
            report = second.recover_from_durable_state()
            assert report["unrecoverable"] == 1
            assert proposal.approval_id not in second._contexts
            outcome = asyncio.run(
                second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            )
            assert outcome.status == "refused"
            assert "unknown_context" in outcome.detail
        finally:
            _shutdown(second)


class TestClosingAndAbortingFenceEverything:
    def test_close_revokes_the_unconsumed_approval(self, media) -> None:
        service = _service(media.root)
        try:
            session_id = _open(service)
            proposal = _preview(service, media.clip, session_id)
            record = service.close_session(session_id)
            assert record.status is SessionStatus.CLOSED
            assert service._deps.approval_ledger.get(proposal.approval_id).state.value == "REVOKED"
            outcome = asyncio.run(
                service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            )
            assert outcome.status == "refused"
            assert service._deps.queue.enqueued == []
        finally:
            _shutdown(service)

    def test_abort_revokes_and_interrupts_the_turn(self, media) -> None:
        service = _service(media.root)
        try:
            session_id = _open(service)
            proposal = _preview(service, media.clip, session_id)
            service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            record = service.abort_session(session_id)
            assert record.status is SessionStatus.ABORTED
            assert service._deps.approval_ledger.get(proposal.approval_id).state.value == "REVOKED"
            assert service._sessions.get_session(session_id).turns["turn-1"].turn_state in {
                TurnState.INTERRUPTED,
                TurnState.CANCELLED,
            }
            outcome = asyncio.run(
                service.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
            )
            assert outcome.status == "refused"
        finally:
            _shutdown(service)

    def test_termination_is_idempotent(self, media) -> None:
        service = _service(media.root)
        try:
            session_id = _open(service)
            _preview(service, media.clip, session_id)
            first = service.close_session(session_id)
            again = service.close_session(session_id)
            assert first.status is SessionStatus.CLOSED
            assert again.status is SessionStatus.CLOSED
        finally:
            _shutdown(service)

    def test_a_crash_between_the_stores_is_reconciled_by_recovery(self, media) -> None:
        first = _service(media.root)
        session_id = _open(first)
        proposal = _preview(first, media.clip, session_id)
        store = first._session_store
        assert store is not None
        # the session is terminated durably but the approval revoke never ran
        store.close_session(session_id)
        assert first._deps.approval_ledger.get(proposal.approval_id).state.value == "PENDING"
        _shutdown(first)

        second = _service(media.root)
        try:
            report = second.recover_from_durable_state()
            assert report["terminated"] >= 1
            assert report["revoked"] >= 1
            assert second._deps.approval_ledger.get(proposal.approval_id).state.value == "REVOKED"
        finally:
            _shutdown(second)


class TestTheTurnMirrorIsNoLongerSilent:
    def test_an_unknown_turn_is_skipped_with_its_typed_reason(
        self, media, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = _service(media.root)
        try:
            session_id = _open(service)
            _preview(service, media.clip, session_id)
            with caplog.at_level("WARNING", logger="nexus_ai_agent.nagar.ibpc.service"):
                service._mark_durable_turn(session_id, "turn-unknown", TurnStatus.EXECUTED)
            assert "ibpc.durable_turn_mirror_skipped" in caplog.text
        finally:
            _shutdown(service)

    def test_a_lifecycle_divergence_propagates(self, media) -> None:
        service = _service(media.root)
        try:
            session_id = _open(service)
            _preview(service, media.clip, session_id)
            # PREVIEW_READY -> EXECUTED is not a legal durable transition: the
            # mirror must surface that, never swallow it as a ValueError
            with pytest.raises(ApprovalError) as exc:
                service._mark_durable_turn(session_id, "turn-1", TurnStatus.EXECUTED)
            assert exc.value.reason == "durable_turn_not_mirrored:invalid_transition"
        finally:
            _shutdown(service)
