"""Cryptographic revision and asset-version pinning for Studio execution.

An approval binds a *revision*.  Between the moment a human approves and the
moment the canonical worker executes, two things can silently move:

1. the **revision state** — a concurrent turn can advance the project's
   revision head, so the approved revision becomes superseded history; and
2. the **input assets on disk** — an operator (or a racing job) can edit a
   source file in place, so "trim the intro" renders against different pixels.

This module makes both movements a *typed refusal* instead of a wrong render:

* :class:`PinnedRevisionEnvelope` is a frozen, content-addressed record of one
  revision: plan/parameter digests, parent lineage, the preview digest a human
  saw, and one :class:`AssetVersionPin` (content SHA-256 + byte size) per input
  asset.  ``revision_jcs_sha256`` is the RFC 8785 JCS SHA-256 over every field,
  so the envelope is self-verifying (``verify_envelope_integrity``).
* :class:`DurableRevisionPinningStore` persists envelopes in SQLite (WAL,
  ``busy_timeout=5000``, ``BEGIN IMMEDIATE`` for every write) together with a
  per-project revision head.  ``pin_revision`` enforces optimistic concurrency
  against that head and is **idempotent** for a byte-identical retry
  (``f(f(x)) = f(x)``) while a *different* payload under the same id is a typed
  conflict.
* ``verify_worker_execution_pins`` is the worker's gate: it re-verifies the
  envelope, the expected revision/plan digests, the current-head rule, and every
  on-disk asset (``os.lstat`` symlink refusal, ``is_relative_to`` containment,
  exact size, ``hmac.compare_digest`` SHA-256).

Failure is always typed and fail-closed: :class:`RevisionConcurrencyConflictError`,
:class:`RevisionIdempotencyConflictError`, :class:`RevisionDriftError`,
:class:`AssetPinViolationError`, all deriving from :class:`RevisionPinningError`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat as stat_module
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_digest,
)

__all__ = [
    "AssetPinViolationError",
    "AssetVersionPin",
    "DurableRevisionPinningStore",
    "PinnedRevisionEnvelope",
    "PinnedRevisionState",
    "RevisionConcurrencyConflictError",
    "RevisionDriftError",
    "RevisionIdempotencyConflictError",
    "RevisionNotFoundError",
    "RevisionPinningError",
    "sha256_file_hex",
    "utc_now",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1024 * 1024
_ENVELOPE_SCHEMA = "nexus.studio.pinned_revision.v1"


class RevisionPinningError(ValueError):
    """Typed, fail-closed refusal from the revision pinning layer."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class RevisionConcurrencyConflictError(RevisionPinningError):
    """The optimistic parent/head precondition did not hold."""


class RevisionIdempotencyConflictError(RevisionPinningError):
    """The same ``revision_id`` was re-pinned with a different payload."""


class RevisionDriftError(RevisionPinningError):
    """The envelope or its expected digests no longer match execution's claim."""


class RevisionNotFoundError(RevisionPinningError):
    """The pinned revision does not exist for the expected project/tenant."""


class AssetPinViolationError(RevisionPinningError):
    """An input asset's bytes, size, or path escaped its pin/authorized root."""


def utc_now() -> str:
    """One spelling of "now" for every durable timestamp in this module."""
    return datetime.now(timezone.utc).isoformat()


def _require_text(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RevisionPinningError("invalid_field", f"{field} must be a non-empty string")
    return value


#: The repository spells every digest ``sha256:<hex>``; pin *fields* are those
#: prefixed spellings.  A bare ``<hex>`` is accepted too, so a caller that read a
#: digest out of another system is not forced to re-prefix it — both normalise
#: to the prefixed spelling, so equality is never a formatting accident.
_HEX64_PREFIXED = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64_BARE = re.compile(r"^[0-9a-f]{64}$")


def _require_digest(field: str, value: object) -> str:
    """Normalise one digest field to ``sha256:<64 lowercase hex>`` (fail closed)."""
    if isinstance(value, str):
        if _HEX64_PREFIXED.match(value) is not None:
            return value
        if _HEX64_BARE.match(value) is not None:
            return f"sha256:{value}"
    raise RevisionPinningError(
        "invalid_digest",
        f"{field} must be a sha256:<64 lowercase hex> digest, got {value!r}",
    )


def _require_hex64(field: str, value: object) -> str:
    """Require a *bare* 64 lowercase hex digest (asset byte pins)."""
    if not isinstance(value, str) or _HEX64.match(value) is None:
        raise RevisionPinningError(
            "invalid_digest", f"{field} must be 64 lowercase hex characters, got {value!r}"
        )
    return value


def sha256_file_hex(path: Path) -> tuple[str, int]:
    """Stream ``path`` once; return ``(lowercase hex sha256, byte size)``."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def sha256_pinned_file(
    path: Path,
    *,
    expected: os.stat_result,
    authorized_root: Path,
) -> tuple[str, int]:
    """Hash the *exact inode* ``expected`` names, refusing any substitution.

    ``lstat`` followed by ``open`` is a classic TOCTOU gap: between the two calls
    the path can be replaced with a symlink, a FIFO, or a different file, and the
    digest is then computed over something the security check never saw.

    This closes it by taking one file descriptor and proving it is the same inode:

    * ``O_NOFOLLOW`` makes the *open* refuse a symlink, rather than trusting the
      earlier ``lstat`` to have caught it;
    * ``O_CLOEXEC`` keeps the descriptor out of any child process;
    * ``fstat(fd)`` must report a regular file whose ``(st_dev, st_ino)`` equal
      the ``(st_dev, st_ino)`` from ``lstat`` — if the path was swapped, the pair
      differs and the pin is refused;
    * the bytes are read *through that descriptor*, never by re-opening the path,
      and the count must match the pinned ``st_size``.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise AssetPinViolationError(
            "asset_open_failed", f"cannot open {path} without following links: {exc}"
        ) from exc
    try:
        fd_stat = os.fstat(fd)
        if not stat_module.S_ISREG(fd_stat.st_mode):
            raise AssetPinViolationError(
                "not_a_file", f"{path} is not a regular file (mode {fd_stat.st_mode:#o})"
            )
        if (fd_stat.st_dev, fd_stat.st_ino) != (expected.st_dev, expected.st_ino):
            raise AssetPinViolationError(
                "inode_substituted",
                f"{path} changed between inspection and open "
                f"({expected.st_dev}:{expected.st_ino} -> "
                f"{fd_stat.st_dev}:{fd_stat.st_ino})",
            )
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1  # ownership transferred to the file object
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
        if size != fd_stat.st_size:
            raise AssetPinViolationError(
                "size_mismatch",
                f"{path} grew or shrank while it was being read ({fd_stat.st_size} -> {size})",
            )
        return digest.hexdigest(), size
    finally:
        if fd != -1:
            os.close(fd)


@dataclass(frozen=True)
class AssetVersionPin:
    """One input asset bound to its exact bytes.

    ``content_sha256`` is bare lowercase hex (the on-disk spelling); the
    repository's ``sha256:`` prefixed spelling belongs to ledger digests, not to
    byte pinning, and mixing the two would make equality against a live digest
    a string-format accident.
    """

    asset_id: str
    relative_path: str
    content_sha256: str
    byte_size: int
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        _require_text("asset_id", self.asset_id)
        relative = _require_text("relative_path", self.relative_path)
        if Path(relative).is_absolute():
            raise RevisionPinningError(
                "invalid_relative_path", f"relative_path must be relative, got {relative!r}"
            )
        if ".." in Path(relative).parts:
            raise RevisionPinningError(
                "invalid_relative_path", f"relative_path must not traverse, got {relative!r}"
            )
        _require_hex64("content_sha256", self.content_sha256)
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise RevisionPinningError("invalid_byte_size", "byte_size must be an integer")
        if self.byte_size < 0:
            raise RevisionPinningError(
                "invalid_byte_size", f"byte_size must be >= 0, got {self.byte_size}"
            )
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise RevisionPinningError(
                "invalid_media_type", "media_type must be a non-empty string"
            )

    def to_canonical_dict(self) -> dict[str, Any]:
        """The exact payload the envelope digest covers."""
        return {
            "asset_id": self.asset_id,
            "relative_path": self.relative_path,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "media_type": self.media_type,
        }


@dataclass(frozen=True)
class PinnedRevisionEnvelope:
    """An immutable, content-addressed revision ready for worker execution."""

    revision_id: str
    project_id: str
    tenant_id: str
    session_id: str
    turn_id: str
    revision_number: int
    parent_revision_id: str | None
    plan_id: str
    plan_jcs_sha256: str
    parameters_jcs_sha256: str
    preview_digest: str
    input_asset_pins: tuple[AssetVersionPin, ...]
    revision_jcs_sha256: str
    created_at_utc: str

    def __post_init__(self) -> None:
        for field in (
            "revision_id",
            "project_id",
            "tenant_id",
            "session_id",
            "turn_id",
            "plan_id",
            "created_at_utc",
        ):
            _require_text(field, getattr(self, field))
        if self.parent_revision_id is not None:
            _require_text("parent_revision_id", self.parent_revision_id)
        for field in ("plan_jcs_sha256", "parameters_jcs_sha256", "preview_digest"):
            object.__setattr__(self, field, _require_digest(field, getattr(self, field)))
        if isinstance(self.revision_number, bool) or not isinstance(self.revision_number, int):
            raise RevisionPinningError("invalid_revision_number", "revision_number must be an int")
        if self.revision_number < 1:
            raise RevisionPinningError(
                "invalid_revision_number",
                f"revision_number must be >= 1, got {self.revision_number}",
            )
        if self.revision_number == 1 and self.parent_revision_id is not None:
            raise RevisionPinningError(
                "invalid_lineage", "a genesis revision (revision_number=1) has no parent"
            )
        if self.revision_number > 1 and self.parent_revision_id is None:
            raise RevisionPinningError(
                "invalid_lineage", "a non-genesis revision must name its parent_revision_id"
            )
        pins: Sequence[AssetVersionPin] = self.input_asset_pins
        if not isinstance(pins, Iterable):
            raise RevisionPinningError("invalid_asset_pins", "input_asset_pins must be iterable")
        materialized = tuple(pins)
        for pin in materialized:
            if not isinstance(pin, AssetVersionPin):
                raise RevisionPinningError(
                    "invalid_asset_pins",
                    f"input_asset_pins must contain AssetVersionPin, got {type(pin).__name__}",
                )
        ordered = tuple(sorted(materialized, key=lambda pin: pin.asset_id))
        asset_ids = [pin.asset_id for pin in ordered]
        if len(set(asset_ids)) != len(asset_ids):
            raise RevisionPinningError("invalid_asset_pins", "duplicate asset_id in pins")
        object.__setattr__(self, "input_asset_pins", ordered)
        if self.revision_jcs_sha256:
            object.__setattr__(
                self,
                "revision_jcs_sha256",
                _require_digest("revision_jcs_sha256", self.revision_jcs_sha256),
            )
        else:
            object.__setattr__(self, "revision_jcs_sha256", self.compute_digest())

    # -- canonical form ---------------------------------------------------- #

    def canonical_payload(self, *, include_digest: bool = False) -> dict[str, Any]:
        """The canonical dict the revision digest is (or is not) computed over."""
        payload: dict[str, Any] = {
            "schema": _ENVELOPE_SCHEMA,
            "revision_id": self.revision_id,
            "project_id": self.project_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "revision_number": self.revision_number,
            "parent_revision_id": self.parent_revision_id,
            "plan_id": self.plan_id,
            "plan_jcs_sha256": self.plan_jcs_sha256,
            "parameters_jcs_sha256": self.parameters_jcs_sha256,
            "preview_digest": self.preview_digest,
            "input_asset_pins": [pin.to_canonical_dict() for pin in self.input_asset_pins],
            "created_at_utc": self.created_at_utc,
        }
        if include_digest:
            payload["revision_jcs_sha256"] = self.revision_jcs_sha256
        return payload

    def to_canonical_dict(self) -> dict[str, Any]:
        """The complete stored form (payload + digest), for persistence."""
        return self.canonical_payload(include_digest=True)

    def compute_digest(self) -> str:
        """RFC 8785 JCS SHA-256 over every field except the digest itself."""
        return canonical_digest(
            self.canonical_payload(include_digest=False),
            CanonicalizationScheme.JCS_RFC8785_V2,
        )

    def verify_envelope_integrity(self) -> bool:
        """True when ``revision_jcs_sha256`` is exactly the digest of the fields."""
        return hmac.compare_digest(self.compute_digest(), self.revision_jcs_sha256)


class PinnedRevisionState(str):
    """The durable lifecycle spelling of a pinned revision (closed set)."""

    LIVE = "LIVE"
    SUPERSEDED = "SUPERSEDED"

    def __new__(cls, value: str) -> PinnedRevisionState:
        if value not in (cls.LIVE, cls.SUPERSEDED):
            raise RevisionPinningError("invalid_state", f"unknown pinned revision state {value!r}")
        return super().__new__(cls, value)


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS nexus_studio_pinned_revisions (
        revision_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        revision_number INTEGER NOT NULL,
        parent_revision_id TEXT,
        plan_id TEXT NOT NULL,
        plan_jcs_sha256 TEXT NOT NULL,
        parameters_jcs_sha256 TEXT NOT NULL,
        preview_digest TEXT NOT NULL,
        input_asset_pins_json TEXT NOT NULL,
        revision_jcs_sha256 TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'LIVE',
        created_at_utc TEXT NOT NULL,
        UNIQUE (project_id, revision_number),
        -- The composite key child rows reference, so a *real* SQLite foreign
        -- key can bind them (PRAGMA foreign_keys = ON is set below).
        UNIQUE (project_id, revision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_studio_project_revision_heads (
        project_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        head_revision_id TEXT NOT NULL,
        head_revision_number INTEGER NOT NULL,
        updated_at_utc TEXT NOT NULL
    )
    """,
    # The head may only ever name a revision that exists for its project, and a
    # pinned revision may not be deleted out from under a live head.
    """
    CREATE TRIGGER IF NOT EXISTS nexus_studio_head_must_exist
    BEFORE INSERT ON nexus_studio_project_revision_heads
    WHEN NOT EXISTS (
        SELECT 1 FROM nexus_studio_pinned_revisions
        WHERE project_id = NEW.project_id AND revision_id = NEW.head_revision_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'head_reference_missing');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS nexus_studio_revision_pin_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        revision_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        event TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        occurred_at_utc TEXT NOT NULL,
        -- A real relational reference, enforced by SQLite because the store sets
        -- PRAGMA foreign_keys = ON: an audit row can never outlive -- or precede
        -- -- the revision it describes, and a revision that still has audit
        -- history cannot be deleted at all (ON DELETE RESTRICT).
        FOREIGN KEY (project_id, revision_id)
            REFERENCES nexus_studio_pinned_revisions (project_id, revision_id)
            ON DELETE RESTRICT
    )
    """,
)


class DurableRevisionPinningStore:
    """SQLite WAL store for pinned revisions, asset pins, and project heads.

    Every write runs inside ``BEGIN IMMEDIATE`` (cross-process write lock), and
    the store serializes in-process callers with an ``RLock`` so a nested
    transaction can never re-enter ``BEGIN IMMEDIATE`` on the same connection.
    """

    def __init__(self, path: str | Path, *, now: Any = utc_now) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lock = threading.RLock()
        self._depth = 0
        self._closed = False
        self._conn = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._transaction():
            for statement in _SCHEMA:
                self._conn.execute(statement)
        try:
            os.chmod(self._path, 0o600)
        except OSError:  # pragma: no cover - filesystems without POSIX modes
            pass

    # -- lifecycle --------------------------------------------------------- #

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> DurableRevisionPinningStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth > 0:
                yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self._conn.execute("COMMIT")

    @contextmanager
    def _read_snapshot(self) -> Iterator[None]:
        """One consistent read view (``BEGIN``) for a multi-statement read."""
        with self._lock:
            if self._depth > 0:
                yield
                return
            self._conn.execute("BEGIN")
            try:
                yield
            finally:
                self._conn.execute("COMMIT")

    # -- asset pinning ----------------------------------------------------- #

    def pin_asset_from_disk(
        self,
        *,
        asset_id: str,
        file_path: Path,
        authorized_asset_root: Path,
        media_type: str = "application/octet-stream",
    ) -> AssetVersionPin:
        """Pin one on-disk asset, refusing symlinks and root escapes."""
        _require_text("asset_id", asset_id)
        root = authorized_asset_root.resolve(strict=True)
        candidate = Path(file_path)
        try:
            stat_result = os.lstat(candidate)
        except OSError as exc:
            raise AssetPinViolationError(
                "asset_missing", f"{candidate} is not accessible: {exc}"
            ) from exc
        if stat_module.S_ISLNK(stat_result.st_mode):
            raise AssetPinViolationError(
                "symlink_rejected", f"{candidate} is a symlink; pins are for real files"
            )
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise AssetPinViolationError("not_a_file", f"{resolved} is not a regular file")
        if not resolved.is_relative_to(root):
            raise AssetPinViolationError(
                "path_escape", f"{resolved} is outside the authorized root {root}"
            )
        # Hash the inode ``lstat`` inspected, not whatever the path points at
        # when the second lookup happens.
        content_sha256, byte_size = sha256_pinned_file(
            resolved, expected=stat_result, authorized_root=root
        )
        relative = resolved.relative_to(root).as_posix()
        return AssetVersionPin(
            asset_id=asset_id,
            relative_path=relative,
            content_sha256=content_sha256,
            byte_size=byte_size,
            media_type=media_type,
        )

    # -- revision pinning -------------------------------------------------- #

    def pin_revision(
        self,
        *,
        revision_id: str,
        project_id: str,
        tenant_id: str,
        session_id: str,
        turn_id: str,
        revision_number: int,
        parent_revision_id: str | None,
        plan_id: str,
        plan_jcs_sha256: str,
        parameters_jcs_sha256: str,
        preview_digest: str,
        input_asset_pins: Sequence[AssetVersionPin] = (),
        created_at_utc: str = "",
    ) -> PinnedRevisionEnvelope:
        """Persist one immutable revision and advance the project head atomically.

        * genesis (``revision_number == 1``) requires ``parent_revision_id=None``
          and a project with no head yet;
        * any later revision requires ``parent_revision_id`` == the current head;
        * re-pinning the exact same id+payload returns the stored envelope
          unchanged (``f(f(x)) = f(x)``);
        * re-pinning the same id with a different payload is a typed conflict.
        """
        ordered_pins = tuple(sorted(input_asset_pins, key=lambda pin: pin.asset_id))
        with self._transaction():
            existing = self._fetch_envelope(revision_id)
            if existing is not None:
                # Idempotency is decided on the *stored* record: a retry that
                # repeats the same revision state (same plan/parameters/preview
                # and the same asset bytes) is the same pin, so it returns the
                # stored envelope byte for byte.  ``created_at_utc`` is therefore
                # taken from the stored row, never from this call's clock.
                retry_digest = self._retry_digest(
                    revision_id=revision_id,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    revision_number=revision_number,
                    parent_revision_id=parent_revision_id,
                    plan_id=plan_id,
                    plan_jcs_sha256=plan_jcs_sha256,
                    parameters_jcs_sha256=parameters_jcs_sha256,
                    preview_digest=preview_digest,
                    input_asset_pins=ordered_pins,
                    created_at_utc=existing.created_at_utc,
                )
                if hmac.compare_digest(existing.revision_jcs_sha256, retry_digest):
                    return existing
                raise RevisionIdempotencyConflictError(
                    "idempotency_conflict",
                    f"revision {revision_id!r} is already pinned with "
                    f"{existing.revision_jcs_sha256}, not {retry_digest}",
                )
            envelope = PinnedRevisionEnvelope(
                revision_id=revision_id,
                project_id=project_id,
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=turn_id,
                revision_number=revision_number,
                parent_revision_id=parent_revision_id,
                plan_id=plan_id,
                plan_jcs_sha256=plan_jcs_sha256,
                parameters_jcs_sha256=parameters_jcs_sha256,
                preview_digest=preview_digest,
                input_asset_pins=ordered_pins,
                revision_jcs_sha256="",
                created_at_utc=created_at_utc or self._now(),
            )
            head = self._fetch_head(envelope.project_id)
            head_revision_id = None if head is None else str(head[0])
            if head is not None and str(head[2]) != envelope.tenant_id:
                raise RevisionConcurrencyConflictError(
                    "cross_tenant_head",
                    f"project {envelope.project_id!r} head belongs to tenant {head[2]!r}",
                )
            if envelope.revision_number == 1:
                if head is not None:
                    raise RevisionConcurrencyConflictError(
                        "genesis_on_existing_project",
                        f"project {envelope.project_id!r} already has head {head_revision_id!r}",
                    )
            else:
                if head is None:
                    raise RevisionConcurrencyConflictError(
                        "missing_head",
                        f"revision {envelope.revision_number} needs an existing head",
                    )
                if not hmac.compare_digest(str(head_revision_id), str(envelope.parent_revision_id)):
                    raise RevisionConcurrencyConflictError(
                        "stale_parent",
                        f"parent {envelope.parent_revision_id!r} != head {head_revision_id!r} "
                        f"(a concurrent turn advanced the project)",
                    )
                if int(head[1]) != envelope.revision_number - 1:
                    raise RevisionConcurrencyConflictError(
                        "revision_number_gap",
                        f"revision_number {envelope.revision_number} does not follow "
                        f"head {int(head[1])}",
                    )
                self._conn.execute(
                    "UPDATE nexus_studio_pinned_revisions SET status = 'SUPERSEDED' "
                    "WHERE revision_id = ?",
                    (head_revision_id,),
                )
            # ``UNIQUE(project_id, revision_number)`` is the schema-level guard; the
            # parent/head preconditions above already make a clash unreachable for
            # well-formed chains, so the constraint is enforced by the table itself
            # (a violated INSERT raises ``sqlite3.IntegrityError`` and rolls back).
            self._conn.execute(
                """
                INSERT INTO nexus_studio_pinned_revisions (
                    revision_id, project_id, tenant_id, session_id, turn_id,
                    revision_number, parent_revision_id, plan_id, plan_jcs_sha256,
                    parameters_jcs_sha256, preview_digest, input_asset_pins_json,
                    revision_jcs_sha256, status, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'LIVE', ?)
                """,
                (
                    envelope.revision_id,
                    envelope.project_id,
                    envelope.tenant_id,
                    envelope.session_id,
                    envelope.turn_id,
                    envelope.revision_number,
                    envelope.parent_revision_id,
                    envelope.plan_id,
                    envelope.plan_jcs_sha256,
                    envelope.parameters_jcs_sha256,
                    envelope.preview_digest,
                    json.dumps(
                        [pin.to_canonical_dict() for pin in envelope.input_asset_pins],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    envelope.revision_jcs_sha256,
                    envelope.created_at_utc,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO nexus_studio_project_revision_heads (
                    project_id, tenant_id, head_revision_id, head_revision_number, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    head_revision_id = excluded.head_revision_id,
                    head_revision_number = excluded.head_revision_number,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    envelope.project_id,
                    envelope.tenant_id,
                    envelope.revision_id,
                    envelope.revision_number,
                    self._now(),
                ),
            )
            self._record_audit_locked(
                revision_id=envelope.revision_id,
                project_id=envelope.project_id,
                event="pinned",
                detail=envelope.revision_jcs_sha256,
            )
        return envelope

    @staticmethod
    def _retry_digest(
        *,
        revision_id: str,
        project_id: str,
        tenant_id: str,
        session_id: str,
        turn_id: str,
        revision_number: int,
        parent_revision_id: str | None,
        plan_id: str,
        plan_jcs_sha256: str,
        parameters_jcs_sha256: str,
        preview_digest: str,
        input_asset_pins: tuple[AssetVersionPin, ...],
        created_at_utc: str,
    ) -> str:
        """The digest a retry *would* produce, built without persisting anything."""
        probe = PinnedRevisionEnvelope(
            revision_id=revision_id,
            project_id=project_id,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            revision_number=revision_number,
            parent_revision_id=parent_revision_id,
            plan_id=plan_id,
            plan_jcs_sha256=plan_jcs_sha256,
            parameters_jcs_sha256=parameters_jcs_sha256,
            preview_digest=preview_digest,
            input_asset_pins=input_asset_pins,
            revision_jcs_sha256="",
            created_at_utc=created_at_utc,
        )
        return probe.revision_jcs_sha256

    # -- worker verification ----------------------------------------------- #

    def verify_worker_execution_pins(
        self,
        *,
        revision_id: str,
        expected_revision_jcs_sha256: str,
        expected_plan_jcs_sha256: str,
        authorized_asset_root: Path,
        require_current_head: bool = True,
        expected_project_id: str = "",
        expected_tenant_id: str = "",
    ) -> PinnedRevisionEnvelope:
        """Worker gate: prove the approved revision and every input asset still match."""
        _require_text("revision_id", revision_id)
        expected_revision_jcs_sha256 = _require_digest(
            "expected_revision_jcs_sha256", expected_revision_jcs_sha256
        )
        expected_plan_jcs_sha256 = _require_digest(
            "expected_plan_jcs_sha256", expected_plan_jcs_sha256
        )
        with self._read_snapshot():
            envelope = self._fetch_envelope(revision_id)
            if envelope is None:
                raise RevisionNotFoundError("unknown_revision", f"{revision_id!r} is not pinned")
            if expected_project_id and envelope.project_id != expected_project_id:
                raise RevisionDriftError(
                    "project_mismatch",
                    f"revision belongs to {envelope.project_id!r}, not {expected_project_id!r}",
                )
            if expected_tenant_id and envelope.tenant_id != expected_tenant_id:
                raise RevisionDriftError(
                    "tenant_mismatch",
                    f"revision belongs to {envelope.tenant_id!r}, not {expected_tenant_id!r}",
                )
            if not envelope.verify_envelope_integrity():
                raise RevisionDriftError(
                    "envelope_tampered",
                    f"revision {revision_id!r} content no longer hashes to its digest",
                )
            if not hmac.compare_digest(envelope.revision_jcs_sha256, expected_revision_jcs_sha256):
                raise RevisionDriftError(
                    "revision_drift",
                    f"expected revision digest {expected_revision_jcs_sha256}, "
                    f"stored {envelope.revision_jcs_sha256}",
                )
            if not hmac.compare_digest(envelope.plan_jcs_sha256, expected_plan_jcs_sha256):
                raise RevisionDriftError(
                    "plan_drift",
                    f"expected plan digest {expected_plan_jcs_sha256}, "
                    f"stored {envelope.plan_jcs_sha256}",
                )
            if require_current_head:
                head = self._fetch_head(envelope.project_id)
                if head is None or str(head[0]) != envelope.revision_id:
                    raise RevisionDriftError(
                        "not_current_head",
                        f"revision {envelope.revision_id!r} is superseded by "
                        f"{None if head is None else head[0]!r}",
                    )
            root = authorized_asset_root.resolve(strict=True)
            for pin in envelope.input_asset_pins:
                self._verify_asset_pin(pin, root)
        return envelope

    def _verify_asset_pin(self, pin: AssetVersionPin, root: Path) -> None:
        candidate = root / pin.relative_path
        try:
            stat_result = os.lstat(candidate)
        except OSError as exc:
            raise AssetPinViolationError(
                "asset_missing", f"pinned asset {pin.asset_id!r} is gone: {exc}"
            ) from exc
        if stat_module.S_ISLNK(stat_result.st_mode):
            raise AssetPinViolationError(
                "symlink_rejected", f"pinned asset {pin.asset_id!r} is now a symlink"
            )
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise AssetPinViolationError(
                "path_escape", f"pinned asset {pin.asset_id!r} resolves outside {root}"
            )
        if not resolved.is_file():
            raise AssetPinViolationError(
                "not_a_file", f"pinned asset {pin.asset_id!r} is not a regular file"
            )
        if stat_result.st_size != pin.byte_size:
            raise AssetPinViolationError(
                "size_mismatch",
                f"pinned asset {pin.asset_id!r} is {stat_result.st_size} bytes, "
                f"pinned {pin.byte_size}",
            )
        # TOCTOU-safe: the digest is computed through a descriptor proven to be
        # the very inode ``lstat`` just inspected.
        live_sha256, _ = sha256_pinned_file(resolved, expected=stat_result, authorized_root=root)
        if not hmac.compare_digest(live_sha256, pin.content_sha256):
            raise AssetPinViolationError(
                "content_drift",
                f"pinned asset {pin.asset_id!r} bytes changed since approval",
            )

    # -- introspection ----------------------------------------------------- #

    def get_envelope(self, revision_id: str) -> PinnedRevisionEnvelope | None:
        """Read one envelope (``None`` when unknown)."""
        with self._read_snapshot():
            return self._fetch_envelope(revision_id)

    def head_of(self, project_id: str) -> str | None:
        """The current head revision id of one project (``None`` when unpinned)."""
        with self._read_snapshot():
            head = self._fetch_head(project_id)
            return None if head is None else str(head[0])

    def list_revisions(self, project_id: str) -> tuple[PinnedRevisionEnvelope, ...]:
        """Every revision of one project, oldest first."""
        with self._read_snapshot():
            rows = self._conn.execute(
                "SELECT * FROM nexus_studio_pinned_revisions WHERE project_id = ? "
                "ORDER BY revision_number ASC",
                (project_id,),
            ).fetchall()
        return tuple(self._row_to_envelope(row) for row in rows)

    def revision_status(self, revision_id: str) -> str | None:
        """The durable status spelling of one revision."""
        with self._read_snapshot():
            row = self._conn.execute(
                "SELECT status FROM nexus_studio_pinned_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
        return None if row is None else str(row[0])

    def audit_events(self) -> tuple[dict[str, Any], ...]:
        """The append-only pin audit (evidence, never authority)."""
        with self._read_snapshot():
            rows = self._conn.execute(
                "SELECT revision_id, project_id, event, detail, occurred_at_utc "
                "FROM nexus_studio_revision_pin_audit ORDER BY audit_id ASC"
            ).fetchall()
        return tuple(
            {
                "revision_id": row[0],
                "project_id": row[1],
                "event": row[2],
                "detail": row[3],
                "occurred_at_utc": row[4],
            }
            for row in rows
        )

    # -- internals --------------------------------------------------------- #

    def _record_audit_locked(
        self, *, revision_id: str, project_id: str, event: str, detail: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO nexus_studio_revision_pin_audit "
            "(revision_id, project_id, event, detail, occurred_at_utc) VALUES (?, ?, ?, ?, ?)",
            (revision_id, project_id, event, detail, self._now()),
        )

    def _fetch_envelope(self, revision_id: str) -> PinnedRevisionEnvelope | None:
        row = self._conn.execute(
            "SELECT * FROM nexus_studio_pinned_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        return None if row is None else self._row_to_envelope(row)

    def _fetch_head(self, project_id: str) -> tuple[Any, ...] | None:
        row: tuple[Any, ...] | None = self._conn.execute(
            "SELECT head_revision_id, head_revision_number, tenant_id "
            "FROM nexus_studio_project_revision_heads WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return row

    @staticmethod
    def _row_to_envelope(row: tuple[Any, ...]) -> PinnedRevisionEnvelope:
        # Column order mirrors the CREATE TABLE statement above.
        raw_pins = json.loads(str(row[11]))
        pins = tuple(
            AssetVersionPin(
                asset_id=str(item["asset_id"]),
                relative_path=str(item["relative_path"]),
                content_sha256=str(item["content_sha256"]),
                byte_size=int(item["byte_size"]),
                media_type=str(item["media_type"]),
            )
            for item in raw_pins
        )
        return PinnedRevisionEnvelope(
            revision_id=str(row[0]),
            project_id=str(row[1]),
            tenant_id=str(row[2]),
            session_id=str(row[3]),
            turn_id=str(row[4]),
            revision_number=int(row[5]),
            parent_revision_id=None if row[6] is None else str(row[6]),
            plan_id=str(row[7]),
            plan_jcs_sha256=str(row[8]),
            parameters_jcs_sha256=str(row[9]),
            preview_digest=str(row[10]),
            input_asset_pins=pins,
            revision_jcs_sha256=str(row[12]),
            created_at_utc=str(row[14]),
        )
