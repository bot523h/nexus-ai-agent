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


@pytest.fixture()
def service_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    revision = {"current": 7}
    service = IBPCService(
        IBPCServiceDeps(
            queue=queue,  # type: ignore[arg-type]
            registry=build_runtime_registry(),
            workspace_root=tmp_path,
            revision_provider=lambda: revision["current"],
        )
    )
    return service, queue, clip, revision


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
        service, queue, clip, _ = service_env
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

        service, queue, clip, _ = service_env
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

        service, queue, clip, _ = service_env
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

        service, queue, clip, revision = service_env
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

        service, queue, clip, _ = service_env
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

        service, queue, clip, _ = service_env
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

        service, queue, clip, _ = service_env
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

        service, queue, clip, _ = service_env
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


class TestPersistenceTruth:
    def test_sessions_and_approvals_are_in_process_only(self, service_env) -> None:
        """Restart semantics: a NEW service instance over the same deps sees no
        sessions, turns, approvals or contexts — nothing durable is claimed."""
        service, _queue, clip, _ = service_env
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
