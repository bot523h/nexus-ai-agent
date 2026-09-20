"""Host-side adapter for the slideshow pack: probing and hashing real media.

The pack itself is pure (stdlib + pydantic); everything that touches the disk
lives here.  Probing produces :class:`AssetEvidence` records that are pinned
into typed commands, so the command bus never performs I/O of its own.

Evidence ids are **content-addressed** (``img_<sha256[:16]>``), which makes a
repeated scan idempotent: the same bytes always map to the same asset id.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageStat

from nexus_ai_agent.creative.packs.slideshow.models import AssetEvidence

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"})
WAV_SUFFIXES = frozenset({".wav"})


class ProbeError(ValueError):
    """A file could not be probed (missing, unreadable or unsupported)."""


def content_sha256(path: Path) -> str:
    """``sha256:<hex>`` of a file's bytes (the TDD content-addressing form)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _evidence_id(prefix: str, content_digest: str) -> str:
    return f"{prefix}_{content_digest.split(':', 1)[1][:16]}"


def probe_image(path: Path | str) -> AssetEvidence:
    """Probe one image: content hash, dimensions and mean luma."""
    source = Path(path)
    if not source.is_file():
        raise ProbeError(f"image not found: {source}")
    if source.suffix.lower() not in IMAGE_SUFFIXES:
        raise ProbeError(f"unsupported image type: {source.suffix!r} ({source})")
    digest = content_sha256(source)
    try:
        with Image.open(source) as image:
            image.load()
            width, height = image.size
            mean_luma = float(ImageStat.Stat(image.convert("L")).mean[0]) / 255.0
    except OSError as exc:
        raise ProbeError(f"cannot read image {source}: {exc}") from exc
    return AssetEvidence(
        evidence_id=_evidence_id("img", digest),
        path=str(source),
        content_sha256=digest,
        media_kind="image",
        width=width,
        height=height,
        mean_luma=max(0.0, min(1.0, mean_luma)),
    )


def probe_audio(path: Path | str) -> AssetEvidence:
    """Probe one audio file.  WAV is decoded with the standard library.

    Compressed containers (mp3/m4a/flac) need the FFmpeg adapter and are
    rejected here with an actionable message rather than probed badly.
    """
    source = Path(path)
    if not source.is_file():
        raise ProbeError(f"audio not found: {source}")
    digest = content_sha256(source)
    if source.suffix.lower() not in WAV_SUFFIXES:
        raise ProbeError(
            f"unsupported audio type: {source.suffix!r}; convert to WAV "
            "(the FFmpeg-backed decoder arrives with the render adapter)"
        )
    try:
        with wave.open(str(source), "rb") as handle:
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
    except wave.Error as exc:
        raise ProbeError(f"cannot read WAV file {source}: {exc}") from exc
    duration_us = int(round(frame_count / float(sample_rate) * 1_000_000)) if sample_rate else 0
    return AssetEvidence(
        evidence_id=_evidence_id("aud", digest),
        path=str(source),
        content_sha256=digest,
        media_kind="audio",
        duration_us=duration_us,
        width=None,
        height=None,
        mean_luma=0.5,
    )


def probe_media(paths: tuple[Path | str, ...]) -> tuple[AssetEvidence, ...]:
    """Probe a mixed list of files, dispatching on suffix."""
    probed: list[AssetEvidence] = []
    for path in paths:
        suffix = Path(path).suffix.lower()
        if suffix in WAV_SUFFIXES:
            probed.append(probe_audio(path))
        else:
            probed.append(probe_image(path))
    return tuple(probed)


def mean_luma_of(path: Path | str) -> float:
    """Mean luma of one image in ``[0, 1]`` (used by analysis helpers)."""
    with Image.open(path) as image:
        return float(np.asarray(image.convert("L"), dtype=np.float32).mean()) / 255.0
