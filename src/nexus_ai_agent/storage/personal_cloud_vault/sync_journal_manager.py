"""Differential sync engine, deduplication, ancestry guard, and orphan chunk GC.

:class:`PersonalCloudVaultManager` coordinates client-side chunk encryption,
content-addressed deduplication across consecutive project edits, optimistic
concurrency verification via ``parent_snapshot_id``, multi-layered race-free
in-flight chunk protection against concurrent garbage collection, fail-closed
verification before file reconstruction, and unreferenced chunk cleanup.
"""

from __future__ import annotations

import contextlib
import hmac
import secrets
import threading
import time
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import (
    BYOSStorageDriver,
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
    normalize_relative_vault_path,
    split_into_chunks,
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


def _collect_source_files(source: Path | str | Mapping[str, bytes]) -> dict[str, bytes]:
    """Read project files from a directory tree or in-memory mapping fail-closed."""
    if isinstance(source, Mapping):
        collected: dict[str, bytes] = {}
        for raw_rel, content in source.items():
            norm = normalize_relative_vault_path(str(raw_rel))
            collected[norm] = bytes(content)
        return dict(sorted(collected.items()))

    raw_dir = Path(source)
    if raw_dir.is_symlink() or not raw_dir.is_dir():
        raise VaultTransportError(
            f"Project source directory must be an existing non-symlink directory: {raw_dir}"
        )
    root = raw_dir.resolve()
    disk_files: dict[str, bytes] = {}
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            raise VaultTransportError(
                f"Symlinks are forbidden inside project vault source: {entry}"
            )
        if entry.is_file():
            rel_posix = entry.relative_to(root).as_posix()
            norm = normalize_relative_vault_path(rel_posix)
            try:
                disk_files[norm] = entry.read_bytes()
            except OSError as exc:
                raise VaultTransportError(f"Failed reading project file {entry}: {exc}") from exc
    return dict(sorted(disk_files.items()))


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
        # Refuse symlinked intermediate directories before resolving.
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
    """Differential encrypted BYOS sync manager with ancestry and tamper protection."""

    def __init__(
        self,
        driver: BYOSStorageDriver,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        kdf_iterations: int = PBKDF2_ITERATIONS,
        pad_to_block_size: int = 1,
    ) -> None:
        validate_chunk_size(chunk_size)
        validate_pad_block_size(pad_to_block_size)
        if kdf_iterations < MIN_PBKDF2_ITERATIONS:
            raise PersonalVaultError(
                f"kdf_iterations must be at least {MIN_PBKDF2_ITERATIONS}, got {kdf_iterations}"
            )
        self._driver = driver
        self._chunk_size = chunk_size
        self._kdf_iterations = kdf_iterations
        self._pad_to_block_size = pad_to_block_size
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
        ts = time.time() if recorded_at is None else recorded_at
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
        wire_bytes = envelope.to_bytes()
        self._driver.put_chunk(chunk_id, wire_bytes)
        with self._lock:
            self._known_chunk_ids.add(chunk_id)
            self._chunk_seen_at[chunk_id] = time.time()
        return len(wire_bytes)

    def _verify_and_heal_manifest_chunks(
        self,
        chunk_plaintexts: Mapping[str, bytes],
        key_material: VaultKeyMaterial,
        *,
        stage: str,
        rollback_snapshot_id: str | None = None,
    ) -> tuple[int, int]:
        """Verify every manifest chunk exists on storage; heal if evicted or fail closed."""
        healed_count = 0
        healed_bytes = 0
        for cid in sorted(chunk_plaintexts):
            if self._driver.has_chunk(cid):
                continue
            written = self._encrypt_and_upload_chunk(cid, chunk_plaintexts[cid], key_material)
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
        """Encrypt and differentially upload a project snapshot with conflict detection."""
        if not project_id.strip():
            raise PersonalVaultError("project_id must not be empty")

        all_manifest_ids, project_manifests = self._load_project_manifests(project_id)
        if snapshot_id is not None and snapshot_id in all_manifest_ids:
            raise VaultRevisionConflictError(
                f"Snapshot {snapshot_id!r} already exists on the remote vault"
            )

        if not project_manifests:
            if parent_snapshot_id is not None:
                raise VaultRevisionConflictError(
                    f"Remote vault has no history for project {project_id!r}, "
                    f"cannot reference parent_snapshot_id={parent_snapshot_id!r}"
                )
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

        source_files = _collect_source_files(source)
        file_entries: list[VaultFileEntry] = []
        chunk_plaintexts_by_id: dict[str, bytes] = {}
        prepared_files: list[tuple[str, bytes, list[VaultChunkRef], list[tuple[str, bytes]]]] = []

        total_chunks = 0
        for rel_path, file_bytes in source_files.items():
            raw_chunks = split_into_chunks(file_bytes, chunk_size=self._chunk_size)
            chunk_refs: list[VaultChunkRef] = []
            chunk_pairs: list[tuple[str, bytes]] = []
            for idx, chunk_plain in enumerate(raw_chunks):
                total_chunks += 1
                chunk_id = compute_chunk_id(chunk_plain, key_material)
                chunk_plaintexts_by_id[chunk_id] = chunk_plain
                chunk_pairs.append((chunk_id, chunk_plain))
                chunk_refs.append(
                    VaultChunkRef(
                        chunk_index=idx,
                        chunk_id=chunk_id,
                        plaintext_len=len(chunk_plain),
                    )
                )
            prepared_files.append((rel_path, file_bytes, chunk_refs, chunk_pairs))

        # Lease all snapshot chunks in `_in_flight_chunks` BEFORE any upload begins.
        acquired_ids = self._acquire_in_flight_chunks(chunk_plaintexts_by_id.keys())
        try:
            uploaded_in_batch: set[str] = set()
            uploaded_chunks = 0
            deduplicated_chunks = 0
            uploaded_bytes = 0
            saved_plaintext_bytes = 0

            for rel_path, file_bytes, chunk_refs, chunk_pairs in prepared_files:
                for chunk_id, chunk_plain in chunk_pairs:
                    if chunk_id in uploaded_in_batch or self._driver.has_chunk(chunk_id):
                        deduplicated_chunks += 1
                        saved_plaintext_bytes += len(chunk_plain)
                    else:
                        written = self._encrypt_and_upload_chunk(
                            chunk_id, chunk_plain, key_material
                        )
                        uploaded_in_batch.add(chunk_id)
                        uploaded_chunks += 1
                        uploaded_bytes += written

                file_mac_hex = compute_file_mac(rel_path, file_bytes, key_material)
                file_entries.append(
                    VaultFileEntry(
                        rel_path=rel_path,
                        total_size=len(file_bytes),
                        file_mac_hex=file_mac_hex,
                        chunks=tuple(chunk_refs),
                    )
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

            # Pre-manifest check: heal any chunk evicted concurrently by external actors.
            self._verify_and_heal_manifest_chunks(
                chunk_plaintexts_by_id,
                key_material,
                stage="pre-manifest",
            )
            self._driver.put_manifest(manifest.snapshot_id, manifest.to_json_bytes())

            # Post-manifest check: ensure every chunk remains committed after manifest write.
            self._verify_and_heal_manifest_chunks(
                chunk_plaintexts_by_id,
                key_material,
                stage="post-manifest",
                rollback_snapshot_id=manifest.snapshot_id,
            )
        finally:
            self._release_in_flight_chunks(acquired_ids)

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
                if isinstance(mtime_val, (int, float)):
                    recorded_ts = float(mtime_val)
        if recorded_ts is None:
            return False
        return (now_epoch - recorded_ts) < min_age_seconds

    def garbage_collect_unreferenced_chunks(
        self,
        *,
        active_snapshot_ids: Collection[str] | None = None,
        candidate_chunk_ids: Iterable[str] | None = None,
        protected_chunk_ids: Collection[str] = (),
        min_age_seconds: float = 0.0,
    ) -> GarbageCollectionReport:
        """Delete orphan chunks not referenced by active manifests or in-flight pushes."""
        if min_age_seconds < 0.0:
            raise PersonalVaultError(f"min_age_seconds must be non-negative, got {min_age_seconds}")

        with self._lock:
            initial_in_flight = set(self._in_flight_chunks)
            known_snapshot = set(self._known_chunk_ids)
            seen_snapshot = dict(self._chunk_seen_at)

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

        protected_all = referenced_chunks | set(protected_chunk_ids) | initial_in_flight
        discovered_candidates: set[str] = known_snapshot | superseded_chunks
        if candidate_chunk_ids is not None:
            discovered_candidates.update(candidate_chunk_ids)
        list_chunks_fn = getattr(self._driver, "list_chunks", None)
        if callable(list_chunks_fn):
            listed = list_chunks_fn()
            if isinstance(listed, Iterable):
                discovered_candidates.update(str(cid) for cid in listed)

        now_epoch = time.time()
        deleted: list[str] = []
        for chunk_id in sorted(discovered_candidates):
            if chunk_id in protected_all:
                continue
            if self._is_younger_than_min_age(
                chunk_id,
                min_age_seconds=min_age_seconds,
                now_epoch=now_epoch,
                seen_snapshot=seen_snapshot,
            ):
                continue
            with self._lock:
                if chunk_id in self._in_flight_chunks:
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
    "GarbageCollectionReport",
    "PersonalCloudVaultManager",
    "SnapshotPullReport",
    "SnapshotPushReport",
]
