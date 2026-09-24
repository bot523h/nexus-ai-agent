"""Artifact truth: spec digests are never file hashes (session 3, NAG-003).

The audit found ``content_sha256`` fields holding ``sha256(<render-spec
text>)`` — a derivation digest — where readers expect a hash of media bytes.
This file pins the three-identity split from ``creative/artifacts.py``:

* ``render_spec_hash`` — the recipe (canonical JSON of the CompiledLane);
* ``physical_artifact_sha256`` — measured from the encoded bytes;
* ``logical_content_identity`` — what the media is (source bytes / derivation).

Machine-enforced invariant: the spec digest and the physical digest are
computed by different functions over different inputs, and
:func:`verify_lane_artifact` re-measures the bytes from disk — so a lane
that reported its spec hash *as* its file hash (mutation M2) fails the
cross-check.  The real-encode test below runs FFmpeg for true.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from nexus_ai_agent.creative.artifacts import (
    ArtifactVerificationError,
    probe_media,
    render_spec_hash_of,
    sha256_bytes,
    sha256_file_bytes,
    verify_artifact,
    verify_lane_artifact,
)
from nexus_ai_agent.creative.rendering import (
    LaneIR,
    LaneSource,
    compile_lane,
    encode_lane,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:
        pytest.skip(f"no ffmpeg for the real-encode proof: {exc}")


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


# ---------------------------------------------------------------------------
# digest helpers: different inputs, different functions
# ---------------------------------------------------------------------------


def test_sha256_helpers_measure_what_they_claim(tmp_path: Path) -> None:
    payload = b"artifact bytes, not a spec"
    assert sha256_bytes(payload) == "sha256:" + hashlib.sha256(payload).hexdigest()
    target = tmp_path / "a.bin"
    target.write_bytes(payload)
    assert sha256_file_bytes(target) == sha256_bytes(payload)


def test_render_spec_hash_is_canonical_json_not_bytes(tmp_path: Path) -> None:
    spec = {"ops": ["trim"], "duration_us": 1_000_000}
    first = render_spec_hash_of(spec)
    assert first == render_spec_hash_of({"duration_us": 1_000_000, "ops": ["trim"]})
    # The same dict written to disk hashes differently — the spec digest is a
    # recipe identity and must never be mistaken for a file hash.
    target = tmp_path / "spec.json"
    target.write_text('{"ops": ["trim"], "duration_us": 1000000}')
    assert sha256_file_bytes(target) != first


# ---------------------------------------------------------------------------
# verify_artifact: the fail-closed ladder
# ---------------------------------------------------------------------------


def test_verify_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ArtifactVerificationError, match="missing"):
        verify_artifact(tmp_path / "ghost.mp4", logical_content_identity="x")


def test_verify_refuses_an_empty_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(ArtifactVerificationError, match="empty"):
        verify_artifact(empty, logical_content_identity="x")


def test_verify_refuses_a_hash_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"bytes")
    with pytest.raises(ArtifactVerificationError, match="mismatch"):
        verify_artifact(
            target,
            logical_content_identity="x",
            expected_sha256="sha256:" + "0" * 64,
        )


def test_verify_refuses_an_unprobable_file(tmp_path: Path) -> None:
    _ffmpeg()
    target = tmp_path / "junk.mp4"
    target.write_bytes(b"this is not a media container" * 100)
    with pytest.raises(ArtifactVerificationError, match="no prover could read"):
        verify_artifact(target, logical_content_identity="x")


# ---------------------------------------------------------------------------
# real encode: the lane's claims re-measured from disk
# ---------------------------------------------------------------------------


def test_real_encode_verifies_against_independent_remeasurement(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 2.0, binary)

    main = LaneSource("main", str(src), "video", 2_000_000)
    compiled = compile_lane(LaneIR(main=main, ops=()))
    destination = tmp_path / "out.mp4"
    lane_artifact = encode_lane(compiled, destination, binary=binary)

    # The lane's own claims are sane (size, hash shape, probe).
    assert lane_artifact.size_bytes > 0
    assert lane_artifact.sha256.startswith("sha256:")
    assert abs(lane_artifact.duration_us - 2_000_000) < 300_000

    physical = verify_lane_artifact(
        lane_artifact, logical_content_identity=f"source:{sha256_file_bytes(src)}"
    )
    assert physical.size_bytes == lane_artifact.size_bytes
    assert physical.physical_sha256 == lane_artifact.sha256
    assert physical.render_spec_hash == compiled.ir_hash
    # ... and the spec digest provably differs from the file digest (NAG-003).
    assert physical.render_spec_hash != physical.physical_sha256
    assert physical.probe.has_video and physical.probe.has_audio
    assert physical.probe.prover in ("ffprobe", "ffmpeg-stderr")
    evidence = physical.evidence()
    assert evidence["artifact_size"] > 0
    assert evidence["probe"]["duration_us"] > 0


def test_verify_lane_artifact_catches_a_lying_lane(tmp_path: Path) -> None:
    """M2: a lane reporting its spec digest as its file hash fails the cross-check."""
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 1.0, binary)
    main = LaneSource("main", str(src), "video", 1_000_000)
    compiled = compile_lane(LaneIR(main=main, ops=()))
    destination = tmp_path / "out.mp4"
    lane_artifact = encode_lane(compiled, destination, binary=binary)

    from dataclasses import replace

    lying = replace(lane_artifact, sha256=compiled.ir_hash)  # spec digest as file hash
    with pytest.raises(ArtifactVerificationError, match="mismatch"):
        verify_lane_artifact(lying, logical_content_identity="x")


def test_probe_names_its_prover(tmp_path: Path) -> None:
    binary = _ffmpeg()
    src = tmp_path / "src.mp4"
    _make_clip(src, 1.0, binary)
    fact = probe_media(src, ffmpeg_bin=binary)
    assert fact.prover in ("ffprobe", "ffmpeg-stderr")
    assert fact.has_video and fact.has_audio
    assert (fact.width, fact.height) == (320, 240)
