"""Hot backup, signed-bundle verification, staged restore and admission control.

Three ways a Studio deployment loses work, and what this module does about each:

1. **The backup that was not a backup.** Copying a live SQLite file with
   ``shutil.copy`` copies a torn page cache and silently drops the ``-wal``
   frames, so the "backup" opens fine and is missing the last transactions.
   :meth:`StudioDisasterRecoveryManager.create_hot_backup` uses
   :meth:`sqlite3.Connection.backup`, the only supported online-backup API: it
   takes a consistent snapshot while writers continue.  The bundle records the
   SHA-256 and byte size of the copy, every table with its row count, and an
   HMAC-SHA256 over the manifest, so a backup can be *proven* rather than hoped
   to be good.
2. **The restore that trusted the file it was handed.** A restore blindly
   replacing a live database can install a truncated file, or replay a stale
   ``-wal``/``-shm`` on top of the new pages.  :meth:`restore_from_backup`
   re-verifies the bundle first, takes an ``fcntl.flock`` on the target, stages
   the copy beside the target and ``PRAGMA integrity_check``\\ s *the staged
   file*, removes the stale sidecars, and only then swaps it into place with
   ``os.replace``.  A failure at any step leaves the target untouched.
3. **The cancellation that over-admitted the machine.** A coroutine cancelled
   mid-job used to free its worker slot while the worker thread was still
   running, so the governor admitted more concurrent jobs than the box can take.
   :meth:`StudioResourceGovernor.run_with_lease` shields the worker, and on
   ``asyncio.CancelledError`` hands the lease to the thread: the slot returns to
   the pool only when the work actually finishes.

Secrets never leave :class:`BackupKeyMaterial`: its ``__repr__``, ``__str__``
and :meth:`redacted_dict` show the key id and nothing else.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat as stat_module
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_digest,
)

__all__ = [
    "AdmissionLease",
    "BackupBundleReport",
    "BackupKeyMaterial",
    "BackupManifest",
    "DisasterRecoveryError",
    "GovernorSaturatedError",
    "RestoreResult",
    "StudioDisasterRecoveryManager",
    "StudioResourceGovernor",
    "utc_now",
]

_T = TypeVar("_T")

HOT_BACKUP_FILENAME = "hot_backup.sqlite3"
MANIFEST_FILENAME = "backup_manifest.json"
MANIFEST_SCHEMA = "nexus.studio.dr_backup_manifest.v1"
_KEY_BYTES = 32

#: A bundle filename is a *name*, never a path: lowercase/uppercase letters,
#: digits, ``_``, ``.`` and ``-``, ending in ``.sqlite3``.  Anything that could
#: traverse (``/``, ``\``, ``..``), hide (a leading dot) or name an absolute
#: location is refused by :class:`BackupManifest` before it can be joined onto a
#: bundle directory at all.
_BACKUP_FILENAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.sqlite3$")


def _validate_backup_filename(name: str) -> str:
    """Refuse anything that is not a plain ``*.sqlite3`` file name.

    This is the first of the two independent path-traversal defences: the
    manifest can never *hold* a traversal, so a bundle directory join cannot
    *produce* one.  The second (in :meth:`verify_backup_bundle`) re-proves
    containment on the resolved path, because a defence that only exists in one
    layer is a defence that disappears with one mutation.
    """
    if not isinstance(name, str) or not name.strip():
        raise DisasterRecoveryError("invalid_backup_filename", "backup_filename must be a name")
    if "/" in name or "\\" in name or ".." in name or "\x00" in name:
        raise DisasterRecoveryError(
            "invalid_backup_filename",
            f"{name!r} is a path, not a filename (separator or '..' present)",
        )
    if Path(name).is_absolute() or Path(name).name != name:
        raise DisasterRecoveryError(
            "invalid_backup_filename", f"{name!r} names a location, not a file"
        )
    if _BACKUP_FILENAME_RE.match(name) is None:
        raise DisasterRecoveryError(
            "invalid_backup_filename",
            f"{name!r} must match ^[A-Za-z0-9_][A-Za-z0-9_.-]*\\.sqlite3$",
        )
    return name


def utc_now() -> str:
    """One spelling of "now" for every durable timestamp in this module."""
    return datetime.now(timezone.utc).isoformat()


class DisasterRecoveryError(ValueError):
    """Typed, fail-closed refusal from the disaster-recovery manager."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class GovernorSaturatedError(RuntimeError):
    """The governor refuses to admit more work right now."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- #
# Key material
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BackupKeyMaterial:
    """An HMAC key for signing backup manifests.

    The secret is deliberately hard to leak by accident: ``repr``, ``str`` and
    ``redacted_dict`` show only the key id.  Callers that genuinely need the
    bytes ask :meth:`secret_bytes` explicitly.
    """

    key_id: str
    secret_hex: str

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not self.key_id.strip():
            raise DisasterRecoveryError("invalid_field", "key_id must be a non-empty string")
        if not isinstance(self.secret_hex, str) or not self.secret_hex.strip():
            raise DisasterRecoveryError("invalid_field", "secret_hex must be a non-empty string")
        normalised = self.secret_hex.strip().lower()
        if len(normalised) % 2:
            raise DisasterRecoveryError(
                "invalid_key", "secret_hex must have an even number of hex digits"
            )
        try:
            raw = bytes.fromhex(normalised)
        except ValueError as exc:
            raise DisasterRecoveryError("invalid_key", f"secret_hex is not hex ({exc})") from exc
        if len(raw) < _KEY_BYTES:
            raise DisasterRecoveryError(
                "weak_key",
                f"backup key must be at least {_KEY_BYTES} bytes, got {len(raw)}",
            )
        object.__setattr__(self, "secret_hex", normalised)

    @classmethod
    def generate(cls, key_id: str = "bk-default") -> BackupKeyMaterial:
        """Fresh 256-bit key from the OS CSPRNG."""
        return cls(key_id=key_id, secret_hex=os.urandom(_KEY_BYTES).hex())

    def secret_bytes(self) -> bytes:
        """The raw key.  The only deliberate path to the secret."""
        return bytes.fromhex(self.secret_hex)

    def redacted_dict(self) -> dict[str, str]:
        return {"key_id": self.key_id, "secret_hex": "<redacted>"}

    def __repr__(self) -> str:
        return f"BackupKeyMaterial(key_id={self.key_id!r}, secret_hex='<redacted>')"

    def __str__(self) -> str:
        return self.__repr__()

    def sign(self, payload: Mapping[str, Any]) -> str:
        """HMAC-SHA256 over the RFC 8785 JCS canonical *digest* of ``payload``.

        Uses the versioned canonicalization authority (naming
        ``JCS_RFC8785_V2`` explicitly) rather than reaching into the JCS
        implementation, so a signature can never be produced by an unversioned
        default.  The digest is itself ``sha256:``-prefixed canonical text; the
        HMAC then binds it to this key.
        """
        canonical = canonical_digest(dict(payload), CanonicalizationScheme.JCS_RFC8785_V2)
        return hmac.new(self.secret_bytes(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, payload: Mapping[str, Any], signature_hex: str) -> bool:
        """Constant-time signature check; never raises on a bad signature."""
        if not isinstance(signature_hex, str) or not signature_hex:
            return False
        return hmac.compare_digest(self.sign(payload), signature_hex.strip().lower())


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BackupManifest:
    """The signed description of one hot-backup bundle."""

    manifest_id: str
    bundle_id: str
    created_at_utc: str
    source_name: str
    backup_filename: str
    backup_sha256: str
    backup_size_bytes: int
    tables: Mapping[str, int] = field(default_factory=dict)
    total_rows: int = 0
    labels: Mapping[str, str] = field(default_factory=dict)
    key_id: str = ""
    manifest_hmac_sha256: str = ""
    schema: str = MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "manifest_id",
            "bundle_id",
            "created_at_utc",
            "source_name",
            "backup_filename",
            "key_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DisasterRecoveryError("invalid_field", f"{name} must be a non-empty string")
        digest = self.backup_sha256.strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise DisasterRecoveryError(
                "invalid_digest", "backup_sha256 must be 64 lowercase hex characters"
            )
        object.__setattr__(self, "backup_sha256", digest)
        if isinstance(self.backup_size_bytes, bool) or self.backup_size_bytes < 0:
            raise DisasterRecoveryError(
                "invalid_byte_size", "backup_size_bytes must be a non-negative int"
            )
        counts = {str(name): int(count) for name, count in self.tables.items()}
        for name, count in counts.items():
            if count < 0:
                raise DisasterRecoveryError(
                    "invalid_row_count", f"table {name!r} reports {count} rows"
                )
        object.__setattr__(self, "tables", dict(sorted(counts.items())))
        object.__setattr__(self, "total_rows", sum(counts.values()))
        object.__setattr__(
            self, "labels", {str(key): str(value) for key, value in sorted(self.labels.items())}
        )
        # Path-traversal defence, layer 1: the manifest may only ever name a
        # canonical ``*.sqlite3`` file, never a relative or absolute path.
        _validate_backup_filename(self.backup_filename)
        if self.schema != MANIFEST_SCHEMA:
            raise DisasterRecoveryError("invalid_schema", f"unsupported schema {self.schema!r}")

    # -- signing ----------------------------------------------------------- #

    def signed_payload(self) -> dict[str, Any]:
        """Every field except the signature itself."""
        return {
            "schema": self.schema,
            "manifest_id": self.manifest_id,
            "bundle_id": self.bundle_id,
            "created_at_utc": self.created_at_utc,
            "source_name": self.source_name,
            "backup_filename": self.backup_filename,
            "backup_sha256": self.backup_sha256,
            "backup_size_bytes": self.backup_size_bytes,
            "tables": {name: self.tables[name] for name in sorted(self.tables)},
            "total_rows": self.total_rows,
            "labels": {name: self.labels[name] for name in sorted(self.labels)},
            "key_id": self.key_id,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.signed_payload()
        payload["manifest_hmac_sha256"] = self.manifest_hmac_sha256
        return payload

    def with_signature(self, key: BackupKeyMaterial) -> BackupManifest:
        """Return a signed copy (the manifest is immutable by design)."""
        if key.key_id != self.key_id:
            raise DisasterRecoveryError(
                "key_id_mismatch",
                f"manifest names key {self.key_id!r}, signer is {key.key_id!r}",
            )
        signature = key.sign(self.signed_payload())
        return BackupManifest(
            manifest_id=self.manifest_id,
            bundle_id=self.bundle_id,
            created_at_utc=self.created_at_utc,
            source_name=self.source_name,
            backup_filename=self.backup_filename,
            backup_sha256=self.backup_sha256,
            backup_size_bytes=self.backup_size_bytes,
            tables=self.tables,
            total_rows=self.total_rows,
            labels=self.labels,
            key_id=self.key_id,
            manifest_hmac_sha256=signature,
        )

    def verify_signature(self, key: BackupKeyMaterial) -> bool:
        """True only for a signature this key produced over exactly these fields."""
        if key.key_id != self.key_id:
            return False
        return key.verify(self.signed_payload(), self.manifest_hmac_sha256)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BackupManifest:
        """Parse a persisted manifest, refusing anything that does not fit the schema."""
        if not isinstance(payload, Mapping):
            raise DisasterRecoveryError("malformed_manifest", "manifest must be a JSON object")
        try:
            return cls(
                manifest_id=str(payload["manifest_id"]),
                bundle_id=str(payload["bundle_id"]),
                created_at_utc=str(payload["created_at_utc"]),
                source_name=str(payload["source_name"]),
                backup_filename=str(payload["backup_filename"]),
                backup_sha256=str(payload["backup_sha256"]),
                backup_size_bytes=int(payload["backup_size_bytes"]),
                tables={str(k): int(v) for k, v in dict(payload.get("tables") or {}).items()},
                labels={str(k): str(v) for k, v in dict(payload.get("labels") or {}).items()},
                key_id=str(payload["key_id"]),
                manifest_hmac_sha256=str(payload.get("manifest_hmac_sha256") or ""),
                schema=str(payload.get("schema") or MANIFEST_SCHEMA),
            )
        except KeyError as exc:
            raise DisasterRecoveryError(
                "malformed_manifest", f"manifest is missing {exc.args[0]!r}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise DisasterRecoveryError("malformed_manifest", str(exc)) from exc


@dataclass(frozen=True)
class BackupBundleReport:
    """The outcome of verifying a bundle, field by field."""

    bundle_id: str
    verified: bool
    manifest: BackupManifest | None
    problems: tuple[str, ...]
    integrity_check: str
    actual_sha256: str
    actual_size_bytes: int
    actual_tables: Mapping[str, int] = field(default_factory=dict)
    actual_total_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "verified": self.verified,
            "problems": list(self.problems),
            "integrity_check": self.integrity_check,
            "actual_sha256": self.actual_sha256,
            "actual_size_bytes": self.actual_size_bytes,
            "actual_tables": {
                name: self.actual_tables[name] for name in sorted(self.actual_tables)
            },
            "actual_total_rows": self.actual_total_rows,
            "manifest": None if self.manifest is None else self.manifest.to_dict(),
        }


@dataclass(frozen=True)
class RestoreResult:
    """What a completed restore did, in numbers a human can audit."""

    bundle_id: str
    target_path: str
    restored_sha256: str
    restored_size_bytes: int
    tables: Mapping[str, int]
    total_rows: int
    staged_path: str
    quarantined_path: str
    removed_sidecars: tuple[str, ...]
    integrity_check: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "target_path": self.target_path,
            "restored_sha256": self.restored_sha256,
            "restored_size_bytes": self.restored_size_bytes,
            "tables": {name: self.tables[name] for name in sorted(self.tables)},
            "total_rows": self.total_rows,
            "staged_path": self.staged_path,
            "quarantined_path": self.quarantined_path,
            "removed_sidecars": list(self.removed_sidecars),
            "integrity_check": self.integrity_check,
        }


# --------------------------------------------------------------------------- #
# The manager
# --------------------------------------------------------------------------- #
class StudioDisasterRecoveryManager:
    """Consistent hot backups, proven bundles, and staged restores.

    Parameters
    ----------
    backup_root:
        Directory the bundles are written under (created if absent).
    key_material:
        The HMAC key every manifest is signed with.  Verification refuses a
        manifest signed by a different key rather than trying others.
    now:
        Injected clock (ISO-8601 UTC string) for deterministic tests.
    """

    def __init__(
        self,
        backup_root: str | Path,
        *,
        key_material: BackupKeyMaterial,
        now: Callable[[], str] = utc_now,
    ) -> None:
        if not isinstance(key_material, BackupKeyMaterial):
            raise DisasterRecoveryError("invalid_key", "key_material must be a BackupKeyMaterial")
        self._root = Path(backup_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._key = key_material
        self._now = now

    @property
    def backup_root(self) -> Path:
        return self._root

    @property
    def key_id(self) -> str:
        return self._key.key_id

    # -- backup ------------------------------------------------------------ #

    def create_hot_backup(
        self,
        *,
        database_path: str | Path,
        labels: Mapping[str, str] | None = None,
    ) -> BackupManifest:
        """Snapshot a live database with ``Connection.backup`` and sign the bundle.

        The source is opened read-only and the snapshot is taken by SQLite
        itself, so concurrent writers cannot tear it and the ``-wal`` frames
        that are live at that instant are included.
        """
        source = Path(database_path)
        if not source.is_file():
            raise DisasterRecoveryError("source_missing", f"{source} is not a file")
        bundle_id = "bkp_" + uuid.uuid4().hex
        bundle_dir = self._root / bundle_id
        bundle_dir.mkdir(parents=True, exist_ok=False)
        target = bundle_dir / HOT_BACKUP_FILENAME
        try:
            self._copy_database(source, target)
            integrity = _integrity_check(target)
            if integrity != "ok":
                raise DisasterRecoveryError(
                    "backup_integrity_failed",
                    f"the fresh snapshot failed integrity_check: {integrity}",
                )
            tables = _table_row_counts(target)
            digest, size = _sha256_file(target)
            manifest = BackupManifest(
                manifest_id="mnf_" + uuid.uuid4().hex,
                bundle_id=bundle_id,
                created_at_utc=self._now(),
                source_name=source.name,
                backup_filename=HOT_BACKUP_FILENAME,
                backup_sha256=digest,
                backup_size_bytes=size,
                tables=tables,
                labels=dict(labels or {}),
                key_id=self._key.key_id,
            ).with_signature(self._key)
            _write_manifest(bundle_dir / MANIFEST_FILENAME, manifest)
        except BaseException:
            # A half-written bundle must not look like a backup.
            shutil.rmtree(bundle_dir, ignore_errors=True)
            raise
        return manifest

    @staticmethod
    def _copy_database(source: Path, target: Path) -> None:
        """SQLite's online backup API — never ``shutil.copy`` on a live database."""
        source_uri = f"file:{source}?mode=ro"
        reader = sqlite3.connect(source_uri, uri=True, timeout=30.0)
        try:
            writer = sqlite3.connect(str(target), timeout=30.0)
            try:
                reader.backup(writer)  # consistent snapshot, even mid-write
                writer.commit()
            finally:
                writer.close()
        finally:
            reader.close()
        _fsync_file(target)

    # -- verification ------------------------------------------------------ #

    def verify_backup_bundle(
        self, bundle: str | Path, *, key_material: BackupKeyMaterial | None = None
    ) -> BackupBundleReport:
        """Verify signature, digest, size, integrity and row counts.

        The order of operations is the security property, not an implementation
        detail:

        1. **Authenticate the manifest before any I/O on its say-so.**  If the
           HMAC does not verify, nothing else happens — no ``backup_path`` is
           opened, no byte is hashed, no SQLite handle is created.  An attacker
           who can write a manifest but not sign it therefore cannot use
           verification as a read primitive.
        2. **Prove path containment on the resolved path.**  ``lstat`` rejects
           symlinks and non-regular files, and ``resolve(strict=True)`` must land
           inside the bundle root.
        3. **Never mutate.**  Verification is read-only: unexpected ``-wal`` /
           ``-shm`` sidecars are *reported*, never unlinked.  Deleting files
           beside an arbitrary caller-supplied path is how a "verify" turns into
           a delete primitive.

        Every problem is collected rather than short-circuited so an operator
        sees the whole picture, except for the two gates above, which return
        immediately by design.
        """
        key = key_material or self._key
        bundle_dir = Path(bundle)
        problems: list[str] = []
        manifest_path = bundle_dir / MANIFEST_FILENAME
        manifest: BackupManifest | None = None
        if not bundle_dir.is_dir():
            return BackupBundleReport(
                bundle_id=bundle_dir.name,
                verified=False,
                manifest=None,
                problems=("bundle_missing",),
                integrity_check="not_checked",
                actual_sha256="",
                actual_size_bytes=0,
            )
        if not manifest_path.is_file():
            return BackupBundleReport(
                bundle_id=bundle_dir.name,
                verified=False,
                manifest=None,
                problems=("manifest_missing",),
                integrity_check="not_checked",
                actual_sha256="",
                actual_size_bytes=0,
            )
        try:
            manifest = BackupManifest.from_dict(json.loads(manifest_path.read_text("utf-8")))
        except (json.JSONDecodeError, DisasterRecoveryError) as exc:
            return BackupBundleReport(
                bundle_id=bundle_dir.name,
                verified=False,
                manifest=None,
                problems=(f"manifest_malformed: {exc}",),
                integrity_check="not_checked",
                actual_sha256="",
                actual_size_bytes=0,
            )

        # -- GATE 1: cryptographic gate, before any untrusted-path I/O -------- #
        if not manifest.verify_signature(key):
            return BackupBundleReport(
                bundle_id=manifest.bundle_id,
                verified=False,
                manifest=manifest,
                problems=("signature_invalid",),
                integrity_check="not_checked",
                actual_sha256="",
                actual_size_bytes=0,
            )

        # -- GATE 2: containment, on the *resolved* path ---------------------- #
        try:
            bundle_root = bundle_dir.resolve(strict=True)
            candidate = bundle_root / manifest.backup_filename
            linked = os.lstat(candidate)
        except OSError:
            return BackupBundleReport(
                bundle_id=manifest.bundle_id,
                verified=False,
                manifest=manifest,
                problems=("backup_missing",),
                integrity_check="not_checked",
                actual_sha256="",
                actual_size_bytes=0,
            )
        if stat_module.S_ISLNK(linked.st_mode):
            return BackupBundleReport(
                bundle_id=manifest.bundle_id,
                verified=False,
                manifest=manifest,
                problems=("backup_symlink_rejected",),
                integrity_check="not_checked",
                actual_sha256="",
                actual_size_bytes=0,
            )
        if not stat_module.S_ISREG(linked.st_mode):
            return BackupBundleReport(
                bundle_id=manifest.bundle_id,
                verified=False,
                manifest=manifest,
                problems=("backup_not_a_regular_file",),
                integrity_check="not_checked",
                actual_sha256="",
                actual_size_bytes=0,
            )
        backup_path = candidate.resolve(strict=True)
        if not backup_path.is_relative_to(bundle_root):
            return BackupBundleReport(
                bundle_id=manifest.bundle_id,
                verified=False,
                manifest=manifest,
                problems=("backup_path_escape",),
                integrity_check="not_checked",
                actual_sha256="",
                actual_size_bytes=0,
            )

        # -- GATE 3: read-only verification ---------------------------------- #
        # A ``-wal`` that still holds frames is un-checkpointed data the bundle
        # does *not* account for: that is a real finding.  An empty ``-wal`` and
        # a ``-shm`` are pure bookkeeping that SQLite itself materialises on any
        # open (including the read this method performs), so flagging those would
        # make verification non-idempotent -- the second pass would condemn the
        # sidecars the first pass created.
        wal = backup_path.with_name(backup_path.name + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            problems.append("unexpected_sidecar: wal")
        actual_digest, actual_size = _sha256_file(backup_path)
        if not hmac.compare_digest(actual_digest, manifest.backup_sha256):
            problems.append("digest_mismatch")
        if actual_size != manifest.backup_size_bytes:
            problems.append("size_mismatch")
        integrity = _integrity_check(backup_path, cleanup=False)
        if integrity != "ok":
            problems.append(f"integrity_check_failed: {integrity}")
        actual_tables = _table_row_counts(backup_path, cleanup=False)
        if dict(actual_tables) != dict(manifest.tables):
            problems.append("table_counts_mismatch")
        actual_total = sum(actual_tables.values())
        if actual_total != manifest.total_rows:
            problems.append("total_rows_mismatch")
        return BackupBundleReport(
            bundle_id=manifest.bundle_id,
            verified=not problems,
            manifest=manifest,
            problems=tuple(problems),
            integrity_check=integrity,
            actual_sha256=actual_digest,
            actual_size_bytes=actual_size,
            actual_tables=actual_tables,
            actual_total_rows=actual_total,
        )

    # -- restore ----------------------------------------------------------- #

    def restore_from_backup(
        self,
        bundle: str | Path,
        *,
        target_database_path: str | Path,
        key_material: BackupKeyMaterial | None = None,
    ) -> RestoreResult:
        """Replace ``target_database_path`` with a verified bundle's snapshot.

        Order matters and is the whole safety argument:

        1. verify the bundle (refuse if anything is off);
        2. take ``fcntl.flock`` on the target so two restores cannot interleave;
        3. snapshot the *current* target aside (quarantine) so a bad restore is
           reversible;
        4. copy into a staging file **beside** the target, fsync it, and run
           ``integrity_check`` on the staged copy;
        5. remove the stale ``-wal``/``-shm`` (they belong to the replaced
           database and would otherwise be replayed onto the new pages);
        6. ``os.replace`` the staged file into place — atomic on POSIX.
        """
        bundle_dir = Path(bundle)
        key = key_material or self._key
        report = self.verify_backup_bundle(bundle_dir, key_material=key)
        if not report.verified or report.manifest is None:
            # ``unexpected_sidecar`` alone is *not* enough to refuse a restore:
            # a read-only verification legitimately makes SQLite materialise a
            # ``-wal``/``-shm`` beside a WAL-mode backup, and the restore below
            # supersedes them anyway (step 5).  Every other problem is fatal.
            blocking = tuple(
                problem
                for problem in report.problems
                if not problem.startswith("unexpected_sidecar:")
            )
            if blocking or report.manifest is None:
                raise DisasterRecoveryError(
                    "backup_unverified",
                    f"refusing to restore bundle {bundle_dir.name!r}: "
                    f"{', '.join(blocking) or 'unknown problem'}",
                )
        manifest = report.manifest
        backup_path = bundle_dir / manifest.backup_filename
        target = Path(target_database_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name(target.name + ".restore.lock")
        handle = _acquire_flock(lock_path)
        try:
            staged = target.with_name(f"{target.name}.restoring-{uuid.uuid4().hex}")
            quarantined = ""
            removed: list[str] = []
            try:
                shutil.copyfile(backup_path, staged)
                _fsync_file(staged)
                staged_integrity = _integrity_check(staged)
                if staged_integrity != "ok":
                    raise DisasterRecoveryError(
                        "staged_integrity_failed",
                        f"the staged copy failed integrity_check: {staged_integrity}",
                    )
                staged_tables = _table_row_counts(staged)
                if dict(staged_tables) != dict(manifest.tables):
                    raise DisasterRecoveryError(
                        "staged_counts_mismatch",
                        "the staged copy's row counts differ from the manifest",
                    )
                if target.is_file():
                    quarantined = str(
                        target.with_name(f"{target.name}.prerestore-{uuid.uuid4().hex[:12]}")
                    )
                    self._copy_database(target, Path(quarantined))
                    _remove_sidecars(Path(quarantined))
                removed.extend(_remove_sidecars(target))
                os.replace(staged, target)
                _fsync_file(target)
            except BaseException:
                staged.unlink(missing_ok=True)
                _remove_sidecars(staged)
                raise
            # A freshly restored database is a single file: make sure no sidecar
            # appeared while we were swapping.
            for suffix in _remove_sidecars(target):
                if suffix not in removed:
                    removed.append(suffix)
        finally:
            _release_flock(handle)
        post_integrity = _integrity_check(target)
        if post_integrity != "ok":
            raise DisasterRecoveryError(
                "restore_integrity_failed",
                f"the restored database failed integrity_check: {post_integrity}",
            )
        restored_digest, restored_size = _sha256_file(target)
        restored_tables = _table_row_counts(target)
        return RestoreResult(
            bundle_id=manifest.bundle_id,
            target_path=str(target),
            restored_sha256=restored_digest,
            restored_size_bytes=restored_size,
            tables=restored_tables,
            total_rows=sum(restored_tables.values()),
            staged_path=str(staged),
            quarantined_path=quarantined,
            removed_sidecars=tuple(removed),
            integrity_check=post_integrity,
        )

    # -- housekeeping ------------------------------------------------------ #

    def list_bundles(self) -> tuple[str, ...]:
        """Bundle ids under the root, newest last (directory name order)."""
        return tuple(
            sorted(
                path.name
                for path in self._root.iterdir()
                if path.is_dir() and (path / MANIFEST_FILENAME).is_file()
            )
        )

    def prune_bundles(self, *, keep: int = 7) -> tuple[str, ...]:
        """Delete all but the newest ``keep`` bundles; refuse ``keep < 1``."""
        if keep < 1:
            raise DisasterRecoveryError("invalid_keep", f"keep must be >= 1, got {keep}")
        bundles = self.list_bundles()
        doomed = bundles[:-keep] if len(bundles) > keep else ()
        for name in doomed:
            shutil.rmtree(self._root / name, ignore_errors=True)
        return doomed


# --------------------------------------------------------------------------- #
# File/t-SQL helpers
# --------------------------------------------------------------------------- #
def _sha256_file(path: Path) -> tuple[str, int]:
    """Streamed SHA-256 and byte size (never loads the database into memory)."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _fsync_file(path: Path) -> None:
    handle = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _integrity_check(path: Path, *, cleanup: bool = True) -> str:
    """``PRAGMA integrity_check`` on a read-only connection.

    ``cleanup`` is True for the internal staging/restore paths, which own the
    directory they are working in.  A *verification* read passes False: opening
    someone else's database read-only must not delete their files, even the
    ``-wal``/``-shm`` SQLite happens to materialise.
    """
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - unopenable file
        return f"open_failed: {exc}"
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "no_result"
    except sqlite3.Error as exc:
        return f"error: {exc}"
    finally:
        connection.close()
        if cleanup:
            _remove_sidecars(path)


def _table_row_counts(path: Path, *, cleanup: bool = True) -> dict[str, int]:
    """Row count for every user table, read through a read-only connection.

    A damaged file is a *finding*, not an exception: verification must be able to
    report "this bundle is corrupt", so an unreadable database yields an empty
    count map that then fails the manifest comparison.

    ``cleanup`` mirrors :func:`_integrity_check`: an internal staging path owns
    its sidecars, a verification target does not.
    """
    try:
        return _table_row_counts_or_raise(path, cleanup=cleanup)
    except sqlite3.Error:
        return {}


def _table_row_counts_or_raise(path: Path, *, cleanup: bool = True) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for name in names:
            escaped = name.replace('"', '""')
            row = connection.execute(
                f'SELECT COUNT(*) FROM "{escaped}"'  # noqa: S608 - identifier is quoted+escaped
            ).fetchone()
            counts[name] = int(row[0]) if row else 0
        return counts
    finally:
        connection.close()
        if cleanup:
            _remove_sidecars(path)


def _remove_sidecars(path: Path) -> tuple[str, ...]:
    """Delete ``-wal``/``-shm`` beside ``path``; return the suffixes removed.

    A read-only open of a WAL-mode database still materialises these; the bundle
    is defined as exactly the backup file plus the manifest, so they are removed
    wherever a read leaves them (verification, staging, restore).
    """
    removed: list[str] = []
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.unlink(missing_ok=True)
            removed.append(suffix)
    return tuple(removed)


def _write_manifest(path: Path, manifest: BackupManifest) -> None:
    """Write the manifest canonically, then fsync it and its directory."""
    payload = manifest.to_dict()
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _fsync_file(path)
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _acquire_flock(lock_path: Path) -> int:
    """Exclusive ``fcntl.flock`` on a sidecar file (POSIX only)."""
    handle = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX)
    except ImportError:  # pragma: no cover - non-POSIX host
        return handle
    except BaseException:
        os.close(handle)
        raise
    return handle


def _release_flock(handle: int) -> None:
    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)
    except ImportError:  # pragma: no cover - non-POSIX host
        pass
    finally:
        os.close(handle)


# --------------------------------------------------------------------------- #
# Admission control
# --------------------------------------------------------------------------- #
class AdmissionLease:
    """One admitted unit of work.  Owned by the governor that issued it."""

    def __init__(self, governor: StudioResourceGovernor, lease_id: str) -> None:
        self._governor = governor
        self._lock = threading.Lock()
        self._released = False
        self.lease_id = lease_id
        self.detached = False
        self.acquired_at = time.monotonic()

    @property
    def held(self) -> bool:
        """True while this lease still occupies a slot."""
        with self._lock:
            return not self._released

    def release(self) -> None:
        """Give the slot back (idempotent)."""
        with self._lock:
            if self._released:
                return
            self._released = True
        self._governor._release(self)

    def detach(self) -> None:
        """Hand ownership to someone else (used on cancellation)."""
        with self._lock:
            self.detached = True

    def __enter__(self) -> AdmissionLease:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def __repr__(self) -> str:
        return f"AdmissionLease(lease_id={self.lease_id!r}, held={self.held})"


class StudioResourceGovernor:
    """Bounded concurrency with cancellation-safe leases.

    ``max_concurrent_jobs`` is a hard ceiling: :meth:`acquire` refuses beyond it
    and beyond ``max_queue_depth`` waiting callers, rather than queueing without
    bound.  :meth:`run_with_lease` additionally makes a *cancelled* coroutine
    keep its slot until the worker thread it started has actually finished.
    """

    def __init__(
        self,
        *,
        max_concurrent_jobs: int,
        max_queue_depth: int = 64,
        executor_workers: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(max_concurrent_jobs, bool) or max_concurrent_jobs < 1:
            raise GovernorSaturatedError(
                "invalid_limit", f"max_concurrent_jobs must be >= 1, got {max_concurrent_jobs!r}"
            )
        if isinstance(max_queue_depth, bool) or max_queue_depth < 0:
            raise GovernorSaturatedError(
                "invalid_limit", f"max_queue_depth must be >= 0, got {max_queue_depth!r}"
            )
        # The threads that run admitted callables are deliberately *at least* the
        # lease ceiling: sizing them to the ceiling exactly would make a caller
        # that blocks on a nested admission deadlock the pool against itself.
        resolved_workers = (
            executor_workers if executor_workers is not None else max(4, max_concurrent_jobs)
        )
        if isinstance(resolved_workers, bool) or resolved_workers < 1:
            raise GovernorSaturatedError(
                "invalid_limit",
                f"executor_workers must be >= 1, got {resolved_workers!r}",
            )
        self._executor_workers = int(resolved_workers)
        self._max_jobs = int(max_concurrent_jobs)
        self._max_queue = int(max_queue_depth)
        self._clock = clock
        self._lock = threading.Lock()
        self._active: dict[str, AdmissionLease] = {}
        self._waiting = 0
        self._peak = 0
        self._admitted = 0
        self._rejected = 0
        self._completed = 0
        self._executor: ThreadPoolExecutor | None = None

    @property
    def max_concurrent_jobs(self) -> int:
        return self._max_jobs

    @property
    def active_leases(self) -> int:
        with self._lock:
            return len(self._active)

    def stats(self) -> dict[str, int]:
        """A snapshot for metrics/health endpoints."""
        with self._lock:
            return {
                "active": len(self._active),
                "waiting": self._waiting,
                "peak": self._peak,
                "admitted": self._admitted,
                "rejected": self._rejected,
                "completed": self._completed,
                "max_concurrent_jobs": self._max_jobs,
                "max_queue_depth": self._max_queue,
                "max_executor_workers": self._executor_workers,
            }

    def acquire(self) -> AdmissionLease:
        """Take a slot, or refuse (never block indefinitely)."""
        return self._acquire_interruptible(cancel_event=None)

    def _acquire_interruptible(
        self,
        *,
        cancel_event: threading.Event | None = None,
        timeout_s: float | None = None,
    ) -> AdmissionLease:
        """Acquire a slot, abandoning the wait when ``cancel_event`` is set.

        The waiter polls ``cancel_event`` between slot checks, so an abandoned
        ``asyncio`` task can tell its worker thread to *stop waiting* rather than
        leaving it to acquire a slot nobody will ever release.  The final
        acquire-and-register step is decided under the governor's lock with a
        last cancellation check, so a cancellation that races the slot opening
        cannot leak a lease: either the event is seen (no lease is allocated) or
        the lease is allocated and returned to the caller, who releases it.
        """
        deadline = None if timeout_s is None else self._clock() + timeout_s
        with self._lock:
            if len(self._active) >= self._max_jobs:
                if self._waiting >= self._max_queue:
                    self._rejected += 1
                    raise GovernorSaturatedError(
                        "saturated",
                        f"{len(self._active)} active of {self._max_jobs} max, "
                        f"{self._waiting} waiting of {self._max_queue} max",
                    )
                self._waiting += 1
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise GovernorSaturatedError(
                        "cancelled", "the waiter was cancelled before a slot opened"
                    )
                with self._lock:
                    if cancel_event is not None and cancel_event.is_set():
                        raise GovernorSaturatedError(
                            "cancelled", "the waiter was cancelled before a slot opened"
                        )
                    if len(self._active) < self._max_jobs:
                        lease = AdmissionLease(self, "lse_" + uuid.uuid4().hex)
                        self._active[lease.lease_id] = lease
                        self._admitted += 1
                        self._peak = max(self._peak, len(self._active))
                        self._waiting = max(0, self._waiting - 1)
                        return lease
                if deadline is not None and self._clock() >= deadline:
                    raise GovernorSaturatedError(
                        "acquire_timeout", "no slot opened inside the wait budget"
                    )
                time.sleep(0.001)
        except BaseException:
            with self._lock:
                self._waiting = max(0, self._waiting - 1)
            raise

    @contextmanager
    def admit(self) -> Iterator[AdmissionLease]:
        """Synchronous admission scope."""
        lease = self.acquire()
        try:
            yield lease
        finally:
            lease.release()

    def _release(self, lease: AdmissionLease) -> None:
        """Called by the lease; never raises."""
        with self._lock:
            existed = self._active.pop(lease.lease_id, None)
            if existed is not None:
                self._completed += 1

    # -- async, cancellation-safe ----------------------------------------- #

    async def run_with_lease(
        self,
        fn: Callable[..., _T],
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        """Run blocking ``fn`` in a bounded worker thread under one lease.

        Both cancellation windows are handled, and they are different:

        * **Cancelled while *waiting* for a slot.**  ``asyncio.to_thread`` cannot
          interrupt a running thread, so cancelling the await would leave an OS
          thread spinning for a slot that, when it finally opens, nobody owns.
          That thread would hand the lease to a dead coroutine — a permanent
          one-slot leak.  A per-waiter ``threading.Event`` is therefore passed
          into the interruptible acquire, and the coroutine sets it in its
          ``finally``.  The thread either sees it before allocating (no lease is
          created) or allocates and returns it to the coroutine, which releases
          it; a lease can never be orphaned.
        * **Cancelled while the worker runs.**  The lease is *detached*, not
          released: the slot returns from a done-callback when the thread
          actually finishes, so cancellation stays prompt without over-admitting
          the box.
        """
        # Acquiring blocks (it spins until a slot frees), so it must NOT run on the
        # event loop: with a bounded pool the slot only frees when a *worker*
        # finishes, and spinning here would keep the loop from ever resuming the
        # futures that free it -- a self-deadlock.
        cancel_event = threading.Event()
        lease_future = await self._start_acquire(cancel_event)
        try:
            lease: AdmissionLease = await asyncio.shield(lease_future)
        except BaseException:
            # Cancelled while waiting: tell the worker thread to stop waiting and
            # arrange for anything it still produces to be released.  This must
            # NOT await the thread: it may be sleeping or blocked, and a
            # cancellation path has to return *promptly* -- awaiting here is how a
            # "fix" for a lease leak becomes an unkillable task.
            cancel_event.set()
            self._release_orphaned_lease(lease_future)
            raise
        executor = self._ensure_executor()
        future = executor.submit(fn, *args, **kwargs)
        try:
            result = await asyncio.shield(asyncio.wrap_future(future))
        except asyncio.CancelledError:
            # The *caller* was cancelled; the worker thread is still running.
            # Detach the lease so the slot returns from a done-callback when the
            # work actually finishes — cancellation must not over-admit the box.
            lease.detach()

            def _release_when_done(_future: Any) -> None:
                lease.release()

            future.add_done_callback(_release_when_done)
            raise
        except BaseException:
            # The worker itself failed: the slot is ours to return immediately.
            lease.release()
            raise
        lease.release()
        return result

    async def _start_acquire(self, cancel_event: threading.Event) -> asyncio.Future[AdmissionLease]:
        """Start the interruptible acquire on a worker thread; return its future."""
        return asyncio.ensure_future(
            asyncio.to_thread(self._acquire_interruptible, cancel_event=cancel_event)
        )

    @staticmethod
    def _release_orphaned_lease(lease_future: asyncio.Future[AdmissionLease]) -> None:
        """Arrange for a lease the cancelled coroutine can no longer own to return.

        Two outcomes are correct and both leak-free: the thread sees the flag and
        raises (nothing to release), or it had already registered a lease and the
        done-callback releases it, because nobody else will ever hold it.

        Deliberately synchronous: it runs on a cancellation path, so it attaches a
        callback rather than awaiting the thread.  A thread that ignores the flag
        cannot delay the ``CancelledError``, and a failing future cannot mask it.
        """

        def _release(future: asyncio.Future[AdmissionLease]) -> None:
            if future.cancelled():
                return
            try:
                orphaned = future.result()
            except BaseException:
                return
            if isinstance(orphaned, AdmissionLease):
                orphaned.release()

        lease_future.add_done_callback(_release)

    def _ensure_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._executor_workers, thread_name_prefix="studio-lease"
                )
            return self._executor

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop the worker pool (idempotent)."""
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=wait)

    def __enter__(self) -> StudioResourceGovernor:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()
