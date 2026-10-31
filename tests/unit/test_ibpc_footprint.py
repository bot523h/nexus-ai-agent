"""Unit tests: the independent footprint verifier on REAL decoded media (M2).

Calibration (docs/engineering/NAGAR_LIVE_EDITING_LIVE_AUDIT.md §4): an
unchanged-content re-encode gives per-frame mean|Δ| ≤ 0.47, a real brightness
change ≈ 31, so NOISE_MEAN_MAX=4.0 separates them with ~8× margin. PASS
requires bit-exact preservation; a lossy-but-honest encode must land in
INCONCLUSIVE, never PASS; a provable out-of-scope change must land in FAIL.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.nagar.ibpc.footprint import (
    NOISE_MEAN_MAX,
    verify_keep,
    verify_range_modify,
)
from nexus_ai_agent.nagar.ibpc.models import CheckVerdict, VideoTimeScope


def _binary() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:
        pytest.skip(f"FFmpeg unavailable: {exc}")


def _run(binary: str, args: list[str]) -> None:
    proc = subprocess.run(
        [binary, "-hide_banner", "-nostdin", "-loglevel", "error", *args],
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-400:])


def _source(path: Path, binary: str, seconds: float = 2.0) -> None:
    _run(
        binary,
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
    )


def _keep_frames(binary: str, src: Path, out: Path, frames: list[tuple[int, int]]) -> None:
    """Produce out = concatenation of the kept frame ranges of src (trim
    semantics: the removed scope's frames are gone, the rest kept).
    Frame-exact + ``fps=15`` so the output timebase matches the source.
    Lossless (-qp 0) so preservation is checkable bit-exactly."""
    filters = []
    labels = []
    for i, (a, b) in enumerate(frames):
        filters.append(f"[0:v]trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS[v{i}]")
        labels.append(f"[v{i}]")
    fc = ";".join(filters) + ";" + "".join(labels) + f"concat=n={len(frames)}:v=1:a=0,fps=15[outv]"
    _run(
        binary,
        [
            "-i",
            str(src),
            "-filter_complex",
            fc,
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-qp",
            "0",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
    )


def _grade(binary: str, src: Path, out: Path, brightness: float = 0.2) -> None:
    _run(
        binary,
        [
            "-i",
            str(src),
            "-vf",
            f"eq=brightness={brightness}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
    )


@pytest.fixture()
def media(tmp_path: Path) -> tuple[str, Path]:
    binary = _binary()
    src = tmp_path / "src.mp4"
    _source(src, binary)
    return binary, src


SCOPE = VideoTimeScope(asset_id="src", effect="keep", start_us=500_000, end_us=1_500_000)


class TestVerifyKeep:
    def test_real_keep_window_is_proven(self, media, tmp_path: Path) -> None:
        binary, src = media
        artifact = tmp_path / "kept.mp4"
        _keep_frames(binary, src, artifact, [(8, 23)])  # keep frames 8..22 = [0.5s,1.5s)
        report = verify_keep(src, artifact, scope=SCOPE, binary=binary)
        by_name = {c.name: c for c in report.checks}
        intended = by_name["intended_change"]
        assert intended.verdict is CheckVerdict.PASS
        ev = intended.evidence
        assert ev["strictly_increasing_mapping"] is True
        assert ev["keep_window_frames"] == [8, 23]
        assert ev["mapped_range"] == [8, 22]
        # every removed source frame lies outside the keep window
        assert all(j < 8 or j >= 23 for j in ev["unmatched_source_frames"])
        assert (
            ev["artifact_frames"] == 15 == ev["source_frames"] - len(ev["unmatched_source_frames"])
        )
        preservation = by_name["preservation_of_keep_window"]
        # lossless pipeline: bit-exact PASS is achievable and must be reported;
        # anything lossy must be INCONCLUSIVE — never silently PASS
        assert preservation.verdict in (CheckVerdict.PASS, CheckVerdict.INCONCLUSIVE)
        if preservation.verdict is CheckVerdict.PASS:
            assert preservation.evidence["max_pair_abs_diff"] == 0
        assert report.verdict is not CheckVerdict.FAIL
        assert report.source_frames == 30
        assert report.artifact_digest.startswith("sha256:")

    def test_unchanged_copy_fails_intended_change(self, media, tmp_path: Path) -> None:
        binary, src = media
        artifact = tmp_path / "copy.mp4"
        artifact.write_bytes(src.read_bytes())
        report = verify_keep(src, artifact, scope=SCOPE, binary=binary)
        by_name = {c.name: c for c in report.checks}
        # nothing was removed -> intended change did NOT happen
        assert by_name["intended_change"].verdict is CheckVerdict.FAIL
        assert by_name["preservation_of_keep_window"].verdict is CheckVerdict.PASS
        assert report.verdict is CheckVerdict.FAIL

    def test_wrong_window_kept_fails(self, media, tmp_path: Path) -> None:
        binary, src = media
        artifact = tmp_path / "wrong.mp4"
        _keep_frames(binary, src, artifact, [(15, 30)])  # kept [1s,2s) instead of [0.5s,1.5s)
        report = verify_keep(src, artifact, scope=SCOPE, binary=binary)
        by_name = {c.name: c for c in report.checks}
        assert by_name["intended_change"].verdict is CheckVerdict.FAIL

    def test_graded_keep_window_fails_preservation(self, media, tmp_path: Path) -> None:
        binary, src = media
        artifact = tmp_path / "kept_graded_src.mp4"
        _keep_frames(binary, src, artifact, [(8, 23)])
        graded = tmp_path / "graded.mp4"
        _grade(binary, artifact, graded)
        report = verify_keep(src, graded, scope=SCOPE, binary=binary)
        by_name = {c.name: c for c in report.checks}
        assert by_name["preservation_of_keep_window"].verdict is CheckVerdict.FAIL
        assert report.verdict is CheckVerdict.FAIL
        # and the margin is far above the noise floor
        assert (
            by_name["preservation_of_keep_window"].evidence["max_pair_mean_diff"]
            > 4 * NOISE_MEAN_MAX
        )

    def test_resolution_mismatch_is_inconclusive(self, media, tmp_path: Path) -> None:
        binary, src = media
        other = tmp_path / "small.mp4"
        _run(
            binary,
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=160x120:rate=15",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(other),
            ],
        )
        report = verify_keep(src, other, scope=SCOPE, binary=binary)
        assert report.verdict is CheckVerdict.INCONCLUSIVE

    def test_missing_file_is_inconclusive_not_crash(self, media, tmp_path: Path) -> None:
        binary, src = media
        report = verify_keep(src, tmp_path / "nope.mp4", scope=SCOPE, binary=binary)
        assert report.verdict is CheckVerdict.INCONCLUSIVE


class TestVerifyRangeModify:
    def test_modify_inside_scope_proven(self, media, tmp_path: Path) -> None:
        binary, src = media
        # grade only the middle second: full-length artifact, scope = modify
        fc = (
            "[0:v]trim=start_frame=0:end_frame=8,setpts=PTS-STARTPTS[a];"
            "[0:v]trim=start_frame=8:end_frame=23,setpts=PTS-STARTPTS[b];"
            "[b]eq=brightness=0.2[bg];"
            "[0:v]trim=start_frame=23:end_frame=30,setpts=PTS-STARTPTS[c];"
            "[a][bg][c]concat=n=3:v=1:a=0,fps=15[outv]"
        )
        artifact = tmp_path / "modified.mp4"
        _run(
            binary,
            [
                "-i",
                str(src),
                "-filter_complex",
                fc,
                "-map",
                "[outv]",
                "-c:v",
                "libx264",
                "-qp",
                "0",
                "-pix_fmt",
                "yuv420p",
                str(artifact),
            ],
        )
        scope = VideoTimeScope(asset_id="src", effect="modify", start_us=500_000, end_us=1_500_000)
        report = verify_range_modify(src, artifact, scope=scope, binary=binary)
        by_name = {c.name: c for c in report.checks}
        assert by_name["intended_change"].verdict is CheckVerdict.PASS
        assert by_name["preservation_outside_scope"].verdict is CheckVerdict.PASS
        assert report.verdict is CheckVerdict.PASS

    def test_modify_scope_with_no_change_fails_intended(self, media, tmp_path: Path) -> None:
        binary, src = media
        artifact = tmp_path / "copy.mp4"
        artifact.write_bytes(src.read_bytes())
        scope = VideoTimeScope(asset_id="src", effect="modify", start_us=500_000, end_us=1_500_000)
        report = verify_range_modify(src, artifact, scope=scope, binary=binary)
        by_name = {c.name: c for c in report.checks}
        assert by_name["intended_change"].verdict is CheckVerdict.FAIL
