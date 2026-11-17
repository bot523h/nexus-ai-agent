"""ATLAS spine governance — the canonical NEXUS spine audit checks.

This module adds, *around* the existing TITAN/Olympus spines (never replacing
them), three spine-governance requirements:

1. **Forensic quarantine retention** — a failed staged attempt (tampered bytes,
   ``ffprobe`` mismatch, stale fencing token, invariant failure) is preserved
   under ``<workspace_root>/.quarantine/<attempt_id>_<fencing_token>/`` with a
   deterministic ``quarantine_receipt.json`` recording ``attempt_id``,
   ``fencing_token``, ``failure_reason`` and the observed digests. The
   authoritative ``published_root`` is never touched.

2. **Atomic, interprocess-locked lineage ledger** — a single authoritative
   document ``<lineage_dir>/lineage_ledger.json`` holds both the
   ``AtlasAssetVersionRecord`` and ``AtlasArtifactPassportRecord`` sets, written
   with canonical RFC 8785 JCS bytes via temp-file + ``os.fsync`` + ``os.replace``
   under an exclusive ``fcntl.flock``. Each commit is also snapshotted to
   ``<lineage_dir>/generations/ledger_gen_<n>_<sha256>.json``. Two legacy *view*
   files are derived inside the same lock for compatibility and are healed from
   the authoritative document on any read, so a torn write can never leave the
   two record sets inconsistent. Fencing-token monotonicity (CAS) is re-read
   from disk inside the lock, so it holds across processes and instances.

3. **Fail-closed CommandBus authorization** — the governed entrypoints require
   an authorizer that permits the *exact* target. Omitting the authorizer fails
   closed (``AUTHORIZER_REQUIRED``) unless the caller explicitly opts into a
   clearly-labelled unauthenticated lab mode; the resulting
   :class:`AuthorityReceipt` marks ``authority_mode`` accordingly.

All filesystem writes go through the repository's existing
:class:`WorkspaceFilesystem` boundary, except the lineage ledger, which needs
raw ``os.replace`` + ``flock`` semantics and is lexically contained under the
workspace root. This module is a *local, advisory* governance layer: it never
mints an authoritative ``AssetVersion``/``ArtifactPassport`` row and never
claims to own project state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol, TypeVar, runtime_checkable

from nexus_ai_agent.creative.atlas.titan_staged_execution_spine import (
    SpineLedger,
    run_staged_spine,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

__all__ = [
    "MAX_QUARANTINE_FILES",
    "SpineAuthorizationError",
    "AtlasGovernanceRefusalError",
    "StaleFencingTokenError",
    "LineageConflictError",
    "QuarantineReceipt",
    "AuthorityReceipt",
    "CommandBusDecision",
    "CommandBusAuthorizer",
    "AtlasAssetVersionRecord",
    "AtlasArtifactPassportRecord",
    "LineageCommit",
    "AtlasLineageRepositoryProtocol",
    "AtlasLineageStore",
    "jcs_canonical_bytes",
    "quarantine_failed_attempt",
    "run_staged_spine_governed",
    "run_olympus_master_governed",
]

MAX_QUARANTINE_FILES: Final[int] = 256
_LINEAGE_DIR: Final[str] = "_lineage"
_AUTHORITATIVE_LEDGER: Final[str] = "lineage_ledger.json"
_ASSET_VIEW: Final[str] = "asset_versions.json"
_PASSPORT_VIEW: Final[str] = "artifact_passports.json"
_GENERATIONS_DIR: Final[str] = "generations"
_LOCK_NAME: Final[str] = ".lineage.lock"

_LAB_MODE: Final[str] = "LAB_UNAUTHENTICATED"
_AUTHORIZED_MODE: Final[str] = "AUTHORIZED"


class SpineAuthorizationError(PermissionError):
    """Raised when a dispatch is not authorized to run."""


class AtlasGovernanceRefusalError(SpineAuthorizationError):
    """Raised when a governed entrypoint refuses to run for a typed reason.

    ``code`` carries a stable machine-readable reason such as
    ``AUTHORIZER_REQUIRED`` or ``MISSING_COMMAND_BUS_AUTHORITY``.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class StaleFencingTokenError(RuntimeError):
    """Raised when a commit presents a fencing token below the highest committed."""


class LineageConflictError(RuntimeError):
    """Raised when an exact-token retry carries a *different* payload."""


# --- RFC 8785 (JCS) canonicalization ---------------------------------------------
# This is the subset JCS needs for the ledger's data model: ``null``/``true``/
# ``false``, integers, finite floats, UTF-8 strings, arrays and objects with
# lexicographically-sorted keys and no insignificant whitespace. The ledger only
# ever serialises identifiers and integers, so the rules here are exact for
# every value this module produces.


def _jcs_number(value: int | float) -> str:
    if isinstance(value, bool):  # pragma: no cover - guarded by caller
        raise TypeError("bool is not a JCS number")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError("JCS forbids non-finite numbers")
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _jcs(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _jcs_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0])
        body = ",".join(
            json.dumps(str(key), ensure_ascii=False) + ":" + _jcs(val) for key, val in items
        )
        return "{" + body + "}"
    raise TypeError(f"unsupported JCS value type: {type(value)!r}")


def jcs_canonical_bytes(value: Any) -> bytes:
    """Return the RFC 8785 JCS canonical UTF-8 bytes of ``value``."""
    return _jcs(value).encode("utf-8")


def _canonical(payload: Any) -> bytes:
    return jcs_canonical_bytes(payload)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _atomic_write(root: Path, dest: Path, raw: bytes) -> str:
    """Write ``raw`` to ``dest`` atomically (temp + fsync + ``os.replace``).

    ``dest`` must be lexically contained under ``root`` and may not be an
    existing symlink. Returns the SHA-256 of the written bytes.
    """
    root_resolved = root.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    parent_resolved = dest.parent.resolve()
    if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
        raise AtlasGovernanceRefusalError(
            "UNSAFE_LINEAGE_PATH", f"lineage path escapes the workspace: {dest}"
        )
    if dest.exists() and dest.is_symlink():
        raise AtlasGovernanceRefusalError(
            "UNSAFE_LINEAGE_PATH", f"lineage leaf is a symlink: {dest}"
        )
    tmp = dest.parent / f".staging-{secrets.token_hex(16)}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(dest))
        dir_fd = os.open(str(dest.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    return _digest(raw)


# --- 1. forensic quarantine retention --------------------------------------------


@dataclass(frozen=True)
class QuarantineReceipt:
    """A deterministic record of a preserved failed staged attempt."""

    attempt_id: str
    fencing_token: int
    failure_reason: str
    observed_digests: tuple[tuple[str, str], ...]
    published_root: str
    receipt_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "atlas.quarantine_receipt",
            "schema_version": 1,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "failure_reason": self.failure_reason,
            "observed_digests": [list(pair) for pair in self.observed_digests],
            "published_root": self.published_root,
            "receipt_sha256": self.receipt_sha256,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical(self.to_dict())


def quarantine_failed_attempt(
    workspace: WorkspaceFilesystem,
    *,
    attempt_id: str,
    fencing_token: int,
    failure_reason: str,
    failed_bundle: dict[str, bytes],
    published_root: str,
) -> QuarantineReceipt:
    """Preserve a failed staged attempt under ``.quarantine/<attempt>_<token>/``.

    ``failed_bundle`` maps a contained relative name to its bytes (the bytes
    that were staged and failed verification). A ``quarantine_receipt.json``
    records the attempt identity, the fencing token, the reason and the observed
    SHA-256 of every preserved file. The authoritative ``published_root`` is
    never written to.
    """
    if not attempt_id or "/" in attempt_id or ".." in attempt_id:
        raise SpineAuthorizationError("attempt_id must be a safe component")
    if not isinstance(fencing_token, int) or isinstance(fencing_token, bool) or fencing_token < 0:
        raise SpineAuthorizationError("fencing_token must be a non-negative int")
    if not failure_reason:
        raise SpineAuthorizationError("failure_reason must be non-empty")
    if len(failed_bundle) > MAX_QUARANTINE_FILES:
        raise SpineAuthorizationError(f"too many files to quarantine (>{MAX_QUARANTINE_FILES})")

    base = f".quarantine/{attempt_id}_{fencing_token}"
    digests: list[tuple[str, str]] = []
    for name, raw in sorted(failed_bundle.items()):
        if not name or "/" in name or "\\" in name or ".." in name or "\x00" in name:
            raise SpineAuthorizationError(f"unsafe bundle file name {name!r}")
        workspace.write_bytes(f"{base}/{name}", raw, create_parents=True)
        digests.append((name, _digest(raw)))

    provisional = QuarantineReceipt(
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        failure_reason=failure_reason,
        observed_digests=tuple(digests),
        published_root=published_root,
        receipt_sha256="0" * 64,
    )
    receipt_sha256 = _digest(provisional.canonical_bytes())
    receipt = QuarantineReceipt(
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        failure_reason=failure_reason,
        observed_digests=tuple(digests),
        published_root=published_root,
        receipt_sha256=receipt_sha256,
    )
    workspace.write_bytes(
        f"{base}/quarantine_receipt.json", receipt.canonical_bytes(), create_parents=True
    )
    return receipt


# --- 2. atomic, interprocess-locked lineage ledger --------------------------------


@dataclass(frozen=True)
class AtlasAssetVersionRecord:
    """A local, advisory projection of one published asset version."""

    asset_ref: str
    content_sha256: str
    size_bytes: int
    fencing_token: int
    attempt_id: str
    published_rel: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_ref": self.asset_ref,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "fencing_token": self.fencing_token,
            "attempt_id": self.attempt_id,
            "published_rel": self.published_rel,
        }


@dataclass(frozen=True)
class AtlasArtifactPassportRecord:
    """A local, advisory projection of one artifact's evidence passport."""

    asset_ref: str
    content_sha256: str
    fencing_token: int
    passport_digest: str
    published_rel: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_ref": self.asset_ref,
            "content_sha256": self.content_sha256,
            "fencing_token": self.fencing_token,
            "passport_digest": self.passport_digest,
            "published_rel": self.published_rel,
        }


@dataclass(frozen=True)
class LineageCommit:
    """The outcome of a lineage commit."""

    committed: bool  # False => idempotent no-op (exact retry)
    highest_token: int
    asset_version_count: int
    passport_count: int
    generation_id: int


@runtime_checkable
class AtlasLineageRepositoryProtocol(Protocol):
    """The port a canonical NEXUS repository implements to accept lineage.

    The file-backed :class:`AtlasLineageStore` satisfies this structurally; a
    production caller (the NEXUS queue / SQLModel store) can inject its own
    implementation into the governed entrypoints.
    """

    def commit(
        self,
        *,
        asset_version: AtlasAssetVersionRecord,
        passport: AtlasArtifactPassportRecord,
        fencing_token: int,
    ) -> LineageCommit: ...


def _empty_document() -> dict[str, Any]:
    return {
        "kind": "atlas.lineage_ledger",
        "schema_version": 1,
        "generation_id": 0,
        "highest_fencing_token": 0,
        "asset_versions": [],
        "artifact_passports": [],
    }


class AtlasLineageStore:
    """A durable, atomic, interprocess-locked lineage ledger.

    The authoritative state is a single JCS document ``lineage_ledger.json``.
    Every read and every commit happens inside an exclusive ``fcntl.flock`` on
    ``.lineage.lock`` and re-reads the document from disk, so fencing-token
    monotonicity holds across *processes* and across *instances* — an instance
    created before another committed cannot act on a stale in-memory token. Each
    commit is snapshotted to ``generations/ledger_gen_<n>_<sha256>.json`` and
    the two legacy view files are derived from the authoritative document; a
    torn view file is healed on the next read.
    """

    def __init__(self, root: WorkspaceFilesystem, *, lineage_dir: str = _LINEAGE_DIR) -> None:
        self.root = root
        self._root_path = Path(root.root)
        self._dir = self._root_path / lineage_dir
        self._authoritative = self._dir / _AUTHORITATIVE_LEDGER
        self._assets_view = self._dir / _ASSET_VIEW
        self._passports_view = self._dir / _PASSPORT_VIEW
        self._generations = self._dir / _GENERATIONS_DIR
        self._lock_path = self._dir / _LOCK_NAME

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl  # POSIX only; the lineage ledger is a POSIX file store

        if self._dir.is_symlink():
            raise AtlasGovernanceRefusalError(
                "UNSAFE_LINEAGE_PATH", "lineage directory is a symlink"
            )
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_document(self) -> dict[str, Any]:
        if not self._authoritative.exists():
            return _empty_document()
        raw = self._authoritative.read_bytes()
        loaded = json.loads(raw.decode("utf-8"))
        if not isinstance(loaded, dict):  # pragma: no cover - corruption guard
            raise LineageConflictError("lineage_ledger.json is not a JSON object")
        return loaded

    def _heal_views(self, document: dict[str, Any]) -> None:
        assets = _jcs(document["asset_versions"]).encode("utf-8")
        passports = _jcs(document["artifact_passports"]).encode("utf-8")
        _atomic_write(self._root_path, self._assets_view, assets)
        _atomic_write(self._root_path, self._passports_view, passports)

    def _views_are_torn(self, document: dict[str, Any]) -> bool:
        want_assets = _jcs(document["asset_versions"]).encode("utf-8")
        want_passports = _jcs(document["artifact_passports"]).encode("utf-8")
        return (
            not self._assets_view.exists()
            or self._assets_view.read_bytes() != want_assets
            or not self._passports_view.exists()
            or self._passports_view.read_bytes() != want_passports
        )

    def _load_locked(self) -> dict[str, Any]:
        document = self._read_document()
        if self._views_are_torn(document):
            self._heal_views(document)
        return document

    @property
    def highest_token(self) -> int:
        with self._locked():
            return int(self._load_locked()["highest_fencing_token"])

    @property
    def generation_id(self) -> int:
        with self._locked():
            return int(self._load_locked()["generation_id"])

    def records(self) -> dict[str, list[dict[str, Any]]]:
        """Return the authoritative record sets (healing any torn view)."""
        with self._locked():
            document = self._load_locked()
            return {
                "asset_versions": list(document["asset_versions"]),
                "artifact_passports": list(document["artifact_passports"]),
            }

    def commit(
        self,
        *,
        asset_version: AtlasAssetVersionRecord,
        passport: AtlasArtifactPassportRecord,
        fencing_token: int,
    ) -> LineageCommit:
        """Commit a lineage pair under CAS fencing-token monotonicity."""
        if not isinstance(fencing_token, int) or isinstance(fencing_token, bool):
            raise StaleFencingTokenError("fencing_token must be an int")
        if asset_version.fencing_token != fencing_token or passport.fencing_token != fencing_token:
            raise StaleFencingTokenError("record fencing_token must match the commit token")

        with self._locked():
            document = self._load_locked()
            highest = int(document["highest_fencing_token"])
            if fencing_token < highest:
                raise StaleFencingTokenError(
                    f"stale fencing token {fencing_token} < committed {highest}"
                )

            new_asset = asset_version.to_dict()
            new_passport = passport.to_dict()
            if fencing_token == highest:
                if new_asset in document["asset_versions"] and (
                    new_passport in document["artifact_passports"]
                ):
                    return LineageCommit(
                        committed=False,
                        highest_token=highest,
                        asset_version_count=len(document["asset_versions"]),
                        passport_count=len(document["artifact_passports"]),
                        generation_id=int(document["generation_id"]),
                    )
                if highest != 0:
                    raise LineageConflictError(
                        f"payload differs from the committed records at token {fencing_token}"
                    )

            document["asset_versions"].append(new_asset)
            document["artifact_passports"].append(new_passport)
            document["highest_fencing_token"] = fencing_token
            generation = int(document["generation_id"]) + 1
            document["generation_id"] = generation

            body = _jcs(document).encode("utf-8")
            gen = self._generations / f"ledger_gen_{generation}_{_digest(body)}.json"
            _atomic_write(self._root_path, gen, body)
            _atomic_write(self._root_path, self._authoritative, body)
            self._heal_views(document)
            return LineageCommit(
                committed=True,
                highest_token=fencing_token,
                asset_version_count=len(document["asset_versions"]),
                passport_count=len(document["artifact_passports"]),
                generation_id=generation,
            )


# --- 3. fail-closed CommandBus authorization -------------------------------------


@dataclass(frozen=True)
class AuthorityReceipt:
    """A structured record of the authority under which a dispatch ran."""

    authority_mode: str
    operation: str
    project_id: str
    revision_id: str
    attempt_id: str
    fencing_token: int

    @property
    def unauthenticated(self) -> bool:
        return self.authority_mode == _LAB_MODE

    def to_note(self) -> str:
        return f"authority_mode={self.authority_mode}"


@dataclass(frozen=True)
class CommandBusDecision:
    """A rich authorization decision returned by a :class:`CommandBusAuthorizer`.

    ``permits`` — when supplied — is consulted with the *exact* target identity
    ``(project_id, revision_id, operation, attempt_id, fencing_token)`` and must
    return ``True``; a blanket ``authorized=True`` is not sufficient.
    """

    authorized: bool
    authority_mode: str = _AUTHORIZED_MODE
    permits: Any = None  # Callable[[str, str, str, str, int], bool] | None


@runtime_checkable
class CommandBusAuthorizer(Protocol):
    """A port the studio supplies to authorize a dispatch before staging."""

    def authorize(
        self,
        *,
        operation: str,
        actor: str,
        project_id: str,
        payload: dict[str, Any],
    ) -> Any: ...


def _interpret_decision(decision: Any) -> tuple[bool, str, Any]:
    if isinstance(decision, CommandBusDecision):
        return decision.authorized, decision.authority_mode, decision.permits
    if isinstance(decision, bool):
        return decision, _AUTHORIZED_MODE, None
    if isinstance(decision, dict):
        return (
            bool(decision.get("authorized")),
            str(decision.get("authority_mode", _AUTHORIZED_MODE)),
            None,
        )
    return bool(decision), _AUTHORIZED_MODE, None


def _authorize(
    *,
    command_bus_authorizer: CommandBusAuthorizer | None,
    allow_unauthenticated_lab_mode: bool,
    actor: str,
    project_id: str,
    revision_id: str,
    operation: str,
    attempt_id: str,
    fencing_token: int,
    payload: dict[str, Any],
) -> AuthorityReceipt:
    """Resolve the authority for a dispatch, failing closed by default."""
    if command_bus_authorizer is None:
        if not allow_unauthenticated_lab_mode:
            raise AtlasGovernanceRefusalError(
                "AUTHORIZER_REQUIRED",
                "a command_bus_authorizer is required; pass "
                "allow_unauthenticated_lab_mode=True only for a clearly-labelled lab run",
            )
        return AuthorityReceipt(
            authority_mode=_LAB_MODE,
            operation=operation,
            project_id=project_id,
            revision_id=revision_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
        )

    decision = command_bus_authorizer.authorize(
        operation=operation, actor=actor, project_id=project_id, payload=payload
    )
    authorized, mode, permits = _interpret_decision(decision)
    if not authorized:
        raise SpineAuthorizationError(f"command bus denied {operation!r}")
    if permits is not None and not permits(
        project_id, revision_id, operation, attempt_id, fencing_token
    ):
        raise SpineAuthorizationError(
            f"authorization does not permit the exact target for {operation!r}"
        )
    return AuthorityReceipt(
        authority_mode=mode,
        operation=operation,
        project_id=project_id,
        revision_id=revision_id,
        attempt_id=attempt_id,
        fencing_token=fencing_token,
    )


_LedgerT = TypeVar("_LedgerT")


def _with_authority_note(ledger: _LedgerT, receipt: AuthorityReceipt) -> _LedgerT:
    note = receipt.to_note()
    existing = tuple(getattr(ledger, "notes", ()))
    if note in existing:
        return ledger
    return replace(ledger, notes=existing + (note,))  # type: ignore[type-var]


def _collect_bundle(workspace: WorkspaceFilesystem, out_dir: str) -> dict[str, bytes]:
    """Read every file under ``out_dir`` (bounded) as a forensic bundle."""
    bundle: dict[str, bytes] = {}
    for path in workspace.iter_files():
        rel = path.relative_to(workspace.root).as_posix()
        if rel == out_dir or rel.startswith(out_dir + "/"):
            name = rel[len(out_dir) + 1 :] if rel != out_dir else ""
            if name:
                bundle[name] = path.read_bytes()
            if len(bundle) >= MAX_QUARANTINE_FILES:
                break
    return bundle


def run_staged_spine_governed(
    film: Any,
    filesystem: WorkspaceFilesystem,
    *,
    command_bus_authorizer: CommandBusAuthorizer | None = None,
    allow_unauthenticated_lab_mode: bool = False,
    actor: str = "atlas",
    project_id: str = "atlas",
    revision_id: str = "rev-1",
    operation: str = "titan.spine.run",
    payload: dict[str, Any] | None = None,
    attempt_id: str = "attempt-1",
    fencing_token: int = 1,
    published_root: str = "",
    lineage: AtlasLineageRepositoryProtocol | None = None,
    **spine_kwargs: Any,
) -> SpineLedger:
    """Run the staged spine with the three governance checks around it.

    1. Authority is resolved *before* any staging: a missing authorizer fails
       closed with ``AUTHORIZER_REQUIRED`` unless
       ``allow_unauthenticated_lab_mode=True`` (which marks the receipt
       ``authority_mode=LAB_UNAUTHENTICATED``); a supplied authorizer must
       permit the exact target.
    2. On failure the staged bytes are preserved under ``.quarantine`` with a
       deterministic receipt (the authoritative namespace is untouched), and the
       original exception is re-raised.
    3. On success, when a ``lineage`` repository is supplied, the ledger is
       written to ``<out_dir>/ledger.json`` and a fenced
       ``AssetVersion``/``ArtifactPassport`` pair is committed.
    """
    receipt = _authorize(
        command_bus_authorizer=command_bus_authorizer,
        allow_unauthenticated_lab_mode=allow_unauthenticated_lab_mode,
        actor=actor,
        project_id=project_id,
        revision_id=revision_id,
        operation=operation,
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        payload=payload or {},
    )

    out_dir = str(spine_kwargs.get("out_dir", "titan_spine"))
    try:
        ledger = run_staged_spine(film, filesystem, **spine_kwargs)
    except Exception as exc:  # noqa: BLE001 - re-raised after quarantine
        quarantine_failed_attempt(
            filesystem,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            failure_reason=f"{type(exc).__name__}: {exc}",
            failed_bundle=_collect_bundle(filesystem, out_dir),
            published_root=published_root,
        )
        raise

    if lineage is not None:
        raw = ledger.canonical_bytes()
        rel = f"{out_dir}/ledger.json"
        filesystem.write_bytes(rel, raw, create_parents=True)
        digest = _digest(raw)
        lineage.commit(
            asset_version=AtlasAssetVersionRecord(
                asset_ref=f"atlas://{ledger.story_fingerprint[:16]}",
                content_sha256=digest,
                size_bytes=len(raw),
                fencing_token=fencing_token,
                attempt_id=attempt_id,
                published_rel=rel,
            ),
            passport=AtlasArtifactPassportRecord(
                asset_ref=f"atlas://{ledger.story_fingerprint[:16]}",
                content_sha256=digest,
                fencing_token=fencing_token,
                passport_digest=ledger.ledger_sha256,
                published_rel=rel,
            ),
            fencing_token=fencing_token,
        )
    return _with_authority_note(ledger, receipt)


def run_olympus_master_governed(
    bible: Any,
    filesystem: WorkspaceFilesystem,
    *,
    command_bus_authorizer: CommandBusAuthorizer | None = None,
    allow_unauthenticated_lab_mode: bool = False,
    actor: str = "atlas",
    project_id: str = "atlas",
    revision_id: str = "rev-1",
    operation: str = "olympus.master.render",
    payload: dict[str, Any] | None = None,
    attempt_id: str = "attempt-1",
    fencing_token: int = 1,
    published_root: str = "",
    lineage: AtlasLineageRepositoryProtocol | None = None,
    **master_kwargs: Any,
) -> Any:
    """Run the Olympus capstone with the same three governance checks.

    Identical contract to :func:`run_staged_spine_governed` but for
    :func:`nexus_ai_agent.creative.atlas.olympus_master.render_master`.
    """
    from nexus_ai_agent.creative.atlas import olympus_master as om

    receipt = _authorize(
        command_bus_authorizer=command_bus_authorizer,
        allow_unauthenticated_lab_mode=allow_unauthenticated_lab_mode,
        actor=actor,
        project_id=project_id,
        revision_id=revision_id,
        operation=operation,
        attempt_id=attempt_id,
        fencing_token=fencing_token,
        payload=payload or {},
    )

    out_dir = str(master_kwargs.get("out_dir", "olympus_master"))
    try:
        ledger = om.render_master(bible, filesystem, **master_kwargs)
    except Exception as exc:  # noqa: BLE001 - re-raised after quarantine
        quarantine_failed_attempt(
            filesystem,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            failure_reason=f"{type(exc).__name__}: {exc}",
            failed_bundle=_collect_bundle(filesystem, out_dir),
            published_root=published_root,
        )
        raise

    if lineage is not None:
        raw = ledger.canonical_bytes()
        rel = f"{out_dir}/master_ledger.json"
        filesystem.write_bytes(rel, raw, create_parents=True)
        digest = _digest(raw)
        lineage.commit(
            asset_version=AtlasAssetVersionRecord(
                asset_ref=f"olympus://{digest[:16]}",
                content_sha256=digest,
                size_bytes=len(raw),
                fencing_token=fencing_token,
                attempt_id=attempt_id,
                published_rel=rel,
            ),
            passport=AtlasArtifactPassportRecord(
                asset_ref=f"olympus://{digest[:16]}",
                content_sha256=digest,
                fencing_token=fencing_token,
                passport_digest=ledger.ledger_sha256,
                published_rel=rel,
            ),
            fencing_token=fencing_token,
        )
    return _with_authority_note(ledger, receipt)
