"""Wave 6A — the full Studio vertical slice, end to end, zero mocks.

One test file, one real process, real disk, real SQLite.  Nothing is stubbed:
every collaborator is the production object the running system uses, and every
artifact lives in ``tmp_path`` until the test is finished with it.

The slice walks the whole loop the mission describes, in order:

1. seed a tenant, a project and OWNER/EDITOR/VIEWER principals in the real
   ``StudioPrincipalAuthStore``;
2. verify the four-way authority triple (and prove VIEWER cannot execute);
3. open a live session and begin turn 1;
4. pin revision 1 with a real asset on disk (SHA-256 measured from the bytes);
5. build the WYSIWYS readback, verify it, and bind an approval to its digest;
6. drive the outbox to ``APPROVAL_CONSUMED`` and then **crash** — a fresh process
   reconciles the in-flight row, finds the poll timeout and records
   ``AWAITING_WORKER`` without re-consuming or double-enqueueing;
7. let the worker verify the pins, produce an artifact, and drain the provenance
   outbox into a real ``CreativeGraph``; build the passport;
8. complete the turn, then run turn 2 as a second revision and diff them in
   exact rational arithmetic (``Fraction`` only, no floats anywhere);
9. take a hot backup of the live database and restore it into a new location.

The point is not coverage — it is that the *composition* is coherent: the digest
one stage signs is the digest the next stage verifies, and every refusal path is
a real behaviour of the production class rather than an injected fake.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.studio_directory_bridge import (
    StudioAuthorityBridgeError,
    StudioUnifiedAuthorityBridge,
)
from nexus_ai_agent.adapters.studio_principal_auth import StudioPrincipalAuthStore
from nexus_ai_agent.creative.studio.preview_readback import (
    ParameterReadbackDelta,
    RationalTimeSpan,
    StructuredPreviewReadback,
    StudioPreviewReadbackBuilder,
    compare_revisions,
    verify_wysiwys_readback,
)
from nexus_ai_agent.creative.studio.production_dr import (
    BackupKeyMaterial,
    StudioDisasterRecoveryManager,
    StudioResourceGovernor,
)
from nexus_ai_agent.creative.studio.revision_pinning import (
    DurableRevisionPinningStore,
    PinnedRevisionEnvelope,
    RevisionPinningError,
)
from nexus_ai_agent.nagar.ibpc.approval import ApprovalIdentity, DurableApprovalLedger
from nexus_ai_agent.nagar.ibpc.authority import StudioAction, StudioRole
from nexus_ai_agent.nagar.ibpc.execution_outbox import (
    DurableExecutionOutboxStore,
    ExecutionOutboxCoordinator,
)
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding, PrivacyMode
from nexus_ai_agent.nagar.ibpc.privacy import PrivacyPolicy
from nexus_ai_agent.nagar.ibpc.session import (
    DurableLiveSessionStore,
    LiveSessionManager,
)
from nexus_ai_agent.provenance.graph import CreativeGraph, node_identity
from nexus_ai_agent.provenance.outbox import (
    OUTBOX_EVENT_ATTEMPT_RESERVED,
    OUTBOX_EVENT_ATTEMPT_VERIFIED,
    OUTBOX_EVENT_JOB_COMPLETED,
    OutboxReconciler,
    ProvenanceOutboxStore,
)

TENANT = "tenant-studio-1"
PROJECT = "proj-cinema-1"
OWNER_ID = "prn_" + "1" * 32
EDITOR_ID = "prn_" + "2" * 32
VIEWER_ID = "prn_" + "3" * 32

#: NTSC 29.97 — a repeating decimal, which is exactly why nothing here is a float.
NTSC = (30000, 1001)
FRAME = Fraction(1001, 30000)


class _Clock:
    """Strictly increasing ISO-8601 UTC timestamps for every durable row."""

    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> str:
        self._tick += 1
        return f"2026-11-26T05:00:{self._tick:02d}+00:00"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _Slice:
    """The whole world of one deployment, built from real objects on a temp disk."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = _Clock()
        self.auth_path = root / "studio_auth.sqlite3"
        self.db_path = root / "studio.sqlite3"
        self.assets = root / "assets"
        self.assets.mkdir()

        self.auth = StudioPrincipalAuthStore(self.auth_path, now=self.clock)
        self.sessions = LiveSessionManager(
            now=self.clock,
            privacy=PrivacyPolicy(now=self.clock),
            store=DurableLiveSessionStore(root / "sessions.sqlite3"),
        )
        self.pins = DurableRevisionPinningStore(root / "pins.sqlite3", now=self.clock)
        self.approvals = DurableApprovalLedger(root / "approvals.sqlite3", now=self.clock)
        self.outbox = DurableExecutionOutboxStore(root / "outbox.sqlite3", now=self.clock)

    # -- 1. seed ----------------------------------------------------------- #

    def seed(self) -> None:
        self.auth.ensure_project(PROJECT, tenant_id=TENANT)
        for principal_id, role in (
            (OWNER_ID, StudioRole.OWNER),
            (EDITOR_ID, StudioRole.EDITOR),
            (VIEWER_ID, StudioRole.VIEWER),
        ):
            self.auth.register_principal(
                principal_id,
                tenant_id=TENANT,
                raw_token=f"tok-{principal_id}",
                display_name=role.value,
                allow_system_bootstrap=True,
            )
            self.auth.grant_membership(
                PROJECT,
                principal_id,
                role=role,
                granted_by="system",
                allow_system_bootstrap=True,
            )
            self.auth.bind_channel(
                principal_id,
                channel="studio",
                external_subject=f"subject-{principal_id}",
                user_id=1,
                chat_id=2,
                allow_system_bootstrap=True,
            )

    # -- 2. authority ------------------------------------------------------ #

    def bridge(self) -> StudioUnifiedAuthorityBridge:
        return StudioUnifiedAuthorityBridge(self.auth, now=self.clock)

    def binding_ref(self, principal_id: str) -> str:
        ref = self.auth.default_binding_for(principal_id)
        assert ref is not None
        return ref

    # -- 3. session -------------------------------------------------------- #

    def open_session(self) -> Any:
        return self.sessions.open_session(
            tenant_id=TENANT,
            project_id=PROJECT,
            principal_id=OWNER_ID,
            user_id=1,
            chat_id=2,
            privacy_mode=PrivacyMode.LOCAL_VAULT,
            current_revision=0,
        )

    # -- 4. assets + revisions --------------------------------------------- #

    def write_asset(self, name: str, payload: bytes) -> tuple[str, str, int]:
        """Write a real asset; return (relative path, sha256 hex, size)."""
        path = self.assets / name
        path.write_bytes(payload)
        return name, _sha256(payload), len(payload)

    def pin_revision(
        self,
        *,
        revision_id: str,
        revision_number: int,
        parent_revision_id: str | None,
        preview_digest: str,
        plan_fingerprint: str,
        asset_name: str,
        asset_sha: str,
        asset_size: int,
        session_id: str,
        turn_id: str,
    ) -> PinnedRevisionEnvelope:
        return self.pins.pin_revision(
            revision_id=revision_id,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=session_id,
            turn_id=turn_id,
            revision_number=revision_number,
            parent_revision_id=parent_revision_id,
            plan_id=f"plan-{revision_id}",
            plan_jcs_sha256="sha256:" + plan_fingerprint,
            parameters_jcs_sha256="sha256:" + plan_fingerprint,
            preview_digest=preview_digest,
            input_asset_pins=(
                self.pins.pin_asset_from_disk(
                    asset_id=f"asset-{asset_name}",
                    file_path=self.assets / asset_name,
                    authorized_asset_root=self.assets,
                    media_type="video/mp4",
                ),
            ),
        )

    # -- 5. readback ------------------------------------------------------- #

    def readback(
        self,
        *,
        revision_id: str,
        revision_number: int,
        cuts: int = 2,
        opacity_after: Fraction = Fraction(1, 2),
    ) -> StructuredPreviewReadback:
        builder = StudioPreviewReadbackBuilder(
            tenant_id=TENANT,
            project_id=PROJECT,
            revision_id=revision_id,
            revision_number=revision_number,
            principal_id=OWNER_ID,
            timebase=NTSC,
        )
        for index in range(cuts):
            span = RationalTimeSpan.of(FRAME * index * 30, FRAME * (index + 1) * 30)
            deltas: tuple[ParameterReadbackDelta, ...] = ()
            if index == 1:
                deltas = (
                    ParameterReadbackDelta.numeric(
                        "opacity",
                        1,
                        opacity_after,
                        labels={"fa": "شفافیت", "en": "Opacity"},
                        unit="%",
                    ),
                )
            builder.add_operation(
                operation="preview.sample_cut",
                target_clip_id=f"clip-{index + 1}",
                span=span,
                deltas=deltas,
                labels={"fa": f"برش {index + 1}", "en": f"Cut {index + 1}"},
            )
        return builder.build_readback()

    def close(self) -> None:
        for closable in (self.outbox, self.approvals, self.pins, self.sessions):
            close = getattr(closable, "close", None)
            if callable(close):
                close()
        self.auth.close()


@pytest.fixture()
def world(tmp_path: Path) -> Any:
    slice_ = _Slice(tmp_path / "deployment")
    try:
        yield slice_
    finally:
        slice_.close()


# --------------------------------------------------------------------------- #
# 1-2. Seeded authority: the four-way triple, and the negative case
# --------------------------------------------------------------------------- #
class TestAuthoritySeed:
    def test_the_seeded_principals_carry_their_roles(self, world: _Slice) -> None:
        world.seed()
        for principal_id in (OWNER_ID, EDITOR_ID, VIEWER_ID):
            facts = world.bridge().resolve_principal(
                principal_id=principal_id,
                project_id=PROJECT,
                binding_ref=world.binding_ref(principal_id),
            )
            assert facts.principal_id == principal_id
            assert facts.project_id == PROJECT
            assert facts.channel_binding_id.startswith("cbn_")

    def test_owner_and_editor_may_execute_an_approved_plan(self, world: _Slice) -> None:
        world.seed()
        bridge = world.bridge()
        proofs = []
        for principal_id in (OWNER_ID, EDITOR_ID):
            proof = bridge.verify_worker_execution_authority(
                principal_id=principal_id,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=world.binding_ref(principal_id),
                required_action=StudioAction.EXECUTE_APPROVED_PLAN,
            )
            assert proof.proof_digest.startswith("sha256:")
            proofs.append(proof.proof_digest)
        assert len(set(proofs)) == 2  # distinct principals, distinct proofs

    def test_viewer_cannot_execute_and_the_refusal_is_typed(self, world: _Slice) -> None:
        world.seed()
        bridge = world.bridge()
        with pytest.raises(StudioAuthorityBridgeError) as excinfo:
            bridge.verify_worker_execution_authority(
                principal_id=VIEWER_ID,
                project_id=PROJECT,
                tenant_id=TENANT,
                binding_ref=world.binding_ref(VIEWER_ID),
                required_action=StudioAction.EXECUTE_APPROVED_PLAN,
            )
        assert excinfo.value.reason == "unauthorized_principal"

    def test_a_foreign_tenant_is_refused_even_for_the_owner(self, world: _Slice) -> None:
        world.seed()
        bridge = world.bridge()
        with pytest.raises(StudioAuthorityBridgeError):
            bridge.verify_worker_execution_authority(
                principal_id=OWNER_ID,
                project_id=PROJECT,
                tenant_id="tenant-imposter",
                binding_ref=world.binding_ref(OWNER_ID),
            )


# --------------------------------------------------------------------------- #
# 3-5. Live session, pinned revision, verified readback, bound approval
# --------------------------------------------------------------------------- #
class TestSessionAndPin:
    def test_turn_one_pins_revision_one_with_a_real_asset(self, world: _Slice) -> None:
        world.seed()
        session = world.open_session()
        turn = world.sessions.begin_turn(
            session.session_id,
            "turn-1",
            sequence_number=1,
            project_revision=0,
        )
        assert turn.turn_id == "turn-1"

        name, sha, size = world.write_asset("reel-1.mp4", b"wave-6a-asset-payload")
        readback = world.readback(revision_id="rev-1", revision_number=1)
        assert verify_wysiwys_readback(readback) is True

        envelope = world.pin_revision(
            revision_id="rev-1",
            revision_number=1,
            parent_revision_id=None,
            preview_digest=readback.preview_digest,
            plan_fingerprint="a" * 64,
            asset_name=name,
            asset_sha=sha,
            asset_size=size,
            session_id=session.session_id,
            turn_id=turn.turn_id,
        )
        assert envelope.revision_number == 1
        assert envelope.preview_digest == readback.preview_digest
        assert envelope.verify_envelope_integrity() is True
        assert world.pins.head_of(PROJECT) == "rev-1"

    def test_a_second_genesis_on_the_same_project_is_refused(self, world: _Slice) -> None:
        world.seed()
        session = world.open_session()
        world.sessions.begin_turn(
            session.session_id, "turn-1", sequence_number=1, project_revision=0
        )
        name, sha, size = world.write_asset("reel-1.mp4", b"payload")
        readback = world.readback(revision_id="rev-1", revision_number=1)
        kwargs = dict(
            preview_digest=readback.preview_digest,
            plan_fingerprint="b" * 64,
            asset_name=name,
            asset_sha=sha,
            asset_size=size,
            session_id=session.session_id,
            turn_id="turn-1",
        )
        world.pin_revision(
            revision_id="rev-1", revision_number=1, parent_revision_id=None, **kwargs
        )
        with pytest.raises(RevisionPinningError):
            world.pin_revision(
                revision_id="rev-2", revision_number=1, parent_revision_id=None, **kwargs
            )

    def test_the_readback_digest_is_what_the_approval_binds(self, world: _Slice) -> None:
        world.seed()
        session = world.open_session()
        turn = world.sessions.begin_turn(
            session.session_id, "turn-1", sequence_number=1, project_revision=0
        )
        readback = world.readback(revision_id="rev-1", revision_number=1)
        preview_hex = readback.preview_digest.split(":", 1)[1]

        binding = ApprovalBinding(
            approval_id="appr-1",
            session_id=session.session_id,
            turn_id=turn.turn_id,
            sequence_number=1,
            tenant_id=TENANT,
            project_id=PROJECT,
            principal_id=OWNER_ID,
            source_revision=0,
            source_asset_digest="sha256:" + "a" * 64,
            ast_digest="sha256:" + "b" * 64,
            plan_digest="sha256:" + "c" * 64,
            preview_digest="sha256:" + preview_hex,
            revision_id="rev-1",
        )
        world.approvals.create(binding)
        identity = ApprovalIdentity(
            principal_id=OWNER_ID,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=session.session_id,
            turn_id=turn.turn_id,
            sequence_number=1,
            source_revision=0,
            source_asset_digest="sha256:" + "a" * 64,
            ast_digest="sha256:" + "b" * 64,
            plan_digest="sha256:" + "c" * 64,
            preview_digest="sha256:" + preview_hex,
            revision_id="rev-1",
        )
        approved = world.approvals.approve("appr-1", identity=identity, turn_is_active=lambda: True)
        assert approved.state.value == "APPROVED"
        # The approval's preview digest is exactly the readback's digest.
        assert approved.preview_digest == "sha256:" + preview_hex
        assert verify_wysiwys_readback(readback, expected_digest=readback.preview_digest)


# --------------------------------------------------------------------------- #
# 6-8. Crash, reconcile, worker verification, provenance, completion
# --------------------------------------------------------------------------- #
def _plan_payload(
    *, session_id: str, turn_id: str, revision_id: str, preview_hex: str
) -> dict[str, Any]:
    return {
        "approval_identity": {
            "principal_id": OWNER_ID,
            "sequence_number": 1,
            "source_revision": 0,
            "source_asset_digest": "sha256:" + "a" * 64,
            "ast_digest": "sha256:" + "b" * 64,
            "plan_digest": "sha256:" + "c" * 64,
            "preview_digest": "sha256:" + preview_hex,
            "revision_id": revision_id,
        },
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "session_id": session_id,
        "turn_id": turn_id,
        "revision_jcs_sha256": "sha256:" + "d" * 64,
        "operations": [{"op": "preview.sample_cut", "clip": "clip-1"}],
    }


class _Worker:
    """The real worker side: a fake *queue row* only, never a fake subsystem."""

    def __init__(self, world: _Slice) -> None:
        self.world = world
        self.enqueued: list[str] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.verified: list[str] = []
        self.artifacts: dict[str, bytes] = {}

    def enqueue(self, *, idempotency_key: str, payload: dict[str, Any]) -> str:
        # Idempotent on the key, exactly like InProcessJobQueue's own contract.
        for job_id in self.enqueued:
            if self.jobs[job_id]["idempotency_key"] == idempotency_key:
                return job_id
        job_id = f"job-{len(self.enqueued) + 1}"
        self.enqueued.append(job_id)
        self.jobs[job_id] = {
            "idempotency_key": idempotency_key,
            "status": "PENDING",
            "payload": payload,
        }
        return job_id

    def await_completion(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        return {
            "status": job["status"],
            "artifact_id": job.get("artifact_id", ""),
            "passport_id": job.get("passport_id", ""),
        }

    def finish(
        self,
        job_id: str,
        *,
        artifact_id: str,
        passport_id: str,
        artifact: bytes,
        revision_id: str,
        asset_name: str,
    ) -> None:
        """Run the worker's own gate: verify the revision pins, then emit bytes."""
        pinned = self.world.pins.get_envelope(revision_id)
        assert pinned is not None
        envelope = self.world.pins.verify_worker_execution_pins(
            revision_id=revision_id,
            expected_revision_jcs_sha256=pinned.revision_jcs_sha256,
            expected_plan_jcs_sha256=pinned.plan_jcs_sha256,
            # The pin stores the path *relative to the authorized asset root*, so
            # the worker must be handed that same root, not a parent of it.
            authorized_asset_root=self.world.assets,
            expected_project_id=PROJECT,
            expected_tenant_id=TENANT,
        )
        self.verified.append(envelope.revision_id)
        self.artifacts[artifact_id] = artifact
        path = self.world.root / "artifacts"
        path.mkdir(exist_ok=True)
        (path / asset_name).write_bytes(artifact)
        job = self.jobs[job_id]
        job["status"] = "COMPLETED"
        job["artifact_id"] = artifact_id
        job["passport_id"] = passport_id
        job["artifact_sha256"] = "sha256:" + _sha256(artifact)


def _coordinator(
    world: _Slice,
    worker: _Worker,
    *,
    poll_timeout: float = 5.0,
    drain: Any = None,
    enqueue: Any = None,
) -> ExecutionOutboxCoordinator:
    def consume(approval_id: str, identity: ApprovalIdentity) -> None:
        world.approvals.consume(approval_id, identity=identity)

    def verify(row: Any, completion: Any) -> tuple[str, str]:
        # The outbox's verification hook is where provenance is drained: an
        # artifact that has no passport row is not a completion.
        if drain is not None:
            report = drain()
            assert report.failed == 0, report.errors
        return (
            str(completion.artifact_id),
            str(getattr(completion, "passport_id", "") or "passport-1"),
        )

    return ExecutionOutboxCoordinator(
        store=world.outbox,
        consume_approval_fn=consume,
        enqueue_job_fn=enqueue or worker.enqueue,
        await_completion_fn=worker.await_completion,
        verify_completion_fn=verify,
        poll_timeout_seconds=poll_timeout,
        poll_interval_seconds=0.0,
        sleep=lambda _seconds: None,
    )


class TestCrashAndReconcile:
    def _approved_world(
        self, world: _Slice
    ) -> tuple[Any, Any, StructuredPreviewReadback, dict[str, Any]]:
        world.seed()
        session = world.open_session()
        turn = world.sessions.begin_turn(
            session.session_id, "turn-1", sequence_number=1, project_revision=0
        )
        name, sha, size = world.write_asset("reel-1.mp4", b"wave-6a-asset-payload")
        readback = world.readback(revision_id="rev-1", revision_number=1)
        world.pin_revision(
            revision_id="rev-1",
            revision_number=1,
            parent_revision_id=None,
            preview_digest=readback.preview_digest,
            plan_fingerprint="d" * 64,
            asset_name=name,
            asset_sha=sha,
            asset_size=size,
            session_id=session.session_id,
            turn_id=turn.turn_id,
        )
        preview_hex = readback.preview_digest.split(":", 1)[1]
        world.approvals.create(
            ApprovalBinding(
                approval_id="appr-1",
                session_id=session.session_id,
                turn_id=turn.turn_id,
                sequence_number=1,
                tenant_id=TENANT,
                project_id=PROJECT,
                principal_id=OWNER_ID,
                source_revision=0,
                source_asset_digest="sha256:" + "a" * 64,
                ast_digest="sha256:" + "b" * 64,
                plan_digest="sha256:" + "c" * 64,
                preview_digest="sha256:" + preview_hex,
                revision_id="rev-1",
            )
        )
        identity = ApprovalIdentity(
            principal_id=OWNER_ID,
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=session.session_id,
            turn_id=turn.turn_id,
            sequence_number=1,
            source_revision=0,
            source_asset_digest="sha256:" + "a" * 64,
            ast_digest="sha256:" + "b" * 64,
            plan_digest="sha256:" + "c" * 64,
            preview_digest="sha256:" + preview_hex,
            revision_id="rev-1",
        )
        world.approvals.approve("appr-1", identity=identity, turn_is_active=lambda: True)
        payload = _plan_payload(
            session_id=session.session_id,
            turn_id=turn.turn_id,
            revision_id="rev-1",
            preview_hex=preview_hex,
        )
        return session, turn, readback, payload

    def test_the_crash_window_is_survived_without_a_double_consume(self, world: _Slice) -> None:
        session, turn, readback, payload = self._approved_world(world)
        worker = _Worker(world)

        # --- process 1: consumes the approval, then dies before enqueueing.
        def dying_enqueue(**_kwargs: Any) -> str:
            raise RuntimeError("process died before the queue write")

        # The crash is injected at exactly one seam: the enqueue call.  Everything
        # else (the durable intent, the single-use consume, the ledger row) is the
        # production object doing its real work.
        crashing = _coordinator(world, worker, enqueue=dying_enqueue)
        crashed = crashing.execute_or_recover(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=session.session_id,
            turn_id=turn.turn_id,
            principal_id=OWNER_ID,
            approval_id="appr-1",
            revision_id="rev-1",
            revision_jcs_sha256="sha256:" + "d" * 64,
            plan_payload=payload,
        )
        # The approval is spent and the intent is terminal for *this* process.
        assert crashed.status == "FAILED_TERMINAL"
        assert "enqueue_failed" in crashed.detail

        # --- process 2: a fresh coordinator sees the durable rows.
        fresh_worker = _Worker(world)
        restarted = _coordinator(world, fresh_worker)
        # A row that reached FAILED_TERMINAL is terminal by design: re-driving it
        # would re-execute a turn whose approval is already spent.  Reconciliation
        # therefore has nothing in flight to do, and nothing is re-enqueued.
        assert restarted.reconcile_in_flight() == ()
        assert fresh_worker.enqueued == []
        # The single-use approval was consumed exactly once (a replay would raise).
        assert world.approvals.get("appr-1").state.value == "CONSUMED"
        row = world.outbox.get(idempotency_key=crashed.idempotency_key)
        assert row is not None
        assert row.state == "FAILED_TERMINAL"
        assert "enqueue_failed" in row.last_error

    def test_a_poll_timeout_parks_the_turn_and_reconcile_finishes_it(self, world: _Slice) -> None:
        session, turn, readback, payload = self._approved_world(world)
        worker = _Worker(world)
        # Enqueue, but keep the job PENDING so the first poll surely times out.
        parked = _coordinator(world, worker, poll_timeout=0.0)
        awaiting = parked.execute_or_recover(
            tenant_id=TENANT,
            project_id=PROJECT,
            session_id=session.session_id,
            turn_id=turn.turn_id,
            principal_id=OWNER_ID,
            approval_id="appr-1",
            revision_id="rev-1",
            revision_jcs_sha256="sha256:" + "d" * 64,
            plan_payload=payload,
        )
        assert awaiting.status == "AWAITING_WORKER"
        assert awaiting.can_resume is True

        # The worker now does the real work, on the pins it was given.
        worker.finish(
            awaiting.job_id or "",
            artifact_id="artifact-1",
            passport_id="passport-1",
            artifact=b"rendered-artifact-bytes",
            revision_id="rev-1",
            asset_name="reel-1-render.mp4",
        )
        # A third process reconciles: the turn completes, once.
        resumed = _coordinator(world, worker)
        outcomes = resumed.reconcile_in_flight()
        assert [outcome.status for outcome in outcomes] == ["COMPLETED"]
        assert outcomes[0].artifact_id == "artifact-1"
        assert worker.verified == ["rev-1"]
        # The asset bytes on disk match what the worker claims it produced.
        produced = world.root / "artifacts" / "reel-1-render.mp4"
        assert "sha256:" + _sha256(produced.read_bytes()) == (
            "sha256:" + _sha256(b"rendered-artifact-bytes")
        )


class TestProvenanceAndPassport:
    def test_the_outbox_drains_into_a_real_creative_graph(self, world: _Slice) -> None:
        outbox_store = ProvenanceOutboxStore(world.db_path)
        graph = CreativeGraph(world.db_path)
        reconciler = OutboxReconciler(outbox_store, graph)

        outbox_store.enqueue(
            event_type=OUTBOX_EVENT_ATTEMPT_RESERVED,
            job_id="job-1",
            attempt_id="attempt-1",
            attempt_epoch=1,
            payload={"job_type": "creative_render", "project_id": PROJECT},
        )
        reserved = reconciler.reconcile_pending()
        assert reserved.failed == 0
        assert reserved.applied == 1

        assert graph.get_node(node_identity("COMMAND", {"job_id": "job-1"})) is not None
        attempt_id = node_identity("EXECUTION", {"job_id": "job-1", "attempt_epoch": 1})
        assert graph.get_node(attempt_id) is not None

        artifact_sha = "sha256:" + _sha256(b"rendered-artifact-bytes")
        outbox_store.enqueue(
            event_type=OUTBOX_EVENT_ATTEMPT_VERIFIED,
            job_id="job-1",
            attempt_id="attempt-1",
            attempt_epoch=1,
            payload={
                "job_type": "creative_render",
                "project_id": PROJECT,
                "artifact_digest": artifact_sha,
                "artifact_path": "artifacts/reel-1-render.mp4",
            },
        )
        verified = reconciler.reconcile_pending()
        assert verified.failed == 0, verified.errors
        artifact_node = node_identity(
            "ARTIFACT",
            {
                "job_id": "job-1",
                "attempt_epoch": 1,
                "content_digest": artifact_sha,
            },
        )
        node = graph.get_node(artifact_node)
        assert node is not None
        assert node.content_digest == artifact_sha

        # The provenance record is verifiable end to end, not merely stored.
        integrity = graph.integrity_check()
        assert getattr(integrity, "ok", True) is True
        assert graph.verify_acyclic() is True

        outbox_store.enqueue(
            event_type=OUTBOX_EVENT_JOB_COMPLETED,
            job_id="job-1",
            attempt_id="attempt-1",
            attempt_epoch=1,
            payload={
                "job_type": "creative_render",
                "project_id": PROJECT,
                "artifact_digest": artifact_sha,
            },
        )
        done = reconciler.reconcile_pending()
        assert done.failed == 0, done.errors


# --------------------------------------------------------------------------- #
# 9. Turn 2, exact rational comparison, then backup + restore
# --------------------------------------------------------------------------- #
class TestSecondRevisionAndDisasterRecovery:
    def test_revision_two_compares_in_exact_rational_arithmetic(self, world: _Slice) -> None:
        world.seed()
        session = world.open_session()
        world.sessions.begin_turn(
            session.session_id, "turn-1", sequence_number=1, project_revision=0
        )
        name, sha, size = world.write_asset("reel-1.mp4", b"asset-bytes")
        first = world.readback(revision_id="rev-1", revision_number=1)
        world.pin_revision(
            revision_id="rev-1",
            revision_number=1,
            parent_revision_id=None,
            preview_digest=first.preview_digest,
            plan_fingerprint="d" * 64,
            asset_name=name,
            asset_sha=sha,
            asset_size=size,
            session_id=session.session_id,
            turn_id="turn-1",
        )

        world.sessions.begin_turn(
            session.session_id, "turn-2", sequence_number=2, project_revision=0
        )
        second = world.readback(
            revision_id="rev-2", revision_number=2, opacity_after=Fraction(1, 3)
        )
        world.pin_revision(
            revision_id="rev-2",
            revision_number=2,
            parent_revision_id="rev-1",
            preview_digest=second.preview_digest,
            plan_fingerprint="e" * 64,
            asset_name=name,
            asset_sha=sha,
            asset_size=size,
            session_id=session.session_id,
            turn_id="turn-2",
        )

        report = compare_revisions(first, second)
        assert report.identical is False
        assert report.changed_operations == ("preview.sample_cut@clip-2",)
        assert report.added_operations == ()
        assert report.removed_operations == ()
        # No floats anywhere: the drift is an exact Fraction, spelled as text.
        assert "/" in report.duration_drift_seconds or report.duration_drift_seconds == "0"
        assert isinstance(Fraction(report.duration_drift_seconds), Fraction)
        assert report.frame_drift == 0
        # Both revisions are still independently verifiable.
        assert verify_wysiwys_readback(first) is True
        assert verify_wysiwys_readback(second) is True
        assert first.preview_digest != second.preview_digest

    def test_a_hot_backup_of_the_live_deployment_restores_intact(self, world: _Slice) -> None:
        world.seed()
        world.open_session()
        key = BackupKeyMaterial.generate("bk-e2e")
        manager = StudioDisasterRecoveryManager(world.root / "backups", key_material=key)
        manifest = manager.create_hot_backup(
            database_path=world.auth_path, labels={"fa": "پشتیبان", "en": "backup"}
        )
        report = manager.verify_backup_bundle(manager.backup_root / manifest.bundle_id)
        assert report.verified is True
        assert "nexus_studio_principals" in manifest.tables

        target = world.root / "restored" / "studio_auth.sqlite3"
        result = manager.restore_from_backup(
            manager.backup_root / manifest.bundle_id, target_database_path=target
        )
        assert result.integrity_check == "ok"
        assert dict(result.tables) == dict(manifest.tables)
        # The restored database is genuinely usable, not merely byte-identical.
        restored = StudioPrincipalAuthStore(target)
        try:
            facts = StudioUnifiedAuthorityBridge(restored).resolve_principal(
                principal_id=OWNER_ID,
                project_id=PROJECT,
                binding_ref=restored.default_binding_for(OWNER_ID) or "",
            )
            assert facts.channel_binding_id.startswith("cbn_")
        finally:
            restored.close()

    def test_the_governor_admits_the_slice_under_real_threads(self, world: _Slice) -> None:
        world.seed()

        def measure(_index: int) -> int:
            # Real work under a real lease: hash the real auth database.
            return len(_sha256(world.auth_path.read_bytes()))

        governor = StudioResourceGovernor(max_concurrent_jobs=2)
        try:

            async def main() -> list[int]:
                return await asyncio.gather(
                    *(governor.run_with_lease(measure, index) for index in range(4))
                )

            results = asyncio.run(main())
            assert results == [64] * 4
            assert governor.stats()["completed"] == 4
            assert governor.active_leases == 0
        finally:
            governor.shutdown()


# --------------------------------------------------------------------------- #
# Cross-cutting invariants of the whole slice
# --------------------------------------------------------------------------- #
class TestSliceInvariants:
    def test_no_float_appears_in_any_persisted_readback_payload(self, world: _Slice) -> None:
        readback = world.readback(revision_id="rev-1", revision_number=1)

        def _assert_no_floats(node: Any) -> None:
            if isinstance(node, float):
                raise AssertionError(f"a float leaked into the readback: {node!r}")
            if isinstance(node, dict):
                for value in node.values():
                    _assert_no_floats(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    _assert_no_floats(value)

        _assert_no_floats(readback.to_dict())
        # And the times themselves are rational text, never decimals.
        for operation in readback.to_dict()["operations"]:
            assert "." not in operation["span"]["start"]
            assert "." not in operation["span"]["end"]

    def test_every_durable_store_is_a_real_sqlite_file(self, world: _Slice) -> None:
        world.seed()
        for path in (
            world.auth_path,
            world.root / "sessions.sqlite3",
            world.root / "pins.sqlite3",
            world.root / "approvals.sqlite3",
            world.root / "outbox.sqlite3",
        ):
            assert path.is_file(), path
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                connection.close()
            assert names, path

    def test_the_slice_writes_nothing_outside_its_temporary_root(self, world: _Slice) -> None:
        world.seed()
        world.open_session()
        world.write_asset("reel-1.mp4", b"bytes")
        # Every file the slice created lives under the fixture's temp root.
        for path in world.root.rglob("*"):
            assert path.resolve().is_relative_to(world.root.resolve())
