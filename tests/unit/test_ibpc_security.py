"""Adversarial security tests for the IBPC slice (NAGAR-IBPC-SECURITY-CLOSURE-01).

Phase B — audio ingress: no caller-supplied id can place/overwrite a file
outside the resolved audio dir (traversal, absolute, backslash, empty, overlong,
collision, symlink), the physical file name is an independent random id created
with O_EXCL at 0600, and cleanup runs on success/exception/timeout/cancel
without ever touching another file.

Phase C/D — approval identity: a binding is consumable only in the exact
session/turn/tenant/project/principal/sequence/revision/asset/digest context it
was issued in; every cross-context use is refused with a typed error and never
enqueues a job (asserted against a recording fake queue).
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.connect.key_provider import StaticKeyRing
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError
from nexus_ai_agent.nagar.ibpc.privacy import PrivacyPolicy
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps, PipelineResult
from nexus_ai_agent.nagar.ibpc.session import (
    LiveSessionManager,
    SessionError,
    validate_turn_id,
)

NOW = lambda: "2026-10-31T00:00:00Z"  # noqa: E731


def _manager() -> LiveSessionManager:
    return LiveSessionManager(now=NOW, privacy=PrivacyPolicy(now=NOW))


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


# --------------------------------------------------------------------------- #
# Phase B: audio path security
# --------------------------------------------------------------------------- #


class _RecordingEngine:
    """Declared test double for the caption port: records the staged path and
    its stat while transcription is 'running'."""

    def __init__(self, text: str = "trim from 0.5 to 1.5 seconds") -> None:
        self._text = text
        self.seen: list[tuple[Path, int]] = []
        self.delay = 0.0
        self.error: Exception | None = None

    def is_available(self) -> bool:
        return True

    async def transcribe(self, audio_path: Any, *, language: str | None = None) -> Any:
        from nexus_ai_agent.creative.packs.caption.models import TranscriptRef, TranscriptSegment

        p = Path(audio_path)
        st = p.stat()
        self.seen.append((p, stat.S_IMODE(st.st_mode)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return TranscriptRef(
            transcript_id="tr_sec",
            segments=(TranscriptSegment(start_us=0, end_us=1_000_000, text=self._text),),
        )


MALICIOUS_TURN_IDS = [
    "../../outside",
    "../../../tmp/ibpc_attack_probe",
    "/abs/path",
    "a/../b",
    "back\\slash",
    "..",
    ".",
    "",
    "x" * 500,
    "t1.bin",
    "naïve-id",
    "with space",
    "semi;colon",
]


class TestTurnIdBoundary:
    @pytest.mark.parametrize("bad", MALICIOUS_TURN_IDS)
    def test_malicious_turn_id_rejected(self, bad: str) -> None:
        with pytest.raises(SessionError) as exc:
            validate_turn_id(bad)
        assert exc.value.reason == "invalid_turn_id"

    def test_valid_turn_ids_accepted(self) -> None:
        for good in ("t1", "turn-42", "a" * 64, "ABC_123-x"):
            assert validate_turn_id(good) == good

    def test_begin_turn_refuses_malicious_id_and_creates_nothing(self, tmp_path: Path) -> None:
        mgr = _manager()
        sid = _open(mgr)
        probe = Path("/tmp/ibpc_attack_probe")
        probe.unlink(missing_ok=True)
        for bad in MALICIOUS_TURN_IDS:
            with pytest.raises(SessionError) as exc:
                mgr.begin_turn(sid, bad, sequence_number=1, project_revision=7)
            assert exc.value.reason == "invalid_turn_id"
        assert not probe.exists()
        assert list(tmp_path.iterdir()) == []
        assert mgr.get_session(sid).turns == {}


class TestSecureAudioStaging:
    @pytest.mark.asyncio
    async def test_file_name_is_random_not_turn_id_and_cleaned_up(self, tmp_path: Path) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        engine = _RecordingEngine()
        audio_dir = tmp_path / "audio"
        await mgr.submit_audio(sid, "t1", b"audio-bytes", engine=engine, audio_dir=audio_dir)
        ((staged, mode),) = engine.seen
        # physical name is an independent random id — never derived from turn_id
        assert "t1" not in staged.name
        assert staged.name.startswith("audio_") and staged.name.endswith(".bin")
        assert len(staged.name) == len("audio_") + 32 + len(".bin")
        # minimum-privilege permissions while staged
        assert mode == 0o600
        # inside the resolved audio dir
        assert staged.parent == audio_dir.resolve()
        # removed after transcription
        assert list(audio_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_oexcl_never_overwrites_an_existing_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import nexus_ai_agent.nagar.ibpc.session as session_mod

        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        root = (tmp_path / "audio").resolve()
        root.mkdir()
        decoy = root / ("audio_" + "ab" * 16 + ".bin")
        decoy.write_bytes(b"decoy")
        tokens = iter(["ab" * 16, "cd" * 16])  # first collides, second succeeds
        monkeypatch.setattr(session_mod.secrets, "token_hex", lambda n: next(tokens))
        engine = _RecordingEngine()
        await mgr.submit_audio(sid, "t1", b"payload", engine=engine, audio_dir=root)
        assert decoy.read_bytes() == b"decoy"  # untouched
        ((staged, _),) = engine.seen
        assert staged != decoy
        assert list(root.iterdir()) == [decoy]  # only the decoy remains

    @pytest.mark.asyncio
    async def test_all_name_collisions_fail_closed_without_overwrite(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import nexus_ai_agent.nagar.ibpc.session as session_mod

        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        root = (tmp_path / "audio").resolve()
        root.mkdir()
        decoy = root / ("audio_" + "ab" * 16 + ".bin")
        decoy.write_bytes(b"decoy")
        monkeypatch.setattr(session_mod.secrets, "token_hex", lambda n: "ab" * 16)
        with pytest.raises(SessionError) as exc:
            await mgr.submit_audio(sid, "t1", b"payload", engine=_RecordingEngine(), audio_dir=root)
        assert exc.value.reason == "audio_stage_failed"
        assert decoy.read_bytes() == b"decoy"
        assert list(root.iterdir()) == [decoy]

    @pytest.mark.asyncio
    async def test_containment_uses_resolved_real_path(self, tmp_path: Path) -> None:
        """audio_dir given as a symlink: staging lands in the REAL directory
        (resolve-based containment), never escapes it."""
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        real = tmp_path / "real_audio"
        real.mkdir()
        link = tmp_path / "audio_link"
        link.symlink_to(real, target_is_directory=True)
        engine = _RecordingEngine()
        await mgr.submit_audio(sid, "t1", b"bytes", engine=engine, audio_dir=link)
        ((staged, _),) = engine.seen
        assert staged.parent == real  # resolved target
        assert list(real.iterdir()) == []  # cleaned up

    @pytest.mark.asyncio
    async def test_cleanup_on_engine_exception(self, tmp_path: Path) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        root = tmp_path / "audio"
        decoy = root / "keep_me.txt"
        root.mkdir()
        decoy.write_bytes(b"not audio")
        engine = _RecordingEngine()
        engine.error = RuntimeError("engine exploded")
        with pytest.raises(RuntimeError):
            await mgr.submit_audio(sid, "t1", b"bytes", engine=engine, audio_dir=root)
        assert list(root.iterdir()) == [decoy]  # staged file gone, decoy survives

    @pytest.mark.asyncio
    async def test_cleanup_on_timeout(self, tmp_path: Path) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        root = tmp_path / "audio"
        engine = _RecordingEngine()
        engine.error = TimeoutError("timed out")
        with pytest.raises(TimeoutError):
            await mgr.submit_audio(sid, "t1", b"bytes", engine=engine, audio_dir=root)
        assert list(root.iterdir()) == []

    @pytest.mark.asyncio
    async def test_cleanup_on_cancellation(self, tmp_path: Path) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        root = tmp_path / "audio"
        engine = _RecordingEngine()
        engine.delay = 60.0  # blocks until cancelled
        task = asyncio.create_task(
            mgr.submit_audio(sid, "t1", b"bytes", engine=engine, audio_dir=root)
        )
        await asyncio.sleep(0.1)  # let it stage + enter the engine
        assert len(list(root.iterdir())) == 1  # staged while transcribing
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(root.iterdir()) == []  # finally-block cleanup ran

    @pytest.mark.asyncio
    async def test_invalid_audio_payloads_refused_and_stage_nothing(self, tmp_path: Path) -> None:
        mgr = _manager()
        sid = _open(mgr)
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        root = tmp_path / "audio"
        for bad in (b"", "not-bytes", b"x" * (25 * 1024 * 1024 + 1)):  # type: ignore[arg-type]
            with pytest.raises(SessionError) as exc:
                await mgr.submit_audio(sid, "t1", bad, engine=_RecordingEngine(), audio_dir=root)  # type: ignore[arg-type]
            assert exc.value.reason == "invalid_audio"
        assert not root.exists() or list(root.iterdir()) == []

    def test_concurrent_staging_creates_distinct_oexcl_files(self, tmp_path: Path) -> None:
        """8 threads staging at once: O_EXCL + random names give 8 distinct
        files, all 0600, and every one is removable by _cleanup_audio."""
        import concurrent.futures

        root = (tmp_path / "audio").resolve()
        root.mkdir()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(
                pool.map(
                    lambda i: LiveSessionManager._stage_audio(root, f"payload-{i}".encode()),
                    range(8),
                )
            )
        assert len({str(p) for p in paths}) == 8
        for p in paths:
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
            assert p.read_bytes().startswith(b"payload-")
            LiveSessionManager._cleanup_audio(root, p)
        assert list(root.iterdir()) == []

    @pytest.mark.asyncio
    async def test_sequential_turns_get_distinct_files_and_both_cleaned(
        self, tmp_path: Path
    ) -> None:
        mgr = _manager()
        sid = _open(mgr)
        root = tmp_path / "audio"
        mgr.begin_turn(sid, "t1", sequence_number=1, project_revision=7)
        e1 = _RecordingEngine()
        await mgr.submit_audio(sid, "t1", b"one", engine=e1, audio_dir=root)
        mgr.begin_turn(sid, "t2", sequence_number=2, project_revision=7)
        e2 = _RecordingEngine()
        await mgr.submit_audio(sid, "t2", b"two", engine=e2, audio_dir=root)
        assert e1.seen[0][0] != e2.seen[0][0]
        assert list(root.iterdir()) == []


# --------------------------------------------------------------------------- #
# Phase C/D: approval identity isolation at the service level
# --------------------------------------------------------------------------- #


class _RecordingQueue:
    """Recording stand-in for the queue PORT (the interface, not the impl):
    refusal paths must never call enqueue, which this proves."""

    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str:
        self.enqueued.append(
            {"job_type": job_type, "idempotency_key": idempotency_key, "payload": payload}
        )
        return f"job_{len(self.enqueued)}"

    async def get_status(self, job_id: str) -> Any:
        from nexus_ai_agent.application.ports.job_queue import JobStatus

        return JobStatus.COMPLETED

    async def get_result(self, job_id: str) -> dict[str, object] | None:
        return None


def _fake_asset(path: Path) -> None:
    """Any stable bytes work: the security properties under test (identity
    binding, refusal-before-enqueue, asset re-hash) do not depend on real
    media, so probing is stubbed and no ffmpeg is needed."""
    path.write_bytes(b"fake-media-bytes-for-security-tests")


BINDING_REF = "cbn_" + "a" * 64


class _RecordingAuthority:
    """Declared test double for the injected canonical authority PORT.

    It records the (principal, project, binding) it was asked to verify and
    returns server-shaped facts.  ``revoked`` models authority revocation so a
    test can prove execution fails closed when trust is withdrawn.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.revoked = False

    def resolve_principal(self, *, principal_id: str, project_id: str, binding_ref: str):
        from nexus_ai_agent.nagar.ibpc.authority import (
            CanonicalAuthorityError,
            PrincipalFacts,
        )

        self.calls.append((principal_id, project_id, binding_ref))
        if self.revoked:
            raise CanonicalAuthorityError("authorization_denied", "authority revoked")
        return PrincipalFacts(
            principal_id=principal_id, project_id=project_id, channel_binding_id=binding_ref
        )


@pytest.fixture()
def service_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import types

    import nexus_ai_agent.nagar.ibpc.service as service_mod

    monkeypatch.setattr(
        service_mod,
        "probe_media",
        lambda path, *, binary=None: (types.SimpleNamespace(duration_us=2_000_000), 30.0),
    )

    def fake_shadow(path, scope, plan_digest, **kwargs):
        payload = {
            "plan_digest": plan_digest,
            "scope_digest": scope.scope_digest,
            "source_duration_us": 2_000_000,
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
            "computed_at": "2026-10-31T00:00:00Z",
            "signature_digest": "sha256:" + "0" * 64,
        }
        return types.SimpleNamespace(
            signature_digest=payload["signature_digest"],
            scope_digest=scope.scope_digest,
            model_dump=lambda **_kw: payload,
        )

    monkeypatch.setattr(service_mod, "build_shadow_preview", fake_shadow)
    monkeypatch.setattr(service_mod, "compute_scope_signature", fake_signature)
    clip = tmp_path / "src.mp4"
    _fake_asset(clip)
    queue = _RecordingQueue()
    revision = {"current": 7}
    authority = _RecordingAuthority()
    service = IBPCService(
        IBPCServiceDeps(
            queue=queue,  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: revision["current"],
            authority=authority,
            channel_binding_ref=BINDING_REF,
            approval_key_provider=StaticKeyRing({"unit": b"u" * 32}, active_key_id="unit"),
        )
    )
    return service, queue, clip, revision, authority


def _submit(service: IBPCService, sid: str, turn: str, seq: int, source: Path) -> PipelineResult:
    result = service.submit_text_intent(
        sid,
        turn,
        sequence_number=seq,
        text="trim from 0.2 to 0.6 seconds",
        asset_id="src",
        source_path=str(source),
    )
    assert isinstance(result, PipelineResult), result
    return result


class TestServiceIdentityIsolation:
    def test_cross_session_consume_refused_no_job(self, service_env) -> None:
        service, queue, clip, _, _authority = service_env
        sid_a = service.open_session(
            tenant_id="ten-a",
            project_id="p-a",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        sid_b = service.open_session(
            tenant_id="ten-b",
            project_id="p-b",
            principal_id="user-1",
            user_id=2,
            chat_id=2,
            current_revision=7,
        )
        res_a = _submit(service, sid_a, "turn-1", 1, clip)
        res_b = _submit(service, sid_b, "turn-1", 1, clip)
        # session B tries to approve/consume session A's approval
        with pytest.raises(ApprovalError) as exc:
            service.approve(sid_b, "turn-1", res_a.approval_id, principal_id="user-1")
        assert exc.value.reason == "identity_mismatch_session_id"
        import asyncio

        outcome = asyncio.run(
            service.execute(sid_b, "turn-1", res_a.approval_id, principal_id="user-1")
        )
        assert outcome.status == "refused" and outcome.detail.startswith(
            "identity_mismatch_session_id"
        )
        # B's own approval is untouched and still pending
        assert service.approvals.get(res_b.approval_id).state.value == "PENDING"
        assert queue.enqueued == []  # no job was ever enqueued

    def test_cross_turn_consume_refused_no_job(self, service_env) -> None:
        import asyncio

        service, queue, clip, _, _authority = service_env
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        res1 = _submit(service, sid, "turn-1", 1, clip)
        _submit(service, sid, "turn-2", 2, clip)
        with pytest.raises(ApprovalError) as exc:
            service.approve(sid, "turn-2", res1.approval_id, principal_id="user-1")
        assert exc.value.reason == "identity_mismatch_turn_id"
        outcome = asyncio.run(
            service.execute(sid, "turn-2", res1.approval_id, principal_id="user-1")
        )
        assert outcome.status == "refused" and outcome.detail.startswith(
            "identity_mismatch_turn_id"
        )
        assert queue.enqueued == []

    def test_wrong_principal_refused_no_job(self, service_env) -> None:
        import asyncio

        service, queue, clip, _, _authority = service_env
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        res = _submit(service, sid, "turn-1", 1, clip)
        with pytest.raises(ApprovalError) as exc:
            service.approve(sid, "turn-1", res.approval_id, principal_id="mallory")
        assert exc.value.reason == "wrong_principal"
        outcome = asyncio.run(
            service.execute(sid, "turn-1", res.approval_id, principal_id="mallory")
        )
        assert outcome.status == "refused" and outcome.detail.startswith("wrong_principal")
        assert queue.enqueued == []

    def test_stale_revision_refused_no_job(self, service_env) -> None:
        import asyncio

        service, queue, clip, revision, _authority = service_env
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        res = _submit(service, sid, "turn-1", 1, clip)
        service.approve(sid, "turn-1", res.approval_id, principal_id="user-1")
        revision["current"] = 8  # project moved on after the approval
        outcome = asyncio.run(
            service.execute(sid, "turn-1", res.approval_id, principal_id="user-1")
        )
        assert outcome.status == "refused" and outcome.detail.startswith("stale_revision")
        assert queue.enqueued == []
        # and the approval was NOT consumed by the refused request
        assert service.approvals.get(res.approval_id).state.value == "APPROVED"

    def test_asset_changed_after_preview_refused_no_job(self, service_env) -> None:
        import asyncio

        service, queue, clip, _, _authority = service_env
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        res = _submit(service, sid, "turn-1", 1, clip)
        service.approve(sid, "turn-1", res.approval_id, principal_id="user-1")
        clip.write_bytes(clip.read_bytes() + b"tampered")  # asset changed on disk
        outcome = asyncio.run(
            service.execute(sid, "turn-1", res.approval_id, principal_id="user-1")
        )
        assert outcome.status == "refused" and outcome.detail.startswith("asset_changed")
        assert queue.enqueued == []

    def test_deleted_context_fails_closed_no_job(self, service_env) -> None:
        import asyncio

        service, queue, clip, _, _authority = service_env
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        res = _submit(service, sid, "turn-1", 1, clip)
        service.approve(sid, "turn-1", res.approval_id, principal_id="user-1")
        del service._contexts[res.approval_id]  # context vanished
        outcome = asyncio.run(
            service.execute(sid, "turn-1", res.approval_id, principal_id="user-1")
        )
        assert outcome.status == "refused" and "context" in outcome.detail
        assert queue.enqueued == []

    def test_tampered_preview_refused_no_job(self, service_env) -> None:
        import asyncio
        from dataclasses import replace

        service, queue, clip, _, _authority = service_env
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        res = _submit(service, sid, "turn-1", 1, clip)
        service.approve(sid, "turn-1", res.approval_id, principal_id="user-1")
        ctx = service._contexts[res.approval_id]
        other = ctx.preview.model_copy(
            update={"output_duration_us": ctx.preview.output_duration_us + 1}
        )
        service._contexts[res.approval_id] = replace(ctx, preview=other)  # tampered context
        outcome = asyncio.run(
            service.execute(sid, "turn-1", res.approval_id, principal_id="user-1")
        )
        assert outcome.status == "refused" and outcome.detail.startswith("preview_tampered")
        assert queue.enqueued == []

    def test_unknown_approval_and_session_are_typed(self, service_env) -> None:
        import asyncio

        service, queue, clip, _, _authority = service_env
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        with pytest.raises(ApprovalError) as exc:
            service.approve(sid, "turn-1", "appr_nope", principal_id="user-1")
        assert exc.value.reason == "unknown_context"
        outcome = asyncio.run(service.execute(sid, "turn-1", "appr_nope", principal_id="user-1"))
        assert outcome.status == "refused" and "context" in outcome.detail
        # unknown session is a typed SessionError, never a KeyError
        with pytest.raises(SessionError) as exc2:
            service.sessions.get_session("sess_nope")
        assert exc2.value.reason == "unknown_session"
        with pytest.raises(ApprovalError) as exc3:
            service.approve("sess_nope", "turn-1", "appr_x", principal_id="user-1")
        assert exc3.value.reason == "unknown_context"
        assert queue.enqueued == []


class TestCanonicalAuthorityContinuity:
    """Phase 1: identity is verified from server state, and trust withdrawn
    mid-flight fails closed before any job is admitted."""

    def test_authority_revoked_before_execute_refuses_no_job(self, service_env) -> None:
        import asyncio

        service, queue, clip, _, authority = service_env
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        res = _submit(service, sid, "turn-1", 1, clip)
        service.approve(sid, "turn-1", res.approval_id, principal_id="user-1")
        authority.revoked = True
        outcome = asyncio.run(
            service.execute(sid, "turn-1", res.approval_id, principal_id="user-1")
        )
        assert outcome.status == "refused"
        assert "authority_unverified" in outcome.detail
        assert queue.enqueued == []

    def test_unconfigured_authority_refuses_before_admission(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import types

        import nexus_ai_agent.nagar.ibpc.service as service_mod

        monkeypatch.setattr(
            service_mod,
            "probe_media",
            lambda path, *, binary=None: (types.SimpleNamespace(duration_us=2_000_000), 30.0),
        )
        clip = tmp_path / "src.mp4"
        _fake_asset(clip)
        queue = _RecordingQueue()
        service = IBPCService(
            IBPCServiceDeps(
                queue=queue,  # type: ignore[arg-type]
                registry=build_runtime_registry(),
                workspace_root=tmp_path,
                revision_provider=lambda: 7,
                authority=None,
                channel_binding_ref=BINDING_REF,
            )
        )
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        result = service.submit_text_intent(
            sid,
            "turn-1",
            sequence_number=1,
            text="trim from 0.2 to 0.6 seconds",
            asset_id="src",
            source_path=str(clip),
        )
        from nexus_ai_agent.nagar.ibpc.intent import Refusal

        assert isinstance(result, Refusal), result
        assert result.reason == "unauthorized_principal"
        assert queue.enqueued == []


class TestPersistenceTruth:
    def test_sessions_and_approvals_are_in_process_only(self, service_env) -> None:
        """Restart semantics: a NEW service instance over the same deps sees no
        sessions, turns, approvals or contexts — nothing durable is claimed."""
        service, _queue, clip, _, _authority = service_env
        sid = service.open_session(
            tenant_id="ten",
            project_id="p",
            principal_id="user-1",
            user_id=1,
            chat_id=1,
            current_revision=7,
        )
        res = _submit(service, sid, "turn-1", 1, clip)
        assert service.approvals.get(res.approval_id) is not None
        # simulate a restart: fresh service, same injected deps
        service2 = IBPCService(
            IBPCServiceDeps(
                queue=_RecordingQueue(),  # type: ignore[arg-type]
                registry=build_runtime_registry(),
                workspace_root=Path("/tmp"),
                revision_provider=lambda: 7,
            )
        )
        with pytest.raises(SessionError) as exc:
            service2.sessions.get_session(sid)
        assert exc.value.reason == "unknown_session"
        with pytest.raises(ApprovalError):
            service2.approvals.get(res.approval_id)
        assert service2._contexts == {}


# --------------------------------------------------------------------------- #
# Phase E (P1): descriptor-pinned staging — no-follow + content binding
# --------------------------------------------------------------------------- #


def test_staging_refuses_a_symlinked_source(service_env, tmp_path: Path) -> None:
    """The source is opened ``O_RDONLY | O_NOFOLLOW``: a symlinked source is
    refused (never followed), so the bytes staged are the admitted inode."""
    service, _queue, clip, _revision, _authority = service_env
    link = tmp_path / "link.mp4"
    link.symlink_to(clip)
    workspace = tmp_path / "ws_abc"
    with pytest.raises(SessionError) as exc:
        service._stage_source(link, workspace)
    assert exc.value.reason == "source_unreadable"


def test_staging_refuses_a_symlinked_destination(service_env, tmp_path: Path) -> None:
    """The destination is opened with ``O_NOFOLLOW`` too: a planted symlink at
    the workspace's ``input.mp4`` cannot redirect the staged write."""
    service, _queue, clip, _revision, _authority = service_env
    outside = tmp_path / "outside_target"
    outside.write_bytes(b"do-not-touch")
    workspace = tmp_path / "ws_def"
    workspace.mkdir()
    (workspace / "input.mp4").symlink_to(outside)
    with pytest.raises(SessionError) as exc:
        service._stage_source(clip, workspace)
    assert exc.value.reason == "staging_failed"
    assert outside.read_bytes() == b"do-not-touch"  # the redirect target is untouched


def test_staging_binds_content_identity(service_env, tmp_path: Path) -> None:
    """A clean staging copy is byte-identical to the source, and ``_sha256_fd``
    reads it without following a final symlink."""
    service, _queue, clip, _revision, _authority = service_env
    workspace = tmp_path / "ws_ghi"
    staged = service._stage_source(clip, workspace)
    assert staged.read_bytes() == clip.read_bytes()
    assert service._sha256_fd(staged) == service._sha256_fd(clip)


def _open_service_session(service: IBPCService) -> str:
    return service.open_session(
        tenant_id="ten",
        project_id="p",
        principal_id="user-1",
        user_id=1,
        chat_id=1,
        current_revision=7,
    )


def test_submit_missing_source_is_typed_refusal(service_env, tmp_path: Path) -> None:
    service, queue, _clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    result = service.submit_text_intent(
        sid,
        "missing-source",
        sequence_number=1,
        text="trim from 0.2 to 0.6 seconds",
        asset_id="src",
        source_path=str(tmp_path / "absent.mp4"),
    )
    from nexus_ai_agent.nagar.ibpc.intent import Refusal

    assert isinstance(result, Refusal) and result.reason == "missing_source"
    assert queue.enqueued == []


def test_submit_unknown_base_and_changed_delta_source_refuse(service_env, tmp_path: Path) -> None:
    service, queue, clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    from nexus_ai_agent.nagar.ibpc.intent import Refusal

    unknown = service.submit_text_intent(
        sid,
        "delta-unknown",
        sequence_number=1,
        text="trim 1 second from the end",
        asset_id="src",
        source_path=str(clip),
        base_approval_id="missing",
    )
    assert isinstance(unknown, Refusal) and unknown.reason == "unknown_base_proposal"
    base = _submit(service, sid, "delta-base", 2, clip)
    changed = tmp_path / "different.mp4"
    _fake_asset(changed)
    changed.write_bytes(changed.read_bytes() + b"distinct")
    result = service.submit_text_intent(
        sid,
        "delta-changed-source",
        sequence_number=3,
        text="trim 0.1 seconds from the end",
        asset_id="src",
        source_path=str(changed),
        base_approval_id=base.approval_id,
    )
    assert isinstance(result, Refusal) and result.reason == "delta_source_mismatch"
    assert queue.enqueued == []


def test_approved_base_cannot_be_refined(service_env) -> None:
    service, queue, clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    base = _submit(service, sid, "approved-base", 1, clip)
    service.approve(sid, "approved-base", base.approval_id, principal_id="user-1")
    from nexus_ai_agent.nagar.ibpc.intent import Refusal

    result = service.submit_text_intent(
        sid,
        "refine-approved",
        sequence_number=2,
        text="trim 0.1 seconds from the end",
        asset_id="src",
        source_path=str(clip),
        base_approval_id=base.approval_id,
    )
    assert isinstance(result, Refusal) and result.reason == "stale_base_proposal"
    assert queue.enqueued == []


def test_compilation_and_media_evidence_failures_refuse(service_env, monkeypatch) -> None:
    import nexus_ai_agent.nagar.ibpc.service as service_mod
    from nexus_ai_agent.nagar.ibpc.compiler import CompilationRefused
    from nexus_ai_agent.nagar.ibpc.intent import Refusal

    service, queue, clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    original_compile = service_mod.compile_ast
    monkeypatch.setattr(
        service_mod,
        "compile_ast",
        lambda *a, **k: (_ for _ in ()).throw(CompilationRefused("test_refusal")),
    )
    compiled = service.submit_text_intent(
        sid,
        "compile-fail",
        sequence_number=1,
        text="trim from 0.2 to 0.6 seconds",
        asset_id="src",
        source_path=str(clip),
    )
    assert isinstance(compiled, Refusal) and compiled.reason == "test_refusal"
    monkeypatch.setattr(service_mod, "compile_ast", original_compile)
    sid2 = _open_service_session(service)
    monkeypatch.setattr(
        service_mod,
        "build_shadow_preview",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ffmpeg down")),
    )
    evidence = service.submit_text_intent(
        sid2,
        "shadow-fail",
        sequence_number=1,
        text="trim from 0.2 to 0.6 seconds",
        asset_id="src",
        source_path=str(clip),
    )
    assert isinstance(evidence, Refusal) and evidence.reason == "media_evidence_unavailable"
    assert queue.enqueued == []


@pytest.mark.asyncio
async def test_voice_configuration_and_transcription_fail_closed(service_env, monkeypatch) -> None:
    import types

    from nexus_ai_agent.nagar.ibpc.intent import Refusal

    service, queue, clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    service._deps.caption_engine = None
    unavailable = await service.submit_voice_intent(
        sid, "voice-no-engine", sequence_number=1, audio=b"x", asset_id="src", source_path=str(clip)
    )
    assert isinstance(unavailable, Refusal) and unavailable.reason == "caption_profile_unavailable"

    service._deps.caption_engine = types.SimpleNamespace(is_available=lambda: True)

    async def fail_audio(*args, **kwargs):
        raise RuntimeError("decoder failure")

    monkeypatch.setattr(service._sessions, "submit_audio", fail_audio)
    failed = await service.submit_voice_intent(
        sid, "voice-fail", sequence_number=2, audio=b"x", asset_id="src", source_path=str(clip)
    )
    assert isinstance(failed, Refusal) and failed.reason == "transcription_failed"
    assert queue.enqueued == []


def test_missing_channel_binding_is_a_typed_refusal(service_env) -> None:
    service, queue, clip, _revision, _authority = service_env
    service._deps.channel_binding_ref = None
    sid = _open_service_session(service)
    result = service.submit_text_intent(
        sid,
        "no-binding",
        sequence_number=1,
        text="trim from 0.2 to 0.6 seconds",
        asset_id="src",
        source_path=str(clip),
    )
    from nexus_ai_agent.nagar.ibpc.intent import Refusal

    assert isinstance(result, Refusal) and result.reason == "unauthorized_principal"
    assert queue.enqueued == []


def test_approval_provider_and_clock_fail_closed(service_env) -> None:
    from nexus_ai_agent.connect.key_provider import KeyProviderError
    from nexus_ai_agent.nagar.ibpc.approval import ApprovalError

    service, _queue, clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    proposal = _submit(service, sid, "invalid-provider", 1, clip)

    class BrokenKeyProvider:
        active_key_id = "broken"

        def key(self, key_id: str):
            raise KeyProviderError("missing configured key")

    service._deps.approval_key_provider = BrokenKeyProvider()  # type: ignore[assignment]
    with pytest.raises(ApprovalError, match="approval_key_unavailable"):
        service.approve(sid, "invalid-provider", proposal.approval_id, principal_id="user-1")

    service._deps.approval_key_provider = StaticKeyRing({"unit": b"u" * 32}, active_key_id="unit")
    sid2 = _open_service_session(service)
    second = _submit(service, sid2, "naive-clock", 1, clip)
    service._deps.now = lambda: "2026-10-31T00:00:00"
    with pytest.raises(ApprovalError, match="approval_key_unavailable"):
        service.approve(sid2, "naive-clock", second.approval_id, principal_id="user-1")


def test_execute_enqueue_failure_and_missing_attempt_evidence(service_env, monkeypatch) -> None:
    import asyncio

    import nexus_ai_agent.nagar.ibpc.service as service_mod

    monkeypatch.setattr(service_mod, "verify_shadow_manifest", lambda _manifest: None)
    service, queue, clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    proposal = _submit(service, sid, "no-result", 1, clip)
    service.approve(sid, "no-result", proposal.approval_id, principal_id="user-1")
    outcome = asyncio.run(
        service.execute(sid, "no-result", proposal.approval_id, principal_id="user-1")
    )
    assert outcome.status == "failed"
    assert len(queue.enqueued) == 1
    assert (
        asyncio.run(service._attempt_identity("job-without-attempts"))
        == "job-without-attempts:attempt?"
    )
    assert service._provenance_reference("job-without-evidence") == {
        "job_id": "job-without-evidence",
        "journal": None,
        "passport": None,
    }


def test_missing_or_deleted_approval_context_is_terminal(service_env) -> None:
    service, _queue, _clip, _revision, _authority = service_env
    assert service._is_terminal_context("not-in-ledger") is True
    assert service._provenance_reference("unrecorded-job")["journal"] is None


@pytest.mark.asyncio
async def test_voice_pipeline_refusal_branches(service_env, tmp_path: Path, monkeypatch) -> None:
    import nexus_ai_agent.nagar.ibpc.service as service_mod
    from nexus_ai_agent.creative.slideshow.ffmpeg import RenderError
    from nexus_ai_agent.nagar.ibpc.intent import Refusal

    service, queue, clip, _revision, authority = service_env

    async def run_voice(turn_id: str, engine: _RecordingEngine, source: Path = clip):
        sid = _open_service_session(service)
        service._deps.caption_engine = engine
        return await service.submit_voice_intent(
            sid,
            turn_id,
            sequence_number=1,
            audio=b"audio",
            asset_id="src",
            source_path=str(source),
            audio_dir=tmp_path / f"audio-{turn_id}",
        )

    authority.revoked = True
    denied = await run_voice("voice-authority", _RecordingEngine())
    assert isinstance(denied, Refusal) and denied.reason == "unauthorized_principal"
    authority.revoked = False

    missing = await run_voice("voice-missing", _RecordingEngine(), tmp_path / "absent.mp4")
    assert isinstance(missing, Refusal) and missing.reason == "missing_source"

    ambiguous = await run_voice("voice-ambiguous", _RecordingEngine("trim it a bit"))
    assert isinstance(ambiguous, (Refusal,)) or hasattr(ambiguous, "questions")

    sid = _open_service_session(service)
    service._deps.caption_engine = _RecordingEngine()
    original_probe = service_mod.probe_media
    monkeypatch.setattr(
        service_mod, "probe_media", lambda *a, **k: (_ for _ in ()).throw(RenderError("bad media"))
    )
    unreadable = await service.submit_voice_intent(
        sid,
        "voice-unreadable",
        sequence_number=1,
        audio=b"audio",
        asset_id="src",
        source_path=str(clip),
        audio_dir=tmp_path / "audio-unreadable",
    )
    assert isinstance(unreadable, Refusal) and unreadable.reason == "unreadable_source"
    monkeypatch.setattr(service_mod, "probe_media", original_probe)

    sid2 = _open_service_session(service)
    original_compile = service_mod.compile_ast
    monkeypatch.setattr(
        service_mod,
        "compile_ast",
        lambda *a, **k: (_ for _ in ()).throw(service_mod.CompilationRefused("voice_compile")),
    )
    compile_refusal = await service.submit_voice_intent(
        sid2,
        "voice-compile",
        sequence_number=1,
        audio=b"audio",
        asset_id="src",
        source_path=str(clip),
        audio_dir=tmp_path / "audio-compile",
    )
    assert isinstance(compile_refusal, Refusal) and compile_refusal.reason == "voice_compile"
    monkeypatch.setattr(service_mod, "compile_ast", original_compile)

    sid3 = _open_service_session(service)
    original_shadow = service_mod.build_shadow_preview
    monkeypatch.setattr(
        service_mod,
        "build_shadow_preview",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("shadow")),
    )
    media_refusal = await service.submit_voice_intent(
        sid3,
        "voice-shadow",
        sequence_number=1,
        audio=b"audio",
        asset_id="src",
        source_path=str(clip),
        audio_dir=tmp_path / "audio-shadow",
    )
    assert (
        isinstance(media_refusal, Refusal) and media_refusal.reason == "media_evidence_unavailable"
    )
    monkeypatch.setattr(service_mod, "build_shadow_preview", original_shadow)
    assert queue.enqueued == []


@pytest.mark.parametrize(
    ("field", "replacement", "reason"),
    [
        ("sequence_number", 7, "identity_mismatch_sequence_number"),
        ("tenant_id", "other-tenant", "identity_mismatch_tenant_id"),
        ("project_id", "other-project", "identity_mismatch_project_id"),
    ],
)
def test_binding_identity_field_tamper_is_refused(
    service_env, field: str, replacement: object, reason: str
) -> None:
    service, _queue, clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    proposal = _submit(service, sid, f"binding-{field}", 1, clip)
    binding = service.approvals.get(proposal.approval_id)
    service.approvals._bindings[proposal.approval_id] = binding.model_copy(
        update={field: replacement}
    )
    with pytest.raises(ApprovalError, match=reason):
        service.approve(sid, f"binding-{field}", proposal.approval_id, principal_id="user-1")


def test_context_binding_ast_and_plan_tampering_are_refused(service_env) -> None:
    service, _queue, clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    proposal = _submit(service, sid, "context-tamper", 1, clip)
    ctx = service._contexts[proposal.approval_id]
    original_session, original_binding = ctx.session_id, ctx.binding_ref
    ctx.session_id = "another-session"
    with pytest.raises(ApprovalError, match="context_mismatch"):
        service.approve(sid, "context-tamper", proposal.approval_id, principal_id="user-1")
    ctx.session_id = original_session
    ctx.binding_ref = "cbn_" + "b" * 64
    with pytest.raises(ApprovalError, match="binding_mismatch"):
        service.approve(sid, "context-tamper", proposal.approval_id, principal_id="user-1")
    ctx.binding_ref = original_binding
    ctx.ast = ctx.ast.model_copy(update={"ast_id": "tampered-ast"})
    with pytest.raises(ApprovalError, match="ast_mismatch"):
        service.approve(sid, "context-tamper", proposal.approval_id, principal_id="user-1")


def test_plan_tamper_and_asset_unreadable_are_refused(service_env) -> None:
    service, _queue, clip, _revision, _authority = service_env
    sid = _open_service_session(service)
    proposal = _submit(service, sid, "plan-tamper", 1, clip)
    ctx = service._contexts[proposal.approval_id]
    ctx.plan = ctx.plan.model_copy(update={"plan_id": "tampered-plan"})
    with pytest.raises(ApprovalError, match="plan_mismatch"):
        service.approve(sid, "plan-tamper", proposal.approval_id, principal_id="user-1")

    sid2 = _open_service_session(service)
    proposal2 = _submit(service, sid2, "asset-unreadable", 1, clip)
    clip.unlink()
    with pytest.raises(ApprovalError, match="asset_unreadable"):
        service.approve(sid2, "asset-unreadable", proposal2.approval_id, principal_id="user-1")


def test_attempt_identity_reads_latest_sync_history(service_env) -> None:
    service, _queue, _clip, _revision, _authority = service_env
    service._deps.attempt_provider = lambda job_id: [{"attempt": 1}, {"attempt": 4}]
    import asyncio

    assert asyncio.run(service._attempt_identity("job-1")) == "job-1:attempt4"


def test_composition_factory_wires_explicit_key_provider(service_env) -> None:
    from nexus_ai_agent.connect.key_provider import StaticKeyRing
    from nexus_ai_agent.nagar.ibpc.composition import build_ibpc_service

    service, _queue, _clip, _revision, _authority = service_env
    ring = StaticKeyRing({"old": b"o" * 32, "active": b"a" * 32}, active_key_id="active")
    composed = build_ibpc_service(service._deps, key_provider=ring)
    assert composed._deps.approval_key_provider is ring
    assert composed._deps.approval_key_provider.key("old").material == b"o" * 32


def test_composition_factory_rejects_provider_with_missing_active_key(service_env) -> None:
    from nexus_ai_agent.nagar.ibpc.approval_mac import KeyProviderError
    from nexus_ai_agent.nagar.ibpc.composition import build_ibpc_service

    service, _queue, _clip, _revision, _authority = service_env

    class MissingActiveKey:
        @property
        def active_key_id(self) -> str:
            return "missing"

        def key(self, key_id: str):
            raise KeyProviderError("unknown key")

    with pytest.raises(KeyProviderError, match="not configured correctly"):
        build_ibpc_service(service._deps, key_provider=MissingActiveKey())  # type: ignore[arg-type]
