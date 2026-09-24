"""Apply-lane lifecycle gates: REGISTERED → RUNNABLE → executor, fail-closed."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai_agent.creative.rendering.executor import (
    LaneExecutionError,
    activate_runtime,
    encode_lane,
    probe_filter_names,
)
from nexus_ai_agent.creative.rendering.ir import LaneIR, LaneSource
from nexus_ai_agent.creative.rendering.lifecycle import (
    REQUIRED_FILTERS,
    LaneLifecycleError,
    LaneLifecycleState,
    apply_probe,
    fail_unregistered,
    mark_failed,
    register_binary,
    shipped_identity_lut,
    shipped_persian_font,
    unregistered,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, resolve_ffmpeg_bin


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError:
        pytest.skip("no FFmpeg binary available")


def test_unregistered_cannot_skip_to_runnable() -> None:
    with pytest.raises(LaneLifecycleError, match="illegal lifecycle transition"):
        apply_probe(unregistered(), frozenset(REQUIRED_FILTERS))


def test_register_missing_file_is_failed_not_registered() -> None:
    runtime = register_binary("/definitely/not/an/ffmpeg")
    assert runtime.state is LaneLifecycleState.FAILED
    with pytest.raises(LaneLifecycleError, match="encode requires RUNNABLE"):
        runtime.require_runnable()


def test_registered_without_probe_cannot_encode(tmp_path: Path) -> None:
    binary = _ffmpeg()
    registered = register_binary(binary)
    assert registered.state is LaneLifecycleState.REGISTERED
    from nexus_ai_agent.creative.rendering.compiler import compile_lane

    compiled = compile_lane(
        LaneIR(main=LaneSource("m", str(tmp_path / "x.mp4"), "video", 1_000_000), ops=())
    )
    with pytest.raises(LaneLifecycleError, match="RUNNABLE"):
        encode_lane(compiled, tmp_path / "out.mp4", runtime=registered)


def test_failed_is_terminal() -> None:
    registered = register_binary(_ffmpeg())
    failed = mark_failed(registered, "probe exploded")
    assert failed.state is LaneLifecycleState.FAILED
    with pytest.raises(LaneLifecycleError, match="illegal lifecycle transition"):
        apply_probe(failed, frozenset(REQUIRED_FILTERS))


def test_unresolved_binary_is_failed() -> None:
    runtime = fail_unregistered("no ffmpeg")
    assert runtime.state is LaneLifecycleState.FAILED
    with pytest.raises(LaneLifecycleError, match="RUNNABLE"):
        runtime.require_runnable()


def test_missing_required_filter_fails_probe() -> None:
    registered = register_binary(_ffmpeg())
    failed = apply_probe(registered, frozenset({"eq", "format"}))
    assert failed.state is LaneLifecycleState.FAILED
    assert failed.failure and "missing required filters" in failed.failure


def test_activate_runtime_is_runnable_with_real_filters() -> None:
    runtime = activate_runtime(_ffmpeg())
    assert runtime.state is LaneLifecycleState.RUNNABLE
    assert REQUIRED_FILTERS <= runtime.filters
    runtime.require_runnable()


def test_probe_filter_names_observes_real_core_filters() -> None:
    names = probe_filter_names(_ffmpeg())
    assert "lut3d" in names
    assert "loudnorm" in names
    assert "format" in names


def test_shipped_lut_and_persian_font_exist() -> None:
    lut = shipped_identity_lut()
    font = shipped_persian_font()
    assert lut.suffix == ".cube"
    text = lut.read_text(encoding="utf-8")
    assert "LUT_3D_SIZE 2" in text
    assert font.suffix == ".ttf"
    assert font.stat().st_size > 1000


def test_encode_rejects_run_before_gate_via_failed_runtime(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.rendering.compiler import compile_lane

    compiled = compile_lane(
        LaneIR(main=LaneSource("m", str(tmp_path / "x.mp4"), "video", 1_000_000), ops=())
    )
    with pytest.raises((LaneLifecycleError, LaneExecutionError)):
        encode_lane(
            compiled,
            tmp_path / "out.mp4",
            runtime=fail_unregistered("blocked"),
        )
