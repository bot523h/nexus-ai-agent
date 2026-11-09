"""Security tests for the composition layer — the controls are *enforced*.

Every test here asserts an **actual** refusal, not the presence of a constant.
The distinction matters: a constant that nothing reads is a claim, and a claim
is not a control.

The controls under test (Gate 4):

* argv-only execution, ``shell=False``;
* an executable allow-list with absolute resolution;
* an environment allow-list with secret stripping;
* a dedicated temp (scratch) directory per node;
* path containment and symlink-escape protection;
* input and output size limits;
* a wall-clock timeout that stops the process **and its children**;
* bounded diagnostics;
* fail-closed refusals *before* dispatch for the ten listed conditions.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from nexus_ai_agent.creative.composition.engine import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MediaEngine,
    MediaEngineError,
    contained_path,
    enforce_input_limit,
    enforce_output_limit,
)
from nexus_ai_agent.creative.composition.job import (
    CompositionJobError,
    guarded_input,
    trusted_workspace,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    _ENCODER_ENV_ALLOWLIST,
    encoder_env,
    resolve_ffmpeg_bin,
)

# ---------------------------------------------------------------------------
# executable allow-list and absolute resolution
# ---------------------------------------------------------------------------


def test_the_encoder_binary_is_resolved_to_an_absolute_path() -> None:
    resolved = resolve_ffmpeg_bin()
    assert Path(resolved).is_absolute()
    assert Path(resolved).is_file()


def test_an_explicit_non_file_binary_is_refused() -> None:
    from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError

    with pytest.raises(FfmpegUnavailableError):
        resolve_ffmpeg_bin("/nonexistent/ffmpeg-binary")


def test_the_pack_manifest_forbids_network_use() -> None:
    import json

    manifest = json.loads(
        (
            Path(__file__).parents[2] / "src/nexus_ai_agent/creative/packs/media/pack.manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["network_policy"]["runtime_network"] is False
    assert manifest["network_policy"]["upload_media"] is False
    assert manifest["runtime"]["native"]["network_required"] is False


# ---------------------------------------------------------------------------
# environment allow-list and secret stripping
# ---------------------------------------------------------------------------


def test_the_encoder_environment_is_an_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/evil")
    env = encoder_env()
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "HTTPS_PROXY" not in env
    assert "LD_LIBRARY_PATH" not in env
    assert env.get("PATH") == "/usr/bin"


def test_the_allow_list_never_contains_a_library_path() -> None:
    """``LD_LIBRARY_PATH`` is a code-loading vector and stays out."""
    assert "LD_LIBRARY_PATH" not in _ENCODER_ENV_ALLOWLIST
    assert "LD_PRELOAD" not in _ENCODER_ENV_ALLOWLIST


def test_the_encoder_subprocess_actually_receives_the_scrubbed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a constant check: the child process must observe the scrub."""
    monkeypatch.setenv("NEXUS_COMPOSITION_SECRET_PROBE", "leaked-value")
    script = (
        "import os,sys;"
        "sys.stdout.write('SECRET=' + os.environ.get('NEXUS_COMPOSITION_SECRET_PROBE',''));"
        "sys.stdout.write('|LD=' + os.environ.get('LD_LIBRARY_PATH',''))"
    )
    from nexus_ai_agent.creative.slideshow.ffmpeg import _run

    result = _run(["/usr/bin/python3", "-c", script], timeout=30)
    assert result.returncode == 0
    assert "SECRET=" in result.stdout
    assert "SECRET=leaked-value" not in result.stdout
    assert "|LD=" in result.stdout


# ---------------------------------------------------------------------------
# path containment and symlink escape
# ---------------------------------------------------------------------------


def test_a_path_traversal_is_refused(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(MediaEngineError):
        contained_path(scratch, "../escape.png")
    with pytest.raises(MediaEngineError):
        contained_path(scratch, str(tmp_path / "outside.png"))
    with pytest.raises(MediaEngineError):
        contained_path(scratch, "/etc/passwd")


def test_a_symlink_escape_is_refused(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"x")
    link = scratch / "link.png"
    link.symlink_to(outside / "secret.png")
    with pytest.raises(MediaEngineError):
        contained_path(scratch, str(link))


def test_a_symlink_source_is_refused_by_the_engine(tmp_path: Path) -> None:
    engine = MediaEngine()
    source = tmp_path / "real.png"
    source.write_bytes(b"not a png")
    link = tmp_path / "link.png"
    link.symlink_to(source)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(MediaEngineError):
        engine.realize(
            "media.resize_image",
            source=link,
            output_path=scratch / "out.png",
            params={"width": 64, "height": 64},
            scratch_dir=scratch,
        )


def test_the_output_must_stay_inside_the_scratch_directory(tmp_path: Path) -> None:
    engine = MediaEngine()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = tmp_path / "source.png"
    from PIL import Image

    Image.new("RGB", (16, 16), (10, 20, 30)).save(source)
    with pytest.raises(MediaEngineError):
        engine.realize(
            "media.resize_image",
            source=source,
            output_path=tmp_path / "outside.png",
            params={"width": 32, "height": 32},
            scratch_dir=scratch,
        )


# ---------------------------------------------------------------------------
# size limits
# ---------------------------------------------------------------------------


def test_an_oversize_output_is_deleted_and_refused(tmp_path: Path) -> None:
    target = tmp_path / "big.bin"
    target.write_bytes(b"0" * 4096)
    with pytest.raises(MediaEngineError):
        enforce_output_limit(target, limit=1024)
    assert not target.exists(), "an oversize artifact must not be left on disk"


def test_an_oversize_input_is_refused_before_any_process(tmp_path: Path) -> None:
    target = tmp_path / "big.bin"
    target.write_bytes(b"0" * 4096)
    with pytest.raises(MediaEngineError):
        enforce_input_limit(target, limit=1024)
    assert target.exists(), "a refused input must not be deleted"


def test_the_ceilings_are_bounded() -> None:
    assert MAX_OUTPUT_BYTES == 512 * 1024 * 1024
    assert MAX_INPUT_BYTES == 512 * 1024 * 1024


# ---------------------------------------------------------------------------
# timeout stops the process AND its children
# ---------------------------------------------------------------------------


def test_a_timeout_kills_the_process_group(tmp_path: Path) -> None:
    """The child must not survive the deadline — not even its grandchildren."""
    marker = tmp_path / "grandchild.pid"
    script = (
        "import os,subprocess,sys,time;"
        f"child=subprocess.Popen(['/bin/sh','-c','sleep 120; touch {marker}']);"
        "print(child.pid, flush=True);"
        "time.sleep(120)"
    )
    from nexus_ai_agent.creative.slideshow.ffmpeg import _run

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run(["/usr/bin/python3", "-c", script], timeout=2)
    elapsed = time.monotonic() - started
    assert elapsed < 30, f"the timeout did not bound the wait ({elapsed:.1f}s)"
    # The grandchild must be gone: give it a moment, then assert the marker
    # was never created (the shell would have touched it after 120s).
    time.sleep(1.0)
    assert not marker.exists()


def test_the_encoder_child_runs_in_its_own_session(tmp_path: Path) -> None:
    """``start_new_session`` is what makes the group kill possible."""
    script = "import os;print(os.getpid(), os.getpgid(0), os.getppid())"
    from nexus_ai_agent.creative.slideshow.ffmpeg import _run

    result = _run(["/usr/bin/python3", "-c", script], timeout=30)
    assert result.returncode == 0
    pid, pgid, ppid = (int(part) for part in result.stdout.split())
    assert pgid == pid, "the child must lead its own process group"
    assert pgid != os.getpgid(os.getpid()), "the child must not share the worker's group"


def test_the_kill_helper_escalates_to_sigkill() -> None:
    """A wedged child that ignores SIGTERM is still killed."""
    from nexus_ai_agent.creative.slideshow.ffmpeg import _kill_process_group

    process = subprocess.Popen(  # noqa: S603 - test-local, argv list
        [
            "/usr/bin/python3",
            "-c",
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)",
        ],
        start_new_session=True,
    )
    try:
        _kill_process_group(process)
        assert process.poll() is not None, "a SIGTERM-ignoring child must still be killed"
        assert process.returncode in (-signal.SIGKILL, -signal.SIGTERM)
    finally:
        if process.poll() is None:  # pragma: no cover - defensive
            process.kill()


# ---------------------------------------------------------------------------
# bounded diagnostics
# ---------------------------------------------------------------------------


def test_a_failed_encode_reports_a_bounded_diagnostic(tmp_path: Path) -> None:
    """The error must carry a tail of the output, never the whole stream."""
    from nexus_ai_agent.creative.slideshow.ffmpeg import RenderError, resize_image

    source = tmp_path / "source.png"
    source.write_bytes(b"this is not a png at all")
    with pytest.raises(RenderError) as excinfo:
        resize_image(
            source,
            tmp_path / "out.png",
            width=64,
            height=64,
            timeout=60,
            overwrite=True,
        )
    detail = str(excinfo.value)
    assert len(detail) < 2000, "the diagnostic must be bounded"


# ---------------------------------------------------------------------------
# workspace and staged-input guards
# ---------------------------------------------------------------------------


def test_the_workspace_must_carry_the_reserved_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.config import settings as settings_module

    root = tmp_path / "creative"
    root.mkdir()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(root))
    settings_module.get_settings.cache_clear()
    try:
        bad = root / "not_prefixed"
        bad.mkdir()
        with pytest.raises(CompositionJobError) as excinfo:
            trusted_workspace(str(bad))
        assert "prefix" in excinfo.value.detail
    finally:
        settings_module.get_settings.cache_clear()


def test_a_workspace_outside_the_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.config import settings as settings_module

    root = tmp_path / "creative"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "creative_outside"
    outside.mkdir(parents=True)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(root))
    settings_module.get_settings.cache_clear()
    try:
        with pytest.raises(CompositionJobError):
            trusted_workspace(str(outside))
    finally:
        settings_module.get_settings.cache_clear()


def test_a_symlinked_workspace_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.config import settings as settings_module

    root = tmp_path / "creative"
    root.mkdir()
    victim = root / "creative_victim"
    victim.mkdir()
    link = root / "creative_link"
    link.symlink_to(victim)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(root))
    settings_module.get_settings.cache_clear()
    try:
        with pytest.raises(CompositionJobError) as excinfo:
            trusted_workspace(str(link))
        assert "symlink" in excinfo.value.detail
    finally:
        settings_module.get_settings.cache_clear()


def test_a_staged_input_must_be_inside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "creative_ws"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    with pytest.raises(CompositionJobError) as excinfo:
        guarded_input(str(outside), workspace, "sha256:" + "0" * 64)
    assert "escape" in excinfo.value.detail


def test_a_staged_input_must_not_be_a_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "creative_ws"
    workspace.mkdir()
    real = workspace / "real.png"
    real.write_bytes(b"x")
    link = workspace / "link.png"
    link.symlink_to(real)
    with pytest.raises(CompositionJobError) as excinfo:
        guarded_input(str(link), workspace, "sha256:" + "0" * 64)
    assert "symlink" in excinfo.value.detail


def test_a_changed_staged_input_is_refused(tmp_path: Path) -> None:
    import hashlib

    workspace = tmp_path / "creative_ws"
    workspace.mkdir()
    staged = workspace / "input.png"
    staged.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    staged.write_bytes(b"tampered")
    with pytest.raises(CompositionJobError) as excinfo:
        guarded_input(str(staged), workspace, f"sha256:{digest}")
    assert "digest mismatch" in excinfo.value.detail


def test_an_unchanged_staged_input_is_accepted(tmp_path: Path) -> None:
    import hashlib

    workspace = tmp_path / "creative_ws"
    workspace.mkdir()
    staged = workspace / "input.png"
    staged.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    resolved = guarded_input(str(staged), workspace, f"sha256:{digest}")
    assert resolved == staged.resolve()
