"""ATLAS spine governance — the three canonical NEXUS spine audit checks.

This module adds, *around* the existing TITAN/Olympus spines (never replacing
them), the three canonical spine-governance requirements:

1. **Forensic quarantine retention** — a failed staged attempt (tampered bytes,
   ``ffprobe`` mismatch, stale fencing token, invariant failure) is preserved
   under ``<workspace_root>/.quarantine/<attempt_id>_<fencing_token>/`` with a
   deterministic ``quarantine_receipt.json`` recording ``attempt_id``,
   ``fencing_token``, ``failure_reason`` and the observed digests. The
   authoritative ``published_root`` is never touched.
2. **Durable lineage store** — verified fenced publication appends deterministic
   ``AtlasAssetVersionRecord`` and ``AtlasArtifactPassportRecord`` entries to a
   file-backed ledger (``<published_root>/_lineage/asset_versions.json`` and
   ``.../artifact_passports.json``) enforcing CAS fencing-token monotonicity
   (a stale ``fencing_token`` below the highest committed token is rejected;
   an exact retry is idempotent).
3. **Injectable CommandBus authorization hook** — the governed entrypoints take
   an optional ``command_bus_authorizer`` port that must authorize before any
   staging begins, failing closed when denied.

Everything is bounded and deterministic; all filesystem writes go through the
repository's existing :class:`WorkspaceFilesystem` boundary. This module is a
*local, advisory* governance layer: it never mints an authoritative
``AssetVersion``/``ArtifactPassport`` row and never claims to own project state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from nexus_ai_agent.creative.atlas.titan_staged_execution_spine import (
    SpineLedger,
    run_staged_spine,
)
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

__all__ = [
    "MAX_QUARANTINE_FILES",
    "SpineAuthorizationError",
    "StaleFencingTokenError",
    "LineageConflictError",
    "QuarantineReceipt",
    "AtlasAssetVersionRecord",
    "AtlasArtifactPassportRecord",
    "LineageCommit",
    "AtlasLineageStore",
    "CommandBusAuthorizer",
    "quarantine_failed_attempt",
    "run_staged_spine_governed",
    "run_olympus_master_governed",
]

MAX_QUARANTINE_FILES: Final[int] = 256
_LINEAGE_DIR: Final[str] = "_lineage"
_ASSET_LEDGER: Final[str] = f"{_LINEAGE_DIR}/asset_versions.json"
_PASSPORT_LEDGER: Final[str] = f"{_LINEAGE_DIR}/artifact_passports.json"


class SpineAuthorizationError(PermissionError):
    """Raised when the injected CommandBus authorizer denies a dispatch."""


class StaleFencingTokenError(RuntimeError):
    """Raised when a commit presents a fencing token below the highest committed."""


class LineageConflictError(RuntimeError):
    """Raised when an exact-token retry carries a *different* payload."""


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


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


# --- 2. durable lineage store with CAS fencing monotonicity -----------------------


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


@dataclass
class AtlasLineageStore:
    """A durable, file-backed AssetVersion/ArtifactPassport lineage ledger.

    Fencing-token monotonicity is enforced (CAS): a commit whose token is below
    the highest committed token is rejected; an exact retry at the current
    highest token is idempotent; a *different* payload at the same token is a
    deterministic conflict.
    """

    root: WorkspaceFilesystem
    _highest_token: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._highest_token = self._read_highest()

    def _read_json_list(self, rel: str) -> list[dict[str, Any]]:
        if not self.root.exists(rel):
            return []
        raw = self.root.read_bytes(rel, max_bytes=1 << 22)
        loaded = json.loads(raw.decode("utf-8"))
        if not isinstance(loaded, list):  # pragma: no cover - corruption guard
            raise LineageConflictError(f"{rel} is not a JSON list")
        return loaded

    def _read_highest(self) -> int:
        highest = 0
        for rec in self._read_json_list(_ASSET_LEDGER) + self._read_json_list(_PASSPORT_LEDGER):
            token = rec.get("fencing_token", 0)
            if isinstance(token, int) and token > highest:
                highest = token
        return highest

    @property
    def highest_token(self) -> int:
        return self._highest_token

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
        if fencing_token < self._highest_token:
            raise StaleFencingTokenError(
                f"stale fencing token {fencing_token} < committed {self._highest_token}"
            )

        assets = self._read_json_list(_ASSET_LEDGER)
        passports = self._read_json_list(_PASSPORT_LEDGER)
        new_asset = asset_version.to_dict()
        new_passport = passport.to_dict()

        if fencing_token == self._highest_token:
            if new_asset in assets and new_passport in passports:
                return LineageCommit(
                    committed=False,
                    highest_token=self._highest_token,
                    asset_version_count=len(assets),
                    passport_count=len(passports),
                )
            if self._highest_token != 0:
                raise LineageConflictError(
                    f"payload differs from the committed records at token {fencing_token}"
                )

        assets.append(new_asset)
        passports.append(new_passport)
        self.root.write_bytes(_ASSET_LEDGER, _canonical(assets), create_parents=True)
        self.root.write_bytes(_PASSPORT_LEDGER, _canonical(passports), create_parents=True)
        self._highest_token = fencing_token
        return LineageCommit(
            committed=True,
            highest_token=self._highest_token,
            asset_version_count=len(assets),
            passport_count=len(passports),
        )


# --- 3. injectable CommandBus authorization hook ---------------------------------


class CommandBusAuthorizer(Protocol):
    """A port the studio supplies to authorize a dispatch before staging."""

    def authorize(
        self, *, operation: str, actor: str, project_id: str, payload: dict[str, Any]
    ) -> bool: ...


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
    actor: str = "atlas",
    project_id: str = "atlas",
    operation: str = "titan.spine.run",
    payload: dict[str, Any] | None = None,
    attempt_id: str = "attempt-1",
    fencing_token: int = 1,
    published_root: str = "",
    lineage: AtlasLineageStore | None = None,
    **spine_kwargs: Any,
) -> SpineLedger:
    """Run the staged spine with the three governance checks around it.

    1. When ``command_bus_authorizer`` is supplied, authorization must succeed
       *before* any staging begins; a denial raises :class:`SpineAuthorizationError`
       and performs zero work.
    2. On failure the staged bytes are preserved under ``.quarantine`` with a
       deterministic receipt (the authoritative namespace is untouched), and the
       original exception is re-raised.
    3. On success, when a ``lineage`` store is supplied, the ledger is written to
       ``<out_dir>/ledger.json`` and a fenced ``AssetVersion``/``ArtifactPassport``
       pair is committed.
    """
    if command_bus_authorizer is not None:
        allowed = command_bus_authorizer.authorize(
            operation=operation, actor=actor, project_id=project_id, payload=payload or {}
        )
        if not allowed:
            raise SpineAuthorizationError(f"command bus denied {operation!r}")

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
    return ledger


def run_olympus_master_governed(
    bible: Any,
    filesystem: WorkspaceFilesystem,
    *,
    command_bus_authorizer: CommandBusAuthorizer | None = None,
    actor: str = "atlas",
    project_id: str = "atlas",
    operation: str = "olympus.master.render",
    payload: dict[str, Any] | None = None,
    attempt_id: str = "attempt-1",
    fencing_token: int = 1,
    published_root: str = "",
    lineage: AtlasLineageStore | None = None,
    **master_kwargs: Any,
) -> Any:
    """Run the Olympus capstone with the same three governance checks.

    Identical contract to :func:`run_staged_spine_governed` but for
    :func:`nexus_ai_agent.creative.atlas.olympus_master.render_master`.
    """
    from nexus_ai_agent.creative.atlas import olympus_master as om

    if command_bus_authorizer is not None:
        allowed = command_bus_authorizer.authorize(
            operation=operation, actor=actor, project_id=project_id, payload=payload or {}
        )
        if not allowed:
            raise SpineAuthorizationError(f"command bus denied {operation!r}")

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
    return ledger
