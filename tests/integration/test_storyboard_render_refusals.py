"""NAGAR One V1: every fail-closed refusal of the storyboard render chain.

REAL-DISK: each render runs in a temporary studio workspace with a durable
approval ledger. REAL-COMPUTE: the success path uses a real ffmpeg; the failure
paths use real subprocess outcomes (missing binary, non-zero exit, timeout).
"""

from __future__ import annotations

import asyncio
import hashlib
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus_ai_agent.creative import storyboard_render as sr
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.creative.storyboard_render import (
    RenderContext,
    StoryboardRenderError,
    build_ffmpeg_argv,
    issue_render_approval,
    make_storyboard_render_handler,
    render_storyboard_sync,
)
from nexus_ai_agent.nagar.creative.handoff import build_storyboard_render_payload
from nexus_ai_agent.nagar.ibpc.approval import (
    DurableApprovalLedger,
    durable_approval_ledger_path,
)

SIGNING = "s" * 48
SCRIPT = "INT. KITCHEN - DAY\nSara: Hello there.\nEXT. STREET - NIGHT\nAli: Goodbye.\n"
PROFILE = {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}


@pytest.fixture()
def ctx(tmp_path: Path) -> RenderContext:
    workspace = tmp_path / "studio"
    workspace.mkdir()
    ledger = DurableApprovalLedger(
        durable_approval_ledger_path(workspace), now=lambda: "2026-11-19T06:00:00Z"
    )
    yield RenderContext(workspace_root=workspace, approval_ledger=ledger, signing_key=SIGNING)  # type: ignore[misc]
    ledger.close()


def _payload(ctx: RenderContext, job_id: str = "job-r-1", **overrides: object) -> dict[str, object]:
    """A fresh, approved payload for one render attempt."""
    compiled = compile_storyboard(SCRIPT)
    src = hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest()
    from nexus_ai_agent.creative import materializer as mz

    approval_id = issue_render_approval(
        ctx.approval_ledger,
        project_id="proj-r",
        job_id=job_id,
        principal_id="owner-r",
        source_sha256=src,
        script_sha256=compiled.script_sha256,
        manifest_sha256=compiled.manifest_sha256,
        render_profile_hash=mz.RenderProfile.from_mapping(PROFILE).profile_hash(),
    )
    base: dict[str, object] = {
        "project_id": "proj-r",
        "job_id": job_id,
        "screenplay_text": SCRIPT,
        "source_sha256": src,
        "manifest_sha256": compiled.manifest_sha256,
        "approval_id": approval_id,
        "principal_id": "owner-r",
        **PROFILE,
    }
    base.update(overrides)
    return build_storyboard_render_payload(base)


def _fake_binary(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _outputs(ctx: RenderContext) -> list[Path]:
    return sorted((ctx.workspace_root).rglob("*.mp4")) + sorted(
        (ctx.workspace_root).rglob("*.passport.json")
    )


# ----------------------------- success path -----------------------------


def test_a_fully_approved_render_produces_an_mp4_and_a_passport(ctx: RenderContext) -> None:
    result = render_storyboard_sync(_payload(ctx), ctx)
    assert result["output_rel_path"].endswith(".mp4")
    assert (ctx.workspace_root / result["output_rel_path"]).is_file()


def test_the_queue_handler_runs_the_chain_in_a_worker_thread(ctx: RenderContext) -> None:
    handler = make_storyboard_render_handler(ctx)
    result = asyncio.run(handler(dict(_payload(ctx, job_id="job-r-handler"))))  # type: ignore[arg-type]
    assert result["output_rel_path"].endswith(".mp4")


def test_the_same_approval_cannot_render_twice(ctx: RenderContext) -> None:
    payload = _payload(ctx, job_id="job-r-replay")
    render_storyboard_sync(payload, ctx)  # type: ignore[arg-type]
    with pytest.raises(StoryboardRenderError):
        render_storyboard_sync(payload, ctx)  # type: ignore[arg-type]


# ----------------------------- request and compile refusals -----------------------------


def test_an_out_of_contract_profile_is_refused_before_any_work(ctx: RenderContext) -> None:
    payload = _payload(ctx, job_id="job-r-profile")
    payload = dict(payload)  # type: ignore[arg-type]
    payload["fps"] = 999
    with pytest.raises(StoryboardRenderError):
        render_storyboard_sync(payload, ctx)  # type: ignore[arg-type]


def test_a_compile_failure_is_a_typed_refusal(
    ctx: RenderContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(ctx, job_id="job-r-compile")

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("compiler exploded")

    monkeypatch.setattr(sr, "compile_storyboard", _boom)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(payload, ctx)  # type: ignore[arg-type]
    assert info.value.reason == "compile_failed"


def test_a_failed_manifest_integrity_check_is_refused(
    ctx: RenderContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(ctx, job_id="job-r-integrity")
    monkeypatch.setattr(sr, "verify_manifest_integrity", lambda *_a, **_k: False)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(payload, ctx)  # type: ignore[arg-type]
    assert info.value.reason == "manifest_integrity_failed"


def test_a_manifest_digest_that_disagrees_is_refused(ctx: RenderContext) -> None:
    payload = dict(_payload(ctx, job_id="job-r-mdigest"))  # type: ignore[arg-type]
    payload["manifest_sha256"] = "e" * 64
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(payload, ctx)
    assert info.value.reason == "manifest_digest_mismatch"


# ----------------------------- ffmpeg outcomes -----------------------------


def test_a_missing_ffmpeg_binary_is_unavailable(ctx: RenderContext) -> None:
    ctx = RenderContext(
        workspace_root=ctx.workspace_root,
        approval_ledger=ctx.approval_ledger,
        signing_key=SIGNING,
        ffmpeg="/nonexistent/ffmpeg-binary",
    )
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(_payload(ctx, job_id="job-r-noffmpeg"), ctx)  # type: ignore[arg-type]
    assert info.value.reason == "ffmpeg_unavailable"


def test_a_failing_ffmpeg_is_refused_with_its_exit_code(ctx: RenderContext, tmp_path: Path) -> None:
    failing = _fake_binary(tmp_path, "ffmpeg-fail", "echo boom >&2\nexit 3")
    ctx = RenderContext(
        workspace_root=ctx.workspace_root,
        approval_ledger=ctx.approval_ledger,
        signing_key=SIGNING,
        ffmpeg=failing,
    )
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(_payload(ctx, job_id="job-r-ffail"), ctx)  # type: ignore[arg-type]
    assert info.value.reason == "ffmpeg_failed"
    assert "rc=3" in info.value.detail
    assert not any(p.suffix == ".mp4" for p in _outputs(ctx))


def test_a_hung_ffmpeg_times_out(ctx: RenderContext, tmp_path: Path) -> None:
    hanging = _fake_binary(tmp_path, "ffmpeg-hang", "sleep 30")
    ctx = RenderContext(
        workspace_root=ctx.workspace_root,
        approval_ledger=ctx.approval_ledger,
        signing_key=SIGNING,
        ffmpeg=hanging,
        ffmpeg_timeout_s=0.3,
    )
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(_payload(ctx, job_id="job-r-hang"), ctx)  # type: ignore[arg-type]
    assert info.value.reason == "ffmpeg_timeout"


# ----------------------------- publication failures -----------------------------


def test_an_unexpected_passport_failure_is_a_render_failure_and_leaves_no_artifact(
    ctx: RenderContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(*_a: object, **_k: object) -> None:
        raise ValueError("disk full")

    monkeypatch.setattr(sr, "build_passport", _broken)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(_payload(ctx, job_id="job-r-passport"), ctx)  # type: ignore[arg-type]
    assert info.value.reason == "render_failed"
    assert not any(p.suffix == ".mp4" for p in _outputs(ctx))


def test_an_artifact_that_changes_after_verification_is_refused(
    ctx: RenderContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sr.stream_sha256
    calls = {"n": 0}

    def _mutating(path: Path) -> tuple[str, int, int]:
        digest, size, inode = real(path)
        calls["n"] += 1
        return ("0" * 64, size, inode) if path.suffix == ".mp4" else (digest, size, inode)

    monkeypatch.setattr(sr, "stream_sha256", _mutating)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(_payload(ctx, job_id="job-r-mutate"), ctx)  # type: ignore[arg-type]
    assert info.value.reason in {"artifact_changed_after_verify", "verifier_violated"}


# ----------------------------- argv construction -----------------------------


def test_an_argv_without_a_complete_bundle_is_refused() -> None:
    bundle = SimpleNamespace(assets=(), profile=None, shot_durations_s=())
    with pytest.raises(StoryboardRenderError) as info:
        build_ffmpeg_argv("ffmpeg", bundle, "/tmp/out.mp4")  # type: ignore[arg-type]
    assert info.value.reason == "bundle_incomplete"


def test_a_malformed_request_never_reaches_the_chain(ctx: RenderContext) -> None:
    with pytest.raises(StoryboardRenderError):
        render_storyboard_sync({"job_id": "x"}, ctx)  # type: ignore[arg-type]


def test_a_materializer_refusal_is_surfaced_with_its_own_reason(
    ctx: RenderContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative.materializer import MaterializationError

    def _refuse(*_a: object, **_k: object) -> None:
        raise MaterializationError("provenance_violated", "forced for the test")

    monkeypatch.setattr(sr.StoryboardMediaMaterializer, "materialize", _refuse)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(_payload(ctx, job_id="job-r-mat"), ctx)  # type: ignore[arg-type]
    assert info.value.reason == "provenance_violated"


def test_a_media_verdict_that_is_not_pass_stops_before_publication(
    ctx: RenderContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative import artifact_verifier as av

    failing = av.Mp4Verdict(status=av.VIOLATED, reasons=("forced",))
    monkeypatch.setattr(sr, "verify_mp4", lambda *_a, **_k: failing)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(_payload(ctx, job_id="job-r-verdict"), ctx)  # type: ignore[arg-type]
    assert info.value.reason == "verifier_violated"
    assert not any(p.suffix == ".mp4" for p in _outputs(ctx))


def test_an_unexpected_error_before_the_move_leaves_nothing_behind(
    ctx: RenderContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(*_a: object, **_k: object) -> list[str]:
        raise RuntimeError("argv builder exploded")

    monkeypatch.setattr(sr, "build_ffmpeg_argv", _broken)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(_payload(ctx, job_id="job-r-early"), ctx)  # type: ignore[arg-type]
    assert info.value.reason == "render_failed"
    assert not any(p.suffix == ".mp4" for p in _outputs(ctx))


def test_an_argv_ignores_assets_that_are_neither_images_nor_tones(
    ctx: RenderContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note-kind asset in a real bundle is skipped; the argv is built from the rest."""
    from dataclasses import replace

    from nexus_ai_agent.creative import materializer as mz

    compiled = compile_storyboard(SCRIPT)
    ws = tmp_path / "argv-ws"
    ws.mkdir()
    mat = mz.StoryboardMediaMaterializer(ws, mz.RenderProfile.from_mapping(PROFILE))
    bundle = mat.materialize(compiled, ws / "out")
    note = replace(bundle.assets[0], kind="note", shot_id="note-0")
    padded = replace(bundle, assets=(*bundle.assets, note))
    argv = build_ffmpeg_argv("ffmpeg", padded, str(ws / "final.mp4"))
    assert argv[0] == "ffmpeg"
    assert all("note" not in part for part in argv)
