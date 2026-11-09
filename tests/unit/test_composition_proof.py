"""Unit tests for the proof-carrying artifact and the independent verification.

The mission's hard distinction is enforced here: **re-hashing is not semantic
verification**.  A file whose bytes hash correctly but whose content is wrong
must fail the semantic pass, and the semantic pass must not read the
producer's summary.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from nexus_ai_agent.creative.composition.proof import (
    IntegrityReport,
    NodeReceipt,
    ProofCarryingArtifact,
    SemanticReport,
    decode_video_frames,
    raw_stream_table,
    verify_image_semantics,
    verify_integrity,
    verify_media_semantics,
    verify_video_semantics,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    FfmpegUnavailableError,
    resolve_ffmpeg_bin,
)


def _ffmpeg() -> str:
    try:
        return resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"FFmpeg unavailable: {exc}")


def _png(path: Path, size=(64, 48), color=(200, 40, 20)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# integrity
# ---------------------------------------------------------------------------


def test_integrity_verifies_an_unchanged_artifact(tmp_path: Path) -> None:
    artifact = _png(tmp_path / "a.png")
    report = verify_integrity(artifact, _sha256(artifact))
    assert report.verified is True
    assert report.method == "independent_rehash"
    assert report.findings == ()


def test_integrity_refuses_a_tampered_artifact(tmp_path: Path) -> None:
    artifact = _png(tmp_path / "a.png")
    recorded = _sha256(artifact)
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    report = verify_integrity(artifact, recorded)
    assert report.verified is False
    assert "digest mismatch" in report.findings[0]


def test_integrity_refuses_a_missing_artifact(tmp_path: Path) -> None:
    report = verify_integrity(tmp_path / "absent.png", "sha256:" + "0" * 64)
    assert report.verified is False
    assert "missing" in report.findings[0]


# ---------------------------------------------------------------------------
# image semantics
# ---------------------------------------------------------------------------


def test_image_semantics_verify_a_plain_png(tmp_path: Path) -> None:
    artifact = _png(tmp_path / "a.png", size=(64, 48))
    report = verify_image_semantics(artifact, expected_width=64, expected_height=48)
    assert report.verified is True
    assert report.measurements["format"] == "PNG"
    assert report.measurements["width"] == 64


def test_image_semantics_refuse_a_dimension_mismatch(tmp_path: Path) -> None:
    artifact = _png(tmp_path / "a.png", size=(64, 48))
    report = verify_image_semantics(artifact, expected_width=128, expected_height=48)
    assert report.verified is False
    assert any("width mismatch" in finding for finding in report.findings)


def test_image_semantics_refuse_a_coloured_image_claiming_grayscale(tmp_path: Path) -> None:
    """The check is *pixel-level*, not a metadata check."""
    artifact = _png(tmp_path / "color.png", color=(200, 40, 20))
    report = verify_image_semantics(artifact, expect_grayscale=True)
    assert report.verified is False
    assert report.measurements["grayscale"] is False
    assert any("not grayscale" in finding for finding in report.findings)


def test_image_semantics_accept_a_real_grayscale_image(tmp_path: Path) -> None:
    artifact = tmp_path / "gray.png"
    Image.new("L", (32, 32), 128).save(artifact)
    report = verify_image_semantics(artifact, expect_grayscale=True)
    assert report.verified is True
    assert report.measurements["grayscale"] is True


def test_image_semantics_refuse_a_non_png(tmp_path: Path) -> None:
    artifact = tmp_path / "a.bmp"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(artifact)
    report = verify_image_semantics(artifact)
    assert report.verified is False
    assert any("expected a PNG" in finding for finding in report.findings)


def test_image_semantics_refuse_an_undecodable_file(tmp_path: Path) -> None:
    artifact = tmp_path / "broken.png"
    artifact.write_bytes(b"not an image")
    report = verify_image_semantics(artifact)
    assert report.verified is False
    assert any("could not be decoded" in finding for finding in report.findings)


# ---------------------------------------------------------------------------
# video semantics
# ---------------------------------------------------------------------------


def _clip(path: Path, *, seconds: int = 1, fps: int = 10, size: str = "64x48") -> Path:
    binary = _ffmpeg()
    import subprocess

    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size={size}:rate={fps}",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )
    return path


def test_video_semantics_verify_a_real_clip(tmp_path: Path) -> None:
    artifact = _clip(tmp_path / "clip.mp4")
    report = verify_video_semantics(
        artifact,
        expected_width=64,
        expected_height=48,
        expected_duration_us=1_000_000,
        expected_fps=10,
    )
    assert report.verified is True, report.findings
    assert report.measurements["codec"] == "h264"
    assert report.measurements["decoded_frames"] == 10


def test_video_semantics_count_decoded_frames_not_container_frames(tmp_path: Path) -> None:
    artifact = _clip(tmp_path / "clip.mp4", seconds=2, fps=15)
    report = verify_video_semantics(artifact, expected_fps=15)
    assert report.verified is True, report.findings
    assert report.measurements["decoded_frames"] == 30


def test_video_semantics_refuse_a_truncated_clip(tmp_path: Path) -> None:
    artifact = _clip(tmp_path / "clip.mp4", seconds=2, fps=10)
    raw = artifact.read_bytes()
    artifact.write_bytes(raw[: len(raw) // 2])
    report = verify_video_semantics(artifact)
    assert report.verified is False
    assert any("does not decode cleanly" in f or "decoded zero" in f for f in report.findings)


def test_video_semantics_refuse_a_dimension_mismatch(tmp_path: Path) -> None:
    artifact = _clip(tmp_path / "clip.mp4")
    report = verify_video_semantics(artifact, expected_width=128)
    assert report.verified is False
    assert any("width mismatch" in finding for finding in report.findings)


def test_video_semantics_refuse_a_duration_mismatch(tmp_path: Path) -> None:
    artifact = _clip(tmp_path / "clip.mp4", seconds=1)
    report = verify_video_semantics(artifact, expected_duration_us=5_000_000)
    assert report.verified is False
    assert any("duration mismatch" in finding for finding in report.findings)


def test_the_raw_stream_table_is_read_from_the_binary(tmp_path: Path) -> None:
    artifact = _clip(tmp_path / "clip.mp4")
    raw = raw_stream_table(artifact)
    assert "Video:" in raw
    assert "h264" in raw.lower()


def test_the_decoder_returns_the_frame_count(tmp_path: Path) -> None:
    artifact = _clip(tmp_path / "clip.mp4", seconds=1, fps=25)
    count, errors = decode_video_frames(artifact)
    assert errors == ""
    assert count == 25


# ---------------------------------------------------------------------------
# the contract-driven dispatch
# ---------------------------------------------------------------------------


def test_the_contract_selects_the_measurement(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.models import MediaContract

    gray = tmp_path / "gray.png"
    Image.new("L", (16, 16), 100).save(gray)
    report = verify_media_semantics(
        gray, MediaContract(kind="image", container="png", codec="png", color_space="gray")
    )
    assert report.verified is True
    assert report.method == "independent_image_decode"


def test_an_unmeasurable_contract_fails_closed(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.studio.models import MediaContract

    artifact = _png(tmp_path / "a.png")
    report = verify_media_semantics(artifact, MediaContract(kind="data"))
    assert report.verified is False


# ---------------------------------------------------------------------------
# the proof-carrying artifact
# ---------------------------------------------------------------------------


def _receipt() -> NodeReceipt:
    return NodeReceipt(
        node_id="resize",
        operation="media.resize_image",
        capability_id="media.resize_image",
        capability_version="1.0.0",
        params={"width": 64, "height": 48},
        input_hashes={"source": "sha256:" + "a" * 64},
        output_path="/tmp/a.png",
        output_hash="sha256:" + "b" * 64,
        output_size_bytes=10,
        duration_seconds=0.1,
        state_revision=2,
        state_hash="sha256:" + "c" * 64,
        asset_id="derived_1",
        semantic=SemanticReport(verified=True, method="independent_image_decode"),
    )


def _artifact(tmp_path: Path) -> ProofCarryingArtifact:
    artifact = _png(tmp_path / "a.png")
    return ProofCarryingArtifact(
        artifact_id="art_1",
        output_name="final",
        output_path=str(artifact),
        output_hash=_sha256(artifact),
        output_size_bytes=artifact.stat().st_size,
        input_hashes={"src": "sha256:" + "0" * 64},
        operation_manifest={"plan_fingerprint": "sha256:" + "1" * 64},
        tool_versions={"ffmpeg": "n9.0.2"},
        parameters={"resize": {"width": 64}},
        lineage=("resize:media.resize_image@1.0.0",),
        receipts=(_receipt(),),
        integrity=verify_integrity(artifact, _sha256(artifact)),
        semantic=verify_image_semantics(artifact),
        plan_fingerprint="sha256:" + "1" * 64,
        registry_fingerprint="sha256:" + "2" * 64,
        license_ledger={"unknown_categories": ["input_asset"]},
        state_revision=2,
        state_hash="sha256:" + "c" * 64,
    )


def test_a_proof_carrying_artifact_reports_both_layers(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    assert artifact.verified is True
    document = artifact.as_dict()
    assert document["integrity"]["method"] == "independent_rehash"
    assert document["semantic"]["method"] == "independent_image_decode"
    assert document["lineage"] == ["resize:media.resize_image@1.0.0"]
    assert document["receipts"][0]["semantic"]["verified"] is True


def test_a_failed_integrity_layer_blocks_the_artifact(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    broken = ProofCarryingArtifact(
        **{
            **{
                field: getattr(artifact, field)
                for field in artifact.__dataclass_fields__  # type: ignore[attr-defined]
            },
            "integrity": IntegrityReport(
                verified=False, method="independent_rehash", findings=("mismatch",)
            ),
        }
    )
    assert broken.verified is False
