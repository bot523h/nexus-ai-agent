"""Creative Artifact Passport: an HMAC-sealed record of one verified render.

A passport is written only after the independent verifier returns ``PASS``.
It binds the output bytes (size and SHA-256) to the request that produced them
(project, source and manifest digests, render profile, asset Merkle root). The
seal is HMAC-SHA256 over the canonical JSON of every field except
``hmac_sha256`` itself, keyed with the studio signing secret.

Verification checks the seal in constant time, then re-hashes the MP4 currently
on disk and compares its size and SHA-256 with the passport. Any mismatch,
missing field, unknown version, or a non-``PASS`` status is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus_ai_agent.creative.materializer import (
    MaterializationError,
    atomic_write,
    canonical_json,
    stream_sha256,
)

PASSPORT_VERSION = 1
PASSPORT_SUFFIX = ".passport.json"
MAX_PASSPORT_BYTES = 65_536
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UNSIGNED_FIELDS = ("hmac_sha256",)
REQUIRED_FIELDS = (
    "passport_version",
    "job_id",
    "project_id",
    "source_sha256",
    "manifest_sha256",
    "render_profile_hash",
    "asset_merkle_root",
    "total_asset_leaves",
    "output_rel_path",
    "output_size_bytes",
    "output_sha256",
    "probed_duration_s",
    "verifier_status",
    "created_at_unix_s",
    "hmac_sha256",
)


class PassportError(ValueError):
    """A typed refusal while building or verifying a passport."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PassportCheck:
    passport: dict[str, Any]
    output_path: Path


def _signing_key(key: object) -> bytes:
    if isinstance(key, str):
        raw = key.encode("utf-8")
    elif isinstance(key, (bytes, bytearray)):
        raw = bytes(key)
    else:
        raise PassportError("invalid_key", "signing key must be str or bytes")
    if len(raw) < 32:
        raise PassportError("invalid_key", "signing key must be at least 32 bytes")
    return raw


def _seal(fields: dict[str, Any], key: bytes) -> str:
    unsigned = {k: v for k, v in fields.items() if k not in _UNSIGNED_FIELDS}
    return hmac.new(key, canonical_json(unsigned), hashlib.sha256).hexdigest()


def build_passport(
    *,
    job_id: str,
    project_id: str,
    source_sha256: str,
    manifest_sha256: str,
    render_profile_hash: str,
    asset_merkle_root: str,
    total_asset_leaves: int,
    output_rel_path: str,
    output_size_bytes: int,
    output_sha256: str,
    probed_duration_s: float,
    verifier_status: str,
    created_at_unix_s: float,
    signing_key: object,
) -> dict[str, Any]:
    """Assemble and seal a passport. Refuses anything that is not a PASS."""
    if verifier_status != "PASS":
        raise PassportError("verifier_not_pass", f"status={verifier_status!r}")
    for name, value in (
        ("source_sha256", source_sha256),
        ("manifest_sha256", manifest_sha256),
        ("render_profile_hash", render_profile_hash),
        ("asset_merkle_root", asset_merkle_root),
        ("output_sha256", output_sha256),
    ):
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            raise PassportError("invalid_digest", name)
    if isinstance(probed_duration_s, bool) or not isinstance(probed_duration_s, (int, float)):
        raise PassportError("invalid_number", "probed_duration_s")
    if not math.isfinite(probed_duration_s):
        raise PassportError("invalid_number", "probed_duration_s must be finite")
    if probed_duration_s <= 0:
        raise PassportError("invalid_number", "probed_duration_s must be positive")
    if output_size_bytes <= 0:
        raise PassportError("invalid_number", "output_size_bytes must be positive")
    key = _signing_key(signing_key)
    fields: dict[str, Any] = {
        "passport_version": PASSPORT_VERSION,
        "job_id": job_id,
        "project_id": project_id,
        "source_sha256": source_sha256,
        "manifest_sha256": manifest_sha256,
        "render_profile_hash": render_profile_hash,
        "asset_merkle_root": asset_merkle_root,
        "total_asset_leaves": int(total_asset_leaves),
        "output_rel_path": output_rel_path,
        "output_size_bytes": int(output_size_bytes),
        "output_sha256": output_sha256,
        "probed_duration_s": repr(float(probed_duration_s)),
        "verifier_status": verifier_status,
        "created_at_unix_s": int(created_at_unix_s),
    }
    fields["hmac_sha256"] = _seal(fields, key)
    return fields


def write_passport(path: Path, passport: dict[str, Any]) -> None:
    """Atomically persist a passport beside its artifact."""
    payload = json.dumps(passport, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8")
    atomic_write(path, lambda handle: handle.write(payload))


def verify_artifact_passport(
    passport_path: Path,
    mp4_path: Path,
    secret_key: object,
) -> PassportCheck:
    """Verify seal, verdict and the exact MP4 bytes on disk. Raises on any mismatch."""
    key = _signing_key(secret_key)
    pst = _lstat_regular(passport_path, "passport")
    if pst.st_size <= 0 or pst.st_size > MAX_PASSPORT_BYTES:
        raise PassportError("passport_size", str(pst.st_size))
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(passport_path, flags)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise PassportError("passport_not_regular", str(passport_path))
            if opened.st_size <= 0 or opened.st_size > MAX_PASSPORT_BYTES:
                raise PassportError("passport_size", str(opened.st_size))
            raw = handle.read(MAX_PASSPORT_BYTES + 1)
        if len(raw) > MAX_PASSPORT_BYTES:
            raise PassportError("passport_size", str(len(raw)))
        passport = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PassportError("passport_unreadable", type(exc).__name__) from exc
    if not isinstance(passport, dict):
        raise PassportError("passport_malformed", "not an object")
    missing = [name for name in REQUIRED_FIELDS if name not in passport]
    if missing:
        raise PassportError("passport_malformed", f"missing {missing}")
    if passport["passport_version"] != PASSPORT_VERSION:
        raise PassportError("passport_version", str(passport["passport_version"]))
    expected_seal = _seal(passport, key)
    if not hmac.compare_digest(str(passport["hmac_sha256"]), expected_seal):
        raise PassportError("seal_mismatch", "HMAC does not verify")
    if passport["verifier_status"] != "PASS":
        raise PassportError("verifier_not_pass", str(passport["verifier_status"]))

    mst = _lstat_regular(mp4_path, "artifact")
    if mst.st_size != passport["output_size_bytes"]:
        raise PassportError("artifact_mismatch", "size differs from passport")
    try:
        digest, _, _ = stream_sha256(Path(mp4_path))
    except (OSError, MaterializationError) as exc:
        raise PassportError("artifact_mismatch", "artifact changed during verification") from exc
    if not hmac.compare_digest(digest, str(passport["output_sha256"])):
        raise PassportError("artifact_mismatch", "sha256 differs from passport")
    return PassportCheck(passport=passport, output_path=Path(mp4_path))


def _lstat_regular(path: Path, label: str) -> os.stat_result:
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise PassportError(f"{label}_missing", str(path)) from exc
    if stat.S_ISLNK(st.st_mode):
        raise PassportError(f"{label}_symlink_refused", str(path))
    if not stat.S_ISREG(st.st_mode):
        raise PassportError(f"{label}_not_regular", str(path))
    return st


__all__ = [
    "PASSPORT_SUFFIX",
    "PASSPORT_VERSION",
    "REQUIRED_FIELDS",
    "PassportCheck",
    "PassportError",
    "build_passport",
    "verify_artifact_passport",
    "write_passport",
]
