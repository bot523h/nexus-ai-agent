"""Type-4 studio pipeline: Storyboard -> SegmentCAS shot cache -> FFmpeg -> Passport -> Vault.

Three subsystems used to be islands (Type-1): the deterministic storyboard
compiler, the content-addressed segment store of the hyper-compiler, and the
encrypted personal cloud vault. This module wires them into one pipeline that a
CLI can drive end to end, and it never re-encodes work that has not changed:

1. :func:`nexus_ai_agent.creative.storyboard_compiler.compile_storyboard` compiles
   the screenplay into a sealed ``CompiledStoryboardManifest``; its canonical
   JSON is written beside the render.
2. Every shot is materialized deterministically (slate PNG + tone WAV) and gets a
   domain-separated cache key over its *content*: ``nagar-shot-cas-key-v1`` plus
   the canonical JSON of ``{shot id, shot content hash, duration, fps, size,
   image digest}``. That key maps, through an atomically written ``shot_index``
   entry, to a :class:`~nexus_ai_agent.creative.hyper_compiler.SegmentCASPlanner`
   artifact whose body is the shot's MP4 segment.
3. A cache hit streams the verified segment body out of the CAS (SHA-256 checked
   while it is copied, then re-hashed from disk) and never runs FFmpeg for that
   shot. A miss renders only that shot (``libx264``, ``yuv420p``, ``-r``, ``-s``),
   commits it to the CAS and then updates the index. The film is assembled with
   the concat demuxer and ``-c copy``, so assembly costs no re-encode.
4. The result is sealed twice: the materializer's asset and per-shot provenance
   (one RFC 6962 inclusion proof per shot) and an HMAC artifact passport that
   seals every per-shot inclusion proof as well.
5. :func:`sync_and_verify_studio_bundle_in_vault` pushes MP4 + manifest +
   passport into the encrypted BYOS vault, pulls the snapshot back into a clean
   directory, and re-verifies the passport and the restored bytes fail-closed.

Memory is bounded by construction: every copy, hash and JSON read goes through
64 KiB streaming buffers whose live total is metered, and the pipeline refuses
to continue once ``memory_ceiling_bytes`` would be crossed.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import os
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from nexus_ai_agent.creative.artifact_passport import (
    PASSPORT_SUFFIX,
    PassportCheck,
    build_passport,
    verify_artifact_passport,
    write_passport,
)
from nexus_ai_agent.creative.artifact_verifier import (
    PASS,
    Mp4Verdict,
    verify_mp4,
)
from nexus_ai_agent.creative.hyper_compiler import (
    SegmentCASPlanner,
    TimelineSegmentSpec,
)
from nexus_ai_agent.creative.materializer import (
    CHUNK_BYTES,
    AssetRecord,
    MaterializationError,
    MaterializedBundle,
    RenderProfile,
    StoryboardMediaMaterializer,
    atomic_write,
    canonical_json,
    merkle_inclusion_proof_record,
    merkle_root,
    merkle_root_sha256,
    planned_shot_durations,
    refuse_symlink_chain,
    shot_content_hash,
    shot_leaf_payloads,
    shot_seed,
    stream_sha256,
    write_slate_png,
    write_tone_wav,
)
from nexus_ai_agent.creative.storyboard_compiler import compile_storyboard
from nexus_ai_agent.storage.personal_cloud_vault import (
    LocalDeviceVaultDriver,
    PersonalCloudVaultManager,
    VaultSnapshotManifest,
)

#: Schema tag of the canonical per-shot segment cache key payload.
SHOT_SEGMENT_SCHEMA = "nagar-shot-segment-v1"
#: Domain separator prefixed to every shot cache key hash (no cross-protocol reuse).
SHOT_CACHE_KEY_DOMAIN = b"nagar-shot-cas-key-v1\x00"
#: Domain separator for the CAS manifest anchor key derived from the studio
#: signing secret (the CAS manifest HMAC never reuses the passport key itself).
CAS_ANCHOR_KEY_DOMAIN = b"nagar-studio-cas-anchor-v1\x00"
#: Sub-directory of the CAS root holding ``shot key -> MP4 content hash`` entries.
SHOT_INDEX_DIRNAME = "shot_index"
#: Renderer identity recorded in the CAS manifest (a foreign engine's bytes never reuse).
SEGMENT_MUX_MODE = "ffmpeg_subprocess_pipe"
#: Basename of the assembled film (distinct from the legacy ``storyboard.mp4``).
FILM_BASENAME = "render"
#: Default hard ceiling for bridge-owned live buffers: 120 MiB.
DEFAULT_MEMORY_CEILING_BYTES = 120 * 1024 * 1024
#: Container duration slack when verifying the assembled film.
DURATION_MARGIN_S = 0.05
#: Every concatenated segment may add one frame of slack.
FRAME_MARGIN_PER_SHOT = 1.0
MAX_INDEX_BYTES = 65_536
#: Upper bound for the append-only sync journal (a sync receipt per snapshot).
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
#: Mode used for every directory this module creates.
DIR_MODE = 0o700
#: ``os.open`` flags that prove a path really is a directory and not a link.
_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_CONCAT_CHARS = ("\n", "\r", "'", "\x00")


class StudioBridgeError(RuntimeError):
    """A typed, fail-closed refusal inside the Type-4 studio pipeline."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- #
# Bounded memory meter
# --------------------------------------------------------------------------- #


class _MemoryMeter:
    """Track the live bytes of every bridge-owned buffer and enforce the ceiling.

    ``charge`` runs before a buffer is allocated and ``release`` once it is
    dropped, so the peak is a real measurement of what this pipeline held in RAM
    rather than an RSS estimate that would also count the interpreter, NumPy and
    FFmpeg's own resident set.
    """

    __slots__ = ("_ceiling", "_live", "_peak")

    def __init__(self, ceiling_bytes: int) -> None:
        if isinstance(ceiling_bytes, bool) or not isinstance(ceiling_bytes, int):
            raise StudioBridgeError("invalid_memory_ceiling", repr(ceiling_bytes))
        if ceiling_bytes <= 0:
            raise StudioBridgeError("invalid_memory_ceiling", f"{ceiling_bytes} <= 0")
        self._ceiling = ceiling_bytes
        self._live = 0
        self._peak = 0

    @property
    def peak_bytes(self) -> int:
        return self._peak

    @property
    def live_bytes(self) -> int:
        return self._live

    def charge(self, size: int) -> None:
        """Reserve ``size`` bytes; raise as soon as the ceiling would be crossed."""
        if size < 0:
            raise StudioBridgeError("invalid_memory_charge", str(size))
        projected = self._live + size
        if projected > self._ceiling:
            raise StudioBridgeError(
                "memory_ceiling_exceeded", f"{projected} > ceiling {self._ceiling}"
            )
        self._live = projected
        self._peak = max(self._peak, projected)

    def release(self, size: int) -> None:
        self._live = max(0, self._live - max(0, size))


def _rusage_scale() -> int:
    """``ru_maxrss`` is reported in bytes on macOS and in KiB everywhere else."""
    return 1 if sys.platform == "darwin" else 1024


def _rusage_peak_rss_bytes(who: int) -> int:
    """One ``getrusage`` high-water mark (``who``), scaled to bytes."""
    return int(resource.getrusage(who).ru_maxrss) * _rusage_scale()


def _process_tree_rss_hwm() -> tuple[int, int, int]:
    """``(self, children, estimate)`` peak RSS of this process and its children, in bytes.

    ``RUSAGE_SELF`` and ``RUSAGE_CHILDREN`` are *independent* high-water marks,
    each remembering its own maximum over its own lifetime. Their sum is
    therefore an **estimate - an upper bound on what the tree may have held at
    once**, not a measurement of simultaneous resident memory: the child's peak
    and the parent's peak need not have occurred at the same instant. Both halves
    are reported separately so a reviewer can see that difference instead of
    being handed one opaque number.
    """
    own = _rusage_peak_rss_bytes(resource.RUSAGE_SELF)
    children = _rusage_peak_rss_bytes(resource.RUSAGE_CHILDREN)
    return own, children, own + children


def _current_process_tree_peak_rss_bytes() -> int:
    """Combined ``RUSAGE_SELF + RUSAGE_CHILDREN`` high-water-mark estimate, in bytes.

    Kept for compatibility with round-1 receipts. This value sums two independent
    peaks, so it is an upper bound on the tree's peak, *not* simultaneous RSS;
    callers that need both halves should use :func:`_process_tree_rss_hwm`.
    """
    return _process_tree_rss_hwm()[2]


# --------------------------------------------------------------------------- #
# Result records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CasStoryboardRenderResult:
    """Everything one incremental render produced, together with its proofs."""

    artifact: MaterializedBundle
    verification: Mp4Verdict
    passport: dict[str, Any]
    passport_path: Path
    manifest_path: Path
    cas_hits: int
    cas_misses: int
    shot_segment_hashes: tuple[str, ...]
    peak_memory_bytes: int
    output_mp4_path: Path
    receipt_path: Path
    #: Peak live bytes of the buffers *this pipeline* allocated (the meter's peak).
    bridge_buffer_peak_bytes: int = 0
    #: Peak RSS of this process alone (``RUSAGE_SELF``), in bytes.
    self_peak_rss_bytes: int = 0
    #: Peak RSS of any reaped child process (``RUSAGE_CHILDREN``), in bytes.
    children_max_rss_bytes: int = 0
    #: ``self + children``: an upper-bound *estimate* of the tree's peak, never
    #: a measurement of simultaneous resident memory.
    process_hwm_estimate_bytes: int = 0
    #: Compatibility alias for :attr:`process_hwm_estimate_bytes` (round-1 receipts).
    process_peak_rss_bytes: int = 0

    @property
    def output_dir(self) -> Path:
        return Path(self.artifact.out_dir)

    @property
    def shot_merkle_root(self) -> str:
        return self.artifact.shot_merkle_root


@dataclass(frozen=True)
class StudioVaultSyncReport:
    """The result of an encrypted vault round trip that re-verified every byte."""

    snapshot_seq: int
    manifest_id: str
    pushed_chunks: int
    deduplicated_chunks: int
    restored_files_count: int
    restored_mp4_path: Path
    restored_passport_path: Path
    restored_passport_verification: PassportCheck
    restored_artifact_verification: Mp4Verdict


# --------------------------------------------------------------------------- #
# Small, symlink-safe filesystem helpers
# --------------------------------------------------------------------------- #


def _open_directory(name: str, *, parent_fd: int | None = None, label: str = "") -> int:
    """Open ``name`` as a directory descriptor, refusing a symlink or non-directory.

    ``parent_fd`` resolves ``name`` *relative to that descriptor*, which is the
    only way to be sure a path component cannot be repointed between the check
    and the open: the kernel resolves it inside the inode we already hold.
    """
    try:
        if parent_fd is None:
            return os.open(Path(name), _DIR_OPEN_FLAGS)
        return os.open(name, _DIR_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raised_at = name if parent_fd is not None else str(name)
        raise StudioBridgeError(
            "not_a_directory", f"{label}: {raised_at} ({type(exc).__name__})"
        ) from exc


def _descend_or_create(parent_fd: int, name: str, *, label: str) -> int:
    """Create-or-open ``name`` **inside** ``parent_fd`` and return its descriptor.

    ``os.mkdir(..., dir_fd=parent_fd)`` creates the child relative to the
    directory descriptor, so a parent that is renamed or replaced by a symlink
    between two components cannot redirect the create: the descriptor still
    names the original inode. The child is then opened with ``O_NOFOLLOW``
    relative to the same descriptor, and only a genuine directory is returned.
    """
    try:
        os.mkdir(name, DIR_MODE, dir_fd=parent_fd)
    except FileExistsError:
        try:
            existing = os.lstat(name, dir_fd=parent_fd)
        except OSError as exc:  # vanished between the create attempt and the stat
            raise StudioBridgeError(
                "not_a_directory", f"{label}: {name} ({type(exc).__name__})"
            ) from exc
        if stat.S_ISLNK(existing.st_mode):
            raise StudioBridgeError("symlink_refused", f"{label}: {name}") from None
        if not stat.S_ISDIR(existing.st_mode):
            raise StudioBridgeError("not_a_directory", f"{label}: {name}") from None
    except OSError as exc:
        raise StudioBridgeError(
            "not_a_directory", f"{label}: {name} ({type(exc).__name__})"
        ) from exc
    child_fd = _open_directory(name, parent_fd=parent_fd, label=label)
    try:
        child = os.fstat(child_fd)
        if not stat.S_ISDIR(child.st_mode):
            raise StudioBridgeError("not_a_directory", f"{label}: {name}")
    except BaseException:
        os.close(child_fd)
        raise
    return child_fd


def _prepare_directory(root: Path, *, label: str) -> Path:
    """Create ``root`` (parents allowed) and prove it is a genuine directory.

    TOCTOU-safe by construction: the missing tail is created *component by
    component*, each component relative to the descriptor of the directory above
    it (``dir_fd=``), never by a multi-component path string. A parent that is
    renamed and replaced by a symlink mid-walk therefore cannot redirect the
    create into ``/outside`` - the descriptor still names the original inode -
    and the final ``(st_dev, st_ino)`` comparison against ``lstat`` of the
    requested path refuses the swap instead of silently following it.
    """
    candidate = Path(root)
    absolute = Path(os.path.abspath(candidate))
    anchor = Path(absolute.anchor or ".")
    try:
        refuse_symlink_chain(anchor, absolute)
    except MaterializationError as exc:
        raise StudioBridgeError("symlink_refused", f"{label}: {exc.detail}") from exc

    existing = absolute
    missing: list[str] = []
    while not existing.exists() and not existing.is_symlink():
        missing.append(existing.name)
        existing = existing.parent
    # The descriptor opened below is the proof: it names an inode, so neither a
    # rename nor a symlink swap of the path can redirect what happens next.
    dir_fd = _open_directory(str(existing), label=label)
    try:
        descriptor = os.fstat(dir_fd)
        path_stat = os.lstat(existing)
        if not stat.S_ISDIR(descriptor.st_mode):
            raise StudioBridgeError("not_a_directory", f"{label}: {existing}")
        if stat.S_ISLNK(path_stat.st_mode) or (descriptor.st_dev, descriptor.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            raise StudioBridgeError("directory_identity_mismatch", f"{label}: {existing}")
        for part in reversed(missing):
            child_fd = _descend_or_create(dir_fd, part, label=label)
            os.close(dir_fd)
            dir_fd = child_fd
        created = os.fstat(dir_fd)
        try:
            final_stat = os.lstat(absolute)
        except OSError as exc:  # the path no longer names what we created
            raise StudioBridgeError("directory_identity_mismatch", f"{label}: {absolute}") from exc
        if stat.S_ISLNK(final_stat.st_mode) or (created.st_dev, created.st_ino) != (
            final_stat.st_dev,
            final_stat.st_ino,
        ):
            raise StudioBridgeError("directory_identity_mismatch", f"{label}: {absolute}")
    finally:
        os.close(dir_fd)
    return absolute


def _safe_child(root: Path, name: str, *, label: str) -> Path:
    """Return ``root/name`` only when it stays inside ``root`` and is not a symlink."""
    if not name or "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
        raise StudioBridgeError("unsafe_path", f"{label}: {name!r}")
    child = Path(root) / name
    if child.is_symlink():
        raise StudioBridgeError("symlink_refused", f"{label}: {child}")
    # Containment is structural: ``name`` is a single validated component with no
    # separators, dots or NULs, so resolving the parent can never leave ``root``.
    return child.parent.resolve() / child.name


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    def _write(handle: IO[bytes]) -> None:
        handle.write(payload)

    atomic_write(path, _write)


def _read_bounded_bytes(
    path: Path, *, limit: int, meter: _MemoryMeter, reason: str = "invalid_json_size"
) -> bytes:
    """Read at most ``limit`` bytes from one ``O_NOFOLLOW`` descriptor.

    The size is taken from ``fstat`` on the descriptor that is then read, so a
    file swapped between the stat and the read cannot change what the bound is
    computed from - and the running total is checked again on every chunk, so a
    file that grows while it is being read is refused before it is buffered.
    The meter is charged for the bytes actually read and released in ``finally``.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise StudioBridgeError("not_regular_file", f"{path}: {type(exc).__name__}") from exc
    charged = 0
    chunks: list[bytes] = []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StudioBridgeError("not_regular_file", str(path))
        if st.st_size <= 0 or st.st_size > limit:
            raise StudioBridgeError(reason, f"{path}: {st.st_size}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1  # the file object owns the descriptor from here on
            while True:
                chunk = handle.read(CHUNK_BYTES)
                if not chunk:
                    break
                if sum(len(part) for part in chunks) + len(chunk) > limit:
                    raise StudioBridgeError(reason, f"{path}: grew past {limit} bytes")
                meter.charge(len(chunk))
                charged += len(chunk)
                chunks.append(chunk)
    finally:
        if fd >= 0:
            os.close(fd)
        meter.release(charged)
    return b"".join(chunks)


def _read_small_json(path: Path, *, limit: int, meter: _MemoryMeter) -> Any:  # noqa: ANN401
    """Read a small JSON document through a bounded, symlink-safe descriptor."""
    return json.loads(_read_bounded_bytes(path, limit=limit, meter=meter).decode("utf-8"))


def _copy_streaming(source: Path, target: Path, *, meter: _MemoryMeter) -> tuple[str, int]:
    """Copy ``source`` to ``target`` atomically in 64 KiB chunks; return (sha256, size)."""
    digest = hashlib.sha256()
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(source, read_flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StudioBridgeError("not_regular_file", str(source))
        target.parent.mkdir(parents=True, exist_ok=True)
        out_fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        written = 0
        try:
            with os.fdopen(fd, "rb", closefd=False) as src, os.fdopen(out_fd, "wb") as dst:
                while True:
                    meter.charge(CHUNK_BYTES)
                    try:
                        chunk = src.read(CHUNK_BYTES)
                    finally:
                        meter.release(CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    dst.write(chunk)
                    written += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return digest.hexdigest(), written
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# Shot cache keys and index
# --------------------------------------------------------------------------- #


def shot_cache_key_payload(
    *,
    shot_id: str,
    shot_content_hash: str,
    duration_ms: int,
    fps: float,
    width: int,
    height: int,
    image_sha256: str,
    toolchain_fingerprint: str = "",
) -> bytes:
    """Canonical JSON describing exactly the inputs a shot segment is made from.

    ``toolchain_fingerprint`` identifies the FFmpeg build that will encode the
    segment (see :func:`_ffmpeg_toolchain_fingerprint`). It is part of the key,
    so a different binary - an upgrade, a rebuild, a repackaged container - can
    never reuse segments encoded by the previous one.
    """
    return canonical_json(
        {
            "schema": SHOT_SEGMENT_SCHEMA,
            "shot_id": shot_id,
            "shot_content_hash": shot_content_hash,
            "duration_ms": duration_ms,
            "fps": repr(float(fps)),
            "width": width,
            "height": height,
            "image_sha256": image_sha256,
            "toolchain_fingerprint": toolchain_fingerprint,
        }
    )


def shot_cache_key_hash(**kwargs: Any) -> str:  # noqa: ANN401 - typed passthrough
    """``SHA-256(domain || canonical shot key payload)`` - the shot's cache address."""
    return hashlib.sha256(SHOT_CACHE_KEY_DOMAIN + shot_cache_key_payload(**kwargs)).hexdigest()


def _segment_pipeline_digest(
    *, fps: float, width: int, height: int, sample_rate: int, toolchain_fingerprint: str
) -> str:
    """Fingerprint of every encoder knob, so a pipeline change invalidates the cache.

    The CAS folds this into its own content-addressed key, which means tuning the
    preset, CRF, pixel format or audio settings - or switching to a different
    FFmpeg build - can never silently serve segments encoded with the old ones.
    """
    return hashlib.sha256(
        canonical_json(
            {
                "schema": "nagar-shot-pipeline-v1",
                "encoder": "libx264",
                "preset": "veryfast",
                "crf": 28,
                "pix_fmt": "yuv420p",
                "audio": "aac",
                "audio_bitrate": "96k",
                "sample_rate": sample_rate,
                "fps": repr(float(fps)),
                "width": width,
                "height": height,
                "mux_mode": SEGMENT_MUX_MODE,
                "toolchain_fingerprint": toolchain_fingerprint,
            }
        )
    ).hexdigest()


def _resolve_binary(ffmpeg_bin: str) -> Path:
    """Resolve a command name or path to the canonical file that would be executed.

    A bare name is looked up on ``PATH`` exactly as a shell would resolve it, so
    the fingerprint describes the binary the pipeline will actually run; a path
    is resolved with ``strict=True`` so a dangling link or a missing file is a
    typed refusal rather than an identity for something that is not there.
    """
    candidate = ffmpeg_bin
    if os.sep not in candidate and (os.altsep is None or os.altsep not in candidate):
        found = shutil.which(candidate)
        if found is None:
            raise StudioBridgeError("ffmpeg_unavailable", f"{ffmpeg_bin} is not on PATH")
        candidate = found
    try:
        return Path(candidate).resolve(strict=True)
    except OSError as exc:
        raise StudioBridgeError(
            "ffmpeg_unavailable", f"{ffmpeg_bin} ({type(exc).__name__})"
        ) from exc


def _binary_identity(ffmpeg_bin: str) -> dict[str, Any]:
    """Canonical path, size and SHA-256 of the encoder binary itself.

    The binary is resolved (:func:`_resolve_binary`), opened with ``O_NOFOLLOW``
    so a link swapped in after resolution cannot redirect the read, proven to be
    a regular file with a positive size through ``fstat`` on that descriptor, and
    hashed as a stream. A binary that cannot be vouched for raises
    ``StudioBridgeError("ffmpeg_unavailable")``: an ``"unavailable"`` identity
    could never be compared with an earlier one, so it must not exist.
    """
    resolved = _resolve_binary(ffmpeg_bin)
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise StudioBridgeError("ffmpeg_unavailable", f"{resolved} ({type(exc).__name__})") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        binary = os.fstat(fd)
        if not stat.S_ISREG(binary.st_mode) or binary.st_size <= 0:
            raise StudioBridgeError(
                "ffmpeg_unavailable", f"{resolved} is not a non-empty regular file"
            )
        while True:
            chunk = os.read(fd, CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        if size != binary.st_size:
            raise StudioBridgeError("ffmpeg_unavailable", f"{resolved} changed while hashed")
    except OSError as exc:
        raise StudioBridgeError("ffmpeg_unavailable", f"{resolved} ({type(exc).__name__})") from exc
    finally:
        os.close(fd)
    return {"resolved_path": str(resolved), "sha256": digest.hexdigest(), "size_bytes": size}


def _binary_stat_key(ffmpeg_bin: str) -> tuple[str, int, int]:
    """Cheap ``(resolved path, mtime_ns, size)`` stamp of the binary on disk.

    This is the cache key of :func:`_ffmpeg_toolchain_fingerprint`: replacing the
    binary in place - same path, same ``-version`` line, new bytes - changes the
    stamp, so a fingerprint computed from the old bytes can never be served for
    the new ones.
    """
    resolved = _resolve_binary(ffmpeg_bin)
    try:
        binary = os.lstat(resolved)
    except OSError as exc:
        raise StudioBridgeError("ffmpeg_unavailable", f"{resolved} ({type(exc).__name__})") from exc
    if not stat.S_ISREG(binary.st_mode) or binary.st_size <= 0:
        raise StudioBridgeError("ffmpeg_unavailable", f"{resolved} is not a non-empty regular file")
    return (str(resolved), int(binary.st_mtime_ns), int(binary.st_size))


def _ffmpeg_toolchain_fingerprint(ffmpeg_bin: str) -> str:
    """Deterministic identity of the FFmpeg toolchain behind one binary path.

    Two builds can report the same version string and still encode different
    bytes, so the fingerprint binds the first ``-version`` line *and* the
    canonical path and SHA-256 of the binary on disk. It is folded into both the
    shot cache key and the CAS pipeline digest. The result is memoised against
    the binary's ``(path, mtime, size)`` stamp, so a rebuild in place busts the
    cache, and a binary that cannot be resolved or hashed raises
    ``StudioBridgeError("ffmpeg_unavailable")`` here rather than being folded
    into the digest as an unusable placeholder.
    """
    return _cached_toolchain_fingerprint(ffmpeg_bin, _binary_stat_key(ffmpeg_bin))


@functools.lru_cache(maxsize=8)
def _cached_toolchain_fingerprint(ffmpeg_bin: str, _stamp: tuple[str, int, int]) -> str:
    """Cached body of :func:`_ffmpeg_toolchain_fingerprint`; ``_stamp`` only keys the cache."""
    identity = _binary_identity(ffmpeg_bin)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv list, no shell
            [identity["resolved_path"], "-version"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        version_line = ""
    else:
        lines = completed.stdout.decode("utf-8", errors="replace").splitlines()[:1]
        version_line = lines[0] if lines else ""
    # The identity - not the string the caller typed - is what gets hashed, so the
    # same binary reached through a bare name, a symlink or its real path is one
    # fingerprint, and a rebuild in place (same path, new bytes) is a new one.
    return hashlib.sha256(
        canonical_json(
            {"ffmpeg": identity["resolved_path"], "version": version_line, "binary": identity}
        )
    ).hexdigest()


def _shot_index_path(cas_root_dir: Path, segment_key_hash: str) -> Path:
    """Where the ``shot key -> CAS entry`` mapping lives (validated, symlink-safe)."""
    if not _HEX64.fullmatch(segment_key_hash):
        raise StudioBridgeError("invalid_cache_key", segment_key_hash)
    index_root = cas_root_dir / SHOT_INDEX_DIRNAME
    index_root.mkdir(parents=True, exist_ok=True)
    if stat.S_ISLNK(os.lstat(index_root).st_mode):
        raise StudioBridgeError("symlink_refused", str(index_root))
    return _safe_child(index_root, f"{segment_key_hash}.json", label="shot index entry")


def _read_shot_index(
    path: Path, *, segment_key_hash: str, meter: _MemoryMeter
) -> dict[str, Any] | None:
    """Return the index entry only when it is structurally sound, otherwise ``None``."""
    if not path.is_file() or path.is_symlink():
        return None
    try:
        entry = _read_small_json(path, limit=MAX_INDEX_BYTES, meter=meter)
    except (OSError, ValueError, StudioBridgeError):
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("schema") != SHOT_SEGMENT_SCHEMA:
        return None
    if entry.get("segment_key_hash") != segment_key_hash:
        return None
    cas_key = entry.get("cas_key")
    content_sha = entry.get("content_sha")
    if not isinstance(cas_key, str) or not _HEX64.fullmatch(cas_key):
        return None
    if not isinstance(content_sha, str) or not _HEX64.fullmatch(content_sha):
        return None
    return entry


def _write_shot_index(
    path: Path,
    *,
    segment_key_hash: str,
    shot_id: str,
    cas_key: str,
    content_sha: str,
    body_size: int,
    shot_content_hash_hex: str,
) -> None:
    """Atomically record ``shot key -> MP4 content hash`` (tmp + fsync + replace)."""
    _atomic_write_bytes(
        path,
        canonical_json(
            {
                "schema": SHOT_SEGMENT_SCHEMA,
                "segment_key_hash": segment_key_hash,
                "shot_id": shot_id,
                "shot_content_hash": shot_content_hash_hex,
                "cas_key": cas_key,
                "content_sha": content_sha,
                "body_size": body_size,
            }
        ),
    )


# --------------------------------------------------------------------------- #
# FFmpeg invocation
# --------------------------------------------------------------------------- #


def _run_ffmpeg(argv: Sequence[str], *, timeout_s: float, label: str) -> None:
    """Run one FFmpeg invocation with a fixed argv (never a shell) and typed errors."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv list, no shell
            list(argv),
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise StudioBridgeError("ffmpeg_timeout", f"{label} after {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise StudioBridgeError("ffmpeg_unavailable", f"{label}: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        tail = completed.stderr[-400:].decode("utf-8", errors="replace")
        raise StudioBridgeError("ffmpeg_failed", f"{label} rc={completed.returncode}: {tail}")


def build_shot_ffmpeg_argv(
    *,
    ffmpeg: str,
    image_path: Path,
    wav_path: Path,
    output_path: Path,
    duration_s: float,
    fps: float,
    width: int,
    height: int,
    sample_rate: int,
) -> list[str]:
    """Deterministic single-shot encode: one PNG plus one tone -> one MP4 segment."""
    fps_str = f"{float(fps):.6f}".rstrip("0").rstrip(".")
    dur = f"{duration_s:.6f}"
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-framerate",
        fps_str,
        "-t",
        dur,
        "-i",
        str(image_path),
        "-t",
        dur,
        "-i",
        str(wav_path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-r",
        fps_str,
        "-s",
        f"{width}x{height}",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    ]


def build_concat_argv(*, ffmpeg: str, concat_list: Path, output_path: Path) -> list[str]:
    """Assemble the film from already-encoded segments without re-encoding."""
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output_path),
    ]


def _write_concat_list(path: Path, segments: Sequence[Path], *, meter: _MemoryMeter) -> None:
    """Write the concat demuxer list; any character FFmpeg could mis-parse is refused."""
    lines: list[str] = []
    for segment in segments:
        text = str(Path(segment).resolve())
        if any(bad in text for bad in _UNSAFE_CONCAT_CHARS):
            raise StudioBridgeError("unsafe_path", f"concat entry: {text!r}")
        lines.append(f"file '{text}'")
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    meter.charge(len(payload))
    try:
        _atomic_write_bytes(path, payload)
    finally:
        meter.release(len(payload))


# --------------------------------------------------------------------------- #
# Shot planning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _ShotPlan:
    """One shot's material, digests and content-addressed cache address."""

    shot_id: str
    duration_s: float
    duration_ms: int
    start_frame: int
    end_frame: int
    image_path: Path
    wav_path: Path
    image_sha256: str
    wav_sha256: str
    shot_content_hash: str
    cache_key_hash: str


def _shot_id(event: Any) -> str:  # noqa: ANN401 - duck-typed editorial event
    return f"s{int(event.scene_index):03d}-sh{int(event.shot_index):03d}"


def _materialize_shot_assets(
    *,
    manifest: Any,  # noqa: ANN401 - CompiledStoryboardManifest
    profile: RenderProfile,
    output_dir: Path,
    local_images: Mapping[str, Path] | None,
    meter: _MemoryMeter,
    toolchain_fingerprint: str,
) -> tuple[list[_ShotPlan], list[AssetRecord], list[float]]:
    """Write one PNG and one WAV per shot, then derive its content-addressed key."""
    plans: list[_ShotPlan] = []
    records: list[AssetRecord] = []
    durations: list[float] = []
    frame_cursor = 0
    planned = planned_shot_durations(manifest)
    events = list(manifest.editorial.events)
    if len(planned) != len(events):  # pragma: no cover - compiler invariant
        raise StudioBridgeError(
            "plan_mismatch", f"{len(planned)} durations for {len(events)} shots"
        )
    for event, duration in zip(events, planned, strict=True):
        shot_id = _shot_id(event)
        kind_name = getattr(event.shot_kind, "value", str(event.shot_kind))
        seed = shot_seed(shot_id, kind_name, int(event.scene_index), int(event.shot_index))
        image_path = _safe_child(output_dir, f"{shot_id}.png", label="shot image")
        wav_path = _safe_child(output_dir, f"{shot_id}.wav", label="shot tone")

        supplied = local_images.get(shot_id) if local_images else None
        if supplied is not None:
            source = Path(supplied)
            if source.is_symlink() or not source.is_file():
                raise StudioBridgeError("local_image_missing", f"{shot_id}: {source}")
            # Copied in bounded chunks so the bundle stays inside the workspace and
            # the sealed asset record always describes bytes this pipeline wrote.
            image_sha, image_size = _copy_streaming(source, image_path, meter=meter)
            _, _, image_inode = stream_sha256(image_path)
            image_record = AssetRecord(
                shot_id=shot_id,
                kind="local_image",
                relative_path=image_path.name,
                resolved_path=str(image_path),
                size_bytes=image_size,
                sha256=image_sha,
                inode=image_inode,
                duration_s=0.0,
            )
        else:
            write_slate_png(
                image_path,
                width=profile.width,
                height=profile.height,
                seed=seed,
                label=f"{shot_id}|{kind_name}",
            )
            image_sha, image_size, image_inode = stream_sha256(image_path)
            image_record = AssetRecord(
                shot_id=shot_id,
                kind="slate_png",
                relative_path=image_path.name,
                resolved_path=str(image_path),
                size_bytes=image_size,
                sha256=image_sha,
                inode=image_inode,
                duration_s=0.0,
            )
        frames = write_tone_wav(
            wav_path, duration_s=duration, sample_rate=profile.sample_rate, seed=seed
        )
        wav_sha, wav_size, wav_inode = stream_sha256(wav_path)
        actual_duration = frames / profile.sample_rate
        records.append(image_record)
        records.append(
            AssetRecord(
                shot_id=shot_id,
                kind="tone_wav",
                relative_path=wav_path.name,
                resolved_path=str(wav_path),
                size_bytes=wav_size,
                sha256=wav_sha,
                inode=wav_inode,
                duration_s=actual_duration,
            )
        )
        durations.append(actual_duration)
        content_hash = shot_content_hash(
            shot_id=shot_id,
            image_sha256=image_sha,
            wav_sha256=wav_sha,
            duration_s=actual_duration,
        )
        span = max(1, int(round(actual_duration * profile.fps)))
        plans.append(
            _ShotPlan(
                shot_id=shot_id,
                duration_s=actual_duration,
                duration_ms=int(round(actual_duration * 1000)),
                start_frame=frame_cursor,
                end_frame=frame_cursor + span,
                image_path=image_path,
                wav_path=wav_path,
                image_sha256=image_sha,
                wav_sha256=wav_sha,
                shot_content_hash=content_hash,
                cache_key_hash=shot_cache_key_hash(
                    shot_id=shot_id,
                    shot_content_hash=content_hash,
                    duration_ms=int(round(actual_duration * 1000)),
                    fps=profile.fps,
                    width=profile.width,
                    height=profile.height,
                    image_sha256=image_sha,
                    toolchain_fingerprint=toolchain_fingerprint,
                ),
            )
        )
        frame_cursor += span
    return plans, records, durations


def _segment_spec(
    plan: _ShotPlan, *, profile: RenderProfile, width: int, height: int
) -> TimelineSegmentSpec:
    """Bind a shot to the hyper-compiler's content-addressed segment spec.

    Frames are *segment-local* (``0 .. span``) on purpose: a shot segment is an
    independent file, so its cache address must not depend on where in the film
    it sits. Otherwise lengthening shot 2 would renumber every later segment and
    silently invalidate the whole cache. The shot's identity is carried by
    ``segment_id`` and its position by the sealed per-shot Merkle tree instead.
    """
    span = max(1, plan.end_frame - plan.start_frame)
    return TimelineSegmentSpec(
        segment_id=f"studio.{plan.shot_id}",
        start_frame=0,
        end_frame=span,
        fps=float(profile.fps),
        width=width,
        height=height,
        shot_dna_digest=plan.shot_content_hash,
        camera_digest=plan.image_sha256,
        lighting_digest=plan.cache_key_hash,
        audio_digest=plan.wav_sha256,
        audio_filter_digest=plan.cache_key_hash,
    )


class _FileSink:
    """Streaming sink that keeps the live-buffer meter honest during a CAS restore."""

    __slots__ = ("_handle", "_meter")

    def __init__(self, handle: IO[bytes], meter: _MemoryMeter) -> None:
        self._handle = handle
        self._meter = meter

    def write(self, data: bytes, /) -> int:
        self._meter.charge(len(data))
        try:
            self._handle.write(data)
        finally:
            self._meter.release(len(data))
        return len(data)


def _restore_segment_from_cas(
    *,
    cas: SegmentCASPlanner,
    cas_key: str,
    expected_content_sha: str,
    target: Path,
    pipeline_digest: str,
    meter: _MemoryMeter,
) -> None:
    """Stream a verified segment out of the CAS, then prove the staged bytes again.

    The CAS proves the manifest provenance and both content hashes while
    streaming; the staged file is then re-hashed from disk, so what FFmpeg will
    concatenate is exactly the bytes whose SHA-256 the index recorded.
    """
    verified = cas.verify_segment_artifact(
        cas_key, expected_mode=SEGMENT_MUX_MODE, expected_pipeline_digest=pipeline_digest
    )
    if verified.content_sha != expected_content_sha:
        raise StudioBridgeError(
            "cas_content_mismatch", f"{cas_key}: {verified.content_sha} != {expected_content_sha}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    out_fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(out_fd, "wb") as handle:
            cas.stream_segment_body(
                cas_key,
                _FileSink(handle, meter),
                expected_mode=SEGMENT_MUX_MODE,
                expected_pipeline_digest=pipeline_digest,
            )
            handle.flush()
            os.fsync(handle.fileno())
        digest, size, _ = stream_sha256(tmp)
        if digest != expected_content_sha or size != verified.body_size:
            raise StudioBridgeError(
                "cas_staged_mismatch", f"{cas_key}: staged {digest}/{size} != index"
            )
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _commit_segment_to_cas(
    *,
    cas: SegmentCASPlanner,
    cas_key: str,
    spec: TimelineSegmentSpec,
    staged: Path,
    pipeline_digest: str,
) -> tuple[str, int]:
    """Persist a freshly rendered segment; return its content hash and body size."""
    digest, size, _ = stream_sha256(staged)
    with staged.open("rb") as handle:
        cas.commit_stream(
            cas_key,
            handle,
            size=size,
            digest=digest,
            mode=SEGMENT_MUX_MODE,
            pipeline_digest=pipeline_digest,
            spec=spec,
        )
    return digest, size


# --------------------------------------------------------------------------- #
# Type-4 render
# --------------------------------------------------------------------------- #


def _validate_profile(*, fps: int, width: int, height: int, sample_rate: int) -> RenderProfile:
    try:
        return RenderProfile.from_mapping(
            {"width": width, "height": height, "fps": fps, "sample_rate": sample_rate}
        )
    except MaterializationError as exc:
        raise StudioBridgeError(exc.reason, exc.detail) from exc


def _verify_segment(
    path: Path, *, expected_s: float, fps: float, ffprobe: str, timeout_s: float
) -> Mp4Verdict:
    return verify_mp4(
        path,
        expected_duration_s=expected_s,
        tolerance_s=DURATION_MARGIN_S + FRAME_MARGIN_PER_SHOT / fps,
        ffprobe=ffprobe,
        timeout_s=timeout_s,
    )


def _cas_anchor_key(signing_key: object) -> bytes:
    """Derive the segment-CAS manifest anchor key from the studio signing secret.

    The CAS demands a trusted anchor (``require_trusted_anchor``): an unsigned
    manifest can re-derive its own content-addressed key, so it is only
    trustworthy when tied to a secret an attacker does not hold. The anchor is
    domain-separated from the passport signing key, so a leaked CAS seal can
    never be replayed as a passport signature (and vice versa).
    """
    if isinstance(signing_key, str):
        raw = signing_key.encode("utf-8")
    elif isinstance(signing_key, (bytes, bytearray)):
        raw = bytes(signing_key)
    else:
        raise StudioBridgeError("invalid_key", "signing key must be str or bytes")
    if len(raw) < 32:
        raise StudioBridgeError("invalid_key", "signing key must be at least 32 bytes")
    return hmac.new(CAS_ANCHOR_KEY_DOMAIN, raw, hashlib.sha256).digest()


def render_storyboard_with_segment_cas(
    script_text: str,
    title: str,
    output_dir: Path,
    cas_root_dir: Path,
    job_id: str,
    approval_id: str = "",
    approved_by: str = "",
    *,
    fps: int = 24,
    width: int = 640,
    height: int = 360,
    sample_rate: int = 16_000,
    local_images: Mapping[str, Path] | None = None,
    memory_ceiling_bytes: int = DEFAULT_MEMORY_CEILING_BYTES,
    signing_key: object,
    project_id: str = "studio-local",
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    timeout_s: float = 180.0,
    manifest: Any = None,  # noqa: ANN401 - optional pre-compiled CompiledStoryboardManifest
    before_render: Callable[[Any], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> CasStoryboardRenderResult:
    """Render ``script_text`` incrementally: unchanged shots never reach FFmpeg.

    ``manifest`` may be supplied by a caller that already compiled (and consumed
    an approval against) the same screenplay. ``before_render`` runs after
    compilation and before the first shot is rendered, which is where an approval
    ledger is consumed against the sealed manifest digests.
    """
    meter = _MemoryMeter(memory_ceiling_bytes)
    profile = _validate_profile(fps=fps, width=width, height=height, sample_rate=sample_rate)
    if not isinstance(script_text, str) or not script_text.strip():
        raise StudioBridgeError("invalid_script", "script_text must be a non-empty string")
    out_dir = _prepare_directory(Path(output_dir), label="output_dir")
    cas_dir = _prepare_directory(Path(cas_root_dir), label="cas_root_dir")

    compiled = manifest
    if compiled is None:
        try:
            compiled = compile_storyboard(script_text)
        except Exception as exc:  # noqa: BLE001 - every compiler refusal becomes typed here
            raise StudioBridgeError("compile_failed", type(exc).__name__) from exc
    manifest_path = _safe_child(out_dir, f"{job_id}.manifest.json", label="manifest")
    manifest_bytes = compiled.to_canonical_json().encode("utf-8")
    meter.charge(len(manifest_bytes))
    try:
        _atomic_write_bytes(manifest_path, manifest_bytes)
    finally:
        meter.release(len(manifest_bytes))
    manifest_sha, manifest_size, manifest_inode = stream_sha256(manifest_path)
    if before_render is not None:
        before_render(compiled)

    toolchain = _ffmpeg_toolchain_fingerprint(ffmpeg)
    plans, records, durations = _materialize_shot_assets(
        manifest=compiled,
        profile=profile,
        output_dir=out_dir,
        local_images=local_images,
        meter=meter,
        toolchain_fingerprint=toolchain,
    )
    cas = SegmentCASPlanner(cas_dir, hmac_key=_cas_anchor_key(signing_key))
    pipeline_digest = _segment_pipeline_digest(
        fps=profile.fps,
        width=width,
        height=height,
        sample_rate=sample_rate,
        toolchain_fingerprint=toolchain,
    )
    segments_dir = _prepare_directory(out_dir / "segments", label="segments")
    staged_segments: list[Path] = []
    segment_content_hashes: list[str] = []
    cas_hits = 0
    cas_misses = 0
    for plan in plans:
        staged = _safe_child(segments_dir, f"{plan.shot_id}.mp4", label="segment")
        spec = _segment_spec(plan, profile=profile, width=width, height=height)
        cas_key = cas.cas_key(spec, pipeline_digest=pipeline_digest, mux_mode=SEGMENT_MUX_MODE)
        index_path = _shot_index_path(cas_dir, plan.cache_key_hash)
        entry = _read_shot_index(index_path, segment_key_hash=plan.cache_key_hash, meter=meter)
        restored: str | None = None
        if entry is not None and entry.get("cas_key") == cas_key:
            try:
                _restore_segment_from_cas(
                    cas=cas,
                    cas_key=cas_key,
                    expected_content_sha=str(entry["content_sha"]),
                    target=staged,
                    pipeline_digest=pipeline_digest,
                    meter=meter,
                )
            except Exception:  # noqa: BLE001 - a broken CAS entry is a miss, never a crash
                staged.unlink(missing_ok=True)
                restored = None
            else:
                restored = str(entry["content_sha"])
        if restored is not None:
            verdict = _verify_segment(
                staged,
                expected_s=plan.duration_s,
                fps=profile.fps,
                ffprobe=ffprobe,
                timeout_s=timeout_s,
            )
            if not verdict.passed:
                raise StudioBridgeError(
                    "cached_segment_invalid", f"{plan.shot_id}: {'; '.join(verdict.reasons)}"
                )
            cas_hits += 1
            segment_content_hashes.append(restored)
            staged_segments.append(staged)
            continue

        staged.unlink(missing_ok=True)
        _run_ffmpeg(
            build_shot_ffmpeg_argv(
                ffmpeg=ffmpeg,
                image_path=plan.image_path,
                wav_path=plan.wav_path,
                output_path=staged,
                duration_s=plan.duration_s,
                fps=profile.fps,
                width=width,
                height=height,
                sample_rate=sample_rate,
            ),
            timeout_s=timeout_s,
            label=f"shot {plan.shot_id}",
        )
        verdict = _verify_segment(
            staged,
            expected_s=plan.duration_s,
            fps=profile.fps,
            ffprobe=ffprobe,
            timeout_s=timeout_s,
        )
        if not verdict.passed:
            raise StudioBridgeError(
                "segment_invalid", f"{plan.shot_id}: {'; '.join(verdict.reasons)}"
            )
        content_sha, body_size = _commit_segment_to_cas(
            cas=cas,
            cas_key=cas_key,
            spec=spec,
            staged=staged,
            pipeline_digest=pipeline_digest,
        )
        _write_shot_index(
            index_path,
            segment_key_hash=plan.cache_key_hash,
            shot_id=plan.shot_id,
            cas_key=cas_key,
            content_sha=content_sha,
            body_size=body_size,
            shot_content_hash_hex=plan.shot_content_hash,
        )
        cas_misses += 1
        segment_content_hashes.append(content_sha)
        staged_segments.append(staged)

    if not staged_segments:  # pragma: no cover - the compiler always emits a shot
        raise StudioBridgeError("empty_storyboard", "the compiler produced no shots")

    concat_list = _safe_child(out_dir, ".concat_list.txt", label="concat list")
    _write_concat_list(concat_list, staged_segments, meter=meter)
    output_mp4 = _safe_child(out_dir, f"{FILM_BASENAME}.mp4", label="film")
    tmp_mp4 = _safe_child(out_dir, f".{FILM_BASENAME}.tmp.mp4", label="film temp")
    try:
        _run_ffmpeg(
            build_concat_argv(ffmpeg=ffmpeg, concat_list=concat_list, output_path=tmp_mp4),
            timeout_s=timeout_s,
            label="concat",
        )
        verdict = verify_mp4(
            tmp_mp4,
            expected_duration_s=sum(durations),
            tolerance_s=DURATION_MARGIN_S + len(plans) * FRAME_MARGIN_PER_SHOT / profile.fps,
            ffprobe=ffprobe,
            timeout_s=timeout_s,
        )
        if not verdict.passed:
            raise StudioBridgeError("verifier_failed", "; ".join(verdict.reasons))
        os.replace(tmp_mp4, output_mp4)
    finally:
        tmp_mp4.unlink(missing_ok=True)
        concat_list.unlink(missing_ok=True)
    if stream_sha256(output_mp4)[0] != verdict.output_sha256:
        raise StudioBridgeError("artifact_changed_after_verify", str(output_mp4))

    bundle = _build_bundle(
        out_dir=out_dir,
        profile=profile,
        manifest_path=manifest_path,
        manifest_sha=manifest_sha,
        manifest_size=manifest_size,
        manifest_inode=manifest_inode,
        records=records,
        durations=durations,
        plans=plans,
    )
    try:
        StoryboardMediaMaterializer(out_dir, profile).verify_materialized_provenance(bundle)
    except MaterializationError as exc:
        raise StudioBridgeError(exc.reason, exc.detail) from exc

    passport = build_passport(
        job_id=job_id,
        project_id=project_id,
        source_sha256=hashlib.sha256(script_text.encode("utf-8")).hexdigest(),
        manifest_sha256=bundle.manifest_sha256,
        render_profile_hash=profile.profile_hash(),
        asset_merkle_root=bundle.asset_merkle_root,
        total_asset_leaves=bundle.total_asset_leaves,
        output_rel_path=output_mp4.relative_to(out_dir).as_posix(),
        output_size_bytes=verdict.size_bytes,
        output_sha256=verdict.output_sha256,
        probed_duration_s=verdict.probed_duration_s,
        verifier_status=PASS,
        created_at_unix_s=clock() if clock is not None else time.time(),
        shot_hashes=bundle.shot_hashes,
        signing_key=signing_key,
    )
    passport_path = _safe_child(out_dir, f"{FILM_BASENAME}{PASSPORT_SUFFIX}", label="passport")
    write_passport(passport_path, passport)
    try:
        verify_artifact_passport(passport_path, output_mp4, signing_key)
    except Exception as exc:  # noqa: BLE001 - any passport refusal is fatal here
        raise StudioBridgeError("passport_verification_failed", type(exc).__name__) from exc

    self_peak_rss, children_rss, process_hwm = _process_tree_rss_hwm()
    receipt_path = _safe_child(out_dir, f"{job_id}.receipt.json", label="receipt")
    _write_receipt(
        receipt_path,
        meter=meter,
        payload={
            "schema": "nagar-studio-render-receipt-v1",
            "job_id": job_id,
            "project_id": project_id,
            "title": title,
            "approval_id": approval_id,
            "approved_by": approved_by,
            "script_sha256": compiled.script_sha256,
            "manifest_sha256": compiled.manifest_sha256,
            "manifest_rel_path": manifest_path.relative_to(out_dir).as_posix(),
            "output_rel_path": output_mp4.relative_to(out_dir).as_posix(),
            "output_sha256": verdict.output_sha256,
            "output_size_bytes": verdict.size_bytes,
            "probed_duration_s": repr(float(verdict.probed_duration_s)),
            "shot_count": len(plans),
            "shot_merkle_root": bundle.shot_merkle_root,
            "cas_hits": cas_hits,
            "cas_misses": cas_misses,
            "bridge_buffer_peak_bytes": meter.peak_bytes,
            "self_peak_rss_bytes": self_peak_rss,
            "children_max_rss_bytes": children_rss,
            "process_hwm_estimate_bytes": process_hwm,
            "process_peak_rss_bytes": process_hwm,
            "ffmpeg_toolchain_fingerprint": toolchain,
        },
    )
    return CasStoryboardRenderResult(
        artifact=bundle,
        verification=verdict,
        passport=passport,
        passport_path=passport_path,
        manifest_path=manifest_path,
        cas_hits=cas_hits,
        cas_misses=cas_misses,
        shot_segment_hashes=tuple(segment_content_hashes),
        peak_memory_bytes=meter.peak_bytes,
        output_mp4_path=output_mp4,
        receipt_path=receipt_path,
        bridge_buffer_peak_bytes=meter.peak_bytes,
        self_peak_rss_bytes=self_peak_rss,
        children_max_rss_bytes=children_rss,
        process_hwm_estimate_bytes=process_hwm,
        process_peak_rss_bytes=process_hwm,
    )


def _build_bundle(
    *,
    out_dir: Path,
    profile: RenderProfile,
    manifest_path: Path,
    manifest_sha: str,
    manifest_size: int,
    manifest_inode: int,
    records: Sequence[AssetRecord],
    durations: Sequence[float],
    plans: Sequence[_ShotPlan],
) -> MaterializedBundle:
    """Assemble the materializer's bundle, including the per-shot Merkle tree."""
    asset_root, leaves = merkle_root([canonical_json(record.as_dict()) for record in records])
    shot_hashes = tuple(plan.shot_content_hash for plan in plans)
    payloads = shot_leaf_payloads(shot_hashes)
    return MaterializedBundle(
        out_dir=str(out_dir),
        profile=profile,
        manifest_path=str(manifest_path),
        manifest_sha256=manifest_sha,
        manifest_size=manifest_size,
        manifest_inode=manifest_inode,
        assets=tuple(records),
        shot_durations_s=tuple(durations),
        asset_merkle_root=asset_root,
        total_asset_leaves=leaves,
        shot_hashes=shot_hashes,
        shot_merkle_root=merkle_root_sha256(payloads),
        shot_inclusion_proofs=tuple(
            merkle_inclusion_proof_record(payloads, index) for index in range(len(payloads))
        ),
    )


def _write_receipt(path: Path, *, meter: _MemoryMeter, payload: Mapping[str, Any]) -> None:
    raw = canonical_json(payload)
    meter.charge(len(raw))
    try:
        _atomic_write_bytes(path, raw)
    finally:
        meter.release(len(raw))


# --------------------------------------------------------------------------- #
# Encrypted vault round trip
# --------------------------------------------------------------------------- #


def _read_journal(path: Path, *, meter: _MemoryMeter) -> list[dict[str, Any]]:
    """Read the append-only sync journal; a corrupt line is a typed refusal."""
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise StudioBridgeError("journal_invalid", str(path))
    if os.lstat(path).st_size <= 0:
        return []
    # ``_read_bounded_bytes`` is the only reader: it opens once with O_NOFOLLOW,
    # bounds the size on that descriptor and raises typed refusals, so the journal
    # can never be read through a swapped link or slurped without a cap.
    raw = _read_bounded_bytes(
        path, limit=MAX_JOURNAL_BYTES, meter=meter, reason="journal_invalid"
    ).decode("utf-8")
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StudioBridgeError("journal_invalid", f"{path}: {exc.msg}") from exc
        if not isinstance(entry, dict):
            raise StudioBridgeError("journal_invalid", f"{path}: non-object entry")
        entries.append(entry)
    return entries


def _current_vault_head(driver: Any, project_id: str) -> str | None:  # noqa: ANN401
    """Return the single remote head of ``project_id`` (``None`` for an empty vault).

    The vault is optimistic-concurrency: a second snapshot must name the head it
    extends, so the bridge asks the driver for the project's manifests and
    resolves the head exactly the way the manager does (a manifest referenced as
    somebody's parent is not a head). Two competing heads are a typed refusal
    rather than a silent fork.
    """
    manifests: dict[str, VaultSnapshotManifest] = {}
    for snapshot_id in driver.list_manifests():
        try:
            manifest = VaultSnapshotManifest.from_json_bytes(driver.get_manifest(snapshot_id))
        except Exception as exc:  # noqa: BLE001 - an unreadable manifest is a hard refusal
            raise StudioBridgeError("vault_manifest_invalid", type(exc).__name__) from exc
        if manifest.project_id == project_id:
            manifests[snapshot_id] = manifest
    if not manifests:
        return None
    referenced = {
        manifest.parent_snapshot_id
        for manifest in manifests.values()
        if manifest.parent_snapshot_id in manifests
    }
    heads = set(manifests) - referenced
    if len(heads) != 1:
        raise StudioBridgeError("vault_ambiguous_head", str(sorted(heads)))
    return next(iter(heads))


def _append_journal(path: Path, *, entry: Mapping[str, Any]) -> None:
    """Append one JSON line and fsync it, so a crash cannot lose a sync receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json(entry) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _stage_vault_bundle(
    *, render_result: CasStoryboardRenderResult, meter: _MemoryMeter
) -> tuple[Path, dict[str, str]]:
    """Copy the three deliverables into a clean directory, streaming every byte."""
    staging = Path(tempfile.mkdtemp(dir=render_result.output_dir, prefix=".vault-push-"))
    digests: dict[str, str] = {}
    for source in (
        render_result.output_mp4_path,
        render_result.manifest_path,
        render_result.passport_path,
    ):
        digest, _ = _copy_streaming(source, staging / source.name, meter=meter)
        digests[source.name] = digest
    return staging, digests


def sync_and_verify_studio_bundle_in_vault(
    *,
    render_result: CasStoryboardRenderResult,
    vault_storage_dir: Path,
    vault_journal_db_path: Path,
    restore_dir: Path,
    master_secret: bytes | str,
    project_id: str,
    signing_key: object,
    device_id: str = "studio-local-node-01",
    ffprobe: str = "ffprobe",
    timeout_s: float = 180.0,
) -> StudioVaultSyncReport:
    """Encrypt, push, pull and re-verify the studio bundle in the personal vault.

    Fail-closed at every step: the push is written by the driver, the pull
    re-derives every chunk MAC and the RFC 6962 manifest root, and the restored
    MP4 and passport are then verified independently. A ``RuntimeError`` means the
    vault round trip did not preserve the artifact bit for bit.
    """
    if not project_id.strip():
        raise StudioBridgeError("invalid_project_id", "project_id must not be empty")
    meter = _MemoryMeter(DEFAULT_MEMORY_CEILING_BYTES)
    vault_dir = _prepare_directory(Path(vault_storage_dir), label="vault_storage_dir")
    restore_root = _prepare_directory(Path(restore_dir), label="restore_dir")
    journal_path = Path(vault_journal_db_path)
    previous = _read_journal(journal_path, meter=meter)
    snapshot_seq = 1 + sum(1 for entry in previous if entry.get("project_id") == project_id)

    # The manifest is the only record of *how* the film was assembled, so its
    # digest is re-derived from the bytes on disk before they are ever encrypted:
    # a passport that names a different manifest must never reach the vault.
    staged_manifest_sha = stream_sha256(render_result.manifest_path)[0]
    passport_manifest_sha = str(render_result.passport.get("manifest_sha256", ""))
    if (
        staged_manifest_sha != passport_manifest_sha
        or staged_manifest_sha != render_result.artifact.manifest_sha256
    ):
        raise StudioBridgeError(
            "manifest_sha256_mismatch",
            f"{render_result.manifest_path}: {staged_manifest_sha} != {passport_manifest_sha}",
        )

    driver = LocalDeviceVaultDriver(vault_dir)
    manager = PersonalCloudVaultManager(driver)
    parent_snapshot_id = _current_vault_head(driver, project_id)
    staging, staged_digests = _stage_vault_bundle(render_result=render_result, meter=meter)
    try:
        push = manager.push_project_snapshot(
            project_id=project_id,
            source=staging,
            master_secret=master_secret,
            parent_snapshot_id=parent_snapshot_id,
            metadata={
                "device_id": device_id,
                "job_id": str(render_result.passport["job_id"]),
                "shot_merkle_root": render_result.shot_merkle_root,
                "snapshot_seq": str(snapshot_seq),
            },
        )
    except Exception as exc:  # noqa: BLE001 - every vault refusal is fatal for a backup
        raise StudioBridgeError("vault_push_failed", type(exc).__name__) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    _append_journal(
        journal_path,
        entry={
            "schema": "nagar-studio-vault-journal-v1",
            "project_id": project_id,
            "device_id": device_id,
            "snapshot_seq": snapshot_seq,
            "snapshot_id": push.snapshot_id,
            "parent_snapshot_id": parent_snapshot_id,
            "pushed_chunks": push.uploaded_chunks,
            "deduplicated_chunks": push.deduplicated_chunks,
            "total_chunks": push.total_chunks,
            "staged_digests": staged_digests,
        },
    )

    try:
        pull = manager.pull_project_snapshot(
            project_id=project_id,
            master_secret=master_secret,
            snapshot_id=push.snapshot_id,
            target_dir=restore_root,
        )
    except Exception as exc:  # noqa: BLE001 - a failed restore is never reported as success
        raise StudioBridgeError("vault_pull_failed", type(exc).__name__) from exc

    restored_mp4 = restore_root / render_result.output_mp4_path.name
    restored_passport = restore_root / render_result.passport_path.name
    restored_manifest = restore_root / render_result.manifest_path.name
    for path in (restored_mp4, restored_passport, restored_manifest):
        if path.is_symlink() or not path.is_file():
            raise StudioBridgeError("vault_restore_incomplete", str(path))
    if stream_sha256(restored_mp4)[0] != str(render_result.passport["output_sha256"]):
        raise StudioBridgeError(
            "vault_restored_bytes_differ", str(render_result.passport["output_sha256"])
        )

    try:
        passport_check = verify_artifact_passport(restored_passport, restored_mp4, signing_key)
    except Exception as exc:  # noqa: BLE001
        raise StudioBridgeError("vault_passport_verification_failed", type(exc).__name__) from exc
    # The restored manifest must be the same bytes that were pushed *and* the
    # bytes the restored passport seals: a single flipped byte is a failed backup.
    restored_manifest_sha = stream_sha256(restored_manifest)[0]
    sealed_manifest_sha = str(passport_check.passport.get("manifest_sha256", ""))
    if restored_manifest_sha != sealed_manifest_sha or restored_manifest_sha != staged_manifest_sha:
        raise StudioBridgeError(
            "vault_restored_manifest_mismatch",
            f"{restored_manifest}: {restored_manifest_sha} != {sealed_manifest_sha}",
        )
    artifact_verdict = verify_mp4(
        restored_mp4,
        expected_duration_s=render_result.artifact.total_duration_s,
        tolerance_s=DURATION_MARGIN_S
        + len(render_result.artifact.shot_hashes)
        * FRAME_MARGIN_PER_SHOT
        / render_result.artifact.profile.fps,
        ffprobe=ffprobe,
        timeout_s=timeout_s,
    )
    if not artifact_verdict.passed:
        raise RuntimeError(
            f"vault restored artifact failed verification: {artifact_verdict.status}"
        )

    return StudioVaultSyncReport(
        snapshot_seq=snapshot_seq,
        manifest_id=push.snapshot_id,
        pushed_chunks=push.uploaded_chunks,
        deduplicated_chunks=push.deduplicated_chunks,
        # The vault publishes restored files straight to disk and keeps the
        # in-memory ``files`` mapping empty by design (O(1) RAM restore), so
        # the restored count is read from the verified snapshot manifest; a
        # legacy report that carried the bytes in memory is counted directly.
        restored_files_count=len(
            pull.files or getattr(getattr(pull, "manifest", None), "files", None) or ()
        ),
        restored_mp4_path=restored_mp4,
        restored_passport_path=restored_passport,
        restored_passport_verification=passport_check,
        restored_artifact_verification=artifact_verdict,
    )


__all__ = [
    "DEFAULT_MEMORY_CEILING_BYTES",
    "FILM_BASENAME",
    "SEGMENT_MUX_MODE",
    "SHOT_CACHE_KEY_DOMAIN",
    "SHOT_INDEX_DIRNAME",
    "SHOT_SEGMENT_SCHEMA",
    "CasStoryboardRenderResult",
    "StudioBridgeError",
    "StudioVaultSyncReport",
    "build_concat_argv",
    "build_shot_ffmpeg_argv",
    "render_storyboard_with_segment_cas",
    "shot_cache_key_hash",
    "shot_cache_key_payload",
    "sync_and_verify_studio_bundle_in_vault",
]
