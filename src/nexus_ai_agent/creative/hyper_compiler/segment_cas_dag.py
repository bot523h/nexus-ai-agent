"""Content-addressed segment DAG for timeline re-render avoidance.

A timeline is decomposed into deterministic :class:`TimelineSegmentSpec`
segments.  Each segment gets a content-addressed key (:func:`segment_cas_key`)
derived from a canonical, length-prefixed, type-tagged encoding of *every*
choice that affects the rendered pixels/audio — including the render-pipeline
fingerprint (``pipeline_digest``), so changing a single knob such as brightness
invalidates exactly the affected segments.  The timeline as a whole commits to an
RFC 6962 Merkle root (:func:`timeline_merkle_root`) that also binds the leaf
count, so two timelines with the same leaves but a different number of them
cannot share a root.

:class:`SegmentCASPlanner` diffs a new timeline against the artifacts already on
disk and classifies each segment as ``CACHE_HIT`` or ``RENDER_REQUIRED``.  A
20-segment timeline where a single segment changed therefore re-renders exactly
one segment (95% cache hit) instead of twenty.  Every artifact is written
atomically (``.tmp`` → ``fsync`` → ``os.replace``) and re-verified — content
hash *and* on-disk file hash, streamed in fixed-size chunks so a whole timeline
is never held in RAM — before it is ever consumed (fail-closed).

Cache keys are validated against ``[0-9a-f]{64}`` and every derived path is
confined to ``cache_dir``, so a hostile or corrupted key can never traverse out
of the cache.  When an ``hmac_key`` is supplied the artifact manifest is sealed
with HMAC-SHA256, so the artifact and its manifest cannot be rewritten
independently without the key.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import struct
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Protocol

from nexus_ai_agent.creative.hyper_compiler.errors import (
    HyperCompilerError,
    SegmentIntegrityError,
)


class ByteSink(Protocol):
    """Anything bytes can be written to (a file handle or an in-memory buffer)."""

    def write(self, data: bytes, /) -> int: ...


#: Domain separators keep the three hash families (spec key, Merkle leaf,
#: Merkle node) from ever colliding on the same byte string.
_SPEC_DOMAIN = b"NEXUS.SEGMENT.CAS.v1"
_MTH_DOMAIN = b"NEXUS.TIMELINE.MTH.v1"
_ARTIFACT_MAGIC = b"NAGARSEG1"
#: Bumped whenever the renderer changes what a segment's bytes mean, so a
#: stale artifact can never be reused across an incompatible renderer.
RENDERER_VERSION = "hyper-compiler/1"
_KEY_HEX_LEN = 64
_CONTENT_SHA_OFFSET = len(_ARTIFACT_MAGIC) + _KEY_HEX_LEN + 8
_HEADER_LEN = _CONTENT_SHA_OFFSET + 32
#: Public alias: byte length of the artifact header (``magic || key || len || sha``).
SEGMENT_HEADER_LEN = _HEADER_LEN
_CHUNK_BYTES = 64 * 1024
#: A manifest is tiny JSON; anything larger is hostile or corrupt.
_MAX_MANIFEST_BYTES = 1024 * 1024
_CAS_KEY_RE = re.compile(r"[0-9a-f]{64}")


def _length_prefixed(tag: bytes, payload: bytes) -> bytes:
    """``tag || len(payload) as u64 || payload`` — length-bound, unambiguous."""
    return tag + len(payload).to_bytes(8, "big") + payload


def validate_cas_key(cas_key: str) -> str:
    """Fail closed unless ``cas_key`` is exactly 64 lowercase hex characters.

    The strict check is what makes :meth:`SegmentCASPlanner.artifact_path` safe:
    a 64-character string containing ``/`` or ``..`` is refused before it can
    ever reach the filesystem.
    """
    if not isinstance(cas_key, str) or _CAS_KEY_RE.fullmatch(cas_key) is None:
        raise SegmentIntegrityError(f"cas_key must be 64 lowercase hex chars, got {cas_key!r}")
    return cas_key


@dataclass(frozen=True)
class TimelineSegmentSpec:
    """One deterministic slice of a timeline (frames are half-open ``[s, e)``).

    The five ``*_digest`` fields are opaque caller-supplied digests of the
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


def canonical_spec_bytes(
    spec: TimelineSegmentSpec,
    *,
    pipeline_digest: str = "",
    mux_mode: str = "",
    renderer_version: str = RENDERER_VERSION,
) -> bytes:
    """Deterministic, length-prefixed, type-tagged encoding of a segment spec.

    ``pipeline_digest`` is the render-pipeline fingerprint; folding it in means a
    change to any pipeline knob (brightness, dissolve, trim, …) changes the key
    even when the timeline spec itself is untouched.  ``mux_mode`` is the engine
    that produced (or will produce) the bytes and ``renderer_version`` the
    renderer identity, so a segment rendered by the CPU mux and one rendered by
    the FFmpeg pipe can never share a cache entry, and neither can be reused
    across an incompatible renderer.
    """
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
        _length_prefixed(b"pipe", pipeline_digest.encode("utf-8")),
        _length_prefixed(b"mux", mux_mode.encode("utf-8")),
        _length_prefixed(b"rdr", renderer_version.encode("utf-8")),
    )
    return _SPEC_DOMAIN + b"".join(parts)


def segment_cas_key(
    spec: TimelineSegmentSpec,
    *,
    hmac_key: bytes | None = None,
    pipeline_digest: str = "",
    mux_mode: str = "",
    renderer_version: str = RENDERER_VERSION,
) -> str:
    """Content-addressed key for ``spec`` (HMAC-SHA256 when a key is supplied)."""
    data = canonical_spec_bytes(
        spec,
        pipeline_digest=pipeline_digest,
        mux_mode=mux_mode,
        renderer_version=renderer_version,
    )
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
    pipeline_digest: str = ""
    content_sha: str = ""
    body_size: int = 0
    mode: str = ""


@dataclass(frozen=True)
class VerifiedSegment:
    """Metadata proven by a streaming verification (no body retained)."""

    cas_key: str
    content_sha: str
    body_size: int
    mode: str = ""


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


def _parse_artifact_header(header: bytes) -> tuple[str, int, bytes]:
    """Decode ``magic || key || content_len || content_sha`` from a header block."""
    if len(header) < _HEADER_LEN:
        raise SegmentIntegrityError(f"artifact header truncated: {len(header)} bytes")
    if header[: len(_ARTIFACT_MAGIC)] != _ARTIFACT_MAGIC:
        raise SegmentIntegrityError("artifact magic mismatch")
    key = header[len(_ARTIFACT_MAGIC) : len(_ARTIFACT_MAGIC) + _KEY_HEX_LEN].decode("ascii")
    (content_len,) = struct.unpack(">Q", header[_CONTENT_SHA_OFFSET - 8 : _CONTENT_SHA_OFFSET])
    content_sha = header[_CONTENT_SHA_OFFSET:_HEADER_LEN]
    return key, content_len, content_sha


def _decode_artifact(raw: bytes) -> tuple[str, bytes]:
    """Decode a complete in-memory artifact (magic/key/length/hash + body)."""
    key, content_len, content_sha = _parse_artifact_header(raw)
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
    def cas_key(
        self,
        spec: TimelineSegmentSpec,
        *,
        pipeline_digest: str = "",
        mux_mode: str = "",
    ) -> str:
        return segment_cas_key(
            spec,
            hmac_key=self._hmac_key,
            pipeline_digest=pipeline_digest,
            mux_mode=mux_mode,
        )

    def _safe_cache_path(self, name: str) -> Path:
        path = self.cache_dir / name
        if not path.resolve().is_relative_to(self.cache_dir.resolve()):
            raise SegmentIntegrityError(f"cache path escapes cache_dir: {name!r}")
        return path

    def artifact_path(self, cas_key: str) -> Path:
        validate_cas_key(cas_key)
        return self._safe_cache_path(f"{cas_key}.nagarseg")

    def manifest_path(self, cas_key: str) -> Path:
        validate_cas_key(cas_key)
        return self._safe_cache_path(f"{cas_key}.manifest.json")

    # -- planning -----------------------------------------------------------
    def plan(
        self,
        timeline: Sequence[TimelineSegmentSpec],
        *,
        pipeline_digests: Sequence[str] | None = None,
        mux_modes: Sequence[str] | None = None,
    ) -> SegmentPlan:
        if pipeline_digests is not None and len(pipeline_digests) != len(timeline):
            raise HyperCompilerError("pipeline_digests length must match the timeline")
        if mux_modes is not None and len(mux_modes) != len(timeline):
            raise HyperCompilerError("mux_modes length must match the timeline")
        planned: list[PlannedSegment] = []
        keys: list[str] = []
        for index, spec in enumerate(timeline):
            digest = "" if pipeline_digests is None else pipeline_digests[index]
            mode = "" if mux_modes is None else mux_modes[index]
            key = self.cas_key(spec, pipeline_digest=digest, mux_mode=mode)
            keys.append(key)
            path = self.artifact_path(key)
            try:
                # Bounded, streaming verification: a whole segment is never
                # materialised just to decide cache-hit vs re-render.
                verified = self.verify_segment_artifact(key)
            except HyperCompilerError:
                status = SegmentStatus.RENDER_REQUIRED
                content_sha = ""
                body_size = 0
                verified_mode = ""
            else:
                status = SegmentStatus.CACHE_HIT
                content_sha = verified.content_sha
                body_size = verified.body_size
                verified_mode = verified.mode
            planned.append(
                PlannedSegment(
                    spec,
                    key,
                    status,
                    path,
                    digest,
                    content_sha=content_sha,
                    body_size=body_size,
                    mode=verified_mode or mode,
                )
            )
        cache_hits = sum(1 for seg in planned if seg.status is SegmentStatus.CACHE_HIT)
        return SegmentPlan(
            segments=tuple(planned),
            merkle_root=timeline_merkle_root(keys),
            total_leaves=len(planned),
            cache_hits=cache_hits,
            render_required=len(planned) - cache_hits,
        )

    # -- manifests ----------------------------------------------------------
    def _write_manifest(
        self,
        cas_key: str,
        *,
        content_sha: str,
        file_sha: str,
        size: int,
        mode: str,
    ) -> None:
        manifest: dict[str, object] = {
            "cas_key": cas_key,
            "content_sha256": content_sha,
            "file_sha256": file_sha,
            "size": size,
            "mode": mode,
            "renderer_version": RENDERER_VERSION,
        }
        if self._hmac_key is not None:
            sealed = json.dumps(manifest, sort_keys=True).encode("utf-8")
            manifest["manifest_hmac"] = hmac.new(self._hmac_key, sealed, hashlib.sha256).hexdigest()
        atomic_write_bytes(
            self.manifest_path(cas_key),
            json.dumps(manifest, sort_keys=True).encode("utf-8"),
        )

    def read_manifest(self, cas_key: str) -> dict[str, object]:
        """Load and validate the manifest (including its HMAC seal when keyed).

        The file size is checked *before* reading, so a hostile multi-gigabyte
        manifest cannot be pulled into memory.
        """
        manifest_file = self.manifest_path(cas_key)
        if not manifest_file.is_file():
            raise SegmentIntegrityError(f"segment manifest missing: {manifest_file}")
        try:
            manifest_size = os.stat(manifest_file).st_size
        except OSError as exc:
            raise SegmentIntegrityError(f"segment manifest unreadable: {exc}") from exc
        if manifest_size > _MAX_MANIFEST_BYTES:
            raise SegmentIntegrityError(
                f"segment manifest too large: {manifest_size} > {_MAX_MANIFEST_BYTES} bytes"
            )
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SegmentIntegrityError(f"segment manifest unreadable: {exc}") from exc
        if not isinstance(manifest, dict):
            raise SegmentIntegrityError("segment manifest is not an object")
        if self._hmac_key is not None:
            tag = manifest.pop("manifest_hmac", None)
            if not isinstance(tag, str):
                raise SegmentIntegrityError("segment manifest seal missing")
            sealed = json.dumps(manifest, sort_keys=True).encode("utf-8")
            expected = hmac.new(self._hmac_key, sealed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(tag, expected):
                raise SegmentIntegrityError("segment manifest seal mismatch")
        return manifest

    # -- artifact lifecycle -------------------------------------------------
    def write_segment_artifact(self, cas_key: str, payload: bytes, *, mode: str = "") -> Path:
        """Atomically persist ``payload`` under ``cas_key`` plus its manifest."""
        path = self.artifact_path(cas_key)
        raw = _encode_artifact(cas_key, payload)
        atomic_write_bytes(path, raw)
        self._write_manifest(
            cas_key,
            content_sha=hashlib.sha256(payload).hexdigest(),
            file_sha=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
            mode=mode,
        )
        return path

    def commit_segment(self, spec: TimelineSegmentSpec, payload: bytes) -> Path:
        """Render-and-cache a single segment; returns the artifact path."""
        return self.write_segment_artifact(self.cas_key(spec), payload)

    def commit_stream(
        self,
        cas_key: str,
        source: BinaryIO,
        *,
        size: int,
        digest: str,
        mode: str = "",
    ) -> Path:
        """Atomically persist a payload streamed from ``source`` (bounded RAM).

        ``size``/``digest`` describe the body (its length and SHA-256); the body
        is copied in fixed-size chunks, so the artifact never has to exist in
        memory as a whole.
        """
        validate_cas_key(cas_key)
        if size < 0:
            raise SegmentIntegrityError(f"payload size must be >= 0, got {size}")
        try:
            expected_sha = bytes.fromhex(digest)
        except ValueError as exc:
            raise SegmentIntegrityError(f"payload digest is not hex: {digest!r}") from exc
        if len(expected_sha) != 32:
            raise SegmentIntegrityError("payload digest must be a 32-byte SHA-256")
        path = self.artifact_path(cas_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        tmp_path = Path(tmp_name)
        header = _ARTIFACT_MAGIC + cas_key.encode("ascii") + struct.pack(">Q", size) + expected_sha
        file_hasher = hashlib.sha256()
        body_hasher = hashlib.sha256()
        body_len = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(header)
                file_hasher.update(header)
                while True:
                    chunk = source.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    handle.write(chunk)
                    file_hasher.update(chunk)
                    body_hasher.update(chunk)
                    body_len += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if body_len != size:
                raise SegmentIntegrityError(f"streamed {body_len} bytes, expected {size}")
            if body_hasher.digest() != expected_sha:
                raise SegmentIntegrityError("streamed payload hash mismatch")
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        self._write_manifest(
            cas_key,
            content_sha=digest,
            file_sha=file_hasher.hexdigest(),
            size=SEGMENT_HEADER_LEN + body_len,
            mode=mode,
        )
        return path

    def verify_segment_artifact(self, cas_key: str, path: Path | None = None) -> VerifiedSegment:
        """Verify the artifact and return its proven metadata (never its body).

        The *content* hash (recomputed over the body) and the *on-disk file* hash
        (recomputed over the whole artifact and compared to the manifest) are
        both checked while streaming in fixed-size chunks, so verifying a large
        artifact never materialises it in RAM.  Fail-closed: any mismatch raises
        :class:`SegmentIntegrityError`.
        """
        handle, verified = self._open_verified(cas_key, path)
        handle.close()
        return verified

    def read_segment_artifact(self, cas_key: str, path: Path | None = None) -> bytes:
        """Return the verified payload of ``cas_key`` (fail-closed)."""
        handle, _ = self._open_verified(cas_key, path)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = handle.read(_CHUNK_BYTES)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            handle.close()
        return b"".join(chunks)

    def stream_segment_body(self, cas_key: str, sink: ByteSink) -> VerifiedSegment:
        """Verify ``cas_key`` and copy its body to ``sink`` in fixed-size chunks.

        The body is never held in memory as a whole, so assembling a final
        timeline from many cached segments stays bounded regardless of length.
        """
        handle, verified = self._open_verified(cas_key, None)
        try:
            while True:
                chunk = handle.read(_CHUNK_BYTES)
                if not chunk:
                    break
                sink.write(chunk)
        finally:
            handle.close()
        return verified

    def _open_verified(self, cas_key: str, path: Path | None) -> tuple[BinaryIO, VerifiedSegment]:
        """Verify ``cas_key`` fully, then return an open handle positioned at the body.

        An explicit ``path`` must still resolve inside ``cache_dir`` — the same
        containment rule as every derived path, so a caller cannot be tricked
        into reading or trusting a file outside the store.
        """
        validated = validate_cas_key(cas_key)
        if path is not None:
            artifact = Path(path)
            if not artifact.resolve().is_relative_to(self.cache_dir.resolve()):
                raise SegmentIntegrityError(f"artifact path escapes cache_dir: {artifact}")
        else:
            artifact = self.artifact_path(validated)
        if not artifact.is_file():
            raise SegmentIntegrityError(f"segment artifact missing: {artifact}")
        manifest = self.read_manifest(validated)
        if manifest.get("renderer_version") not in (None, RENDERER_VERSION):
            raise SegmentIntegrityError(
                f"segment rendered by an incompatible renderer: "
                f"{manifest.get('renderer_version')!r}"
            )
        stored_mode = manifest.get("mode", "")
        handle = artifact.open("rb")
        try:
            header = handle.read(SEGMENT_HEADER_LEN)
            stored_key, content_len, content_sha = _parse_artifact_header(header)
            file_hasher = hashlib.sha256()
            file_hasher.update(header)
            body_hasher = hashlib.sha256()
            body_len = 0
            while True:
                chunk = handle.read(_CHUNK_BYTES)
                if not chunk:
                    break
                file_hasher.update(chunk)
                body_hasher.update(chunk)
                body_len += len(chunk)
            if stored_key != validated:
                raise SegmentIntegrityError(f"artifact key mismatch: {stored_key} != {validated}")
            if content_len != body_len:
                raise SegmentIntegrityError(
                    f"artifact length mismatch: header={content_len} body={body_len}"
                )
            if body_hasher.digest() != content_sha:
                raise SegmentIntegrityError("artifact content hash mismatch")
            if manifest.get("file_sha256") != file_hasher.hexdigest():
                raise SegmentIntegrityError("segment file hash mismatch on disk")
            if manifest.get("size") != SEGMENT_HEADER_LEN + body_len:
                raise SegmentIntegrityError("segment file size mismatch on disk")
            if manifest.get("cas_key") != validated:
                raise SegmentIntegrityError("manifest key mismatch")
            if manifest.get("content_sha256") != content_sha.hex():
                raise SegmentIntegrityError("segment content hash mismatch on disk")
            handle.seek(SEGMENT_HEADER_LEN)
        except BaseException:
            handle.close()
            raise
        verified = VerifiedSegment(
            cas_key=validated,
            content_sha=content_sha.hex(),
            body_size=body_len,
            mode=stored_mode if isinstance(stored_mode, str) else "",
        )
        return handle, verified


__all__ = [
    "RENDERER_VERSION",
    "SEGMENT_HEADER_LEN",
    "PlannedSegment",
    "SegmentCASPlanner",
    "SegmentPlan",
    "SegmentStatus",
    "TimelineSegmentSpec",
    "VerifiedSegment",
    "atomic_write_bytes",
    "canonical_spec_bytes",
    "merkle_root",
    "merkle_root_hex",
    "segment_cas_key",
    "timeline_merkle_root",
    "validate_cas_key",
]
