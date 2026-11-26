"""Wave 3 comprehensive tests: durable live IBPC sessions/turns, canonical
single-use approvals, durable revocable privacy consent, and Studio RBAC."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
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
    AuthorizationError,
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
        allow_system_bootstrap=True,
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
        ws = tmp_path / "ws"
        ws.mkdir()
        clip = ws / "clip.mp4"
        _fake_media(clip)
        auth_store, bref = _setup_auth_store(tmp_path / "auth.sqlite3", now=clock)
        queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")
        service = build_studio_ibpc_service(
            queue=queue,
            registry=build_runtime_registry(),
            workspace_root=ws,
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
        ws = tmp_path / "ws"
        ws.mkdir()
        clip = ws / "clip.mp4"
        _fake_media(clip)
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
                store.bind_channel(
                    pid,
                    channel="studio",
                    external_subject=f"sub-{pid}",
                    allow_system_bootstrap=True,
                )
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
            store.revoke_channel_binding(bref_viewer, allow_system_bootstrap=True)
            with pytest.raises(
                StudioAuthorizationError, match="invalid_or_revoked_channel_binding"
            ):
                authorizer.authorize_action(
                    principal_id=VIEWER_ID,
                    project_id="proj-1",
                    action=StudioAction.READ_SESSION,
                    binding_ref=bref_viewer,
                )

            store.revoke_principal(OWNER_ID, allow_system_bootstrap=True)
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
        ws = tmp_path / "ws"
        ws.mkdir()
        clip = ws / "clip.mp4"
        _fake_media(clip)
        auth_db = tmp_path / "studio_auth.sqlite3"

        import base64 as _b64

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-bearer-token")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-http")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_STUDIO_ACTIVE_REVISION", "0")
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "queue.sqlite3"))
        monkeypatch.setenv(
            "NEXUS_IBPC_APPROVAL_KEY_RING",
            json.dumps({"k1": _b64.b64encode(b"k" * 32).decode("ascii")}),
        )
        monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        store = StudioPrincipalAuthStore(auth_db, now=clock)
        store.ensure_project("proj-http", tenant_id="ten-1")
        store.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-123")
        store.bind_channel(
            OWNER_ID, channel="studio", external_subject="sub-owner", allow_system_bootstrap=True
        )
        store.grant_membership("proj-http", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        store.register_principal(VIEWER_ID, tenant_id="ten-1", raw_token="tok-viewer-123")
        store.bind_channel(
            VIEWER_ID, channel="studio", external_subject="sub-viewer", allow_system_bootstrap=True
        )
        store.grant_membership("proj-http", VIEWER_ID, role=StudioRole.VIEWER, granted_by=OWNER_ID)

        store.register_principal(AUDITOR_ID, tenant_id="ten-1", raw_token="tok-auditor-123")
        store.bind_channel(
            AUDITOR_ID,
            channel="studio",
            external_subject="sub-auditor",
            allow_system_bootstrap=True,
        )
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
                tenant_id="ten-1",
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
            bref = store.bind_channel(
                OWNER_ID, channel="studio", external_subject="sub-o", allow_system_bootstrap=True
            )
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

            store.revoke_principal(OWNER_ID, allow_system_bootstrap=True)
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
        import base64 as _b64

        http_auth_db = tmp_path / "http_auth.sqlite3"
        http_ws = tmp_path / "http_ws"
        http_ws.mkdir()
        clip = http_ws / "clip.mp4"
        _fake_media(clip)
        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-err")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(http_ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(http_auth_db))
        monkeypatch.setenv("NEXUS_STUDIO_ACTIVE_REVISION", "0")
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "http_q.sqlite3"))
        monkeypatch.setenv("NEXUS_STUDIO_REQUIRE_PRINCIPAL", "1")
        monkeypatch.setenv(
            "NEXUS_IBPC_APPROVAL_KEY_RING",
            json.dumps({"k1": _b64.b64encode(b"k" * 32).decode("ascii")}),
        )
        monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        s = StudioPrincipalAuthStore(http_auth_db, now=clock)
        s.ensure_project("proj-err", tenant_id="ten-1")
        s.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-err")
        s.bind_channel(
            OWNER_ID, channel="studio", external_subject="sub-owner", allow_system_bootstrap=True
        )
        s.grant_membership("proj-err", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        s.register_principal(VIEWER_ID, tenant_id="ten-1", raw_token="tok-viewer-err")
        s.bind_channel(
            VIEWER_ID, channel="studio", external_subject="sub-viewer", allow_system_bootstrap=True
        )
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
        voice_clip = voice_ws / "clip.mp4"
        _fake_media(voice_clip)
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
                source_path=str(voice_clip),
                idempotency_key="txt-key-1",
            )
            r_txt2 = svc.submit_text_intent(
                sid_v,
                "t-txt-dup",
                text="trim from 1 to 2 seconds",
                sequence_number=2,
                asset_id="clip",
                source_path=str(voice_clip),
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
                    source_path=str(voice_clip),
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
                    source_path=str(voice_clip),
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


class TestWave3SecurityClosureAndCoverage:
    """Wave A/B/C security invariants & >=98% branch coverage tests."""

    def test_cross_project_consent_revoke_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        store = DurablePrivacyConsentStore(tmp_path / "privacy.sqlite3")
        try:
            grant = store.grant_consent(
                consent_id="cns_proj_a",
                project_id="proj-a",
                tenant_id="ten-1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value,
                data_category=DataCategory.TRANSCRIPT.value,
                provider_id="gemini",
                granted_at_utc=clock(),
            )
            assert grant.tenant_id == "ten-1"
            with pytest.raises(PrivacyViolation, match="consent_not_found"):
                store.revoke_consent(
                    "cns_proj_a",
                    revoked_by=OWNER_ID,
                    revoked_at_utc=clock(),
                    project_id="proj-b",
                    expected_tenant_id="ten-1",
                )
            with pytest.raises(PrivacyViolation, match="consent_not_found"):
                store.revoke_consent(
                    "cns_proj_a",
                    revoked_by=OWNER_ID,
                    revoked_at_utc=clock(),
                    project_id="proj-a",
                    expected_tenant_id="ten-other",
                )
            with pytest.raises(PrivacyViolation, match="consent_not_found"):
                store.revoke_consent(
                    "cns_missing",
                    revoked_by=OWNER_ID,
                    revoked_at_utc=clock(),
                    project_id="proj-a",
                )
            with pytest.raises(PrivacyViolation, match="unknown_consent"):
                store.revoke_consent(
                    "cns_missing",
                    revoked_by=OWNER_ID,
                    revoked_at_utc=clock(),
                )
            still_active = store.get_consent("cns_proj_a")
            assert still_active is not None and still_active.revoked_at_utc == ""
            # Direct service-level revoke_privacy_consent guard
            ws = tmp_path / "ws"
            ws.mkdir()
            auth, bref = _setup_auth_store(
                tmp_path / "auth.sqlite3", now=clock, project_id="proj-b"
            )

            class _Q:
                async def enqueue(self, **_kw):
                    return "j"

                async def get_status(self, _j):
                    return None

                async def get_result(self, _j):
                    return None

            svc = build_studio_ibpc_service(
                queue=_Q(),
                registry=build_runtime_registry(),
                workspace_root=ws,
                revision_provider=lambda: 0,
                key_provider=StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1"),
                auth_store=auth,
                channel_binding_ref=bref,
                now=clock,
            )
            try:
                with pytest.raises(PrivacyViolation, match="consent_not_found"):
                    svc.revoke_privacy_consent(
                        "cns_proj_a",
                        project_id="proj-b",
                        actor_id=OWNER_ID,
                    )
            finally:
                svc.close()
                auth.close()
        finally:
            store.close()

        # HTTP cross-project revoke returns 404 and leaves proj-a consent intact.
        import base64 as _b64

        http_ws = tmp_path / "http_ws"
        http_ws.mkdir()
        http_auth = tmp_path / "http_auth.sqlite3"
        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-a,proj-b")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(http_ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(http_auth))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "http_q.sqlite3"))
        monkeypatch.setenv(
            "NEXUS_IBPC_APPROVAL_KEY_RING",
            json.dumps({"k1": _b64.b64encode(b"k" * 32).decode("ascii")}),
        )
        monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        s = StudioPrincipalAuthStore(http_auth, now=clock)
        s.ensure_project("proj-a", tenant_id="ten-1")
        s.ensure_project("proj-b", tenant_id="ten-1")
        s.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-sec1")
        s.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-sec1",
            allow_system_bootstrap=True,
        )
        s.grant_membership("proj-a", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        s.grant_membership("proj-b", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        s.close()

        client = TestClient(app_module.app)
        headers = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-sec1",
        }
        r_g = client.post(
            "/api/studio/projects/proj-a/ibpc/privacy/consents",
            headers=headers,
            json={
                "mode": "TRUSTED_CLOUD",
                "processing_scope": "EXTERNAL_PROVIDER_DISPATCH",
                "data_category": "TRANSCRIPT",
                "provider_id": "gemini",
            },
        )
        assert r_g.status_code == 201
        cid = r_g.json()["consent_id"]
        assert (
            client.post(
                f"/api/studio/projects/proj-b/ibpc/privacy/consents/{cid}/revoke",
                headers=headers,
                json={"reason": "cross_project_attack"},
            ).status_code
            == 404
        )
        # Same-project revoke still succeeds -> 200.
        assert (
            client.post(
                f"/api/studio/projects/proj-a/ibpc/privacy/consents/{cid}/revoke",
                headers=headers,
                json={"reason": "owner_revoked"},
            ).status_code
            == 200
        )

    def test_external_provider_consent_requires_exact_provider_id(self, tmp_path: Path) -> None:
        clock = _Clock()
        store = DurablePrivacyConsentStore(tmp_path / "privacy.sqlite3")
        try:
            policy = PrivacyPolicy(
                now=clock,
                store=store,
                configured_providers=frozenset({"gemini", "openai"}),
            )
            # Empty provider_id or mismatched provider_id never satisfies "gemini".
            store.grant_consent(
                consent_id="cns_empty_prov",
                project_id="proj-1",
                tenant_id="ten-1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value,
                data_category=DataCategory.TRANSCRIPT.value,
                provider_id="",
                granted_at_utc=clock(),
            )
            store.grant_consent(
                consent_id="cns_openai",
                project_id="proj-1",
                tenant_id="ten-1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value,
                data_category=DataCategory.TRANSCRIPT.value,
                provider_id="openai",
                granted_at_utc=clock(),
            )
            with pytest.raises(PrivacyViolation, match="consent_missing"):
                policy.enforce_processing_consent(
                    project_id="proj-1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    provider_id="gemini",
                )
            # Also dispatching with empty provider_id fails closed.
            with pytest.raises(PrivacyViolation, match="consent_missing"):
                policy.enforce_processing_consent(
                    project_id="proj-1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.TRANSCRIPT,
                    provider_id="",
                )
            # Exact provider_id="gemini" grant succeeds.
            store.grant_consent(
                consent_id="cns_gemini",
                project_id="proj-1",
                tenant_id="ten-1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value,
                data_category=DataCategory.TRANSCRIPT.value,
                provider_id="gemini",
                granted_at_utc=clock(),
            )
            matched = policy.enforce_processing_consent(
                project_id="proj-1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                data_category=DataCategory.TRANSCRIPT,
                provider_id="gemini",
            )
            assert matched is not None and matched.consent_id == "cns_gemini"
            # Local processing under LOCAL_VAULT uses LOCAL_PROCESSING scope.
            assert (
                policy.enforce_processing_consent(
                    project_id="proj-1",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.LOCAL_VAULT,
                    processing_scope=ProcessingScope.LOCAL_PROCESSING,
                    data_category=DataCategory.AUDIO,
                )
                is None
            )
            # Context manager close coverage.
            assert store.list_consents(project_id="proj-1")
        finally:
            store.close()
        store.close()  # idempotent second close

    @pytest.mark.asyncio
    async def test_cross_project_and_cross_principal_turn_submission_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        _stub_media_tools(monkeypatch)
        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        clip = ws / "clip.mp4"
        _fake_media(clip)
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-a", tenant_id="ten-1")
        auth.ensure_project("proj-b", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-sec3")
        bref_owner = auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-sec3",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-a", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        auth.grant_membership("proj-b", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        auth.register_principal(EDITOR_ID, tenant_id="ten-1", raw_token="tok-editor-sec3")
        auth.bind_channel(
            EDITOR_ID,
            channel="studio",
            external_subject="sub-editor-sec3",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-a", EDITOR_ID, role=StudioRole.EDITOR, granted_by=OWNER_ID)

        class _Q:
            async def enqueue(self, **_kw):
                return "job-sec3"

            async def get_status(self, _j):
                return None

            async def get_result(self, _j):
                return None

        class _FakeCap:
            def is_available(self) -> bool:
                return True

            async def transcribe_file(self, _p: Path, **_kw):
                return types.SimpleNamespace(text="trim from 1 to 2 seconds", is_final=True)

        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")
        svc = build_studio_ibpc_service(
            queue=_Q(),
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth,
            channel_binding_ref=bref_owner,
            now=clock,
        )
        svc._deps = dataclasses.replace(svc._deps, caption_engine=_FakeCap())  # type: ignore[misc]
        try:
            sid = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-a",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
            )
            # Cross-project / cross-tenant / cross-principal get/submit/close/approve/execute
            # refused!
            with pytest.raises(AuthorizationError, match="session_project_mismatch"):
                svc.get_session(sid, principal_id=OWNER_ID, expected_project_id="proj-b")
            with pytest.raises(AuthorizationError, match="tenant_mismatch"):
                svc.get_session(sid, principal_id=OWNER_ID, expected_tenant_id="ten-other")
            with pytest.raises(AuthorizationError, match="session_project_mismatch"):
                svc.submit_text_intent(
                    sid,
                    "t-xproj",
                    sequence_number=1,
                    text="trim from 1 to 2 seconds",
                    asset_id="clip",
                    source_path=str(clip),
                    caller_principal_id=OWNER_ID,
                    expected_project_id="proj-b",
                )
            with pytest.raises(AuthorizationError, match="wrong_principal"):
                svc.submit_text_intent(
                    sid,
                    "t-xprin",
                    sequence_number=1,
                    text="trim from 1 to 2 seconds",
                    asset_id="clip",
                    source_path=str(clip),
                    caller_principal_id=EDITOR_ID,
                    expected_project_id="proj-a",
                )
            with pytest.raises(AuthorizationError, match="wrong_principal"):
                await svc.submit_voice_intent(
                    sid,
                    "t-vxprin",
                    sequence_number=1,
                    audio=b"RIFF\x00\x00\x00\x00WAVE",
                    asset_id="clip",
                    source_path=str(clip),
                    caller_principal_id=EDITOR_ID,
                    expected_project_id="proj-a",
                )
            with pytest.raises(AuthorizationError, match="wrong_principal"):
                svc.close_session(sid, principal_id=EDITOR_ID, expected_project_id="proj-a")
            with pytest.raises(AuthorizationError, match="session_project_mismatch"):
                svc.pause_session(sid, principal_id=OWNER_ID, expected_project_id="proj-b")
            with pytest.raises(AuthorizationError, match="session_project_mismatch"):
                svc.abort_session(sid, principal_id=OWNER_ID, expected_project_id="proj-b")
            with pytest.raises(AuthorizationError, match="session_project_mismatch"):
                svc.resume_session(sid, principal_id=OWNER_ID, expected_project_id="proj-b")

            res = svc.submit_text_intent(
                sid,
                "t-ok",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip),
                caller_principal_id=OWNER_ID,
                expected_project_id="proj-a",
                expected_tenant_id="ten-1",
            )
            assert isinstance(res, PipelineResult)
            with pytest.raises(AuthorizationError, match="session_project_mismatch"):
                svc.approve(
                    sid,
                    "t-ok",
                    res.approval_id,
                    principal_id=OWNER_ID,
                    expected_project_id="proj-b",
                )
            with pytest.raises(AuthorizationError, match="tenant_mismatch"):
                svc.approve(
                    sid,
                    "t-ok",
                    res.approval_id,
                    principal_id=OWNER_ID,
                    expected_tenant_id="ten-other",
                )
            svc.approve(
                sid,
                "t-ok",
                res.approval_id,
                principal_id=OWNER_ID,
                expected_project_id="proj-a",
                expected_tenant_id="ten-1",
            )
            with pytest.raises(AuthorizationError, match="session_project_mismatch"):
                await svc.execute(
                    sid,
                    "t-ok",
                    res.approval_id,
                    principal_id=OWNER_ID,
                    expected_project_id="proj-b",
                )

            # Voice intent under TRUSTED_CLOUD without AUDIO consent fails closed.
            svc.grant_privacy_consent(
                project_id="proj-a",
                tenant_id="ten-1",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                data_category=DataCategory.TRANSCRIPT,
                provider_id="gemini",
            )
            sid_tc = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-a",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                privacy_mode=PrivacyMode.TRUSTED_CLOUD,
                consent_scope="transcript",
            )
            with pytest.raises(PrivacyViolation):
                await svc.submit_voice_intent(
                    sid_tc,
                    "t-voice-noconsent",
                    sequence_number=1,
                    audio=b"RIFF\x00\x00\x00\x00WAVE",
                    asset_id="clip",
                    source_path=str(clip),
                    caller_principal_id=OWNER_ID,
                    expected_project_id="proj-a",
                    expected_tenant_id="ten-1",
                )
        finally:
            svc.close()
            auth.close()

        # HTTP cross-project / cross-principal session routes return 403/404.
        import base64 as _b64

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-a,proj-b")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "http_q.sqlite3"))
        monkeypatch.setenv(
            "NEXUS_IBPC_APPROVAL_KEY_RING",
            json.dumps({"k1": _b64.b64encode(b"k" * 32).decode("ascii")}),
        )
        monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        owner_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-sec3",
        }
        editor_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-editor-sec3",
        }
        r_open = client.post(
            "/api/studio/projects/proj-a/ibpc/sessions",
            headers=owner_h,
            json={},
        )
        assert r_open.status_code == 201
        http_sid = r_open.json()["session_id"]
        assert r_open.json()["tenant_id"] == "ten-1"
        assert (
            client.post(
                f"/api/studio/projects/proj-b/ibpc/sessions/{http_sid}/turns",
                headers=owner_h,
                json={
                    "turn_id": "t-http-xp",
                    "sequence_number": 1,
                    "text": "trim from 1 to 2 seconds",
                    "asset_id": "clip",
                    "source_path": str(clip),
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/studio/projects/proj-a/ibpc/sessions/{http_sid}/turns",
                headers=editor_h,
                json={
                    "turn_id": "t-http-xprin",
                    "sequence_number": 1,
                    "text": "trim from 1 to 2 seconds",
                    "asset_id": "clip",
                    "source_path": str(clip),
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/studio/projects/proj-b/ibpc/sessions/{http_sid}/close",
                headers=owner_h,
            ).status_code
            == 403
        )

    def test_cross_tenant_membership_grant_and_session_spoof_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        auth_db = tmp_path / "auth.sqlite3"
        with StudioPrincipalAuthStore(auth_db, now=clock) as store:
            store.ensure_project("proj-t1", tenant_id="ten-1")
            store.ensure_project("proj-t2", tenant_id="ten-2")
            store.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-t1-owner")
            b1 = store.bind_channel(
                OWNER_ID,
                channel="studio",
                external_subject="sub-t1-owner",
                allow_system_bootstrap=True,
            )
            store.grant_membership("proj-t1", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

            t2_user = "prn_" + "e" * 32
            store.register_principal(t2_user, tenant_id="ten-2", raw_token="tok-t2-user")
            store.bind_channel(
                t2_user,
                channel="studio",
                external_subject="sub-t2-user",
                allow_system_bootstrap=True,
            )
            store.grant_membership("proj-t2", t2_user, role=StudioRole.OWNER, granted_by=t2_user)

            # Cross-tenant membership grant fails closed.
            with pytest.raises(AuthorizationError, match="cross_tenant_membership_denied"):
                store.grant_membership(
                    "proj-t1", t2_user, role=StudioRole.EDITOR, granted_by=OWNER_ID
                )
            # Unknown project/principal checks.
            with pytest.raises(AuthorizationError, match="project_not_found"):
                store.grant_membership(
                    "proj-missing", OWNER_ID, role=StudioRole.EDITOR, granted_by=OWNER_ID
                )
            with pytest.raises(AuthorizationError, match="principal_not_found"):
                store.grant_membership(
                    "proj-t1", "prn_" + "f" * 32, role=StudioRole.EDITOR, granted_by=OWNER_ID
                )
            with pytest.raises(AuthorizationError, match="principal_not_found"):
                store.grant_membership(
                    "proj-t1", OWNER_ID, role=StudioRole.EDITOR, granted_by="prn_" + "f" * 32
                )
            with pytest.raises(AuthorizationError, match="principal_not_found"):
                store.get_principal_tenant("prn_" + "f" * 32)
            with pytest.raises(AuthorizationError, match="project_not_found"):
                store.get_project_tenant("proj-missing")
            with pytest.raises(AuthorizationError, match="project_not_found"):
                store.get_project_revision("proj-missing")
            with pytest.raises(AuthorizationError, match="project_not_found"):
                store.set_project_revision("proj-missing", "1")
            with pytest.raises(AuthorizationError, match="tenant_mismatch"):
                store.assert_same_tenant(
                    principal_id=OWNER_ID, project_id="proj-t1", tenant_id="ten-2"
                )
            # Cross-tenant authorize_action fails closed.
            with pytest.raises(StudioAuthorizationError, match="cross_tenant_denied"):
                store.authorize_action(
                    principal_id=OWNER_ID,
                    project_id="proj-t2",
                    action=StudioAction.READ_SESSION,
                    binding_ref=b1,
                )

        import base64 as _b64

        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-t1,proj-t2")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        monkeypatch.setenv(
            "NEXUS_IBPC_APPROVAL_KEY_RING",
            json.dumps({"k1": _b64.b64encode(b"k" * 32).decode("ascii")}),
        )
        monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        h_t1 = {"Authorization": "Bearer deploy-tok", "X-Nexus-Principal-Token": "tok-t1-owner"}
        # Spoofed tenant_id -> 403.
        assert (
            client.post(
                "/api/studio/projects/proj-t1/ibpc/sessions",
                headers=h_t1,
                json={"tenant_id": "ten-2"},
            ).status_code
            == 403
        )
        # Cross-tenant membership grant over HTTP -> 403.
        assert (
            client.post(
                "/api/studio/projects/proj-t1/memberships",
                headers=h_t1,
                json={"principal_id": t2_user, "role": "EDITOR"},
            ).status_code
            == 403
        )
        # Cross-tenant project open -> 403.
        assert (
            client.post(
                "/api/studio/projects/proj-t2/ibpc/sessions",
                headers=h_t1,
                json={},
            ).status_code
            == 403
        )

    def test_symlink_and_outside_asset_root_source_path_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        asset_root = tmp_path / "assets"
        asset_root.mkdir()
        outside = tmp_path / "secret_outside.mp4"
        _fake_media(outside)
        inside_real = asset_root / "valid.mp4"
        _fake_media(inside_real)
        symlink_inside = asset_root / "evil_link.mp4"
        symlink_inside.symlink_to(outside)
        internal_symlink = asset_root / "internal_link.mp4"
        internal_symlink.symlink_to(inside_real)

        probe_calls = 0

        def _counting_probe(*_a, **_kw):
            nonlocal probe_calls
            probe_calls += 1
            raise AssertionError("probe_media must never be called for untrusted source_path")

        monkeypatch.setattr(service_mod, "probe_media", _counting_probe)

        auth, bref = _setup_auth_store(tmp_path / "auth.sqlite3", now=clock)

        class _Q:
            async def enqueue(self, **_kw):
                return "j"

            async def get_status(self, _j):
                return None

            async def get_result(self, _j):
                return None

        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")
        svc = build_studio_ibpc_service(
            queue=_Q(),
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth,
            channel_binding_ref=bref,
            now=clock,
            authorized_asset_root=asset_root,
        )
        try:
            sid = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
            )
            # Symlink inside authorized_asset_root -> AuthorizationError("untrusted_source_path").
            with pytest.raises(AuthorizationError, match="untrusted_source_path"):
                svc.submit_text_intent(
                    sid,
                    "t-sym",
                    sequence_number=1,
                    text="trim from 1 to 2 seconds",
                    asset_id="clip",
                    source_path=str(symlink_inside),
                )
            with pytest.raises(AuthorizationError, match="untrusted_source_path"):
                svc.submit_text_intent(
                    sid,
                    "t-sym-in",
                    sequence_number=1,
                    text="trim from 1 to 2 seconds",
                    asset_id="clip",
                    source_path=str(internal_symlink),
                )
            # Existing file outside authorized_asset_root ->
            # AuthorizationError("untrusted_source_path").
            with pytest.raises(AuthorizationError, match="untrusted_source_path"):
                svc.submit_text_intent(
                    sid,
                    "t-out",
                    sequence_number=1,
                    text="trim from 1 to 2 seconds",
                    asset_id="clip",
                    source_path=str(outside),
                )
            # Non-existent file outside authorized_asset_root -> AuthorizationError (not
            # missing_source!).
            with pytest.raises(AuthorizationError, match="untrusted_source_path"):
                svc.submit_text_intent(
                    sid,
                    "t-out-missing",
                    sequence_number=1,
                    text="trim from 1 to 2 seconds",
                    asset_id="clip",
                    source_path=str(tmp_path / "nonexistent_outside.mp4"),
                )
            # Missing file INSIDE authorized_asset_root returns typed Refusal("missing_source").
            refusal = svc.submit_text_intent(
                sid,
                "t-in-missing",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(asset_root / "missing_inside.mp4"),
            )
            assert getattr(refusal, "reason", None) == "missing_source"
            assert probe_calls == 0
        finally:
            svc.close()
            auth.close()

        # HTTP endpoint rejects outside/symlink paths with 403 and respects NEXUS_STUDIO_ASSET_ROOT.
        import base64 as _b64

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-w3")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_ASSET_ROOT", str(asset_root))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(tmp_path / "auth.sqlite3"))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        monkeypatch.setenv(
            "NEXUS_IBPC_APPROVAL_KEY_RING",
            json.dumps({"k1": _b64.b64encode(b"k" * 32).decode("ascii")}),
        )
        monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        headers = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": f"tok-{OWNER_ID}-secret",
        }
        r_s = client.post("/api/studio/projects/proj-w3/ibpc/sessions", headers=headers, json={})
        assert r_s.status_code == 201
        http_sid = r_s.json()["session_id"]
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/ibpc/sessions/{http_sid}/turns",
                headers=headers,
                json={
                    "turn_id": "t-http-sym",
                    "sequence_number": 1,
                    "text": "trim from 1 to 2 seconds",
                    "asset_id": "clip",
                    "source_path": str(symlink_inside),
                },
            ).status_code
            == 403
        )

    @pytest.mark.asyncio
    async def test_missing_approval_key_fails_closed_no_fallback_and_key_rotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings
        from nexus_ai_agent.nagar.ibpc.approval import (
            ApprovalBinding,
            ApprovalIdentity,
            DurableApprovalLedger,
            canonical_approval_payload,
            compute_approval_hash,
        )
        from nexus_ai_agent.nagar.ibpc.models import CanonicalizationScheme, canonical_digest

        _stub_media_tools(monkeypatch)
        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        clip = ws / "clip.mp4"
        _fake_media(clip)
        auth_db = tmp_path / "auth.sqlite3"
        auth, bref = _setup_auth_store(auth_db, now=clock)

        # HTTP without NEXUS_IBPC_APPROVAL_KEY_RING -> 503 (never "studio-default").
        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-w3")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        monkeypatch.delenv("NEXUS_IBPC_APPROVAL_KEY_RING", raising=False)
        monkeypatch.delenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", raising=False)
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        headers = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": f"tok-{OWNER_ID}-secret",
        }
        assert (
            client.post(
                "/api/studio/projects/proj-w3/ibpc/sessions", headers=headers, json={}
            ).status_code
            == 503
        )

        # Check RFC 8785 JCS canonical_approval_payload & compute_approval_hash parity.
        payload = canonical_approval_payload(
            project_id="proj-w3",
            revision_id="0",
            plan_digest="sha256:" + "a" * 64,
            preview_digest="sha256:" + "b" * 64,
            scope_signature="sha256:" + "c" * 64,
            principal_id=OWNER_ID,
            expires_at_utc="2026-11-26T12:10:00Z",
        )
        assert compute_approval_hash(**payload) == canonical_digest(
            payload, CanonicalizationScheme.JCS_RFC8785_V2
        )

        # Key rotation: approve under k1, restart with ring {"k1", "k2"} active="k2" -> succeeds;
        # restart with ring {"k2"} only (k1 revoked) -> fails closed with approval_key_unavailable!
        class _RecordingQ:
            def __init__(self) -> None:
                self.enqueued: list[dict] = []
                self._results: dict[str, dict] = {}

            async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
                jid = f"job-rot-{len(self.enqueued) + 1}"
                out = Path(str(payload["workspace_dir"])) / "out.mp4"
                out.write_bytes(b"OK")
                self.enqueued.append({"job_id": jid})
                self._results[jid] = {"success": True, "artifact_path": str(out)}
                return jid

            async def get_status(self, _j: str):
                from nexus_ai_agent.application.ports.job_queue import JobStatus

                return JobStatus.COMPLETED

            async def get_result(self, j: str):
                return self._results.get(j)

        from nexus_ai_agent.nagar.ibpc.models import CheckVerdict

        monkeypatch.setattr(
            service_mod,
            "verify_keep",
            lambda *_a, **_kw: types.SimpleNamespace(
                verdict=CheckVerdict.PASS, report_digest="sha256:" + "3" * 64
            ),
        )
        monkeypatch.setenv("CREATIVE_TEMP_DIR", str(ws))
        get_settings.cache_clear()

        ring_k1 = StaticKeyRing({"k1": b"1" * 32}, active_key_id="k1")
        q = _RecordingQ()
        svc1 = build_studio_ibpc_service(
            queue=q,
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=ring_k1,
            auth_store=auth,
            channel_binding_ref=bref,
            now=clock,
        )
        sid1 = svc1.open_session(
            tenant_id="ten-1", project_id="proj-w3", principal_id=OWNER_ID, user_id=1, chat_id=1
        )
        sid2 = svc1.open_session(
            tenant_id="ten-1", project_id="proj-w3", principal_id=OWNER_ID, user_id=1, chat_id=2
        )
        r1 = svc1.submit_text_intent(
            sid1,
            "t-rot-1",
            sequence_number=1,
            text="trim from 1 to 2 seconds",
            asset_id="clip",
            source_path=str(clip),
        )
        r2 = svc1.submit_text_intent(
            sid2,
            "t-rot-2",
            sequence_number=1,
            text="trim from 1 to 2 seconds",
            asset_id="clip",
            source_path=str(clip),
        )
        assert isinstance(r1, PipelineResult) and isinstance(r2, PipelineResult)
        b1 = svc1.approve(sid1, "t-rot-1", r1.approval_id, principal_id=OWNER_ID)
        svc1.approve(sid2, "t-rot-2", r2.approval_id, principal_id=OWNER_ID)
        assert b1.key_id == "k1"
        svc1.close()

        # Ring rotated to k2 while k1 remains for verification -> execute r1 succeeds!
        ring_rotated = StaticKeyRing({"k1": b"1" * 32, "k2": b"2" * 32}, active_key_id="k2")
        svc2 = build_studio_ibpc_service(
            queue=q,
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=ring_rotated,
            auth_store=auth,
            channel_binding_ref=bref,
            now=clock,
        )
        out1 = await svc2.execute(sid1, "t-rot-1", r1.approval_id, principal_id=OWNER_ID)
        assert out1.status in ("executed", "executed_unverified")
        svc2.close()

        # Ring with k1 revoked -> consume refuses r2 with approval_key_unavailable.
        ring_k2_only = StaticKeyRing({"k2": b"2" * 32}, active_key_id="k2")
        svc3 = build_studio_ibpc_service(
            queue=q,
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=ring_k2_only,
            auth_store=auth,
            channel_binding_ref=bref,
            now=clock,
        )
        out2 = await svc3.execute(sid2, "t-rot-2", r2.approval_id, principal_id=OWNER_ID)
        assert out2.status == "refused"
        assert "approval_key_unavailable" in out2.detail or "approval_mac_invalid" in out2.detail
        svc3.close()

        # Direct DurableApprovalLedger key validation coverage.
        with DurableApprovalLedger(tmp_path / "ledger_keys.sqlite3", now=clock) as ledger:
            ident = ApprovalIdentity(
                principal_id=OWNER_ID,
                tenant_id="ten-1",
                project_id="proj-w3",
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                source_revision=0,
                source_asset_digest="sha256:" + "1" * 64,
                ast_digest="sha256:" + "2" * 64,
                plan_digest="sha256:" + "3" * 64,
                preview_digest="sha256:" + "4" * 64,
                revision_id="0",
                scope_signature="sha256:" + "5" * 64,
                expires_at_utc="2026-11-26T12:10:00Z",
            )
            binding = ApprovalBinding(
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                source_revision=0,
                source_asset_digest=ident.source_asset_digest,
                ast_digest=ident.ast_digest,
                plan_digest=ident.plan_digest,
                preview_digest=ident.preview_digest,
                revision_id="0",
                scope_signature=ident.scope_signature or "",
                expires_at_utc="2026-11-26T12:10:00Z",
            )
            stored = ledger.create(binding)
            with pytest.raises(ApprovalError, match="approval_key_unavailable"):
                ledger.approve(
                    stored.approval_id,
                    identity=ident,
                    turn_is_active=lambda: True,
                    key_id="missing-key",
                    key_provider=ring_k1,
                )
            ledger.approve(
                stored.approval_id,
                identity=ident,
                turn_is_active=lambda: True,
                key_id="k1",
                key_provider=ring_k1,
            )
            with pytest.raises(ApprovalError, match="approval_key_unavailable"):
                ledger.consume(stored.approval_id, identity=ident, key_provider=None)
            with pytest.raises(ApprovalError, match="approval_key_unavailable"):
                ledger.consume(stored.approval_id, identity=ident, key_provider=ring_k2_only)
        auth.close()

    def test_stale_and_spoofed_base_revision_id_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        _stub_media_tools(monkeypatch)
        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        clip = ws / "clip.mp4"
        _fake_media(clip)
        auth_db = tmp_path / "auth.sqlite3"
        auth, bref = _setup_auth_store(auth_db, now=clock)

        class _Q:
            async def enqueue(self, **_kw):
                return "j"

            async def get_status(self, _j):
                return None

            async def get_result(self, _j):
                return None

        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")
        svc = build_studio_ibpc_service(
            queue=_Q(),
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth,
            channel_binding_ref=bref,
            now=clock,
        )
        try:
            with pytest.raises(StaleRevisionError, match="revision_mismatch"):
                svc.open_session(
                    tenant_id="ten-1",
                    project_id="proj-w3",
                    principal_id=OWNER_ID,
                    user_id=1,
                    chat_id=1,
                    base_revision_id="999",
                )
            with pytest.raises(StaleRevisionError, match="revision_mismatch"):
                svc.open_session(
                    tenant_id="ten-1",
                    project_id="proj-w3",
                    principal_id=OWNER_ID,
                    user_id=1,
                    chat_id=1,
                    active_revision_id="999",
                )
            sid = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                base_revision_id="0",
            )
            with pytest.raises(StaleRevisionError, match="revision_mismatch"):
                svc.submit_text_intent(
                    sid,
                    "t-bad-rev",
                    sequence_number=1,
                    text="trim from 1 to 2 seconds",
                    asset_id="clip",
                    source_path=str(clip),
                    base_revision_id="999",
                )
            # Advance authoritative project revision -> session becomes stale!
            auth.set_project_revision("proj-w3", "1")
            with pytest.raises(StaleRevisionError, match="stale_revision"):
                svc.submit_text_intent(
                    sid,
                    "t-stale-rev",
                    sequence_number=1,
                    text="trim from 1 to 2 seconds",
                    asset_id="clip",
                    source_path=str(clip),
                )
        finally:
            svc.close()

        # Reset project revision to "0" for HTTP check.
        auth.set_project_revision("proj-w3", "0")
        auth.close()

        import base64 as _b64

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-w3")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        monkeypatch.setenv(
            "NEXUS_IBPC_APPROVAL_KEY_RING",
            json.dumps({"k1": _b64.b64encode(b"k" * 32).decode("ascii")}),
        )
        monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        headers = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": f"tok-{OWNER_ID}-secret",
        }
        assert (
            client.post(
                "/api/studio/projects/proj-w3/ibpc/sessions",
                headers=headers,
                json={"active_revision_id": "999"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/studio/projects/proj-w3/ibpc/sessions",
                headers=headers,
                json={"base_revision_id": "999"},
            ).status_code
            == 409
        )
        r_ok = client.post(
            "/api/studio/projects/proj-w3/ibpc/sessions",
            headers=headers,
            json={"base_revision_id": "0"},
        )
        assert r_ok.status_code == 201
        http_sid = r_ok.json()["session_id"]
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/ibpc/sessions/{http_sid}/turns",
                headers=headers,
                json={
                    "turn_id": "t-spoof",
                    "sequence_number": 1,
                    "text": "trim from 1 to 2 seconds",
                    "asset_id": "clip",
                    "source_path": str(clip),
                    "base_revision_id": "42",
                },
            ).status_code
            == 409
        )

    def test_studio_rbac_fail_closed_by_default_when_auth_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = ws / "queue" / "studio_auth.sqlite3"
        auth, _bref = _setup_auth_store(auth_db, now=clock)
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-w3")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        monkeypatch.delenv("NEXUS_STUDIO_AUTH_DB", raising=False)
        monkeypatch.delenv("NEXUS_STUDIO_REQUIRE_PRINCIPAL", raising=False)
        monkeypatch.delenv("NEXUS_STUDIO_ALLOW_UNAUTHENTICATED_PRINCIPAL", raising=False)
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        deploy_h = {"Authorization": "Bearer deploy-tok"}
        # Even WITHOUT NEXUS_STUDIO_REQUIRE_PRINCIPAL=1, studio_auth.sqlite3 present fails closed ->
        # 401!
        assert (
            client.get(
                "/api/studio/projects/proj-w3/jobs/job-1/state",
                headers=deploy_h,
            ).status_code
            == 401
        )
        # With explicit legacy opt-out, unauthenticated principal falls through to route (404
        # missing job).
        monkeypatch.setenv("NEXUS_STUDIO_ALLOW_UNAUTHENTICATED_PRINCIPAL", "1")
        assert (
            client.get(
                "/api/studio/projects/proj-w3/jobs/job-1/state",
                headers=deploy_h,
            ).status_code
            == 404
        )
        # NEXUS_STUDIO_REQUIRE_PRINCIPAL=0 also explicitly disables default principal check.
        monkeypatch.delenv("NEXUS_STUDIO_ALLOW_UNAUTHENTICATED_PRINCIPAL", raising=False)
        monkeypatch.setenv("NEXUS_STUDIO_REQUIRE_PRINCIPAL", "0")
        assert (
            client.get(
                "/api/studio/projects/proj-w3/jobs/job-1/state",
                headers=deploy_h,
            ).status_code
            == 404
        )

    def test_membership_and_principal_lifecycle_apis_and_admin_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-w3", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-life")
        auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-life",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-w3", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        auth.register_principal(EDITOR_ID, tenant_id="ten-1", raw_token="tok-editor-life")
        auth.bind_channel(
            EDITOR_ID,
            channel="studio",
            external_subject="sub-editor-life",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-w3", EDITOR_ID, role=StudioRole.EDITOR, granted_by=OWNER_ID)

        t2_owner = "prn_" + "9" * 32
        auth.register_principal(t2_owner, tenant_id="ten-2", raw_token="tok-t2-life")
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-w3")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        owner_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-life",
        }
        editor_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-editor-life",
        }
        new_pid = "prn_" + "8" * 32

        # Non-OWNER cannot provision principals or bindings -> 403.
        assert (
            client.post(
                "/api/studio/principals?project_id=proj-w3",
                headers=editor_h,
                json={"principal_id": new_pid, "raw_token": "tok-new-principal-123"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/studio/principals/{EDITOR_ID}/bindings?project_id=proj-w3",
                headers=editor_h,
                json={"channel": "studio", "external_subject": "sub-e2"},
            ).status_code
            == 403
        )
        # Cross-tenant principal / binding rejected -> 403.
        assert (
            client.post(
                "/api/studio/principals?project_id=proj-w3",
                headers=owner_h,
                json={
                    "principal_id": new_pid,
                    "raw_token": "tok-new-principal-123",
                    "tenant_id": "ten-2",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/studio/principals/{t2_owner}/bindings?project_id=proj-w3",
                headers=owner_h,
                json={"channel": "studio", "external_subject": "sub-xten"},
            ).status_code
            == 403
        )
        # Invalid principal id -> 400; short token -> 422; unknown principal binding -> 404.
        assert (
            client.post(
                "/api/studio/principals/bad$pid/bindings?project_id=proj-w3",
                headers=owner_h,
                json={"channel": "studio", "external_subject": "sub-bad"},
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/studio/principals/{new_pid}/bindings?project_id=proj-w3",
                headers=owner_h,
                json={"channel": "studio", "external_subject": "sub-missing"},
            ).status_code
            == 404
        )
        # Invalid principal format caught by ValueError -> 400.
        assert (
            client.post(
                "/api/studio/principals?project_id=proj-w3",
                headers=owner_h,
                json={"principal_id": "not-a-valid-pid", "raw_token": "tok-valid-len-123"},
            ).status_code
            == 400
        )

        # OWNER provisions principal + binds channel + grants/lists/revokes membership.
        r_p = client.post(
            "/api/studio/principals?project_id=proj-w3",
            headers=owner_h,
            json={
                "principal_id": new_pid,
                "raw_token": "tok-new-principal-123",
                "display_name": "New Editor",
            },
        )
        assert r_p.status_code == 201
        r_b = client.post(
            f"/api/studio/principals/{new_pid}/bindings?project_id=proj-w3",
            headers=owner_h,
            json={"channel": "studio", "external_subject": "sub-new-editor"},
        )
        assert r_b.status_code == 201
        assert (
            client.post(
                "/api/studio/projects/proj-w3/memberships",
                headers=owner_h,
                json={"principal_id": new_pid, "role": "EDITOR"},
            ).status_code
            == 201
        )
        r_list = client.get("/api/studio/projects/proj-w3/memberships", headers=owner_h)
        assert r_list.status_code == 200
        assert any(
            m["principal_id"] == new_pid and m["status"] == "ACTIVE"
            for m in r_list.json()["memberships"]
        )

        # DELETE membership: invalid id (400), unknown target (404), cross-tenant target (403),
        # valid (200).
        assert (
            client.delete(
                "/api/studio/projects/proj-w3/memberships/bad$pid",
                headers=owner_h,
            ).status_code
            == 400
        )
        assert (
            client.delete(
                f"/api/studio/projects/proj-w3/memberships/{'prn_' + '7' * 32}",
                headers=owner_h,
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/studio/projects/proj-w3/memberships/{t2_owner}",
                headers=owner_h,
            ).status_code
            == 403
        )
        r_del = client.delete(
            f"/api/studio/projects/proj-w3/memberships/{new_pid}",
            headers=owner_h,
        )
        assert r_del.status_code == 200 and r_del.json()["status"] == "REVOKED"

        # Admin audit endpoint records all lifecycle events and DB table is append-only.
        r_adm = client.get("/api/studio/projects/proj-w3/admin/audit", headers=owner_h)
        assert r_adm.status_code == 200
        events = [e["event_type"] for e in r_adm.json()["audit"]]
        assert "grant_membership" in events and "revoke_membership" in events

        with StudioPrincipalAuthStore(auth_db, now=clock) as check_store:
            all_admin = check_store.list_admin_audit(target_principal_id=new_pid)
            assert [e["event_type"] for e in all_admin] == [
                "register_principal",
                "bind_channel",
                "grant_membership",
                "revoke_membership",
            ]
            assert check_store.list_admin_audit()  # unfiltered path
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                check_store._conn.execute("UPDATE nexus_studio_admin_audit SET actor_id = 'tamper'")
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                check_store._conn.execute("DELETE FROM nexus_studio_admin_audit")

        # Deterministic close: verify no leftover open SQLite FDs in this process for auth_db.
        import gc

        gc.collect()
        fd_targets: list[str] = []
        proc_fd = Path("/proc/self/fd")
        if proc_fd.exists():
            for entry in proc_fd.iterdir():
                try:
                    fd_targets.append(os.readlink(entry))
                except OSError:
                    continue
        assert str(auth_db) not in fd_targets

    @pytest.mark.asyncio
    async def test_wave3_branch_coverage_closure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.api import studio as studio_api
        from nexus_ai_agent.config.settings import get_settings
        from nexus_ai_agent.nagar.ibpc.approval import (
            ApprovalBinding,
            ApprovalIdentity,
            DurableApprovalLedger,
        )
        from nexus_ai_agent.nagar.ibpc.composition import build_ibpc_service
        from nexus_ai_agent.nagar.ibpc.models import VideoTimeScope, _reject_non_finite
        from nexus_ai_agent.nagar.ibpc.privacy import _parse_utc
        from nexus_ai_agent.nagar.ibpc.session import (
            DurableLiveSessionStore,
            LiveSessionManager,
            StaleRevisionError,
        )

        clock = _Clock()

        # 1. models.py: _reject_non_finite nested dict/list & VideoTimeScope.forbidden_ranges edge
        # cases.
        _reject_non_finite({"a": [1, 2.0, {"b": 3}]})
        with pytest.raises(ValueError, match="non-finite"):
            _reject_non_finite({"a": [float("nan")]})
        scope_keep = VideoTimeScope(asset_id="clip", start_us=0, end_us=500, effect="keep")
        assert scope_keep.forbidden_ranges(0) == ()
        assert scope_keep.forbidden_ranges(200)[0].end_us == 200
        scope_keep_empty = VideoTimeScope(asset_id="clip", start_us=500, end_us=1000, effect="keep")
        assert scope_keep_empty.forbidden_ranges(300) == ()
        assert scope_keep_empty._complement(0) == ()

        # 2. composition.py: non-durable and pre-injected store branches.
        class _StubQ:
            async def enqueue(self, **_kw):
                return "j-cov"

            async def get_status(self, _j):
                return None

            async def get_result(self, _j):
                return None

        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")
        ws_comp = tmp_path / "ws_comp"
        ws_comp.mkdir()
        svc_nd = build_ibpc_service(
            IBPCServiceDeps(
                queue=_StubQ(),
                registry=build_runtime_registry(),
                workspace_root=ws_comp,
                revision_provider=lambda: 0,
                durable=False,
                now=clock,
            ),
            key_provider=ring,
        )
        svc_nd.close()
        s_pre = DurableLiveSessionStore(ws_comp / "pre_s.sqlite3")
        a_pre = DurableApprovalLedger(ws_comp / "pre_a.sqlite3", now=clock)
        p_pre = DurablePrivacyConsentStore(ws_comp / "pre_p.sqlite3")
        svc_pre = build_ibpc_service(
            IBPCServiceDeps(
                queue=_StubQ(),
                registry=build_runtime_registry(),
                workspace_root=ws_comp,
                revision_provider=lambda: 0,
                durable=True,
                session_store=s_pre,
                approval_ledger=a_pre,
                privacy_store=p_pre,
                authorized_asset_root=ws_comp,
                now=clock,
            ),
            key_provider=ring,
        )
        svc_pre.close()
        s_pre.close()
        a_pre.close()
        p_pre.close()

        # 3. studio_principal_auth.py: idempotent close + bind_channel when principal absent.
        auth_tmp = StudioPrincipalAuthStore(tmp_path / "auth_extra.sqlite3", now=clock)
        with pytest.raises(AuthorizationError, match="principal_not_found"):
            auth_tmp.bind_channel(
                "prn_" + "1" * 32,
                channel="studio",
                external_subject="sub-orphan",
                allow_system_bootstrap=True,
            )
        auth_tmp.close()
        auth_tmp.close()

        # 4. studio.py: get_studio_submission_service + download_studio_artifact missing-config
        # cleanup.
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q_studio.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)
        sub_svc = studio_api.get_studio_submission_service()
        assert sub_svc is not None

        # 5. privacy.py: _parse_utc invalid/naive, list_consents(None), record_consent/has_consent
        # edge cases.
        with pytest.raises(PrivacyViolation, match="invalid_timestamp"):
            _parse_utc("not-a-date")
        with pytest.raises(PrivacyViolation, match="invalid_timestamp"):
            _parse_utc("2026-11-26T00:00:00")
        pstore = DurablePrivacyConsentStore(tmp_path / "priv_cov.sqlite3")
        assert pstore.list_consents() == ()
        pol = PrivacyPolicy(now=clock, store=pstore, configured_providers=frozenset({"gemini"}))
        with pytest.raises(PrivacyViolation, match="invalid_mode"):
            pol.require_local("BAD", "audio")  # type: ignore[arg-type]
        with pytest.raises(PrivacyViolation, match="invalid_mode"):
            pol.record_consent("BAD", principal_id=OWNER_ID, scope="s")  # type: ignore[arg-type]
        with pytest.raises(PrivacyViolation, match="consent_missing"):
            pol.check_egress(PrivacyMode.TRUSTED_CLOUD, "audio", consented=False)
        pstore.grant_consent(
            consent_id="cns_cov_1",
            project_id="p-cov",
            tenant_id="ten-1",
            principal_id=OWNER_ID,
            mode=PrivacyMode.TRUSTED_CLOUD,
            processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value,
            data_category=DataCategory.TRANSCRIPT.value,
            provider_id="gemini",
            granted_at_utc=clock(),
            expires_at_utc="2026-11-26T12:00:00Z",
        )
        # Check exact boundary expiry (now == expires_at_utc).
        clock.current = "2026-11-26T12:00:00Z"
        with pytest.raises(PrivacyViolation, match="consent_expired"):
            pol.enforce_processing_consent(
                project_id="p-cov",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                data_category=DataCategory.TRANSCRIPT,
                provider_id="gemini",
            )
        pstore.close()
        pstore.close()

        # 6. approval.py: identity mismatch branches, job reservation & key_id branches.
        with DurableApprovalLedger(tmp_path / "appr_cov.sqlite3", now=clock) as ledger:
            ident = ApprovalIdentity(
                principal_id=OWNER_ID,
                tenant_id="ten-1",
                project_id="proj-1",
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                source_revision=0,
                source_asset_digest="sha256:" + "1" * 64,
                ast_digest="sha256:" + "2" * 64,
                plan_digest="sha256:" + "3" * 64,
                preview_digest="sha256:" + "4" * 64,
                revision_id="0",
                scope_signature="sha256:" + "5" * 64,
                expires_at_utc="2026-11-26T13:00:00Z",
            )
            b = ApprovalBinding(
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id=OWNER_ID,
                source_revision=0,
                source_asset_digest=ident.source_asset_digest,
                ast_digest=ident.ast_digest,
                plan_digest=ident.plan_digest,
                preview_digest=ident.preview_digest,
                revision_id="0",
                scope_signature=ident.scope_signature or "",
                expires_at_utc="2026-11-26T13:00:00Z",
                key_id="k1",
            )
            stored = ledger.create(b)
            for field, bad_val in (
                ("principal_id", EDITOR_ID),
                ("project_id", "proj-2"),
                ("plan_digest", "sha256:" + "9" * 64),
                ("preview_digest", "sha256:" + "9" * 64),
                ("revision_id", "9"),
                ("expires_at_utc", "2099-01-01T00:00:00Z"),
                ("scope_signature", "sha256:" + "9" * 64),
            ):
                bad_ident = dataclasses.replace(ident, **{field: bad_val})
                with pytest.raises(ApprovalError):
                    ledger.approve(
                        stored.approval_id, identity=bad_ident, turn_is_active=lambda: True
                    )
            ledger.approve(
                stored.approval_id, identity=ident, turn_is_active=lambda: True, key_id="k1"
            )
            ledger.consume(
                stored.approval_id, identity=ident, job_id="job-r1", attempt=1, key_provider=ring
            )
            # Recovery with higher attempt, stale attempt, and finalization.
            ledger.consume(
                stored.approval_id, identity=ident, job_id="job-r1", attempt=2, key_provider=ring
            )
            with pytest.raises(ApprovalError, match="stale_attempt"):
                ledger.consume(
                    stored.approval_id,
                    identity=ident,
                    job_id="job-r1",
                    attempt=1,
                    key_provider=ring,
                )
            ledger.assert_active_reservation(
                stored.approval_id, job_id="job-r1", attempt=2, identity=ident
            )
            ledger.finalize_consumption(stored.approval_id, job_id="job-r1", attempt=2)
            with pytest.raises(ApprovalError, match="replay"):
                ledger.consume(
                    stored.approval_id,
                    identity=ident,
                    job_id="job-r1",
                    attempt=3,
                    key_provider=ring,
                )
        ledger.close()  # already closed via __exit__

        # 7. session.py: close/get_turn/stale_revision/list_turn_contexts branches.
        with DurableLiveSessionStore(tmp_path / "sess_cov.sqlite3") as sstore:
            mgr = LiveSessionManager(now=clock, privacy=PrivacyPolicy(now=clock), store=sstore)
            sess = mgr.open_session(
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                active_revision_id="0",
            )
            t = mgr.begin_turn(
                sess.session_id,
                "t-1",
                sequence_number=1,
                project_revision=0,
                target_revision_id="0",
            )
            assert mgr.get_turn(sess.session_id, "t-1").turn_id == t.turn_id
            mgr.mark_intent_parsed(sess.session_id, "t-1", intent_digest="sha256:" + "1" * 64)
            mgr.mark_plan_preview(
                sess.session_id, "t-1", shadow_preview_digest="sha256:" + "2" * 64
            )
            # Bumping active_revision_id in store makes mark_awaiting_approval/mark_approved raise
            # StaleRevisionError.
            with sstore._transaction():
                sstore._conn.execute(
                    "UPDATE nexus_ibpc_sessions SET active_revision_id = '1' WHERE session_id = ?",
                    (sess.session_id,),
                )
            with pytest.raises(StaleRevisionError):
                mgr.mark_awaiting_approval(sess.session_id, "t-1", approval_id="appr-1")
            with sstore._transaction():
                sstore._conn.execute(
                    "UPDATE nexus_ibpc_sessions SET active_revision_id = '0' WHERE session_id = ?",
                    (sess.session_id,),
                )
            mgr.mark_awaiting_approval(sess.session_id, "t-1", approval_id="appr-1")
            with sstore._transaction():
                sstore._conn.execute(
                    "UPDATE nexus_ibpc_sessions SET active_revision_id = '1' WHERE session_id = ?",
                    (sess.session_id,),
                )
            with pytest.raises(StaleRevisionError):
                mgr.mark_approved(sess.session_id, "t-1")
            # Non-dict JSON in turn_contexts skipped gracefully.
            with sstore._transaction():
                sstore._conn.execute(
                    "INSERT INTO nexus_ibpc_turn_contexts "
                    "(approval_id, session_id, turn_id, context_json, updated_at) "
                    "VALUES ('bad_ctx', ?, 't-1', '[1, 2]', ?)",
                    (sess.session_id, clock()),
                )
            assert sstore.load_turn_context("bad_ctx") is None
            assert isinstance(sstore.list_turn_contexts(), tuple)
        sstore.close()

        # 8. service.py & studio_ibpc.py: remaining HTTP/service error mapping branches.
        import base64 as _b64

        _stub_media_tools(monkeypatch)
        ws_cov = tmp_path / "ws_cov"
        ws_cov.mkdir()
        clip_cov = ws_cov / "clip.mp4"
        _fake_media(clip_cov)
        auth_cov_db = tmp_path / "auth_cov.sqlite3"
        with StudioPrincipalAuthStore(auth_cov_db, now=clock) as ac:
            ac.ensure_project("proj-cov", tenant_id="ten-1")
            ac.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-cov")
            ac.bind_channel(
                OWNER_ID,
                channel="studio",
                external_subject="sub-owner-cov",
                allow_system_bootstrap=True,
            )
            ac.grant_membership("proj-cov", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
            ac.ensure_project("proj-t2", tenant_id="ten-2")
            t2_p = "prn_" + "d" * 32
            ac.register_principal(t2_p, tenant_id="ten-2", raw_token="tok-t2-cov")
            ac.bind_channel(
                t2_p, channel="studio", external_subject="sub-t2-cov", allow_system_bootstrap=True
            )
            ac.grant_membership("proj-t2", t2_p, role=StudioRole.OWNER, granted_by=t2_p)

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-cov,proj-t2")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws_cov))
        monkeypatch.delenv("NEXUS_STUDIO_ASSET_ROOT", raising=False)
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_cov_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q_cov.sqlite3"))
        monkeypatch.setenv(
            "NEXUS_IBPC_APPROVAL_KEY_RING",
            json.dumps({"k1": _b64.b64encode(b"k" * 32).decode("ascii")}),
        )
        monkeypatch.setenv("NEXUS_IBPC_APPROVAL_ACTIVE_KEY_ID", "k1")
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        h1 = {"Authorization": "Bearer deploy-tok", "X-Nexus-Principal-Token": "tok-owner-cov"}
        h2 = {"Authorization": "Bearer deploy-tok", "X-Nexus-Principal-Token": "tok-t2-cov"}

        r_s1 = client.post("/api/studio/projects/proj-cov/ibpc/sessions", headers=h1, json={})
        assert r_s1.status_code == 201
        sid1 = r_s1.json()["session_id"]

        r_t1 = client.post(
            f"/api/studio/projects/proj-cov/ibpc/sessions/{sid1}/turns",
            headers=h1,
            json={
                "turn_id": "t-1",
                "sequence_number": 1,
                "text": "trim from 1 to 2 seconds",
                "asset_id": "clip",
                "source_path": str(clip_cov),
            },
        )
        assert r_t1.status_code == 201
        aid1 = r_t1.json()["approval_id"]

        # Cross-tenant GET on same session fails with AuthorizationError("tenant_mismatch") -> 403!
        # Give t2_p OWNER on proj-cov? Wait: cross-tenant membership is denied; instead test
        # approve/execute with wrong project -> 403 via proj-t2? Use monkeypatch on
        # get_project_tenant
        # or test approve/execute on proj-cov when service raises AuthorizationError.
        with monkeypatch.context() as mp:
            mp.setattr(
                StudioPrincipalAuthStore,
                "get_project_tenant",
                lambda *_a, **_kw: (_ for _ in ()).throw(
                    AuthorizationError("project_not_found", "missing")
                ),
            )
            assert (
                client.post(
                    "/api/studio/projects/proj-cov/ibpc/sessions", headers=h1, json={}
                ).status_code
                == 403
            )
        with monkeypatch.context() as mp:
            mp.setattr(
                StudioPrincipalAuthStore,
                "get_project_tenant",
                lambda *_a, **_kw: "ten-other",
            )
            assert (
                client.post(
                    "/api/studio/projects/proj-cov/ibpc/sessions", headers=h1, json={}
                ).status_code
                == 403
            )
        with monkeypatch.context() as mp:
            mp.setattr(
                service_mod.IBPCService,
                "get_session",
                lambda *_a, **_kw: (_ for _ in ()).throw(
                    AuthorizationError("tenant_mismatch", "wrong tenant")
                ),
            )
            assert (
                client.get(
                    f"/api/studio/projects/proj-cov/ibpc/sessions/{sid1}", headers=h1
                ).status_code
                == 403
            )
        with monkeypatch.context() as mp:
            mp.setattr(
                service_mod.IBPCService,
                "approve",
                lambda *_a, **_kw: (_ for _ in ()).throw(
                    AuthorizationError("tenant_mismatch", "wrong tenant")
                ),
            )
            assert (
                client.post(
                    f"/api/studio/projects/proj-cov/ibpc/sessions/{sid1}/turns/t-1/approve",
                    headers=h1,
                    json={"approval_id": aid1},
                ).status_code
                == 403
            )
        with monkeypatch.context() as mp:

            async def _auth_err(*_a, **_kw):
                raise AuthorizationError("tenant_mismatch", "wrong tenant")

            mp.setattr(service_mod.IBPCService, "execute", _auth_err)
            assert (
                client.post(
                    f"/api/studio/projects/proj-cov/ibpc/sessions/{sid1}/turns/t-1/execute",
                    headers=h1,
                    json={"approval_id": aid1},
                ).status_code
                == 403
            )
        # Also test service.py open_session without project_revision_resolver (in-memory).
        mem_svc = build_ibpc_service(
            IBPCServiceDeps(
                queue=_StubQ(),
                registry=build_runtime_registry(),
                workspace_root=ws_cov,
                revision_provider=lambda: 0,
                durable=False,
                now=clock,
            ),
            key_provider=ring,
        )
        with pytest.raises(StaleRevisionError, match="revision_mismatch"):
            mem_svc.open_session(
                tenant_id="ten-1",
                project_id="proj-m",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                base_revision_id="99",
            )
        mem_svc.close()
        assert h2 is not None

        # 9. Remaining branch closures across privacy, approval, studio, studio_ibpc, session,
        # service.
        # studio.py line 205: authorized.close() on StoryboardJobConfigError after
        # authorize_artifact_download.
        from nexus_ai_agent.creative.storyboard_jobs import StoryboardJobConfigError

        class _DummyAuth:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        dummy_auth = _DummyAuth()
        with monkeypatch.context() as mp:

            async def _fake_get_job(_jid):
                return {
                    "project_id": "proj-cov",
                    "canonical_status": "COMPLETED",
                    "result": {"artifact_verification": {"media_verdict": "PASS"}},
                }

            mp.setattr(
                app_module,
                "get_creative_registry",
                lambda: types.SimpleNamespace(get_job=_fake_get_job),
            )
            mp.setattr(studio_api, "artifact_rel_path_for_job", lambda *_a: "out.mp4")
            mp.setattr(studio_api, "studio_settings", lambda *_a: (ws_cov, b"k" * 32))
            mp.setattr(studio_api, "authorize_artifact_download", lambda *_a: dummy_auth)
            mp.setattr(
                studio_api,
                "iter_authorized_chunks",
                lambda *_a: (_ for _ in ()).throw(StoryboardJobConfigError("bad cfg")),
            )
            with pytest.raises(studio_api.HTTPException) as exc_dl:
                await studio_api.download_studio_artifact("proj-cov", "job-1")
            assert exc_dl.value.status_code == 503
            assert dummy_auth.closed

        # studio_ibpc.py lines 271 & 501: open_session StaleRevisionError->409 & execute
        # refused->409.
        with monkeypatch.context() as mp:
            mp.setattr(
                service_mod.IBPCService,
                "open_session",
                lambda *_a, **_kw: (_ for _ in ()).throw(
                    StaleRevisionError("stale_revision", "stale")
                ),
            )
            assert (
                client.post(
                    "/api/studio/projects/proj-cov/ibpc/sessions", headers=h1, json={}
                ).status_code
                == 409
            )
        with monkeypatch.context() as mp:
            from nexus_ai_agent.nagar.ibpc.service import ExecutionOutcome

            async def _refused(*_a, **_kw):
                return ExecutionOutcome(status="refused", detail="refused_test")

            mp.setattr(service_mod.IBPCService, "execute", _refused)
            assert (
                client.post(
                    f"/api/studio/projects/proj-cov/ibpc/sessions/{sid1}/turns/t-1/execute",
                    headers=h1,
                    json={"approval_id": aid1},
                ).status_code
                == 409
            )

        # privacy.py remaining branches: consents property, check_egress no_provider_wired,
        # has_consent active & in-memory, grant_durable_consent store_unconfigured/invalid
        # scope/category.
        pol_mem = PrivacyPolicy(now=clock, store=None, configured_providers=frozenset())
        with pytest.raises(PrivacyViolation, match="no_provider_wired"):
            pol_mem.check_egress(PrivacyMode.TRUSTED_CLOUD, "audio", consented=True, provider_id="")
        pol_mem.record_consent(PrivacyMode.TRUSTED_CLOUD, principal_id=OWNER_ID, scope="transcript")
        assert pol_mem.has_consent(
            PrivacyMode.TRUSTED_CLOUD, principal_id=OWNER_ID, scope="transcript"
        )
        with pytest.raises(PrivacyViolation, match="store_unconfigured"):
            pol_mem.grant_durable_consent(
                project_id="p",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                data_category=DataCategory.TRANSCRIPT,
            )
        pstore2 = DurablePrivacyConsentStore(tmp_path / "priv_cov2.sqlite3")
        pol2 = PrivacyPolicy(now=clock, store=pstore2, configured_providers=frozenset({"gemini"}))
        with pytest.raises(PrivacyViolation, match="invalid_scope"):
            pol2.grant_durable_consent(
                project_id="p",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope="NOPE",
                data_category=DataCategory.TRANSCRIPT,
            )
        with pytest.raises(PrivacyViolation, match="invalid_category"):
            pol2.grant_durable_consent(
                project_id="p",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                data_category="NOPE",
            )
        pol2.grant_durable_consent(
            project_id="p-cov2",
            principal_id=OWNER_ID,
            mode=PrivacyMode.TRUSTED_CLOUD,
            processing_scope=ProcessingScope.LOCAL_PROCESSING,
            data_category=DataCategory.AUDIO,
        )
        assert len(pol2.consents) == 1
        assert pol2.has_consent(
            PrivacyMode.TRUSTED_CLOUD,
            principal_id=OWNER_ID,
            scope="audio",
            project_id="p-cov2",
            processing_scope=ProcessingScope.LOCAL_PROCESSING.value,
        )
        pstore2.close()

        # approval.py legacy schema migration (approvals without key_id, reservations without
        # last_attempt),
        # revoke on CONSUMED, finalize_consumption errors, no-key_provider approve/consume.
        mig_db = tmp_path / "legacy_appr.sqlite3"
        conn = sqlite3.connect(str(mig_db))
        conn.execute(
            "CREATE TABLE approvals (approval_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
            " turn_id TEXT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE approval_reservations (approval_id TEXT PRIMARY KEY, job_id"
            " TEXT NOT NULL, fingerprint TEXT NOT NULL, finalized_at TEXT)"
        )
        conn.commit()
        conn.close()
        with DurableApprovalLedger(mig_db, now=clock) as leg:
            cols = {r[1] for r in leg._conn.execute("PRAGMA table_info(approvals)")}
            assert "key_id" in cols
            ident2 = ApprovalIdentity(
                principal_id=OWNER_ID,
                tenant_id="ten-1",
                project_id="proj-1",
                session_id="sess-2",
                turn_id="turn-2",
                sequence_number=1,
                source_revision=0,
                source_asset_digest="sha256:" + "1" * 64,
                ast_digest="sha256:" + "2" * 64,
                plan_digest="sha256:" + "3" * 64,
                preview_digest="sha256:" + "4" * 64,
            )
            b2 = ApprovalBinding(
                session_id="sess-2",
                turn_id="turn-2",
                sequence_number=1,
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id=OWNER_ID,
                source_revision=0,
                source_asset_digest=ident2.source_asset_digest,
                ast_digest=ident2.ast_digest,
                plan_digest=ident2.plan_digest,
                preview_digest=ident2.preview_digest,
            )
            st2 = leg.create(b2)
            leg.approve(st2.approval_id, identity=ident2, turn_is_active=lambda: True, key_id="k1")
            leg.consume(
                st2.approval_id, identity=ident2, job_id="j-2", attempt=1, key_provider=ring
            )
            with pytest.raises(ApprovalError, match="replay"):
                leg.revoke(st2.approval_id)
            with pytest.raises(ApprovalError, match="replay"):
                leg.finalize_consumption(st2.approval_id, job_id="other-job", attempt=1)

        # service.py helpers: _attempt_identity (async, sync, exc), _provenance_reference, _drain,
        # execute error paths.
        async def _async_attempts(_jid):
            return [{"attempt": 1}, {"attempt": 3}, "ignored"]

        svc_helper = build_ibpc_service(
            IBPCServiceDeps(
                queue=_StubQ(),
                registry=build_runtime_registry(),
                workspace_root=ws_cov,
                revision_provider=lambda: 0,
                attempt_provider=_async_attempts,
                journal=types.SimpleNamespace(
                    records_for_job=lambda _j: (_ for _ in ()).throw(RuntimeError("j-err")),
                    all_records=lambda: (),
                ),
                facts_provider=object(),
                now=clock,
            ),
            key_provider=ring,
        )
        assert await svc_helper._attempt_identity("job-x") == "job-x:attempt3"
        svc_helper._deps = dataclasses.replace(
            svc_helper._deps,
            attempt_provider=lambda _j: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert await svc_helper._attempt_identity("job-x") == "job-x:attempt?"
        prov_ref = svc_helper._provenance_reference("job-x")
        assert "error" in prov_ref["journal"] and "error" in prov_ref["passport"]
        assert await svc_helper._drain("job-x", timeout=0.0) is None
        svc_helper.close()

    @pytest.mark.asyncio
    async def test_wave3_remaining_branches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.api import studio as studio_api
        from nexus_ai_agent.config.settings import get_settings
        from nexus_ai_agent.creative.storyboard_jobs import StoryboardJobConfigError
        from nexus_ai_agent.nagar.ibpc.approval import (
            ApprovalAlreadyConsumedError,
            ApprovalBinding,
            ApprovalIdentity,
            DurableApprovalLedger,
        )
        from nexus_ai_agent.nagar.ibpc.composition import build_ibpc_service
        from nexus_ai_agent.nagar.ibpc.models import (
            DataCategory,
            PrivacyMode,
            ProcessingScope,
        )
        from nexus_ai_agent.nagar.ibpc.privacy import (
            DurablePrivacyConsentStore,
            PrivacyPolicy,
            PrivacyViolation,
        )
        from nexus_ai_agent.nagar.ibpc.session import (
            DurableLiveSessionStore,
            LiveSessionManager,
            SessionError,
            SessionStatus,
        )

        clock = _Clock()
        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")

        # 1. studio.py: StoryboardJobConfigError BEFORE authorize_artifact_download (authorized is
        # None).
        with monkeypatch.context() as mp:

            async def _fake_get_job(_jid):
                return {
                    "project_id": "proj-cov",
                    "canonical_status": "COMPLETED",
                    "result": {"artifact_verification": {"media_verdict": "PASS"}},
                }

            mp.setattr(
                app_module,
                "get_creative_registry",
                lambda: types.SimpleNamespace(get_job=_fake_get_job),
            )
            mp.setattr(studio_api, "artifact_rel_path_for_job", lambda *_a: "out.mp4")
            mp.setattr(
                studio_api,
                "studio_settings",
                lambda *_a: (_ for _ in ()).throw(StoryboardJobConfigError("unconfigured")),
            )
            with pytest.raises(studio_api.HTTPException) as exc_dl:
                await studio_api.download_studio_artifact("proj-cov", "job-1")
            assert exc_dl.value.status_code == 503

        # 2. privacy.py: legacy schema migration (missing tenant_id), __enter__/__exit__,
        # in-memory consents property, check_egress success, require_local_biometric_consent=False.
        priv_mig_db = tmp_path / "priv_mig.sqlite3"
        c_p = sqlite3.connect(str(priv_mig_db))
        c_p.execute(
            """
            CREATE TABLE nexus_privacy_consents (
                consent_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                processing_scope TEXT NOT NULL,
                data_category TEXT NOT NULL,
                provider_id TEXT NOT NULL DEFAULT '',
                granted_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL DEFAULT '',
                revoked_at_utc TEXT NOT NULL DEFAULT '',
                revoked_by TEXT NOT NULL DEFAULT '',
                revoke_reason TEXT NOT NULL DEFAULT ''
            )
            """
        )
        c_p.commit()
        c_p.close()
        with DurablePrivacyConsentStore(priv_mig_db) as ps_mig:
            cols_p = {
                r[1] for r in ps_mig._conn.execute("PRAGMA table_info(nexus_privacy_consents)")
            }
            assert "tenant_id" in cols_p
        ps_mem = PrivacyPolicy(now=clock, store=None, configured_providers=frozenset({"gemini"}))
        ps_mem.record_consent(PrivacyMode.TRUSTED_CLOUD, principal_id=OWNER_ID, scope="audio")
        assert len(ps_mem.consents) == 1
        ps_mem.check_egress(
            PrivacyMode.TRUSTED_CLOUD, "audio", consented=True, provider_id="gemini"
        )
        assert (
            ps_mem.enforce_processing_consent(
                project_id="p",
                principal_id=OWNER_ID,
                mode=PrivacyMode.LOCAL_VAULT,
                processing_scope=ProcessingScope.LOCAL_PROCESSING,
                data_category=DataCategory.BIOMETRIC_FACE_ROI,
                require_local_biometric_consent=False,
            )
            is None
        )

        # 3. approval.py: _check_identity field mismatches (principal/project/plan/preview),
        #    reservation identity mismatch on consume & assert_active_reservation, stale_attempt,
        #    and finalize_consumption ApprovalAlreadyConsumedError.
        with DurableApprovalLedger(tmp_path / "appr_rem.sqlite3", now=clock) as leg:
            ident = ApprovalIdentity(
                principal_id=OWNER_ID,
                tenant_id="ten-1",
                project_id="proj-1",
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                source_revision=0,
                source_asset_digest="sha256:" + "1" * 64,
                ast_digest="sha256:" + "2" * 64,
                plan_digest="sha256:" + "3" * 64,
                preview_digest="sha256:" + "4" * 64,
            )
            b = ApprovalBinding(
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id=OWNER_ID,
                source_revision=0,
                source_asset_digest=ident.source_asset_digest,
                ast_digest=ident.ast_digest,
                plan_digest=ident.plan_digest,
                preview_digest=ident.preview_digest,
            )
            for field, val in (
                ("principal_id", EDITOR_ID),
                ("project_id", "proj-other"),
                ("plan_digest", "sha256:" + "9" * 64),
                ("preview_digest", "sha256:" + "9" * 64),
            ):
                with pytest.raises(ApprovalError):
                    leg._check_identity(b, dataclasses.replace(ident, **{field: val}))
            st = leg.create(b)
            leg.approve(st.approval_id, identity=ident, turn_is_active=lambda: True)
            leg.consume(st.approval_id, identity=ident, job_id="job-1", attempt=2)
            with pytest.raises(ApprovalError, match="stale_attempt"):
                leg.assert_active_reservation(
                    st.approval_id, job_id="job-1", attempt=1, identity=ident
                )
            with pytest.raises(ApprovalAlreadyConsumedError):
                leg.finalize_consumption(st.approval_id, job_id="job-1", attempt=1)
            # Mutate stored binding's principal_id while keeping identical identity_digest in
            # reservation
            # to exercise `except ApprovalError: raise ApprovalError("replay", "reserved identity
            # changed")`.
            tampered_b = st.model_copy(
                update={"state": st.state.CONSUMED, "principal_id": EDITOR_ID}
            )
            leg._store(tampered_b)
            with pytest.raises(ApprovalError, match="reserved identity changed"):
                leg.consume(st.approval_id, identity=ident, job_id="job-1", attempt=2)
            with pytest.raises(ApprovalError, match="reserved identity changed"):
                leg.assert_active_reservation(
                    st.approval_id, job_id="job-1", attempt=2, identity=ident
                )

        # 4. session.py: set_privacy_mode (wrong_principal + success), _cleanup_audio(None),
        #    turn_is_active False for stale/non-active, and IdempotencyConflictError in save_turn.
        with DurableLiveSessionStore(tmp_path / "sess_mig.sqlite3") as smig:
            mgr = LiveSessionManager(
                now=clock,
                privacy=PrivacyPolicy(now=clock, configured_providers=frozenset({"gemini"})),
                store=smig,
            )
            sess = mgr.open_session(
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
            )
            with pytest.raises(PrivacyViolation, match="wrong_principal"):
                mgr.set_privacy_mode(
                    sess.session_id,
                    PrivacyMode.TRUSTED_CLOUD,
                    principal_id=EDITOR_ID,
                    consent_scope="transcript",
                )
            mgr.set_privacy_mode(
                sess.session_id,
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                consent_scope="transcript",
            )
            t1 = mgr.begin_turn(
                sess.session_id,
                "t-1",
                sequence_number=1,
                project_revision=0,
                idempotency_key="idem-k",
                payload_digest="sha256:" + "1" * 64,
            )
            from nexus_ai_agent.nagar.ibpc.session import IdempotencyConflictError

            with pytest.raises(IdempotencyConflictError):
                mgr.begin_turn(
                    sess.session_id,
                    "t-1-dup",
                    sequence_number=2,
                    project_revision=0,
                    idempotency_key="idem-k",
                    payload_digest="sha256:" + "2" * 64,
                )
            t2 = mgr.begin_turn(sess.session_id, "t-2", sequence_number=2, project_revision=0)
            assert mgr.turn_is_active(sess.session_id, t1.turn_id) is False
            assert mgr.turn_is_active(sess.session_id, "missing") is False
            assert mgr.turn_is_active(sess.session_id, t2.turn_id) is True
            mgr.pause_session(sess.session_id)
            assert mgr.turn_is_active(sess.session_id, t2.turn_id) is False
            mgr._cleanup_audio(tmp_path, None)
            with monkeypatch.context() as mp:
                mp.setattr(os.path, "realpath", lambda _p: (_ for _ in ()).throw(OSError("io")))
                mgr._cleanup_audio(tmp_path, tmp_path / "any.wav")

        # 5. service.py: execute refusal paths (shadow_scope_mismatch, media_signature_mismatch,
        # source_changed_during_staging, media_evidence_missing, source_unreadable, _TurnContext
        # persistence).
        _stub_media_tools(monkeypatch)
        ws_svc = tmp_path / "ws_svc"
        ws_svc.mkdir()
        clip_svc = ws_svc / "clip.mp4"
        _fake_media(clip_svc)
        auth_svc, bref_svc = _setup_auth_store(tmp_path / "auth_svc.sqlite3", now=clock)

        class _StubQ2:
            async def enqueue(self, **_kw):
                return "job-s2"

            async def get_status(self, _j):
                return None

            async def get_result(self, _j):
                return None

        svc = build_studio_ibpc_service(
            queue=_StubQ2(),
            registry=build_runtime_registry(),
            workspace_root=ws_svc,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth_svc,
            channel_binding_ref=bref_svc,
            now=clock,
        )
        try:
            sid_a = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
            )
            res_a = svc.submit_text_intent(
                sid_a,
                "t-a",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_svc),
            )
            assert isinstance(res_a, PipelineResult)
            svc.approve(sid_a, "t-a", res_a.approval_id, principal_id=OWNER_ID)
            # media_evidence_missing (mutated during staging after scope_signature check)
            ctx_a = svc._contexts[res_a.approval_id]
            orig_sha256_fd = svc._sha256_fd
            with monkeypatch.context() as mp:

                def _sha_clear_manifest(p):
                    ctx_a.shadow_manifest = None
                    return orig_sha256_fd(p)

                mp.setattr(svc, "_sha256_fd", _sha_clear_manifest)
                out_miss = await svc.execute(sid_a, "t-a", res_a.approval_id, principal_id=OWNER_ID)
            assert out_miss.status == "refused" and "media_evidence_missing" in out_miss.detail

            # shadow_scope_mismatch (mutated during staging after scope_signature check)
            sid_b = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=2,
            )
            res_b = svc.submit_text_intent(
                sid_b,
                "t-b",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_svc),
            )
            assert isinstance(res_b, PipelineResult)
            svc.approve(sid_b, "t-b", res_b.approval_id, principal_id=OWNER_ID)
            ctx_b = svc._contexts[res_b.approval_id]
            with monkeypatch.context() as mp:

                def _sha_bad_manifest(p):
                    ctx_b.shadow_manifest = types.SimpleNamespace(
                        plan_digest="sha256:" + "f" * 64,
                        scope_digest=ctx_b.ast.scope.scope_digest,
                    )
                    return orig_sha256_fd(p)

                mp.setattr(svc, "_sha256_fd", _sha_bad_manifest)
                mp.setattr(service_mod, "verify_shadow_manifest", lambda _m: None)
                out_sm = await svc.execute(sid_b, "t-b", res_b.approval_id, principal_id=OWNER_ID)
            assert out_sm.status == "refused" and "shadow_scope_mismatch" in out_sm.detail

            # media_signature_mismatch
            sid_c = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=3,
            )
            res_c = svc.submit_text_intent(
                sid_c,
                "t-c",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_svc),
            )
            assert isinstance(res_c, PipelineResult)
            svc.approve(sid_c, "t-c", res_c.approval_id, principal_id=OWNER_ID)
            with monkeypatch.context() as mp:
                mp.setattr(
                    service_mod,
                    "compute_scope_signature",
                    lambda *_a, **_kw: types.SimpleNamespace(signature_digest="sha256:" + "9" * 64),
                )
                out_sig = await svc.execute(sid_c, "t-c", res_c.approval_id, principal_id=OWNER_ID)
                assert out_sig.status == "refused" and "media_signature_mismatch" in out_sig.detail

            # source_changed_during_staging
            sid_d = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=4,
            )
            res_d = svc.submit_text_intent(
                sid_d,
                "t-d",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_svc),
            )
            assert isinstance(res_d, PipelineResult)
            svc.approve(sid_d, "t-d", res_d.approval_id, principal_id=OWNER_ID)
            with monkeypatch.context() as mp:
                mp.setattr(svc, "_sha256_fd", lambda _p: "sha256:" + "8" * 64)
                out_chg = await svc.execute(sid_d, "t-d", res_d.approval_id, principal_id=OWNER_ID)
                assert (
                    out_chg.status == "refused"
                    and "source_changed_during_staging" in out_chg.detail
                )

            # _TurnContext.from_persisted_dict with verified_identity=None & approval_envelope=None
            d_ctx = svc._contexts[res_d.approval_id].to_persisted_dict()
            d_ctx["verified_identity"] = None
            d_ctx["approval_envelope"] = None
            d_ctx["shadow_manifest"] = None
            d_ctx["media_signature"] = None
            restored = service_mod._TurnContext.from_persisted_dict(d_ctx)
            assert restored.verified_identity is None and restored.approval_envelope is None
            assert restored.to_persisted_dict()["verified_identity"] is None

            # _stage_source OSError
            with pytest.raises(SessionError, match="source_unreadable"):
                svc._stage_source(ws_svc / "missing_file.mp4", ws_svc / "stg_err")
        finally:
            svc.close()
            auth_svc.close()

        # Verify _validate_source_path when authorized_asset_root is None (non-durable).
        nd_svc = service_mod.IBPCService(
            service_mod.IBPCServiceDeps(
                queue=_StubQ2(),
                registry=build_runtime_registry(),
                workspace_root=ws_svc,
                revision_provider=lambda: 0,
                durable=False,
                authorized_asset_root=None,
                now=clock,
            )
        )
        assert nd_svc._validate_source_path(str(clip_svc)) == clip_svc
        nd_svc.close()
        assert get_settings is not None
        assert build_ibpc_service is not None
        assert SessionStatus.ACTIVE.value == "ACTIVE"

    @pytest.mark.asyncio
    async def test_wave3_final_branch_closure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.api import studio as studio_api
        from nexus_ai_agent.nagar.ibpc.approval import (
            ApprovalBinding,
            ApprovalIdentity,
            DurableApprovalLedger,
        )
        from nexus_ai_agent.nagar.ibpc.models import PrivacyMode, digest_of
        from nexus_ai_agent.nagar.ibpc.privacy import PrivacyPolicy, PrivacyViolation
        from nexus_ai_agent.nagar.ibpc.session import (
            DurableLiveSessionStore,
            LiveSessionManager,
            SessionError,
        )

        clock = _Clock()
        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")

        # 1. studio.py: authorized.close() when DownloadRefused raised AFTER
        # authorize_artifact_download
        class _DummyAuth:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        dummy_auth = _DummyAuth()
        with monkeypatch.context() as mp:

            async def _fake_get_job(_jid):
                return {
                    "project_id": "proj-cov",
                    "canonical_status": "COMPLETED",
                    "result": {"artifact_verification": {"media_verdict": "PASS"}},
                }

            mp.setattr(
                app_module,
                "get_creative_registry",
                lambda: types.SimpleNamespace(get_job=_fake_get_job),
            )
            mp.setattr(studio_api, "artifact_rel_path_for_job", lambda *_a: "out.mp4")
            mp.setattr(studio_api, "studio_settings", lambda *_a: (tmp_path, b"k" * 32))
            mp.setattr(studio_api, "authorize_artifact_download", lambda *_a: dummy_auth)
            mp.setattr(
                studio_api,
                "iter_authorized_chunks",
                lambda *_a: (_ for _ in ()).throw(
                    studio_api.DownloadRefused(409, "stale_passport")
                ),
            )
            with pytest.raises(studio_api.HTTPException) as exc_dr:
                await studio_api.download_studio_artifact("proj-cov", "job-1")
            assert exc_dr.value.status_code == 409
            assert dummy_auth.closed is True
        with pytest.raises(studio_api.HTTPException) as exc_bad_id:
            await studio_api.download_studio_artifact("proj-cov", "../bad")
        assert exc_bad_id.value.status_code == 400

        # 2. approval.py: _check_identity lines 266/268/270/272 (matching identity_digest,
        # mismatched field),
        # idempotent consume on matching attempt (364->366), idempotent finalize_consumption
        # (450->exit),
        #    and stale_attempt in assert_active_reservation (418).
        with DurableApprovalLedger(tmp_path / "appr_final.sqlite3", now=clock) as leg:
            ident = ApprovalIdentity(
                principal_id=OWNER_ID,
                tenant_id="ten-1",
                project_id="proj-1",
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                source_revision=0,
                source_asset_digest="sha256:" + "1" * 64,
                ast_digest="sha256:" + "2" * 64,
                plan_digest="sha256:" + "3" * 64,
                preview_digest="sha256:" + "4" * 64,
            )
            for field, bad_val in (
                ("principal_id", EDITOR_ID),
                ("project_id", "proj-other"),
                ("plan_digest", "sha256:" + "9" * 64),
                ("preview_digest", "sha256:" + "9" * 64),
            ):
                b_bad = ApprovalBinding(
                    session_id="sess-1",
                    turn_id="turn-1",
                    sequence_number=1,
                    tenant_id="ten-1",
                    project_id="proj-1",
                    principal_id=OWNER_ID,
                    source_revision=0,
                    source_asset_digest=ident.source_asset_digest,
                    ast_digest=ident.ast_digest,
                    plan_digest=ident.plan_digest,
                    preview_digest=ident.preview_digest,
                )
                forced_digest = digest_of(
                    {
                        "schema_id": b_bad.schema_id,
                        "approval_id": b_bad.approval_id,
                        "nonce": b_bad.nonce,
                        **ident.as_dict(),
                    }
                )
                b_bad = b_bad.model_copy(
                    update={"identity_digest": forced_digest, "approval_hash": "", field: bad_val}
                )
                with pytest.raises(ApprovalError, match="identity_mismatch_"):
                    leg._check_identity(b_bad, ident)

            b_ok = ApprovalBinding(
                session_id="sess-1",
                turn_id="turn-1",
                sequence_number=1,
                tenant_id="ten-1",
                project_id="proj-1",
                principal_id=OWNER_ID,
                source_revision=0,
                source_asset_digest=ident.source_asset_digest,
                ast_digest=ident.ast_digest,
                plan_digest=ident.plan_digest,
                preview_digest=ident.preview_digest,
            )
            st_ok = leg.create(b_ok)
            leg.approve(st_ok.approval_id, identity=ident, turn_is_active=lambda: True)
            leg.consume(st_ok.approval_id, identity=ident, job_id="job-1", attempt=2)
            # Same attempt again (364->366)
            leg.consume(st_ok.approval_id, identity=ident, job_id="job-1", attempt=2)
            # assert_active_reservation with attempt=1 raises stale_attempt
            with pytest.raises(ApprovalError, match="stale_attempt"):
                leg.assert_active_reservation(
                    st_ok.approval_id, job_id="job-1", attempt=1, identity=ident
                )
            with pytest.raises(ApprovalError, match="replay"):
                leg.assert_active_reservation(
                    st_ok.approval_id, job_id="wrong-job", attempt=2, identity=ident
                )
            # Idempotent finalize_consumption (second call skips write when finalized_at is set)
            leg.finalize_consumption(st_ok.approval_id, job_id="job-1", attempt=2)
            leg.finalize_consumption(st_ok.approval_id, job_id="job-1", attempt=2)

        # 3. session.py: close() idempotency, _on_revoke_session callback, get_turn unknown,
        #    submit_partial_transcript twice, mark_failed on terminal turn.
        revoked_sessions: list[str] = []
        sm_final = DurableLiveSessionStore(tmp_path / "sess_final.sqlite3")
        assert sm_final.path == tmp_path / "sess_final.sqlite3"
        mgr_f = LiveSessionManager(
            now=clock,
            privacy=PrivacyPolicy(now=clock),
            store=sm_final,
            on_revoke_session=revoked_sessions.append,
        )
        s_f = mgr_f.open_session(
            tenant_id="ten-1", project_id="proj-1", principal_id=OWNER_ID, user_id=1, chat_id=1
        )
        t_f = mgr_f.begin_turn(
            s_f.session_id,
            "t-f",
            sequence_number=1,
            project_revision=0,
            idempotency_key="k-f",
            payload_digest="sha256:" + "1" * 64,
        )
        t_f_dup = mgr_f.begin_turn(
            s_f.session_id,
            "t-f-dup",
            sequence_number=1,
            project_revision=0,
            idempotency_key="k-f",
            payload_digest="sha256:" + "1" * 64,
        )
        assert t_f_dup.turn_id == t_f.turn_id
        sm_final._conn.execute(
            "UPDATE nexus_ibpc_turns SET turn_state='TRANSCRIBING' WHERE turn_id=?", ("t-f",)
        )
        sm_final._conn.commit()
        mgr_f.submit_partial_transcript(s_f.session_id, t_f.turn_id, "trim")
        with pytest.raises(SessionError, match="unknown_turn"):
            mgr_f.get_turn(s_f.session_id, "no-such-turn")
        with pytest.raises(SessionError, match="unknown_turn"):
            await mgr_f.submit_audio(
                s_f.session_id,
                "no-such-turn",
                b"audio",
                engine=types.SimpleNamespace(),
                audio_dir=tmp_path,
            )
        # EXECUTING turn prevents close_session
        sm_final._conn.execute(
            "UPDATE nexus_ibpc_turns SET turn_state='EXECUTING' WHERE turn_id=?", ("t-f",)
        )
        sm_final._conn.commit()
        from nexus_ai_agent.nagar.ibpc.session import TransitionError

        with pytest.raises(TransitionError, match="executing"):
            mgr_f.close_session(s_f.session_id)
        sm_final._conn.execute(
            "UPDATE nexus_ibpc_turns SET turn_state='TRANSCRIPT_PARTIAL' WHERE turn_id=?", ("t-f",)
        )
        sm_final._conn.commit()
        mgr_f.mark_failed(s_f.session_id, t_f.turn_id)
        # Second mark_failed on already-FAILED turn (1430->1432)
        mgr_f.mark_failed(s_f.session_id, t_f.turn_id)
        mgr_f.close_session(s_f.session_id)
        assert revoked_sessions == [s_f.session_id]
        sm_final.close()
        sm_final.close()  # idempotent second close

        # Non-durable LiveSessionManager _persist_session
        mgr_nd = LiveSessionManager(now=clock, privacy=PrivacyPolicy(now=clock), store=None)
        s_nd = mgr_nd.open_session(
            tenant_id="ten-1", project_id="proj-1", principal_id=OWNER_ID, user_id=1, chat_id=1
        )
        mgr_nd.pause_session(s_nd.session_id)

        # 4. service.py: _load_or_get_context (session_store=None, terminal binding, missing row),
        #    _validate_source_path lstat OSError, turn_state not APPROVED on execute,
        #    revision_id mismatch in _resolve_identity.
        _stub_media_tools(monkeypatch)
        ws_f = tmp_path / "ws_final"
        ws_f.mkdir()
        clip_f = ws_f / "clip.mp4"
        _fake_media(clip_f)
        auth_f, bref_f = _setup_auth_store(tmp_path / "auth_final.sqlite3", now=clock)

        class _StubQF:
            async def enqueue(self, **_kw):
                return "job-f"

            async def get_status(self, _j):
                return None

            async def get_result(self, _j):
                return None

        nd_f = service_mod.IBPCService(
            service_mod.IBPCServiceDeps(
                queue=_StubQF(),
                registry=build_runtime_registry(),
                workspace_root=ws_f,
                revision_provider=lambda: 0,
                durable=False,
                authorized_asset_root=ws_f,
                owned_closables=(object(),),
                now=clock,
            )
        )
        assert nd_f._load_or_get_context("missing") is None
        s_nd_svc = nd_f.open_session(
            tenant_id="ten-1",
            project_id="proj-w3",
            principal_id=OWNER_ID,
            user_id=1,
            chat_id=1,
            base_revision_id="0",
        )
        assert (
            nd_f._validate_session_binding(nd_f._sessions.get_session(s_nd_svc), action=None)
            == OWNER_ID
        )
        with pytest.raises(SessionError, match="workspace_prefix"):
            nd_f._guarded_workspace("../../escape")
        with monkeypatch.context() as mp:
            mp.setattr(service_mod, "WORKSPACE_PREFIX", "../../")
            with pytest.raises(SessionError, match="workspace_escape"):
                nd_f._guarded_workspace("appr")
        with pytest.raises(PrivacyViolation):
            nd_f.revoke_privacy_consent("c-none", actor_id=OWNER_ID, project_id="proj-w3")
        with monkeypatch.context() as mp:
            mp.setattr(os, "lstat", lambda _p: (_ for _ in ()).throw(PermissionError("denied")))
            with pytest.raises(AuthorizationError, match="untrusted_source_path"):
                nd_f._validate_source_path(str(clip_f))
        nd_f.close()

        svc_f = build_studio_ibpc_service(
            queue=_StubQF(),
            registry=build_runtime_registry(),
            workspace_root=ws_f,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth_f,
            channel_binding_ref=bref_f,
            now=clock,
        )
        try:
            sid_f = svc_f.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                base_revision_id="0",
            )
            res_f = svc_f.submit_text_intent(
                sid_f,
                "t-1",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_f),
            )
            assert isinstance(res_f, PipelineResult)
            svc_f.approve(sid_f, "t-1", res_f.approval_id, principal_id=OWNER_ID)
            # Force turn state to AWAITING_APPROVAL (non-terminal, non-APPROVED) -> hits line 1804!
            svc_f._session_store._conn.execute(
                "UPDATE nexus_ibpc_turns SET turn_state = 'AWAITING_APPROVAL' WHERE turn_id = ?",
                ("t-1",),
            )
            svc_f._session_store._conn.commit()
            out_bad_turn = await svc_f.execute(
                sid_f, "t-1", res_f.approval_id, principal_id=OWNER_ID
            )
            assert out_bad_turn.status == "refused" and "cannot execute" in out_bad_turn.detail

            # Terminal binding (CONSUMED) in _load_or_get_context
            svc_f._contexts.pop(res_f.approval_id, None)
            assert svc_f._load_or_get_context(res_f.approval_id) is None

            # Pending approval with deleted context row -> load_turn_context returns None
            sid_f2 = svc_f.open_session(
                tenant_id="ten-1", project_id="proj-w3", principal_id=OWNER_ID, user_id=1, chat_id=2
            )
            res_f2 = svc_f.submit_text_intent(
                sid_f2,
                "t-2",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_f),
                idempotency_key="idem-t2",
            )
            assert isinstance(res_f2, PipelineResult)
            svc_f._contexts.pop(res_f2.approval_id, None)
            svc_f._session_store._conn.execute(
                "DELETE FROM nexus_ibpc_turn_contexts WHERE approval_id = ?",
                (res_f2.approval_id,),
            )
            svc_f._session_store._conn.commit()
            assert svc_f._load_or_get_context(res_f2.approval_id) is None

            # submit_text_intent idempotency replay when existing_ctx is None -> falls through
            # (916->928)
            svc_f._session_store._conn.execute(
                "UPDATE nexus_ibpc_turns SET turn_state = 'TRANSCRIPT_FINAL' WHERE turn_id = ?",
                ("t-2",),
            )
            svc_f._session_store._conn.commit()
            res_f2_b = svc_f.submit_text_intent(
                sid_f2,
                "t-2",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_f),
                idempotency_key="idem-t2",
            )
            assert isinstance(res_f2_b, (service_mod.Refusal, PipelineResult))

            # base_approval_id with missing context -> unknown_base_proposal (958)
            sid_f2c = svc_f.open_session(
                tenant_id="ten-1",
                project_id="proj-w3",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=22,
            )
            res_base = svc_f.submit_text_intent(
                sid_f2c,
                "t-base",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_f),
            )
            assert isinstance(res_base, PipelineResult)
            svc_f._contexts.pop(res_base.approval_id, None)
            svc_f._session_store._conn.execute(
                "DELETE FROM nexus_ibpc_turn_contexts WHERE approval_id = ?",
                (res_base.approval_id,),
            )
            svc_f._session_store._conn.commit()
            res_delta_miss = svc_f.submit_text_intent(
                sid_f2c,
                "t-delta",
                sequence_number=2,
                text="make it shorter",
                asset_id="clip",
                source_path=str(clip_f),
                base_approval_id=res_base.approval_id,
            )
            assert (
                isinstance(res_delta_miss, service_mod.Refusal)
                and res_delta_miss.reason == "unknown_base_proposal"
            )

            # _stage_source failure on destination closes src_fd (1631)
            bad_ws = ws_f / "bad_ws"
            bad_ws.mkdir()
            (bad_ws / "input.mp4").mkdir()
            with pytest.raises(SessionError, match="staging_failed"):
                svc_f._stage_source(clip_f, bad_ws)

            # _resolve_identity stale revision_id mismatch + line 1818 (mark_executing no-op)
            sid_f3 = svc_f.open_session(
                tenant_id="ten-1", project_id="proj-w3", principal_id=OWNER_ID, user_id=1, chat_id=3
            )
            res_f3 = svc_f.submit_text_intent(
                sid_f3,
                "t-3",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_f),
            )
            assert isinstance(res_f3, PipelineResult)
            svc_f.approve(sid_f3, "t-3", res_f3.approval_id, principal_id=OWNER_ID)
            with monkeypatch.context() as mp:
                mp.setattr(svc_f._sessions, "mark_executing", lambda *_a, **_kw: None)
                out_noexec = await svc_f.execute(
                    sid_f3, "t-3", res_f3.approval_id, principal_id=OWNER_ID
                )
                assert (
                    out_noexec.status == "refused"
                    and "no longer in an executing" in out_noexec.detail
                )

            sid_f4 = svc_f.open_session(
                tenant_id="ten-1", project_id="proj-w3", principal_id=OWNER_ID, user_id=1, chat_id=4
            )
            res_f4 = svc_f.submit_text_intent(
                sid_f4,
                "t-4",
                sequence_number=1,
                text="trim from 1 to 2 seconds",
                asset_id="clip",
                source_path=str(clip_f),
            )
            assert isinstance(res_f4, PipelineResult)
            svc_f.approve(sid_f4, "t-4", res_f4.approval_id, principal_id=OWNER_ID)
            svc_f._session_store._conn.execute(
                "UPDATE nexus_ibpc_sessions SET active_revision_id = 'rev-other' "
                "WHERE session_id = ?",
                (sid_f4,),
            )
            svc_f._session_store._conn.commit()
            out_rev = await svc_f.execute(sid_f4, "t-4", res_f4.approval_id, principal_id=OWNER_ID)
            assert out_rev.status == "refused" and "stale_revision" in out_rev.detail
        finally:
            svc_f.close()
            auth_f.close()
        assert PrivacyMode.LOCAL_VAULT.value == "LOCAL_VAULT"


class TestWave3FinalSecurityClosure:
    """Adversarial regression suite for Wave 3 P1-1, P1-2, P2-1, P2-2, and P2-3."""

    def test_p1_1_register_principal_rejects_duplicate_and_token_hijack_via_store_and_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-w3", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-p11")
        auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-p11",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-w3", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        victim = "prn_" + "a" * 32
        auth.register_principal(victim, tenant_id="ten-2", raw_token="tok-victim-secret")

        # Duplicate principal_id across same or different tenant must refuse without overwriting.
        with pytest.raises(AuthorizationError, match="principal_already_exists"):
            auth.register_principal(victim, tenant_id="ten-1", raw_token="tok-attacker-new")
        assert auth.get_principal_tenant(victim) == "ten-2"
        assert auth.authenticate_token("tok-victim-secret") == victim
        with pytest.raises(PrincipalAuthenticationError, match="invalid_principal_token"):
            auth.authenticate_token("tok-unknown-missing")

        # Duplicate raw_token on a distinct principal_id must refuse.
        other_pid = "prn_" + "b" * 32
        with pytest.raises(AuthorizationError, match="principal_token_conflict"):
            auth.register_principal(other_pid, tenant_id="ten-1", raw_token="tok-victim-secret")

        # Even revoked principal_id cannot be re-registered to resurrect/hijack identity.
        auth.revoke_principal(
            victim, actor_id="system", expected_tenant_id="ten-2", allow_system_bootstrap=True
        )
        with pytest.raises(AuthorizationError, match="principal_already_exists"):
            auth.register_principal(victim, tenant_id="ten-1", raw_token="tok-resurrect-999")
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-w3")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        owner_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-p11",
        }
        # Existing principal_id via HTTP -> 409 Conflict.
        r_dup = client.post(
            "/api/studio/principals?project_id=proj-w3",
            headers=owner_h,
            json={"principal_id": victim, "raw_token": "tok-hijack-api"},
        )
        assert r_dup.status_code == 409
        # Duplicate raw_token via HTTP -> 409 Conflict.
        r_tok = client.post(
            "/api/studio/principals?project_id=proj-w3",
            headers=owner_h,
            json={"principal_id": other_pid, "raw_token": "tok-owner-p11"},
        )
        assert r_tok.status_code == 409

    def test_p1_2_bind_channel_rejects_collision_hijack_and_generates_server_binding_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-w3", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-p12")
        owner_ref = auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-p12",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-w3", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        p2 = "prn_" + "c" * 32
        auth.register_principal(p2, tenant_id="ten-1", raw_token="tok-p2-p12")
        # Collision on binding_ref or (channel, external_subject) must refuse without overwrite.
        with pytest.raises(AuthorizationError, match="binding_already_exists"):
            auth.bind_channel(
                p2,
                channel="studio",
                external_subject="sub-other-p12",
                binding_ref=owner_ref,
                allow_system_bootstrap=True,
            )
        with pytest.raises(AuthorizationError, match="binding_already_exists"):
            auth.bind_channel(
                p2,
                channel="studio",
                external_subject="sub-owner-p12",
                binding_ref="bind-fresh-ref-1",
                allow_system_bootstrap=True,
            )
        assert auth.default_binding_for(OWNER_ID) == owner_ref

        # Binding to a revoked principal must fail closed.
        p_rev = "prn_" + "d" * 32
        auth.register_principal(p_rev, tenant_id="ten-1", raw_token="tok-rev-p12")
        auth.revoke_principal(p_rev, allow_system_bootstrap=True)
        with pytest.raises(AuthorizationError, match="principal_revoked"):
            auth.bind_channel(
                p_rev, channel="telegram", external_subject="tg-rev", allow_system_bootstrap=True
            )
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-w3")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        owner_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-p12",
        }
        r_ok = client.post(
            f"/api/studio/principals/{p2}/bindings?project_id=proj-w3",
            headers=owner_h,
            json={"channel": "telegram", "external_subject": "tg-p2-123"},
        )
        assert r_ok.status_code == 201
        generated_ref = r_ok.json()["binding_ref"]
        assert generated_ref.startswith("bind-") and len(generated_ref) == 21

        # Re-binding the same (channel, external_subject) or binding_ref -> 409 Conflict.
        r_dup_sub = client.post(
            f"/api/studio/principals/{OWNER_ID}/bindings?project_id=proj-w3",
            headers=owner_h,
            json={"channel": "telegram", "external_subject": "tg-p2-123"},
        )
        assert r_dup_sub.status_code == 409
        r_dup_ref = client.post(
            f"/api/studio/principals/{OWNER_ID}/bindings?project_id=proj-w3",
            headers=owner_h,
            json={
                "channel": "telegram",
                "external_subject": "tg-unique-999",
                "binding_ref": generated_ref,
            },
        )
        assert r_dup_ref.status_code == 409
        # Binding to a revoked principal via API -> 403.
        assert (
            client.post(
                f"/api/studio/principals/{p_rev}/bindings?project_id=proj-w3",
                headers=owner_h,
                json={"channel": "telegram", "external_subject": "tg-rev-api"},
            ).status_code
            == 403
        )

    def test_p2_1_principal_and_binding_revocation_cascades_and_audits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-w3", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-p21")
        auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-p21",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-w3", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        target = "prn_" + "e" * 32
        auth.register_principal(target, tenant_id="ten-1", raw_token="tok-target-p21")
        b1 = auth.bind_channel(
            target, channel="studio", external_subject="sub-target-1", allow_system_bootstrap=True
        )
        b2 = auth.bind_channel(
            target, channel="telegram", external_subject="sub-target-2", allow_system_bootstrap=True
        )
        auth.grant_membership("proj-w3", target, role=StudioRole.EDITOR, granted_by=OWNER_ID)

        t2_target = "prn_" + "f" * 32
        auth.register_principal(t2_target, tenant_id="ten-2", raw_token="tok-t2-p21")
        b_t2 = auth.bind_channel(
            t2_target, channel="studio", external_subject="sub-t2-p21", allow_system_bootstrap=True
        )
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-w3")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        owner_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-p21",
        }
        target_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-target-p21",
        }

        # Non-OWNER cannot revoke principals or bindings -> 403.
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/principals/{OWNER_ID}/revoke",
                headers=target_h,
            ).status_code
            == 403
        )
        # Cross-tenant revoke -> 403; unknown -> 404; invalid id -> 400.
        assert (
            client.post(
                "/api/studio/projects/proj-w3/principals/bad$id/revoke",
                headers=owner_h,
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/principals/{'prn_' + '0' * 32}/revoke",
                headers=owner_h,
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/studio/projects/proj-w3/principals/bad$id/bindings/bind-1/revoke",
                headers=owner_h,
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/principals/{target}/bindings/bad$ref/revoke",
                headers=owner_h,
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/principals/{OWNER_ID}/bindings/{b2}/revoke",
                headers=owner_h,
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/principals/{t2_target}/revoke",
                headers=owner_h,
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/principals/{t2_target}/bindings/{b_t2}/revoke",
                headers=owner_h,
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/principals/{target}/bindings/bind-doesnotexist/revoke",
                headers=owner_h,
            ).status_code
            == 404
        )
        # Revoking single binding succeeds and records REVOKE_CHANNEL_BINDING audit.
        r_rb = client.post(
            f"/api/studio/projects/proj-w3/principals/{target}/bindings/{b1}/revoke",
            headers=owner_h,
        )
        assert r_rb.status_code == 200 and r_rb.json()["status"] == "REVOKED"

        # Revoking principal cascades to remaining active binding b2 and records REVOKE_PRINCIPAL.
        r_rp = client.post(
            f"/api/studio/projects/proj-w3/principals/{target}/revoke",
            headers=owner_h,
        )
        assert r_rp.status_code == 200 and r_rp.json()["status"] == "REVOKED"

        with StudioPrincipalAuthStore(auth_db, now=clock) as check_auth:
            with pytest.raises(PrincipalAuthenticationError, match="revoked"):
                check_auth.authenticate_token("tok-target-p21")
            assert check_auth.default_binding_for(target) is None
            b_rows = check_auth._conn.execute(
                "SELECT binding_ref, status FROM nexus_studio_channel_bindings "
                "WHERE principal_id = ? ORDER BY binding_ref",
                (target,),
            ).fetchall()
            assert all(str(r[1]) == "REVOKED" for r in b_rows)
            assert {str(r[0]) for r in b_rows} == {b1, b2}
            events = [
                e["event_type"] for e in check_auth.list_admin_audit(target_principal_id=target)
            ]
            assert "REVOKE_CHANNEL_BINDING" in events and "REVOKE_PRINCIPAL" in events

    @pytest.mark.asyncio
    async def test_p2_2_privacy_consent_enforces_tenant_isolation_and_legacy_backfill(
        self, tmp_path: Path
    ) -> None:
        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        clip = ws / "clip.mp4"
        clip.write_bytes(b"ftypmp42\x00\x00\x00\x00")

        auth = StudioPrincipalAuthStore(tmp_path / "auth.sqlite3", now=clock)
        auth.ensure_project("proj-a", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-p22")
        b_owner = auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-p22",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-a", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        class _Q:
            def list_jobs(self, *, project_id: str) -> list[Any]:
                return []

        class _FakeCap:
            def is_available(self) -> bool:
                return True

            async def transcribe_file(self, _p: Path, **_kw: Any) -> Any:
                return types.SimpleNamespace(text="trim from 1 to 2 seconds", is_final=True)

        ring = StaticKeyRing({"k1": b"k" * 32}, active_key_id="k1")
        svc = build_studio_ibpc_service(
            queue=_Q(),
            registry=build_runtime_registry(),
            workspace_root=ws,
            revision_provider=lambda: 0,
            key_provider=ring,
            auth_store=auth,
            channel_binding_ref=b_owner,
            now=clock,
        )
        svc._deps = dataclasses.replace(svc._deps, caption_engine=_FakeCap())  # type: ignore[misc]
        try:
            # Verify grant_privacy_consent resolves tenant_id from project when omitted.
            auto_grant = svc.grant_privacy_consent(
                project_id="proj-a",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.LOCAL_PROCESSING,
                data_category=DataCategory.TRANSCRIPT,
            )
            assert auto_grant.tenant_id == "ten-1"
            # Grant consent tagged to ten-2 on proj-a directly in privacy store.
            svc._privacy.grant_durable_consent(
                project_id="proj-a",
                tenant_id="ten-2",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                data_category=DataCategory.AUDIO,
                provider_id="gemini",
            )
            assert svc._privacy._store is not None
            with pytest.raises(PrivacyViolation, match="consent_tenant_mismatch"):
                svc._privacy._store.check_consent_status(
                    project_id="proj-a",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value,
                    data_category=DataCategory.AUDIO.value,
                    provider_id="gemini",
                    now_utc=clock(),
                    tenant_id="ten-1",
                )
            with pytest.raises(PrivacyViolation, match="consent_tenant_mismatch"):
                svc._privacy.enforce_processing_consent(
                    project_id="proj-a",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.EXTERNAL_PROVIDER_DISPATCH,
                    data_category=DataCategory.AUDIO,
                    provider_id="gemini",
                    tenant_id="ten-1",
                )

            # Also verify LOCAL_PROCESSING consent enforcement in submit_voice_intent passes
            # session.tenant_id and fails closed on tenant mismatch.
            svc._privacy.grant_durable_consent(
                project_id="proj-a",
                tenant_id="ten-2",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.LOCAL_PROCESSING,
                data_category=DataCategory.AUDIO,
            )
            with pytest.raises(PrivacyViolation, match="consent_tenant_mismatch"):
                svc._privacy.enforce_processing_consent(
                    project_id="proj-a",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.LOCAL_PROCESSING,
                    data_category=DataCategory.AUDIO,
                    tenant_id="ten-1",
                )

            sid = svc.open_session(
                tenant_id="ten-1",
                project_id="proj-a",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                privacy_mode=PrivacyMode.TRUSTED_CLOUD,
                consent_scope="audio",
            )
            # Legacy row with empty tenant_id='' fails closed until deterministic backfill runs.
            svc._privacy._store._conn.execute(
                "UPDATE nexus_privacy_consents SET tenant_id = '' WHERE project_id = 'proj-a'"
            )
            svc._privacy._store._conn.commit()
            with pytest.raises(PrivacyViolation, match="consent_tenant_mismatch"):
                svc._privacy._store.check_consent_status(
                    project_id="proj-a",
                    principal_id=OWNER_ID,
                    mode=PrivacyMode.TRUSTED_CLOUD,
                    processing_scope=ProcessingScope.LOCAL_PROCESSING.value,
                    data_category=DataCategory.AUDIO.value,
                    now_utc=clock(),
                    tenant_id="ten-1",
                )
            updated = svc._privacy._store.backfill_legacy_tenant_id(
                project_id="proj-a", tenant_id="ten-1"
            )
            assert updated == 4
            status, _ = svc._privacy._store.check_consent_status(
                project_id="proj-a",
                principal_id=OWNER_ID,
                mode=PrivacyMode.TRUSTED_CLOUD,
                processing_scope=ProcessingScope.LOCAL_PROCESSING.value,
                data_category=DataCategory.AUDIO.value,
                now_utc=clock(),
                tenant_id="ten-1",
            )
            assert status == "active"

            # After backfill to ten-1, submit_voice_intent gets past has_consent (no_provider_wired)
            # instead of failing consent_tenant_mismatch; if ten-2 consent is re-added, it refuses.
            svc._privacy._store._conn.execute(
                "UPDATE nexus_privacy_consents SET tenant_id = 'ten-2' WHERE project_id = 'proj-a'"
            )
            svc._privacy._store._conn.commit()
            with pytest.raises(PrivacyViolation):
                await svc.submit_voice_intent(
                    sid,
                    "t-p22-1",
                    sequence_number=1,
                    audio=b"RIFF\x00\x00\x00\x00WAVE",
                    asset_id="clip",
                    source_path=str(clip),
                    caller_principal_id=OWNER_ID,
                    expected_project_id="proj-a",
                    expected_tenant_id="ten-1",
                )
        finally:
            svc.close()
            auth.close()

    def test_p2_3_owner_scoped_to_project_and_production_forbids_rbac_opt_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-a", tenant_id="ten-1")
        auth.ensure_project("proj-b", tenant_id="ten-1")

        owner_b = "prn_" + "1" * 32
        viewer_a = "prn_" + "2" * 32
        auth.register_principal(owner_b, tenant_id="ten-1", raw_token="tok-owner-b-p23")
        auth.bind_channel(
            owner_b,
            channel="studio",
            external_subject="sub-owner-b-p23",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-b", owner_b, role=StudioRole.OWNER, granted_by=owner_b)

        auth.register_principal(viewer_a, tenant_id="ten-1", raw_token="tok-viewer-a-p23")
        auth.bind_channel(
            viewer_a,
            channel="studio",
            external_subject="sub-viewer-a-p23",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-a", viewer_a, role=StudioRole.VIEWER, granted_by=owner_b)

        assert auth.has_owner_role(owner_b, "ten-1", project_id="proj-b") is True
        assert auth.has_owner_role(owner_b, "ten-1", project_id="proj-a") is False
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-a,proj-b")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        owner_b_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-b-p23",
        }
        # OWNER of proj-b cannot mutate principals or bindings under proj-a scope -> 403.
        assert (
            client.post(
                "/api/studio/principals?project_id=proj-a",
                headers=owner_b_h,
                json={"principal_id": "prn_" + "3" * 32, "raw_token": "tok-unauth-p23"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/api/studio/principals/{viewer_a}/bindings?project_id=proj-a",
                headers=owner_b_h,
                json={"channel": "studio", "external_subject": "sub-unauth-p23"},
            ).status_code
            == 403
        )

        # In production, RBAC opt-out flags and unauthenticated studio calls fail closed.
        deploy_h = {"Authorization": "Bearer deploy-tok"}
        monkeypatch.setenv("NEXUS_ENV", "production")
        for env_key, env_val in (
            ("NEXUS_STUDIO_REQUIRE_PRINCIPAL", "0"),
            ("NEXUS_STUDIO_RBAC_OPT_OUT", "1"),
            ("NEXUS_STUDIO_ALLOW_UNAUTHENTICATED_PRINCIPAL", "1"),
        ):
            with monkeypatch.context() as mp:
                mp.setenv(env_key, env_val)
                r_opt = client.get("/api/studio/projects/proj-a/jobs/job-1/state", headers=deploy_h)
                assert r_opt.status_code == 403
        r_no_tok = client.get("/api/studio/projects/proj-a/jobs/job-1/state", headers=deploy_h)
        assert r_no_tok.status_code == 401
        r_prod_ok = client.get("/api/studio/projects/proj-a/jobs/job-1/state", headers=owner_b_h)
        assert r_prod_ok.status_code == 403  # viewer/non-member on proj-a fails RBAC, not 401

    def test_owner_mutation_without_project_id_or_across_projects_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-alpha", tenant_id="ten-1")
        auth.ensure_project("proj-beta", tenant_id="ten-1")

        owner_alpha = "prn_" + "4" * 32
        member_beta = "prn_" + "5" * 32
        auth.register_principal(owner_alpha, tenant_id="ten-1", raw_token="tok-owner-alpha")
        auth.bind_channel(
            owner_alpha,
            channel="studio",
            external_subject="sub-owner-alpha",
            allow_system_bootstrap=True,
        )
        auth.grant_membership(
            "proj-alpha", owner_alpha, role=StudioRole.OWNER, granted_by=owner_alpha
        )

        auth.register_principal(member_beta, tenant_id="ten-1", raw_token="tok-member-beta")
        auth.grant_membership(
            "proj-beta", member_beta, role=StudioRole.EDITOR, granted_by=owner_alpha
        )
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-alpha,proj-beta")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        alpha_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-alpha",
        }
        # Omitting project_id on principal/binding provisioning fails closed with 403.
        r_no_proj_p = client.post(
            "/api/studio/principals",
            headers=alpha_h,
            json={"principal_id": "prn_" + "6" * 32, "raw_token": "tok-unscoped-p1a"},
        )
        assert r_no_proj_p.status_code == 403
        assert "project_scope_required" in r_no_proj_p.text

        r_no_proj_b = client.post(
            f"/api/studio/principals/{member_beta}/bindings",
            headers=alpha_h,
            json={"channel": "studio", "external_subject": "sub-unscoped-p1a"},
        )
        assert r_no_proj_b.status_code == 403
        assert "project_scope_required" in r_no_proj_b.text

        # OWNER of proj-alpha cannot bind channel to a principal exclusive to proj-beta.
        r_cross_proj_b = client.post(
            f"/api/studio/principals/{member_beta}/bindings?project_id=proj-alpha",
            headers=alpha_h,
            json={"channel": "studio", "external_subject": "sub-cross-proj-p1a"},
        )
        assert r_cross_proj_b.status_code == 403
        assert "principal_not_in_project" in r_cross_proj_b.text

    def test_revoke_principal_and_binding_blocked_when_target_not_in_project_or_active_in_other_project(  # noqa: E501
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-alpha", tenant_id="ten-1")
        auth.ensure_project("proj-beta", tenant_id="ten-1")

        owner_alpha = "prn_" + "a1" * 16
        owner_beta = "prn_" + "b1" * 16
        beta_only = "prn_" + "c1" * 16
        shared_user = "prn_" + "d1" * 16

        auth.register_principal(owner_alpha, tenant_id="ten-1", raw_token="tok-oa-p1b")
        auth.bind_channel(
            owner_alpha,
            channel="studio",
            external_subject="sub-oa-p1b",
            allow_system_bootstrap=True,
        )
        auth.grant_membership(
            "proj-alpha", owner_alpha, role=StudioRole.OWNER, granted_by=owner_alpha
        )

        auth.register_principal(owner_beta, tenant_id="ten-1", raw_token="tok-ob-p1b")
        auth.bind_channel(
            owner_beta, channel="studio", external_subject="sub-ob-p1b", allow_system_bootstrap=True
        )
        auth.grant_membership("proj-beta", owner_beta, role=StudioRole.OWNER, granted_by=owner_beta)

        auth.register_principal(beta_only, tenant_id="ten-1", raw_token="tok-bo-p1b")
        b_beta_only = auth.bind_channel(
            beta_only, channel="studio", external_subject="sub-bo-p1b", allow_system_bootstrap=True
        )
        auth.grant_membership("proj-beta", beta_only, role=StudioRole.EDITOR, granted_by=owner_beta)

        auth.register_principal(shared_user, tenant_id="ten-1", raw_token="tok-su-p1b")
        b_shared = auth.bind_channel(
            shared_user,
            channel="studio",
            external_subject="sub-su-p1b",
            allow_system_bootstrap=True,
        )
        auth.grant_membership(
            "proj-alpha", shared_user, role=StudioRole.EDITOR, granted_by=owner_alpha
        )
        auth.grant_membership(
            "proj-beta", shared_user, role=StudioRole.EDITOR, granted_by=owner_beta
        )
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-alpha,proj-beta")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        alpha_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-oa-p1b",
        }
        # Target only in proj-beta cannot be revoked by proj-alpha owner -> 403.
        r1 = client.post(
            f"/api/studio/projects/proj-alpha/principals/{beta_only}/revoke",
            headers=alpha_h,
        )
        assert r1.status_code == 403 and "principal_not_in_project" in r1.text
        r2 = client.post(
            f"/api/studio/projects/proj-alpha/principals/{beta_only}/bindings/{b_beta_only}/revoke",
            headers=alpha_h,
        )
        assert r2.status_code == 403 and "principal_not_in_project" in r2.text

        # Shared principal active in proj-beta cannot be globally revoked by proj-alpha -> 403.
        r3 = client.post(
            f"/api/studio/projects/proj-alpha/principals/{shared_user}/revoke",
            headers=alpha_h,
        )
        assert r3.status_code == 403 and "principal_active_in_other_projects" in r3.text
        r4 = client.post(
            f"/api/studio/projects/proj-alpha/principals/{shared_user}/bindings/{b_shared}/revoke",
            headers=alpha_h,
        )
        assert r4.status_code == 403 and "principal_active_in_other_projects" in r4.text

        # Neither target nor its bindings were revoked.
        with StudioPrincipalAuthStore(auth_db, now=clock) as check_auth:
            assert check_auth.authenticate_token("tok-bo-p1b") == beta_only
            assert check_auth.authenticate_token("tok-su-p1b") == shared_user

    def test_record_consent_propagates_tenant_and_durable_expired_or_revoked_never_falls_back_to_memory(  # noqa: E501
        self, tmp_path: Path
    ) -> None:
        from nexus_ai_agent.nagar.ibpc.session import DurableLiveSessionStore, LiveSessionManager

        clock = _Clock("2026-11-01T00:00:00Z")
        pstore = DurablePrivacyConsentStore(tmp_path / "priv_p2a.sqlite3")
        policy = PrivacyPolicy(now=clock, store=pstore, configured_providers=frozenset({"gemini"}))
        sstore = DurableLiveSessionStore(tmp_path / "sess_p2a.sqlite3")
        mgr = LiveSessionManager(now=clock, privacy=policy, store=sstore)
        try:
            sess = mgr.open_session(
                tenant_id="ten-p2a",
                project_id="proj-p2a",
                principal_id=OWNER_ID,
                user_id=1,
                chat_id=1,
                privacy_mode=PrivacyMode.TRUSTED_CLOUD,
                consent_scope="transcript",
            )
            consents = pstore.list_consents(project_id="proj-p2a")
            assert len(consents) == 1 and consents[0].tenant_id == "ten-p2a"
            assert policy.has_consent(
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                scope="transcript",
                project_id="proj-p2a",
                tenant_id="ten-p2a",
            )

            # Revoke in durable store: has_consent must return False (no memory fallback).
            pstore.revoke_consent(
                consents[0].consent_id,
                revoked_by=OWNER_ID,
                revoked_at_utc=clock(),
                reason="test_revoke",
            )
            assert not policy.has_consent(
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                scope="transcript",
                project_id="proj-p2a",
                tenant_id="ten-p2a",
            )

            # Re-enable via set_privacy_mode (with tenant_id propagated), then test durable expiry.
            rec2 = mgr.set_privacy_mode(
                sess.session_id,
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                consent_scope="audio",
            )
            assert rec2.tenant_id == "ten-p2a"
            # Grant expiring consent and advance clock past expiry -> no memory fallback.
            policy.record_consent(
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                scope="video",
                project_id="proj-p2a",
                tenant_id="ten-p2a",
                expires_at_utc="2026-11-01T00:05:00Z",
            )
            assert policy.has_consent(
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                scope="video",
                project_id="proj-p2a",
                tenant_id="ten-p2a",
            )
            clock.current = "2026-11-01T00:10:00Z"
            assert not policy.has_consent(
                PrivacyMode.TRUSTED_CLOUD,
                principal_id=OWNER_ID,
                scope="video",
                project_id="proj-p2a",
                tenant_id="ten-p2a",
            )
        finally:
            sstore.close()
            pstore.close()

        # Pure in-memory mode also enforces tenant_id & expires_at_utc.
        mem_clock = _Clock("2026-11-01T00:00:00Z")
        mem_pol = PrivacyPolicy(now=mem_clock, store=None)
        mem_pol.record_consent(
            PrivacyMode.TRUSTED_CLOUD,
            principal_id=OWNER_ID,
            scope="audio",
            tenant_id="ten-1",
            expires_at_utc="2026-11-01T00:05:00Z",
        )
        assert not mem_pol.has_consent(
            PrivacyMode.TRUSTED_CLOUD,
            principal_id=OWNER_ID,
            scope="audio",
            tenant_id="ten-other",
        )
        assert mem_pol.has_consent(
            PrivacyMode.TRUSTED_CLOUD,
            principal_id=OWNER_ID,
            scope="audio",
            tenant_id="ten-1",
        )
        mem_clock.current = "2026-11-01T00:10:00Z"
        assert not mem_pol.has_consent(
            PrivacyMode.TRUSTED_CLOUD,
            principal_id=OWNER_ID,
            scope="audio",
            tenant_id="ten-1",
        )

    def test_project_admin_audit_endpoint_returns_revoke_principal_and_revoke_binding_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-w3", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-p2b")
        auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-p2b",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-w3", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        target = "prn_" + "e2" * 16
        auth.register_principal(target, tenant_id="ten-1", raw_token="tok-target-p2b")
        b_target = auth.bind_channel(
            target, channel="studio", external_subject="sub-target-p2b", allow_system_bootstrap=True
        )
        auth.grant_membership("proj-w3", target, role=StudioRole.EDITOR, granted_by=OWNER_ID)
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-w3")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        owner_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-p2b",
        }
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/principals/{target}/bindings/{b_target}/revoke",
                headers=owner_h,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/studio/projects/proj-w3/principals/{target}/revoke",
                headers=owner_h,
            ).status_code
            == 200
        )
        r_audit = client.get("/api/studio/projects/proj-w3/admin/audit", headers=owner_h)
        assert r_audit.status_code == 200
        rows = r_audit.json()["audit"]
        revoke_rows = {
            row["event_type"]: row
            for row in rows
            if row["event_type"] in ("REVOKE_CHANNEL_BINDING", "REVOKE_PRINCIPAL")
        }
        assert set(revoke_rows) == {"REVOKE_CHANNEL_BINDING", "REVOKE_PRINCIPAL"}
        for ev in revoke_rows.values():
            assert ev["project_id"] == "proj-w3"
            assert ev["actor_id"] == OWNER_ID
            assert ev["target_principal_id"] == target

    def test_toctou_actor_downgraded_from_owner_to_viewer_after_precheck_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.api import studio_ibpc as studio_ibpc_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-alpha", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-toctou")
        auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-toctou",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-alpha", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        target = "prn_" + "1a" * 16
        auth.register_principal(target, tenant_id="ten-1", raw_token="tok-target-toctou")
        b_target = auth.bind_channel(
            target,
            channel="studio",
            external_subject="sub-target-toctou",
            actor_id=OWNER_ID,
            project_id="proj-alpha",
        )
        auth.grant_membership("proj-alpha", target, role=StudioRole.VIEWER, granted_by=OWNER_ID)
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-alpha")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        orig_precheck = studio_ibpc_module._authorize_owner_mutation

        def _precheck_then_downgrade(
            store: StudioPrincipalAuthStore,
            *,
            actor_id: str,
            project_id: str | None,
        ) -> str:
            verified = orig_precheck(store, actor_id=actor_id, project_id=project_id)
            # Simulate concurrent downgrade to VIEWER between precheck and store mutation.
            store.grant_membership(
                "proj-alpha", OWNER_ID, role=StudioRole.VIEWER, granted_by=OWNER_ID
            )
            return verified

        monkeypatch.setattr(
            studio_ibpc_module, "_authorize_owner_mutation", _precheck_then_downgrade
        )

        client = TestClient(app_module.app)
        owner_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-toctou",
        }
        new_pid = "prn_" + "2b" * 16

        # 1) register_principal -> 403 unauthorized_project_owner
        r_reg = client.post(
            "/api/studio/principals?project_id=proj-alpha",
            headers=owner_h,
            json={"principal_id": new_pid, "raw_token": "tok-new-toctou"},
        )
        assert r_reg.status_code == 403 and "unauthorized_project_owner" in r_reg.text

        # Restore OWNER and test bind_channel -> 403 unauthorized_project_owner
        with StudioPrincipalAuthStore(auth_db, now=clock) as s:
            s.grant_membership("proj-alpha", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        r_bind = client.post(
            f"/api/studio/principals/{target}/bindings?project_id=proj-alpha",
            headers=owner_h,
            json={"channel": "telegram", "external_subject": "tg-toctou"},
        )
        assert r_bind.status_code == 403 and "unauthorized_project_owner" in r_bind.text

        # Restore OWNER and test revoke_channel_binding -> 403 unauthorized_project_owner
        with StudioPrincipalAuthStore(auth_db, now=clock) as s:
            s.grant_membership("proj-alpha", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        r_rb = client.post(
            f"/api/studio/projects/proj-alpha/principals/{target}/bindings/{b_target}/revoke",
            headers=owner_h,
        )
        assert r_rb.status_code == 403 and "unauthorized_project_owner" in r_rb.text

        # Restore OWNER, then test concurrent REVOKED membership -> 403
        def _precheck_then_revoke(
            store: StudioPrincipalAuthStore,
            *,
            actor_id: str,
            project_id: str | None,
        ) -> str:
            verified = orig_precheck(store, actor_id=actor_id, project_id=project_id)
            store.revoke_membership("proj-alpha", OWNER_ID, revoked_by=OWNER_ID)
            return verified

        with StudioPrincipalAuthStore(auth_db, now=clock) as s:
            s.grant_membership("proj-alpha", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        monkeypatch.setattr(studio_ibpc_module, "_authorize_owner_mutation", _precheck_then_revoke)
        r_rp = client.post(
            f"/api/studio/projects/proj-alpha/principals/{target}/revoke",
            headers=owner_h,
        )
        assert r_rp.status_code == 403 and "unauthorized_project_owner" in r_rp.text

        # Confirm none of the 4 mutations committed.
        with StudioPrincipalAuthStore(auth_db, now=clock) as check_auth:
            with pytest.raises(AuthorizationError, match="principal_not_found"):
                check_auth.get_principal_tenant(new_pid)
            assert check_auth.authenticate_token("tok-target-toctou") == target
            assert check_auth.default_binding_for(target) == b_target

    def test_direct_store_call_by_non_owner_rejected(self, tmp_path: Path) -> None:
        clock = _Clock()
        with StudioPrincipalAuthStore(tmp_path / "auth_direct.sqlite3", now=clock) as store:
            store.ensure_project("proj-alpha", tenant_id="ten-1")
            store.ensure_project("proj-other", tenant_id="ten-2")
            store.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-dir")
            store.register_principal(EDITOR_ID, tenant_id="ten-1", raw_token="tok-editor-dir")
            store.register_principal(VIEWER_ID, tenant_id="ten-1", raw_token="tok-viewer-dir")
            store.register_principal(OUTSIDER_ID, tenant_id="ten-1", raw_token="tok-out-dir")
            t2_actor = "prn_" + "2c" * 16
            store.register_principal(t2_actor, tenant_id="ten-2", raw_token="tok-t2-dir")
            store.grant_membership(
                "proj-other", t2_actor, role=StudioRole.OWNER, granted_by=t2_actor
            )

            store.grant_membership(
                "proj-alpha", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID
            )
            store.grant_membership(
                "proj-alpha", EDITOR_ID, role=StudioRole.EDITOR, granted_by=OWNER_ID
            )
            store.grant_membership(
                "proj-alpha", VIEWER_ID, role=StudioRole.VIEWER, granted_by=OWNER_ID
            )
            b_viewer = store.bind_channel(
                VIEWER_ID,
                channel="studio",
                external_subject="sub-viewer-dir",
                actor_id=OWNER_ID,
                project_id="proj-alpha",
            )

            fresh_pid = "prn_" + "3d" * 16
            for non_owner in (EDITOR_ID, VIEWER_ID, OUTSIDER_ID):
                with pytest.raises(AuthorizationError, match="unauthorized_project_owner"):
                    store.register_principal(
                        fresh_pid,
                        tenant_id="ten-1",
                        raw_token="tok-fresh-dir",
                        actor_id=non_owner,
                        project_id="proj-alpha",
                    )
                with pytest.raises(AuthorizationError, match="unauthorized_project_owner"):
                    store.bind_channel(
                        VIEWER_ID,
                        channel="telegram",
                        external_subject=f"tg-{non_owner}",
                        actor_id=non_owner,
                        project_id="proj-alpha",
                    )
                with pytest.raises(AuthorizationError, match="unauthorized_project_owner"):
                    store.revoke_principal(
                        VIEWER_ID,
                        actor_id=non_owner,
                        project_id="proj-alpha",
                    )
                with pytest.raises(AuthorizationError, match="unauthorized_project_owner"):
                    store.revoke_channel_binding(
                        b_viewer,
                        actor_id=non_owner,
                        project_id="proj-alpha",
                    )

            # Empty actor, inactive actor, and cross-tenant actor also rejected inside transaction.
            with pytest.raises(AuthorizationError, match="actor_principal_required"):
                store.bind_channel(
                    VIEWER_ID,
                    channel="telegram",
                    external_subject="tg-empty-actor",
                    actor_id="",
                    project_id="proj-alpha",
                )
            with pytest.raises(AuthorizationError, match="actor_principal_inactive_or_missing"):
                store.bind_channel(
                    VIEWER_ID,
                    channel="telegram",
                    external_subject="tg-missing-actor",
                    actor_id="prn_" + "9f" * 16,
                    project_id="proj-alpha",
                )
            with pytest.raises(AuthorizationError, match="cross_tenant_principal_access"):
                store.register_principal(
                    fresh_pid,
                    tenant_id="ten-1",
                    raw_token="tok-cross-t2",
                    actor_id=t2_actor,
                    project_id="proj-alpha",
                )
            with pytest.raises(AuthorizationError, match="cross_tenant_principal_access"):
                store.bind_channel(
                    VIEWER_ID,
                    channel="telegram",
                    external_subject="tg-cross-t2",
                    actor_id=t2_actor,
                    project_id="proj-other",
                )
            with pytest.raises(AuthorizationError, match="principal_tenant_mismatch"):
                store.revoke_principal(
                    VIEWER_ID,
                    actor_id=t2_actor,
                    project_id="proj-other",
                )

    def test_revoked_target_membership_cannot_pass_project_revocation_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth_rev_mem.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-alpha", tenant_id="ten-1")
        auth.ensure_project("proj-beta", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-revmem")
        auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-revmem",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-alpha", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        target = "prn_" + "4e" * 16
        auth.register_principal(target, tenant_id="ten-1", raw_token="tok-target-revmem")
        b_target = auth.bind_channel(
            target,
            channel="studio",
            external_subject="sub-target-revmem",
            actor_id=OWNER_ID,
            project_id="proj-alpha",
        )
        auth.grant_membership("proj-alpha", target, role=StudioRole.VIEWER, granted_by=OWNER_ID)
        # Revoke target's membership in proj-alpha while leaving an ACTIVE membership in proj-beta.
        auth.revoke_membership("proj-alpha", target, revoked_by=OWNER_ID)
        auth.grant_membership("proj-beta", target, role=StudioRole.EDITOR, granted_by=OWNER_ID)

        with pytest.raises(AuthorizationError, match="principal_not_in_project"):
            auth.revoke_principal(target, actor_id=OWNER_ID, project_id="proj-alpha")
        with pytest.raises(AuthorizationError, match="principal_not_in_project"):
            auth.revoke_channel_binding(b_target, actor_id=OWNER_ID, project_id="proj-alpha")
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-alpha,proj-beta")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        owner_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-revmem",
        }
        r_p = client.post(
            f"/api/studio/projects/proj-alpha/principals/{target}/revoke",
            headers=owner_h,
        )
        assert r_p.status_code == 403 and "principal_not_in_project" in r_p.text
        r_b = client.post(
            f"/api/studio/projects/proj-alpha/principals/{target}/bindings/{b_target}/revoke",
            headers=owner_h,
        )
        assert r_b.status_code == 403 and "principal_not_in_project" in r_b.text

    def test_bind_channel_without_project_scope_rejected(self, tmp_path: Path) -> None:
        clock = _Clock()
        with StudioPrincipalAuthStore(tmp_path / "auth_noproj.sqlite3", now=clock) as store:
            store.ensure_project("proj-alpha", tenant_id="ten-1")
            store.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-np")
            store.grant_membership(
                "proj-alpha", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID
            )
            with pytest.raises(AuthorizationError, match="project_scope_required"):
                store.bind_channel(
                    OWNER_ID,
                    channel="studio",
                    external_subject="sub-no-proj",
                    actor_id=OWNER_ID,
                    project_id="",
                )
            with pytest.raises(AuthorizationError, match="project_scope_required"):
                store.bind_channel(
                    OWNER_ID,
                    channel="studio",
                    external_subject="sub-no-proj-2",
                    actor_id=OWNER_ID,
                    project_id="",
                    allow_system_bootstrap=True,
                )
            with pytest.raises(AuthorizationError, match="project_scope_required"):
                store.bind_channel(
                    OWNER_ID,
                    channel="studio",
                    external_subject="sub-sys-no-boot",
                    actor_id="system",
                    project_id="",
                )

    def test_multi_project_global_revocation_records_audit_for_all_affected_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus_ai_agent.api import app as app_module
        from nexus_ai_agent.config.settings import get_settings

        clock = _Clock()
        ws = tmp_path / "ws"
        ws.mkdir()
        auth_db = tmp_path / "auth_multi.sqlite3"
        auth = StudioPrincipalAuthStore(auth_db, now=clock)
        auth.ensure_project("proj-alpha", tenant_id="ten-1")
        auth.ensure_project("proj-beta", tenant_id="ten-1")
        auth.register_principal(OWNER_ID, tenant_id="ten-1", raw_token="tok-owner-multi")
        auth.bind_channel(
            OWNER_ID,
            channel="studio",
            external_subject="sub-owner-multi",
            allow_system_bootstrap=True,
        )
        auth.grant_membership("proj-alpha", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)
        auth.grant_membership("proj-beta", OWNER_ID, role=StudioRole.OWNER, granted_by=OWNER_ID)

        target = "prn_" + "5f" * 16
        auth.register_principal(target, tenant_id="ten-1", raw_token="tok-target-multi")
        b_target = auth.bind_channel(
            target,
            channel="studio",
            external_subject="sub-target-multi",
            actor_id=OWNER_ID,
            project_id="proj-alpha",
        )
        auth.grant_membership("proj-alpha", target, role=StudioRole.VIEWER, granted_by=OWNER_ID)
        auth.grant_membership("proj-beta", target, role=StudioRole.VIEWER, granted_by=OWNER_ID)
        auth.close()

        monkeypatch.setenv("NEXUS_DASHBOARD_TOKEN", "deploy-tok")
        monkeypatch.setenv("NEXUS_STUDIO_PROJECT_IDS", "proj-alpha,proj-beta")
        monkeypatch.setenv("NEXUS_STUDIO_WORKSPACE_ROOT", str(ws))
        monkeypatch.setenv("NEXUS_STUDIO_AUTH_DB", str(auth_db))
        monkeypatch.setenv("NEXUS_QUEUE_DB_PATH", str(tmp_path / "q.sqlite3"))
        get_settings.cache_clear()
        monkeypatch.setattr(app_module, "_creative_registry", None)

        client = TestClient(app_module.app)
        owner_h = {
            "Authorization": "Bearer deploy-tok",
            "X-Nexus-Principal-Token": "tok-owner-multi",
        }

        # Revoke channel binding & principal via proj-alpha; both proj-alpha & proj-beta audited.
        r_rb = client.post(
            f"/api/studio/projects/proj-alpha/principals/{target}/bindings/{b_target}/revoke",
            headers=owner_h,
        )
        assert r_rb.status_code == 200
        r_rp = client.post(
            f"/api/studio/projects/proj-alpha/principals/{target}/revoke",
            headers=owner_h,
        )
        assert r_rp.status_code == 200

        for proj in ("proj-alpha", "proj-beta"):
            r_aud = client.get(f"/api/studio/projects/{proj}/admin/audit", headers=owner_h)
            assert r_aud.status_code == 200
            events = {
                ev["event_type"]: ev
                for ev in r_aud.json()["audit"]
                if ev["event_type"] in ("REVOKE_CHANNEL_BINDING", "REVOKE_PRINCIPAL")
            }
            assert set(events) == {"REVOKE_CHANNEL_BINDING", "REVOKE_PRINCIPAL"}
            for ev in events.values():
                assert ev["project_id"] == proj
                assert ev["target_principal_id"] == target
                detail = json.loads(ev["detail"])
                assert detail["initiating_project_id"] == "proj-alpha"
                assert detail["affected_projects"] == ["proj-alpha", "proj-beta"]
