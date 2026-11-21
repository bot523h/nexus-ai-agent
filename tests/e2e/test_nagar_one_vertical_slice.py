"""NAGAR One V1 vertical slice: screenplay -> verified MP4 -> passport -> download gate.

Positive path uses the real compiler, real FFmpeg and real ffprobe. Adversarial
cases attack each trust boundary: approval replay and mutation, TOCTOU between
materialize and render, a lying ffmpeg (exit 0 with garbage output), post-render
tampering at download time, and path traversal.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from nexus_ai_agent.creative import materializer as mz
from nexus_ai_agent.creative.artifact_download import DownloadRefused, authorize_artifact_download
from nexus_ai_agent.creative.artifact_passport import PASSPORT_SUFFIX, verify_artifact_passport
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.creative.storyboard_render import (
    RenderContext,
    StoryboardRenderError,
    issue_render_approval,
    render_storyboard_sync,
)
from nexus_ai_agent.creative.studio_cli import StudioCliError, studio_doctor, studio_init
from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalLedger,
    DurableApprovalLedger,
    durable_approval_ledger_path,
)

SIGNING_KEY = "s" * 48
SCRIPT = (
    "INT. KITCHEN - DAY\n"
    "Sara: Hello, are you there?\n"
    "Ali: Yes, I am here.\n"
    "EXT. STREET - NIGHT\n"
    "سارا: سلام، حالت چطوره؟\n"
)
PROFILE = {"width": 320, "height": 180, "fps": 12, "sample_rate": 16000}


class Env:
    """One isolated studio: workspace, ledger, context and a helper to submit jobs."""

    def __init__(self, root: Path, ledger: ApprovalLedger | None = None) -> None:
        self.root = root
        self.ws = root / "workspace"
        self.ws.mkdir(parents=True)
        self.ledger = ledger or ApprovalLedger(now=lambda: "2026-11-18T07:40:00Z")
        self.ctx = RenderContext(
            workspace_root=self.ws,
            approval_ledger=self.ledger,
            signing_key=SIGNING_KEY,
            clock=lambda: 1_700_000_000.0,
        )
        self.text = SCRIPT
        self.compiled = compile_storyboard(SCRIPT)
        self.src = hashlib.sha256(SCRIPT.encode("utf-8")).hexdigest()
        self.profile_hash = mz.RenderProfile.from_mapping(PROFILE).profile_hash()

    def approve(self, job_id: str = "job-1", *, profile_hash: str | None = None) -> str:
        return issue_render_approval(
            self.ledger,
            project_id="proj-1",
            job_id=job_id,
            principal_id="owner-1",
            source_sha256=self.src,
            script_sha256=self.compiled.script_sha256,
            manifest_sha256=self.compiled.manifest_sha256,
            render_profile_hash=profile_hash or self.profile_hash,
        )

    def payload(self, approval_id: str, job_id: str = "job-1", **overrides: object) -> dict:
        data: dict = {
            "project_id": "proj-1",
            "job_id": job_id,
            "screenplay_text": self.text,
            "source_sha256": self.src,
            "manifest_sha256": self.compiled.manifest_sha256,
            "approval_id": approval_id,
            "principal_id": "owner-1",
            **PROFILE,
        }
        data.update(overrides)
        return data

    def artifact(self, job_id: str = "job-1") -> Path:
        return self.ws / "projects" / "proj-1" / "renders" / job_id / "storyboard.mp4"


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _fake_ffmpeg(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(f"#!{sys.executable}\nimport sys\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


# ----------------------------- positive path -----------------------------


def test_positive_end_to_end_real_ffmpeg_pass_and_passport(env: Env) -> None:
    approval = env.approve()
    result = render_storyboard_sync(env.payload(approval), env.ctx)

    assert result["status"] == "rendered"
    assert result["verifier_status"] == "PASS"
    assert result["total_shots"] == env.compiled.total_shots
    out = env.artifact()
    assert out.is_file() and out.stat().st_size == result["output_size_bytes"] > 0
    assert hashlib.sha256(out.read_bytes()).hexdigest() == result["output_sha256"]

    passport = out.parent / f"storyboard{PASSPORT_SUFFIX}"
    check = verify_artifact_passport(passport, out, SIGNING_KEY)
    assert check.passport["manifest_sha256"] == env.compiled.manifest_sha256
    assert check.passport["render_profile_hash"] == env.profile_hash
    assert check.passport["asset_merkle_root"] == result["asset_merkle_root"]

    # The download gate serves exactly the verified bytes.
    authorized = authorize_artifact_download(env.ws, result["output_rel_path"], SIGNING_KEY)
    assert authorized.path == out.resolve()
    assert authorized.path.read_bytes() == out.read_bytes()


def test_no_stray_temp_files_remain_after_success(env: Env) -> None:
    render_storyboard_sync(env.payload(env.approve()), env.ctx)
    leftovers = [p for p in env.artifact().parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# ----------------------------- approval: replay and mutation -----------------------------


def test_durable_approval_survives_restarts_and_replay_after_restart_is_refused(
    tmp_path: Path,
) -> None:
    """Issue → restart → render → restart → replay is refused fail-closed."""
    ws_root = tmp_path / "studio"
    ws_root.mkdir()
    db_path = durable_approval_ledger_path(ws_root / "workspace")
    clock = lambda: "2026-11-18T07:40:00Z"  # noqa: E731

    first = Env(tmp_path / "first", DurableApprovalLedger(db_path, now=clock))
    approval = first.approve()
    first.ledger.close()  # simulated process exit

    second = Env(tmp_path / "second", DurableApprovalLedger(db_path, now=clock))
    second.ws = first.ws
    second.ctx = RenderContext(
        workspace_root=first.ws,
        approval_ledger=second.ledger,
        signing_key=SIGNING_KEY,
        clock=lambda: 1_700_000_000.0,
    )
    result = render_storyboard_sync(second.payload(approval), second.ctx)
    assert result["status"] == "rendered"
    assert result["verifier_status"] == "PASS"
    second.ledger.close()

    third_ledger = DurableApprovalLedger(db_path, now=clock)
    third = Env(tmp_path / "third", third_ledger)
    third.ws = first.ws
    third.ctx = RenderContext(
        workspace_root=first.ws,
        approval_ledger=third_ledger,
        signing_key=SIGNING_KEY,
        clock=lambda: 1_700_000_000.0,
    )
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(third.payload(approval, job_id="job-2"), third.ctx)
    assert info.value.reason == "approval_replay"
    assert not third.artifact("job-2").exists()
    third_ledger.close()


def test_replayed_approval_is_refused_and_creates_no_second_artifact(env: Env) -> None:
    approval = env.approve()
    render_storyboard_sync(env.payload(approval), env.ctx)
    before = env.artifact().stat().st_mtime_ns
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(env.payload(approval, job_id="job-2"), env.ctx)
    assert info.value.reason == "approval_replay"
    assert env.artifact().stat().st_mtime_ns == before
    assert not env.artifact("job-2").exists()


def test_changed_render_profile_after_approval_is_refused_without_burning_it(env: Env) -> None:
    approval = env.approve()  # approved for the original profile
    tampered = env.payload(approval, width=640, height=360)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(tampered, env.ctx)
    assert info.value.reason == "approval_identity_mismatch_preview_digest"
    assert not env.artifact().exists()
    # The mismatch did not consume the approval: the correct request still succeeds.
    result = render_storyboard_sync(env.payload(approval), env.ctx)
    assert result["verifier_status"] == "PASS"


def test_edited_screenplay_after_approval_is_refused_at_handoff(env: Env) -> None:
    approval = env.approve()
    edited = env.payload(approval, screenplay_text=SCRIPT + "Ali: Actually, no.\n")
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(edited, env.ctx)
    assert info.value.reason == "source_digest_mismatch"
    assert not env.artifact().exists()


def test_unknown_approval_is_refused(env: Env) -> None:
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(env.payload("appr-does-not-exist"), env.ctx)
    assert info.value.reason == "approval_unknown_approval"


# ----------------------------- request hardening -----------------------------


@pytest.mark.parametrize("bad_fps", [float("nan"), float("inf"), float("-inf"), True])
def test_non_finite_or_bool_fps_is_refused_before_any_work(env: Env, bad_fps: object) -> None:
    approval = env.approve()
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(env.payload(approval, fps=bad_fps), env.ctx)
    assert info.value.reason == "invalid_profile"
    # Refused before consumption: the approval is still usable.
    assert render_storyboard_sync(env.payload(approval), env.ctx)["verifier_status"] == "PASS"


@pytest.mark.parametrize("bad_project", ["../../etc", "proj/one", "..", "proj one"])
def test_path_traversal_in_project_id_is_refused(env: Env, bad_project: str) -> None:
    approval = env.approve()
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(env.payload(approval, project_id=bad_project), env.ctx)
    assert info.value.reason == "invalid_identifier"
    assert not (env.ws / "projects").exists() or not any((env.ws / "projects").iterdir())


# ----------------------------- TOCTOU between materialize and render -----------------------------


def test_byte_flip_between_materialize_and_render_stops_the_job(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = mz.StoryboardMediaMaterializer.materialize

    def _materialize_then_tamper(self, manifest, out_dir, **kwargs):  # type: ignore[no-untyped-def]
        bundle = original(self, manifest, out_dir, **kwargs)
        wav = Path(next(a for a in bundle.assets if a.kind == "tone_wav").resolved_path)
        raw = bytearray(wav.read_bytes())
        raw[len(raw) // 2] ^= 0x01
        wav.write_bytes(bytes(raw))
        return bundle

    monkeypatch.setattr(mz.StoryboardMediaMaterializer, "materialize", _materialize_then_tamper)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(env.payload(env.approve()), env.ctx)
    assert info.value.reason == "provenance_violated"
    assert not env.artifact().exists()
    assert not (env.artifact().parent / f"storyboard{PASSPORT_SUFFIX}").exists()


def test_asset_swapped_for_symlink_before_render_stops_the_job(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = mz.StoryboardMediaMaterializer.materialize
    decoy = tmp_path / "decoy.png"

    def _materialize_then_symlink(self, manifest, out_dir, **kwargs):  # type: ignore[no-untyped-def]
        bundle = original(self, manifest, out_dir, **kwargs)
        png = Path(next(a for a in bundle.assets if a.kind == "slate_png").resolved_path)
        decoy.write_bytes(png.read_bytes())
        png.unlink()
        os.symlink(decoy, png)
        return bundle

    monkeypatch.setattr(mz.StoryboardMediaMaterializer, "materialize", _materialize_then_symlink)
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(env.payload(env.approve()), env.ctx)
    assert info.value.reason == "provenance_violated"
    assert not env.artifact().exists()


# ----------------------------- lying FFmpeg and independent verifier -----------------------------


def test_ffmpeg_exit_zero_with_garbage_output_is_not_trusted(env: Env, tmp_path: Path) -> None:
    liar = _fake_ffmpeg(
        tmp_path,
        "out = sys.argv[-1]\nopen(out, 'wb').write(b'not really an mp4' * 20)\nsys.exit(0)",
    )
    env.ctx = RenderContext(
        workspace_root=env.ws,
        approval_ledger=env.ledger,
        signing_key=SIGNING_KEY,
        ffmpeg=liar,
        clock=lambda: 1_700_000_000.0,
    )
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(env.payload(env.approve()), env.ctx)
    assert info.value.reason == "verifier_violated"
    assert not env.artifact().exists()
    assert not (env.artifact().parent / f"storyboard{PASSPORT_SUFFIX}").exists()
    assert [p for p in env.artifact().parent.iterdir() if p.name.endswith(".tmp")] == []


def test_ffmpeg_nonzero_exit_is_a_typed_failure(env: Env, tmp_path: Path) -> None:
    failing = _fake_ffmpeg(tmp_path, "sys.stderr.write('boom')\nsys.exit(3)")
    env.ctx = RenderContext(
        workspace_root=env.ws,
        approval_ledger=env.ledger,
        signing_key=SIGNING_KEY,
        ffmpeg=failing,
        clock=lambda: 1_700_000_000.0,
    )
    with pytest.raises(StoryboardRenderError) as info:
        render_storyboard_sync(env.payload(env.approve()), env.ctx)
    assert info.value.reason == "ffmpeg_failed"
    assert not env.artifact().exists()


# ----------------------------- download gate -----------------------------


def _render_ok(env: Env) -> str:
    return render_storyboard_sync(env.payload(env.approve()), env.ctx)["output_rel_path"]


def test_post_render_tamper_blocks_download_with_409(env: Env) -> None:
    rel = _render_ok(env)
    raw = bytearray(env.artifact().read_bytes())
    raw[-12] ^= 0x55
    env.artifact().write_bytes(bytes(raw))
    with pytest.raises(DownloadRefused) as info:
        authorize_artifact_download(env.ws, rel, SIGNING_KEY)
    assert info.value.status_code == 409
    assert info.value.reason == "passport_artifact_mismatch"


def test_forged_passport_field_blocks_download_with_409(env: Env) -> None:
    rel = _render_ok(env)
    passport = env.artifact().parent / f"storyboard{PASSPORT_SUFFIX}"
    data = json.loads(passport.read_text(encoding="utf-8"))
    data["verifier_status"] = "PASS"
    data["project_id"] = "other-project"
    passport.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DownloadRefused) as info:
        authorize_artifact_download(env.ws, rel, SIGNING_KEY)
    assert info.value.status_code == 409


def test_missing_passport_blocks_download_with_409(env: Env) -> None:
    rel = _render_ok(env)
    (env.artifact().parent / f"storyboard{PASSPORT_SUFFIX}").unlink()
    with pytest.raises(DownloadRefused) as info:
        authorize_artifact_download(env.ws, rel, SIGNING_KEY)
    assert (info.value.status_code, info.value.reason) == (409, "passport_missing")


def test_download_with_wrong_signing_key_is_refused(env: Env) -> None:
    rel = _render_ok(env)
    with pytest.raises(DownloadRefused) as info:
        authorize_artifact_download(env.ws, rel, "w" * 48)
    assert info.value.status_code == 409


@pytest.mark.parametrize(
    "rel, status",
    [
        ("projects/../../etc/passwd.mp4", 403),
        ("/etc/passwd.mp4", 422),
        ("..\\..\\secret.mp4", 422),
        ("nope.txt", 422),
        ("projects/proj-1/renders/job-1/missing.mp4", 404),
    ],
)
def test_download_path_traversal_and_malformed_paths_are_refused(
    env: Env, rel: str, status: int
) -> None:
    _render_ok(env)
    with pytest.raises(DownloadRefused) as info:
        authorize_artifact_download(env.ws, rel, SIGNING_KEY)
    assert info.value.status_code == status


def test_download_through_symlink_is_refused_with_403(env: Env) -> None:
    _render_ok(env)
    os.symlink(env.artifact(), env.ws / "linked.mp4")
    with pytest.raises(DownloadRefused) as info:
        authorize_artifact_download(env.ws, "linked.mp4", SIGNING_KEY)
    assert info.value.status_code == 403
    assert env.artifact().is_file()  # the real artifact is untouched


# ----------------------------- studio init and doctor -----------------------------


def test_studio_init_creates_owner_only_layout_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "studio"
    first = studio_init(root)
    assert {p.name for p in first} == {"projects", "assets", "artifacts", "queue"}
    for name in ("projects", "assets", "artifacts", "queue"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o700
    assert studio_init(root) == []


def test_studio_init_refuses_a_symlinked_directory(tmp_path: Path) -> None:
    root = tmp_path / "studio"
    root.mkdir()
    real = tmp_path / "elsewhere"
    real.mkdir()
    os.symlink(real, root / "artifacts")
    with pytest.raises(StudioCliError, match="symlink"):
        studio_init(root)


def test_studio_doctor_is_healthy_with_real_binaries(tmp_path: Path) -> None:
    root = tmp_path / "studio"
    studio_init(root)
    report = studio_doctor(root)
    assert report.healthy is True
    assert report.exit_code == 0
    assert {c.name for c in report.checks} >= {"ffmpeg_runs", "ffprobe_runs", "queue_sqlite"}


def test_studio_doctor_fails_closed_when_ffmpeg_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "studio"
    studio_init(root)
    report = studio_doctor(root, ffmpeg="/nonexistent/ffmpeg")
    assert report.healthy is False
    assert report.exit_code == 1
    assert next(c for c in report.checks if c.name == "ffmpeg_runs").ok is False


def test_studio_doctor_reports_missing_layout_without_crashing(tmp_path: Path) -> None:
    report = studio_doctor(tmp_path / "never-initialized")
    assert report.healthy is False
    assert next(c for c in report.checks if c.name == "workspace_layout").ok is False
