"""Wave 3-R3/R4 — the full Service A → shutdown → Service B lifecycle.

Everything here runs against the *real* durable stores (SQLite files shared by
all three "processes"), which is the point: the in-memory session manager and the
context map are caches, so a second process must be able to reconstruct the
session, its turns, the revision head and the pending approval — and then either
execute it exactly once or refuse it for a concrete, typed reason.

Only the artifact re-measurement is stubbed (its own suite covers it), because a
render needs ffmpeg and an encoded clip.
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
from nexus_ai_agent.nagar.ibpc.authority import CanonicalAuthorityError, PrincipalFacts
from nexus_ai_agent.nagar.ibpc.live_session import (
    DurableLiveSessionStore,
    SessionStatus,
    TurnStatus,
)
from nexus_ai_agent.nagar.ibpc.models import CheckVerdict, ShadowPreviewManifest
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps
from nexus_ai_agent.nagar.ibpc.signature import ScopeSignature

NOW = lambda: "2026-11-25T00:00:00Z"  # noqa: E731
TTL_INSTANT = "2026-11-25T00:10:00Z"
KEY = b"i" * 32
PRINCIPAL = "prn_" + "8" * 32
PROJECT = "project-r3-integration"
BINDING_REF = "cbn_" + "b" * 64
REVISION = "rev:7"


class _Queue:
    """A terminal queue that records the single publication."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, dict]] = []
        self.artifact_path = ""

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
        self.enqueued.append((job_type, idempotency_key, payload))
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str) -> JobStatus:
        return JobStatus.COMPLETED

    async def get_result(self, job_id: str) -> dict:
        return {"success": True, "output": "out.mp4", "artifact_path": self.artifact_path}


class _Authority:
    def __init__(self, *, revoked: bool = False) -> None:
        self.revoked = revoked

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        if self.revoked:
            raise CanonicalAuthorityError("authorization_denied", "membership revoked")
        return PrincipalFacts(
            principal_id=principal_id,
            project_id=project_id,
            channel_binding_id=binding_ref,
        )


@pytest.fixture()
def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_probe(path, *, binary=None):
        return types.SimpleNamespace(duration_us=3_000_000), 30.0

    def fake_shadow(path, scope, plan_digest, **kwargs):
        return ShadowPreviewManifest(
            shadow_id="shad_r3_integration",
            plan_digest=plan_digest,
            scope_digest=scope.scope_digest,
            source_duration_us=3_000_000,
            scope_start_us=scope.start_us,
            scope_end_us=scope.end_us,
            keyframes=(),
            composite_digest="sha256:" + "0" * 64,
        )

    def fake_signature(path, scope, **kwargs):
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

    monkeypatch.setattr(service_mod, "probe_media", fake_probe)
    monkeypatch.setattr(service_mod, "build_shadow_preview", fake_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)
    monkeypatch.setattr(service_mod, "verify_shadow_manifest", lambda manifest: None)
    clip = tmp_path / "source.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048)
    return types.SimpleNamespace(clip=clip, root=tmp_path)


def _process(root: Path, *, revoked: bool = False, clock=NOW) -> IBPCService:
    """One "process": fresh in-memory state over the shared SQLite files."""
    ring = StaticKeyRing({"unit": KEY}, active_key_id="unit")
    return IBPCService(
        IBPCServiceDeps(  # type: ignore[arg-type]
            queue=_Queue(),
            registry=build_runtime_registry(),
            workspace_root=root,
            revision_provider=lambda: 7,
            now=clock,
            channel_binding_ref=BINDING_REF,
            authority=_Authority(revoked=revoked),
            approval_key_provider=ring,
            approval_ledger=DurableCanonicalApprovalLedger(
                root / "approvals.sqlite3", key_provider=ring, now=clock
            ),
            session_store=DurableLiveSessionStore(root / "sessions.sqlite3", now=clock),
        )
    )


def _stop(service: IBPCService) -> None:
    service._deps.approval_ledger.close()
    service._deps.session_store.close()


def _artifact(service: IBPCService, root: Path) -> None:
    artifact = root / "out.mp4"
    artifact.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 512)
    service._deps.queue.artifact_path = str(artifact)
    report = types.SimpleNamespace(
        verdict=CheckVerdict.INCONCLUSIVE, report_digest="sha256:" + "4" * 64
    )
    service._verify_artifact = lambda ctx, path, source: (  # type: ignore[method-assign]
        "sha256:" + "3" * 64,
        report,
    )


def _approved(root: Path, clip: Path):
    service = _process(root)
    session_id = service.open_session(
        tenant_id="ten",
        project_id=PROJECT,
        principal_id=PRINCIPAL,
        user_id=1,
        chat_id=1,
        current_revision=7,
    )
    proposal = service.submit_text_intent(
        session_id,
        "turn-1",
        sequence_number=1,
        text="trim from 0.0 to 2.0 seconds",
        asset_id="asset-r3",
        source_path=str(clip),
    )
    service.approve(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
    return service, session_id, proposal


def test_service_a_to_b_executes_the_pending_approval_exactly_once(media) -> None:
    first, session_id, proposal = _approved(media.root, media.clip)
    store = first._session_store
    # what A leaves behind, in the durable rows a B must be able to read
    persisted = store.get_session(session_id)
    assert persisted.tenant_id == "ten"
    assert persisted.active_revision_id == REVISION
    assert persisted.last_turn_index == 1
    turn = store.get_turn(session_id, "turn-1")
    assert turn.turn_status is TurnStatus.APPROVED
    assert turn.issued_expires_at_utc == TTL_INSTANT
    context = json.loads(turn.execution_context_json)
    assert context["issued_expires_at_utc"] == TTL_INSTANT
    assert context["session_id"] == session_id
    assert context["principal_id"] == PRINCIPAL
    assert context["project_id"] == PROJECT
    _stop(first)

    second = _process(media.root)
    assert second._sessions._sessions == {}
    report = second.recover_from_durable_state()
    assert report == {
        "sessions": 1,
        "turns": 1,
        "contexts": 1,
        "revoked": 0,
        "terminated": 0,
        "unrecoverable": 0,
        "contexts_kept": 0,
    }
    recovered = second._sessions.get_session(session_id)
    assert (recovered.tenant_id, recovered.project_id, recovered.principal_id) == (
        "ten",
        PROJECT,
        PRINCIPAL,
    )
    assert recovered.current_revision == 7
    assert recovered.max_sequence == 1

    _artifact(second, media.root)
    outcome = asyncio.run(
        second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
    )
    assert outcome.status in ("executed", "executed_unverified"), outcome.detail
    assert outcome.capsule is not None
    assert second._deps.queue.enqueued[0][1] == f"ibpc:{proposal.approval_id}"
    assert second._session_store.get_turn(session_id, "turn-1").turn_status is TurnStatus.EXECUTED
    replay = asyncio.run(
        second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
    )
    assert replay.status == "refused"
    assert "already_consumed" in replay.detail
    assert len(second._deps.queue.enqueued) == 1
    _stop(second)


def test_a_third_process_never_republishes_and_closing_fences_it(media) -> None:
    first, session_id, proposal = _approved(media.root, media.clip)
    _stop(first)
    second = _process(media.root)
    _artifact(second, media.root)
    assert asyncio.run(
        second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
    ).status in ("executed", "executed_unverified")
    closed = second.close_session(session_id)
    assert closed.status is SessionStatus.CLOSED
    _stop(second)

    third = _process(media.root)
    report = third.recover_from_durable_state()
    assert report["sessions"] == 0  # CLOSED sessions are not re-opened
    assert report["terminated"] == 1
    outcome = asyncio.run(
        third.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
    )
    assert outcome.status == "refused"
    assert third._deps.queue.enqueued == []
    _stop(third)


def test_an_aborted_session_cannot_be_executed_by_a_later_process(media) -> None:
    first, session_id, proposal = _approved(media.root, media.clip)
    aborted = first.abort_session(session_id)
    assert aborted.status is SessionStatus.ABORTED
    assert first._deps.approval_ledger.get(proposal.approval_id).state.value == "REVOKED"
    _stop(first)

    second = _process(media.root)
    _artifact(second, media.root)
    outcome = asyncio.run(
        second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
    )
    assert outcome.status == "refused"
    assert second._deps.queue.enqueued == []
    _stop(second)


def test_a_crash_between_the_two_stores_leaves_no_executable_orphan(media) -> None:
    first, session_id, proposal = _approved(media.root, media.clip)
    # the durable session is terminated, the approval revoke never ran
    first._session_store.close_session(session_id)
    assert first._deps.approval_ledger.get(proposal.approval_id).state.value == "APPROVED"
    _stop(first)

    second = _process(media.root)
    report = second.recover_from_durable_state()
    assert report["revoked"] == 1
    assert second._deps.approval_ledger.get(proposal.approval_id).state.value == "REVOKED"
    _artifact(second, media.root)
    outcome = asyncio.run(
        second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
    )
    assert outcome.status == "refused"
    assert second._deps.queue.enqueued == []
    _stop(second)


def test_recovery_re_verifies_the_principal_and_refuses_a_revoked_one(media) -> None:
    first, session_id, proposal = _approved(media.root, media.clip)
    _stop(first)

    # the membership was revoked while the approval was pending
    second = _process(media.root, revoked=True)
    _artifact(second, media.root)
    outcome = asyncio.run(
        second.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
    )
    assert outcome.status == "refused"
    assert "authority_unverified" in outcome.detail
    assert second._deps.queue.enqueued == []
    assert second._deps.approval_ledger.get(proposal.approval_id).state.value == "APPROVED"
    _stop(second)


def test_an_approval_that_expired_during_the_outage_is_refused(media) -> None:
    first, session_id, proposal = _approved(media.root, media.clip)
    _stop(first)

    late = _process(media.root, clock=lambda: "2027-01-01T00:00:00Z")
    _artifact(late, media.root)
    outcome = asyncio.run(
        late.execute(session_id, "turn-1", proposal.approval_id, principal_id=PRINCIPAL)
    )
    assert outcome.status == "refused"
    assert "approval_expired" in outcome.detail
    assert late._deps.queue.enqueued == []
    _stop(late)
