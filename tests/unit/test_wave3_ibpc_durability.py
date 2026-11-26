"""Wave 3 comprehensive tests: durable live IBPC sessions/turns, canonical
single-use approvals, durable revocable privacy consent, and Studio RBAC."""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.adapters.studio_principal_auth import (
    PrincipalAuthenticationError,
    ProjectMembershipAuthorizer,
    StudioPrincipalAuthStore,
    build_studio_ibpc_service,
)
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc import (
    ApprovalBinding,
    ApprovalError,
    DataCategory,
    DurableApprovalLedger,
    DurableLiveSessionStore,
    DurablePrivacyConsentStore,
    IBPCServiceDeps,
    IdempotencyConflictError,
    LiveSessionManager,
    PipelineResult,
    PrivacyMode,
    PrivacyPolicy,
    PrivacyViolation,
    ProcessingScope,
    SessionClosedError,
    SessionEpochConflictError,
    SessionStatus,
    StaleRevisionError,
    StudioAction,
    StudioAuthorizationError,
    StudioRole,
    build_ibpc_service,
    create_default_key_provider,
)
from nexus_ai_agent.nagar.ibpc import service as service_mod
from nexus_ai_agent.nagar.ibpc.approval import ApprovalIdentity
from nexus_ai_agent.nagar.ibpc.approval_mac import StaticKeyRing
from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import compute_canonical_approval_hash

BINDING_REF = "cbn_" + "a" * 64
OWNER_ID = "prn_" + "1" * 32
EDITOR_ID = "prn_" + "2" * 32
VIEWER_ID = "prn_" + "3" * 32
AUDITOR_ID = "prn_" + "4" * 32
OUTSIDER_ID = "prn_" + "5" * 32


class _Clock:
    def __init__(self, initial: str = "2026-11-25T10:00:00Z") -> None:
        self.current = initial

    def __call__(self) -> str:
        return self.current


def _fake_media(path: Path) -> None:
    path.write_bytes(b"FAKE-MP4-BYTES-0001")


def _stub_media_tools(monkeypatch: pytest.MonkeyPatch, duration_us: int = 5_000_000) -> None:
    def fake_probe(path: Path, *, binary: str | None = None):
        return types.SimpleNamespace(duration_us=duration_us, width=16, height=16), 30.0

    def fake_shadow(path, scope, plan_digest, **kwargs):
        payload = {
            "schema_id": "nexus.ibpc.shadow_preview.v1",
            "shadow_id": "shad_" + "1" * 32,
            "plan_digest": plan_digest,
            "scope_digest": scope.scope_digest,
            "source_duration_us": duration_us,
            "scope_start_us": scope.start_us,
            "scope_end_us": scope.end_us,
            "keyframes": [],
            "composite_digest": "sha256:" + "1" * 64,
        }
        return types.SimpleNamespace(
            plan_digest=plan_digest,
            scope_digest=scope.scope_digest,
            composite_digest=payload["composite_digest"],
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
            "computed_at": "2026-11-25T10:00:00Z",
            "signature_digest": "sha256:" + "2" * 64,
        }
        return types.SimpleNamespace(
            signature_digest=payload["signature_digest"],
            scope_digest=scope.scope_digest,
            model_dump=lambda **_kw: payload,
        )

    monkeypatch.setattr(service_mod, "probe_media", fake_probe)
    monkeypatch.setattr(service_mod, "build_shadow_preview", fake_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)
    monkeypatch.setattr(service_mod, "verify_shadow_manifest", lambda _m: None)


def _setup_auth_store(
    db_path: Path,
    *,
    now: _Clock,
    principal_id: str = OWNER_ID,
    project_id: str = "proj-w3",
    role: StudioRole = StudioRole.OWNER,
) -> tuple[StudioPrincipalAuthStore, str]:
    store = StudioPrincipalAuthStore(db_path, now=now)
    store.register_principal(
        principal_id,
        tenant_id="ten-1",
        raw_token=f"tok-{principal_id}-secret",
        display_name="Owner",
    )
    bref = store.bind_channel(
        principal_id,
        channel="studio",
        external_subject=f"sub-{principal_id}",
        user_id=101,
        chat_id=202,
    )
    store.ensure_project(project_id, tenant_id="ten-1", active_revision_id="0")
    store.grant_membership(project_id, principal_id, role=role, granted_by=principal_id)
    return store, bref


# ---------------------------------------------------------------------------
# Slice 3.1: DurableLiveSessionStore & Invariants A-F
# ---------------------------------------------------------------------------


class TestSlice31DurableLiveSessions:
    def test_wal_pragmas_and_monotonic_turn_ordering_invariant_a(self, tmp_path: Path) -> None:
        clock = _Clock()
        store = DurableLiveSessionStore(tmp_path / "sessions.sqlite3")
        try:
            mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
            busy = store._conn.execute("PRAGMA busy_timeout").fetchone()[0]
            fk = store._conn.execute("PRAGMA foreign_keys").fetchone()[0]
            sync = store._conn.execute("PRAGMA synchronous").fetchone()[0]
            assert str(mode).lower() == "wal"
            assert int(busy) == 5000
            assert int(fk) == 1
            assert int(sync) == 2  # FULL

            mgr = LiveSessionManager(now=clock, privacy=PrivacyPolicy(now=clock), store=store)
            sess = mgr.open_session(
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id="usr-1",
                user_id=1,
                chat_id=1,
                current_revision=3,
                active_revision_id="rev-3",
            )
            t1 = mgr.begin_turn(
                sess.session_id,
                "t1",
                sequence_number=1,
                project_revision=3,
                target_revision_id="rev-3",
            )
            t2 = mgr.begin_turn(
                sess.session_id,
                "t2",
                sequence_number=2,
                project_revision=3,
                target_revision_id="rev-3",
            )
            t3 = mgr.begin_turn(
                sess.session_id,
                "t3",
                sequence_number=3,
                project_revision=3,
                target_revision_id="rev-3",
            )
            assert [t1.turn_index, t2.turn_index, t3.turn_index] == [1, 2, 3]
            listed = mgr.list_turns(sess.session_id)
            assert [t.turn_id for t in listed] == ["t1", "t2", "t3"]

            # Schema-level UNIQUE(session_id, turn_index) refuses duplicate index.
            with pytest.raises(sqlite3.IntegrityError):
                store._conn.execute(
                    """
                    INSERT INTO nexus_ibpc_turns (
                        session_id, turn_id, turn_index, sequence_number,
                        project_revision, target_revision_id, turn_state,
                        transcript_state, created_at, updated_at
                    ) VALUES (?, 't_dup_idx', 2, 4, 3, 'rev-3', 'RECEIVED', 'EMPTY', ?, ?)
                    """,
                    (sess.session_id, clock(), clock()),
                )
        finally:
            store.close()

    def test_idempotency_deduplication_and_conflict_invariant_b(self, tmp_path: Path) -> None:
        clock = _Clock()
        store = DurableLiveSessionStore(tmp_path / "sessions.sqlite3")
        try:
            mgr = LiveSessionManager(now=clock, privacy=PrivacyPolicy(now=clock), store=store)
            s1 = mgr.open_session(
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id="usr-1",
                user_id=1,
                chat_id=1,
                current_revision=0,
            )
            s2 = mgr.open_session(
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id="usr-1",
                user_id=1,
                chat_id=1,
                current_revision=0,
            )
            digest_a = digest_of({"cmd": "trim", "start": 0})
            digest_b = digest_of({"cmd": "trim", "start": 1})
            t1 = mgr.begin_turn(
                s1.session_id,
                "turn-1",
                sequence_number=1,
                project_revision=0,
                idempotency_key="idem-1",
                payload_digest=digest_a,
            )
            epoch_after_first = mgr.get_session(s1.session_id).session_epoch
            # Exact replay returns existing turn without duplicate or epoch advance.
            t1_replay = mgr.begin_turn(
                s1.session_id,
                "turn-1-retry",
                sequence_number=2,
                project_revision=0,
                idempotency_key="idem-1",
                payload_digest=digest_a,
            )
            assert t1_replay.turn_id == t1.turn_id
            assert t1_replay.turn_index == t1.turn_index
            assert mgr.get_session(s1.session_id).session_epoch == epoch_after_first

            # Same key + different payload raises IdempotencyConflictError.
            with pytest.raises(IdempotencyConflictError):
                mgr.begin_turn(
                    s1.session_id,
                    "turn-1-conflict",
                    sequence_number=2,
                    project_revision=0,
                    idempotency_key="idem-1",
                    payload_digest=digest_b,
                )

            # Same key on a DIFFERENT session is allowed and scoped independently.
            t_other = mgr.begin_turn(
                s2.session_id,
                "turn-other",
                sequence_number=1,
                project_revision=0,
                idempotency_key="idem-1",
                payload_digest=digest_b,
            )
            assert t_other.session_id == s2.session_id
        finally:
            store.close()

    def test_stale_revision_rejected_and_advance_revision_invariant_c(self, tmp_path: Path) -> None:
        clock = _Clock()
        store = DurableLiveSessionStore(tmp_path / "sessions.sqlite3")
        try:
            mgr = LiveSessionManager(now=clock, privacy=PrivacyPolicy(now=clock), store=store)
            sess = mgr.open_session(
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id="usr-1",
                user_id=1,
                chat_id=1,
                current_revision=1,
                active_revision_id="rev-1",
            )
            with pytest.raises(StaleRevisionError):
                mgr.begin_turn(
                    sess.session_id,
                    "stale-turn",
                    sequence_number=1,
                    project_revision=1,
                    target_revision_id="rev-0",
                )
            turn = mgr.begin_turn(
                sess.session_id,
                "live-turn",
                sequence_number=1,
                project_revision=1,
                target_revision_id="rev-1",
            )
            assert turn.target_revision_id == "rev-1"
            mgr.mark_intent_parsed(sess.session_id, "live-turn", intent_digest="sha256:" + "a" * 64)
            mgr.advance_revision(sess.session_id, new_revision=2, new_revision_id="rev-2")
            with pytest.raises(StaleRevisionError):
                mgr.mark_plan_preview(sess.session_id, "live-turn")
        finally:
            store.close()

    def test_safe_close_pause_abort_revokes_approvals_invariant_d(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_media_tools(monkeypatch)
        clock = _Clock()
        clip = tmp_path / "clip.mp4"
        _fake_media(clip)
        auth_store, bref = _setup_auth_store(tmp_path / "auth.sqlite3", now=clock)
        queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")
        service = build_studio_ibpc_service(
            queue=queue,
            registry=build_runtime_registry(),
            workspace_root=tmp_path / "ws",
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth_store,
            channel_binding_ref=bref,
            now=clock,
        )
        try:
            sid = service.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                current_revision=0,
            )
            res = service.submit_text_intent(
                sid,
                "turn-1",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip),
            )
            assert isinstance(res, PipelineResult)
            # Pause revokes pending approval and blocks new turns until resumed.
            paused = service.pause_session(sid, principal_id=OWNER_ID)
            assert paused.status is SessionStatus.PAUSED
            assert service.approvals.get(res.approval_id).state.value == "REVOKED"
            with pytest.raises(SessionClosedError):
                service.submit_text_intent(
                    sid,
                    "turn-2",
                    sequence_number=2,
                    text="trim from 1 to 2 seconds",
                    asset_id="clip",
                    source_path=str(clip),
                )
            resumed = service.resume_session(sid, principal_id=OWNER_ID)
            assert resumed.status is SessionStatus.ACTIVE
            res2 = service.submit_text_intent(
                sid,
                "turn-2",
                sequence_number=2,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip),
            )
            assert isinstance(res2, PipelineResult)
            service.approve(sid, "turn-2", res2.approval_id, principal_id=OWNER_ID)
            # Close revokes even APPROVED unconsumed binding and is terminal.
            closed = service.close_session(sid, principal_id=OWNER_ID)
            assert closed.status is SessionStatus.CLOSED
            assert service.approvals.get(res2.approval_id).state.value == "REVOKED"
            with pytest.raises(SessionClosedError):
                service.resume_session(sid, principal_id=OWNER_ID)
            with pytest.raises(SessionClosedError):
                service.abort_session(sid, principal_id=OWNER_ID)

            # Abort on a second active session also terminates and revokes.
            sid2 = service.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
            )
            res3 = service.submit_text_intent(
                sid2,
                "turn-a",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip),
            )
            assert isinstance(res3, PipelineResult)
            aborted = service.abort_session(sid2, principal_id=OWNER_ID)
            assert aborted.status is SessionStatus.ABORTED
            assert service.approvals.get(res3.approval_id).state.value == "REVOKED"
        finally:
            service.close()
            auth_store.close()

    @pytest.mark.asyncio
    async def test_full_restart_recovery_and_replay_refusal_invariant_e(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_media_tools(monkeypatch)
        clock = _Clock()
        clip = tmp_path / "clip.mp4"
        _fake_media(clip)
        ws = tmp_path / "ws"
        ws.mkdir()
        from nexus_ai_agent.config.settings import get_settings

        monkeypatch.setenv("CREATIVE_TEMP_DIR", str(ws))
        get_settings.cache_clear()
        auth_db = tmp_path / "auth.sqlite3"

        class _RecordingQueue:
            def __init__(self) -> None:
                self.enqueued: list[dict] = []
                self._results: dict[str, dict] = {}

            async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
                jid = f"job-{len(self.enqueued) + 1}"
                out = Path(str(payload["workspace_dir"])) / "out.mp4"
                out.write_bytes(b"RENDERED-MP4")
                self.enqueued.append(
                    {
                        "job_id": jid,
                        "job_type": job_type,
                        "idempotency_key": idempotency_key,
                        "payload": payload,
                    }
                )
                self._results[jid] = {"success": True, "artifact_path": str(out)}
                return jid

            async def get_status(self, job_id: str):
                from nexus_ai_agent.application.ports.job_queue import JobStatus

                return JobStatus.COMPLETED

            async def get_result(self, job_id: str) -> dict | None:
                return self._results.get(job_id)

        from nexus_ai_agent.nagar.ibpc.models import CheckVerdict

        queue = _RecordingQueue()
        monkeypatch.setattr(
            service_mod,
            "verify_keep",
            lambda *_a, **_kw: types.SimpleNamespace(
                verdict=CheckVerdict.PASS,
                report_digest="sha256:" + "3" * 64,
            ),
        )

        auth1, bref = _setup_auth_store(auth_db, now=clock)
        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")
        svc1 = build_studio_ibpc_service(
            queue=queue,
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth1,
            channel_binding_ref=bref,
            now=clock,
        )
        sid = svc1.open_session(
            tenant_id="ten-1",
            project_id="proj-w3",
            principal_id=OWNER_ID,
            user_id=1,
            chat_id=1,
        )
        res = svc1.submit_text_intent(
            sid,
            "turn-1",
            sequence_number=1,
            text="trim from 1 to 2 seconds",
            asset_id="clip",
            source_path=str(clip),
        )
        assert isinstance(res, PipelineResult)
        svc1.approve(sid, "turn-1", res.approval_id, principal_id=OWNER_ID)
        svc1.close()
        auth1.close()

        # Simulate process restart with fresh service & auth store instances!
        auth2 = StudioPrincipalAuthStore(auth_db, now=clock)
        svc2 = build_studio_ibpc_service(
            queue=queue,
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth2,
            channel_binding_ref=bref,
            now=clock,
        )
        try:
            recovered = svc2.get_session(sid, principal_id=OWNER_ID)
            assert recovered.session_id == sid
            assert "turn-1" in recovered.turns
            outcome = await svc2.execute(sid, "turn-1", res.approval_id, principal_id=OWNER_ID)
            assert outcome.status in ("executed", "executed_unverified")
            assert outcome.job_id is not None
            assert len(queue.enqueued) == 1

            # Restart a third time: replay of the consumed approval must fail closed!
            svc2.close()
            svc3 = build_studio_ibpc_service(
                queue=queue,
                registry=build_runtime_registry(),
                workspace_root=ws,
                revision_provider=lambda: 0,
                key_provider=ring,
                auth_store=auth2,
                channel_binding_ref=bref,
                now=clock,
            )
            try:
                replay = await svc3.execute(sid, "turn-1", res.approval_id, principal_id=OWNER_ID)
                assert replay.status == "refused"
                assert replay.job_id is None
                assert len(queue.enqueued) == 1
            finally:
                svc3.close()
        finally:
            auth2.close()

    def test_concurrent_turn_serialization_invariant_f(self, tmp_path: Path) -> None:
        clock = _Clock()
        store = DurableLiveSessionStore(tmp_path / "sessions.sqlite3")
        try:
            mgr = LiveSessionManager(now=clock, privacy=PrivacyPolicy(now=clock), store=store)
            sess = mgr.open_session(
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id="usr-1",
                user_id=1,
                chat_id=1,
            )
            initial_epoch = sess.session_epoch
            barrier = threading.Barrier(4)
            wins: list[str] = []
            conflicts: list[Exception] = []
            lock = threading.Lock()

            def worker(idx: int) -> None:
                barrier.wait()
                try:
                    t = mgr.begin_turn(
                        sess.session_id,
                        f"turn-{idx}",
                        sequence_number=idx + 1,
                        project_revision=0,
                        expected_epoch=initial_epoch,
                    )
                    with lock:
                        wins.append(t.turn_id)
                except SessionEpochConflictError as exc:
                    with lock:
                        conflicts.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert len(wins) == 1
            assert len(conflicts) == 3
            assert mgr.get_session(sess.session_id).session_epoch == initial_epoch + 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Slice 3.2: Canonical Approval Binding & DurableApprovalLedger
# ---------------------------------------------------------------------------


class TestSlice32DurableApprovalBinding:
    def test_canonical_binding_hash_expiry_and_tamper_rejection(self, tmp_path: Path) -> None:
        clock = _Clock("2026-11-25T10:00:00Z")
        ledger = DurableApprovalLedger(tmp_path / "approvals.sqlite3", now=clock)
        try:
            scope_sig = "sha256:" + "9" * 64
            plan_digest = "sha256:" + "4" * 64
            preview_digest = "sha256:" + "5" * 64
            ast_digest = "sha256:" + "6" * 64
            asset_digest = "sha256:" + "7" * 64
            exp = "2026-11-25T10:10:00Z"
            ahash = compute_canonical_approval_hash(
                project_id="proj-1",
                revision_id="rev-7",
                plan_digest=plan_digest,
                preview_digest=preview_digest,
                scope_signature=scope_sig,
                principal_id="usr-1",
                expires_at_utc=exp,
            )
            assert ahash != compute_canonical_approval_hash(
                project_id="proj-1",
                revision_id="rev-7",
                plan_digest=plan_digest,
                preview_digest=preview_digest,
                scope_signature=scope_sig,
                principal_id="usr-1",
                expires_at_utc="2026-11-25T10:15:00Z",
            )
            binding = ApprovalBinding(
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id="usr-1",
                source_revision=7,
                source_asset_digest=asset_digest,
                ast_digest=ast_digest,
                plan_digest=plan_digest,
                preview_digest=preview_digest,
                revision_id="rev-7",
                scope_signature=scope_sig,
                expires_at_utc=exp,
                approval_hash=ahash,
            )
            created = ledger.create(binding)
            ident = ApprovalIdentity(
                principal_id="usr-1",
                tenant_id="ten-1",
                project_id="proj-1",
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                source_revision=7,
                source_asset_digest=asset_digest,
                ast_digest=ast_digest,
                plan_digest=plan_digest,
                preview_digest=preview_digest,
                revision_id="rev-7",
                scope_signature=scope_sig,
                expires_at_utc=exp,
            )

            # Tampered scope_signature fails closed in constant time.
            bad_scope = ApprovalIdentity(
                **{**ident.__dict__, "scope_signature": "sha256:" + "0" * 64}
            )
            with pytest.raises(ApprovalError, match="scope_signature_mismatch"):
                ledger.approve(created.approval_id, identity=bad_scope, turn_is_active=lambda: True)

            # Tampered approval_hash on stored row fails closed.
            tampered_row = created.model_copy(update={"approval_hash": "sha256:" + "0" * 64})
            ledger._conn.execute(
                "UPDATE approvals SET body = ? WHERE approval_id = ?",
                (tampered_row.model_dump_json(), created.approval_id),
            )
            with pytest.raises(ApprovalError, match="approval_hash_mismatch"):
                ledger.approve(created.approval_id, identity=ident, turn_is_active=lambda: True)

            # Restore valid row and test expiry enforcement.
            ledger._conn.execute(
                "UPDATE approvals SET body = ? WHERE approval_id = ?",
                (created.model_dump_json(), created.approval_id),
            )
            clock.current = "2026-11-25T10:10:00Z"
            with pytest.raises(ApprovalError, match="expired"):
                ledger.approve(created.approval_id, identity=ident, turn_is_active=lambda: True)

            # Valid window: approve then consume once; replay fails closed.
            clock.current = "2026-11-25T10:05:00Z"
            ledger.approve(created.approval_id, identity=ident, turn_is_active=lambda: True)
            consumed = ledger.consume(created.approval_id, identity=ident)
            assert consumed.state.value == "CONSUMED"
            with pytest.raises(ApprovalError, match="replay"):
                ledger.consume(created.approval_id, identity=ident)
        finally:
            ledger.close()

    def test_build_ibpc_service_wires_durable_stores(self, tmp_path: Path) -> None:
        key_b64 = base64.b64encode(b"z" * 32).decode("ascii")
        env = {
            "NEXUS_IBPC_APPROVAL_KEY_RING": json.dumps({"active": key_b64}),
            "NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID": "active",
        }
        provider = create_default_key_provider(env)
        queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
        svc = build_ibpc_service(
            IBPCServiceDeps(
                queue=queue,  # type: ignore[arg-type]
                registry=build_runtime_registry(),
                workspace_root=tmp_path / "ws",
                revision_provider=lambda: 0,
            ),
            key_provider=provider,
        )
        try:
            assert isinstance(svc.approvals, DurableApprovalLedger)
            assert svc.sessions.store is not None
            assert svc.privacy.store is not None
        finally:
            svc.close()


# ---------------------------------------------------------------------------
# Slice 3.3: DurablePrivacyConsentStore
# ---------------------------------------------------------------------------


class TestSlice33DurablePrivacyConsent:
    def test_durable_grant_immediate_revoke_expiry_and_append_only_audit(
        self, tmp_path: Path
    ) -> None:
        clock = _Clock("2026-11-25T10:00:00Z")
        store = DurablePrivacyConsentStore(tmp_path / "privacy.sqlite3")
        policy = PrivacyPolicy(now=clock, store=store, configured_providers=frozenset({"gemini"}))
        try:
            # Default LOCAL_VAULT refuses external provider dispatch.
            with pytest.raises(PrivacyViolation) as local_exc:
                policy.enforce_processing_consent(
                    project_id="proj-1",
                    principal_id="usr-1",
                    mode=PrivacyMode.LOCAL_VAULT,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    provider_id="gemini",
                )
            assert local_exc.value.reason == "egress_refused"

            # Grant external dispatch + biometric ROI consent with expiry.
            grant = policy.grant_durable_consent(
                project_id="proj-1",
                principal_id="usr-1",
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                data_category=DataCategory.TRANSCRIPT,
                provider_id="gemini",
                expires_at_utc="2026-11-25T11:00:00Z",
                metadata={"transcript": "SENSITIVE_RAW_TEXT_DO_NOT_LOG", "note": "ui-toggle"},
            )
            bio_grant = policy.grant_durable_consent(
                project_id="proj-1",
                principal_id="usr-1",
                mode=PrivacyMode.LOCAL_VAULT,
                processing_scope=ProcessingScope.LOCAL_PROCESSING,
                data_category=DataCategory.BIOMETRIC_FACE_ROI,
                expires_at_utc="2026-11-25T11:00:00Z",
            )
            # Active consents pass.
            assert (
                policy.enforce_processing_consent(
                    project_id="proj-1",
                    principal_id="usr-1",
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    provider_id="gemini",
                ).consent_id
                == grant.consent_id
            )
            assert (
                policy.enforce_processing_consent(
                    project_id="proj-1",
                    principal_id="usr-1",
                    mode=PrivacyMode.LOCAL_VAULT,
                    processing_scope=ProcessingScope.LOCAL_PROCESSING,
                    data_category=DataCategory.BIOMETRIC_FACE_ROI,
                ).consent_id
                == bio_grant.consent_id
            )

            # Immediate revocation takes effect immediately across policy + restart.
            policy.revoke_durable_consent(
                grant.consent_id, actor_id="usr-1", reason="user_withdrew_consent"
            )
            with pytest.raises(PrivacyViolation) as revoked_exc:
                policy.enforce_processing_consent(
                    project_id="proj-1",
                    principal_id="usr-1",
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    provider_id="gemini",
                )
            assert revoked_exc.value.reason == "consent_revoked"

            # Expiry check on biometric grant.
            clock.current = "2026-11-25T11:00:00Z"
            with pytest.raises(PrivacyViolation) as exp_exc:
                policy.enforce_processing_consent(
                    project_id="proj-1",
                    principal_id="usr-1",
                    mode=PrivacyMode.LOCAL_VAULT,
                    processing_scope=ProcessingScope.LOCAL_PROCESSING,
                    data_category=DataCategory.BIOMETRIC_FACE_ROI,
                )
            assert exp_exc.value.reason == "consent_expired"

            # Verify zero raw sensitive payload in DB + append-only triggers.
            raw_dump = (
                store._conn.execute(
                    "SELECT group_concat(metadata_digest || reason) "
                    "FROM nexus_privacy_consent_audit"
                ).fetchone()[0]
                or ""
            )
            assert "SENSITIVE_RAW_TEXT_DO_NOT_LOG" not in raw_dump
            audit_rows = store.list_audit(project_id="proj-1")
            assert [a.event_type for a in audit_rows] == [
                "DENIED",
                "GRANTED",
                "GRANTED",
                "REVOKED",
                "DENIED",
                "EXPIRED",
            ]
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                store._conn.execute("UPDATE nexus_privacy_consent_audit SET reason = 'tamper'")
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                store._conn.execute("DELETE FROM nexus_privacy_consent_audit")
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Slice 3.4: StudioPrincipalAuthStore, ProjectMembershipAuthorizer & HTTP API
# ---------------------------------------------------------------------------


class TestSlice34StudioPrincipalAuthAndAPI:
    def test_rbac_matrix_and_revocation_fail_closed(self, tmp_path: Path) -> None:
        clock = _Clock()
        store = StudioPrincipalAuthStore(tmp_path / "studio_auth.sqlite3", now=clock)
        authorizer = ProjectMembershipAuthorizer(store)
        try:
            store.ensure_project("proj-1", tenant_id="ten-1")
            for pid, role in (
                (OWNER_ID, StudioRole.OWNER),
                (EDITOR_ID, StudioRole.EDITOR),
                (VIEWER_ID, StudioRole.VIEWER),
                (AUDITOR_ID, StudioRole.AUDITOR),
            ):
                store.register_principal(pid, tenant_id="ten-1", raw_token=f"secret-{pid}")
                store.bind_channel(pid, channel="studio", external_subject=f"sub-{pid}")
                store.grant_membership("proj-1", pid, role=role, granted_by=OWNER_ID)
            store.register_principal(OUTSIDER_ID, tenant_id="ten-1", raw_token="secret-outsider")

            # OWNER: everything allowed.
            assert authorizer.authorize_action(
                principal_id=OWNER_ID,
                project_id="proj-1",
                action=StudioAction.MANAGE_MEMBERSHIP,
            ).allowed

            # EDITOR: edits allowed, MANAGE_MEMBERSHIP denied.
            assert authorizer.authorize_action(
                principal_id=EDITOR_ID,
                project_id="proj-1",
                action=StudioAction.SUBMIT_INTENT,
            ).allowed
            with pytest.raises(StudioAuthorizationError, match="insufficient_role"):
                authorizer.authorize_action(
                    principal_id=EDITOR_ID,
                    project_id="proj-1",
                    action=StudioAction.MANAGE_MEMBERSHIP,
                )

            # VIEWER: read allowed, mutations & audit denied.
            assert authorizer.authorize_action(
                principal_id=VIEWER_ID,
                project_id="proj-1",
                action=StudioAction.READ_SESSION,
            ).allowed
            for denied_action in (
                StudioAction.SUBMIT_INTENT,
                StudioAction.APPROVE_PLAN,
                StudioAction.EXECUTE_PLAN,
                StudioAction.MANAGE_CONSENT,
                StudioAction.READ_AUDIT,
            ):
                with pytest.raises(StudioAuthorizationError, match="insufficient_role"):
                    authorizer.authorize_action(
                        principal_id=VIEWER_ID,
                        project_id="proj-1",
                        action=denied_action,
                    )

            # AUDITOR: read + READ_AUDIT allowed, mutations denied.
            assert authorizer.authorize_action(
                principal_id=AUDITOR_ID,
                project_id="proj-1",
                action=StudioAction.READ_AUDIT,
            ).allowed
            with pytest.raises(StudioAuthorizationError, match="insufficient_role"):
                authorizer.authorize_action(
                    principal_id=AUDITOR_ID,
                    project_id="proj-1",
                    action=StudioAction.EXECUTE_PLAN,
                )

            # Non-member denied.
            with pytest.raises(StudioAuthorizationError, match="not_a_project_member"):
                authorizer.authorize_action(
                    principal_id=OUTSIDER_ID,
                    project_id="proj-1",
                    action=StudioAction.READ_SESSION,
                )

            # Revoked membership / principal / channel binding fail closed.
            store.revoke_membership("proj-1", EDITOR_ID)
            with pytest.raises(StudioAuthorizationError, match="membership_revoked"):
                authorizer.authorize_action(
                    principal_id=EDITOR_ID,
                    project_id="proj-1",
                    action=StudioAction.READ_SESSION,
                )

            bref_viewer = store.default_binding_for(VIEWER_ID) or ""
            store.revoke_channel_binding(bref_viewer)
            with pytest.raises(
                StudioAuthorizationError, match="invalid_or_revoked_channel_binding"
            ):
                authorizer.authorize_action(
                    principal_id=VIEWER_ID,
                    project_id="proj-1",
                    action=StudioAction.READ_SESSION,
                    binding_ref=bref_viewer,
                )

            store.revoke_principal(OWNER_ID)
            with pytest.raises(PrincipalAuthenticationError, match="principal_revoked"):
                store.authenticate_token(f"secret-{OWNER_ID}")
        finally:
            store.close()

    def test_studio_ibpc_http_endpoints_enforce_rbac_and_persistence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        _stub_media_tools(monkeypatch)
        clock = _Clock()
        clip = tmp_path / "clip.mp4"
        _fake_media(clip)
        ws = tmp_path / "ws"
        auth_db = tmp_path / "studio_auth.sqlite3"

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-bearer-token")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-http")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_STUDIO_ACTIVE_REVISION", "0")
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "queue.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        store = StudioPrincipalAuthStore(auth_db, now=clock)
        store.ensure_project("proj-http", tenant_id="ten-1")
        store.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-123")
        store.bind_channel(OWNER_ID, channel="studio", external_subject="sub-owner")
        store.grant_membership("proj-http", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        store.register_principal(VIEWER_ID, tenant_id="ten-1", raw_token="tok-viewer-123")
        store.bind_channel(VIEWER_ID, channel="studio", external_subject="sub-viewer")
        store.grant_membership("proj-http", VIEWER_ID, role=StudioRole.VIEWER, granted_by=OWNER_ID)

        store.register_principal(AUDITOR_ID, tenant_id="ten-1", raw_token="tok-auditor-123")
        store.bind_channel(AUDITOR_ID, channel="studio", external_subject="sub-auditor")
        store.grant_membership(
            "proj-http", AUDITOR_ID, role=StudioRole.AUDITOR, granted_by=OWNER_ID
        )
        store.close()

        client = TestClient(app_module.app)
        deploy_headers = {"Authorization": "Bearer deploy-bearer-token"}

        # Missing or wrong principal token -> 401.
        r_no_ptok = client.post(
            "/api/studio/projects/proj-http/ibpc/sessions",
            headers=deploy_headers,
            json={"tenant_id": "ten-1"},
        )
        assert r_no_ptok.status_code == 401

        # VIEWER attempting to create session -> 403.
        r_viewer_open = client.post(
            "/api/studio/projects/proj-http/ibpc/sessions",
            headers={**deploy_headers, "X-Nexus-Principal-Token": "tok-viewer-123"},
            json={"tenant_id": "ten-1"},
        )
        assert r_viewer_open.status_code == 403

        # OWNER opens session -> 201, submits turn -> 201, approves -> 200.
        owner_headers = {**deploy_headers, "X-Nexus-Principal-Token": "tok-owner-123"}
        r_open = client.post(
            "/api/studio/projects/proj-http/ibpc/sessions",
            headers=owner_headers,
            json={"tenant_id": "ten-1"},
        )
        assert r_open.status_code == 201, r_open.text
        sid = r_open.json()["session_id"]

        # VIEWER can read session -> 200.
        r_viewer_read = client.get(
            f"/api/studio/projects/proj-http/ibpc/sessions/{sid}",
            headers={**deploy_headers, "X-Nexus-Principal-Token": "tok-viewer-123"},
        )
        assert r_viewer_read.status_code == 200

        r_turn = client.post(
            f"/api/studio/projects/proj-http/ibpc/sessions/{sid}/turns",
            headers=owner_headers,
            json={
                "turn_id": "turn-1",
                "sequence_number": 1,
                "text": "trim from 1 to 2 seconds",
                "asset_id": "clip",
                "source_path": str(clip),
                "idempotency_key": "http-idem-1",
            },
        )
        assert r_turn.status_code == 201, r_turn.text
        approval_id = r_turn.json()["approval_id"]

        # VIEWER cannot approve -> 403.
        r_viewer_appr = client.post(
            f"/api/studio/projects/proj-http/ibpc/sessions/{sid}/turns/turn-1/approve",
            headers={**deploy_headers, "X-Nexus-Principal-Token": "tok-viewer-123"},
            json={"approval_id": approval_id},
        )
        assert r_viewer_appr.status_code == 403

        r_appr = client.post(
            f"/api/studio/projects/proj-http/ibpc/sessions/{sid}/turns/turn-1/approve",
            headers=owner_headers,
            json={"approval_id": approval_id},
        )
        assert r_appr.status_code == 200, r_appr.text
        assert r_appr.json()["state"] == "APPROVED"

        # Consent grant + revoke + AUDITOR audit read over HTTP.
        r_grant = client.post(
            "/api/studio/projects/proj-http/ibpc/privacy/consents",
            headers=owner_headers,
            json={
                "mode": "TRUSTED_CLOUD",
                "processing_scope": "EXTERNAL_PROVIDER_DISPATCH",
                "data_category": "TRANSCRIPT",
                "provider_id": "gemini",
            },
        )
        assert r_grant.status_code == 201, r_grant.text
        consent_id = r_grant.json()["consent_id"]

        r_revoke = client.post(
            f"/api/studio/projects/proj-http/ibpc/privacy/consents/{consent_id}/revoke",
            headers=owner_headers,
            json={"reason": "test_revoke"},
        )
        assert r_revoke.status_code == 200

        r_audit = client.get(
            "/api/studio/projects/proj-http/ibpc/privacy/audit",
            headers={**deploy_headers, "X-Nexus-Principal-Token": "tok-auditor-123"},
        )
        assert r_audit.status_code == 200
        assert len(r_audit.json()["audit"]) >= 2

        # Close session revokes pending/approved bindings.
        r_close = client.post(
            f"/api/studio/projects/proj-http/ibpc/sessions/{sid}/close",
            headers=owner_headers,
        )
        assert r_close.status_code == 200
        assert r_close.json()["status"] == "CLOSED"


class TestWave3EdgeCasesAndCoverage:
    def test_session_store_and_manager_error_branches(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.session import (
            DuplicateTurnError,
            DurableLiveSessionStore,
            IdempotencyConflictError,
            LiveSessionManager,
            SessionClosedError,
            SessionEpochConflictError,
            SessionError,
            StaleRevisionError,
            StaleTurnError,
            TransitionError,
            VoiceTurn,
        )

        clock = _Clock()
        db = tmp_path / "sessions.sqlite3"
        privacy_db = tmp_path / "privacy.sqlite3"
        pstore = DurablePrivacyConsentStore(privacy_db)
        policy = PrivacyPolicy(
            now=clock,
            store=pstore,
            configured_providers=frozenset({"gemini"}),
        )
        store = DurableLiveSessionStore(db)
        mgr = LiveSessionManager(
            now=clock,
            privacy=policy,
            store=store,
        )
        try:
            # Non-existent session & turn paths.
            with pytest.raises(SessionError, match="unknown_session"):
                mgr.get_session("sess-missing")
            with pytest.raises(SessionError, match="unknown_session"):
                mgr.close_session("sess-missing")
            assert store.load_turn_context("appr-missing") is None

            sess = mgr.open_session(
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                privacy_mode=PrivacyMode.TRUSTED_CLOUD,
                consent_scope="transcript",
                current_revision=1,
            )
            # Unknown turn errors.
            with pytest.raises(SessionError, match="unknown_turn"):
                mgr.mark_executing(sess.session_id, "missing-turn")
            with pytest.raises(SessionError, match="unknown_turn"):
                mgr.cancel(sess.session_id, "missing-turn")
            with pytest.raises(SessionError, match="unknown_turn"):
                mgr.mark_failed(sess.session_id, "missing-turn")

            # Consent & egress check via manager.
            assert len(policy.consents) >= 1
            assert policy.has_consent(
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                scope="transcript",
                project_id="proj-1",
            )
            with pytest.raises(PrivacyViolation, match="no_provider_wired"):
                mgr.assert_no_egress(sess.session_id, "transcript")
            with pytest.raises(PrivacyViolation, match="consent_missing"):
                mgr.assert_no_egress(sess.session_id, "audio")
            rec = policy.record_consent(
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                scope="audio",
                project_id="proj-1",
            )
            assert rec.consent_id
            with pytest.raises(PrivacyViolation, match="no_provider_wired"):
                mgr.assert_no_egress(sess.session_id, "audio")
            mgr.set_privacy_mode(
                sess.session_id,
                PrivacyMode.LOCAL_VAULT,
                principal_id=OWNER_ID,
                consent_scope="local",
            )
            with pytest.raises(PrivacyViolation, match="egress_refused"):
                mgr.assert_no_egress(sess.session_id, "transcript")
            mgr.set_privacy_mode(
                sess.session_id,
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                consent_scope="transcript",
            )

            # Turn sequence, revision, epoch & idempotency guards.
            cur_epoch = mgr.get_session(sess.session_id).session_epoch
            t1 = mgr.begin_turn(
                sess.session_id,
                "t1",
                sequence_number=2,
                payload_digest="sha256:" + "1" * 64,
                project_revision=1,
                expected_epoch=cur_epoch,
                idempotency_key="idem-1",
            )
            # Same idempotency key & same digest replays cleanly.
            replay = mgr.begin_turn(
                sess.session_id,
                "t1-replay",
                sequence_number=2,
                payload_digest="sha256:" + "1" * 64,
                project_revision=1,
                idempotency_key="idem-1",
            )
            assert replay.turn_id == t1.turn_id
            # Same idempotency key & different digest -> IdempotencyConflictError.
            with pytest.raises(IdempotencyConflictError):
                mgr.begin_turn(
                    sess.session_id,
                    "t1-conflict",
                    sequence_number=3,
                    payload_digest="sha256:" + "2" * 64,
                    project_revision=1,
                    idempotency_key="idem-1",
                )
            # Duplicate turn id -> DuplicateTurnError.
            with pytest.raises(DuplicateTurnError):
                mgr.begin_turn(
                    sess.session_id,
                    "t1",
                    sequence_number=3,
                    payload_digest="sha256:" + "3" * 64,
                    project_revision=1,
                )
            # Stale sequence -> StaleTurnError.
            with pytest.raises(StaleTurnError):
                mgr.begin_turn(
                    sess.session_id,
                    "t2-stale-seq",
                    sequence_number=1,
                    payload_digest="sha256:" + "3" * 64,
                    project_revision=1,
                )
            # Stale revision -> StaleRevisionError.
            with pytest.raises(StaleRevisionError):
                mgr.begin_turn(
                    sess.session_id,
                    "t2-stale-rev",
                    sequence_number=3,
                    payload_digest="sha256:" + "3" * 64,
                    project_revision=99,
                )
            # Epoch conflict -> SessionEpochConflictError.
            with pytest.raises(SessionEpochConflictError):
                mgr.begin_turn(
                    sess.session_id,
                    "t2-bad-epoch",
                    sequence_number=3,
                    payload_digest="sha256:" + "3" * 64,
                    project_revision=1,
                    expected_epoch=99,
                )
            assert not mgr.turn_is_active(sess.session_id, "missing")
            assert mgr.turn_is_active(sess.session_id, "t1")
            mgr.pause_session(sess.session_id)
            assert not mgr.turn_is_active(sess.session_id, "t1")
            mgr.resume_session(sess.session_id)
            mgr.resume_session(sess.session_id)
            assert not mgr.turn_is_active(sess.session_id, "t1")
            t_resumed = mgr.begin_turn(
                sess.session_id,
                "t-resumed",
                sequence_number=3,
                payload_digest="sha256:" + "6" * 64,
                project_revision=1,
            )
            assert mgr.turn_is_active(sess.session_id, t_resumed.turn_id)
            mgr.advance_revision(sess.session_id, new_revision=2)
            with pytest.raises(StaleRevisionError):
                mgr.mark_awaiting_approval(sess.session_id, t_resumed.turn_id, approval_id="appr_1")

            # Direct store guard errors for unknown/closed session.
            fake_turn = VoiceTurn(
                session_id="missing",
                turn_id="tx",
                sequence_number=99,
                project_revision=2,
                payload_digest="sha256:" + "4" * 64,
            )
            from nexus_ai_agent.nagar.ibpc.session import LiveSession

            with pytest.raises(SessionError, match="unknown_session"):
                store.insert_turn_atomic(
                    session=LiveSession(
                        session_id="missing",
                        tenant_id="t",
                        project_id="p",
                        principal_id=OWNER_ID,
                        user_id=1,
                        chat_id=1,
                        privacy_mode=PrivacyMode.LOCAL_VAULT,
                        current_revision=2,
                        created_at=clock(),
                    ),
                    turn=fake_turn,
                    expected_epoch=0,
                    now_utc=clock(),
                )
            t2 = mgr.begin_turn(
                sess.session_id,
                "t2-run",
                sequence_number=4,
                payload_digest="sha256:" + "4" * 64,
                project_revision=2,
            )
            assert t2.turn_id == "t2-run"
            with pytest.raises(TransitionError):
                mgr.mark_executing(sess.session_id, "t2-run")
            mgr.mark_intent_parsed(sess.session_id, "t2-run", intent_digest="sha256:" + "a" * 64)
            mgr.mark_plan_preview(
                sess.session_id,
                "t2-run",
                shadow_preview_digest="sha256:" + "b" * 64,
            )
            mgr.mark_awaiting_approval(sess.session_id, "t2-run", approval_id="appr_run")
            mgr.mark_approved(sess.session_id, "t2-run")
            mgr.mark_executing(sess.session_id, "t2-run")
            with pytest.raises(TransitionError):
                mgr.cancel(sess.session_id, "t2-run")
            mgr.mark_failed(sess.session_id, "t2-run")
            with pytest.raises(TransitionError, match="terminal_turn"):
                mgr.interrupt(sess.session_id, "t2-run")

            # Pause/resume & close session idempotency & closed-session refusal.
            mgr.pause_session(sess.session_id)
            mgr.resume_session(sess.session_id)
            mgr.resume_session(sess.session_id)
            closed = mgr.close_session(sess.session_id)
            assert closed.status is SessionStatus.CLOSED
            closed_again = mgr.close_session(sess.session_id)
            assert closed_again.status is SessionStatus.CLOSED
            with pytest.raises(SessionClosedError):
                mgr.begin_turn(
                    sess.session_id,
                    "t-after-close",
                    sequence_number=5,
                    payload_digest="sha256:" + "5" * 64,
                    project_revision=2,
                )
        finally:
            store.close()
            pstore.close()

    def test_privacy_approval_models_and_authority_edge_branches(self, tmp_path: Path) -> None:
        from nexus_ai_agent.nagar.ibpc.approval import (
            ApprovalAlreadyConsumedError,
            ApprovalError,
            ApprovalIdentity,
            DurableApprovalLedger,
        )
        from nexus_ai_agent.nagar.ibpc.authority import (
            CanonicalAuthorityError,
            DirectoryAuthority,
        )
        from nexus_ai_agent.nagar.ibpc.models import (
            DurableConsentGrant,
            VideoTimeRange,
            VideoTimeScope,
        )

        # models.py edge cases.
        with pytest.raises(ValueError, match="empty range"):
            VideoTimeRange(start_us=10, end_us=10)
        with pytest.raises(ValueError):
            VideoTimeRange.model_validate({"start_us": float("nan"), "end_us": 100})
        full_spec = VideoTimeScope(
            asset_id="a",
            effect="keep",
            start_us=0,
            end_us=1_000_000,
        )
        assert full_spec.removed_ranges(1_000_000) == ()
        assert full_spec.forbidden_ranges(0) == ()
        mod_spec = VideoTimeScope(
            asset_id="a",
            effect="modify",
            start_us=200_000,
            end_us=500_000,
        )
        assert mod_spec.removed_ranges(1_000_000) == ()
        assert len(mod_spec.forbidden_ranges(1_000_000)) == 2
        assert full_spec.forbidden_ranges(100) != ()
        past_spec = VideoTimeScope(asset_id="a", effect="keep", start_us=500, end_us=900)
        assert past_spec.forbidden_ranges(100) == ()

        grant_m = DurableConsentGrant(
            consent_id="cns_1",
            project_id="p1",
            principal_id=OWNER_ID,
            mode=PrivacyMode.TRUSTED_CLOUD,
            processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value,
            data_category=DataCategory.TRANSCRIPT.value,
            granted_at_utc="2026-11-25T10:00:00Z",
        )
        assert grant_m.consent_digest.startswith("sha256:")

        # DirectoryAuthority malformed ref.
        class _FakeDir:
            def authorize(self, _actor, _project_id) -> bool:
                return True

            def require_binding(self, _ref, _principal) -> None:
                raise ValueError("bad binding ref")

        with pytest.raises(CanonicalAuthorityError, match="malformed_reference"):
            DirectoryAuthority(_FakeDir()).resolve_principal(  # type: ignore[arg-type]
                principal_id=OWNER_ID,
                project_id="p1",
                binding_ref="not-a-cbn",
            )

        # PrivacyPolicy unconfigured & invalid branches.
        clock = _Clock()
        no_store_policy = PrivacyPolicy(now=clock)
        with pytest.raises(PrivacyViolation, match="store_unconfigured"):
            no_store_policy.grant_durable_consent(
                project_id="p1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                data_category=DataCategory.TRANSCRIPT,
            )
        with pytest.raises(PrivacyViolation, match="store_unconfigured"):
            no_store_policy.revoke_durable_consent("c1", actor_id=OWNER_ID)
        with pytest.raises(PrivacyViolation, match="invalid_scope"):
            no_store_policy.enforce_processing_consent(
                project_id="p1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.LOCAL_VAULT,
                processing_scope="BAD_SCOPE",
                data_category=DataCategory.TRANSCRIPT,
            )
        with pytest.raises(PrivacyViolation, match="invalid_category"):
            no_store_policy.enforce_processing_consent(
                project_id="p1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.LOCAL_VAULT,
                processing_scope=ProcessingScope.LOCAL_PROCESSING,
                data_category="BAD_CAT",
            )
        with pytest.raises(PrivacyViolation, match="consent_missing"):
            no_store_policy.enforce_processing_consent(
                project_id="p1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.LOCAL_VAULT,
                processing_scope=ProcessingScope.LOCAL_PROCESSING,
                data_category=DataCategory.BIOMETRIC_FACE_ROI,
            )

        pstore = DurablePrivacyConsentStore(tmp_path / "priv.sqlite3")
        policy = PrivacyPolicy(
            now=clock,
            store=pstore,
            configured_providers=frozenset({"gemini"}),
        )
        try:
            with pytest.raises(PrivacyViolation, match="invalid_scope"):
                policy.grant_durable_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope="BAD",
                    data_category=DataCategory.TRANSCRIPT,
                )
            with pytest.raises(PrivacyViolation, match="invalid_category"):
                policy.grant_durable_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.LOCAL_PROCESSING,
                    data_category="BAD",
                )
            # Local vault external dispatch emits DENIED audit.
            with pytest.raises(PrivacyViolation, match="egress_refused"):
                policy.enforce_processing_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.LOCAL_VAULT,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    provider_id="gemini",
                )
            # Unconfigured provider emits DENIED audit with no_provider_wired.
            policy.grant_durable_consent(
                project_id="p1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                data_category=DataCategory.TRANSCRIPT,
                provider_id="other_provider",
            )
            with pytest.raises(PrivacyViolation, match="no_provider_wired"):
                policy.enforce_processing_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    provider_id="other_provider",
                )
            assert len(pstore.list_consents(project_id="p1", principal_id=OWNER_ID)) >= 1
            assert len(pstore.list_audit(project_id="p1", principal_id=OWNER_ID)) >= 2
            assert len(pstore.list_audit()) >= 2
        finally:
            pstore.close()

        from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding

        # ApprovalLedger identity/timestamp/reservation/consume edges.
        ledger = DurableApprovalLedger(tmp_path / "approvals.sqlite3", now=clock)
        try:
            base_id = ApprovalIdentity(
                session_id="s1",
                turn_id="t1",
                tenant_id="ten",
                project_id="p1",
                principal_id=OWNER_ID,
                sequence_number=1,
                source_revision=0,
                source_asset_digest="sha256:" + "0" * 64,
                ast_digest="sha256:" + "1" * 64,
                plan_digest="sha256:" + "2" * 64,
                preview_digest="sha256:" + "3" * 64,
                revision_id="rev-0",
                scope_signature="sig-0",
                expires_at_utc="2026-11-25T10:10:00Z",
            )
            assert base_id.identity_digest().startswith("sha256:")

            def _make_binding(aid: str, ident: ApprovalIdentity) -> ApprovalBinding:
                return ApprovalBinding(
                    approval_id=aid,
                    session_id=ident.session_id,
                    turn_id=ident.turn_id,
                    tenant_id=ident.tenant_id,
                    project_id=ident.project_id,
                    principal_id=ident.principal_id,
                    sequence_number=ident.sequence_number,
                    source_revision=ident.source_revision,
                    source_asset_digest=ident.source_asset_digest,
                    ast_digest=ident.ast_digest,
                    plan_digest=ident.plan_digest,
                    preview_digest=ident.preview_digest,
                    revision_id=ident.revision_id or "",
                    scope_signature=ident.scope_signature or "",
                    expires_at_utc=ident.expires_at_utc or "",
                    created_at=clock(),
                )

            b = ledger.create(_make_binding("appr_" + "a" * 32, base_id))
            assert (
                ledger.approve(
                    b.approval_id, identity=base_id, turn_is_active=lambda: True
                ).state.value
                == "APPROVED"
            )

            # Mismatches on revision_id, expires_at_utc, principal_id, project_id, plan, preview.
            for field, val, err in [
                ("revision_id", "rev-other", "revision_id_mismatch"),
                ("expires_at_utc", "2026-11-25T10:20:00Z", "expires_at_mismatch"),
                ("principal_id", VIEWER_ID, "identity_mismatch_principal_id"),
                ("project_id", "p2", "identity_mismatch_project_id"),
                ("plan_digest", "sha256:" + "9" * 64, "identity_mismatch_plan_digest"),
                ("preview_digest", "sha256:" + "8" * 64, "identity_mismatch_preview_digest"),
            ]:
                bad = ApprovalIdentity(**{**base_id.__dict__, field: val})
                with pytest.raises(ApprovalError, match=err):
                    ledger.consume(b.approval_id, identity=bad)

            # Invalid timestamps.
            bad_ts_id = ApprovalIdentity(
                **{**base_id.__dict__, "expires_at_utc": "not-a-timestamp"}
            )
            b_bad_ts = ledger.create(_make_binding("appr_" + "b" * 32, bad_ts_id))
            with pytest.raises(ApprovalError, match="invalid_timestamp"):
                ledger.approve(
                    b_bad_ts.approval_id, identity=bad_ts_id, turn_is_active=lambda: True
                )

            naive_id = ApprovalIdentity(
                **{**base_id.__dict__, "expires_at_utc": "2026-11-25T10:10:00"}
            )
            b_naive = ledger.create(_make_binding("appr_" + "c" * 32, naive_id))
            with pytest.raises(ApprovalError, match="invalid_timestamp"):
                ledger.approve(b_naive.approval_id, identity=naive_id, turn_is_active=lambda: True)

            # Reservation + finalize_consumption + ApprovalAlreadyConsumedError + revoke_session.
            b_res = ledger.create(_make_binding("appr_" + "d" * 32, base_id))
            ledger.approve(b_res.approval_id, identity=base_id, turn_is_active=lambda: True)
            with pytest.raises(ApprovalError, match="invalid_job_id"):
                ledger.consume(b_res.approval_id, identity=base_id, job_id="")
            with pytest.raises(ApprovalError, match="invalid_attempt"):
                ledger.consume(
                    b_res.approval_id,
                    identity=base_id,
                    job_id="j1",
                    attempt=0,
                )
            ledger.consume(
                b_res.approval_id,
                identity=base_id,
                job_id="j1",
                attempt=1,
            )
            ledger.assert_active_reservation(
                b_res.approval_id, identity=base_id, job_id="j1", attempt=1
            )
            with pytest.raises(ApprovalError, match="stale_attempt"):
                ledger.assert_active_reservation(
                    b_res.approval_id, identity=base_id, job_id="j1", attempt=2
                )
            ledger.finalize_consumption(b_res.approval_id, job_id="j1", attempt=1)
            with pytest.raises(ApprovalError, match="replay"):
                ledger.consume(
                    b_res.approval_id,
                    identity=base_id,
                    job_id="j1",
                )
            assert ApprovalAlreadyConsumedError("test").reason == "already_consumed"

            b_pending = ledger.create(_make_binding("appr_" + "e" * 32, base_id))
            revoked = ledger.revoke_session("s1")
            assert any(x.approval_id == b_pending.approval_id for x in revoked)
        finally:
            ledger.close()

    def test_studio_principal_auth_and_http_error_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.adapters.studio_principal_auth import (
            StudioPrincipalAuthStore,
            build_studio_ibpc_service,
        )
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings
        from nexus_ai_agent.nagar.ibpc.authority import (
            CanonicalAuthorityError,
            StudioAction,
            StudioAuthorizationError,
            StudioRole,
        )

        clock = _Clock()
        auth_db = tmp_path / "auth.sqlite3"
        store = StudioPrincipalAuthStore(auth_db, now=clock)
        try:
            with pytest.raises(ValueError, match="raw_token"):
                store.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="short")
            with pytest.raises(PrincipalAuthenticationError, match="missing_principal_token"):
                store.authenticate_token(None)
            with pytest.raises(StudioAuthorizationError, match="principal_not_found"):
                store.authorize_action(
                    principal_id="prn_" + "9" * 32,
                    project_id="p1",
                    action=StudioAction.READ_SESSION,
                )
            store.ensure_project("p1", tenant_id="ten-1")
            store.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-1234")
            bref = store.bind_channel(OWNER_ID, channel="studio", external_subject="sub-o")
            store.grant_membership("p1", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

            with pytest.raises(CanonicalAuthorityError, match="missing_binding"):
                store.resolve_principal(principal_id=OWNER_ID, project_id="p1", binding_ref="")
            with pytest.raises(CanonicalAuthorityError, match="malformed_principal"):
                store.resolve_principal(
                    principal_id="bad-principal", project_id="p1", binding_ref=bref
                )
            with pytest.raises(CanonicalAuthorityError, match="authorization_denied"):
                store.resolve_principal(
                    principal_id=OWNER_ID, project_id="other-proj", binding_ref=bref
                )
            assert len(store.list_auth_audit(project_id="p1")) >= 1
            assert len(store.list_auth_audit()) >= 1

            # Trigger protects append-only auth audit & tests rollback.
            with pytest.raises(sqlite3.DatabaseError):
                with store._transaction():
                    store._conn.execute("DELETE FROM nexus_studio_auth_audit")

            store.revoke_principal(OWNER_ID)
            with pytest.raises(StudioAuthorizationError, match="principal_revoked"):
                store.authorize_action(
                    principal_id=OWNER_ID,
                    project_id="p1",
                    action=StudioAction.READ_SESSION,
                )
        finally:
            store.close()

        # Default build_studio_ibpc_service creates queue/studio_auth.sqlite3.
        ws = tmp_path / "ws_default"
        ws.mkdir()

        class _StubQ:
            async def enqueue(self, **_kw):
                return "j1"

            async def get_status(self, _jid):
                return None

            async def get_result(self, _jid):
                return None

        svc_def = build_studio_ibpc_service(
            queue=_StubQ(),
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1"),
            now=clock,
        )
        svc_def.close()
        assert (ws / "queue" / "studio_auth.sqlite3").exists()

        # HTTP error branches + _enforce_optional_principal_rbac on existing /api/studio routes.
        _stub_media_tools(monkeypatch)
        clip = tmp_path / "clip.mp4"
        _fake_media(clip)
        http_auth_db = tmp_path / "http_auth.sqlite3"
        http_ws = tmp_path / "http_ws"
        http_ws.mkdir()
        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-err")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(http_ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(http_auth_db))
        monkeypatch.setenv("NEXUS_STUDIO_ACTIVE_REVISION", "0")
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "http_q.sqlite3"))
        monkeypatch.setenv("NEXUS_STUDIO_REQUIRE_PRINCIPAL", "1")
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        s = StudioPrincipalAuthStore(http_auth_db, now=clock)
        s.ensure_project("proj-err", tenant_id="ten-1")
        s.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-err")
        s.bind_channel(OWNER_ID, channel="studio", external_subject="sub-owner")
        s.grant_membership("proj-err", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        s.register_principal(VIEWER_ID, tenant_id="ten-1", raw_token="tok-viewer-err")
        s.bind_channel(VIEWER_ID, channel="studio", external_subject="sub-viewer")
        s.grant_membership("proj-err", VIEWER_ID, role=StudioRole.VIEWER, granted_by=OWNER_ID)
        s.register_principal(EDITOR_ID, tenant_id="ten-1", raw_token="tok-editor-err")
        s.close()

        client = TestClient(app_module.app)
        deploy_h = {"Authorization": "Bearer deploy-tok"}
        owner_h = {**deploy_h, "X-Nexus-Principal-Token": "tok-owner-err"}
        viewer_h = {**deploy_h, "X-Nexus-Principal-Token": "tok-viewer-err"}

        # Existing /api/studio routes enforce NEXUS_STUDIO_REQUIRE_PRINCIPAL=1.
        assert (
            client.get(
                "/api/studio/projects/proj-err/jobs/job-1/state", headers=deploy_h
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/studio/projects/proj-err/renders",
                headers=viewer_h,
                json={"input_path": str(clip), "start_s": 0.0, "end_s": 1.0},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/api/studio/projects/proj-err/jobs/job-1/state", headers=viewer_h
            ).status_code
            == 404
        )

        # studio_ibpc 404 / 409 / 400 / 201 paths.
        assert (
            client.get(
                "/api/studio/projects/proj-err/ibpc/sessions/missing", headers=owner_h
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/studio/projects/proj-err/ibpc/sessions/missing/close", headers=owner_h
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/studio/projects/proj-err/ibpc/sessions/missing/turns",
                headers=owner_h,
                json={
                    "turn_id": "t1",
                    "sequence_number": 1,
                    "text": "trim from 1 to 2 seconds",
                    "asset_id": "clip",
                    "source_path": str(clip),
                },
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/studio/projects/proj-err/ibpc/sessions/missing/turns/t1/approve",
                headers=owner_h,
                json={"approval_id": "appr_missing"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/studio/projects/proj-err/ibpc/sessions/missing/turns/t1/execute",
                headers=owner_h,
                json={"approval_id": "appr_missing"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/studio/projects/proj-err/ibpc/privacy/consents",
                headers=owner_h,
                json={
                    "mode": "TRUSTED_CLOUD",
                    "processing_scope": "BAD_SCOPE",
                    "data_category": "TRANSCRIPT",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/studio/projects/proj-err/ibpc/privacy/consents/missing/revoke",
                headers=owner_h,
                json={"reason": "test"},
            ).status_code
            == 404
        )
        r_mem = client.post(
            "/api/studio/projects/proj-err/memberships",
            headers=owner_h,
            json={"principal_id": EDITOR_ID, "role": "EDITOR"},
        )
        assert r_mem.status_code == 201

        # studio_ibpc extra branches: invalid project_id (400), TRUSTED_CLOUD without consent (403),
        # cross-project session get (404), ambiguous turn (422), idempotency conflict (409),
        # execute 200 + replay 409, grant_project_consent 400, _workspace_root default.
        from nexus_ai_agent.api.studio_ibpc import _studio_workspace_root

        monkeypatch.delenv("NEXUS_STUDIO_WORKSPACE_ROOT", raising=False)
        assert _studio_workspace_root() == Path("workspace")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(http_ws))

        assert (
            client.post(
                "/api/studio/projects/bad$proj/ibpc/sessions",
                headers=owner_h,
                json={"tenant_id": "ten-1"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/studio/projects/proj-err/ibpc/sessions",
                headers=owner_h,
                json={"tenant_id": "ten-1", "privacy_mode": "TRUSTED_CLOUD"},
            ).status_code
            == 403
        )
        # Open session in proj-err, verify cross-project get in proj-other -> 404.
        s.ensure_project("proj-other", tenant_id="ten-1") if False else None
        s2 = StudioPrincipalAuthStore(http_auth_db, now=clock)
        s2.ensure_project("proj-other", tenant_id="ten-1")
        s2.grant_membership("proj-other", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        s2.close()
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-err,proj-other")
        r_s = client.post(
            "/api/studio/projects/proj-err/ibpc/sessions",
            headers=owner_h,
            json={"tenant_id": "ten-1"},
        )
        assert r_s.status_code == 201
        sid_err = r_s.json()["session_id"]
        assert (
            client.get(
                f"/api/studio/projects/proj-other/ibpc/sessions/{sid_err}", headers=owner_h
            ).status_code
            == 404
        )
        # Ambiguous intent -> 422.
        assert (
            client.post(
                f"/api/studio/projects/proj-err/ibpc/sessions/{sid_err}/turns",
                headers=owner_h,
                json={
                    "turn_id": "t-ambig",
                    "sequence_number": 1,
                    "text": "trim",
                    "asset_id": "clip",
                    "source_path": str(clip),
                },
            ).status_code
            == 422
        )
        # Idempotency conflict -> 409.
        r_t1 = client.post(
            f"/api/studio/projects/proj-err/ibpc/sessions/{sid_err}/turns",
            headers=owner_h,
            json={
                "turn_id": "t-ok",
                "sequence_number": 2,
                "text": "trim from 1 to 2 seconds",
                "asset_id": "clip",
                "source_path": str(clip),
                "idempotency_key": "k-idem",
            },
        )
        assert r_t1.status_code == 201
        assert (
            client.post(
                f"/api/studio/projects/proj-err/ibpc/sessions/{sid_err}/turns",
                headers=owner_h,
                json={
                    "turn_id": "t-conflict",
                    "sequence_number": 3,
                    "text": "trim from 0 to 1 seconds",
                    "asset_id": "clip",
                    "source_path": str(clip),
                    "idempotency_key": "k-idem",
                },
            ).status_code
            == 409
        )
        # grant_project_consent raising PrivacyViolation -> 400.
        with monkeypatch.context() as mp:
            mp.setattr(
                service_mod.IBPCService,
                "grant_privacy_consent",
                lambda *_a, **_kw: (_ for _ in ()).throw(
                    PrivacyViolation("bad_consent", "invalid consent request")
                ),
            )
            assert (
                client.post(
                    "/api/studio/projects/proj-err/ibpc/privacy/consents",
                    headers=owner_h,
                    json={
                        "mode": "TRUSTED_CLOUD",
                        "processing_scope": "EXTERNAL_PROVIDER_DISPATCH",
                        "data_category": "TRANSCRIPT",
                    },
                ).status_code
                == 400
            )
        # execute_ibpc_turn returning 200 and StudioAuthorizationError -> 403.
        from nexus_ai_agent.nagar.ibpc.service import ExecutionOutcome

        with monkeypatch.context() as mp:

            async def _fake_exec(*_a, **_kw):
                return ExecutionOutcome(status="executed", job_id="job-http-1", detail="ok")

            mp.setattr(service_mod.IBPCService, "execute", _fake_exec)
            r_ex = client.post(
                f"/api/studio/projects/proj-err/ibpc/sessions/{sid_err}/turns/t-ok/execute",
                headers=owner_h,
                json={"approval_id": r_t1.json()["approval_id"]},
            )
            assert r_ex.status_code == 200
            assert r_ex.json()["job_id"] == "job-http-1"

        with monkeypatch.context() as mp:

            async def _fake_exec_auth(*_a, **_kw):
                from nexus_ai_agent.nagar.ibpc.authority import StudioAuthorizationDecision

                raise StudioAuthorizationError(
                    StudioAuthorizationDecision(
                        allowed=False,
                        principal_id=OWNER_ID,
                        project_id="proj-err",
                        role="OWNER",
                        action="EXECUTE_PLAN",
                        reason="denied",
                        decided_at_utc=clock(),
                    )
                )

            mp.setattr(service_mod.IBPCService, "execute", _fake_exec_auth)
            assert (
                client.post(
                    f"/api/studio/projects/proj-err/ibpc/sessions/{sid_err}/turns/t-ok/execute",
                    headers=owner_h,
                    json={"approval_id": r_t1.json()["approval_id"]},
                ).status_code
                == 403
            )

        # IBPCService(durable=True) creates & closes owned SQLite stores when uninjected.
        owned_ws = tmp_path / "owned_ws"
        owned_ws.mkdir()
        svc_owned = service_mod.IBPCService(
            service_mod.IBPCServiceDeps(
                queue=_StubQ(),
                registry=build_runtime_registry(),
                workspace_root=owned_ws,
                revision_provider=lambda: 0,
                durable=True,
                now=clock,
            )
        )
        svc_owned.close()
        from nexus_ai_agent.nagar.ibpc.approval import durable_approval_ledger_path
        from nexus_ai_agent.nagar.ibpc.privacy import durable_privacy_consent_path
        from nexus_ai_agent.nagar.ibpc.session import durable_session_store_path

        assert durable_session_store_path(owned_ws).exists()
        assert durable_approval_ledger_path(owned_ws).exists()
        assert durable_privacy_consent_path(owned_ws).exists()

        get_settings.cache_clear()

    def test_wave3_ibpc_voice_turn_and_edge_branches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.nagar.ibpc.approval import (
            ApprovalBinding,
            ApprovalIdentity,
            DurableApprovalLedger,
        )
        from nexus_ai_agent.nagar.ibpc.models import PrivacyMode
        from nexus_ai_agent.nagar.ibpc.privacy import (
            DataCategory,
            DurablePrivacyConsentStore,
            PrivacyPolicy,
            ProcessingScope,
        )
        from nexus_ai_agent.nagar.ibpc.session import (
            DuplicateTurnError,
            DurableLiveSessionStore,
            IdempotencyConflictError,
            LiveSession,
            SessionClosedError,
            SessionEpochConflictError,
            SessionStatus,
            StaleRevisionError,
            StaleTurnError,
            VoiceTurn,
        )

        _stub_media_tools(monkeypatch)
        clock = _Clock()
        clip = tmp_path / "clip.mp4"
        _fake_media(clip)

        # 1. Privacy store validation & local biometric consent branch.
        pstore = DurablePrivacyConsentStore(tmp_path / "priv.sqlite3")
        try:
            with pytest.raises(PrivacyViolation, match="invalid_timestamp"):
                pstore.grant_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    granted_at_utc="",
                )
            with pytest.raises(PrivacyViolation, match="invalid_timestamp"):
                pstore.grant_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    granted_at_utc="not-a-date",
                )
            with pytest.raises(PrivacyViolation, match="invalid_timestamp"):
                pstore.grant_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    granted_at_utc="2026-11-26T00:00:00",
                )
            with pytest.raises(PrivacyViolation, match="invalid_mode"):
                pstore.grant_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode="BAD_MODE",  # type: ignore[arg-type]
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    granted_at_utc=clock(),
                )
            with pytest.raises(PrivacyViolation, match="invalid_identity"):
                pstore.grant_consent(
                    project_id="",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    granted_at_utc=clock(),
                )
            with pytest.raises(PrivacyViolation, match="invalid_expiry"):
                pstore.grant_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    granted_at_utc="2026-11-26T00:10:00Z",
                    expires_at_utc="2026-11-26T00:05:00Z",
                )
            with pstore._transaction():
                with pstore._transaction():
                    g = pstore.grant_consent(
                        project_id="p1",
                        principal_id=OWNER_ID,
                        mode=PrivacyMode.TRUSTED_CLOUD,
                        processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                        data_category=DataCategory.TRANSCRIPT,
                        granted_at_utc=clock(),
                    )
            rev1 = pstore.revoke_consent(g.consent_id, revoked_at_utc=clock(), revoked_by=OWNER_ID)
            rev2 = pstore.revoke_consent(g.consent_id, revoked_at_utc=clock(), revoked_by=OWNER_ID)
            assert rev1.consent_id == rev2.consent_id
            with pytest.raises(PrivacyViolation, match="unknown_consent"):
                pstore.revoke_consent(
                    "consent_missing", revoked_at_utc=clock(), revoked_by=OWNER_ID
                )

            fw = PrivacyPolicy(store=pstore, now=clock)
            with pytest.raises(PrivacyViolation, match="consent_missing"):
                fw.enforce_processing_consent(
                    project_id="p1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.LOCAL_VAULT,
                    processing_scope=ProcessingScope.LOCAL_PROCESSING,
                    data_category=DataCategory.BIOMETRIC_FACE_ROI,
                    require_local_biometric_consent=True,
                )
        finally:
            pstore.close()

        # 2. Approval identity mismatches (principal_id, project_id, plan_digest, preview_digest).
        ledger = DurableApprovalLedger(tmp_path / "appr.sqlite3", now=clock)
        try:
            d1 = "sha256:" + "1" * 64
            d2 = "sha256:" + "2" * 64
            d3 = "sha256:" + "3" * 64
            d4 = "sha256:" + "4" * 64
            d5 = "sha256:" + "5" * 64
            exp = "2026-11-26T01:00:00Z"
            ahash = compute_canonical_approval_hash(
                project_id="p1",
                revision_id="rev-0",
                plan_digest=d2,
                preview_digest=d3,
                scope_signature=d5,
                principal_id=OWNER_ID,
                expires_at_utc=exp,
            )
            b = ledger.create(
                ApprovalBinding(
                    session_id="s1",
                    turn_id="t1",
                    sequence_number=1,
                    tenant_id="ten-1",
                    project_id="p1",
                    principal_id=OWNER_ID,
                    source_revision=0,
                    source_asset_digest=d4,
                    ast_digest=d1,
                    plan_digest=d2,
                    preview_digest=d3,
                    revision_id="rev-0",
                    scope_signature=d5,
                    expires_at_utc=exp,
                    approval_hash=ahash,
                )
            )
            base_kw: dict[str, Any] = dict(
                session_id="s1",
                turn_id="t1",
                sequence_number=1,
                tenant_id="ten-1",
                project_id="p1",
                principal_id=OWNER_ID,
                source_revision=0,
                source_asset_digest=d4,
                ast_digest=d1,
                plan_digest=d2,
                preview_digest=d3,
                revision_id="rev-0",
                scope_signature=d5,
                expires_at_utc=exp,
            )
            for field, bad_val, err_code in [
                ("principal_id", EDITOR_ID, "identity_mismatch_principal_id"),
                ("project_id", "p2", "identity_mismatch_project_id"),
                ("plan_digest", "sha256:" + "9" * 64, "identity_mismatch_plan_digest"),
                ("preview_digest", "sha256:" + "9" * 64, "identity_mismatch_preview_digest"),
            ]:
                kw = dict(base_kw)
                kw[field] = bad_val
                with pytest.raises(ApprovalError, match=err_code):
                    ledger.approve(
                        b.approval_id,
                        identity=ApprovalIdentity(**kw),
                        turn_is_active=lambda: True,
                    )
        finally:
            ledger.close()

        # 3. DurableLiveSessionStore insert_turn_atomic error paths.
        sstore = DurableLiveSessionStore(tmp_path / "sess.sqlite3")
        try:
            sess = LiveSession(
                session_id="s-atomic",
                tenant_id="ten-1",
                project_id="p1",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                privacy_mode=PrivacyMode.LOCAL_VAULT,
                current_revision=0,
                active_revision_id="rev-0",
                created_at=clock(),
            )
            sstore.create_session(sess, now_utc=clock())
            t1 = VoiceTurn(
                session_id="s-atomic",
                turn_id="t1",
                sequence_number=1,
                project_revision=0,
                target_revision_id="rev-0",
                idempotency_key="k1",
                payload_digest="sha256:" + "a" * 64,
            )
            sstore.insert_turn_atomic(
                session=sess, turn=t1, expected_epoch=sess.session_epoch, now_utc=clock()
            )
            # Duplicate turn_id with different idempotency key -> DuplicateTurnError
            with pytest.raises(DuplicateTurnError):
                sstore.insert_turn_atomic(
                    session=sess,
                    turn=VoiceTurn(
                        session_id="s-atomic",
                        turn_id="t1",
                        sequence_number=2,
                        project_revision=0,
                        target_revision_id="rev-0",
                    ),
                    expected_epoch=sess.session_epoch,
                    now_utc=clock(),
                )
            # Idempotency conflict -> IdempotencyConflictError
            with pytest.raises(IdempotencyConflictError):
                sstore.insert_turn_atomic(
                    session=sess,
                    turn=VoiceTurn(
                        session_id="s-atomic",
                        turn_id="t-conflict",
                        sequence_number=2,
                        project_revision=0,
                        target_revision_id="rev-0",
                        idempotency_key="k1",
                        payload_digest="sha256:" + "b" * 64,
                    ),
                    expected_epoch=sess.session_epoch,
                    now_utc=clock(),
                )
            # Out of order -> StaleTurnError
            with pytest.raises(StaleTurnError):
                sstore.insert_turn_atomic(
                    session=sess,
                    turn=VoiceTurn(
                        session_id="s-atomic",
                        turn_id="t-ooo",
                        sequence_number=1,
                        project_revision=0,
                        target_revision_id="rev-0",
                    ),
                    expected_epoch=sess.session_epoch,
                    now_utc=clock(),
                )
            # Stale revision -> StaleRevisionError
            with pytest.raises(StaleRevisionError):
                sstore.insert_turn_atomic(
                    session=sess,
                    turn=VoiceTurn(
                        session_id="s-atomic",
                        turn_id="t-rev",
                        sequence_number=2,
                        project_revision=1,
                        target_revision_id="rev-1",
                    ),
                    expected_epoch=sess.session_epoch,
                    now_utc=clock(),
                )
            # Epoch conflict -> SessionEpochConflictError
            with pytest.raises(SessionEpochConflictError):
                sstore.insert_turn_atomic(
                    session=sess,
                    turn=VoiceTurn(
                        session_id="s-atomic",
                        turn_id="t-epoch",
                        sequence_number=2,
                        project_revision=0,
                        target_revision_id="rev-0",
                    ),
                    expected_epoch=99,
                    now_utc=clock(),
                )
            # Closed session -> SessionClosedError + update_session_cas stale epoch
            with pytest.raises(SessionEpochConflictError):
                sstore.update_session_cas(sess, expected_epoch=999, now_utc=clock())
            sess.status = SessionStatus.CLOSED
            sstore.update_session_cas(sess, expected_epoch=sess.session_epoch, now_utc=clock())
            with pytest.raises(SessionClosedError):
                sstore.insert_turn_atomic(
                    session=sess,
                    turn=VoiceTurn(
                        session_id="s-atomic",
                        turn_id="t-closed",
                        sequence_number=2,
                        project_revision=0,
                        target_revision_id="rev-0",
                    ),
                    expected_epoch=sess.session_epoch,
                    now_utc=clock(),
                )
        finally:
            sstore.close()

        # 4. IBPCService submit_voice_turn, idempotency replay, _hydrate_active_contexts skip
        # branches, get_privacy_audit without store, and _approval_expiry naive timestamp.
        import asyncio
        import dataclasses
        from types import SimpleNamespace

        class _FakeCaptionEngine:
            def is_available(self) -> bool:
                return True

            async def transcribe(self, *_a, **_kw) -> Any:
                return SimpleNamespace(text="trim from 1 to 2 seconds")

        class _StubQ:
            async def enqueue(self, **_kw):
                return "j1"

            async def get_status(self, _jid):
                from nexus_ai_agent.application.ports.job_queue import JobStatus

                return JobStatus.FAILED_TERMINAL

            async def get_result(self, _jid):
                return None

        mem_svc = service_mod.IBPCService(
            service_mod.IBPCServiceDeps(
                queue=_StubQ(),
                registry=build_runtime_registry(),
                workspace_root=tmp_path / "mem_ws",
                revision_provider=lambda: 0,
                durable=False,
                now=clock,
            )
        )
        assert mem_svc.list_privacy_audit(project_id="p1", principal_id=OWNER_ID) == ()
        with pytest.raises(ValueError, match="timezone-aware"):
            mem_svc._approval_expiry("2026-11-26T00:00:00")

        voice_ws = tmp_path / "voice_ws"
        voice_ws.mkdir()
        auth, bref = _setup_auth_store(voice_ws / "auth.sqlite3", now=clock)
        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")
        svc = build_studio_ibpc_service(
            queue=_StubQ(),
            registry=build_runtime_registry(),
            workspace_root=voice_ws,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth,
            channel_binding_ref=bref,
            now=clock,
        )
        svc._deps = dataclasses.replace(svc._deps, caption_engine=_FakeCaptionEngine())  # type: ignore[misc]
        try:
            sid_v = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
            )
            r_txt1 = svc.submit_text_intent(
                sid_v,
                "t-txt",
                text="trim from 1 to 2 seconds",
                sequence_number=1,
                asset_id="clip",
                source_path=str(clip),
                idempotency_key="txt-key-1",
            )
            r_txt2 = svc.submit_text_intent(
                sid_v,
                "t-txt-dup",
                text="trim from 1 to 2 seconds",
                sequence_number=2,
                asset_id="clip",
                source_path=str(clip),
                idempotency_key="txt-key-1",
            )
            assert isinstance(r_txt1, PipelineResult) and isinstance(r_txt2, PipelineResult)
            assert r_txt2.approval_id == r_txt1.approval_id

            # submit_voice_intent fresh + idempotent replay
            r_v1 = asyncio.run(
                svc.submit_voice_intent(
                    sid_v,
                    "t-voice",
                    audio=b"RIFF\x00\x00\x00\x00WAVE",
                    sequence_number=2,
                    asset_id="clip",
                    source_path=str(clip),
                    idempotency_key="voice-key-1",
                )
            )
            assert isinstance(r_v1, PipelineResult)
            r_v2 = asyncio.run(
                svc.submit_voice_intent(
                    sid_v,
                    "t-voice-dup",
                    audio=b"RIFF\x00\x00\x00\x00WAVE",
                    sequence_number=3,
                    asset_id="clip",
                    source_path=str(clip),
                    idempotency_key="voice-key-1",
                )
            )
            assert isinstance(r_v2, PipelineResult)
            assert r_v2.approval_id == r_v1.approval_id

            # Save an orphaned context row + revoke an approval, then restart for hydration checks.
            assert svc._session_store is not None
            svc._session_store.save_turn_context(
                approval_id="appr_orphan",
                session_id=sid_v,
                turn_id="t-txt",
                context_payload={"broken": True},
                now_utc=clock(),
            )
            svc.approvals.revoke(r_txt1.approval_id)
        finally:
            svc.close()
            auth.close()

        auth2 = StudioPrincipalAuthStore(voice_ws / "auth.sqlite3", now=clock)
        svc2 = build_studio_ibpc_service(
            queue=_StubQ(),
            registry=build_runtime_registry(),
            workspace_root=voice_ws,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth2,
            channel_binding_ref=bref,
            now=clock,
        )
        try:
            assert "appr_orphan" not in svc2._contexts
            assert r_txt1.approval_id not in svc2._contexts
            assert r_v1.approval_id in svc2._contexts
            svc2._contexts.pop(r_v1.approval_id)
            assert svc2._load_or_get_context(r_v1.approval_id) is not None
            assert svc2._load_or_get_context("appr_missing") is None
            svc2.approve(sid_v, "t-voice", r_v1.approval_id, principal_id=OWNER_ID)
            auth2.grant_membership("proj-w3", OWNER_ID, role=StudioRole.VIEWER, granted_by=OWNER_ID)
            with pytest.raises(StudioAuthorizationError, match="insufficient_role"):
                asyncio.run(svc2.execute(sid_v, "t-voice", r_v1.approval_id, principal_id=OWNER_ID))
        finally:
            svc2.close()
            auth2.close()
