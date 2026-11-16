"""Signed resource-pack manifests over an RFC 6962 Merkle tree.

Tree construction (the "Golden Rule #4" contract)
-------------------------------------------------
* leaf hash   = ``SHA256(0x00 || leaf_payload)`` where ``leaf_payload`` is the
  unambiguous, length-prefixed encoding of ``relative_path``, ``sha256`` and
  ``size_bytes`` (see :func:`leaf_payload`);
* node hash   = ``SHA256(0x01 || left || right)``;
* an odd node at the end of a level is **promoted unchanged** to the next
  level — it is never paired with a copy of itself.  Duplicating it is the
  Bitcoin CVE-2012-2459 defect: ``[a, b, c]`` and ``[a, b, c, c]`` would share
  one root, so a tampered (duplicated) entry list would still verify.  The
  level-wise promotion is exactly the RFC 6962 ``MTH`` (split at the largest
  power of two below ``n``), which the tests prove against a recursive
  reference implementation;
* final root  = ``SHA256(0x02 || u64_be(total_leaves) || tree_root)`` — the
  leaf count is locked into the published root, so even an implementation
  that *did* duplicate could not forge a different-length list.

Trust model
-----------
:class:`ResourcePackManifest` is a frozen, *structurally* validated value.
Whether its declared totals and root are honest is decided by
:func:`verify_manifest_integrity` (and by an Ed25519 signature when a trusted
public key is configured).  :func:`verify_installed_pack` re-derives the root
from the **bytes on disk** and fails closed on any discrepancy.

The Ed25519 backend is ``cryptography``; it is loaded lazily so that an
installation without it still imports this module — but every signature
operation then fails with :class:`SignatureBackendUnavailableError` (a
verification *failure*, never a silent pass).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Literal, cast, get_args

from .errors import (
    ManifestValidationError,
    PackVerificationError,
    SignatureBackendUnavailableError,
    SignatureVerificationError,
)

__all__ = [
    "AcquisitionMode",
    "MANIFEST_SCHEMA_VERSION",
    "PackTier",
    "PackVerificationReport",
    "ResourceBlobEntry",
    "ResourcePackManifest",
    "build_manifest",
    "canonical_manifest_signing_bytes",
    "compute_merkle_root",
    "leaf_hash",
    "leaf_payload",
    "manifest_from_json",
    "manifest_to_dict",
    "manifest_to_json",
    "merkle_tree_root",
    "node_hash",
    "sha256_file",
    "sign_manifest",
    "verify_installed_pack",
    "verify_manifest_integrity",
    "verify_manifest_signature",
]

AcquisitionMode = Literal["remote_blob", "local_dna_seed"]
PackTier = Literal["core", "standard", "cinema_hd"]

MANIFEST_SCHEMA_VERSION = 1
_ACQUISITION_MODES: frozenset[str] = frozenset(get_args(AcquisitionMode))
_TIERS: frozenset[str] = frozenset(get_args(PackTier))
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PACK_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
_SIGNATURE_RE = re.compile(r"[0-9a-f]{128}")
_SIGNING_DOMAIN = b"NEXUS-RESOURCE-PACK-MANIFEST/v1\n"
_ED25519_MODULE = "cryptography.hazmat.primitives.asymmetric.ed25519"
_HASH_CHUNK = 1024 * 1024
_MAX_PATH_BYTES = 1024

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"
_ROOT_PREFIX = b"\x02"


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_relative_path(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise ManifestValidationError("relative_path must be a non-empty string")
    if len(path.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ManifestValidationError(f"relative_path too long: {path[:64]!r}...")
    if path.startswith("/") or "\\" in path or "\x00" in path or ":" in path:
        raise ManifestValidationError(f"relative_path must be a relative POSIX path: {path!r}")
    for segment in path.split("/"):
        if segment in ("", ".", ".."):
            raise ManifestValidationError(f"relative_path has an unsafe segment: {path!r}")
    return path


def _validate_sha256(value: object, what: str = "sha256") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ManifestValidationError(f"{what} must be 64 lowercase hex characters")
    return value


def _validate_non_negative(value: object, what: str) -> int:
    if not _is_int(value) or cast(int, value) < 0:
        raise ManifestValidationError(f"{what} must be a non-negative integer")
    return cast(int, value)


def _freeze_json(value: object, where: str) -> object:
    """Deep-copy a JSON-like value into immutable containers.

    Floats are rejected on purpose: seed recipes must be reproducible bit for
    bit on every platform, and integer (milli-unit) parameters guarantee it.
    """
    if value is None or isinstance(value, (bool, str)) or _is_int(value):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ManifestValidationError(f"{where}: mapping keys must be strings")
            frozen[key] = _freeze_json(item, f"{where}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, where) for item in value)
    raise ManifestValidationError(
        f"{where}: unsupported value {type(value).__name__} (use ints in milli-units, not floats)"
    )


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceBlobEntry:
    """One file of a pack.

    ``seed_recipe`` is required for ``local_dna_seed`` entries (it is what the
    device bakes) and forbidden for ``remote_blob`` entries.  It is stored as
    a read-only mapping so the frozen entry is genuinely immutable.
    """

    relative_path: str
    sha256: str
    size_bytes: int
    acquisition_mode: AcquisitionMode
    seed_recipe: Mapping[str, object] | None = field(default=None)

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        _validate_sha256(self.sha256)
        _validate_non_negative(self.size_bytes, "size_bytes")
        if self.acquisition_mode not in _ACQUISITION_MODES:
            raise ManifestValidationError(f"unknown acquisition_mode {self.acquisition_mode!r}")
        if self.acquisition_mode == "local_dna_seed":
            if not isinstance(self.seed_recipe, Mapping) or not self.seed_recipe:
                raise ManifestValidationError(
                    f"{self.relative_path}: local_dna_seed entries need a seed_recipe"
                )
            object.__setattr__(
                self, "seed_recipe", _freeze_json(self.seed_recipe, self.relative_path)
            )
        elif self.seed_recipe is not None:
            raise ManifestValidationError(
                f"{self.relative_path}: remote_blob entries must not carry a seed_recipe"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "acquisition_mode": self.acquisition_mode,
            "seed_recipe": _thaw_json(self.seed_recipe),
        }


@dataclass(frozen=True)
class ResourcePackManifest:
    """A versioned pack: entries sorted by ``relative_path`` plus its locked root."""

    pack_id: str
    title_fa: str
    tier: PackTier
    version: str
    mandatory: bool
    entries: tuple[ResourceBlobEntry, ...]
    total_installed_bytes: int
    total_network_download_bytes: int
    min_free_space_bytes: int
    total_leaves: int
    merkle_root: str
    signature: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pack_id, str) or not _PACK_ID_RE.fullmatch(self.pack_id):
            raise ManifestValidationError(f"invalid pack_id {self.pack_id!r}")
        if not isinstance(self.title_fa, str) or not self.title_fa.strip():
            raise ManifestValidationError("title_fa must be a non-empty string")
        if self.tier not in _TIERS:
            raise ManifestValidationError(f"unknown tier {self.tier!r}")
        if not isinstance(self.version, str) or not _VERSION_RE.fullmatch(self.version):
            raise ManifestValidationError(f"invalid version {self.version!r}")
        if not isinstance(self.mandatory, bool):
            raise ManifestValidationError("mandatory must be a bool")
        if self.tier == "core" and not self.mandatory:
            raise ManifestValidationError("the core tier is always mandatory")
        if not isinstance(self.entries, tuple) or not self.entries:
            raise ManifestValidationError("entries must be a non-empty tuple")
        if not all(isinstance(entry, ResourceBlobEntry) for entry in self.entries):
            raise ManifestValidationError("entries must be ResourceBlobEntry values")
        paths = [entry.relative_path for entry in self.entries]
        if paths != sorted(paths):
            raise ManifestValidationError("entries must be sorted by relative_path")
        if len(set(paths)) != len(paths):
            raise ManifestValidationError("duplicate relative_path in entries")
        _check_no_file_directory_clash(paths)
        for name in (
            "total_installed_bytes",
            "total_network_download_bytes",
            "min_free_space_bytes",
            "total_leaves",
        ):
            _validate_non_negative(getattr(self, name), name)
        _validate_sha256(self.merkle_root, "merkle_root")
        if self.signature is not None and (
            not isinstance(self.signature, str) or not _SIGNATURE_RE.fullmatch(self.signature)
        ):
            raise ManifestValidationError("signature must be 128 lowercase hex characters")

    @property
    def remote_entries(self) -> tuple[ResourceBlobEntry, ...]:
        return tuple(e for e in self.entries if e.acquisition_mode == "remote_blob")

    @property
    def seed_entries(self) -> tuple[ResourceBlobEntry, ...]:
        return tuple(e for e in self.entries if e.acquisition_mode == "local_dna_seed")


def _check_no_file_directory_clash(paths: Sequence[str]) -> None:
    """``a/b`` and ``a/b/c`` cannot both be files on any filesystem."""
    files = set(paths)
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            if "/".join(parts[:depth]) in files:
                parent = "/".join(parts[:depth])
                raise ManifestValidationError(f"{path!r} nests under the file {parent!r}")


# --------------------------------------------------------------------------
# RFC 6962 Merkle tree (no odd-node duplication, leaf count locked in root)
# --------------------------------------------------------------------------


def leaf_payload(relative_path: str, sha256: str, size_bytes: int) -> bytes:
    """Length-prefixed, ambiguity-free leaf encoding.

    ``u32_be(len(path)) || utf8(path) || sha256_raw(32) || u64_be(size)`` —
    no separator characters, so no two distinct triples share a payload.
    """
    encoded = _validate_relative_path(relative_path).encode("utf-8")
    digest = bytes.fromhex(_validate_sha256(sha256))
    size = _validate_non_negative(size_bytes, "size_bytes")
    return struct.pack(">I", len(encoded)) + encoded + digest + struct.pack(">Q", size)


def leaf_hash(payload: bytes) -> bytes:
    return hashlib.sha256(_LEAF_PREFIX + payload).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


def merkle_tree_root(leaf_hashes: Sequence[bytes]) -> bytes:
    """RFC 6962 ``MTH`` computed level by level, promoting an odd last node."""
    if not leaf_hashes:
        return hashlib.sha256(b"").digest()  # RFC 6962 §2.1: MTH({}) = SHA-256()
    level = list(leaf_hashes)
    while len(level) > 1:
        nxt = [node_hash(level[i], level[i + 1]) for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])  # promote — NEVER node_hash(last, last)
        level = nxt
    return level[0]


def compute_merkle_root(
    triples: Iterable[tuple[str, str, int]], *, total_leaves: int | None = None
) -> str:
    """Final, leaf-count-locked root over ``(relative_path, sha256, size)`` triples.

    ``total_leaves`` defaults to the number of triples; passing a different
    value shows what a forger would have to publish (and it never matches).
    """
    hashes = [leaf_hash(leaf_payload(path, sha, size)) for path, sha, size in triples]
    count = len(hashes) if total_leaves is None else total_leaves
    count = _validate_non_negative(count, "total_leaves")
    final = hashlib.sha256(_ROOT_PREFIX + struct.pack(">Q", count) + merkle_tree_root(hashes))
    return final.hexdigest()


def _entry_triples(entries: Iterable[ResourceBlobEntry]) -> list[tuple[str, str, int]]:
    return [(e.relative_path, e.sha256, e.size_bytes) for e in entries]


# --------------------------------------------------------------------------
# construction & integrity
# --------------------------------------------------------------------------


def build_manifest(
    *,
    pack_id: str,
    title_fa: str,
    tier: PackTier,
    version: str,
    entries: Iterable[ResourceBlobEntry],
    mandatory: bool | None = None,
    min_free_space_bytes: int | None = None,
) -> ResourcePackManifest:
    """Publisher-side factory: sorts entries and derives every total and the root.

    ``mandatory`` defaults to ``tier == "core"``.  ``min_free_space_bytes``
    defaults to twice the installed size (CAS copy + install tree, the worst
    case when hard links are unavailable).
    """
    ordered = tuple(sorted(entries, key=lambda e: e.relative_path))
    installed = sum(e.size_bytes for e in ordered)
    network = sum(e.size_bytes for e in ordered if e.acquisition_mode == "remote_blob")
    return ResourcePackManifest(
        pack_id=pack_id,
        title_fa=title_fa,
        tier=tier,
        version=version,
        mandatory=(tier == "core") if mandatory is None else mandatory,
        entries=ordered,
        total_installed_bytes=installed,
        total_network_download_bytes=network,
        min_free_space_bytes=2 * installed
        if min_free_space_bytes is None
        else min_free_space_bytes,
        total_leaves=len(ordered),
        merkle_root=compute_merkle_root(_entry_triples(ordered)),
    )


def _integrity_problems(manifest: ResourcePackManifest) -> list[str]:
    problems: list[str] = []
    entries = manifest.entries
    if manifest.total_leaves != len(entries):
        problems.append(
            f"total_leaves={manifest.total_leaves} but the manifest lists {len(entries)} entries"
        )
    installed = sum(e.size_bytes for e in entries)
    if manifest.total_installed_bytes != installed:
        problems.append(f"total_installed_bytes={manifest.total_installed_bytes} != {installed}")
    network = sum(e.size_bytes for e in manifest.remote_entries)
    if manifest.total_network_download_bytes != network:
        problems.append(
            f"total_network_download_bytes={manifest.total_network_download_bytes} != {network}"
        )
    if manifest.min_free_space_bytes < installed:
        problems.append("min_free_space_bytes is smaller than the installed size")
    expected = compute_merkle_root(_entry_triples(entries), total_leaves=manifest.total_leaves)
    if expected != manifest.merkle_root:
        problems.append("merkle_root does not match the entries and total_leaves")
    return problems


def verify_manifest_integrity(manifest: ResourcePackManifest) -> None:
    """Fail closed unless every declared total and the locked root are honest."""
    problems = _integrity_problems(manifest)
    if problems:
        raise PackVerificationError(problems)


# --------------------------------------------------------------------------
# canonical serialisation
# --------------------------------------------------------------------------


def manifest_to_dict(
    manifest: ResourcePackManifest, *, include_signature: bool = True
) -> dict[str, object]:
    data: dict[str, object] = {
        "schema": MANIFEST_SCHEMA_VERSION,
        "pack_id": manifest.pack_id,
        "title_fa": manifest.title_fa,
        "tier": manifest.tier,
        "version": manifest.version,
        "mandatory": manifest.mandatory,
        "entries": [entry.to_dict() for entry in manifest.entries],
        "total_installed_bytes": manifest.total_installed_bytes,
        "total_network_download_bytes": manifest.total_network_download_bytes,
        "min_free_space_bytes": manifest.min_free_space_bytes,
        "total_leaves": manifest.total_leaves,
        "merkle_root": manifest.merkle_root,
    }
    if include_signature:
        data["signature"] = manifest.signature
    return data


def _canonical_json(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def manifest_to_json(manifest: ResourcePackManifest) -> bytes:
    return _canonical_json(manifest_to_dict(manifest))


def canonical_manifest_signing_bytes(manifest: ResourcePackManifest) -> bytes:
    """The exact bytes an Ed25519 signature covers (every field but the signature)."""
    return _SIGNING_DOMAIN + _canonical_json(manifest_to_dict(manifest, include_signature=False))


_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "pack_id",
        "title_fa",
        "tier",
        "version",
        "mandatory",
        "entries",
        "total_installed_bytes",
        "total_network_download_bytes",
        "min_free_space_bytes",
        "total_leaves",
        "merkle_root",
        "signature",
    }
)
_ENTRY_KEYS = frozenset(
    {"relative_path", "sha256", "size_bytes", "acquisition_mode", "seed_recipe"}
)


def manifest_from_json(data: bytes | str) -> ResourcePackManifest:
    """Strict parser: unknown keys, wrong types and floats are rejected.

    Parsing never *trusts* the totals or the root — call
    :func:`verify_manifest_integrity` / :func:`verify_manifest_signature`.
    """
    try:
        raw = json.loads(data)
    except (ValueError, UnicodeDecodeError) as error:
        raise ManifestValidationError(f"manifest is not valid JSON: {error}") from error
    if not isinstance(raw, dict) or set(raw) != _MANIFEST_KEYS:
        raise ManifestValidationError("manifest object has missing or unknown keys")
    if raw["schema"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError(f"unsupported manifest schema {raw['schema']!r}")
    raw_entries = raw["entries"]
    if not isinstance(raw_entries, list):
        raise ManifestValidationError("entries must be a list")
    entries: list[ResourceBlobEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict) or set(item) != _ENTRY_KEYS:
            raise ManifestValidationError("entry object has missing or unknown keys")
        entries.append(
            ResourceBlobEntry(
                relative_path=item["relative_path"],
                sha256=item["sha256"],
                size_bytes=item["size_bytes"],
                acquisition_mode=item["acquisition_mode"],
                seed_recipe=item["seed_recipe"],
            )
        )
    return ResourcePackManifest(
        pack_id=raw["pack_id"],
        title_fa=raw["title_fa"],
        tier=raw["tier"],
        version=raw["version"],
        mandatory=raw["mandatory"],
        entries=tuple(entries),
        total_installed_bytes=raw["total_installed_bytes"],
        total_network_download_bytes=raw["total_network_download_bytes"],
        min_free_space_bytes=raw["min_free_space_bytes"],
        total_leaves=raw["total_leaves"],
        merkle_root=raw["merkle_root"],
        signature=raw["signature"],
    )


# --------------------------------------------------------------------------
# Ed25519 (cryptography backend, loaded lazily, fail-closed)
# --------------------------------------------------------------------------


def _ed25519_backend() -> ModuleType:
    try:
        return importlib.import_module(_ED25519_MODULE)
    except ImportError as error:
        raise SignatureBackendUnavailableError(
            "Ed25519 backend unavailable: install the 'cryptography' package"
        ) from error


def sign_manifest(manifest: ResourcePackManifest, private_key: bytes) -> ResourcePackManifest:
    """Publisher tooling: return a copy of *manifest* carrying an Ed25519 signature.

    ``private_key`` is the raw 32-byte seed supplied by the caller (a release
    pipeline secret).  This module never stores, derives or generates keys.
    """
    if not isinstance(private_key, bytes) or len(private_key) != 32:
        raise SignatureVerificationError("Ed25519 private key must be 32 raw bytes")
    verify_manifest_integrity(manifest)  # never sign a dishonest manifest
    backend = _ed25519_backend()
    key: Any = backend.Ed25519PrivateKey.from_private_bytes(private_key)
    signature = cast(bytes, key.sign(canonical_manifest_signing_bytes(manifest)))
    return replace(manifest, signature=signature.hex())


def verify_manifest_signature(manifest: ResourcePackManifest, public_key: bytes) -> None:
    """Raise :class:`SignatureVerificationError` unless the signature verifies."""
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise SignatureVerificationError("Ed25519 public key must be 32 raw bytes")
    if manifest.signature is None:
        raise SignatureVerificationError("manifest is unsigned but a trusted key is configured")
    backend = _ed25519_backend()
    message = canonical_manifest_signing_bytes(manifest)
    try:
        key: Any = backend.Ed25519PublicKey.from_public_bytes(public_key)
        key.verify(bytes.fromhex(manifest.signature), message)
    except Exception as error:  # InvalidSignature, malformed key — all fail closed
        raise SignatureVerificationError(
            f"Ed25519 signature does not verify ({type(error).__name__})"
        ) from error


# --------------------------------------------------------------------------
# on-disk verification
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> tuple[str, int]:
    """Streamed SHA-256 and byte count of a file."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class PackVerificationReport:
    """Successful outcome of :func:`verify_installed_pack`."""

    pack_id: str
    version: str
    verified_files: int
    verified_bytes: int
    merkle_root: str
    signature_verified: bool


def _scan_tree(install_dir: Path) -> tuple[set[str], list[str]]:
    """Every regular file under *install_dir* (POSIX relative), plus problems."""
    files: set[str] = set()
    problems: list[str] = []
    for path in sorted(install_dir.rglob("*")):
        rel = path.relative_to(install_dir).as_posix()
        if path.is_symlink():
            problems.append(f"{rel}: symbolic links are not allowed in an installed pack")
        elif path.is_file():
            files.add(rel)
        elif not path.is_dir():
            problems.append(f"{rel}: not a regular file")
    return files, problems


def verify_installed_pack(
    manifest: ResourcePackManifest,
    install_dir: Path,
    *,
    trusted_public_key: bytes | None = None,
) -> PackVerificationReport:
    """Fail-closed verification of an installed pack against its manifest.

    Checks, in order: declared totals and locked root; the Ed25519 signature
    (mandatory when *trusted_public_key* is given); every file's presence,
    type, size and SHA-256; the absence of unexpected files or symlinks; and
    finally the Merkle root recomputed from the **observed** on-disk bytes.
    Any single discrepancy raises :class:`PackVerificationError`.
    """
    problems = _integrity_problems(manifest)
    signature_verified = False
    if trusted_public_key is not None:
        verify_manifest_signature(manifest, trusted_public_key)
        signature_verified = True
    if not install_dir.is_dir() or install_dir.is_symlink():
        raise PackVerificationError([*problems, f"install directory missing: {install_dir}"])
    on_disk, scan_problems = _scan_tree(install_dir)
    problems.extend(scan_problems)
    observed: list[tuple[str, str, int]] = []
    for entry in manifest.entries:
        if entry.relative_path not in on_disk:
            problems.append(f"{entry.relative_path}: missing")
            continue
        path = install_dir / entry.relative_path
        size = path.stat().st_size
        if size != entry.size_bytes:
            problems.append(f"{entry.relative_path}: size {size} != {entry.size_bytes}")
            continue
        digest, _ = sha256_file(path)
        if digest != entry.sha256:
            problems.append(f"{entry.relative_path}: sha256 mismatch")
        observed.append((entry.relative_path, digest, size))
    for extra in sorted(on_disk - {e.relative_path for e in manifest.entries}):
        problems.append(f"{extra}: unexpected file not listed in the manifest")
    if not problems:
        observed_root = compute_merkle_root(observed, total_leaves=manifest.total_leaves)
        if observed_root != manifest.merkle_root:  # pragma: no cover - implied by per-file checks
            problems.append("merkle root recomputed from disk does not match")
    if problems:
        raise PackVerificationError(problems)
    return PackVerificationReport(
        pack_id=manifest.pack_id,
        version=manifest.version,
        verified_files=len(observed),
        verified_bytes=sum(size for _, _, size in observed),
        merkle_root=manifest.merkle_root,
        signature_verified=signature_verified,
    )
