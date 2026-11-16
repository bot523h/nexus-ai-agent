"""Runtime delivery status for resource packs already installed on this device.

The signature machinery (`pack_manifest.signature_required`) is only worth
anything if something *runs* it.  Before this module the fail-closed policy
existed but was reachable only from the sync manager, so a deployment could sit
in the "no trust root configured" state indefinitely without anyone noticing —
the exact shape of an unreachable safety check.

:func:`delivery_statuses` is that consumer: given a packs root, it reads the
active pointer each pack left behind, re-verifies the installed tree against its
signed manifest, and reports one row per pack with an honest state.  It is what
``GET /api/studio/resource-packs`` answers with, and it is deliberately
read-only — no download, no activation, no pointer write.

Three outcomes are kept apart on purpose:

* ``ready`` — the bytes verify against the manifest *and* the signature verified
  against the trusted key;
* ``unsigned_allowed`` — the bytes verify, and the deployment explicitly opted
  out of signing (``allow_unsigned=True``);
* ``signature_required`` — the bytes may be perfectly fine, but this deployment
  has no trust root, so nothing may be delivered.  This is a configuration
  state, never a claim that the pack was tampered with.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .errors import PackSyncError, PackVerificationError, SignatureVerificationError
from .pack_manifest import ResourcePackManifest, manifest_from_json, verify_installed_pack

PackDeliveryState = Literal[
    "ready",
    "unsigned_allowed",
    "signature_required",
    "repair_required",
    "install_required",
]
PackSignatureState = Literal["verified", "unsigned_allowed", "not_checked"]

__all__ = [
    "InstalledPackStatus",
    "PackDeliveryState",
    "PackSignatureState",
    "delivery_report",
    "delivery_statuses",
]


@dataclass(frozen=True)
class InstalledPackStatus:
    """One installed pack, as this device can honestly describe it."""

    pack_id: str
    version: str | None
    state: PackDeliveryState
    signature: PackSignatureState
    verified_files: int
    detail: str

    @property
    def usable(self) -> bool:
        """True only when the pack may actually be delivered to a render."""
        return self.state in ("ready", "unsigned_allowed")

    def as_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "version": self.version,
            "state": self.state,
            "signature": self.signature,
            "verified_files": self.verified_files,
            "usable": self.usable,
            "detail": self.detail,
        }


def _pack_ids(packs_root: Path) -> list[str]:
    """Installed pack ids — a directory under ``packs/`` that carries a pointer."""
    base = packs_root / "packs"
    if not base.is_dir():
        return []
    return sorted(
        child.name
        for child in base.iterdir()
        if child.is_dir() and (child / "active.json").is_file()
    )


def _read_pointer(pack_dir: Path) -> dict[str, Any]:
    try:
        raw = json.loads((pack_dir / "active.json").read_bytes())
    except (OSError, ValueError) as error:
        raise PackSyncError(f"unreadable active pointer: {error}") from error
    if not isinstance(raw, dict):
        raise PackSyncError("active pointer is not a JSON object")
    return raw


def _status_for_pack(
    packs_root: Path,
    pack_id: str,
    *,
    trusted_public_key: bytes | None,
    allow_unsigned: bool,
) -> InstalledPackStatus:
    pack_dir = packs_root / "packs" / pack_id
    try:
        pointer = _read_pointer(pack_dir)
    except PackSyncError as error:
        return InstalledPackStatus(
            pack_id=pack_id,
            version=None,
            state="repair_required",
            signature="not_checked",
            verified_files=0,
            detail=f"active pointer is unreadable: {error}",
        )
    install_name = pointer.get("install_dir")
    version = pointer.get("version")
    if not isinstance(install_name, str) or not isinstance(version, str):
        return InstalledPackStatus(
            pack_id=pack_id,
            version=None,
            state="repair_required",
            signature="not_checked",
            verified_files=0,
            detail="active pointer does not name a version and install directory",
        )
    manifest_path = pack_dir / "versions" / f"{install_name}.manifest.json"
    install_dir = pack_dir / "versions" / install_name
    try:
        manifest: ResourcePackManifest = manifest_from_json(manifest_path.read_bytes())
    except (OSError, ValueError) as error:
        return InstalledPackStatus(
            pack_id=pack_id,
            version=version,
            state="repair_required",
            signature="not_checked",
            verified_files=0,
            detail=f"installed manifest is unreadable: {error}",
        )
    try:
        report = verify_installed_pack(
            manifest,
            install_dir,
            trusted_public_key=trusted_public_key,
            allow_unsigned=allow_unsigned,
        )
    except SignatureVerificationError as error:
        return InstalledPackStatus(
            pack_id=pack_id,
            version=version,
            state="signature_required",
            signature="not_checked",
            verified_files=0,
            detail=(
                "the pack cannot be delivered: the bytes on disk were not authenticated "
                f"({error}). Configure a trusted Ed25519 public key, or run this "
                "deployment with an explicit unsigned policy."
            ),
        )
    except PackVerificationError as error:
        return InstalledPackStatus(
            pack_id=pack_id,
            version=version,
            state="repair_required",
            signature="not_checked",
            verified_files=0,
            detail=f"installed bytes do not match the manifest ({error})",
        )
    return InstalledPackStatus(
        pack_id=pack_id,
        version=version,
        state="ready" if report.signature_verified else "unsigned_allowed",
        signature="verified" if report.signature_verified else "unsigned_allowed",
        verified_files=report.verified_files,
        detail=(
            f"{report.verified_files} files, {report.verified_bytes} bytes, Ed25519 verified"
            if report.signature_verified
            else f"{report.verified_files} files, {report.verified_bytes} bytes, "
            "signature not required by this deployment's policy"
        ),
    )


def delivery_statuses(
    packs_root: Path,
    *,
    trusted_public_key: bytes | None = None,
    allow_unsigned: bool = False,
) -> list[InstalledPackStatus]:
    """One row per installed pack, verifying each against its signed manifest.

    *allow_unsigned* defaults to ``False``: without a trusted key every pack is
    reported ``signature_required`` instead of being called ready.  Nothing is
    written, created or repaired here.
    """
    root = Path(packs_root)
    return [
        _status_for_pack(
            root,
            pack_id,
            trusted_public_key=trusted_public_key,
            allow_unsigned=allow_unsigned,
        )
        for pack_id in _pack_ids(root)
    ]


def delivery_report(
    packs_root: Path | None,
    *,
    trusted_public_key: bytes | None = None,
    allow_unsigned: bool = False,
) -> dict[str, Any]:
    """The JSON shape served by ``GET /api/studio/resource-packs``.

    An unconfigured root reports ``configured: False`` rather than an empty,
    falsely reassuring list: "no packs installed" and "no root configured" are
    different operational states and must not render identically.
    """
    if packs_root is None:
        return {
            "configured": False,
            "fail_closed": True,
            "packs_root": None,
            "packs": [],
            "reason": "no resource-pack root is configured for this deployment",
        }
    rows = delivery_statuses(
        packs_root, trusted_public_key=trusted_public_key, allow_unsigned=allow_unsigned
    )
    return {
        "configured": True,
        "fail_closed": not allow_unsigned,
        "packs_root": str(packs_root),
        "packs": [row.as_dict() for row in rows],
        "reason": None,
    }
