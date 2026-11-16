"""Differential streaming sync engine, cross-process leases, crash resume, and GC.

:class:`PersonalCloudVaultManager` coordinates O(1)-memory streaming client-side
chunk encryption, content-addressed deduplication across consecutive project edits,
crash-resumable staging journals, optimistic concurrency verification via
``parent_snapshot_id``, multi-layered intra-process and cross-process in-flight
chunk leases against concurrent garbage collection, fail-closed verification
before file reconstruction, and unreferenced chunk cleanup.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import math
import os
import secrets
import stat
import threading
import time
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import (
    BYOSStorageDriver,
    VaultStagingJournal,
    _atomic_write_file,
)
from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
    DEFAULT_CHUNK_SIZE,
    MIN_PBKDF2_ITERATIONS,
    PBKDF2_ITERATIONS,
    EncryptedChunkEnvelope,
    VaultChunkRef,
    VaultFileEntry,
    VaultKeyMaterial,
    VaultSnapshotManifest,
    build_snapshot_manifest,
    compute_chunk_id,
    compute_file_mac,
    decrypt_chunk,
    derive_vault_keys,
    encrypt_chunk,
    generate_vault_salt,
    init_streaming_file_mac,
    normalize_relative_vault_path,
    validate_chunk_size,
    validate_pad_block_size,
    verify_manifest_integrity,
)
from nexus_ai_agent.storage.personal_cloud_vault.errors import (
    PersonalVaultError,
    VaultRevisionConflictError,
    VaultTamperDetectedError,
    VaultTransportError,
)

#: Fixed re-read block size for source-integrity hashing (keeps pushes O(1) in RAM).
_SOURCE_DIGEST_BLOCK_BYTES = 64 * 1024
DEFAULT_GC_MIN_AGE_SECONDS = 3600.0
DEFAULT_LEASE_TTL_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class _ChunkSourceLocator:
    """Lightweight pointer to a chunk's origin on disk for O(1)-RAM re-reads."""

    file_path: Path | None
    byte_offset: int
    byte_length: int
    chunk_index: int
    inline_rel_path: str | None = None
    source_sha256_hex: str | None = None


@dataclass(frozen=True)
class SnapshotPushReport:
    """Summary of a differential encrypted project snapshot upload."""

    manifest: VaultSnapshotManifest
    snapshot_id: str
    total_chunks: int
    uploaded_chunks: int
    deduplicated_chunks: int
    uploaded_bytes: int
    saved_plaintext_bytes: int
    deduplication_ratio: float


@dataclass(frozen=True)
class SnapshotPullReport:
    """Summary of a verified fail-closed project snapshot restoration."""

    manifest: VaultSnapshotManifest
    files: dict[str, bytes]
    verified_unique_chunks: int
    total_restored_bytes: int


@dataclass(frozen=True)
class GarbageCollectionReport:
    """Summary of orphan chunk cleanup across active vault snapshots."""

    deleted_chunk_ids: tuple[str, ...]
    retained_chunk_ids: tuple[str, ...]

    @property
    def deleted_count(self) -> int:
        return len(self.deleted_chunk_ids)


@dataclass(frozen=True, slots=True)
class _DiskFileStatFingerprint:
    """Initial POSIX inode/size/mtime/ctime snapshot verified via ``fstat`` while streaming.

    ``st_ctime_ns`` is included on purpose: an attacker (or a racing process) that
    rewrites a file in place and restores ``st_mtime_ns`` with :func:`os.utime` still
    bumps the inode change time, which userspace cannot forge. The fingerprint is
    additionally paired with a streaming SHA-256 computed while the chunks are read,
    so a same-size rewrite that somehow preserved every timestamp is still caught.
    """

    st_ino: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int


def _discover_source_plan(
    source: Path | str | Mapping[str, bytes],
) -> tuple[
    list[tuple[str, Path | None, int, _DiskFileStatFingerprint | None]],
    dict[str, bytes] | None,
]:
    """Build a sorted file plan without reading disk file contents into RAM."""
    if isinstance(source, Mapping):
        inline_map: dict[str, bytes] = {}
        for raw_rel, content in source.items():
            norm = normalize_relative_vault_path(str(raw_rel))
            inline_map[norm] = bytes(content)
        plan: list[tuple[str, Path | None, int, _DiskFileStatFingerprint | None]] = [
            (rel, None, len(inline_map[rel]), None) for rel in sorted(inline_map)
        ]
        return plan, inline_map

    raw_dir = Path(source)
    if raw_dir.is_symlink() or not raw_dir.is_dir():
        raise VaultTransportError(
            f"Project source directory must be an existing non-symlink directory: {raw_dir}"
        )
    root = raw_dir.resolve()
    disk_plan: list[tuple[str, Path | None, int, _DiskFileStatFingerprint | None]] = []
    for entry in sorted(root.rglob("*")):
        try:
            if entry.is_symlink():
                raise VaultTransportError(
                    f"Symlinks are forbidden inside project vault source: {entry}"
                )
            if entry.is_file():
                rel_posix = entry.relative_to(root).as_posix()
                norm = normalize_relative_vault_path(rel_posix)
                st = entry.stat()
                fp = _DiskFileStatFingerprint(
                    st_ino=st.st_ino,
                    st_size=st.st_size,
                    st_mtime_ns=st.st_mtime_ns,
                    st_ctime_ns=st.st_ctime_ns,
                )
                disk_plan.append((norm, entry, st.st_size, fp))
        except OSError as exc:
            raise VaultTransportError(f"Failed stating project file {entry}: {exc}") from exc
    disk_plan.sort(key=lambda item: item[0])
    return disk_plan, None


def _verify_fstat_matches_fingerprint(
    disk_path: Path,
    actual_st: os.stat_result,
    expected_fp: _DiskFileStatFingerprint,
    *,
    phase: str,
) -> None:
    if (
        actual_st.st_ino != expected_fp.st_ino
        or actual_st.st_size != expected_fp.st_size
        or actual_st.st_mtime_ns != expected_fp.st_mtime_ns
        or actual_st.st_ctime_ns != expected_fp.st_ctime_ns
    ):
        raise VaultTransportError(
            f"Concurrent file modification/replacement detected ({phase}) on {disk_path}"
        )


def _stream_source_chunks(
    *,
    rel_path: str,
    disk_path: Path | None,
    expected_size: int,
    expected_fp: _DiskFileStatFingerprint | None,
    chunk_size: int,
    inline_sources: Mapping[str, bytes] | None,
    digests_out: dict[str, str] | None = None,
) -> Iterator[tuple[int, int, bytes]]:
    """Yield ``(chunk_index, byte_offset, chunk_bytes)`` with ``fstat`` checks.

    When ``digests_out`` is provided, the streaming SHA-256 of the exact bytes read
    from the source is stored under ``rel_path``. Callers re-read and compare it before
    the manifest is committed, so a same-size in-place rewrite that restored every
    timestamp (``mtime``/``ctime``) is still detected.
    """
    if disk_path is not None:
        assert expected_fp is not None
        bytes_read = 0
        idx = 0
        stream_hasher = hashlib.sha256()
        try:
            with disk_path.open("rb") as handle:
                open_st = os.fstat(handle.fileno())
                _verify_fstat_matches_fingerprint(
                    disk_path, open_st, expected_fp, phase="open-fstat"
                )
                while True:
                    chunk = handle.read(chunk_size)
                    if not chunk:
                        break
                    stream_hasher.update(chunk)
                    offset = bytes_read
                    bytes_read += len(chunk)
                    yield idx, offset, chunk
                    idx += 1
                end_fstat = os.fstat(handle.fileno())
                _verify_fstat_matches_fingerprint(
                    disk_path, end_fstat, expected_fp, phase="eof-fstat"
                )
                end_lstat = disk_path.lstat()
                if stat.S_ISLNK(end_lstat.st_mode) or bytes_read != expected_size:
                    raise VaultTransportError(
                        f"Concurrent file size/symlink change while streaming {disk_path}"
                    )
                _verify_fstat_matches_fingerprint(
                    disk_path, end_lstat, expected_fp, phase="eof-lstat"
                )
        except OSError as exc:
            raise VaultTransportError(f"Failed reading project file {disk_path}: {exc}") from exc
        if digests_out is not None:
            digests_out[rel_path] = stream_hasher.hexdigest()
        return

    assert inline_sources is not None
    raw_bytes = inline_sources[rel_path]
    if digests_out is not None:
        digests_out[rel_path] = hashlib.sha256(raw_bytes).hexdigest()
    view = memoryview(raw_bytes)
    for idx, offset in enumerate(range(0, expected_size, chunk_size)):
        yield idx, offset, bytes(view[offset : offset + chunk_size])


def _read_chunk_from_locator(
    locator: _ChunkSourceLocator,
    inline_sources: Mapping[str, bytes] | None,
) -> bytes:
    """Re-read a single evicted chunk from disk via ``seek`` + ``read`` on demand."""
    if locator.file_path is not None:
        try:
            with locator.file_path.open("rb") as handle:
                handle.seek(locator.byte_offset)
                data = handle.read(locator.byte_length)
        except OSError as exc:
            raise VaultTransportError(
                f"Failed re-reading chunk from {locator.file_path}: {exc}"
            ) from exc
        if len(data) != locator.byte_length:
            raise VaultTransportError(
                f"Short read while healing chunk from {locator.file_path}: "
                f"expected {locator.byte_length} bytes, got {len(data)}"
            )
        return data

    assert inline_sources is not None and locator.inline_rel_path is not None
    raw = inline_sources[locator.inline_rel_path]
    end = locator.byte_offset + locator.byte_length
    return bytes(memoryview(raw)[locator.byte_offset : end])


def _verify_source_digest(
    disk_path: Path,
    *,
    expected_sha256_hex: str | None,
    phase: str,
) -> None:
    """Re-read a disk source and compare it with the bytes originally streamed.

    This closes the residual window in which a file is rewritten in place at exactly
    the same size *and* every timestamp (``mtime``/``ctime``) is restored: the content
    hash captured while streaming is the authoritative source of truth, so uploads
    never commit a manifest whose chunks do not match the current source.
    """
    if expected_sha256_hex is None:
        return
    hasher = hashlib.sha256()
    try:
        with disk_path.open("rb") as handle:
            while True:
                block = handle.read(_SOURCE_DIGEST_BLOCK_BYTES)
                if not block:
                    break
                hasher.update(block)
    except OSError as exc:
        raise VaultTransportError(f"Failed re-reading project file {disk_path}: {exc}") from exc
    actual_sha256_hex = hasher.hexdigest()
    if not hmac.compare_digest(actual_sha256_hex, expected_sha256_hex):
        raise VaultTransportError(
            f"Fail-closed ({phase}): local source content mutated for {disk_path}"
        )


def _write_verified_files_to_directory(target_dir: Path | str, files: Mapping[str, bytes]) -> None:
    """Persist already-verified files atomically inside ``target_dir``."""
    raw_target = Path(target_dir)
    if raw_target.is_symlink():
        raise VaultTransportError(f"Target directory must not be a symlink: {raw_target}")
    try:
        raw_target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VaultTransportError(f"Cannot create target directory {raw_target}: {exc}") from exc

    root = raw_target.resolve()
    for rel_path, content in sorted(files.items()):
        norm = normalize_relative_vault_path(rel_path)
        dest = root / norm
        current = root
        for segment in norm.split("/")[:-1]:
            current = current / segment
            if current.is_symlink():
                raise VaultTransportError(
                    f"Symlinked subdirectory forbidden during snapshot restore: {current}"
                )
            current.mkdir(parents=True, exist_ok=True)
        resolved_dest = dest.resolve()
        if not resolved_dest.is_relative_to(root) or dest.is_symlink():
            raise VaultTransportError(f"Refusing path escape or symlink target: {dest}")
        _atomic_write_file(resolved_dest, content)


class PersonalCloudVaultManager:
    """Differential encrypted BYOS sync manager with O(1) streaming & cross-process leases."""

    def __init__(
        self,
        driver: BYOSStorageDriver,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        kdf_iterations: int = PBKDF2_ITERATIONS,
        pad_to_block_size: int = 1,
        lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        validate_chunk_size(chunk_size)
        validate_pad_block_size(pad_to_block_size)
        if kdf_iterations < MIN_PBKDF2_ITERATIONS:
            raise PersonalVaultError(
                f"kdf_iterations must be at least {MIN_PBKDF2_ITERATIONS}, got {kdf_iterations}"
            )
        if (
            isinstance(lease_ttl_seconds, bool)
            or not isinstance(lease_ttl_seconds, (int, float))
            or not math.isfinite(float(lease_ttl_seconds))
            or float(lease_ttl_seconds) <= 0.0
        ):
            raise PersonalVaultError(
                f"lease_ttl_seconds must be finite and positive, got {lease_ttl_seconds!r}"
            )
        self._driver = driver
        self._chunk_size = chunk_size
        self._kdf_iterations = kdf_iterations
        self._pad_to_block_size = pad_to_block_size
        self._lease_ttl_seconds = float(lease_ttl_seconds)
        self._lock = threading.RLock()
        self._known_chunk_ids: set[str] = set()
        self._in_flight_chunks: set[str] = set()
        self._in_flight_refcounts: dict[str, int] = {}
        self._chunk_seen_at: dict[str, float] = {}

    @property
    def driver(self) -> BYOSStorageDriver:
        return self._driver

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    @property
    def in_flight_chunks(self) -> frozenset[str]:
        """Return a thread-safe snapshot of chunk IDs currently being pushed."""
        with self._lock:
            return frozenset(self._in_flight_chunks)

    def record_orphan_candidate_chunk(
        self,
        chunk_id: str,
        *,
        recorded_at: float | None = None,
    ) -> None:
        """Register a chunk ID in the local sync journal for future GC inspection."""
        if recorded_at is None:
            ts = time.time()
        else:
            if (
                isinstance(recorded_at, bool)
                or not isinstance(recorded_at, (int, float))
                or not math.isfinite(float(recorded_at))
            ):
                raise PersonalVaultError(f"recorded_at must be finite, got {recorded_at!r}")
            ts = float(recorded_at)
        with self._lock:
            self._known_chunk_ids.add(chunk_id)
            self._chunk_seen_at[chunk_id] = ts

    def _acquire_in_flight_chunks(self, chunk_ids: Iterable[str]) -> tuple[str, ...]:
        unique_ids = tuple(sorted(set(chunk_ids)))
        now = time.time()
        with self._lock:
            for cid in unique_ids:
                self._in_flight_refcounts[cid] = self._in_flight_refcounts.get(cid, 0) + 1
                self._in_flight_chunks.add(cid)
                self._known_chunk_ids.add(cid)
                self._chunk_seen_at[cid] = now
        return unique_ids

    def _release_in_flight_chunks(self, acquired_ids: Iterable[str]) -> None:
        with self._lock:
            for cid in acquired_ids:
                remaining = self._in_flight_refcounts.get(cid, 0) - 1
                if remaining <= 0:
                    self._in_flight_refcounts.pop(cid, None)
                    self._in_flight_chunks.discard(cid)
                else:
                    self._in_flight_refcounts[cid] = remaining

    def _acquire_driver_lease(self, project_id: str) -> str | None:
        acquire_fn = getattr(self._driver, "acquire_push_lease", None)
        if callable(acquire_fn):
            lease_id = acquire_fn(
                (),
                project_id=project_id,
                ttl_seconds=self._lease_ttl_seconds,
            )
            return str(lease_id)
        return None

    def _update_driver_lease(
        self,
        lease_id: str | None,
        chunk_ids: Collection[str],
        *,
        project_id: str,
    ) -> None:
        if lease_id is None:
            return
        update_fn = getattr(self._driver, "update_push_lease", None)
        if callable(update_fn):
            update_fn(
                lease_id,
                chunk_ids,
                project_id=project_id,
                ttl_seconds=self._lease_ttl_seconds,
            )

    def _release_driver_lease(self, lease_id: str | None) -> None:
        if lease_id is None:
            return
        release_fn = getattr(self._driver, "release_push_lease", None)
        if callable(release_fn):
            release_fn(lease_id)

    def _read_driver_leased_chunks(self) -> set[str]:
        read_fn = getattr(self._driver, "read_active_leased_chunks", None)
        if callable(read_fn):
            leased = read_fn()
            if isinstance(leased, Iterable):
                return {str(cid) for cid in leased}
        return set()

    def _load_staging_journal(
        self,
        project_id: str,
        parent_snapshot_id: str | None,
    ) -> VaultStagingJournal | None:
        load_fn = getattr(self._driver, "load_staging_journal", None)
        if callable(load_fn):
            loaded = load_fn(project_id, parent_snapshot_id)
            if isinstance(loaded, VaultStagingJournal):
                return loaded
        return None

    def _save_staging_journal(self, journal: VaultStagingJournal) -> None:
        save_fn = getattr(self._driver, "save_staging_journal", None)
        if callable(save_fn):
            save_fn(journal)

    def _clear_staging_journal(
        self,
        project_id: str,
        parent_snapshot_id: str | None,
    ) -> None:
        clear_fn = getattr(self._driver, "clear_staging_journal", None)
        if callable(clear_fn):
            clear_fn(project_id, parent_snapshot_id)

    def _encrypt_and_upload_chunk(
        self,
        chunk_id: str,
        chunk_plain: bytes,
        key_material: VaultKeyMaterial,
    ) -> int:
        envelope = encrypt_chunk(
            chunk_plain,
            key_material,
            chunk_index=0,
            pad_to_block_size=self._pad_to_block_size,
        )
        del chunk_plain
        wire_bytes = envelope.to_bytes()
        del envelope
        wire_len = len(wire_bytes)
        self._driver.put_chunk(chunk_id, wire_bytes)
        del wire_bytes
        with self._lock:
            self._known_chunk_ids.add(chunk_id)
            self._chunk_seen_at[chunk_id] = time.time()
        return wire_len

    def _verify_and_heal_manifest_chunks(
        self,
        chunk_locators: Mapping[str, _ChunkSourceLocator],
        inline_sources: Mapping[str, bytes] | None,
        key_material: VaultKeyMaterial,
        *,
        stage: str,
        rollback_snapshot_id: str | None = None,
    ) -> tuple[int, int]:
        """Verify every manifest chunk exists on storage; heal from disk if evicted."""
        healed_count = 0
        healed_bytes = 0
        for cid in sorted(chunk_locators):
            if self._driver.has_chunk(cid):
                continue
            healed_plain = _read_chunk_from_locator(chunk_locators[cid], inline_sources)
            recomputed_cid = compute_chunk_id(healed_plain, key_material)
            if not hmac.compare_digest(recomputed_cid, cid):
                raise VaultTransportError(
                    f"Fail-closed ({stage}): local source mutated while healing chunk {cid}"
                )
            written = self._encrypt_and_upload_chunk(cid, healed_plain, key_material)
            del healed_plain
            healed_count += 1
            healed_bytes += written
            if not self._driver.has_chunk(cid):
                if rollback_snapshot_id is not None:
                    delete_manifest_fn = getattr(self._driver, "delete_manifest", None)
                    if callable(delete_manifest_fn):
                        with contextlib.suppress(Exception):
                            delete_manifest_fn(rollback_snapshot_id)
                raise VaultTransportError(
                    f"Fail-closed ({stage}): chunk {cid} missing from storage after re-upload"
                )
        return healed_count, healed_bytes

    def _load_project_manifests(
        self,
        project_id: str,
    ) -> tuple[set[str], dict[str, VaultSnapshotManifest]]:
        all_ids = set(self._driver.list_manifests())
        project_manifests: dict[str, VaultSnapshotManifest] = {}
        for sid in sorted(all_ids):
            raw = self._driver.get_manifest(sid)
            manifest = VaultSnapshotManifest.from_json_bytes(raw)
            with self._lock:
                self._known_chunk_ids.update(manifest.referenced_chunk_ids())
            if manifest.project_id == project_id:
                project_manifests[sid] = manifest
        return all_ids, project_manifests

    @staticmethod
    def _compute_remote_heads(
        project_manifests: Mapping[str, VaultSnapshotManifest],
    ) -> set[str]:
        referenced_parents = {
            manifest.parent_snapshot_id
            for manifest in project_manifests.values()
            if manifest.parent_snapshot_id is not None
            and manifest.parent_snapshot_id in project_manifests
        }
        return set(project_manifests.keys()) - referenced_parents

    def _resolve_push_key_material_and_staging(
        self,
        *,
        project_id: str,
        parent_snapshot_id: str | None,
        master_secret: str | bytes,
        salt: bytes | None,
        project_manifests: Mapping[str, VaultSnapshotManifest],
    ) -> tuple[VaultKeyMaterial, set[str]]:
        existing_journal = self._load_staging_journal(project_id, parent_snapshot_id)
        if not project_manifests:
            if parent_snapshot_id is not None:
                raise VaultRevisionConflictError(
                    f"Remote vault has no history for project {project_id!r}, "
                    f"cannot reference parent_snapshot_id={parent_snapshot_id!r}"
                )
            if (
                salt is None
                and existing_journal is not None
                and existing_journal.kdf_iterations == self._kdf_iterations
                and existing_journal.chunk_size == self._chunk_size
            ):
                candidate_km = derive_vault_keys(
                    master_secret,
                    bytes.fromhex(existing_journal.salt_hex),
                    iterations=existing_journal.kdf_iterations,
                )
                if hmac.compare_digest(candidate_km.key_id.hex(), existing_journal.key_id_hex):
                    return candidate_km, set(existing_journal.uploaded_chunk_ids)

            effective_salt = generate_vault_salt() if salt is None else salt
            key_material = derive_vault_keys(
                master_secret,
                effective_salt,
                iterations=self._kdf_iterations,
            )
        else:
            remote_heads = self._compute_remote_heads(project_manifests)
            if parent_snapshot_id is None:
                raise VaultRevisionConflictError(
                    f"Remote vault already has head(s) {sorted(remote_heads)} for "
                    f"project {project_id!r}; parent_snapshot_id is required"
                )
            if parent_snapshot_id not in project_manifests:
                raise VaultRevisionConflictError(
                    f"parent_snapshot_id {parent_snapshot_id!r} not found in remote history"
                )
            if remote_heads != {parent_snapshot_id}:
                raise VaultRevisionConflictError(
                    f"Revision conflict for project {project_id!r}: remote head(s) "
                    f"{sorted(remote_heads)} supersede or diverge from "
                    f"parent_snapshot_id={parent_snapshot_id!r}"
                )
            parent_manifest = project_manifests[parent_snapshot_id]
            effective_salt = bytes.fromhex(parent_manifest.salt_hex) if salt is None else salt
            key_material = derive_vault_keys(
                master_secret,
                effective_salt,
                iterations=parent_manifest.kdf_iterations,
            )
            verify_manifest_integrity(parent_manifest, key_material)

        if (
            existing_journal is not None
            and existing_journal.chunk_size == self._chunk_size
            and hmac.compare_digest(existing_journal.key_id_hex, key_material.key_id.hex())
        ):
            return key_material, set(existing_journal.uploaded_chunk_ids)
        return key_material, set()

    def push_project_snapshot(
        self,
        *,
        project_id: str,
        source: Path | str | Mapping[str, bytes],
        master_secret: str | bytes,
        parent_snapshot_id: str | None = None,
        snapshot_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
        salt: bytes | None = None,
    ) -> SnapshotPushReport:
        """Stream-encrypt and differentially upload a snapshot in O(1) bounded RAM."""
        if not project_id.strip():
            raise PersonalVaultError("project_id must not be empty")

        all_manifest_ids, project_manifests = self._load_project_manifests(project_id)
        if snapshot_id is not None and snapshot_id in all_manifest_ids:
            raise VaultRevisionConflictError(
                f"Snapshot {snapshot_id!r} already exists on the remote vault"
            )

        key_material, staged_chunk_ids = self._resolve_push_key_material_and_staging(
            project_id=project_id,
            parent_snapshot_id=parent_snapshot_id,
            master_secret=master_secret,
            salt=salt,
            project_manifests=project_manifests,
        )
        source_plan, inline_sources = _discover_source_plan(source)

        # P1-1: Persist initial staging journal BEFORE uploading even the first chunk
        # so salt_hex / key_id_hex survive a crash right after the first put_chunk.
        self._save_staging_journal(
            VaultStagingJournal(
                project_id=project_id,
                parent_snapshot_id=parent_snapshot_id,
                salt_hex=key_material.salt.hex(),
                kdf_iterations=key_material.iterations,
                key_id_hex=key_material.key_id.hex(),
                chunk_size=self._chunk_size,
                updated_at_epoch=time.time(),
                uploaded_chunk_ids=tuple(sorted(staged_chunk_ids)),
            )
        )

        chunk_locators: dict[str, _ChunkSourceLocator] = {}
        source_digests: dict[str, str] = {}
        file_entries: list[VaultFileEntry] = []
        leased_in_push: list[str] = []
        leased_set: set[str] = set()
        uploaded_in_batch: set[str] = set()
        total_chunks = 0
        uploaded_chunks = 0
        deduplicated_chunks = 0
        uploaded_bytes = 0
        saved_plaintext_bytes = 0

        lease_id = self._acquire_driver_lease(project_id)
        try:
            for rel_path, disk_path, expected_size, expected_fp in source_plan:
                file_mac = init_streaming_file_mac(rel_path, expected_size, key_material)
                chunk_refs: list[VaultChunkRef] = []

                for idx, byte_offset, chunk_plain in _stream_source_chunks(
                    rel_path=rel_path,
                    disk_path=disk_path,
                    expected_size=expected_size,
                    expected_fp=expected_fp,
                    chunk_size=self._chunk_size,
                    inline_sources=inline_sources,
                    digests_out=source_digests,
                ):
                    total_chunks += 1
                    chunk_len = len(chunk_plain)
                    file_mac.update(chunk_plain)
                    chunk_id = compute_chunk_id(chunk_plain, key_material)

                    if chunk_id not in chunk_locators:
                        chunk_locators[chunk_id] = _ChunkSourceLocator(
                            file_path=disk_path,
                            byte_offset=byte_offset,
                            byte_length=chunk_len,
                            chunk_index=idx,
                            inline_rel_path=rel_path if disk_path is None else None,
                            source_sha256_hex=source_digests.get(rel_path),
                        )

                    # Lease chunk in RAM and cross-process lease file BEFORE put_chunk.
                    if chunk_id not in leased_set:
                        self._acquire_in_flight_chunks((chunk_id,))
                        leased_in_push.append(chunk_id)
                        leased_set.add(chunk_id)
                        self._update_driver_lease(
                            lease_id,
                            leased_set,
                            project_id=project_id,
                        )

                    if chunk_id in uploaded_in_batch or self._driver.has_chunk(chunk_id):
                        deduplicated_chunks += 1
                        saved_plaintext_bytes += chunk_len
                        uploaded_in_batch.add(chunk_id)
                        chunk_plain = b""
                        if chunk_id not in staged_chunk_ids:
                            staged_chunk_ids.add(chunk_id)
                            self._save_staging_journal(
                                VaultStagingJournal(
                                    project_id=project_id,
                                    parent_snapshot_id=parent_snapshot_id,
                                    salt_hex=key_material.salt.hex(),
                                    kdf_iterations=key_material.iterations,
                                    key_id_hex=key_material.key_id.hex(),
                                    chunk_size=self._chunk_size,
                                    updated_at_epoch=time.time(),
                                    uploaded_chunk_ids=tuple(sorted(staged_chunk_ids)),
                                )
                            )
                    else:
                        payload_to_send = chunk_plain
                        chunk_plain = b""
                        written = self._encrypt_and_upload_chunk(
                            chunk_id,
                            payload_to_send,
                            key_material,
                        )
                        del payload_to_send
                        uploaded_in_batch.add(chunk_id)
                        staged_chunk_ids.add(chunk_id)
                        uploaded_chunks += 1
                        uploaded_bytes += written
                        self._save_staging_journal(
                            VaultStagingJournal(
                                project_id=project_id,
                                parent_snapshot_id=parent_snapshot_id,
                                salt_hex=key_material.salt.hex(),
                                kdf_iterations=key_material.iterations,
                                key_id_hex=key_material.key_id.hex(),
                                chunk_size=self._chunk_size,
                                updated_at_epoch=time.time(),
                                uploaded_chunk_ids=tuple(sorted(staged_chunk_ids)),
                            )
                        )

                    chunk_refs.append(
                        VaultChunkRef(
                            chunk_index=idx,
                            chunk_id=chunk_id,
                            plaintext_len=chunk_len,
                        )
                    )

                file_entries.append(
                    VaultFileEntry(
                        rel_path=rel_path,
                        total_size=expected_size,
                        file_mac_hex=file_mac.hexdigest(),
                        chunks=tuple(chunk_refs),
                    )
                )

            # The streaming SHA-256 is authoritative: re-read every disk source and
            # fail closed if the on-disk bytes no longer match what was uploaded.
            for rel_path, disk_path, _expected_size, _fp in source_plan:
                if disk_path is not None:
                    _verify_source_digest(
                        disk_path,
                        expected_sha256_hex=source_digests.get(rel_path),
                        phase="pre-manifest-digest",
                    )

            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            effective_snapshot_id = (
                f"snap-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
                f"{secrets.token_hex(6)}"
                if snapshot_id is None
                else snapshot_id
            )
            manifest = build_snapshot_manifest(
                project_id=project_id,
                snapshot_id=effective_snapshot_id,
                parent_snapshot_id=parent_snapshot_id,
                created_at_utc=now_iso,
                chunk_size=self._chunk_size,
                files=file_entries,
                key_material=key_material,
                metadata=metadata,
            )

            # Pre-manifest check: heal from disk via _ChunkSourceLocator if evicted.
            self._verify_and_heal_manifest_chunks(
                chunk_locators,
                inline_sources,
                key_material,
                stage="pre-manifest",
            )
            self._driver.put_manifest(manifest.snapshot_id, manifest.to_json_bytes())

            # Post-manifest check: verify every chunk remains committed after manifest write.
            self._verify_and_heal_manifest_chunks(
                chunk_locators,
                inline_sources,
                key_material,
                stage="post-manifest",
                rollback_snapshot_id=manifest.snapshot_id,
            )
            self._clear_staging_journal(project_id, parent_snapshot_id)
        finally:
            self._release_in_flight_chunks(leased_in_push)
            self._release_driver_lease(lease_id)

        dedup_ratio = (deduplicated_chunks / total_chunks) if total_chunks > 0 else 0.0
        return SnapshotPushReport(
            manifest=manifest,
            snapshot_id=manifest.snapshot_id,
            total_chunks=total_chunks,
            uploaded_chunks=uploaded_chunks,
            deduplicated_chunks=deduplicated_chunks,
            uploaded_bytes=uploaded_bytes,
            saved_plaintext_bytes=saved_plaintext_bytes,
            deduplication_ratio=dedup_ratio,
        )

    def pull_project_snapshot(
        self,
        *,
        project_id: str,
        master_secret: str | bytes,
        snapshot_id: str | None = None,
        target_dir: Path | str | None = None,
    ) -> SnapshotPullReport:
        """Verify manifest, RFC 6962 Merkle root, and all chunk HMACs before restoring."""
        if not project_id.strip():
            raise PersonalVaultError("project_id must not be empty")

        if snapshot_id is None:
            _, project_manifests = self._load_project_manifests(project_id)
            if not project_manifests:
                raise VaultTransportError(f"No snapshots found for project {project_id!r}")
            heads = self._compute_remote_heads(project_manifests)
            if len(heads) != 1:
                raise VaultRevisionConflictError(
                    f"Ambiguous remote heads for project {project_id!r}: {sorted(heads)}"
                )
            resolved_id = next(iter(heads))
            manifest = project_manifests[resolved_id]
        else:
            raw_manifest = self._driver.get_manifest(snapshot_id)
            manifest = VaultSnapshotManifest.from_json_bytes(raw_manifest)
            if manifest.snapshot_id != snapshot_id or manifest.project_id != project_id:
                raise VaultTamperDetectedError(
                    f"Manifest identity mismatch: expected ({project_id!r}, {snapshot_id!r}), "
                    f"got ({manifest.project_id!r}, {manifest.snapshot_id!r})"
                )

        key_material = derive_vault_keys(
            master_secret,
            bytes.fromhex(manifest.salt_hex),
            iterations=manifest.kdf_iterations,
        )
        verify_manifest_integrity(manifest, key_material)

        # Fail-closed phase: fetch, authenticate, and decrypt EVERY chunk BEFORE writing files.
        verified_chunks: dict[str, bytes] = {}
        for entry in manifest.files:
            for ref in entry.chunks:
                if ref.chunk_id in verified_chunks:
                    cached = verified_chunks[ref.chunk_id]
                    if len(cached) != ref.plaintext_len:
                        raise VaultTamperDetectedError(
                            f"Inconsistent chunk length for {ref.chunk_id}"
                        )
                    continue
                raw_envelope = self._driver.get_chunk(ref.chunk_id)
                envelope = EncryptedChunkEnvelope.from_bytes(raw_envelope)
                if not hmac.compare_digest(envelope.chunk_id, ref.chunk_id):
                    raise VaultTamperDetectedError(
                        f"Chunk substitution detected: requested {ref.chunk_id}, "
                        f"got {envelope.chunk_id}"
                    )
                plaintext = decrypt_chunk(envelope, key_material, expected_chunk_index=0)
                if len(plaintext) != ref.plaintext_len:
                    raise VaultTamperDetectedError(
                        f"Decrypted chunk size {len(plaintext)} != manifest {ref.plaintext_len}"
                    )
                verified_chunks[ref.chunk_id] = plaintext
                with self._lock:
                    self._known_chunk_ids.add(ref.chunk_id)

        restored_files: dict[str, bytes] = {}
        total_bytes = 0
        for entry in manifest.files:
            assembled = b"".join(verified_chunks[ref.chunk_id] for ref in entry.chunks)
            actual_file_mac = compute_file_mac(entry.rel_path, assembled, key_material)
            if not hmac.compare_digest(actual_file_mac, entry.file_mac_hex):
                raise VaultTamperDetectedError(
                    f"Reassembled file MAC mismatch for {entry.rel_path!r}"
                )
            restored_files[entry.rel_path] = assembled
            total_bytes += len(assembled)

        if target_dir is not None:
            _write_verified_files_to_directory(target_dir, restored_files)

        return SnapshotPullReport(
            manifest=manifest,
            files=restored_files,
            verified_unique_chunks=len(verified_chunks),
            total_restored_bytes=total_bytes,
        )

    def _is_younger_than_min_age(
        self,
        chunk_id: str,
        *,
        min_age_seconds: float,
        now_epoch: float,
        seen_snapshot: Mapping[str, float],
    ) -> bool:
        if min_age_seconds <= 0.0:
            return False
        recorded_ts = seen_snapshot.get(chunk_id)
        if recorded_ts is None:
            mtime_fn = getattr(self._driver, "get_chunk_mtime", None)
            if callable(mtime_fn):
                mtime_val = mtime_fn(chunk_id)
                if (
                    not isinstance(mtime_val, bool)
                    and isinstance(mtime_val, (int, float))
                    and math.isfinite(float(mtime_val))
                ):
                    recorded_ts = float(mtime_val)
        if recorded_ts is None:
            # P1-3 Fail-Safe: unknown age is stamped with `first_seen = now_epoch` and retained.
            with self._lock:
                first_seen = self._chunk_seen_at.setdefault(chunk_id, now_epoch)
                self._known_chunk_ids.add(chunk_id)
            return (now_epoch - first_seen) < min_age_seconds
        return (now_epoch - recorded_ts) < min_age_seconds

    def garbage_collect_unreferenced_chunks(
        self,
        *,
        active_snapshot_ids: Collection[str] | None = None,
        candidate_chunk_ids: Iterable[str] | None = None,
        protected_chunk_ids: Collection[str] = (),
        min_age_seconds: float = DEFAULT_GC_MIN_AGE_SECONDS,
    ) -> GarbageCollectionReport:
        """Delete orphan chunks not referenced by active manifests or in-flight leases."""
        if (
            isinstance(min_age_seconds, bool)
            or not isinstance(min_age_seconds, (int, float))
            or not math.isfinite(float(min_age_seconds))
            or float(min_age_seconds) < 0.0
        ):
            raise PersonalVaultError(
                f"min_age_seconds must be finite and non-negative, got {min_age_seconds!r}"
            )
        effective_min_age = float(min_age_seconds)

        with self._lock:
            initial_in_flight = set(self._in_flight_chunks)
            known_snapshot = set(self._known_chunk_ids)
            seen_snapshot = dict(self._chunk_seen_at)

        cross_process_leased = self._read_driver_leased_chunks()
        stored_manifest_ids = set(self._driver.list_manifests())
        if active_snapshot_ids is None:
            active_ids = stored_manifest_ids
        else:
            active_ids = set(active_snapshot_ids)
            missing = active_ids - stored_manifest_ids
            if missing:
                raise VaultTransportError(
                    f"Cannot run GC: active snapshot(s) missing from storage: {sorted(missing)}"
                )

        referenced_chunks: set[str] = set()
        superseded_chunks: set[str] = set()

        for sid in sorted(stored_manifest_ids):
            manifest = VaultSnapshotManifest.from_json_bytes(self._driver.get_manifest(sid))
            chunks_in_manifest = manifest.referenced_chunk_ids()
            if sid in active_ids:
                referenced_chunks.update(chunks_in_manifest)
            else:
                superseded_chunks.update(chunks_in_manifest)

        protected_all = (
            referenced_chunks | set(protected_chunk_ids) | initial_in_flight | cross_process_leased
        )
        discovered_candidates: set[str] = known_snapshot | superseded_chunks
        if candidate_chunk_ids is not None:
            discovered_candidates.update(candidate_chunk_ids)
        list_chunks_fn = getattr(self._driver, "list_chunks", None)
        if callable(list_chunks_fn):
            listed = list_chunks_fn()
            if isinstance(listed, Iterable):
                discovered_candidates.update(str(cid) for cid in listed)

        now_epoch = time.time()
        atomic_delete_fn = getattr(self._driver, "delete_chunk_if_unleased", None)
        deleted: list[str] = []
        for chunk_id in sorted(discovered_candidates):
            if chunk_id in protected_all:
                continue
            if self._is_younger_than_min_age(
                chunk_id,
                min_age_seconds=effective_min_age,
                now_epoch=now_epoch,
                seen_snapshot=seen_snapshot,
            ):
                continue
            with self._lock:
                if chunk_id in self._in_flight_chunks:
                    continue
                if callable(atomic_delete_fn):
                    if atomic_delete_fn(chunk_id, now_epoch=now_epoch):
                        deleted.append(chunk_id)
                        self._known_chunk_ids.discard(chunk_id)
                        self._chunk_seen_at.pop(chunk_id, None)
                    elif not self._driver.has_chunk(chunk_id):
                        self._known_chunk_ids.discard(chunk_id)
                        self._chunk_seen_at.pop(chunk_id, None)
                    continue
                if chunk_id in self._read_driver_leased_chunks():
                    continue
                if self._driver.has_chunk(chunk_id):
                    self._driver.delete_chunk(chunk_id)
                    deleted.append(chunk_id)
                self._known_chunk_ids.discard(chunk_id)
                self._chunk_seen_at.pop(chunk_id, None)

        return GarbageCollectionReport(
            deleted_chunk_ids=tuple(deleted),
            retained_chunk_ids=tuple(sorted(referenced_chunks)),
        )


__all__ = [
    "DEFAULT_GC_MIN_AGE_SECONDS",
    "DEFAULT_LEASE_TTL_SECONDS",
    "GarbageCollectionReport",
    "PersonalCloudVaultManager",
    "SnapshotPullReport",
    "SnapshotPushReport",
    "_ChunkSourceLocator",
]
