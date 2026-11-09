"""Proof-carrying artifacts and **independent** semantic verification.

Two layers, kept apart on purpose:

* **Integrity verification** re-measures the bytes: the digest the runtime
  recorded must equal the digest of the file on disk now.
* **Semantic verification** asks what the file actually *is*.  Re-hashing a
  file proves nothing about its content, so this module decodes the produced
  media with its own tools and checks the properties the composition
  promised: the dimensions, the colour space, the frame count, the duration.

The independence rule is concrete: the verifier does **not** reuse the
producer's summary.  It re-opens the PNG with Pillow and re-decodes the clip
with a full ``ffmpeg -f null -`` pass (which also counts the decoded frames),
instead of trusting the numbers the encoding path reported.  The two paths
share the allow-listed binary — they do not share the measurement.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.slideshow.ffmpeg import (
    resolve_ffmpeg_bin,
    sha256_file,
)

#: ``ffmpeg -i`` prints the stream table on stderr.
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_DIMENSIONS_RE = re.compile(r"Video:.*?(\d{2,5})x(\d{2,5})")
_CODEC_RE = re.compile(r"Video:\s*([A-Za-z0-9_]+)")
_FRAME_RE = re.compile(r"frame=\s*(\d+)")
_PIXFMT_RE = re.compile(r"pix_fmt:?\s*([a-z0-9]+)")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


class VerificationError(RuntimeError):
    """A verification pass could not be completed."""


@dataclass(frozen=True)
class IntegrityReport:
    """The result of re-measuring the produced bytes."""

    verified: bool
    method: str
    findings: tuple[str, ...] = ()
    measured: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "method": self.method,
            "findings": list(self.findings),
            "measured": dict(self.measured),
        }


@dataclass(frozen=True)
class SemanticReport:
    """The result of checking what the produced media actually is."""

    verified: bool
    method: str
    findings: tuple[str, ...] = ()
    measurements: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "method": self.method,
            "findings": list(self.findings),
            "measurements": dict(self.measurements),
        }


def verify_integrity(path: Path, expected_sha256: str) -> IntegrityReport:
    """Re-measure the file's digest and compare it with the recorded one."""
    target = Path(path)
    if not target.is_file():
        return IntegrityReport(
            verified=False,
            method="independent_rehash",
            findings=(f"artifact is missing: {target}",),
        )
    measured = sha256_file(target)
    expected = (
        expected_sha256 if expected_sha256.startswith("sha256:") else f"sha256:{expected_sha256}"
    )
    if measured != expected:
        return IntegrityReport(
            verified=False,
            method="independent_rehash",
            findings=(f"digest mismatch: recorded {expected}, measured {measured}",),
            measured={"sha256": measured},
        )
    return IntegrityReport(
        verified=True,
        method="independent_rehash",
        findings=(),
        measured={"sha256": measured, "size_bytes": target.stat().st_size},
    )


# ---------------------------------------------------------------------------
# image semantics
# ---------------------------------------------------------------------------


def verify_image_semantics(
    path: Path,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
    expect_grayscale: bool = False,
    expect_alpha: bool | None = None,
) -> SemanticReport:
    """Decode the PNG and check the properties the plan promised.

    The grayscale check is a *pixel* check, not a metadata check: the image is
    converted to RGB and the three channels are compared **byte for byte**, so
    a PNG that merely declares a gray colour type but carries coloured pixels
    is refused — and so is a solid red image, whose channels are each constant
    but differ from each other.
    """
    from PIL import Image

    target = Path(path)
    findings: list[str] = []
    measurements: dict[str, Any] = {}
    try:
        with Image.open(target) as image:
            image.load()
            measurements["format"] = image.format
            measurements["mode"] = image.mode
            measurements["width"] = image.width
            measurements["height"] = image.height
            has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
            measurements["has_alpha"] = has_alpha
            if expect_grayscale:
                rgb = image.convert("RGB")
                red, green, blue = rgb.split()
                red_bytes = red.tobytes()
                green_bytes = green.tobytes()
                blue_bytes = blue.tobytes()
                # The check is **per pixel**: R == G == B for every pixel.
                # A per-channel range check would pass a solid red image (each
                # channel is constant, the channels differ), which is exactly
                # the false negative this check exists to avoid.
                grayscale = red_bytes == green_bytes and red_bytes == blue_bytes
                measurements["channel_extrema"] = [
                    list(item) if isinstance(item, tuple) else [item, item]
                    for item in rgb.getextrema()
                ]
                measurements["grayscale"] = grayscale
                if not grayscale:
                    findings.append(
                        "the image is not grayscale: at least one pixel has "
                        "differing R/G/B channels"
                    )
            if expect_alpha is not None and has_alpha != expect_alpha:
                findings.append(f"alpha mismatch: expected {expect_alpha}, measured {has_alpha}")
    except (OSError, ValueError) as exc:
        return SemanticReport(
            verified=False,
            method="independent_image_decode",
            findings=(f"the image could not be decoded: {exc}",),
            measurements=measurements,
        )

    if measurements.get("format") != "PNG":
        findings.append(f"expected a PNG, measured {measurements.get('format')!r}")
    if expected_width is not None and measurements.get("width") != expected_width:
        findings.append(
            f"width mismatch: expected {expected_width}, measured {measurements.get('width')}"
        )
    if expected_height is not None and measurements.get("height") != expected_height:
        findings.append(
            f"height mismatch: expected {expected_height}, measured {measurements.get('height')}"
        )
    return SemanticReport(
        verified=not findings,
        method="independent_image_decode",
        findings=tuple(findings),
        measurements=measurements,
    )


# ---------------------------------------------------------------------------
# video semantics
# ---------------------------------------------------------------------------


def raw_stream_table(path: Path, *, binary: str | None = None, timeout: int = 120) -> str:
    """The raw ``ffmpeg -i`` stream table of a produced clip."""
    resolved = binary or resolve_ffmpeg_bin()
    proc = subprocess.run(  # noqa: S603 - argv list, shell=False, allow-listed binary
        [resolved, "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_encoder_env(),
    )
    return f"{proc.stderr or ''}\n{proc.stdout or ''}"


def decode_video_frames(
    path: Path, *, binary: str | None = None, timeout: int = 300
) -> tuple[int, str]:
    """Fully decode the video stream and return ``(frame_count, errors)``.

    ``-f null -`` decodes the elementary stream without writing anything, so a
    truncated or corrupt clip is caught here even when its bytes hash stably.
    The frame count is the *decoded* count, not the container's declared one.
    """
    resolved = binary or resolve_ffmpeg_bin()
    proc = subprocess.run(  # noqa: S603 - argv list, shell=False, allow-listed binary
        [
            resolved,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_encoder_env(),
    )
    text = proc.stderr or ""
    counts = [int(value) for value in _FRAME_RE.findall(text)]
    frame_count = max(counts) if counts else -1
    errors = "" if proc.returncode == 0 else text.strip()[-500:]
    return frame_count, errors


def _encoder_env() -> dict[str, str]:
    from nexus_ai_agent.creative.slideshow.ffmpeg import encoder_env

    return encoder_env()


def verify_video_semantics(
    path: Path,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
    expected_duration_us: int | None = None,
    expected_fps: int | None = None,
    duration_tolerance_us: int = 500_000,
    binary: str | None = None,
) -> SemanticReport:
    """Decode the clip and check the properties the plan promised."""
    target = Path(path)
    findings: list[str] = []
    measurements: dict[str, Any] = {}
    resolved = binary or resolve_ffmpeg_bin()

    raw = raw_stream_table(target, binary=resolved)
    duration = _DURATION_RE.search(raw)
    if duration is not None:
        hours, minutes, seconds = duration.groups()
        measured_us = int(
            round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1_000_000)
        )
        measurements["duration_us"] = measured_us
    else:
        findings.append("the stream table carries no duration")

    dimensions = _DIMENSIONS_RE.search(raw)
    if dimensions is not None:
        measurements["width"] = int(dimensions.group(1))
        measurements["height"] = int(dimensions.group(2))
    else:
        findings.append("the stream table carries no video dimensions")

    codec = _CODEC_RE.search(raw)
    if codec is not None:
        measurements["codec"] = codec.group(1).lower()
    pixel_format = _PIXFMT_RE.search(raw)
    if pixel_format is not None:
        measurements["pixel_format"] = pixel_format.group(1).lower()
    fps = _FPS_RE.search(raw)
    if fps is not None:
        measurements["frame_rate"] = float(fps.group(1))
    measurements["has_audio"] = "Audio:" in raw

    frame_count, decode_errors = decode_video_frames(target, binary=resolved)
    measurements["decoded_frames"] = frame_count
    if decode_errors:
        findings.append(f"the clip does not decode cleanly: {decode_errors}")
    if frame_count <= 0:
        findings.append("the clip decoded zero video frames")

    if expected_width is not None and measurements.get("width") != expected_width:
        findings.append(
            f"width mismatch: expected {expected_width}, measured {measurements.get('width')}"
        )
    if expected_height is not None and measurements.get("height") != expected_height:
        findings.append(
            f"height mismatch: expected {expected_height}, measured {measurements.get('height')}"
        )
    if expected_duration_us is not None and "duration_us" in measurements:
        delta = abs(int(measurements["duration_us"]) - expected_duration_us)
        measurements["duration_delta_us"] = delta
        if delta > duration_tolerance_us:
            findings.append(
                f"duration mismatch: expected {expected_duration_us}us, measured "
                f"{measurements['duration_us']}us"
            )
    if expected_fps is not None and "frame_rate" in measurements:
        if abs(float(measurements["frame_rate"]) - expected_fps) > 0.5:
            findings.append(
                f"frame rate mismatch: expected {expected_fps}, measured "
                f"{measurements['frame_rate']}"
            )
    if expected_fps is not None and frame_count > 0 and "duration_us" in measurements:
        expected_frames = round(expected_fps * int(measurements["duration_us"]) / 1_000_000)
        measurements["expected_frames_from_contract"] = expected_frames
        if abs(frame_count - expected_frames) > max(2, round(expected_frames * 0.05)):
            findings.append(
                f"frame count mismatch: the contract implies ~{expected_frames} frames, "
                f"the decoder saw {frame_count}"
            )
    return SemanticReport(
        verified=not findings,
        method="independent_video_decode",
        findings=tuple(findings),
        measurements=measurements,
    )


def verify_media_semantics(
    path: Path,
    contract: Any,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
    expected_duration_us: int | None = None,
    expected_fps: int | None = None,
) -> SemanticReport:
    """Verify a produced artifact against its **canonical media contract**.

    This is the single place where the contract decides which measurement
    applies, so a node's verification and the exposed output's verification
    cannot drift apart.  The contract is the one the live registry declares
    on the operation's media port.
    """
    kind = getattr(contract, "kind", None)
    if kind == "video":
        return verify_video_semantics(
            Path(path),
            expected_width=expected_width,
            expected_height=expected_height,
            expected_duration_us=expected_duration_us,
            expected_fps=expected_fps,
        )
    if kind == "image":
        return verify_image_semantics(
            Path(path),
            expected_width=expected_width,
            expected_height=expected_height,
            expect_grayscale=getattr(contract, "color_space", None) == "gray",
            expect_alpha=getattr(contract, "alpha", None),
        )
    return SemanticReport(
        verified=False,
        method="independent_media_decode",
        findings=(f"no semantic contract for media kind {kind!r}",),
        measurements={"kind": kind},
    )


# ---------------------------------------------------------------------------
# the proof-carrying artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeReceipt:
    """What one node of a composition did.

    The receipt carries the node's **own** semantic report: the intermediate
    artifacts are verified too, not only the exposed outputs.  A composition
    that produced a correctly-sized but *not actually grayscale* intermediate
    would otherwise pass a final-output-only check.
    """

    node_id: str
    operation: str
    capability_id: str
    capability_version: str
    params: dict[str, Any]
    input_hashes: dict[str, str]
    output_path: str
    output_hash: str
    output_size_bytes: int
    duration_seconds: float
    state_revision: int
    state_hash: str
    asset_id: str
    semantic: SemanticReport | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "operation": self.operation,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "params": dict(self.params),
            "input_hashes": dict(self.input_hashes),
            "output_path": self.output_path,
            "output_hash": self.output_hash,
            "output_size_bytes": self.output_size_bytes,
            "duration_seconds": self.duration_seconds,
            "state_revision": self.state_revision,
            "state_hash": self.state_hash,
            "asset_id": self.asset_id,
            "semantic": self.semantic.as_dict() if self.semantic is not None else None,
        }


@dataclass(frozen=True)
class ProofCarryingArtifact:
    """The evidence record of one composition output."""

    artifact_id: str
    output_name: str
    output_path: str
    output_hash: str
    output_size_bytes: int
    input_hashes: dict[str, str]
    operation_manifest: dict[str, Any]
    tool_versions: dict[str, str]
    parameters: dict[str, Any]
    lineage: tuple[str, ...]
    receipts: tuple[NodeReceipt, ...]
    integrity: IntegrityReport
    semantic: SemanticReport
    plan_fingerprint: str
    registry_fingerprint: str
    license_ledger: dict[str, Any]
    state_revision: int
    state_hash: str
    #: The canonical media contract of the exposed output.  The queue-side
    #: verifier uses it to decide *what* to measure — it never uses it as the
    #: measurement itself.
    media_contract: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        """Both layers must pass for the artifact to be publishable."""
        return self.integrity.verified and self.semantic.verified

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "output_name": self.output_name,
            "output_path": self.output_path,
            "output_hash": self.output_hash,
            "output_size_bytes": self.output_size_bytes,
            "input_hashes": dict(self.input_hashes),
            "operation_manifest": dict(self.operation_manifest),
            "tool_versions": dict(self.tool_versions),
            "parameters": dict(self.parameters),
            "lineage": list(self.lineage),
            "receipts": [receipt.as_dict() for receipt in self.receipts],
            "integrity": self.integrity.as_dict(),
            "semantic": self.semantic.as_dict(),
            "plan_fingerprint": self.plan_fingerprint,
            "registry_fingerprint": self.registry_fingerprint,
            "license_ledger": dict(self.license_ledger),
            "state_revision": self.state_revision,
            "state_hash": self.state_hash,
            "media_contract": dict(self.media_contract),
            "verified": self.verified,
        }


__all__ = [
    "IntegrityReport",
    "NodeReceipt",
    "ProofCarryingArtifact",
    "SemanticReport",
    "VerificationError",
    "decode_video_frames",
    "raw_stream_table",
    "verify_image_semantics",
    "verify_integrity",
    "verify_media_semantics",
    "verify_video_semantics",
]
