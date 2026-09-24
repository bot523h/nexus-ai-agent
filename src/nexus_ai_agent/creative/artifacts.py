"""Artifact truth: logical identity vs. render spec vs. physical bytes (session 3).

Session-2 honesty finding (NAG-003): several pack operations record a
``content_sha256`` that is ``sha256(<render-spec text>)`` — a *derivation
digest* — in the same field that elsewhere means "hash of the media bytes".
A downstream reader cannot tell whether the hash identifies a file that
exists or a spec that was imagined.  This module separates the three
identities for every artifact-producing path and verifies the physical one
against the bytes on disk:

* ``logical_content_identity`` — what the media *is* (for sources: the hash
  of the staged input bytes; for derived media: ``(parents, op, params)``);
* ``render_spec_hash`` — ``sha256`` of the canonical render spec (the
  :class:`CompiledLane` ``ir_hash``); identifies the *recipe*, never the file;
* ``physical_artifact_sha256`` — ``sha256`` of the produced file's bytes,
  measured after the encode; the only hash a downloader can re-check.

Invariant (machine-enforced by ``tests/unit/test_artifact_truth.py``)::

    sha256(render_spec) != physical_artifact_sha256

unless the bytes were actually hashed — i.e. the two digests are computed by
different functions over different inputs, and :func:`verify_artifact`
recomputes the physical one from disk every time.

Probing: :func:`probe_media` prefers ``ffprobe`` (machine-readable
container/stream facts — the designed evidence tool) and falls back to the
repository's ``ffmpeg``-stderr parser when no ``ffprobe`` binary resolves.
The ``prover`` field records which one produced the facts, so "probed"
never silently means "guessed".

Failure rule: any verification failure raises :class:`ArtifactVerificationError`
— a failed verification can never yield a :class:`PhysicalArtifact`, so no
caller can report ``COMPLETED``/``success`` from a broken render.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MICROSECONDS_PER_SECOND = 1_000_000


class ArtifactVerificationError(ValueError):
    """The bytes on disk do not back the claimed artifact (fail-closed)."""


@dataclass(frozen=True)
class StreamFact:
    """One probed stream (container truth, not spec wishes)."""

    index: int
    codec_type: str  # "video" | "audio" | "subtitle" | ...
    codec_name: str
    width: int | None = None
    height: int | None = None
    duration_us: int | None = None


@dataclass(frozen=True)
class ProbeFact:
    """Container/stream facts plus which prover produced them."""

    prover: str  # "ffprobe" | "ffmpeg-stderr"
    container: str
    duration_us: int
    streams: tuple[StreamFact, ...]
    width: int | None = None
    height: int | None = None

    @property
    def has_video(self) -> bool:
        return any(s.codec_type == "video" for s in self.streams)

    @property
    def has_audio(self) -> bool:
        return any(s.codec_type == "audio" for s in self.streams)

    def as_dict(self) -> dict[str, Any]:
        return {
            "prover": self.prover,
            "container": self.container,
            "duration_us": self.duration_us,
            "width": self.width,
            "height": self.height,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "streams": [
                {
                    "index": s.index,
                    "codec_type": s.codec_type,
                    "codec_name": s.codec_name,
                    "width": s.width,
                    "height": s.height,
                    "duration_us": s.duration_us,
                }
                for s in self.streams
            ],
        }


@dataclass(frozen=True)
class PhysicalArtifact:
    """A verified artifact: bytes exist, hash measured, probe consistent.

    Construct only via :func:`verify_artifact` / :func:`verify_lane_artifact`
    — direct construction skips verification, which is exactly what the
    failure rule forbids callers to do.
    """

    artifact_path: str
    size_bytes: int
    physical_sha256: str  # "sha256:<hex>" of the file bytes
    probe: ProbeFact
    logical_content_identity: str
    render_spec_hash: str | None
    provenance: dict[str, Any]

    def evidence(self) -> dict[str, Any]:
        return {
            "artifact_path": self.artifact_path,
            "artifact_size": self.size_bytes,
            "physical_artifact_sha256": self.physical_sha256,
            "logical_content_identity": self.logical_content_identity,
            "render_spec_hash": self.render_spec_hash,
            "probe": self.probe.as_dict(),
            "provenance": dict(self.provenance),
        }


def sha256_bytes(data: bytes) -> str:
    """``sha256:<hex>`` of in-memory bytes (documents, specs)."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file_bytes(path: Path) -> str:
    """``sha256:<hex>`` streamed from a file (never loads it whole)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def render_spec_hash_of(spec: dict[str, Any]) -> str:
    """Canonical ``sha256`` of a render-spec dict (recipe identity).

    Sort-keys + compact separators make this byte-stable across runs; it
    identifies the *recipe* and must never be reported as a file hash.
    """
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(canonical.encode("utf-8"))


def resolve_ffprobe_bin(override: str | None = None) -> str | None:
    """Resolve an ``ffprobe`` binary: override → ``NEXUS_FFPROBE_BIN`` → ``PATH``.

    Returns ``None`` (no exception) when unresolvable — the caller falls back
    to the ``ffmpeg``-stderr prover and records it.  The ``imageio-ffmpeg``
    wheel ships no ``ffprobe``; production images install the real one.
    """
    candidates: list[str] = []
    if override:
        candidates.append(override)
    env = os.environ.get("NEXUS_FFPROBE_BIN")
    if env:
        candidates.append(env)
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("ffprobe")
    return found


def _probe_with_ffprobe(path: Path, binary: str, timeout: int) -> ProbeFact:
    try:
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArtifactVerificationError(f"ffprobe timed out on {path}") from exc
    if result.returncode != 0:
        raise ArtifactVerificationError(
            f"ffprobe failed on {path}: {(result.stderr or '').strip()[-300:]}"
        )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ArtifactVerificationError(f"ffprobe emitted no JSON for {path}: {exc}") from exc
    fmt = payload.get("format") or {}
    try:
        duration_s = float(fmt.get("duration", 0.0))
    except (TypeError, ValueError):
        duration_s = 0.0
    streams: list[StreamFact] = []
    width: int | None = None
    height: int | None = None
    for index, node in enumerate(payload.get("streams") or []):
        codec_type = str(node.get("codec_type") or "unknown")
        codec_name = str(node.get("codec_name") or "unknown")
        stream = StreamFact(
            index=int(node.get("index", index)),
            codec_type=codec_type,
            codec_name=codec_name,
            width=node.get("width"),
            height=node.get("height"),
        )
        streams.append(stream)
        if codec_type == "video" and width is None:
            width = node.get("width")
            height = node.get("height")
    return ProbeFact(
        prover="ffprobe",
        container=str(fmt.get("format_name") or path.suffix.lstrip(".") or "unknown"),
        duration_us=int(duration_s * MICROSECONDS_PER_SECOND),
        streams=tuple(streams),
        width=width,
        height=height,
    )


def _probe_with_ffmpeg_stderr(path: Path, *, binary: str | None, timeout: int) -> ProbeFact:
    from nexus_ai_agent.creative.slideshow.ffmpeg import probe_video, resolve_ffmpeg_bin

    resolved = binary or resolve_ffmpeg_bin()
    info = probe_video(path, binary=resolved)
    streams: list[StreamFact] = []
    if info.width is not None:
        streams.append(
            StreamFact(
                index=0,
                codec_type="video",
                codec_name="unknown",
                width=info.width,
                height=info.height,
                duration_us=info.duration_us,
            )
        )
    if info.has_audio:
        streams.append(
            StreamFact(
                index=len(streams),
                codec_type="audio",
                codec_name="unknown",
                duration_us=info.duration_us,
            )
        )
    return ProbeFact(
        prover="ffmpeg-stderr",
        container=path.suffix.lstrip(".") or "unknown",
        duration_us=info.duration_us,
        streams=tuple(streams),
        width=info.width,
        height=info.height,
    )


def probe_media(
    path: Path,
    *,
    ffprobe_bin: str | None = None,
    ffmpeg_bin: str | None = None,
    timeout: int = 120,
) -> ProbeFact:
    """Probe container/stream facts: ``ffprobe`` first, ``ffmpeg``-stderr fallback.

    The returned :class:`ProbeFact` always names its ``prover``.  Raises
    :class:`ArtifactVerificationError` when neither prover can read the file.
    """
    resolved = resolve_ffprobe_bin(ffprobe_bin)
    if resolved is not None:
        try:
            return _probe_with_ffprobe(path, resolved, timeout)
        except ArtifactVerificationError:
            # A failing ffprobe is evidence of a broken file, not a cue to
            # try a weaker prover that might wave it through — fail closed.
            raise
    try:
        return _probe_with_ffmpeg_stderr(path, binary=ffmpeg_bin, timeout=timeout)
    except Exception as exc:
        raise ArtifactVerificationError(f"no prover could read {path}: {exc}") from exc


def verify_artifact(
    path: Path,
    *,
    logical_content_identity: str,
    render_spec_hash: str | None = None,
    expected_sha256: str | None = None,
    expected_duration_us: int | None = None,
    duration_tolerance_us: int = 300_000,
    provenance: dict[str, Any] | None = None,
    ffprobe_bin: str | None = None,
    ffmpeg_bin: str | None = None,
) -> PhysicalArtifact:
    """Verify a produced file and return its measured identity.

    Checks, in order — the first failure raises, so a broken render can
    never produce a verifiable artifact:

    1. the path exists and is a non-empty regular file;
    2. ``sha256(bytes)`` is measured from disk (and matches ``expected_sha256``
       when the caller pins one — a spec digest never matches by accident);
    3. the file probes (``ffprobe`` preferred; fallback recorded);
    4. probed duration is positive and, when pinned, within tolerance;
    5. at least one stream exists (an "empty" container is not a deliverable).
    """
    candidate = Path(path)
    if not candidate.is_file():
        raise ArtifactVerificationError(f"artifact missing: {candidate}")
    size = candidate.stat().st_size
    if size <= 0:
        raise ArtifactVerificationError(f"artifact is empty: {candidate}")
    measured = sha256_file_bytes(candidate)
    if expected_sha256 is not None and measured != expected_sha256:
        raise ArtifactVerificationError(
            f"artifact hash mismatch for {candidate}: "
            f"measured {measured} != expected {expected_sha256}"
        )
    probe = probe_media(candidate, ffprobe_bin=ffprobe_bin, ffmpeg_bin=ffmpeg_bin)
    if probe.duration_us <= 0:
        raise ArtifactVerificationError(f"artifact has no positive duration: {candidate}")
    if not probe.streams:
        raise ArtifactVerificationError(f"artifact has no streams: {candidate}")
    if expected_duration_us is not None and (
        abs(probe.duration_us - expected_duration_us) > duration_tolerance_us
    ):
        raise ArtifactVerificationError(
            f"artifact duration {probe.duration_us}µs is outside ±{duration_tolerance_us}µs "
            f"of expected {expected_duration_us}µs: {candidate}"
        )
    return PhysicalArtifact(
        artifact_path=str(candidate),
        size_bytes=size,
        physical_sha256=measured,
        probe=probe,
        logical_content_identity=logical_content_identity,
        render_spec_hash=render_spec_hash,
        provenance=dict(provenance or {}),
    )


def verify_lane_artifact(
    lane_artifact: Any,
    *,
    logical_content_identity: str,
    expected_duration_us: int | None = None,
    duration_tolerance_us: int = 300_000,
    ffprobe_bin: str | None = None,
) -> PhysicalArtifact:
    """Independently re-verify a :class:`LaneArtifact` against its bytes.

    The lane reports its own hash/probe at encode time; this re-measures both
    from disk and cross-checks — a lane that lied (or a file swapped after the
    encode) fails here.  ``expected_sha256`` is the lane's own claim, so M2
    (spec digest instead of bytes) turns RED at this comparison.
    """
    return verify_artifact(
        Path(lane_artifact.path),
        logical_content_identity=logical_content_identity,
        render_spec_hash=lane_artifact.lane_ir_hash,
        expected_sha256=lane_artifact.sha256,
        expected_duration_us=(
            lane_artifact.duration_us if expected_duration_us is None else expected_duration_us
        ),
        duration_tolerance_us=duration_tolerance_us,
        provenance={
            "lane_ir_hash": lane_artifact.lane_ir_hash,
            "lane_ops": list(getattr(lane_artifact, "ops", ()) or ()),
            "lane_binary": lane_artifact.binary,
        },
        ffprobe_bin=ffprobe_bin,
        ffmpeg_bin=lane_artifact.binary,
    )


__all__ = [
    "ArtifactVerificationError",
    "MICROSECONDS_PER_SECOND",
    "PhysicalArtifact",
    "ProbeFact",
    "StreamFact",
    "probe_media",
    "render_spec_hash_of",
    "resolve_ffprobe_bin",
    "sha256_bytes",
    "sha256_file_bytes",
    "verify_artifact",
    "verify_lane_artifact",
]
