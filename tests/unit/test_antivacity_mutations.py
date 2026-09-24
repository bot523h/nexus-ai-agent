"""Anti-vacuity mutations M1–M7: each test breaks one thing the suite must catch.

A green suite can still be vacuous — passing because nothing is checked.  Each
test below applies one mutation an optimist (or a liar) might introduce and
asserts the suite goes red: the gate, hash, or refusal that catches it is the
point, not the mutation itself.

* M1 — the artifact file vanishes after the encode → verification fails
  (gates read bytes, not structs);
* M2 — the lane reports its spec digest as its file hash → cross-check fails;
* M3 — an unmapped effect is silently dropped → the plan hash moves and the
  unmapped list names it;
* M4 — ``fontsdir`` is accepted but not threaded → the graph lacks it;
* M5 — the staged LUT is truncated garbage → resolve-time refusal;
* M6 — a twin payload changes (LUT path/intensity) → ``ir_hash`` moves;
* M7 — the artifact duration disagrees with the plan → verification fails.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.artifacts import (
    ArtifactVerificationError,
    sha256_file_bytes,
    verify_artifact,
    verify_lane_artifact,
)
from nexus_ai_agent.creative.luts import (
    LutValidationError,
    library_path,
    validate_cube_file,
)
from nexus_ai_agent.creative.rendering import (
    LaneIR,
    LaneSource,
    LutOp,
    SubtitleOp,
    compile_lane,
    encode_lane,
)
from nexus_ai_agent.creative.rendering.plan import compile_execution_plan
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.creative.studio.models import (
    Clip,
    EffectLayerRef,
    MediaRef,
    Timeline,
    TimeRangeUS,
    Track,
    new_project,
)


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:
        pytest.skip(f"no ffmpeg for mutation {exc}")


def _make_clip(path: Path, duration_s: float, binary: str) -> None:
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=30:duration={duration_s}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_s}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]


def _plan_with(*operations: str, **kwargs: object) -> object:
    media = MediaRef(
        asset_id="a", content_sha256="sha256:a", media_kind="video", duration_us=8_000_000
    )
    effects = [
        EffectLayerRef(
            operation=name,
            parameters={} if name != "color.apply_lut" else {"lut_name": "warm"},
            range=TimeRangeUS(start_us=0, end_us=4_000_000),
        )
        for name in operations
    ]
    clip = Clip(
        clip_id="c1",
        media_ref=media,
        source_range=TimeRangeUS(start_us=0, end_us=4_000_000),
        timeline_range=TimeRangeUS(start_us=0, end_us=4_000_000),
        effects=effects,
    )
    track = Track(track_id="v", name="V", kind="video", clips=[clip])
    project = new_project(
        "p", "M", Timeline(timeline_id="tl", duration_us=4_000_000, tracks=[track])
    )
    return compile_execution_plan(project, track_id="v", **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# M1: the file vanishes — the gate reads bytes, not the LaneArtifact struct
# ---------------------------------------------------------------------------


def test_m1_deleted_artifact_fails_verification(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 1.0, binary)
    main = LaneSource("main", str(src), "video", 1_000_000)
    destination = tmp_path / "out.mp4"
    lane_artifact = encode_lane(compile_lane(LaneIR(main=main, ops=())), destination, binary=binary)
    assert destination.is_file()
    destination.unlink()  # mutation: the bytes are gone, the struct survives
    with pytest.raises(ArtifactVerificationError, match="missing"):
        verify_lane_artifact(lane_artifact, logical_content_identity="x")


# ---------------------------------------------------------------------------
# M2: spec digest reported as file hash — the cross-check catches it
# ---------------------------------------------------------------------------


def test_m2_spec_digest_as_file_hash_fails(tmp_path: Path) -> None:
    from dataclasses import replace

    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 1.0, binary)
    main = LaneSource("main", str(src), "video", 1_000_000)
    compiled = compile_lane(LaneIR(main=main, ops=()))
    lane_artifact = encode_lane(compiled, tmp_path / "out.mp4", binary=binary)
    assert lane_artifact.sha256 != compiled.ir_hash  # sanity: real hashes differ
    lying = replace(lane_artifact, sha256=compiled.ir_hash)  # mutation
    with pytest.raises(ArtifactVerificationError, match="mismatch"):
        verify_lane_artifact(lying, logical_content_identity="x")


# ---------------------------------------------------------------------------
# M3: silently dropping an unmapped effect moves the plan hash
# ---------------------------------------------------------------------------


def test_m3_dropped_unmapped_effect_is_visible_in_hash_and_list() -> None:
    staged = {"warm": str(library_path("warm"))}
    honest = _plan_with("color.apply_lut", "color.auto_balance", lut_paths=staged)
    assert honest.unmapped_effects == ("c1:color.auto_balance",)
    without = _plan_with("color.apply_lut", lut_paths=staged)
    assert without.unmapped_effects == ()
    # A plan that silently dropped auto_balance would hash like `without` —
    # the honest plan provably does not.
    assert honest.plan_hash != without.plan_hash


# ---------------------------------------------------------------------------
# M4: fontsdir must reach the graph, not just the signature
# ---------------------------------------------------------------------------


def test_m4_fontsdir_reaches_the_filtergraph() -> None:
    main = LaneSource("main", "/media/m.mp4", "video", 10_000_000)
    op = (SubtitleOp(subtitle_path="/stage/c.srt"),)
    threaded = compile_lane(LaneIR(main=main, ops=op), fontsdir="/assets/fonts")
    assert ":fontsdir='/assets/fonts'" in threaded.filtergraph
    dropped = compile_lane(LaneIR(main=main, ops=op))
    assert "fontsdir" not in dropped.filtergraph
    assert threaded.ir_hash != dropped.ir_hash


# ---------------------------------------------------------------------------
# M5: a truncated LUT is refused at resolve time (not at encode time)
# ---------------------------------------------------------------------------


def test_m5_truncated_lut_is_refused_with_the_defect_named(tmp_path: Path) -> None:
    lines = library_path("warm").read_text().splitlines(keepends=True)
    truncated = tmp_path / "truncated.cube"
    truncated.write_text("".join(lines[:100]))  # mutation: 100 lines, needs 4100+
    with pytest.raises(LutValidationError, match="truncated.cube"):
        validate_cube_file(truncated)


# ---------------------------------------------------------------------------
# M6: twin payload changes move ir_hash (hash blindness fails here)
# ---------------------------------------------------------------------------


def test_m6_lut_payload_changes_move_the_hash() -> None:
    main = LaneSource("main", "/media/m.mp4", "video", 10_000_000)

    def _hash(lut_path: str, intensity: float) -> str:
        return compile_lane(
            LaneIR(main=main, ops=(LutOp(lut_name="w", lut_path=lut_path, intensity=intensity),))
        ).ir_hash

    assert _hash("/a.cube", 1.0) != _hash("/b.cube", 1.0)  # path swap
    assert _hash("/a.cube", 1.0) != _hash("/a.cube", 0.5)  # intensity tweak


# ---------------------------------------------------------------------------
# M7: duration disagreement between artifact and plan fails verification
# ---------------------------------------------------------------------------


def test_m7_wrong_duration_artifact_is_refused(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 1.0, binary)
    digest = sha256_file_bytes(src)
    # The file is really ~1s; claiming the plan said 60s must fail.
    with pytest.raises(ArtifactVerificationError, match="outside"):
        verify_artifact(
            src,
            logical_content_identity="x",
            expected_sha256=digest,
            expected_duration_us=60_000_000,
            duration_tolerance_us=300_000,
            ffmpeg_bin=binary,
        )
