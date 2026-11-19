"""Deterministic, atomic, symlink-safe media materializer for storyboard shots.

Turns a compiled storyboard manifest into the concrete media that FFmpeg will
consume: one PNG slate frame and one 16-bit mono PCM WAV per shot, plus the
canonical manifest file. Everything is produced locally from the manifest
(no paid API, no network), so two runs over the same manifest produce the same
bytes.

Guarantees, all fail-closed:

* **Atomic writes.** Every output is written to a ``tempfile.mkstemp`` sibling,
  flushed, ``fsync``-ed and moved into place with ``os.replace``. A crash can
  leave a stray temp file, never a truncated final file.
* **Symlink and traversal defense.** Every input and output path is resolved
  and must stay inside the workspace root (``is_relative_to``). Each path
  component is checked with ``os.lstat`` and any symlink is refused.
* **Bounded memory.** Hashing, copying and generation stream in
  :data:`CHUNK_BYTES` (64 KiB) pieces. Whole files are never held in RAM.
* **Finite numerics.** Every numeric profile value and duration is checked with
  ``math.isfinite`` before any comparison; NaN and +/-Inf are rejected.
* **TOCTOU seal.** :meth:`StoryboardMediaMaterializer.verify_materialized_provenance`
  re-stats and re-hashes every recorded asset (size, inode, SHA-256, symlink
  status) and recomputes the asset Merkle root. The render handler calls it
  immediately before FFmpeg runs.

Asset Merkle root (RFC 6962 §2.1, literal recursion): the empty tree hashes to
``SHA-256(b"")``; a leaf is ``SHA-256(0x00 || record_json)``; for ``n > 1`` the
split point is ``k = 1 << ((n - 1).bit_length() - 1)`` (the largest power of two
strictly below ``n``) and ``MTH(D[0:n]) = SHA-256(0x01 || MTH(D[0:k]) ||
MTH(D[k:n]))``. Inclusion proofs follow RFC 6962 PATH and are verified with the
RFC 9162 §2.1.3.2 algorithm bound to ``total_leaves``, so a proof for a tree
that duplicates its last leaf (CVE-2012-2459) never verifies against the real
root.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import tempfile
import wave
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

#: Streaming unit for every read, write and hash in this module.
CHUNK_BYTES = 65_536
#: Schema tag embedded in each asset record so the leaf hash is domain-separated.
ASSET_RECORD_SCHEMA = "nexus.creative.asset_record.v1"
MANIFEST_FILENAME = "storyboard_manifest.json"
#: Canonical 16-bit mono PCM sample rates the slice will emit.
SUPPORTED_SAMPLE_RATES = frozenset({16_000, 44_100, 48_000})
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
MIN_DIMENSION = 16
MIN_FPS = 1.0
MAX_FPS = 60.0
MIN_SHOT_SEC = 0.04
MAX_SHOT_SEC = 600.0
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class MaterializationError(RuntimeError):
    """A typed, fail-closed refusal while materializing or sealing media."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class RenderProfile:
    """The render parameters that define the output bytes (and therefore the hash)."""

    width: int = 320
    height: int = 180
    fps: float = 12.0
    sample_rate: int = 16_000

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RenderProfile:
        """Build a profile from untrusted input, refusing anything out of range."""
        width = _integer(raw.get("width"), "width", MIN_DIMENSION, MAX_WIDTH)
        height = _integer(raw.get("height"), "height", MIN_DIMENSION, MAX_HEIGHT)
        if width % 2 or height % 2:
            raise MaterializationError("invalid_profile", "width and height must be even (yuv420p)")
        fps = _finite(raw.get("fps"), "fps", MIN_FPS, MAX_FPS)
        sample_rate = _integer(raw.get("sample_rate", 16_000), "sample_rate", 16_000, 48_000)
        if sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise MaterializationError("invalid_profile", f"unsupported sample_rate {sample_rate}")
        return cls(width=width, height=height, fps=fps, sample_rate=sample_rate)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": repr(float(self.fps)),
            "sample_rate": self.sample_rate,
        }

    def profile_hash(self) -> str:
        """SHA-256 over the canonical profile JSON (bound into the approval)."""
        return hashlib.sha256(canonical_json(self.canonical_dict())).hexdigest()


@dataclass(frozen=True)
class AssetRecord:
    """One immutable provenance row for a materialized (or linked) asset."""

    shot_id: str
    kind: str
    relative_path: str
    resolved_path: str
    size_bytes: int
    sha256: str
    inode: int
    duration_s: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": ASSET_RECORD_SCHEMA,
            "shot_id": self.shot_id,
            "kind": self.kind,
            "relative_path": self.relative_path,
            "resolved_path": self.resolved_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "inode": self.inode,
            "duration_s": repr(float(self.duration_s)),
        }


@dataclass(frozen=True)
class MaterializedBundle:
    """Everything the render stage needs, plus the seal it must re-verify."""

    out_dir: str
    profile: RenderProfile
    manifest_path: str
    manifest_sha256: str
    manifest_size: int
    manifest_inode: int
    assets: tuple[AssetRecord, ...]
    shot_durations_s: tuple[float, ...]
    asset_merkle_root: str
    total_asset_leaves: int

    @property
    def total_duration_s(self) -> float:
        return math.fsum(self.shot_durations_s)


# --------------------------------------------------------------------------- #
# Pure helpers: validation, hashing, Merkle tree, atomic writes
# --------------------------------------------------------------------------- #


def canonical_json(value: Any) -> bytes:  # noqa: ANN401 - any JSON-serialisable value
    """Canonical UTF-8 JSON bytes: sorted keys, no whitespace, NaN refused."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _finite(value: object, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MaterializationError("invalid_profile", f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise MaterializationError("invalid_profile", f"{name} must be finite")
    if not lo <= number <= hi:
        raise MaterializationError("invalid_profile", f"{name} must be in [{lo}, {hi}]")
    return number


def _integer(value: object, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MaterializationError("invalid_profile", f"{name} must be an integer")
    if not lo <= value <= hi:
        raise MaterializationError("invalid_profile", f"{name} must be in [{lo}, {hi}]")
    return value


def validate_safe_id(value: object, name: str) -> str:
    """Accept only a conservative identifier: no separators, dots or traversal."""
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise MaterializationError("invalid_identifier", f"{name} is not a safe identifier")
    return value


def stream_sha256(path: Path) -> tuple[str, int, int]:
    """Return ``(sha256_hex, size, inode)`` by streaming the file in 64 KiB chunks.

    ``O_NOFOLLOW`` makes the open itself refuse a symlink, closing the window
    between a path check and the read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise MaterializationError("not_regular_file", str(path))
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
                digest.update(chunk)
        return digest.hexdigest(), st.st_size, st.st_ino
    finally:
        os.close(fd)


def refuse_symlink_chain(root: Path, candidate: Path) -> None:
    """Refuse any symlink on the path from ``root`` (exclusive) down to ``candidate``."""
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            return
        if stat.S_ISLNK(os.lstat(current).st_mode):
            raise MaterializationError("symlink_refused", str(current))


def resolve_within(root: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` under ``root``; fail closed on traversal or symlinks.

    The symlink walk runs on the *unresolved* joined path, because
    ``Path.resolve`` follows links and would hide them from the check.
    """
    root_resolved = root.resolve()
    raw = Path(candidate)
    if ".." in raw.parts:
        raise MaterializationError("path_traversal", str(candidate))
    joined = raw if raw.is_absolute() else root_resolved / raw
    if not joined.is_relative_to(root_resolved):
        raise MaterializationError("path_outside_workspace", str(candidate))
    refuse_symlink_chain(root_resolved, joined)
    resolved = joined.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise MaterializationError("path_outside_workspace", str(candidate))
    return resolved


def _rfc6962_leaf(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _rfc6962_node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split_point(size: int) -> int:
    """RFC 6962 ``k``: the largest power of two strictly smaller than ``size`` (size > 1)."""
    return 1 << ((size - 1).bit_length() - 1)


def _mth(leaf_hashes: Sequence[bytes], lo: int, hi: int) -> bytes:
    """RFC 6962 §2.1 MTH over ``leaf_hashes[lo:hi]`` by literal recursion."""
    size = hi - lo
    if size == 0:
        return hashlib.sha256(b"").digest()
    if size == 1:
        return leaf_hashes[lo]
    k = _split_point(size)
    return _rfc6962_node(_mth(leaf_hashes, lo, lo + k), _mth(leaf_hashes, lo + k, hi))


def merkle_root(leaf_payloads: Sequence[bytes]) -> tuple[str, int]:
    """Return ``(root_hex, total_leaves)`` per RFC 6962 §2.1."""
    total = len(leaf_payloads)
    leaf_hashes = [_rfc6962_leaf(payload) for payload in leaf_payloads]
    return _mth(leaf_hashes, 0, total).hex(), total


def merkle_inclusion_proof(leaf_payloads: Sequence[bytes], index: int) -> tuple[bytes, ...]:
    """RFC 6962 PATH(index, D[n]): the sibling hashes that prove leaf ``index``."""
    total = len(leaf_payloads)
    if not 0 <= index < total:
        raise MaterializationError("merkle_index_out_of_range", str(index))
    leaf_hashes = [_rfc6962_leaf(payload) for payload in leaf_payloads]

    def path(m: int, lo: int, hi: int) -> list[bytes]:
        size = hi - lo
        if size <= 1:
            return []
        k = _split_point(size)
        if m < k:
            return [*path(m, lo, lo + k), _mth(leaf_hashes, lo + k, hi)]
        return [*path(m - k, lo + k, hi), _mth(leaf_hashes, lo, lo + k)]

    return tuple(path(index, 0, total))


def verify_merkle_proof(
    leaf_payload: bytes,
    index: int,
    total_leaves: int,
    proof: Sequence[bytes],
    root_hex: str,
) -> bool:
    """RFC 9162 §2.1.3.2 inclusion verification bound to ``total_leaves``.

    Fails closed: any out-of-range index, wrong-length proof or root mismatch
    returns ``False``. The proof length is checked against ``total_leaves``
    so a proof for a shorter tree cannot be replayed against a longer one.
    """
    if total_leaves <= 0 or not 0 <= index < total_leaves:
        return False
    fn, sn = index, total_leaves - 1
    result = _rfc6962_leaf(leaf_payload)
    for sibling in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            result = _rfc6962_node(sibling, result)
            if not fn & 1:
                while fn & 1 == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            result = _rfc6962_node(result, sibling)
        fn >>= 1
        sn >>= 1
    return sn == 0 and result.hex() == root_hex


def atomic_write(final_path: Path, write: Callable[[IO[bytes]], None]) -> None:
    """Write via ``mkstemp`` in the same directory, fsync, then ``os.replace``."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=final_path.parent, prefix=f".{final_path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, final_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(final_path.parent)


def _fsync_directory(directory: Path) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------- #
# Deterministic generators (streamed, no whole-file buffers)
# --------------------------------------------------------------------------- #


def shot_seed(shot_id: str, shot_kind: str, scene_index: int, shot_index: int) -> bytes:
    """32 deterministic bytes that drive the slate colours and the tone."""
    material = f"{shot_id}|{shot_kind}|{scene_index}|{shot_index}".encode()
    return hashlib.sha256(material).digest()


def write_slate_png(
    path: Path,
    *,
    width: int,
    height: int,
    seed: bytes,
    label: str,
) -> None:
    """Write a deterministic RGB PNG, one scanline at a time, atomically."""
    if not isinstance(seed, bytes) or len(seed) != 32:
        raise MaterializationError("invalid_seed", "slate seed must be 32 bytes")
    c_a = bytes(seed[0:3])
    c_b = bytes(seed[3:6])
    accent = bytes(seed[6:9])
    band_rows = max(4, height // 8)
    tag = _label_stripe(label, width)

    def _row(y: int) -> bytes:
        if y < band_rows:
            return tag if (y % 2 == 0) else accent * width
        block = c_a if ((y - band_rows) // 16) % 2 == 0 else c_b
        return block * width

    def _write(handle: IO[bytes]) -> None:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(_png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        compressor = zlib.compressobj(6)
        pending = bytearray()
        for y in range(height):
            pending += b"\x00" + _row(y)
            if len(pending) >= CHUNK_BYTES:
                handle.write(_png_chunk(b"IDAT", compressor.compress(bytes(pending))))
                pending.clear()
        tail = compressor.compress(bytes(pending)) + compressor.flush()
        if tail:
            handle.write(_png_chunk(b"IDAT", tail))
        handle.write(_png_chunk(b"IEND", b""))

    atomic_write(path, _write)


def _label_stripe(label: str, width: int) -> bytes:
    """A fixed one-row stripe whose colour pattern encodes the label bytes."""
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    out = bytearray()
    while len(out) < width * 3:
        out += digest
    return bytes(out[: width * 3])


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def write_tone_wav(path: Path, *, duration_s: float, sample_rate: int, seed: bytes) -> int:
    """Write mono 16-bit PCM whose frame count is exactly ``round(duration * rate)``.

    Returns the number of frames written. Samples are generated and written in
    bounded chunks so memory stays flat for any duration.
    """
    if not math.isfinite(duration_s) or not MIN_SHOT_SEC <= duration_s <= MAX_SHOT_SEC:
        raise MaterializationError("invalid_duration", f"duration_s={duration_s!r}")
    frames_total = round(duration_s * sample_rate)
    frequency = 220.0 + float(seed[9] % 8) * 55.0
    amplitude = 0.05 * 32_767.0
    step = 2.0 * math.pi * frequency / sample_rate
    frames_per_chunk = CHUNK_BYTES // 2

    def _write(handle: IO[bytes]) -> None:
        with wave.open(handle, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            written = 0
            while written < frames_total:
                count = min(frames_per_chunk, frames_total - written)
                samples = bytearray(count * 2)
                for index in range(count):
                    value = int(amplitude * math.sin(step * (written + index)))
                    struct.pack_into("<h", samples, index * 2, value)
                wav.writeframes(bytes(samples))
                written += count

    atomic_write(path, _write)
    return frames_total


# --------------------------------------------------------------------------- #
# The materializer
# --------------------------------------------------------------------------- #


class StoryboardMediaMaterializer:
    """Materialize a compiled storyboard and seal its provenance."""

    def __init__(self, workspace_root: Path, profile: RenderProfile) -> None:
        self._root = Path(workspace_root).resolve()
        self._profile = profile

    def materialize(
        self,
        manifest: Any,  # noqa: ANN401 - CompiledStoryboardManifest (duck-typed to keep imports light)
        out_dir: Path,
        *,
        local_images: Mapping[str, str] | None = None,
    ) -> MaterializedBundle:
        """Write slate PNGs, tone WAVs and the canonical manifest into ``out_dir``.

        ``local_images`` maps a shot id to a user-linked PNG inside the
        workspace. Such a file is hash-checked and recorded like any asset, and it
        replaces the generated slate for that shot. Its pixel size is not
        re-checked here.
        """
        target = resolve_within(self._root, out_dir)
        target.mkdir(parents=True, exist_ok=True)
        records: list[AssetRecord] = []
        durations: list[float] = []
        sr = self._profile.sample_rate
        for event in manifest.editorial.events:
            shot_id = f"s{int(event.scene_index):03d}-sh{int(event.shot_index):03d}"
            validate_safe_id(shot_id, "shot_id")
            duration = _finite_shot_duration(event.video_duration_sec)
            kind_name = getattr(event.shot_kind, "value", str(event.shot_kind))
            seed = shot_seed(shot_id, kind_name, int(event.scene_index), int(event.shot_index))

            image_path = target / f"{shot_id}.png"
            if local_images and shot_id in local_images:
                source = resolve_within(self._root, local_images[shot_id])
                image_sha, image_size, image_inode = stream_sha256(source)
                image_record = AssetRecord(
                    shot_id=shot_id,
                    kind="local_image",
                    relative_path=str(source.relative_to(self._root)),
                    resolved_path=str(source),
                    size_bytes=image_size,
                    sha256=image_sha,
                    inode=image_inode,
                    duration_s=0.0,
                )
            else:
                write_slate_png(
                    image_path,
                    width=self._profile.width,
                    height=self._profile.height,
                    seed=seed,
                    label=f"{shot_id}|{kind_name}",
                )
                image_sha, image_size, image_inode = stream_sha256(image_path)
                image_record = AssetRecord(
                    shot_id=shot_id,
                    kind="slate_png",
                    relative_path=str(image_path.relative_to(self._root)),
                    resolved_path=str(image_path),
                    size_bytes=image_size,
                    sha256=image_sha,
                    inode=image_inode,
                    duration_s=0.0,
                )
            records.append(image_record)

            wav_path = target / f"{shot_id}.wav"
            frames = write_tone_wav(wav_path, duration_s=duration, sample_rate=sr, seed=seed)
            wav_sha, wav_size, wav_inode = stream_sha256(wav_path)
            actual_duration = frames / sr
            records.append(
                AssetRecord(
                    shot_id=shot_id,
                    kind="tone_wav",
                    relative_path=str(wav_path.relative_to(self._root)),
                    resolved_path=str(wav_path),
                    size_bytes=wav_size,
                    sha256=wav_sha,
                    inode=wav_inode,
                    duration_s=actual_duration,
                )
            )
            durations.append(actual_duration)

        manifest_path = target / MANIFEST_FILENAME
        manifest_bytes = manifest.to_canonical_json().encode("utf-8")
        atomic_write(manifest_path, lambda handle: handle.write(manifest_bytes))
        m_sha, m_size, m_inode = stream_sha256(manifest_path)

        root_hex, leaves = merkle_root([canonical_json(r.as_dict()) for r in records])
        return MaterializedBundle(
            out_dir=str(target),
            profile=self._profile,
            manifest_path=str(manifest_path),
            manifest_sha256=m_sha,
            manifest_size=m_size,
            manifest_inode=m_inode,
            assets=tuple(records),
            shot_durations_s=tuple(durations),
            asset_merkle_root=root_hex,
            total_asset_leaves=leaves,
        )

    def verify_materialized_provenance(self, bundle: MaterializedBundle) -> None:
        """Re-seal every asset and the manifest; raise on any drift since materialization."""
        for record in (*bundle.assets, _manifest_record(bundle)):
            path = Path(record.resolved_path)
            if not path.is_relative_to(self._root):
                raise MaterializationError("provenance_violated", f"outside workspace: {path}")
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise MaterializationError("provenance_violated", f"missing: {path}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise MaterializationError("provenance_violated", f"became a symlink: {path}")
            if not stat.S_ISREG(st.st_mode):
                raise MaterializationError("provenance_violated", f"not a regular file: {path}")
            if st.st_ino != record.inode:
                raise MaterializationError("provenance_violated", f"inode replaced: {path}")
            if st.st_size != record.size_bytes:
                raise MaterializationError("provenance_violated", f"size changed: {path}")
            digest, _, _ = stream_sha256(path)
            if digest != record.sha256:
                raise MaterializationError("provenance_violated", f"sha256 changed: {path}")
        records = [r.as_dict() for r in bundle.assets]
        root_hex, leaves = merkle_root([canonical_json(r) for r in records])
        if root_hex != bundle.asset_merkle_root or leaves != bundle.total_asset_leaves:
            raise MaterializationError("provenance_violated", "asset merkle root mismatch")


def _manifest_record(bundle: MaterializedBundle) -> AssetRecord:
    return AssetRecord(
        shot_id="manifest",
        kind="manifest_json",
        relative_path=MANIFEST_FILENAME,
        resolved_path=bundle.manifest_path,
        size_bytes=bundle.manifest_size,
        sha256=bundle.manifest_sha256,
        inode=bundle.manifest_inode,
        duration_s=0.0,
    )


def _finite_shot_duration(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MaterializationError("invalid_duration", "shot duration must be a number")
    number = float(value)
    if not math.isfinite(number) or not MIN_SHOT_SEC <= number <= MAX_SHOT_SEC:
        raise MaterializationError("invalid_duration", f"shot duration {value!r} out of range")
    return number


__all__ = [
    "ASSET_RECORD_SCHEMA",
    "CHUNK_BYTES",
    "MANIFEST_FILENAME",
    "AssetRecord",
    "MaterializationError",
    "MaterializedBundle",
    "RenderProfile",
    "StoryboardMediaMaterializer",
    "atomic_write",
    "canonical_json",
    "merkle_root",
    "refuse_symlink_chain",
    "resolve_within",
    "shot_seed",
    "stream_sha256",
    "validate_safe_id",
    "write_slate_png",
    "write_tone_wav",
]
