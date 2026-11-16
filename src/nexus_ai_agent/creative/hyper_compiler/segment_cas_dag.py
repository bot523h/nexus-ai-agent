"""Content-addressed segment DAG for timeline re-render avoidance.

A timeline is decomposed into deterministic :class:`TimelineSegmentSpec`
segments.  Each segment gets a content-addressed key (:func:`segment_cas_key`)
derived from a canonical, length-prefixed, type-tagged encoding of *every*
choice that affects the rendered pixels/audio.  The timeline as a whole commits
to an RFC 6962 Merkle root (:func:`timeline_merkle_root`) that also binds the
leaf count, so two timelines with the same leaves but a different number of
them cannot share a root.

:class:`SegmentCASPlanner` diffs a new timeline against the artifacts already on
disk and classifies each segment as ``CACHE_HIT`` or ``RENDER_REQUIRED``.  A
20-segment timeline where a single segment changed therefore re-renders exactly
one segment (95% cache hit) instead of twenty.  Every artifact is written
atomically (``.tmp`` → ``fsync`` → ``os.replace``) and re-verified — content
hash *and* on-disk file hash — before it is ever consumed (fail-closed).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import struct
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from nexus_ai_agent.creative.hyper_compiler.errors import (
    HyperCompilerError,
    SegmentIntegrityError,
)

#: Domain separators keep the three hash families (spec key, Merkle leaf,
#: Merkle node) from ever colliding on the same byte string.
_SPEC_DOMAIN = b"NEXUS.SEGMENT.CAS.v1"
_MTH_DOMAIN = b"NEXUS.TIMELINE.MTH.v1"
_ARTIFACT_MAGIC = b"NAGARSEG1"
_KEY_HEX_LEN = 64
_CONTENT_SHA_OFFSET = len(_ARTIFACT_MAGIC) + _KEY_HEX_LEN + 8
_HEADER_LEN = _CONTENT_SHA_OFFSET + 32


def _length_prefixed(tag: bytes, payload: bytes) -> bytes:
    """``tag || len(payload) as u64 || payload`` — length-bound, unambiguous."""
    return tag + len(payload).to_bytes(8, "big") + payload


@dataclass(frozen=True)
class TimelineSegmentSpec:
    """One deterministic slice of a timeline (frames are half-open ``[s, e)``).

    The four ``*_digest`` fields are opaque caller-supplied digests of the
    upstream decisions (shot DNA, camera, lighting, audio) that feed the
    segment; they are folded into the content-addressed key so any upstream
    change invalidates exactly the affected segments.
    """

    segment_id: str
    start_frame: int
    end_frame: int
    fps: float
    width: int
    height: int
    shot_dna_digest: str
    camera_digest: str
    lighting_digest: str
    audio_digest: str
    audio_filter_digest: str

    def __post_init__(self) -> None:
        if not self.segment_id:
            raise HyperCompilerError("segment_id must be non-empty")
        if self.start_frame < 0:
            raise HyperCompilerError(f"start_frame must be >= 0, got {self.start_frame}")
        if self.end_frame <= self.start_frame:
            raise HyperCompilerError(
                f"end_frame ({self.end_frame}) must be > start_frame ({self.start_frame})"
            )
        if not math.isfinite(self.fps) or self.fps <= 0.0:
            raise HyperCompilerError(f"fps must be finite and > 0, got {self.fps!r}")
        if self.width <= 0 or self.height <= 0:
            raise HyperCompilerError(f"frame size must be positive, got {self.width}x{self.height}")
        for name in (
            "shot_dna_digest",
            "camera_digest",
            "lighting_digest",
            "audio_digest",
            "audio_filter_digest",
        ):
            if not getattr(self, name):
                raise HyperCompilerError(f"{name} must be non-empty")

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame


def canonical_spec_bytes(spec: TimelineSegmentSpec) -> bytes:
    """Deterministic, length-prefixed, type-tagged encoding of a segment spec."""
    parts = (
        _length_prefixed(b"seg", spec.segment_id.encode("utf-8")),
        _length_prefixed(b"i64", struct.pack(">q", spec.start_frame)),
        _length_prefixed(b"i64", struct.pack(">q", spec.end_frame)),
        _length_prefixed(b"f64", struct.pack(">d", spec.fps)),
        _length_prefixed(b"i64", struct.pack(">q", spec.width)),
        _length_prefixed(b"i64", struct.pack(">q", spec.height)),
        _length_prefixed(b"dna", spec.shot_dna_digest.encode("utf-8")),
        _length_prefixed(b"cam", spec.camera_digest.encode("utf-8")),
        _length_prefixed(b"lit", spec.lighting_digest.encode("utf-8")),
        _length_prefixed(b"aud", spec.audio_digest.encode("utf-8")),
        _length_prefixed(b"afx", spec.audio_filter_digest.encode("utf-8")),
    )
    return _SPEC_DOMAIN + b"".join(parts)


def segment_cas_key(spec: TimelineSegmentSpec, *, hmac_key: bytes | None = None) -> str:
    """Content-addressed key for ``spec`` (HMAC-SHA256 when a key is supplied)."""
    data = canonical_spec_bytes(spec)
    if hmac_key is not None:
        return hmac.new(hmac_key, data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


# ── RFC 6962 Merkle tree ────────────────────────────────────────────────────
def _merkle_leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _merkle_node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _merkle_hash_nodes(nodes: Sequence[bytes]) -> bytes:
    """RFC 6962 MTH over already-leaf-hashed nodes (odd tail is promoted)."""
    count = len(nodes)
    if count == 1:
        return nodes[0]
    split = 1
    while split * 2 < count:
        split *= 2
    return _merkle_node_hash(
        _merkle_hash_nodes(nodes[:split]),
        _merkle_hash_nodes(nodes[split:]),
    )


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    """RFC 6962 Merkle Tree Hash of ``leaves``.

    An empty tree hashes the empty string; a single leaf hashes
    ``0x00 || leaf``.  For an odd number of leaves the final node is *promoted*
    (never duplicated), so ``root(5 leaves)`` differs from the classic
    "duplicate the last node" construction.
    """
    if not leaves:
        return hashlib.sha256(b"").digest()
    return _merkle_hash_nodes([_merkle_leaf_hash(leaf) for leaf in leaves])


def merkle_root_hex(leaves: Sequence[bytes]) -> str:
    return merkle_root(leaves).hex()


def timeline_merkle_root(cas_keys: Sequence[str], *, total_leaves: int | None = None) -> str:
    """Timeline commitment: Merkle root of the segment keys, bound to the count.

    ``total_leaves`` defaults to ``len(cas_keys)``; passing a different value
    yields a different root, which is what makes the leaf count itself part of
    the commitment.
    """
    bound = len(cas_keys) if total_leaves is None else total_leaves
    if bound < 0:
        raise HyperCompilerError(f"total_leaves must be >= 0, got {bound}")
    leaves = [key.encode("utf-8") for key in cas_keys]
    inner = merkle_root(leaves)
    return hashlib.sha256(_MTH_DOMAIN + struct.pack(">Q", bound) + inner).hexdigest()


# ── Segment planning ────────────────────────────────────────────────────────
class SegmentStatus(str, Enum):
    """Whether a segment can be reused from cache or must be rendered again."""

    CACHE_HIT = "CACHE_HIT"
    RENDER_REQUIRED = "RENDER_REQUIRED"


@dataclass(frozen=True)
class PlannedSegment:
    spec: TimelineSegmentSpec
    cas_key: str
    status: SegmentStatus
    artifact_path: Path


@dataclass(frozen=True)
class SegmentPlan:
    """The planner's verdict for a whole timeline."""

    segments: tuple[PlannedSegment, ...]
    merkle_root: str
    total_leaves: int
    cache_hits: int
    render_required: int

    @property
    def cache_hit_ratio(self) -> float:
        if not self.segments:
            return 0.0
        return self.cache_hits / len(self.segments)

    def render_targets(self) -> tuple[PlannedSegment, ...]:
        return tuple(s for s in self.segments if s.status is SegmentStatus.RENDER_REQUIRED)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: ``.tmp`` → ``fsync`` → ``replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _encode_artifact(cas_key: str, body: bytes) -> bytes:
    key_bytes = cas_key.encode("ascii")
    if len(key_bytes) != _KEY_HEX_LEN:
        raise SegmentIntegrityError(f"cas_key must be {_KEY_HEX_LEN} hex chars, got {cas_key!r}")
    return (
        _ARTIFACT_MAGIC
        + key_bytes
        + struct.pack(">Q", len(body))
        + hashlib.sha256(body).digest()
        + body
    )


def _decode_artifact(raw: bytes) -> tuple[str, bytes]:
    if len(raw) < _HEADER_LEN:
        raise SegmentIntegrityError(f"artifact truncated: {len(raw)} bytes < header")
    if raw[: len(_ARTIFACT_MAGIC)] != _ARTIFACT_MAGIC:
        raise SegmentIntegrityError("artifact magic mismatch")
    key = raw[len(_ARTIFACT_MAGIC) : len(_ARTIFACT_MAGIC) + _KEY_HEX_LEN].decode("ascii")
    (content_len,) = struct.unpack(">Q", raw[_CONTENT_SHA_OFFSET - 8 : _CONTENT_SHA_OFFSET])
    content_sha = raw[_CONTENT_SHA_OFFSET:_HEADER_LEN]
    body = raw[_HEADER_LEN:]
    if content_len != len(body):
        raise SegmentIntegrityError(
            f"artifact length mismatch: header={content_len} body={len(body)}"
        )
    if hashlib.sha256(body).digest() != content_sha:
        raise SegmentIntegrityError("artifact content hash mismatch")
    return key, body


class SegmentCASPlanner:
    """Plan cache hits / re-renders and persist verified segment artifacts."""

    def __init__(self, cache_dir: Path, *, hmac_key: bytes | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self._hmac_key = hmac_key

    # -- keys & paths -------------------------------------------------------
    def cas_key(self, spec: TimelineSegmentSpec) -> str:
        return segment_cas_key(spec, hmac_key=self._hmac_key)

    def artifact_path(self, cas_key: str) -> Path:
        return self.cache_dir / f"{cas_key}.nagarseg"

    def manifest_path(self, cas_key: str) -> Path:
        return self.cache_dir / f"{cas_key}.manifest.json"

    # -- planning -----------------------------------------------------------
    def plan(self, timeline: Sequence[TimelineSegmentSpec]) -> SegmentPlan:
        planned: list[PlannedSegment] = []
        keys: list[str] = []
        for spec in timeline:
            key = self.cas_key(spec)
            keys.append(key)
            path = self.artifact_path(key)
            try:
                self.verify_segment_artifact(key, path)
            except HyperCompilerError:
                status = SegmentStatus.RENDER_REQUIRED
            else:
                status = SegmentStatus.CACHE_HIT
            planned.append(PlannedSegment(spec, key, status, path))
        cache_hits = sum(1 for seg in planned if seg.status is SegmentStatus.CACHE_HIT)
        return SegmentPlan(
            segments=tuple(planned),
            merkle_root=timeline_merkle_root(keys),
            total_leaves=len(planned),
            cache_hits=cache_hits,
            render_required=len(planned) - cache_hits,
        )

    # -- artifact lifecycle -------------------------------------------------
    def write_segment_artifact(self, cas_key: str, payload: bytes) -> Path:
        """Atomically persist ``payload`` under ``cas_key`` plus its manifest."""
        path = self.artifact_path(cas_key)
        raw = _encode_artifact(cas_key, payload)
        atomic_write_bytes(path, raw)
        manifest = {
            "cas_key": cas_key,
            "content_sha256": hashlib.sha256(payload).hexdigest(),
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
        atomic_write_bytes(
            self.manifest_path(cas_key),
            json.dumps(manifest, sort_keys=True).encode("utf-8"),
        )
        return path

    def commit_segment(self, spec: TimelineSegmentSpec, payload: bytes) -> Path:
        """Render-and-cache a single segment; returns the artifact path."""
        return self.write_segment_artifact(self.cas_key(spec), payload)

    def verify_segment_artifact(self, cas_key: str, path: Path | None = None) -> bytes:
        """Return the verified payload, or raise :class:`SegmentIntegrityError`.

        Both the *content* hash (recomputed over the body) and the *on-disk file*
        hash (recomputed over the whole artifact and compared to the manifest)
        are checked before the bytes are handed back.
        """
        artifact = Path(path) if path is not None else self.artifact_path(cas_key)
        if not artifact.is_file():
            raise SegmentIntegrityError(f"segment artifact missing: {artifact}")
        raw = artifact.read_bytes()
        manifest_file = self.manifest_path(cas_key)
        if not manifest_file.is_file():
            raise SegmentIntegrityError(f"segment manifest missing: {manifest_file}")
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SegmentIntegrityError(f"segment manifest unreadable: {exc}") from exc
        if not isinstance(manifest, dict):
            raise SegmentIntegrityError("segment manifest is not an object")
        if manifest.get("file_sha256") != hashlib.sha256(raw).hexdigest():
            raise SegmentIntegrityError("segment file hash mismatch on disk")
        if manifest.get("size") != len(raw):
            raise SegmentIntegrityError("segment file size mismatch on disk")
        stored_key, body = _decode_artifact(raw)
        if stored_key != cas_key:
            raise SegmentIntegrityError(f"artifact key mismatch: {stored_key} != {cas_key}")
        if manifest.get("cas_key") != cas_key:
            raise SegmentIntegrityError("manifest key mismatch")
        if manifest.get("content_sha256") != hashlib.sha256(body).hexdigest():
            raise SegmentIntegrityError("segment content hash mismatch on disk")
        return body


__all__ = [
    "PlannedSegment",
    "SegmentCASPlanner",
    "SegmentPlan",
    "SegmentStatus",
    "TimelineSegmentSpec",
    "atomic_write_bytes",
    "canonical_spec_bytes",
    "merkle_root",
    "merkle_root_hex",
    "segment_cas_key",
    "timeline_merkle_root",
]
