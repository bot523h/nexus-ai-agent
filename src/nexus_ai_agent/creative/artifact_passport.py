"""Creative Artifact Passport: an HMAC-sealed record of one verified render.

A passport is written only after the independent verifier returns ``PASS``.
It binds the output bytes (size and SHA-256) to the request that produced them
(project, source and manifest digests, render profile, asset Merkle root). The
seal is HMAC-SHA256 over the canonical JSON of every field except
``hmac_sha256`` itself, keyed with the studio signing secret.

Verification checks the seal in constant time, then re-hashes the MP4 currently
on disk and compares its size and SHA-256 with the passport. Any mismatch,
missing field, unknown version, or a non-``PASS`` status is refused.

Per-shot Merkle provenance (RFC 6962): every passport also seals the per-shot
tree - ``shot_hashes``, ``shot_merkle_root`` and one inclusion proof per shot -
so the HMAC covers each audit path individually. Verification is fail-closed in
three steps: the root is recomputed from the declared shot hashes, the proof
count is compared with the shot count, and every proof must agree on its leaf
index, leaf hash, root and leaf count *and* replay to the sealed root. Any
failure refuses the passport with the typed code ``shot_merkle_root_mismatch``,
``shot_inclusion_proof_count_mismatch`` or ``shot_inclusion_proof_invalid``.

Versioning: ``passport_version`` 1 is the pre-Merkle format that predates the
per-shot tree; version 2 (:data:`PASSPORT_VERSION`) always carries it. Passports
already written to disk as version 1 keep verifying, because the Merkle fields
are *optional* there - but if a version 1 passport does carry any of them they
are validated exactly as strictly as version 2's. A caller that must not accept
legacy evidence asks for ``min_version=2`` and every version 1 passport is then
refused with ``passport_version_unsupported``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from nexus_ai_agent.creative.materializer import (
    MaterializationError,
    atomic_write,
    canonical_json,
    merkle_inclusion_proof_record,
    merkle_root_sha256,
    shot_leaf_payloads,
    verify_shot_inclusion_proofs,
)

#: Version this module emits. Version 2 added the sealed per-shot Merkle tree.
PASSPORT_VERSION = 2
#: Versions :func:`verify_artifact_passport` can still verify.
SUPPORTED_PASSPORT_VERSIONS = (1, 2)
PASSPORT_SUFFIX = ".passport.json"
#: Hard ceiling on a serialized passport document: a passport is a single JSON
#: document that is read whole into memory before it is verified, so its size
#: is bounded to keep verification memory-flat and to refuse a padded or
#: bloated document outright.  A full 96-shot storyboard (and every board up
#: to 128 shots) seals one RFC 6962 audit path per shot — measured at ~87 KiB
#: for 96 shots — so the ceiling is 256 KiB: deterministic headroom for the
#: sealed tree while staying memory-flat.
MAX_PASSPORT_BYTES = 262_144
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UNSIGNED_FIELDS = ("hmac_sha256",)
#: The fields every passport carried before the per-shot tree existed.
PASSPORT_V1_FIELDS = (
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
#: The three fields version 2 added. Optional in version 1, mandatory in version 2.
SHOT_MERKLE_FIELDS = ("shot_hashes", "shot_merkle_root", "shot_inclusion_proofs")
REQUIRED_V1_FIELDS = frozenset(PASSPORT_V1_FIELDS)
REQUIRED_FIELDS = REQUIRED_V1_FIELDS | frozenset(SHOT_MERKLE_FIELDS)
#: Typed violation codes produced by :func:`passport_shot_proof_violations`.
SHOT_MERKLE_ROOT_MISMATCH = "shot_merkle_root_mismatch"
SHOT_PROOF_COUNT_MISMATCH = "shot_inclusion_proof_count_mismatch"
SHOT_PROOF_INVALID = "shot_inclusion_proof_invalid"


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
    shot_hashes: Sequence[str] = (),
    signing_key: object,
) -> dict[str, Any]:
    """Assemble and seal a passport. Refuses anything that is not a PASS.

    ``shot_hashes`` is the optional per-shot tree: when supplied, the RFC 6962
    root and one inclusion proof per shot are derived here and sealed, so no
    caller can emit a passport whose proofs disagree with its own leaves. An
    empty sequence seals the empty-tree root and no proofs. The emitted passport
    is always version 2, i.e. it always carries all three Merkle fields.
    """
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
    leaves, proofs = _shot_tree(shot_hashes)
    key = _signing_key(signing_key)
    fields: dict[str, Any] = {
        "passport_version": int(PASSPORT_VERSION),
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
        "shot_hashes": list(shot_hashes),
        "shot_merkle_root": merkle_root_sha256(leaves),
        "shot_inclusion_proofs": proofs,
    }
    violations = verify_shot_inclusion_proofs(
        shot_hashes=fields["shot_hashes"],
        merkle_root=fields["shot_merkle_root"],
        proofs=fields["shot_inclusion_proofs"],
    )
    if violations:  # pragma: no cover - build-time invariant, cannot be reached
        raise PassportError(violations[0], "self-verification of the sealed shot tree failed")
    fields["hmac_sha256"] = _seal(fields, key)
    return fields


def _shot_tree(shot_hashes: Sequence[str]) -> tuple[list[bytes], list[dict[str, Any]]]:
    """Validate shot hashes and derive the leaf payloads plus every audit path."""
    if isinstance(shot_hashes, (str, bytes)) or not isinstance(shot_hashes, Sequence):
        raise PassportError("invalid_digest", "shot_hashes must be a sequence of hex digests")
    try:
        leaves = shot_leaf_payloads(tuple(shot_hashes))
    except MaterializationError as exc:
        raise PassportError("invalid_digest", f"shot_hashes: {exc.detail}") from exc
    proofs = [merkle_inclusion_proof_record(leaves, index) for index in range(len(leaves))]
    return leaves, proofs


def passport_shot_proof_violations(passport: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the typed per-shot Merkle violations carried by ``passport``.

    An empty tuple means the sealed tree verifies. Structural problems are
    reported as violations rather than exceptions so callers (and the download
    gate) get a stable, typed refusal either way.
    """
    shot_hashes = passport.get("shot_hashes")
    merkle_root = passport.get("shot_merkle_root")
    proofs = passport.get("shot_inclusion_proofs")
    if not isinstance(merkle_root, str):
        return (SHOT_MERKLE_ROOT_MISMATCH,)
    if not isinstance(shot_hashes, list) or not all(isinstance(h, str) for h in shot_hashes):
        return (SHOT_PROOF_INVALID,)
    if not isinstance(proofs, list):
        return (SHOT_PROOF_INVALID,)
    return verify_shot_inclusion_proofs(
        shot_hashes=shot_hashes, merkle_root=merkle_root, proofs=proofs
    )


def write_passport(path: Path, passport: dict[str, Any]) -> None:
    """Atomically persist a passport beside its artifact."""
    payload = json.dumps(passport, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8")

    def _write(handle: IO[bytes]) -> None:
        handle.write(payload)

    atomic_write(path, _write)


def _fd_sha256(fd: int, *, chunk_bytes: int = 1_048_576) -> tuple[str, int]:
    """SHA-256 and byte count read from exactly this descriptor.

    :func:`os.pread` never moves the descriptor's offset, so the hash is a
    pure function of the open file description it runs against — a concurrent
    writer appends *after* the hashed region or changes bytes we already read
    only at its own peril: the byte count is compared against the ``fstat``
    size taken before hashing, and any drift is a typed refusal.
    """
    digest = hashlib.sha256()
    size = 0
    offset = 0
    while True:
        block = os.pread(fd, chunk_bytes, offset)
        if not block:
            break
        digest.update(block)
        size += len(block)
        offset += len(block)
    return digest.hexdigest(), size


def verify_artifact_passport(
    passport_path: Path,
    mp4_path: Path,
    secret_key: object,
    *,
    min_version: int = 1,
    mp4_fd: int | None = None,
) -> PassportCheck:
    """Verify seal, verdict and the exact MP4 bytes on disk. Raises on any mismatch.

    ``min_version`` is the oldest passport version the caller is willing to
    accept: the default 1 keeps every already-written passport verifiable, while
    a caller that only trusts evidence sealed with the per-shot Merkle tree asks
    for ``min_version=2`` and legacy passports are refused as
    ``passport_version_unsupported``.

    The artifact is opened **exactly once** — ``O_RDONLY | O_NOFOLLOW`` — and
    every check (regular file, size, SHA-256) runs against that same
    descriptor, closing the stat-then-open TOCTOU window: what is hashed is
    byte-for-byte what a caller holding the descriptor streams. A caller that
    already owns a verified-open descriptor (the download gate) passes it as
    ``mp4_fd``; the caller keeps ownership and the descriptor is left open,
    rewound to offset 0 by the caller after the checks pass.
    """
    if isinstance(min_version, bool) or not isinstance(min_version, int) or min_version < 1:
        raise PassportError("invalid_min_version", repr(min_version))
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
            if (opened.st_ino, opened.st_dev) != (pst.st_ino, pst.st_dev):
                # The path was swapped for a different inode between the stat
                # and the single open: refuse rather than read a different file.
                raise PassportError("passport_unreadable", "inode changed between stat and open")
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
    version = passport.get("passport_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in SUPPORTED_PASSPORT_VERSIONS
    ):
        raise PassportError("passport_version_unsupported", repr(version))
    if version < min_version:
        raise PassportError(
            "passport_version_unsupported", f"version {version} < required {min_version}"
        )
    required = REQUIRED_FIELDS if version >= 2 else REQUIRED_V1_FIELDS
    missing = sorted(name for name in required if name not in passport)
    if missing:
        raise PassportError("passport_malformed", f"missing {missing}")
    expected_seal = _seal(passport, key)
    if not hmac.compare_digest(str(passport["hmac_sha256"]), expected_seal):
        raise PassportError("seal_mismatch", "HMAC does not verify")
    if passport["verifier_status"] != "PASS":
        raise PassportError("verifier_not_pass", str(passport["verifier_status"]))
    # Per-shot Merkle provenance is checked before a single artifact byte is
    # re-read: a passport whose tree does not recompute is refused outright.
    # Version 2 must carry the tree; version 1 may omit it, but any Merkle field
    # it *does* carry is validated just as strictly - a partial tree is a defect.
    if version >= 2 or any(name in passport for name in SHOT_MERKLE_FIELDS):
        proof_violations = passport_shot_proof_violations(passport)
        if proof_violations:
            raise PassportError(proof_violations[0], ", ".join(proof_violations))

    # The artifact side: one single open descriptor is the only thing that is
    # ever hashed. When the caller did not supply one it is opened here (and
    # closed here); the lstat above it only preserves the typed refusal codes.
    owns_fd = mp4_fd is None
    mst: os.stat_result | None = None
    artifact_fd: int | None = mp4_fd
    if owns_fd:
        mst = _lstat_regular(mp4_path, "artifact")
        if mst.st_size != passport["output_size_bytes"]:
            raise PassportError("artifact_mismatch", "size differs from passport")
        try:
            artifact_fd = os.open(
                mp4_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise PassportError(
                "artifact_mismatch", "artifact changed during verification"
            ) from exc
    try:
        assert artifact_fd is not None
        opened = os.fstat(artifact_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PassportError("artifact_not_regular", str(mp4_path))
        if mst is not None and (opened.st_ino, opened.st_dev) != (mst.st_ino, mst.st_dev):
            raise PassportError("artifact_mismatch", "artifact inode changed during verification")
        if opened.st_size != passport["output_size_bytes"]:
            raise PassportError("artifact_mismatch", "size differs from passport")
        try:
            digest, hashed_bytes = _fd_sha256(artifact_fd)
        except OSError as exc:
            raise PassportError(
                "artifact_mismatch", "artifact changed during verification"
            ) from exc
        if hashed_bytes != opened.st_size:
            raise PassportError("artifact_mismatch", "artifact changed during verification")
    finally:
        if owns_fd and artifact_fd is not None:
            os.close(artifact_fd)
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
    "PASSPORT_V1_FIELDS",
    "PASSPORT_VERSION",
    "REQUIRED_FIELDS",
    "REQUIRED_V1_FIELDS",
    "SHOT_MERKLE_FIELDS",
    "SUPPORTED_PASSPORT_VERSIONS",
    "SHOT_MERKLE_ROOT_MISMATCH",
    "SHOT_PROOF_COUNT_MISMATCH",
    "SHOT_PROOF_INVALID",
    "PassportCheck",
    "PassportError",
    "build_passport",
    "passport_shot_proof_violations",
    "verify_artifact_passport",
    "write_passport",
]
