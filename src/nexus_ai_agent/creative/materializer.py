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

Asset Merkle root: RFC 6962 section 2.1, with domain-separated SHA-256
leaves and internal nodes. The previous level-by-level promotion algorithm has
the same root. ``total_leaves`` is recorded next to the root.

Shot Merkle root and per-shot inclusion proofs: the asset tree above commits to
*assets* (one PNG and one WAV per shot). A second, per-shot tree commits to each
shot's A/V material as one leaf: ``shot_content_hash`` = SHA-256 of the
canonical shot record, and the RFC 6962 leaf payload is that hash's ASCII hex.
For every shot an inclusion proof (:func:`merkle_inclusion_proof_record`) is
produced and carried in :class:`MaterializedBundle` and, sealed, in the artifact
passport. :func:`verify_shot_inclusion_proofs` re-derives the root from the
declared shot hashes and verifies every audit path fail-closed, returning typed
violation codes instead of trusting the stored proofs.
"""

from __future__ import annotations

import hashlib
import hmac
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
#: Schema tag for the canonical per-shot content record that seeds a shot leaf.
SHOT_CONTENT_SCHEMA = "nagar-shot-content-v1"
#: Schema tag carried by every serialised inclusion proof.
SHOT_PROOF_SCHEMA = "nagar-merkle-inclusion-proof-v1"
#: RFC 6962 Merkle Tree Hash of the empty tree (``SHA-256("")``).
EMPTY_MERKLE_ROOT = hashlib.sha256(b"").hexdigest()
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


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
    #: One SHA-256 per shot, over the canonical shot content record (A/V material).
    shot_hashes: tuple[str, ...] = ()
    #: RFC 6962 root of the per-shot tree (``EMPTY_MERKLE_ROOT`` when there are no shots).
    shot_merkle_root: str = ""
    #: RFC 6962 audit path for every shot, in shot order (fail-closed verified).
    shot_inclusion_proofs: tuple[dict[str, Any], ...] = ()

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

    ``O_NOFOLLOW`` refuses a final symlink. ``O_NONBLOCK`` prevents a FIFO
    substituted before open from blocking before the descriptor is checked.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
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


def _merkle_hash(leaves: Sequence[bytes], start: int, end: int) -> bytes:
    n = end - start
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return _rfc6962_leaf(leaves[start])
    k = 1 << ((n - 1).bit_length() - 1)
    return _rfc6962_node(
        _merkle_hash(leaves, start, start + k), _merkle_hash(leaves, start + k, end)
    )


def merkle_root(leaf_payloads: Sequence[bytes]) -> tuple[str, int]:
    """Return the RFC 6962 root and leaf count (preserves the existing API)."""
    return _merkle_hash(leaf_payloads, 0, len(leaf_payloads)).hex(), len(leaf_payloads)


def merkle_inclusion_proof(leaves: Sequence[bytes], index: int) -> tuple[str, ...]:
    """Return the bottom-up RFC 6962 audit path for a leaf."""
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(leaves):
        raise ValueError("leaf index outside tree")

    def path(start: int, end: int, i: int) -> list[bytes]:
        n = end - start
        if n == 1:
            return []
        k = 1 << ((n - 1).bit_length() - 1)
        if i < k:
            return path(start, start + k, i) + [_merkle_hash(leaves, start + k, end)]
        return path(start + k, end, i - k) + [_merkle_hash(leaves, start, start + k)]

    return tuple(h.hex() for h in path(0, len(leaves), index))


def verify_merkle_inclusion_proof(
    leaf: bytes, *, index: int, total_leaves: int, proof: Sequence[str], expected_root: str
) -> bool:
    """Verify a tree-size-bound audit path; reject malformed or surplus hashes."""
    if (
        not isinstance(leaf, bytes)
        or isinstance(index, bool)
        or not isinstance(index, int)
        or isinstance(total_leaves, bool)
        or not isinstance(total_leaves, int)
        or total_leaves <= 0
        or index < 0
        or index >= total_leaves
        or not isinstance(expected_root, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_root)
        or not isinstance(proof, (list, tuple))
    ):
        return False
    # Construct directions from the authenticated tree size, then consume bottom-up.
    directions: list[bool] = []
    i, n = index, total_leaves
    while n > 1:
        k = 1 << ((n - 1).bit_length() - 1)
        if i < k:
            directions.append(False)  # sibling on right
            n = k
        else:
            directions.append(True)  # sibling on left
            i -= k
            n -= k
    if len(proof) != len(directions):
        return False
    result = _rfc6962_leaf(leaf)
    for sibling, on_left in zip(proof, reversed(directions), strict=True):
        if not isinstance(sibling, str) or not re.fullmatch(r"[0-9a-f]{64}", sibling):
            return False
        digest = bytes.fromhex(sibling)
        result = _rfc6962_node(digest, result) if on_left else _rfc6962_node(result, digest)
    return hmac.compare_digest(result.hex(), expected_root)


def merkle_root_sha256(leaf_payloads: Sequence[bytes]) -> str:
    """Return the RFC 6962 Merkle Tree Hash of ``leaf_payloads`` as lowercase hex.

    The empty tree hashes ``SHA-256("")``, a single leaf hashes ``0x00 || leaf``
    and internal nodes hash ``0x01 || left || right``; the odd tail is promoted,
    never duplicated. This is the value sealed in an artifact passport.
    """
    return _merkle_hash(leaf_payloads, 0, len(leaf_payloads)).hex()


def shot_content_hash(
    *,
    shot_id: str,
    image_sha256: str,
    wav_sha256: str,
    duration_s: float,
) -> str:
    """SHA-256 over the canonical per-shot content record (the shot's leaf value).

    Binding the shot id, both asset digests and the exact frame-quantized
    duration means any change to the shot's A/V material - or to its length -
    produces a different leaf, hence a different inclusion proof and root.
    """
    validate_safe_id(shot_id, "shot_id")
    for name, value in (("image_sha256", image_sha256), ("wav_sha256", wav_sha256)):
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            raise MaterializationError("invalid_digest", name)
    duration = _finite_shot_duration(duration_s)
    record = {
        "schema": SHOT_CONTENT_SCHEMA,
        "shot_id": shot_id,
        "image_sha256": image_sha256,
        "wav_sha256": wav_sha256,
        "duration_s": repr(float(duration)),
    }
    return hashlib.sha256(canonical_json(record)).hexdigest()


def shot_leaf_payloads(shot_hashes: Sequence[str]) -> list[bytes]:
    """Turn validated shot hashes into RFC 6962 leaf payloads (ASCII hex bytes).

    Fail-closed: a non-string, a wrong length or any character outside
    ``[0-9a-f]`` is refused before it can enter a tree.
    """
    payloads: list[bytes] = []
    for value in shot_hashes:
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            raise MaterializationError("invalid_digest", "shot_hashes must be lowercase hex64")
        payloads.append(value.encode("ascii"))
    return payloads


def merkle_inclusion_proof_record(leaf_payloads: Sequence[bytes], index: int) -> dict[str, Any]:
    """Serialise the RFC 6962 audit path of ``leaf_payloads[index]``.

    The returned mapping carries everything a verifier needs *and* nothing it
    can be tricked by: the claimed leaf index, the leaf value itself, the number
    of leaves the path was built for, the root it was built under, and the
    bottom-up sibling hashes. It is only ever trusted after
    :func:`verify_shot_inclusion_proofs` (or the passport verifier) re-derives
    the root from the declared leaves and replays the path.
    """
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(leaf_payloads):
        raise MaterializationError("invalid_leaf_index", repr(index))
    payload = leaf_payloads[index]
    if not isinstance(payload, bytes) or not _HEX64.fullmatch(payload.decode("ascii", "replace")):
        raise MaterializationError("invalid_leaf_payload", "leaf payload must be lowercase hex")
    return {
        "schema": SHOT_PROOF_SCHEMA,
        "leaf_index": index,
        "leaf_hash": payload.decode("ascii"),
        "total_leaves": len(leaf_payloads),
        "root_hash": merkle_root_sha256(leaf_payloads),
        "sibling_hashes": list(merkle_inclusion_proof(leaf_payloads, index)),
    }


def _proof_is_valid(
    proof: object,
    payloads: Sequence[bytes],
    shot_hashes: Sequence[str],
    index: int,
    merkle_root: str,
) -> bool:
    """Replay one audit path; every field must agree with the declared leaves."""
    if not isinstance(proof, Mapping):  # noqa: SIM102 - explicit, flat fail-closed checks
        return False
    if proof.get("leaf_index") != index:
        return False
    if proof.get("leaf_hash") != shot_hashes[index]:
        return False
    if proof.get("root_hash") != merkle_root:
        return False
    if proof.get("total_leaves") != len(shot_hashes):
        return False
    path = proof.get("sibling_hashes")
    if not isinstance(path, (list, tuple)):
        return False
    return verify_merkle_inclusion_proof(
        payloads[index],
        index=index,
        total_leaves=len(shot_hashes),
        proof=path,
        expected_root=merkle_root,
    )


def verify_shot_inclusion_proofs(
    *,
    shot_hashes: Sequence[str],
    merkle_root: str,
    proofs: Sequence[object],
) -> tuple[str, ...]:
    """Fail-closed verification of a whole per-shot tree.

    Returns an empty tuple when the tree and every audit path verify; otherwise
    the typed violation codes, in a deterministic order:

    * ``shot_merkle_root_mismatch`` - the root does not equal the RFC 6962 root
      recomputed from the declared shot hashes;
    * ``shot_inclusion_proof_count_mismatch`` - the number of proofs differs
      from the number of shots (only meaningful for a non-empty tree);
    * ``shot_inclusion_proof_invalid`` - a proof is not a mapping, disagrees
      with the declared leaf index/hash/root/leaf count, has a malformed path,
      or replays to a different root.
    """
    if not isinstance(merkle_root, str) or not _HEX64.fullmatch(merkle_root):
        return ("shot_merkle_root_mismatch",)
    try:
        payloads = shot_leaf_payloads(shot_hashes)
    except MaterializationError:
        return ("shot_inclusion_proof_invalid",)
    violations: list[str] = []
    recomputed = merkle_root_sha256(payloads)
    if not hmac.compare_digest(recomputed, merkle_root):
        violations.append("shot_merkle_root_mismatch")
    if payloads and len(proofs) != len(payloads):
        violations.append("shot_inclusion_proof_count_mismatch")
    if len(proofs) == len(payloads):
        for index, proof in enumerate(proofs):
            if not _proof_is_valid(proof, payloads, shot_hashes, index, merkle_root):
                violations.append("shot_inclusion_proof_invalid")
    return tuple(violations)


def shot_content_hashes_from_assets(assets: Sequence[AssetRecord]) -> tuple[str, ...]:
    """Re-derive the shot tree leaves from sealed asset records, in shot order.

    Used by :meth:`StoryboardMediaMaterializer.verify_materialized_provenance` so
    the shot root is proven to bind the very asset digests that were just
    re-hashed from disk (not merely a self-consistent list of hashes).
    """
    images: dict[str, str] = {}
    wavs: dict[str, tuple[str, float]] = {}
    order: list[str] = []
    for record in assets:
        if record.kind in ("slate_png", "local_image"):
            images[record.shot_id] = record.sha256
            order.append(record.shot_id)
        elif record.kind == "tone_wav":
            wavs[record.shot_id] = (record.sha256, record.duration_s)
    hashes: list[str] = []
    for shot_id in order:
        wav_sha, duration = wavs[shot_id]
        hashes.append(
            shot_content_hash(
                shot_id=shot_id,
                image_sha256=images[shot_id],
                wav_sha256=wav_sha,
                duration_s=duration,
            )
        )
    return tuple(hashes)


DEFAULT_SAMPLE_RATE_HZ = 16_000


def planned_shot_durations(
    manifest: Any, sample_rate_hz: int = DEFAULT_SAMPLE_RATE_HZ
) -> tuple[float, ...]:  # noqa: ANN401
    """Compute the frame-quantized durations from the compiled editorial events."""
    if isinstance(sample_rate_hz, bool) or sample_rate_hz not in SUPPORTED_SAMPLE_RATES:
        raise MaterializationError("invalid_profile", "unsupported sample rate")
    return tuple(
        round(_finite_shot_duration(event.video_duration_sec) * sample_rate_hz) / sample_rate_hz
        for event in manifest.editorial.events
    )


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
        shot_hashes: list[str] = []
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
            # Per-shot leaf: one hash over this shot's image, tone and duration.
            shot_hashes.append(
                shot_content_hash(
                    shot_id=shot_id,
                    image_sha256=image_sha,
                    wav_sha256=wav_sha,
                    duration_s=actual_duration,
                )
            )

        manifest_path = target / MANIFEST_FILENAME
        manifest_bytes = manifest.to_canonical_json().encode("utf-8")

        def _write_manifest(handle: IO[bytes]) -> None:
            handle.write(manifest_bytes)

        atomic_write(manifest_path, _write_manifest)
        m_sha, m_size, m_inode = stream_sha256(manifest_path)

        root_hex, leaves = merkle_root([canonical_json(r.as_dict()) for r in records])
        shot_payloads = shot_leaf_payloads(shot_hashes)
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
            shot_hashes=tuple(shot_hashes),
            shot_merkle_root=merkle_root_sha256(shot_payloads),
            shot_inclusion_proofs=tuple(
                merkle_inclusion_proof_record(shot_payloads, index)
                for index in range(len(shot_payloads))
            ),
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
        self._verify_shot_seal(bundle)

    def _verify_shot_seal(self, bundle: MaterializedBundle) -> None:
        """Prove the per-shot tree binds the re-hashed assets and every audit path.

        Two independent claims are checked: the shot leaves are re-derived from
        the *sealed asset records* (so a swapped PNG/WAV or a changed duration
        changes a leaf), and every stored inclusion proof replays to the
        declared root. Either mismatch is a fail-closed provenance refusal.
        """
        if not bundle.shot_hashes:
            if bundle.shot_inclusion_proofs or bundle.shot_merkle_root:
                raise MaterializationError("provenance_violated", "shot proof without shots")
            return
        if shot_content_hashes_from_assets(bundle.assets) != bundle.shot_hashes:
            raise MaterializationError("provenance_violated", "shot content hash mismatch")
        violations = verify_shot_inclusion_proofs(
            shot_hashes=bundle.shot_hashes,
            merkle_root=bundle.shot_merkle_root,
            proofs=bundle.shot_inclusion_proofs,
        )
        if violations:
            raise MaterializationError(
                "provenance_violated", f"shot inclusion proof: {violations[0]}"
            )


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
    "DEFAULT_SAMPLE_RATE_HZ",
    "EMPTY_MERKLE_ROOT",
    "MANIFEST_FILENAME",
    "SHOT_CONTENT_SCHEMA",
    "SHOT_PROOF_SCHEMA",
    "AssetRecord",
    "MaterializationError",
    "MaterializedBundle",
    "RenderProfile",
    "StoryboardMediaMaterializer",
    "atomic_write",
    "canonical_json",
    "merkle_inclusion_proof",
    "merkle_inclusion_proof_record",
    "merkle_root",
    "merkle_root_sha256",
    "planned_shot_durations",
    "refuse_symlink_chain",
    "resolve_within",
    "shot_content_hash",
    "shot_content_hashes_from_assets",
    "shot_leaf_payloads",
    "shot_seed",
    "stream_sha256",
    "validate_safe_id",
    "verify_merkle_inclusion_proof",
    "verify_shot_inclusion_proofs",
    "write_slate_png",
    "write_tone_wav",
]
