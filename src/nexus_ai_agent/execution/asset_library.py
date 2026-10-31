"""Content-addressed Asset Library under the queue-owned artifact root.

Phase-4 storage substrate (owner brief 2026-10-15 §4).  One blob per distinct
content, addressed by its SHA-256, admitted through an attempt-scoped staging
file and made visible by a single atomic link — so the authoritative namespace
never contains a partial, unverified or provider-named file.

Properties (each has a test in ``tests/unit/test_asset_library.py``):

* **Content addressing / dedup.** ``cas/sha256/<aa>/<bb>/<hex>``; admitting the
  same bytes twice (same or different attempts) yields one blob and reports
  ``deduplicated=True`` for the second admission — attempts stay distinct in
  the *caller's* durable record, not in the blob namespace.
* **Atomic finalize.** Bytes are streamed into ``cas/incoming/<attempt>-<nonce>.part``
  (``O_CREAT|O_EXCL|O_NOFOLLOW``), hashed while copied, bounded, ``fsync``ed,
  then published with ``os.link`` — which fails with ``EEXIST`` instead of
  replacing an existing blob.  A crash before the link leaves only a ``.part``
  that ``gc`` reaps; a crash after it leaves a complete blob.
* **Hostile sources.** The source is opened ``O_NOFOLLOW|O_NONBLOCK`` and must
  ``fstat`` as a regular file: symlinks, FIFOs, devices and directories are
  refused before a byte is read (a FIFO cannot block the worker).
* **Race safety.** Concurrent admissions of identical bytes produce exactly one
  blob and exactly one ``deduplicated=False``; a loser verifies the winner's
  bytes (size + full re-hash) before discarding its own copy.
* **Integrity.** A blob whose bytes no longer match its address is reported as
  ``corrupt`` by :meth:`AssetLibrary.fsck` / :meth:`AssetLibrary.verify` and is
  never silently "repaired" by an overwrite.
* **MIME.** The type is *sniffed* from the bytes (conservative magic table);
  a declared type that disagrees is recorded as ``mime_mismatch`` — the caller
  (verifier) decides, the store never trusts the declaration.
* **DB↔FS consistency and retention.** :meth:`fsck` reconciles the blobs on
  disk against the set of references the durable authority holds (passports /
  job rows) and reports ``missing`` (referenced, not on disk — an error),
  ``orphans`` (on disk, unreferenced) and ``corrupt``; :meth:`gc` deletes only
  unreferenced blobs and stale ``.part`` files older than a minimum age.

This module is storage only: it holds **no** index, manifest, queue or
verifier of its own.  Which blobs are *authoritative* is decided by the
durable job row / passport that references a ``storage_key`` — exactly as
``jobs.creative_passport.StoredAsset`` already does.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import os
import re
import secrets
import stat
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

__all__ = [
    "AdmittedAsset",
    "AssetBoundaryError",
    "AssetIntegrityError",
    "AssetLibrary",
    "AssetLibraryError",
    "AssetTooLargeError",
    "FsckReport",
    "sniff_mime",
]

#: Same ceiling as the creative passport evidence files.
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_CHUNK = 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


class AssetLibraryError(RuntimeError):
    """Base error of the asset library."""


class AssetBoundaryError(AssetLibraryError):
    """A path or source could not be proven safe (symlink, FIFO, escape, bad key)."""


class AssetTooLargeError(AssetLibraryError):
    """The source exceeds the configured ceiling."""


class AssetIntegrityError(AssetLibraryError):
    """An existing blob does not match its content address."""


@dataclass(frozen=True)
class AdmittedAsset:
    """What one admission produced (the caller records it durably)."""

    sha256: str  # "sha256:<hex>" — same shape as StoredAsset.sha256
    size_bytes: int
    storage_key: str  # "cas/sha256/aa/bb/<hex>" — relative to the artifact root
    mime: str
    declared_mime: str | None
    mime_mismatch: bool
    deduplicated: bool
    attempt_id: str


@dataclass(frozen=True)
class FsckReport:
    """Reconciliation of the blob namespace against the durable references."""

    checked: int
    missing: tuple[str, ...] = ()  # referenced keys with no blob
    orphans: tuple[str, ...] = ()  # blobs nobody references
    corrupt: tuple[str, ...] = ()  # blobs whose bytes do not hash to their address
    stale_incoming: tuple[str, ...] = ()  # .part files older than the minimum age
    unexpected: tuple[str, ...] = field(default_factory=tuple)  # non-blob entries in cas/

    @property
    def consistent(self) -> bool:
        return not (self.missing or self.corrupt or self.unexpected)


_MAGIC: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"%PDF-", "application/pdf"),
    (0, b"OggS", "audio/ogg"),
    (0, b"ID3", "audio/mpeg"),
    (0, b"\x1aE\xdf\xa3", "video/webm"),
    (0, b"PK\x03\x04", "application/zip"),
    (0, b"\x1f\x8b", "application/gzip"),
    (0, b"fLaC", "audio/flac"),
)


def sniff_mime(head: bytes) -> str:
    """Conservative magic-number sniff; ``application/octet-stream`` when unsure."""
    for offset, magic, mime in _MAGIC:
        if head[offset : offset + len(magic)] == magic:
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return "video/quicktime" if brand.startswith(b"qt") else "video/mp4"
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head and b"\x00" not in head:
        with contextlib.suppress(UnicodeDecodeError):
            head.decode("utf-8")
            return "text/plain"
    return "application/octet-stream"


def _hex_of(sha: str) -> str:
    digest = sha.removeprefix("sha256:")
    if not _HEX64.match(digest):
        raise AssetBoundaryError(f"not a sha256 content address: {sha!r}")
    return digest


def _key_of(hex_digest: str) -> str:
    return f"cas/sha256/{hex_digest[:2]}/{hex_digest[2:4]}/{hex_digest}"


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | _O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reject_symlink_components(root: Path, target: Path) -> None:
    """Refuse when any existing component of *target* (below *root*) is a symlink.

    ``O_NOFOLLOW`` only guards the *final* component, so a symlinked intermediate
    shard directory (``cas/sha256/aa`` → elsewhere) would silently redirect a
    published blob outside the artifact root. Walk the components from *root*
    down and require each existing one to be a real directory.
    """
    try:
        relative = target.relative_to(root)
    except ValueError as exc:  # pragma: no cover - defensive
        raise AssetBoundaryError(f"{target} is outside {root}") from exc
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break  # not created yet — nothing to follow
        if stat.S_ISLNK(mode):
            raise AssetBoundaryError(f"{current} is a symlink (path escape)")
        if not stat.S_ISDIR(mode):
            raise AssetBoundaryError(f"{current} is not a directory")


class AssetLibrary:
    """SHA-256 content-addressed blob store rooted at ``root/cas``."""

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._root = Path(root)
        self._cas = self._root / "cas"
        self._incoming = self._cas / "incoming"
        self._max_bytes = max_bytes

    # ── layout ────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    def key_for(self, sha256: str) -> str:
        return _key_of(_hex_of(sha256))

    def path_for(self, sha256: str) -> Path:
        return self._root / self.key_for(sha256)

    def exists(self, sha256: str) -> bool:
        try:
            return stat.S_ISREG(os.lstat(self.path_for(sha256)).st_mode)
        except FileNotFoundError:
            return False

    def _ensure_dirs(self) -> None:
        for directory in (self._root, self._cas, self._incoming):
            _reject_symlink_components(self._root, directory / "_")
            directory.mkdir(parents=True, exist_ok=True)

    # ── admission ─────────────────────────────────────────────────────

    def admit(
        self, source: Path, *, attempt_id: str, declared_mime: str | None = None
    ) -> AdmittedAsset:
        """Copy *source* into the store (dedup by content) and describe the result."""
        if not _ATTEMPT.match(attempt_id):
            raise AssetBoundaryError(f"unsafe attempt id {attempt_id!r}")
        fd = self._open_regular_source(Path(source))
        try:
            with os.fdopen(fd, "rb", closefd=True) as stream:
                return self._admit_stream(
                    stream, attempt_id=attempt_id, declared_mime=declared_mime
                )
        except OSError as exc:
            raise AssetLibraryError(f"cannot read {source}: {exc}") from exc

    def admit_bytes(
        self, data: bytes, *, attempt_id: str, declared_mime: str | None = None
    ) -> AdmittedAsset:
        if not _ATTEMPT.match(attempt_id):
            raise AssetBoundaryError(f"unsafe attempt id {attempt_id!r}")
        return self._admit_stream(
            io.BytesIO(data), attempt_id=attempt_id, declared_mime=declared_mime
        )

    def _open_regular_source(self, source: Path) -> int:
        flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC | _O_BINARY
        try:
            fd = os.open(source, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AssetBoundaryError(f"{source} is a symlink") from exc
            raise AssetLibraryError(f"cannot open {source}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise AssetBoundaryError(f"{source} is not a regular file")
            if info.st_size > self._max_bytes:
                raise AssetTooLargeError(f"{source} exceeds {self._max_bytes} bytes")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _admit_stream(
        self, stream: BinaryIO, *, attempt_id: str, declared_mime: str | None
    ) -> AdmittedAsset:
        self._ensure_dirs()
        part = self._incoming / f"{attempt_id}-{secrets.token_hex(8)}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY
        digest = hashlib.sha256()
        size = 0
        head = b""
        pfd = os.open(part, flags, 0o600)
        try:
            with os.fdopen(pfd, "wb", closefd=True) as out:
                while chunk := stream.read(_CHUNK):
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise AssetTooLargeError(f"source exceeds {self._max_bytes} bytes")
                    if len(head) < 64:
                        head += chunk[: 64 - len(head)]
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            hex_digest = digest.hexdigest()
            final = self._root / _key_of(hex_digest)
            _reject_symlink_components(self._root, final)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(part, 0o444)
            deduplicated = self._publish(part, final, hex_digest, size)
            _fsync_dir(final.parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(part)
        mime = sniff_mime(head)
        return AdmittedAsset(
            sha256=f"sha256:{hex_digest}",
            size_bytes=size,
            storage_key=_key_of(hex_digest),
            mime=mime,
            declared_mime=declared_mime,
            mime_mismatch=bool(declared_mime) and declared_mime != mime,
            deduplicated=deduplicated,
            attempt_id=attempt_id,
        )

    def _publish(self, part: Path, final: Path, hex_digest: str, size: int) -> bool:
        """Link the staged file to its address; True when an identical blob already existed."""
        try:
            os.link(part, final)
        except FileExistsError:
            exists = True
        except OSError as exc:
            raise AssetLibraryError(f"cannot publish blob: {exc}") from exc
        else:
            return False
        if not exists:  # pragma: no cover - defensive
            return False
        existing_hex, existing_size = self._measure(final)
        if (existing_hex, existing_size) != (hex_digest, size):
            raise AssetIntegrityError(
                f"blob at {final.name[:12]}… does not match its address (corrupt store)"
            )
        return True

    # ── reading / verification ────────────────────────────────────────

    def _measure(self, path: Path) -> tuple[str, int]:
        flags = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC | _O_BINARY
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise AssetBoundaryError(f"{path} is not a regular file")
        except BaseException:
            os.close(fd)
            raise
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb", closefd=True) as stream:
            while chunk := stream.read(_CHUNK):
                size += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), size

    def verify(self, sha256: str) -> int:
        """Re-hash the blob; return its size or raise (missing → AssetLibraryError)."""
        hex_digest = _hex_of(sha256)
        path = self._root / _key_of(hex_digest)
        try:
            measured, size = self._measure(path)
        except FileNotFoundError as exc:
            raise AssetLibraryError(f"blob {hex_digest[:12]}… is missing") from exc
        if measured != hex_digest:
            raise AssetIntegrityError(f"blob {hex_digest[:12]}… is corrupt")
        return size

    @contextlib.contextmanager
    def open_verified(self, sha256: str) -> Iterator[BinaryIO]:
        """Open a blob for reading after proving its bytes still match its address."""
        self.verify(sha256)
        path = self.path_for(sha256)
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY)
        with os.fdopen(fd, "rb", closefd=True) as stream:
            yield stream

    # ── reconciliation / retention ────────────────────────────────────

    def _blobs(self) -> Iterator[tuple[str, Path]]:
        base = self._cas / "sha256"
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*")):
            if path.is_dir() or path.is_symlink():
                if path.is_symlink():
                    yield "unexpected:" + str(path.relative_to(self._root)), path
                continue
            name = path.name
            expected = base / name[:2] / name[2:4] / name
            if not _HEX64.match(name) or path != expected:
                yield "unexpected:" + str(path.relative_to(self._root)), path
                continue
            yield _key_of(name), path

    def fsck(self, referenced: Iterable[str], *, min_age_seconds: float = 3600) -> FsckReport:
        """Reconcile disk against *referenced* storage keys / sha256 addresses."""
        wanted = {self._normalise_ref(ref) for ref in referenced}
        on_disk: dict[str, Path] = {}
        unexpected: list[str] = []
        for key, path in self._blobs():
            if key.startswith("unexpected:"):
                unexpected.append(key.removeprefix("unexpected:"))
            else:
                on_disk[key] = path
        corrupt = [
            key
            for key, path in on_disk.items()
            if self._safe_measure(path) != (path.name, path.stat().st_size)
        ]
        missing = sorted(wanted - on_disk.keys())
        orphans = sorted(on_disk.keys() - wanted)
        stale = [str(p.relative_to(self._root)) for p in self._stale_parts(min_age_seconds)]
        return FsckReport(
            checked=len(on_disk),
            missing=tuple(missing),
            orphans=tuple(orphans),
            corrupt=tuple(sorted(corrupt)),
            stale_incoming=tuple(stale),
            unexpected=tuple(unexpected),
        )

    def gc(self, referenced: Iterable[str], *, min_age_seconds: float = 3600) -> tuple[str, ...]:
        """Delete unreferenced blobs and stale ``.part`` files older than *min_age_seconds*.

        Referenced blobs are never touched — even when corrupt (that is an
        incident to report, not to erase).  Returns the removed keys.
        """
        wanted = {self._normalise_ref(ref) for ref in referenced}
        cutoff = time.time() - min_age_seconds
        removed: list[str] = []
        for key, path in self._blobs():
            if key.startswith("unexpected:") or key in wanted:
                continue
            with contextlib.suppress(FileNotFoundError):
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
                    removed.append(key)
        for part in self._stale_parts(min_age_seconds):
            with contextlib.suppress(FileNotFoundError):
                part.unlink()
                removed.append(str(part.relative_to(self._root)))
        return tuple(removed)

    def _stale_parts(self, min_age_seconds: float) -> list[Path]:
        if not self._incoming.is_dir():
            return []
        cutoff = time.time() - min_age_seconds
        stale: list[Path] = []
        for part in sorted(self._incoming.glob("*.part")):
            with contextlib.suppress(FileNotFoundError):
                if not part.is_symlink() and part.stat().st_mtime <= cutoff:
                    stale.append(part)
        return stale

    def _safe_measure(self, path: Path) -> tuple[str, int]:
        try:
            return self._measure(path)
        except (OSError, AssetLibraryError):
            return ("", -1)

    @staticmethod
    def _normalise_ref(ref: str) -> str:
        if ref.startswith("cas/sha256/"):
            return _key_of(_hex_of(ref.rsplit("/", 1)[-1]))
        return _key_of(_hex_of(ref))
