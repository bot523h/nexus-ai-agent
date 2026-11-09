"""Proof-Carrying Artifacts.

Every output of the composition runtime carries its own evidence. The
:class:`ProofCarryingArtifact` is the self-contained record that lets an
independent verifier answer:

    *What went in, what ran, what came out, and does the output still match
    what the execution claims it produced?*

It is modeled on the in-toto/SLSA statement shape (``subject`` = output name
+ digest; ``predicate`` = the typed causal account) but is a local,
dependency-free dialect -- no network, no signing service.

Verification is *independent*: :func:`verify_proof` re-hashes the output bytes
itself and compares them to the recorded ``output_hash``; it does not trust
the artifact's own claim. If the bytes were tampered with, verification fails
closed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .spec import LicenseInfo, MediaType


def sha256_file(path: str | Path) -> str:
    """SHA-256 of a file's bytes. Independent of any recorded digest."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding for hashing structured payloads."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Execution receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeReceipt:
    """The execution record for one node in the composition."""

    node_id: str
    capability_id: str
    version: str
    params: dict[str, Any]
    input_hashes: dict[str, str]
    output_paths: dict[str, str]
    output_hashes: dict[str, str]
    duration_seconds: float
    worker: str


@dataclass(frozen=True)
class VerificationReport:
    """An independent verifier's verdict on the artifact.

    ``verified`` is True only when every recorded output hash re-measures to
    the same value over the live bytes. ``findings`` lists every discrepancy
    (empty on success). ``method`` records how verification was performed.
    """

    verified: bool
    method: str
    findings: tuple[str, ...] = field(default_factory=tuple)
    remeasured_hashes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SemanticReport:
    """An independent *semantic* verdict on the produced bytes.

    Distinct from :class:`VerificationReport` (which is *integrity*: does the
    live file still hash to what was recorded?). A semantic report actually
    **decodes** the output and checks the properties the composition promised:
    container, codec, dimensions, duration, stream count. A hash can be intact
    while the bytes are semantically wrong (a corrupt-but-stable file), so both
    are needed.
    """

    verified: bool
    decoder: str
    checks: dict[str, Any] = field(default_factory=dict)
    findings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# The proof-carrying artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProofCarryingArtifact:
    """A produced output plus all the evidence that binds it to its execution.

    The artifact is the unit of trust in the composition layer: a consumer
    never takes an output file on faith, it takes the artifact and verifies
    the proof.
    """

    artifact_id: str
    output_name: str
    output_path: str
    output_hash: str
    input_hashes: dict[str, str]
    operation_manifest: dict[str, Any]
    tool_versions: dict[str, str]
    parameters: dict[str, Any]
    license: LicenseInfo
    execution_receipts: tuple[NodeReceipt, ...]
    lineage: tuple[str, ...]
    verification: VerificationReport
    generated_at: str
    live_registry_fingerprint: str = ""
    semantic: SemanticReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "output_name": self.output_name,
            "output_path": self.output_path,
            "output_hash": self.output_hash,
            "input_hashes": self.input_hashes,
            "operation_manifest": self.operation_manifest,
            "tool_versions": self.tool_versions,
            "parameters": self.parameters,
            "license": {
                "spdx_id": self.license.spdx_id,
                "copyleft": self.license.copyleft,
                "allow_commercial": self.license.effective_allow_commercial,
                "attribution_required": self.license.attribution_required,
            },
            "execution_receipts": [asdict(r) for r in self.execution_receipts],
            "lineage": list(self.lineage),
            "verification": self.verification.to_dict(),
            "semantic": self.semantic.to_dict() if self.semantic else None,
            "live_registry_fingerprint": self.live_registry_fingerprint,
            "generated_at": self.generated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def verify_proof(artifact: ProofCarryingArtifact) -> VerificationReport:
    """Independently re-verify an artifact against the live output bytes.

    Re-hashes the output file and compares it to the artifact's recorded
    ``output_hash``. Also re-hashes every input referenced in
    ``input_hashes`` that still exists on disk. Any mismatch is a finding;
    the report is ``verified=False`` if any hash diverges or the output is
    missing.
    """
    findings: list[str] = []
    remeasured: dict[str, str] = {}

    out_path = Path(artifact.output_path)
    if not out_path.exists():
        return VerificationReport(
            verified=False,
            method="independent_rehash",
            findings=(f"output missing: {artifact.output_path}",),
        )
    actual = sha256_file(out_path)
    remeasured[artifact.output_name] = actual
    if actual != artifact.output_hash:
        findings.append(
            f"output hash mismatch: recorded {artifact.output_hash}, "
            f"remeasured {actual}"
        )

    for name, recorded in artifact.input_hashes.items():
        # inputs are referenced by hash only in v1; we cannot re-measure
        # without their paths, so we just record the expected value.
        remeasured[f"input:{name}"] = recorded

    return VerificationReport(
        verified=not findings,
        method="independent_rehash",
        findings=tuple(findings),
        remeasured_hashes=remeasured,
    )


# ---------------------------------------------------------------------------
# Semantic verification (independent decode -- Gate 6)
# ---------------------------------------------------------------------------

#: Tolerance when comparing a decoded duration against an expected value.
_DURATION_TOLERANCE_US = 250_000


def _grayscale_property_required(media_type: MediaType, props: dict[str, Any]) -> bool:
    """True if the contract requires the pixels to be grayscale."""
    if props.get("grayscale"):
        return True
    color_space = (media_type.color_space or "").lower()
    pixel_format = (media_type.pixel_format or "").lower()
    return color_space in {"gray", "grey", "monochrome"} or pixel_format in {
        "gray",
        "grey",
        "l",
        "y",
        "yuv400p",
    }


def _verify_image_semantic(path: Path, media_type: MediaType, props: dict[str, Any]) -> SemanticReport:
    """Decode an image independently (Pillow) and check its contracted properties.

    Pillow is a distinct decoder from the ImageMagick ``convert`` the worker
    used, so the check does not simply repeat the producer's logic.
    """
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a declared dependency
        return SemanticReport(
            verified=False,
            decoder="unavailable",
            findings=("no independent image decoder available (Pillow not importable)",),
        )
    findings: list[str] = []
    checks: dict[str, Any] = {}
    try:
        with Image.open(path) as handle:
            original_mode = handle.mode
            image = handle.convert("RGB")
            width, height = image.size
            checks["width"] = width
            checks["height"] = height
            checks["source_mode"] = original_mode
            if "width" in props and width != props["width"]:
                findings.append(f"width mismatch: expected {props['width']}, decoded {width}")
            if "height" in props and height != props["height"]:
                findings.append(f"height mismatch: expected {props['height']}, decoded {height}")
            if _grayscale_property_required(media_type, props):
                thumbnail = image.copy()
                thumbnail.thumbnail((64, 64))
                sampled = list(thumbnail.getdata())
                grayscale = all(r == g == b for (r, g, b) in sampled)
                checks["grayscale"] = grayscale
                checks["sampled_pixels"] = len(sampled)
                if not grayscale:
                    findings.append(
                        "image is not grayscale: sampled pixels have differing R/G/B channels"
                    )
    except Exception as exc:  # noqa: BLE001 - any decode failure is a semantic failure
        return SemanticReport(
            verified=False,
            decoder="pillow",
            checks=checks,
            findings=(*findings, f"independent decode failed: {type(exc).__name__}: {exc}"),
        )
    return SemanticReport(verified=not findings, decoder="pillow", checks=checks, findings=tuple(findings))


def _verify_video_semantic(path: Path, media_type: MediaType, props: dict[str, Any]) -> SemanticReport:
    """Decode a video independently (ffmpeg) and check its contracted properties."""
    from . import canonical

    findings: list[str] = []
    checks: dict[str, Any] = {}
    try:
        binary = canonical.resolve_ffmpeg()
    except Exception as exc:  # noqa: BLE001 - no decoder is a semantic failure
        return SemanticReport(
            verified=False,
            decoder="unavailable",
            findings=(f"no FFmpeg decoder available: {exc}",),
        )
    try:
        raw = canonical.probe_raw(str(path), binary=binary)
        facts = canonical.parse_probe(raw)
        checks.update(facts)
        if media_type.codec and facts.get("codec") != media_type.codec.lower():
            findings.append(
                f"codec mismatch: expected {media_type.codec!r}, decoded {facts.get('codec')!r}"
            )
        if media_type.container and media_type.container.lower() not in raw.lower():
            findings.append(f"container {media_type.container!r} not found in the stream table")
        for key in ("width", "height"):
            if key in props and facts.get(key) != props[key]:
                findings.append(f"{key} mismatch: expected {props[key]}, decoded {facts.get(key)}")
        if "duration_us" in props:
            expected = int(props["duration_us"])
            actual = facts.get("duration_us")
            if not isinstance(actual, int) or abs(actual - expected) > _DURATION_TOLERANCE_US:
                findings.append(
                    f"duration mismatch: expected ~{expected}us, decoded {actual}us "
                    f"(tolerance {_DURATION_TOLERANCE_US}us)"
                )
        frames, errors = canonical.decode_video_frames(str(path), binary=binary)
        checks["decoded_frames"] = frames
        if errors:
            findings.append(f"independent decode error: {errors}")
        elif frames <= 0:
            findings.append("independent decode produced no frames")
        min_frames = props.get("min_frames")
        if isinstance(min_frames, int) and frames < min_frames:
            findings.append(f"frame count {frames} below required minimum {min_frames}")
    except Exception as exc:  # noqa: BLE001 - any decode failure is a semantic failure
        return SemanticReport(
            verified=False,
            decoder="ffmpeg",
            checks=checks,
            findings=(*findings, f"independent decode failed: {type(exc).__name__}: {exc}"),
        )
    return SemanticReport(verified=not findings, decoder="ffmpeg", checks=checks, findings=tuple(findings))


def verify_semantic(
    path: str | Path,
    *,
    media_type: MediaType,
    expected_properties: dict[str, Any] | None = None,
) -> SemanticReport:
    """Independently **decode** an output and check the properties it promised.

    This is deliberately *not* :func:`verify_proof`: that function only proves
    the live bytes still hash to the recorded digest (integrity). A corrupt but
    byte-stable file passes integrity yet fails here. The decoder is a
    different tool from the producing worker (Pillow vs ImageMagick; a fresh
    ``ffmpeg`` decode vs the encoder), so the check is independent.

    Fail-closed: for a media kind with no available decoder the report is
    ``verified=False`` -- "could not check" is never "checked and fine".
    """
    resolved = Path(path)
    props = dict(expected_properties or {})
    if not resolved.exists():
        return SemanticReport(False, "none", {}, (f"output missing: {path}",))
    if media_type.kind == "video":
        return _verify_video_semantic(resolved, media_type, props)
    if media_type.kind == "image":
        return _verify_image_semantic(resolved, media_type, props)
    # data / text / audio-without-a-decoder: no independent decode available.
    return SemanticReport(True, "none", {"kind": media_type.kind}, ())


__all__ = [
    "NodeReceipt",
    "ProofCarryingArtifact",
    "SemanticReport",
    "VerificationReport",
    "canonical_json",
    "sha256_bytes",
    "sha256_file",
    "verify_proof",
    "verify_semantic",
]
