"""Zero-dependency BYOS (Bring Your Own Storage) vault drivers.

Provides the :class:`BYOSStorageDriver` protocol and three stdlib-only drivers:

1. :class:`LocalDeviceVaultDriver` — atomic (``.tmp`` + ``fsync`` + ``os.replace``)
   storage on a local folder or external drive with strict ``max_quota_bytes``
   enforcement.
2. :class:`S3CompatibleSigV4Driver` — pure-stdlib AWS Signature Version 4
   (``AWS4-HMAC-SHA256``) client compatible with Cloudflare R2, AWS S3, and MinIO,
   supporting injectable ``http_transport`` for deterministic offline testing.
3. :class:`PortableBundleVaultDriver` — single-file ``.nagarvault`` binary archive
   with binary index table, per-entry CRC32 + SHA-256, and full-archive SHA-256
   verification for air-gapped / offline cross-device transfer.
"""

from __future__ import annotations

import contextlib
import email.utils
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
    VaultSnapshotManifest,
)
from nexus_ai_agent.storage.personal_cloud_vault.errors import (
    PersonalVaultError,
    VaultAuthenticationError,
    VaultQuotaExceededError,
    VaultRevisionConflictError,
    VaultTamperDetectedError,
    VaultTransportError,
)

_CHUNK_ID_RE = re.compile(r"^[0-9a-f]{16,128}$")
_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validate_chunk_id(chunk_id: str) -> str:
    if not _CHUNK_ID_RE.fullmatch(chunk_id):
        raise PersonalVaultError(f"Invalid chunk_id format: {chunk_id!r}")
    return chunk_id


def _validate_snapshot_id(snapshot_id: str) -> str:
    if not _SNAPSHOT_ID_RE.fullmatch(snapshot_id) or ".." in snapshot_id:
        raise PersonalVaultError(f"Invalid snapshot_id format: {snapshot_id!r}")
    return snapshot_id


def _validate_positive_finite_seconds(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PersonalVaultError(f"{name} must be a finite positive number, got {value!r}")
    as_float = float(value)
    if not math.isfinite(as_float) or as_float <= 0.0:
        raise PersonalVaultError(f"{name} must be finite and positive, got {value!r}")
    return as_float


def _validate_finite_epoch(value: float | None, *, default_now: float) -> float:
    if value is None:
        return default_now
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PersonalVaultError(f"now_epoch must be a finite number, got {value!r}")
    as_float = float(value)
    if not math.isfinite(as_float):
        raise PersonalVaultError(f"now_epoch must be finite, got {value!r}")
    return as_float


def _build_lease_json_bytes(
    *,
    lease_id: str,
    project_id: str,
    now_epoch: float,
    ttl_seconds: float,
    chunk_ids: Iterable[str],
) -> bytes:
    validated_chunks = sorted({_validate_chunk_id(cid) for cid in chunk_ids})
    # ``ttl_seconds`` is validated finite/positive by every caller, so the computed
    # expiration can never become non-finite (float("inf") is not reachable by rounding).
    expires_at = now_epoch + ttl_seconds
    return json.dumps(
        {
            "lease_id": lease_id,
            "pid": os.getpid(),
            "project_id": project_id,
            "created_at_epoch": now_epoch,
            "expires_at_epoch": expires_at,
            "ttl_seconds": ttl_seconds,
            "chunk_ids": validated_chunks,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_and_validate_lease_payload(
    raw: bytes,
    *,
    source_label: str,
) -> tuple[float, set[str]]:
    """Parse a lease document fail-closed, raising :class:`VaultTransportError` if corrupt."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise VaultTransportError(f"Corrupted push lease JSON in {source_label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise VaultTransportError(
            f"Malformed push lease payload in {source_label}: expected JSON object"
        )
    raw_exp = parsed.get("expires_at_epoch")
    if isinstance(raw_exp, bool) or not isinstance(raw_exp, (int, float)):
        raise VaultTransportError(
            f"Missing or non-numeric expires_at_epoch in push lease {source_label}"
        )
    expires_at = float(raw_exp)
    if not math.isfinite(expires_at):
        raise VaultTransportError(
            f"Non-finite expires_at_epoch in push lease {source_label}: {expires_at!r}"
        )
    raw_ids = parsed.get("chunk_ids")
    if not isinstance(raw_ids, list):
        raise VaultTransportError(f"Malformed chunk_ids in push lease {source_label}: not a list")
    chunk_set: set[str] = set()
    for cid in raw_ids:
        if not isinstance(cid, str) or not _CHUNK_ID_RE.fullmatch(cid):
            raise VaultTransportError(f"Invalid chunk_id {cid!r} in push lease {source_label}")
        chunk_set.add(cid)
    return expires_at, chunk_set


def _parse_http_or_iso_timestamp(raw_ts: str) -> float | None:
    """Parse RFC 7231 HTTP ``Last-Modified`` or ISO-8601 S3 ``<LastModified>`` string."""
    cleaned = raw_ts.strip()
    if not cleaned:
        return None
    with contextlib.suppress(TypeError, ValueError):
        iso_norm = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
        dt_iso = datetime.fromisoformat(iso_norm)
        if dt_iso.tzinfo is None:
            dt_iso = dt_iso.replace(tzinfo=timezone.utc)
        return dt_iso.timestamp()
    with contextlib.suppress(TypeError, ValueError):
        dt_http = email.utils.parsedate_to_datetime(cleaned)
        if dt_http.tzinfo is None:
            dt_http = dt_http.replace(tzinfo=timezone.utc)
        return dt_http.timestamp()
    return None


def _atomic_write_file(target_path: Path, data: bytes) -> None:
    """Atomically persist ``data`` to ``target_path`` via ``.tmp`` + ``fsync`` + ``replace``."""
    parent = target_path.parent
    tmp_name = f".{target_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    tmp_path = parent / tmp_name
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target_path)
        dir_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise VaultTransportError(f"Atomic file write failed for {target_path}: {exc}") from exc


@runtime_checkable
class BYOSStorageDriver(Protocol):
    """Protocol implemented by all user-owned Personal Cloud & Local Vault backends."""

    def put_chunk(self, chunk_id: str, data: bytes) -> None:
        """Persist an encrypted chunk envelope under ``chunk_id``."""

    def get_chunk(self, chunk_id: str) -> bytes:
        """Retrieve raw encrypted chunk bytes for ``chunk_id``."""

    def has_chunk(self, chunk_id: str) -> bool:
        """Return ``True`` when ``chunk_id`` already exists on the target store."""

    def put_manifest(self, snapshot_id: str, data: bytes) -> None:
        """Persist raw manifest bytes for ``snapshot_id``."""

    def get_manifest(self, snapshot_id: str) -> bytes:
        """Retrieve raw manifest bytes for ``snapshot_id``."""

    def list_manifests(self) -> list[str]:
        """Return sorted ``snapshot_id`` identifiers stored on the backend."""

    def delete_chunk(self, chunk_id: str) -> None:
        """Delete an unreferenced encrypted chunk identified by ``chunk_id``."""


@dataclass(frozen=True)
class VaultStagingJournal:
    """Crash-recovery staging journal tracking non-secret KDF params and uploaded chunks."""

    project_id: str
    parent_snapshot_id: str | None
    salt_hex: str
    kdf_iterations: int
    key_id_hex: str
    chunk_size: int
    updated_at_epoch: float
    uploaded_chunk_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": self.project_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "salt_hex": self.salt_hex,
            "kdf_iterations": self.kdf_iterations,
            "key_id_hex": self.key_id_hex,
            "chunk_size": self.chunk_size,
            "updated_at_epoch": self.updated_at_epoch,
            "uploaded_chunk_ids": list(self.uploaded_chunk_ids),
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> VaultStagingJournal:
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
            raise PersonalVaultError("Unsupported staging journal schema")
        raw_parent = parsed.get("parent_snapshot_id")
        parent_id = None if raw_parent is None else str(raw_parent)
        raw_chunks = parsed["uploaded_chunk_ids"]
        if not isinstance(raw_chunks, list):
            raise PersonalVaultError("Staging journal uploaded_chunk_ids must be a list")
        validated_chunks = tuple(_validate_chunk_id(str(cid)) for cid in raw_chunks)
        salt_hex = str(parsed["salt_hex"])
        key_id_hex = str(parsed["key_id_hex"])
        bytes.fromhex(salt_hex)
        bytes.fromhex(key_id_hex)
        return cls(
            project_id=str(parsed["project_id"]),
            parent_snapshot_id=parent_id,
            salt_hex=salt_hex,
            kdf_iterations=int(parsed["kdf_iterations"]),
            key_id_hex=key_id_hex,
            chunk_size=int(parsed["chunk_size"]),
            updated_at_epoch=float(parsed["updated_at_epoch"]),
            uploaded_chunk_ids=validated_chunks,
        )


# --------------------------------------------------------------------------- #
# 1. LocalDeviceVaultDriver
# --------------------------------------------------------------------------- #


class LocalDeviceVaultDriver:
    """Atomic local-folder / external-drive BYOS vault driver with quota enforcement."""

    def __init__(
        self,
        root_dir: Path | str,
        *,
        max_quota_bytes: int | None = None,
    ) -> None:
        if max_quota_bytes is not None and max_quota_bytes <= 0:
            raise PersonalVaultError(
                f"max_quota_bytes must be positive when set, got {max_quota_bytes}"
            )
        raw_root = Path(root_dir)
        if raw_root.is_symlink():
            raise VaultTransportError(f"Vault root directory must not be a symlink: {raw_root}")
        self._root_dir = raw_root.resolve()
        self._chunks_dir = self._root_dir / "chunks"
        self._manifests_dir = self._root_dir / "manifests"
        self._leases_dir = self._root_dir / ".leases"
        self._staging_dir = self._root_dir / ".staging"
        self._max_quota_bytes = max_quota_bytes

        try:
            self._chunks_dir.mkdir(parents=True, exist_ok=True)
            self._manifests_dir.mkdir(parents=True, exist_ok=True)
            self._leases_dir.mkdir(parents=True, exist_ok=True)
            self._staging_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VaultTransportError(f"Cannot initialize local vault directory: {exc}") from exc

    @property
    def root_dir(self) -> Path:
        return self._root_dir

    @property
    def max_quota_bytes(self) -> int | None:
        return self._max_quota_bytes

    def used_bytes(self) -> int:
        """Compute total bytes currently stored across chunks and manifests."""
        total = 0
        for folder in (self._chunks_dir, self._manifests_dir):
            for entry in folder.iterdir():
                if entry.name.startswith("."):
                    continue
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
        return total

    def _enforce_quota(self, target_path: Path, incoming_size: int) -> None:
        if self._max_quota_bytes is None:
            return
        existing_size = target_path.stat().st_size if target_path.is_file() else 0
        projected = self.used_bytes() - existing_size + incoming_size
        if projected > self._max_quota_bytes:
            raise VaultQuotaExceededError(
                f"Local vault quota exceeded: projected {projected} bytes > "
                f"max_quota_bytes {self._max_quota_bytes}"
            )

    def _chunk_path(self, chunk_id: str) -> Path:
        valid = _validate_chunk_id(chunk_id)
        return self._chunks_dir / f"{valid}.chk"

    def _manifest_path(self, snapshot_id: str) -> Path:
        valid = _validate_snapshot_id(snapshot_id)
        return self._manifests_dir / f"{valid}.json"

    def put_chunk(self, chunk_id: str, data: bytes) -> None:
        path = self._chunk_path(chunk_id)
        self._enforce_quota(path, len(data))
        _atomic_write_file(path, data)

    def get_chunk(self, chunk_id: str) -> bytes:
        path = self._chunk_path(chunk_id)
        if path.is_symlink() or not path.is_file():
            raise VaultTransportError(f"Chunk not found in local vault: {chunk_id}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise VaultTransportError(f"Failed reading chunk {chunk_id}: {exc}") from exc

    def has_chunk(self, chunk_id: str) -> bool:
        path = self._chunk_path(chunk_id)
        return path.is_file() and not path.is_symlink()

    def get_chunk_mtime(self, chunk_id: str) -> float | None:
        """Return POSIX modification timestamp of ``chunk_id``, or ``None`` if absent."""
        path = self._chunk_path(chunk_id)
        if path.is_symlink() or not path.is_file():
            return None
        return path.stat().st_mtime

    def put_manifest(self, snapshot_id: str, data: bytes) -> None:
        path = self._manifest_path(snapshot_id)
        self._enforce_quota(path, len(data))
        _atomic_write_file(path, data)

    def get_manifest(self, snapshot_id: str) -> bytes:
        path = self._manifest_path(snapshot_id)
        if path.is_symlink() or not path.is_file():
            raise VaultTransportError(f"Manifest not found in local vault: {snapshot_id}")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise VaultTransportError(f"Failed reading manifest {snapshot_id}: {exc}") from exc

    def list_manifests(self) -> list[str]:
        manifests: list[str] = []
        for entry in self._manifests_dir.iterdir():
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".json":
                if not entry.name.startswith("."):
                    manifests.append(entry.stem)
        return sorted(manifests)

    def list_chunks(self) -> list[str]:
        """Return sorted chunk IDs currently stored in the local vault."""
        chunks: list[str] = []
        for entry in self._chunks_dir.iterdir():
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".chk":
                if not entry.name.startswith("."):
                    chunks.append(entry.stem)
        return sorted(chunks)

    def delete_chunk(self, chunk_id: str) -> None:
        path = self._chunk_path(chunk_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise VaultTransportError(f"Failed deleting chunk {chunk_id}: {exc}") from exc

    def delete_manifest(self, snapshot_id: str) -> None:
        """Delete a snapshot manifest file from the local vault."""
        path = self._manifest_path(snapshot_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise VaultTransportError(f"Failed deleting manifest {snapshot_id}: {exc}") from exc

    def _lease_path(self, lease_id: str) -> Path:
        valid = _validate_snapshot_id(lease_id)
        return self._leases_dir / f"lease-{valid}.json"

    @contextlib.contextmanager
    def _coordination_lock(
        self,
        *,
        timeout_seconds: float = 10.0,
        stale_lock_seconds: float = 30.0,
    ) -> Iterator[None]:
        """Short-lived cross-process mutex (``O_CREAT | O_EXCL``) closing GC/Lease TOCTOU."""
        lock_path = self._leases_dir / ".gc_coord.lock"
        deadline = time.monotonic() + timeout_seconds
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        while True:
            try:
                fd = os.open(lock_path, flags, 0o600)
                try:
                    os.write(fd, f"{os.getpid()}:{time.time():.6f}".encode("ascii"))
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                with contextlib.suppress(OSError):
                    st = lock_path.lstat()
                    if (time.time() - st.st_mtime) > stale_lock_seconds:
                        lock_path.unlink(missing_ok=True)
                        continue
                if time.monotonic() >= deadline:
                    raise VaultTransportError(
                        f"Timed out acquiring vault GC coordination lock: {lock_path}"
                    ) from None
                time.sleep(0.002)
            except OSError as exc:
                raise VaultTransportError(
                    f"Failed acquiring vault GC coordination lock {lock_path}: {exc}"
                ) from exc
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink(missing_ok=True)

    def acquire_push_lease(
        self,
        chunk_ids: Iterable[str] = (),
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
        lease_id: str | None = None,
    ) -> str:
        """Atomically create a cross-process lease file via ``O_CREAT | O_EXCL | O_WRONLY``."""
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        now = time.time()
        effective_id = (
            f"{os.getpid()}-{int(now * 1000)}-{secrets.token_hex(6)}"
            if lease_id is None
            else _validate_snapshot_id(lease_id)
        )
        lease_path = self._lease_path(effective_id)
        payload = _build_lease_json_bytes(
            lease_id=effective_id,
            project_id=project_id,
            now_epoch=now,
            ttl_seconds=finite_ttl,
            chunk_ids=chunk_ids,
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        with self._coordination_lock():
            try:
                fd = os.open(lease_path, flags, 0o600)
            except FileExistsError as exc:
                raise VaultRevisionConflictError(
                    f"Push lease {effective_id!r} already exists in {self._leases_dir}"
                ) from exc
            except OSError as exc:
                raise VaultTransportError(
                    f"Failed creating push lease {lease_path}: {exc}"
                ) from exc
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        return effective_id

    def update_push_lease(
        self,
        lease_id: str,
        chunk_ids: Iterable[str],
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
    ) -> None:
        """Atomically refresh TTL and in-flight ``chunk_ids`` on an active push lease."""
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        valid_id = _validate_snapshot_id(lease_id)
        lease_path = self._lease_path(valid_id)
        now = time.time()
        payload = _build_lease_json_bytes(
            lease_id=valid_id,
            project_id=project_id,
            now_epoch=now,
            ttl_seconds=finite_ttl,
            chunk_ids=chunk_ids,
        )
        with self._coordination_lock():
            if lease_path.is_symlink() or not lease_path.is_file():
                raise VaultTransportError(f"Active push lease not found: {lease_id}")
            _atomic_write_file(lease_path, payload)

    def release_push_lease(self, lease_id: str) -> None:
        """Release and unlink a cross-process push lease file."""
        lease_path = self._lease_path(lease_id)
        with self._coordination_lock(), contextlib.suppress(OSError):
            lease_path.unlink(missing_ok=True)

    def _read_active_leased_chunks_unlocked(self, *, effective_now: float) -> set[str]:
        active_chunks: set[str] = set()
        for entry in sorted(self._leases_dir.iterdir()):
            if entry.name.startswith("."):
                continue
            if (
                entry.is_symlink()
                or not entry.is_file()
                or not entry.name.startswith("lease-")
                or entry.suffix != ".json"
            ):
                raise VaultTransportError(
                    f"Unexpected or symlinked file in vault lease directory: {entry}"
                )
            try:
                raw_bytes = entry.read_bytes()
            except OSError as exc:
                raise VaultTransportError(f"Failed reading push lease file {entry}: {exc}") from exc
            expires_at, chunk_set = _parse_and_validate_lease_payload(
                raw_bytes,
                source_label=str(entry),
            )
            if expires_at <= effective_now:
                with contextlib.suppress(OSError):
                    entry.unlink(missing_ok=True)
                continue
            active_chunks.update(chunk_set)
        return active_chunks

    def read_active_leased_chunks(self, *, now_epoch: float | None = None) -> set[str]:
        """Read unexpired in-flight chunk IDs fail-closed across cross-process lease files."""
        effective_now = _validate_finite_epoch(now_epoch, default_now=time.time())
        with self._coordination_lock():
            return self._read_active_leased_chunks_unlocked(effective_now=effective_now)

    def delete_chunk_if_unleased(
        self,
        chunk_id: str,
        *,
        now_epoch: float | None = None,
    ) -> bool:
        """Atomically re-verify leases under ``.gc_coord.lock`` and delete ``chunk_id``."""
        valid = _validate_chunk_id(chunk_id)
        effective_now = _validate_finite_epoch(now_epoch, default_now=time.time())
        with self._coordination_lock():
            active_leased = self._read_active_leased_chunks_unlocked(effective_now=effective_now)
            if valid in active_leased or not self.has_chunk(valid):
                return False
            self.delete_chunk(valid)
            return True

    def _staging_journal_path(self, project_id: str, parent_snapshot_id: str | None) -> Path:
        key_bytes = f"{project_id}\x00{parent_snapshot_id or ''}".encode()
        digest = hashlib.sha256(key_bytes).hexdigest()[:32]
        return self._staging_dir / f"stage-{digest}.json"

    def save_staging_journal(self, journal: VaultStagingJournal) -> None:
        """Atomically persist a crash-recovery staging journal."""
        path = self._staging_journal_path(journal.project_id, journal.parent_snapshot_id)
        _atomic_write_file(path, journal.to_json_bytes())

    def load_staging_journal(
        self,
        project_id: str,
        parent_snapshot_id: str | None,
    ) -> VaultStagingJournal | None:
        """Load an active crash-recovery staging journal if present and valid."""
        path = self._staging_journal_path(project_id, parent_snapshot_id)
        if path.is_symlink() or not path.is_file():
            return None
        try:
            journal = VaultStagingJournal.from_json_bytes(path.read_bytes())
        except (OSError, ValueError, KeyError, TypeError, PersonalVaultError):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        if journal.project_id != project_id or journal.parent_snapshot_id != parent_snapshot_id:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return None
        return journal

    def clear_staging_journal(self, project_id: str, parent_snapshot_id: str | None) -> None:
        """Remove a completed staging journal."""
        path = self._staging_journal_path(project_id, parent_snapshot_id)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 2. S3CompatibleSigV4Driver (AWS4-HMAC-SHA256 — R2 / AWS S3 / MinIO)
# --------------------------------------------------------------------------- #


def _sigv4_uri_encode(value: str, *, encode_slash: bool) -> str:
    safe_chars = "-_.~" if encode_slash else "/-_.~"
    return urllib.parse.quote(value, safe=safe_chars)


def _hmac_sha256_bytes(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


@dataclass(frozen=True)
class SigV4Artifacts:
    """Structured AWS Signature Version 4 signing outputs for auditing and tests."""

    canonical_request: str
    string_to_sign: str
    signature: str
    authorization_header: str
    signed_headers: dict[str, str]


@dataclass(frozen=True)
class S3SigV4Config:
    """Connection and credential settings for an S3-compatible user bucket."""

    endpoint_url: str
    bucket: str
    region: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    prefix: str = "nagar-vault"
    service: str = "s3"
    path_style: bool = True
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.endpoint_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PersonalVaultError(
                f"endpoint_url must be a valid http(s) URL, got {self.endpoint_url!r}"
            )
        if not self.bucket or "/" in self.bucket:
            raise PersonalVaultError(f"Invalid S3 bucket name: {self.bucket!r}")
        if not self.region.strip():
            raise PersonalVaultError("S3 region must not be empty")
        if not self.access_key_id.strip() or not self.secret_access_key:
            raise VaultAuthenticationError("S3 access_key_id and secret_access_key are required")


@dataclass(frozen=True)
class S3HttpRequest:
    """Immutable HTTP request passed to ``http_transport``."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes = b""


@dataclass(frozen=True)
class S3HttpResponse:
    """Immutable HTTP response returned by ``http_transport``."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


HttpTransportCallable = Callable[[S3HttpRequest], S3HttpResponse]


def compute_sigv4_signature(
    *,
    method: str,
    url: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    service: str = "s3",
    payload: bytes = b"",
    extra_headers: Mapping[str, str] | None = None,
    session_token: str | None = None,
    timestamp: datetime | None = None,
) -> SigV4Artifacts:
    """Compute standard ``AWS4-HMAC-SHA256`` request signature using Python stdlib."""
    if not access_key_id or not secret_access_key:
        raise VaultAuthenticationError("AWS SigV4 credentials must not be empty")

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PersonalVaultError(f"SigV4 target must be an http(s) URL, got {url!r}")

    now_utc = (
        datetime.now(timezone.utc) if timestamp is None else timestamp.astimezone(timezone.utc)
    )
    amz_date = now_utc.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now_utc.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    raw_path = parsed.path if parsed.path else "/"
    unquoted_path = urllib.parse.unquote(raw_path)
    canonical_uri = _sigv4_uri_encode(unquoted_path, encode_slash=False)

    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    encoded_pairs = sorted(
        (
            _sigv4_uri_encode(key, encode_slash=True),
            _sigv4_uri_encode(val, encode_slash=True),
        )
        for key, val in query_pairs
    )
    canonical_query = "&".join(f"{key}={val}" for key, val in encoded_pairs)

    header_map: dict[str, str] = {
        "host": parsed.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        header_map["x-amz-security-token"] = session_token
    if extra_headers:
        for key, value in extra_headers.items():
            normalized_val = " ".join(value.strip().split())
            header_map[key.strip().lower()] = normalized_val

    sorted_header_items = sorted(header_map.items(), key=lambda item: item[0])
    canonical_headers = "".join(f"{key}:{val}\n" for key, val in sorted_header_items)
    signed_header_names = ";".join(key for key, _ in sorted_header_items)

    http_method = method.upper()
    canonical_request = "\n".join(
        [
            http_method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_header_names,
            payload_hash,
        ]
    )
    canonical_request_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            canonical_request_hash,
        ]
    )

    k_date = _hmac_sha256_bytes(f"AWS4{secret_access_key}".encode(), date_stamp)
    k_region = _hmac_sha256_bytes(k_date, region)
    k_service = _hmac_sha256_bytes(k_region, service)
    k_signing = _hmac_sha256_bytes(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )

    final_headers = dict(sorted_header_items)
    final_headers["Authorization"] = authorization_header
    return SigV4Artifacts(
        canonical_request=canonical_request,
        string_to_sign=string_to_sign,
        signature=signature,
        authorization_header=authorization_header,
        signed_headers=final_headers,
    )


def _default_urllib_transport(request: S3HttpRequest, *, timeout: float) -> S3HttpResponse:
    """Execute ``request`` using Python's stdlib :mod:`urllib.request`."""
    parsed = urllib.parse.urlsplit(request.url)
    if parsed.scheme not in ("http", "https"):
        raise VaultTransportError(f"Disallowed URL scheme: {parsed.scheme!r}")
    data = request.body if request.method in ("PUT", "POST") else None
    req = urllib.request.Request(
        url=request.url,
        data=data,
        headers=request.headers,
        method=request.method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return S3HttpResponse(
                status_code=int(resp.status),
                headers=resp_headers,
                body=resp.read(),
            )
    except urllib.error.HTTPError as exc:
        err_body = exc.read() if exc.fp is not None else b""
        err_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return S3HttpResponse(
            status_code=int(exc.code),
            headers=err_headers,
            body=err_body,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VaultTransportError(f"S3 HTTP transport error: {exc}") from exc


_XML_ENTITY_REF_RE = re.compile(rb"&([a-zA-Z_][a-zA-Z0-9._-]*);")
_ALLOWED_XML_ENTITIES = frozenset({b"amp", b"lt", b"gt", b"quot", b"apos"})
_FORBIDDEN_XML_TOKENS = (
    b"<!doctype",
    b"<!entity",
    b"<!element",
    b"<!attlist",
    b"<!notation",
    b"<![cdata[",
)


def _parse_s3_list_xml_entries(
    xml_bytes: bytes,
) -> tuple[list[tuple[str, float | None]], str | None]:
    """Safely parse S3 ``ListObjectsV2`` XML keys and optional ``<LastModified>`` timestamps."""
    if b"\x00" in xml_bytes:
        raise VaultTransportError("NUL byte forbidden in S3 ListObjectsV2 XML response")
    lowered = xml_bytes.lower()
    for token in _FORBIDDEN_XML_TOKENS:
        if token in lowered:
            raise VaultTransportError(
                f"Forbidden XML construct {token.decode('ascii')!r} in S3 ListObjectsV2 response"
            )
    for match in _XML_ENTITY_REF_RE.finditer(lowered):
        if match.group(1) not in _ALLOWED_XML_ENTITIES:
            raise VaultTransportError(
                "Forbidden custom XML entity reference in S3 ListObjectsV2 response"
            )
    try:
        root = ET.fromstring(xml_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise VaultTransportError("Malformed XML in S3 ListObjectsV2 response") from exc

    entries: list[tuple[str, float | None]] = []
    is_truncated = False
    next_token: str | None = None

    for child in root:
        local_child = child.tag.rsplit("}", 1)[-1]
        if local_child == "Contents":
            item_key: str | None = None
            item_mtime: float | None = None
            for sub in child:
                sub_name = sub.tag.rsplit("}", 1)[-1]
                if sub_name == "Key" and sub.text:
                    item_key = sub.text
                elif sub_name == "LastModified" and sub.text:
                    item_mtime = _parse_http_or_iso_timestamp(sub.text)
            if item_key is not None:
                entries.append((item_key, item_mtime))
        elif local_child == "IsTruncated" and child.text:
            is_truncated = child.text.strip().lower() == "true"
        elif local_child == "NextContinuationToken" and child.text:
            next_token = child.text.strip()

    return entries, (next_token if is_truncated else None)


def _parse_s3_list_xml(xml_bytes: bytes) -> tuple[list[str], str | None]:
    """Safely parse S3 ``ListObjectsV2`` XML response without DTD/entity/CDATA constructs."""
    entries, next_token = _parse_s3_list_xml_entries(xml_bytes)
    return [key for key, _ in entries], next_token


class S3CompatibleSigV4Driver:
    """Direct user-bucket BYOS driver (Cloudflare R2 / AWS S3 / MinIO) via SigV4."""

    def __init__(
        self,
        config: S3SigV4Config,
        *,
        http_transport: HttpTransportCallable | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._transport: HttpTransportCallable = (
            (lambda req: _default_urllib_transport(req, timeout=config.timeout_seconds))
            if http_transport is None
            else http_transport
        )
        self._clock: Callable[[], datetime] = (
            (lambda: datetime.now(timezone.utc)) if clock is None else clock
        )
        self._clean_prefix = config.prefix.strip("/")
        self._chunk_mtimes: dict[str, float] = {}

    @property
    def config(self) -> S3SigV4Config:
        return self._config

    def _object_key(self, category: str, leaf_name: str) -> str:
        if self._clean_prefix:
            return f"{self._clean_prefix}/{category}/{leaf_name}"
        return f"{category}/{leaf_name}"

    def _build_url(
        self, object_key: str | None = None, query: Mapping[str, str] | None = None
    ) -> str:
        parsed = urllib.parse.urlsplit(self._config.endpoint_url)
        base_path = parsed.path.rstrip("/")
        if self._config.path_style:
            host = parsed.netloc
            if object_key:
                full_path = f"{base_path}/{self._config.bucket}/{object_key}"
            else:
                full_path = f"{base_path}/{self._config.bucket}"
        else:
            host = f"{self._config.bucket}.{parsed.netloc}"
            full_path = f"{base_path}/{object_key}" if object_key else (base_path or "/")

        query_str = urllib.parse.urlencode(sorted(query.items())) if query else ""
        return urllib.parse.urlunsplit((parsed.scheme, host, full_path, query_str, ""))

    def _send(
        self,
        method: str,
        *,
        object_key: str | None = None,
        query: Mapping[str, str] | None = None,
        body: bytes = b"",
        content_type: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> S3HttpResponse:
        url = self._build_url(object_key=object_key, query=query)
        extra: dict[str, str] = dict(extra_headers) if extra_headers else {}
        if content_type is not None:
            extra["content-type"] = content_type
        if method in ("PUT", "POST"):
            extra["content-length"] = str(len(body))

        artifacts = compute_sigv4_signature(
            method=method,
            url=url,
            access_key_id=self._config.access_key_id,
            secret_access_key=self._config.secret_access_key,
            region=self._config.region,
            service=self._config.service,
            payload=body,
            extra_headers=extra,
            session_token=self._config.session_token,
            timestamp=self._clock(),
        )
        request = S3HttpRequest(
            method=method.upper(),
            url=url,
            headers=artifacts.signed_headers,
            body=body,
        )
        response = self._transport(request)
        if response.status_code in (401, 403):
            raise VaultAuthenticationError(
                f"S3 authentication/authorization rejected ({response.status_code}) for {method}"
            )
        if response.status_code == 507:
            raise VaultQuotaExceededError("S3 bucket quota exceeded (HTTP 507)")
        return response

    def put_chunk(self, chunk_id: str, data: bytes) -> None:
        valid = _validate_chunk_id(chunk_id)
        resp = self._send(
            "PUT",
            object_key=self._object_key("chunks", f"{valid}.chk"),
            body=data,
            content_type="application/octet-stream",
        )
        if resp.status_code not in (200, 201, 204):
            raise VaultTransportError(f"S3 put_chunk failed with HTTP {resp.status_code}")
        self._chunk_mtimes[valid] = self._clock().timestamp()

    def get_chunk(self, chunk_id: str) -> bytes:
        valid = _validate_chunk_id(chunk_id)
        resp = self._send("GET", object_key=self._object_key("chunks", f"{valid}.chk"))
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 get_chunk failed for {valid} with HTTP {resp.status_code}"
            )
        return resp.body

    def has_chunk(self, chunk_id: str) -> bool:
        valid = _validate_chunk_id(chunk_id)
        resp = self._send("HEAD", object_key=self._object_key("chunks", f"{valid}.chk"))
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        raise VaultTransportError(f"S3 has_chunk failed for {valid} with HTTP {resp.status_code}")

    def get_chunk_mtime(self, chunk_id: str) -> float | None:
        """Return UTC epoch mtime of ``chunk_id`` from S3 ``Last-Modified`` or metadata."""
        valid = _validate_chunk_id(chunk_id)
        resp = self._send("HEAD", object_key=self._object_key("chunks", f"{valid}.chk"))
        if resp.status_code == 404:
            self._chunk_mtimes.pop(valid, None)
            return None
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 get_chunk_mtime failed for {valid} with HTTP {resp.status_code}"
            )
        lowered_headers = {k.lower(): v for k, v in resp.headers.items()}
        raw_lm = lowered_headers.get("last-modified")
        if raw_lm is not None:
            parsed_ts = _parse_http_or_iso_timestamp(raw_lm)
            if parsed_ts is not None:
                self._chunk_mtimes[valid] = parsed_ts
                return parsed_ts
        return self._chunk_mtimes.get(valid)

    def put_manifest(self, snapshot_id: str, data: bytes) -> None:
        valid = _validate_snapshot_id(snapshot_id)
        resp = self._send(
            "PUT",
            object_key=self._object_key("manifests", f"{valid}.json"),
            body=data,
            content_type="application/json",
        )
        if resp.status_code not in (200, 201, 204):
            raise VaultTransportError(f"S3 put_manifest failed with HTTP {resp.status_code}")

    def get_manifest(self, snapshot_id: str) -> bytes:
        valid = _validate_snapshot_id(snapshot_id)
        resp = self._send("GET", object_key=self._object_key("manifests", f"{valid}.json"))
        if resp.status_code != 200:
            raise VaultTransportError(
                f"S3 get_manifest failed for {valid} with HTTP {resp.status_code}"
            )
        return resp.body

    def _list_category_stems(self, category: str, suffix: str) -> list[str]:
        prefix = self._object_key(category, "")
        stems: list[str] = []
        continuation: str | None = None
        while True:
            query: dict[str, str] = {"list-type": "2", "prefix": prefix}
            if continuation is not None:
                query["continuation-token"] = continuation
            resp = self._send("GET", query=query)
            if resp.status_code != 200:
                raise VaultTransportError(
                    f"S3 list ({category}) failed with HTTP {resp.status_code}"
                )
            entries, continuation = _parse_s3_list_xml_entries(resp.body)
            for key, mtime_val in entries:
                if key.startswith(prefix) and key.endswith(suffix):
                    stem = key[len(prefix) : -len(suffix)]
                    if stem and "/" not in stem:
                        stems.append(stem)
                        if category == "chunks" and mtime_val is not None:
                            self._chunk_mtimes[stem] = mtime_val
            if continuation is None:
                break
        return sorted(set(stems))

    def list_manifests(self) -> list[str]:
        return self._list_category_stems("manifests", ".json")

    def list_chunks(self) -> list[str]:
        """List all chunk IDs stored under this bucket prefix."""
        return self._list_category_stems("chunks", ".chk")

    def delete_chunk(self, chunk_id: str) -> None:
        valid = _validate_chunk_id(chunk_id)
        resp = self._send("DELETE", object_key=self._object_key("chunks", f"{valid}.chk"))
        if resp.status_code not in (200, 202, 204, 404):
            raise VaultTransportError(
                f"S3 delete_chunk failed for {valid} with HTTP {resp.status_code}"
            )
        self._chunk_mtimes.pop(valid, None)

    def acquire_push_lease(
        self,
        chunk_ids: Iterable[str] = (),
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
        lease_id: str | None = None,
    ) -> str:
        """Create a lease object under ``leases/<lease_id>.json`` on the S3 bucket."""
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        now = self._clock().timestamp()
        effective_id = (
            f"{os.getpid()}-{int(now * 1000)}-{secrets.token_hex(6)}"
            if lease_id is None
            else _validate_snapshot_id(lease_id)
        )
        obj_key = self._object_key("leases", f"{effective_id}.json")
        head_resp = self._send("HEAD", object_key=obj_key)
        if head_resp.status_code == 200:
            raise VaultRevisionConflictError(f"S3 push lease {effective_id!r} already exists")
        if head_resp.status_code != 404:
            raise VaultTransportError(
                f"S3 lease probe failed for {effective_id} with HTTP {head_resp.status_code}"
            )
        payload = _build_lease_json_bytes(
            lease_id=effective_id,
            project_id=project_id,
            now_epoch=now,
            ttl_seconds=finite_ttl,
            chunk_ids=chunk_ids,
        )
        put_resp = self._send(
            "PUT",
            object_key=obj_key,
            body=payload,
            content_type="application/json",
            extra_headers={"if-none-match": "*"},
        )
        if put_resp.status_code in (409, 412):
            raise VaultRevisionConflictError(f"S3 push lease {effective_id!r} collision")
        if put_resp.status_code not in (200, 201, 204):
            raise VaultTransportError(
                f"S3 acquire_push_lease failed with HTTP {put_resp.status_code}"
            )
        return effective_id

    def update_push_lease(
        self,
        lease_id: str,
        chunk_ids: Iterable[str],
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
    ) -> None:
        """Refresh ``leases/<lease_id>.json`` on the S3 bucket."""
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        valid_id = _validate_snapshot_id(lease_id)
        obj_key = self._object_key("leases", f"{valid_id}.json")
        head_resp = self._send("HEAD", object_key=obj_key)
        if head_resp.status_code != 200:
            raise VaultTransportError(f"Active S3 push lease not found: {valid_id}")
        now = self._clock().timestamp()
        payload = _build_lease_json_bytes(
            lease_id=valid_id,
            project_id=project_id,
            now_epoch=now,
            ttl_seconds=finite_ttl,
            chunk_ids=chunk_ids,
        )
        put_resp = self._send(
            "PUT",
            object_key=obj_key,
            body=payload,
            content_type="application/json",
        )
        if put_resp.status_code not in (200, 201, 204):
            raise VaultTransportError(
                f"S3 update_push_lease failed with HTTP {put_resp.status_code}"
            )

    def release_push_lease(self, lease_id: str) -> None:
        """Delete ``leases/<lease_id>.json`` from the S3 bucket."""
        valid_id = _validate_snapshot_id(lease_id)
        obj_key = self._object_key("leases", f"{valid_id}.json")
        resp = self._send("DELETE", object_key=obj_key)
        if resp.status_code not in (200, 202, 204, 404):
            raise VaultTransportError(
                f"S3 release_push_lease failed for {valid_id} with HTTP {resp.status_code}"
            )

    def read_active_leased_chunks(self, *, now_epoch: float | None = None) -> set[str]:
        """List and parse all active ``leases/*.json`` objects fail-closed."""
        effective_now = _validate_finite_epoch(now_epoch, default_now=self._clock().timestamp())
        active_chunks: set[str] = set()
        for stem in self._list_category_stems("leases", ".json"):
            obj_key = self._object_key("leases", f"{stem}.json")
            resp = self._send("GET", object_key=obj_key)
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                raise VaultTransportError(
                    f"S3 read_active_leased_chunks failed for {stem} with HTTP {resp.status_code}"
                )
            expires_at, chunk_set = _parse_and_validate_lease_payload(
                resp.body,
                source_label=obj_key,
            )
            if expires_at <= effective_now:
                with contextlib.suppress(VaultTransportError):
                    self.release_push_lease(stem)
                continue
            active_chunks.update(chunk_set)
        return active_chunks

    def delete_chunk_if_unleased(
        self,
        chunk_id: str,
        *,
        now_epoch: float | None = None,
    ) -> bool:
        """Re-check S3 ``leases/*.json`` objects immediately before deleting ``chunk_id``."""
        valid = _validate_chunk_id(chunk_id)
        if valid in self.read_active_leased_chunks(now_epoch=now_epoch):
            return False
        if not self.has_chunk(valid):
            return False
        self.delete_chunk(valid)
        return True


# --------------------------------------------------------------------------- #
# 3. PortableBundleVaultDriver (.nagarvault single-file binary archive)
# --------------------------------------------------------------------------- #

BUNDLE_MAGIC = b"NAGARVLT"
BUNDLE_VERSION = 1
BUNDLE_SUFFIX = ".nagarvault"
_BUNDLE_HEADER_PREFIX = struct.Struct(">8sHIQQ")  # 30 bytes
_BUNDLE_HEADER_TOTAL = _BUNDLE_HEADER_PREFIX.size + 32  # + 32B archive sha256 = 62 bytes
_BUNDLE_INDEX_HEAD = struct.Struct(">BH")
_BUNDLE_INDEX_TAIL = struct.Struct(">QQI32s")
_ENTRY_CHUNK = 1
_ENTRY_MANIFEST = 2
_ENTRY_LEASE = 3


class PortableBundleVaultDriver:
    """Single-file ``.nagarvault`` binary archive driver with integrity verification."""

    def __init__(
        self,
        bundle_path: Path | str,
        *,
        max_quota_bytes: int | None = None,
    ) -> None:
        raw_path = Path(bundle_path)
        if raw_path.suffix != BUNDLE_SUFFIX:
            raise PersonalVaultError(
                f"Portable bundle path must end with {BUNDLE_SUFFIX!r}, got {raw_path.name!r}"
            )
        if max_quota_bytes is not None and max_quota_bytes <= 0:
            raise PersonalVaultError(
                f"max_quota_bytes must be positive when set, got {max_quota_bytes}"
            )
        if raw_path.is_symlink():
            raise VaultTransportError(f"Bundle path must not be a symlink: {raw_path}")

        self._bundle_path = raw_path.resolve()
        self._max_quota_bytes = max_quota_bytes
        self._chunks: dict[str, bytes] = {}
        self._manifests: dict[str, bytes] = {}
        self._leases: dict[str, bytes] = {}
        self._chunk_mtimes: dict[str, float] = {}

        try:
            self._bundle_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VaultTransportError(f"Cannot create bundle parent directory: {exc}") from exc

        if self._bundle_path.exists():
            self._load_and_verify()

    @property
    def bundle_path(self) -> Path:
        return self._bundle_path

    def verify_bundle(self) -> None:
        """Verify binary header, index bounds, per-blob CRC32/SHA-256, and archive SHA-256."""
        self._load_and_verify()

    def _load_and_verify(self) -> None:
        if self._bundle_path.is_symlink() or not self._bundle_path.is_file():
            raise VaultTransportError(f"Bundle file does not exist: {self._bundle_path}")
        try:
            raw = self._bundle_path.read_bytes()
        except OSError as exc:
            raise VaultTransportError(f"Cannot read bundle {self._bundle_path}: {exc}") from exc

        if len(raw) < _BUNDLE_HEADER_TOTAL:
            raise VaultTamperDetectedError("Truncated .nagarvault bundle header")

        header_prefix = raw[: _BUNDLE_HEADER_PREFIX.size]
        stored_archive_sha = raw[_BUNDLE_HEADER_PREFIX.size : _BUNDLE_HEADER_TOTAL]
        magic, version, entry_count, index_offset, index_len = _BUNDLE_HEADER_PREFIX.unpack(
            header_prefix
        )
        if magic != BUNDLE_MAGIC or version != BUNDLE_VERSION:
            raise VaultTamperDetectedError("Invalid .nagarvault magic or unsupported version")

        if (
            index_offset < _BUNDLE_HEADER_TOTAL
            or index_offset > len(raw)
            or index_offset + index_len != len(raw)
        ):
            raise VaultTamperDetectedError("Corrupted .nagarvault binary index bounds")

        payload_and_index = raw[_BUNDLE_HEADER_TOTAL:]
        archive_hasher = hashlib.sha256()
        archive_hasher.update(header_prefix)
        archive_hasher.update(payload_and_index)
        if not hmac.compare_digest(archive_hasher.digest(), stored_archive_sha):
            raise VaultTamperDetectedError(
                "Archive-level SHA-256 mismatch in .nagarvault bundle: file is corrupted"
            )

        loaded_chunks: dict[str, bytes] = {}
        loaded_manifests: dict[str, bytes] = {}
        loaded_leases: dict[str, bytes] = {}
        archive_mtime = self._bundle_path.stat().st_mtime
        cursor = index_offset
        index_end = index_offset + index_len

        for _ in range(entry_count):
            if cursor + _BUNDLE_INDEX_HEAD.size > index_end:
                raise VaultTamperDetectedError("Truncated entry header in .nagarvault binary index")
            entry_kind, key_len = _BUNDLE_INDEX_HEAD.unpack_from(raw, cursor)
            cursor += _BUNDLE_INDEX_HEAD.size

            if cursor + key_len + _BUNDLE_INDEX_TAIL.size > index_end:
                raise VaultTamperDetectedError("Truncated entry metadata in .nagarvault index")
            try:
                key = raw[cursor : cursor + key_len].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise VaultTamperDetectedError("Invalid UTF-8 key in .nagarvault index") from exc
            cursor += key_len

            blob_offset, blob_len, expected_crc32, expected_sha256 = _BUNDLE_INDEX_TAIL.unpack_from(
                raw, cursor
            )
            cursor += _BUNDLE_INDEX_TAIL.size

            if (
                blob_offset < _BUNDLE_HEADER_TOTAL
                or blob_offset + blob_len > index_offset
                or blob_len < 0
            ):
                raise VaultTamperDetectedError("Out-of-bounds payload slice in .nagarvault index")

            blob = raw[blob_offset : blob_offset + blob_len]
            actual_crc32 = zlib.crc32(blob) & 0xFFFFFFFF
            actual_sha256 = hashlib.sha256(blob).digest()
            if actual_crc32 != expected_crc32 or not hmac.compare_digest(
                actual_sha256, expected_sha256
            ):
                raise VaultTamperDetectedError(
                    f"Entry checksum mismatch in .nagarvault bundle for {key!r}"
                )

            if entry_kind == _ENTRY_CHUNK:
                _validate_chunk_id(key)
                loaded_chunks[key] = blob
            elif entry_kind == _ENTRY_MANIFEST:
                _validate_snapshot_id(key)
                loaded_manifests[key] = blob
            elif entry_kind == _ENTRY_LEASE:
                if not key.startswith("leases/") or not key.endswith(".json"):
                    raise VaultTamperDetectedError(f"Malformed lease key in .nagarvault: {key!r}")
                stem = key[len("leases/") : -len(".json")]
                _validate_snapshot_id(stem)
                loaded_leases[stem] = blob
            else:
                raise VaultTamperDetectedError(f"Unknown .nagarvault entry kind: {entry_kind}")

        if cursor != index_end:
            raise VaultTamperDetectedError("Trailing unparsed bytes in .nagarvault binary index")

        self._chunks = loaded_chunks
        self._manifests = loaded_manifests
        self._leases = loaded_leases
        for cid in loaded_chunks:
            self._chunk_mtimes.setdefault(cid, archive_mtime)

    def _serialize_archive(
        self,
        chunks: Mapping[str, bytes],
        manifests: Mapping[str, bytes],
        leases: Mapping[str, bytes],
    ) -> bytes:
        ordered_entries: list[tuple[int, str, bytes]] = []
        for chunk_id in sorted(chunks):
            ordered_entries.append((_ENTRY_CHUNK, chunk_id, chunks[chunk_id]))
        for snapshot_id in sorted(manifests):
            ordered_entries.append((_ENTRY_MANIFEST, snapshot_id, manifests[snapshot_id]))
        for lease_id in sorted(leases):
            ordered_entries.append((_ENTRY_LEASE, f"leases/{lease_id}.json", leases[lease_id]))

        payload_parts: list[bytes] = []
        index_parts: list[bytes] = []
        current_offset = _BUNDLE_HEADER_TOTAL

        for kind, key, blob in ordered_entries:
            key_bytes = key.encode("utf-8")
            blob_len = len(blob)
            crc = zlib.crc32(blob) & 0xFFFFFFFF
            sha = hashlib.sha256(blob).digest()
            payload_parts.append(blob)
            index_parts.append(
                _BUNDLE_INDEX_HEAD.pack(kind, len(key_bytes))
                + key_bytes
                + _BUNDLE_INDEX_TAIL.pack(current_offset, blob_len, crc, sha)
            )
            current_offset += blob_len

        payload_bytes = b"".join(payload_parts)
        index_bytes = b"".join(index_parts)
        header_prefix = _BUNDLE_HEADER_PREFIX.pack(
            BUNDLE_MAGIC,
            BUNDLE_VERSION,
            len(ordered_entries),
            current_offset,
            len(index_bytes),
        )
        archive_sha = hashlib.sha256(header_prefix + payload_bytes + index_bytes).digest()
        return header_prefix + archive_sha + payload_bytes + index_bytes

    def _flush(
        self,
        next_chunks: dict[str, bytes],
        next_manifests: dict[str, bytes],
        next_leases: dict[str, bytes],
    ) -> None:
        archive_bytes = self._serialize_archive(next_chunks, next_manifests, next_leases)
        if self._max_quota_bytes is not None and len(archive_bytes) > self._max_quota_bytes:
            raise VaultQuotaExceededError(
                f"Portable bundle size {len(archive_bytes)} exceeds max_quota_bytes "
                f"{self._max_quota_bytes}"
            )
        _atomic_write_file(self._bundle_path, archive_bytes)
        self._chunks = next_chunks
        self._manifests = next_manifests
        self._leases = next_leases

    def put_chunk(self, chunk_id: str, data: bytes) -> None:
        valid = _validate_chunk_id(chunk_id)
        updated_chunks = dict(self._chunks)
        updated_chunks[valid] = bytes(data)
        self._flush(updated_chunks, dict(self._manifests), dict(self._leases))
        self._chunk_mtimes[valid] = time.time()

    def get_chunk(self, chunk_id: str) -> bytes:
        valid = _validate_chunk_id(chunk_id)
        if valid not in self._chunks:
            raise VaultTransportError(f"Chunk not found in .nagarvault bundle: {valid}")
        return self._chunks[valid]

    def has_chunk(self, chunk_id: str) -> bool:
        valid = _validate_chunk_id(chunk_id)
        return valid in self._chunks

    def get_chunk_mtime(self, chunk_id: str) -> float | None:
        """Return POSIX mtime recorded for ``chunk_id`` inside this bundle, or ``None``."""
        valid = _validate_chunk_id(chunk_id)
        if valid not in self._chunks:
            return None
        return self._chunk_mtimes.get(valid)

    def put_manifest(self, snapshot_id: str, data: bytes) -> None:
        valid = _validate_snapshot_id(snapshot_id)
        updated_manifests = dict(self._manifests)
        updated_manifests[valid] = bytes(data)
        self._flush(dict(self._chunks), updated_manifests, dict(self._leases))

    def get_manifest(self, snapshot_id: str) -> bytes:
        valid = _validate_snapshot_id(snapshot_id)
        if valid not in self._manifests:
            raise VaultTransportError(f"Manifest not found in .nagarvault bundle: {valid}")
        return self._manifests[valid]

    def list_manifests(self) -> list[str]:
        return sorted(self._manifests)

    def list_chunks(self) -> list[str]:
        """Return sorted chunk IDs stored inside this ``.nagarvault`` bundle."""
        return sorted(self._chunks)

    def delete_chunk(self, chunk_id: str) -> None:
        valid = _validate_chunk_id(chunk_id)
        if valid in self._chunks:
            updated_chunks = dict(self._chunks)
            del updated_chunks[valid]
            self._flush(updated_chunks, dict(self._manifests), dict(self._leases))
            self._chunk_mtimes.pop(valid, None)

    def acquire_push_lease(
        self,
        chunk_ids: Iterable[str] = (),
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
        lease_id: str | None = None,
    ) -> str:
        """Record a ``leases/<lease_id>.json`` entry inside this ``.nagarvault`` bundle."""
        if self._bundle_path.exists():
            self._load_and_verify()
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        now = time.time()
        effective_id = (
            f"{os.getpid()}-{int(now * 1000)}-{secrets.token_hex(6)}"
            if lease_id is None
            else _validate_snapshot_id(lease_id)
        )
        if effective_id in self._leases:
            raise VaultRevisionConflictError(
                f"Portable bundle lease {effective_id!r} already exists"
            )
        payload = _build_lease_json_bytes(
            lease_id=effective_id,
            project_id=project_id,
            now_epoch=now,
            ttl_seconds=finite_ttl,
            chunk_ids=chunk_ids,
        )
        updated_leases = dict(self._leases)
        updated_leases[effective_id] = payload
        self._flush(dict(self._chunks), dict(self._manifests), updated_leases)
        return effective_id

    def update_push_lease(
        self,
        lease_id: str,
        chunk_ids: Iterable[str],
        *,
        project_id: str = "",
        ttl_seconds: float = 3600.0,
    ) -> None:
        """Refresh an active ``leases/<lease_id>.json`` entry inside this bundle."""
        if self._bundle_path.exists():
            self._load_and_verify()
        finite_ttl = _validate_positive_finite_seconds(ttl_seconds, name="ttl_seconds")
        valid_id = _validate_snapshot_id(lease_id)
        if valid_id not in self._leases:
            raise VaultTransportError(f"Active bundle push lease not found: {valid_id}")
        payload = _build_lease_json_bytes(
            lease_id=valid_id,
            project_id=project_id,
            now_epoch=time.time(),
            ttl_seconds=finite_ttl,
            chunk_ids=chunk_ids,
        )
        updated_leases = dict(self._leases)
        updated_leases[valid_id] = payload
        self._flush(dict(self._chunks), dict(self._manifests), updated_leases)

    def release_push_lease(self, lease_id: str) -> None:
        """Remove a ``leases/<lease_id>.json`` entry from this bundle."""
        if self._bundle_path.exists():
            self._load_and_verify()
        valid_id = _validate_snapshot_id(lease_id)
        if valid_id in self._leases:
            updated_leases = dict(self._leases)
            del updated_leases[valid_id]
            self._flush(dict(self._chunks), dict(self._manifests), updated_leases)

    def read_active_leased_chunks(self, *, now_epoch: float | None = None) -> set[str]:
        """Read unexpired ``leases/*.json`` entries fail-closed from this bundle."""
        if self._bundle_path.exists():
            self._load_and_verify()
        effective_now = _validate_finite_epoch(now_epoch, default_now=time.time())
        active_chunks: set[str] = set()
        expired_ids: list[str] = []
        for stem, raw_bytes in sorted(self._leases.items()):
            expires_at, chunk_set = _parse_and_validate_lease_payload(
                raw_bytes,
                source_label=f"bundle://leases/{stem}.json",
            )
            if expires_at <= effective_now:
                expired_ids.append(stem)
                continue
            active_chunks.update(chunk_set)
        if expired_ids:
            updated_leases = {k: v for k, v in self._leases.items() if k not in expired_ids}
            self._flush(dict(self._chunks), dict(self._manifests), updated_leases)
        return active_chunks

    def delete_chunk_if_unleased(
        self,
        chunk_id: str,
        *,
        now_epoch: float | None = None,
    ) -> bool:
        """Verify bundle leases and delete ``chunk_id`` if unleased."""
        valid = _validate_chunk_id(chunk_id)
        if valid in self.read_active_leased_chunks(now_epoch=now_epoch):
            return False
        if not self.has_chunk(valid):
            return False
        self.delete_chunk(valid)
        return True


def export_snapshot_to_bundle(
    source_driver: BYOSStorageDriver,
    snapshot_id: str,
    bundle_path: Path | str,
) -> PortableBundleVaultDriver:
    """Package a single encrypted snapshot and all its chunks into a ``.nagarvault`` file."""
    manifest_bytes = source_driver.get_manifest(snapshot_id)
    manifest = VaultSnapshotManifest.from_json_bytes(manifest_bytes)
    bundle = PortableBundleVaultDriver(bundle_path)
    for chunk_id in sorted(manifest.referenced_chunk_ids()):
        chunk_bytes = source_driver.get_chunk(chunk_id)
        bundle.put_chunk(chunk_id, chunk_bytes)
    bundle.put_manifest(snapshot_id, manifest_bytes)
    bundle.verify_bundle()
    return bundle


__all__ = [
    "BUNDLE_MAGIC",
    "BUNDLE_SUFFIX",
    "BUNDLE_VERSION",
    "BYOSStorageDriver",
    "HttpTransportCallable",
    "LocalDeviceVaultDriver",
    "PortableBundleVaultDriver",
    "S3CompatibleSigV4Driver",
    "S3HttpRequest",
    "S3HttpResponse",
    "S3SigV4Config",
    "SigV4Artifacts",
    "VaultStagingJournal",
    "compute_sigv4_signature",
    "export_snapshot_to_bundle",
]
