"""Production configuration preflight and an isolated local SQLite drill.

A local drill is not a remote backup. SQLite source and work directories must
be trusted against pathname replacement while the SQLite library opens files.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping, Set
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

from nexus_ai_agent.config.settings import Settings

_B2 = (
    "NEXUS_B2_KEY_ID",
    "NEXUS_B2_APPLICATION_KEY",
    "NEXUS_B2_BUCKET",
    "NEXUS_B2_ENDPOINT",
    "NEXUS_B2_REGION",
)
_R2 = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
_DATABASE = "NEXUS_DATABASE_URL"
ABSOLUTE_MAX_DRILL_DATABASE_BYTES = 64 * 1024 * 1024
_ALIASES = {
    _DATABASE: (_DATABASE, "DATABASE_URL"),
    _B2[0]: (_B2[0], "B2_KEY_ID", "B2_APPLICATION_KEY_ID"),
    _B2[1]: (_B2[1], "B2_APPLICATION_KEY"),
    _B2[2]: (_B2[2], "NEXUS_B2_BUCKET_NAME", "B2_BUCKET", "B2_BUCKET_NAME"),
    _B2[3]: (_B2[3], "NEXUS_B2_ENDPOINT_URL", "B2_ENDPOINT", "B2_ENDPOINT_URL"),
    _B2[4]: (_B2[4], "B2_REGION"),
    _R2[0]: ("NEXUS_R2_ACCOUNT_ID", _R2[0]),
    _R2[1]: ("NEXUS_R2_ACCESS_KEY_ID", _R2[1]),
    _R2[2]: ("NEXUS_R2_SECRET_ACCESS_KEY", _R2[2]),
    _R2[3]: ("NEXUS_R2_BUCKET", "NEXUS_R2_BUCKET_NAME", _R2[3], "R2_BUCKET_NAME"),
    "R2_ENDPOINT_URL": ("NEXUS_R2_ENDPOINT_URL", "R2_ENDPOINT_URL"),
}


class BackupPreflightError(ValueError):
    """Production configuration is missing or invalid, without secret values."""


def _valid_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _valid_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return len(host) <= 253 and all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in host.split(".")
        )


def _valid_url(
    value: str, schemes: frozenset[str], *, database: bool, allow_verify_ca: bool = False
) -> bool:
    try:
        parsed = urlsplit(value)
        # Accessing port validates both its syntax and range.
        port = parsed.port
        return (
            parsed.scheme in schemes
            and _valid_host(parsed.hostname)
            and (port is None or 0 < port <= 65535)
            and not parsed.fragment
            and not any(char.isspace() for char in value)
            and (not database or bool(parsed.path.strip("/")))
            and (
                not database
                or parse_qs(parsed.query, keep_blank_values=True).get("sslmode")
                in ((["verify-full"], ["verify-ca"]) if allow_verify_ca else (["verify-full"],))
            )
            and (database or (not parsed.username and not parsed.password))
        )
    except ValueError:
        return False


def _valid_bucket(value: str, *, b2: bool) -> bool:
    minimum = 6 if b2 else 3
    return minimum <= len(value) <= 63 and bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", value))


def _valid_credential(value: str) -> bool:
    lower = value.lower()
    return (
        _valid_value(value)
        and not any(char.isspace() for char in value)
        and lower not in {"changeme", "example", "dummy", "test", "todo"}
        and not lower.startswith("your-")
    )


def _valid_b2_endpoint(endpoint: str, region: str) -> bool:
    try:
        parts = urlsplit(endpoint)
        match = re.fullmatch(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", parts.hostname or "")
        return bool(
            _valid_url(endpoint, frozenset({"https"}), database=False)
            and not parts.query
            and not parts.fragment
            and parts.path in ("", "/")
            and parts.port in (None, 443)
            and match
            and match.group(1) == region
        )
    except ValueError:
        return False


def _valid_r2_endpoint(endpoint: str, account: str) -> bool:
    try:
        parts = urlsplit(endpoint)
        return bool(
            _valid_url(endpoint, frozenset({"https"}), database=False)
            and not parts.query
            and not parts.fragment
            and not parts.path
            and parts.port in (None, 443)
            and parts.hostname == account + ".r2.cloudflarestorage.com"
        )
    except ValueError:
        return False


def verify_production_backup_preflight(
    env_mapping: Mapping[str, str] | Settings,
    *,
    require_remote_tier: bool = True,
    allow_local_database: bool = False,
    allow_verify_ca: bool = False,
) -> str:
    """Validate configuration only; never claim a remote backup took place.

    A wholly unconfigured environment is allowed only when explicitly opting
    out. Partial configuration is always an error. Secrets are never included
    in errors, including when URL parsing fails.
    """
    if any(
        type(flag) is not bool
        for flag in (require_remote_tier, allow_local_database, allow_verify_ca)
    ):
        raise BackupPreflightError("FAILED_PREFLIGHT_REMOTE_MISSING")
    if isinstance(env_mapping, Settings):
        fields = {
            _DATABASE: env_mapping.database_url,
            _B2[0]: env_mapping.b2_key_id,
            _B2[1]: env_mapping.b2_application_key,
            _B2[2]: env_mapping.b2_bucket,
            _B2[3]: env_mapping.b2_endpoint,
            _B2[4]: env_mapping.b2_region,
            _R2[0]: env_mapping.r2_account_id,
            _R2[1]: env_mapping.r2_access_key_id,
            _R2[2]: env_mapping.r2_secret_access_key,
            _R2[3]: env_mapping.r2_bucket,
        }
        env_mapping = {name: value for name, value in fields.items() if value is not None}
    normalized: dict[str, str] = {}
    for canonical, aliases in _ALIASES.items():
        values = [env_mapping[name] for name in aliases if env_mapping.get(name) not in (None, "")]
        if values:
            if any(not _valid_value(value) for value in values) or any(
                value != values[0] for value in values[1:]
            ):
                raise BackupPreflightError("FAILED_PREFLIGHT_REMOTE_MISSING")
            normalized[canonical] = values[0]
    present = set(normalized)
    if not present and not require_remote_tier:
        return "REMOTE_UNCONFIGURED"
    db = normalized.get(_DATABASE, "")
    b2 = all(name in present for name in _B2)
    r2 = all(name in present for name in _R2)
    # A partially configured tier is suspicious even if the other is complete.
    if (
        (not db and not allow_local_database)
        or (
            bool(db)
            and (
                not _valid_value(db)
                or not _valid_url(
                    db,
                    frozenset(
                        {"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"}
                    ),
                    database=True,
                    allow_verify_ca=allow_verify_ca,
                )
            )
        )
        or not (b2 or r2)
        or (bool(present.intersection(_B2)) and not b2)
        or (bool(present.intersection(_R2)) and not r2)
        or (b2 and not _valid_b2_endpoint(normalized[_B2[3]], normalized[_B2[4]]))
        or (b2 and not _valid_bucket(normalized[_B2[2]], b2=True))
        or (r2 and not _valid_bucket(normalized[_R2[3]], b2=False))
        or (b2 and not all(_valid_credential(normalized[key]) for key in _B2[:2]))
        or (r2 and not all(_valid_credential(normalized[key]) for key in _R2[1:3]))
        or (r2 and not re.fullmatch(r"[a-f0-9]{32}", normalized[_R2[0]]))
        or ("R2_ENDPOINT_URL" in present and not r2)
        or (
            "R2_ENDPOINT_URL" in present
            and not _valid_r2_endpoint(normalized["R2_ENDPOINT_URL"], normalized[_R2[0]])
        )
    ):
        raise BackupPreflightError("FAILED_PREFLIGHT_REMOTE_MISSING")
    return "REMOTE_CONFIGURATION_VALIDATED"


class BackupIntegrityTamperError(ValueError):
    """The local encrypted artifact failed authentication or validation."""


@dataclass(frozen=True)
class BackupDrillEvidence:
    status: str
    source_sha256: str
    ciphertext_sha256: str
    merkle_root: str
    row_counts: Mapping[str, int]
    archive_path: Path
    manifest_path: Path
    restored_path: Path
    key_source: str
    key_fingerprint: str
    retention_receipt: BackupRetentionReceipt | None = None


def _key(key: bytes | bytearray | memoryview) -> bytes:
    """Reject simple patterns; callers must still supply a CSPRNG-generated key.

    Structural tests cannot prove entropy, and hashing cannot create entropy.
    """
    if not isinstance(key, (bytes, bytearray, memoryview)):
        raise ValueError("invalid encryption key")
    raw = bytes(key)
    if (
        len(raw) != 32
        or len(set(raw)) < 16
        or any(raw[:period] * (32 // period) == raw for period in (4, 8, 16))
        or len({(raw[i + 1] - raw[i]) % 256 for i in range(31)}) == 1
    ):
        raise ValueError("invalid encryption key")
    return hashlib.sha256(b"nexus-local-drill-aesgcm-v1\x00" + raw).digest()


_KEY_PROVENANCE_SENTINEL = object()
_KEY_CAPABILITY_AUTHORITY = secrets.token_bytes(32)
_KEY_FINGERPRINT_DOMAIN = b"nexus-drill-key-fingerprint-v1\x00"


@dataclass(frozen=True)
class BackupKeyMaterial:
    """A factory-issued key label, not a security boundary against Python code.

    Environment provenance attests only to where the value was read, not its
    entropy or the trustworthiness of the environment provider.
    """

    key_bytes: bytes = field(repr=False)
    source: str
    fingerprint_sha256: str
    _sentinel: object = field(repr=False, compare=False)
    _attestation: bytes = field(default=b"", repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._sentinel is not _KEY_PROVENANCE_SENTINEL:
            raise ValueError("invalid key provenance capability")
        if type(self.key_bytes) is not bytes or self.source not in (
            "csprng",
            "secret_env",
            "raw_unattested",
        ):
            raise ValueError("invalid key material")
        _key(self.key_bytes)
        if not hmac.compare_digest(self.fingerprint_sha256, _key_fingerprint(self.key_bytes)):
            raise ValueError("invalid key fingerprint")
        expected = _key_attestation(self.key_bytes, self.source)
        if not isinstance(self._attestation, bytes) or not hmac.compare_digest(
            self._attestation, expected
        ):
            raise ValueError("invalid key provenance capability")

    @classmethod
    def generate(cls) -> BackupKeyMaterial:
        key = generate_drill_encryption_key()
        return cls(
            key,
            "csprng",
            _key_fingerprint(key),
            _KEY_PROVENANCE_SENTINEL,
            _key_attestation(key, "csprng"),
        )

    @classmethod
    def from_secret_env(
        cls, env_var: str = "NEXUS_BACKUP_ENCRYPTION_KEY", env: Mapping[str, str] | None = None
    ) -> BackupKeyMaterial:
        value = (os.environ if env is None else env).get(env_var)
        if not isinstance(value, str):
            raise ValueError("missing or invalid backup key")
        try:
            if len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
                raw = bytes.fromhex(value)
            else:
                raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raw = None
        if raw is None:
            # Raising after the handler prevents untrusted decoder inputs from
            # being retained in the new exception's implicit context.
            raise ValueError("missing or invalid backup key")
        return cls(
            raw,
            "secret_env",
            _key_fingerprint(raw),
            _KEY_PROVENANCE_SENTINEL,
            _key_attestation(raw, "secret_env"),
        )

    @classmethod
    def from_raw_bytes(cls, raw_key: bytes, *, source: str = "raw_unattested") -> BackupKeyMaterial:
        if source != "raw_unattested" or type(raw_key) is not bytes:
            raise ValueError("invalid raw key source")
        return cls(
            raw_key,
            source,
            _key_fingerprint(raw_key),
            _KEY_PROVENANCE_SENTINEL,
            _key_attestation(raw_key, source),
        )


def _key_attestation(raw: bytes, source: str) -> bytes:
    """Bind factory labels to key bytes within this interpreter process."""
    return hmac.new(
        _KEY_CAPABILITY_AUTHORITY, source.encode("ascii") + b"\x00" + raw, hashlib.sha256
    ).digest()


def _key_fingerprint(raw: bytes) -> str:
    return hashlib.sha256(_KEY_FINGERPRINT_DOMAIN + raw).hexdigest()


def _material(
    key: BackupKeyMaterial | bytes | bytearray | memoryview, *, require_trusted: bool
) -> BackupKeyMaterial:
    if type(require_trusted) is not bool:
        raise ValueError("require_trusted_key_source must be boolean")
    if type(key) is BackupKeyMaterial:
        BackupKeyMaterial.__post_init__(key)
        result = key
    else:
        if require_trusted or not isinstance(key, (bytes, bytearray, memoryview)):
            raise ValueError("trusted key material required")
        result = BackupKeyMaterial.from_raw_bytes(bytes(key))
    if require_trusted and result.source not in ("csprng", "secret_env"):
        raise ValueError("trusted key material required")
    return result


def generate_drill_encryption_key() -> bytes:
    """Generate a key with the OS CSPRNG, rejecting disallowed patterns."""
    for _ in range(128):
        candidate = secrets.token_bytes(32)
        try:
            _key(candidate)
        except ValueError:
            continue
        return candidate
    raise RuntimeError("could not generate an acceptable drill key")


def _validate_size_limit(limit: int) -> None:
    if type(limit) is not int or not 0 < limit <= ABSOLUTE_MAX_DRILL_DATABASE_BYTES:
        raise ValueError("max_database_bytes must be within (0, ABSOLUTE_MAX_DRILL_DATABASE_BYTES]")


def _read_regular(path: Path, limit: int = ABSOLUTE_MAX_DRILL_DATABASE_BYTES) -> bytes:
    _validate_size_limit(limit)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_size > limit:
            raise ValueError("database size exceeds drill limit")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("database size exceeds drill limit")
        return data
    finally:
        os.close(fd)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _merkle(data: bytes) -> str:
    leaves = [
        hashlib.sha256(b"\x00" + data[i : i + 65536]).digest() for i in range(0, len(data), 65536)
    ]
    if not leaves:
        return hashlib.sha256(b"").hexdigest()

    def root(nodes: list[bytes]) -> bytes:
        if len(nodes) == 1:
            return nodes[0]
        split = 1 << ((len(nodes) - 1).bit_length() - 1)
        return hashlib.sha256(b"\x01" + root(nodes[:split]) + root(nodes[split:])).digest()

    return root(leaves).hex()


def _inventory(conn: sqlite3.Connection) -> dict[str, int]:
    if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    names = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND substr(name, 1, 7) != 'sqlite_' ORDER BY name"
    ).fetchall()
    result = {}
    for (name,) in names:
        quoted = '"' + name.replace('"', '""') + '"'
        result[name] = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]  # nosec B608
    return result


def _exclusive_write(path: Path, data: bytes) -> None:
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    name = ".drill-" + secrets.token_hex(16)
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            os.link(name, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(fd)
            os.unlink(name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def _parse_manifest(raw: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for k, v in items:
            if k in result:
                raise ValueError("duplicate field")
            result[k] = v
        return result

    obj = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(obj, dict):
        raise ValueError("invalid manifest")
    fields = {
        "schema_version",
        "source_sha256",
        "row_counts",
        "nonce_hex",
        "ciphertext_sha256",
        "merkle_root",
        "auth_tag_hex",
    }
    if set(obj) not in (
        fields,
        fields | {"key_source", "key_fingerprint"},
        fields | {"key_source", "key_fingerprint", "created_at_utc"},
    ):
        raise ValueError("invalid manifest")
    return obj


def _verify_encrypted_sqlite_backup(
    archive_path: Path,
    manifest_path: Path,
    key: BackupKeyMaterial | bytes | bytearray | memoryview,
    *,
    max_database_bytes: int = 64 * 1024 * 1024,
    require_trusted_key_source: bool = True,
) -> tuple[bytes, int | None]:
    """Authenticate and return plaintext plus the authenticated UTC timestamp."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _validate_size_limit(max_database_bytes)
    material = _material(key, require_trusted=require_trusted_key_source)
    aes_key = _key(material.key_bytes)
    try:
        manifest = _parse_manifest(_read_regular(manifest_path, 1024 * 1024))
        ciphertext = _read_regular(archive_path, max_database_bytes)
        aad = {
            k: manifest[k] for k in ("schema_version", "source_sha256", "row_counts", "nonce_hex")
        }
        version = manifest["schema_version"]
        if type(version) is not int or version not in (1, 2, 3):
            raise ValueError("invalid version")
        if version in (2, 3):
            aad.update({k: manifest[k] for k in ("key_source", "key_fingerprint")})
            if manifest["key_source"] not in ("csprng", "secret_env", "raw_unattested"):
                raise ValueError("invalid key source")
            if not hmac.compare_digest(
                str(manifest["key_fingerprint"]), material.fingerprint_sha256
            ):
                raise ValueError("key fingerprint mismatch")
        elif "key_source" in manifest or "key_fingerprint" in manifest:
            raise ValueError("invalid legacy manifest")
        created_us: int | None = None
        if version == 3:
            timestamp = manifest.get("created_at_utc")
            if not isinstance(timestamp, str):
                raise ValueError("invalid creation timestamp")
            created = datetime.fromisoformat(timestamp)
            if created.tzinfo is None or created.utcoffset() != timedelta(0):
                raise ValueError("creation timestamp must be UTC")
            created = created.astimezone(timezone.utc)
            delta = created - datetime(1970, 1, 1, tzinfo=timezone.utc)
            created_us = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
            aad["created_at_utc"] = timestamp
        elif "created_at_utc" in manifest:
            raise ValueError("unexpected creation timestamp")
        nonce = bytes.fromhex(str(manifest["nonce_hex"]))
        tag = bytes.fromhex(str(manifest["auth_tag_hex"]))
        if len(nonce) != 12 or len(tag) != 16:
            raise ValueError("invalid nonce or tag")
        if not hmac.compare_digest(
            hashlib.sha256(ciphertext).hexdigest(), str(manifest["ciphertext_sha256"])
        ):
            raise ValueError("ciphertext digest mismatch")
        if not hmac.compare_digest(_merkle(ciphertext), str(manifest["merkle_root"])):
            raise ValueError("merkle root mismatch")
        plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext + tag, _canonical(aad))
        if not hmac.compare_digest(
            hashlib.sha256(plaintext).hexdigest(), str(manifest["source_sha256"])
        ):
            raise ValueError("plaintext digest mismatch")
        return plaintext, created_us
    except (ValueError, TypeError, OSError, InvalidTag, KeyError):
        pass
    # Raise outside the handler: even __context__ must not retain untrusted input.
    raise BackupIntegrityTamperError("encrypted backup authentication failed")


@contextmanager
def backup_reader_lease(generation_path: Path, *, nonblocking: bool = False) -> Iterator[None]:
    """Hold a cooperative reader lock; recheck that the generation was not pruned.

    All readers and pruners must cooperate, and the root must be trusted against
    non-cooperating pathname replacement. The lock is held until the reader exits.
    """
    generation = Path(generation_path).absolute()
    directory = os.open(generation, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("backup generation is not a directory")
        fd = os.open(
            ".reader.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        try:
            lock_info = os.fstat(fd)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                raise PermissionError("unsafe backup reader lock")
            fcntl.flock(fd, fcntl.LOCK_SH | (fcntl.LOCK_NB if nonblocking else 0))
            current = os.stat(generation, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise FileNotFoundError("backup generation was replaced")
            yield
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def verify_encrypted_sqlite_backup(
    archive_path: Path,
    manifest_path: Path,
    key: BackupKeyMaterial | bytes | bytearray | memoryview,
    *,
    max_database_bytes: int = 64 * 1024 * 1024,
    require_trusted_key_source: bool = True,
) -> bytes:
    """Verify a generation while participating in its cooperative reader lease."""
    archive, manifest = Path(archive_path), Path(manifest_path)
    if archive.parent == manifest.parent and archive.parent.name.startswith("drill-"):
        with backup_reader_lease(archive.parent):
            plaintext, _ = _verify_encrypted_sqlite_backup(
                archive,
                manifest,
                key,
                max_database_bytes=max_database_bytes,
                require_trusted_key_source=require_trusted_key_source,
            )
            return plaintext
    plaintext, _ = _verify_encrypted_sqlite_backup(
        archive,
        manifest,
        key,
        max_database_bytes=max_database_bytes,
        require_trusted_key_source=require_trusted_key_source,
    )
    return plaintext


@dataclass(frozen=True)
class BackupRetentionReceipt:
    retained_ids: tuple[str, ...]
    pruned_ids: tuple[str, ...]
    skipped_corrupt_ids: tuple[str, ...]
    skipped_pinned_ids: tuple[str, ...]
    skipped_leased_ids: tuple[str, ...] = ()


def _validate_generation_entries(directory_fd: int) -> None:
    allowed = {"backup.enc", "manifest.json", "restored.sqlite", ".reader.lock", ".pin"}
    entries = set(os.listdir(directory_fd))
    if not entries <= allowed:
        raise ValueError("unexpected generation contents")
    for entry in entries:
        if not stat.S_ISREG(os.stat(entry, dir_fd=directory_fd, follow_symlinks=False).st_mode):
            raise ValueError("unsafe generation contents")


def enforce_backup_retention(
    backup_root: Path,
    *,
    keep_generations: int,
    key_material: BackupKeyMaterial | bytes,
    active_pins: Set[str] = frozenset(),
    active_reader_leases: Set[str] = frozenset(),
    require_trusted_key_source: bool = True,
) -> BackupRetentionReceipt:
    """Authenticate first, then prune superseded cooperating local generations.

    V3 creation time is authenticated; legacy generations use archive mtime.
    Ties use generation ID. Roots must be trusted against path swaps.
    Readers not using the lease and changing external pin sets cannot be fenced.
    """
    if type(keep_generations) is not int or keep_generations < 1:
        raise ValueError("keep_generations must be >= 1")
    material = _material(key_material, require_trusted=require_trusted_key_source)
    if not isinstance(active_pins, Set) or not isinstance(active_reader_leases, Set):
        raise ValueError("pins and reader leases must be sets")
    pins, readers = frozenset(active_pins), frozenset(active_reader_leases)
    if any(type(item) is not str for item in pins | readers):
        raise ValueError("pins and reader leases must contain strings")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("safe descriptor-relative retention is unavailable")
    root = Path(backup_root).absolute()
    _check_trusted_directory(root, forbidden_mode=stat.S_IWGRP | stat.S_IWOTH)
    root_fd = os.open(root, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        lock_fd = os.open(
            ".backup-retention.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=root_fd,
        )
        try:
            lock_info = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                raise PermissionError("unsafe backup retention lock")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            valid: list[tuple[int, str]] = []
            corrupt: list[str] = []
            leased: list[str] = []
            for name in sorted(os.listdir(root_fd)):
                if not name.startswith("drill-"):
                    continue
                generation = root / name
                try:
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        raise ValueError("invalid generation")
                    with backup_reader_lease(generation, nonblocking=True):
                        gen_fd = os.open(
                            name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                            dir_fd=root_fd,
                        )
                        try:
                            _validate_generation_entries(gen_fd)
                            _, created_us = _verify_encrypted_sqlite_backup(
                                generation / "backup.enc",
                                generation / "manifest.json",
                                material,
                                require_trusted_key_source=require_trusted_key_source,
                            )
                            archive_info = os.stat(
                                "backup.enc", dir_fd=gen_fd, follow_symlinks=False
                            )
                        finally:
                            os.close(gen_fd)
                    if not stat.S_ISREG(archive_info.st_mode):
                        raise ValueError("invalid archive")
                    valid.append(
                        (
                            created_us * 1000
                            if created_us is not None
                            else archive_info.st_mtime_ns,
                            name,
                        )
                    )
                except BlockingIOError:
                    leased.append(name)
                except (OSError, ValueError):
                    corrupt.append(name)
            valid.sort(reverse=True)
            retained = [name for _, name in valid[:keep_generations]]
            pruned: list[str] = []
            pinned: list[str] = []
            for _, name in valid[keep_generations:]:
                if name in pins:
                    pinned.append(name)
                    continue
                if name in readers:
                    leased.append(name)
                    continue
                generation = root / name
                moved = False
                try:
                    gen_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                        dir_fd=root_fd,
                    )
                    try:
                        info = os.fstat(gen_fd)
                        try:
                            os.stat(".pin", dir_fd=gen_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            pinned.append(name)
                            continue
                        reader_fd = os.open(
                            ".reader.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=gen_fd
                        )
                        try:
                            try:
                                fcntl.flock(reader_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                leased.append(name)
                                continue
                            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                                raise ValueError("generation changed")
                            _validate_generation_entries(gen_fd)
                            _verify_encrypted_sqlite_backup(
                                generation / "backup.enc",
                                generation / "manifest.json",
                                material,
                                require_trusted_key_source=require_trusted_key_source,
                            )
                            quarantine = ".pruned-" + secrets.token_hex(16)
                            os.rename(name, quarantine, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                            moved = True
                            os.fsync(root_fd)
                            shutil.rmtree(quarantine, dir_fd=root_fd)
                            os.fsync(root_fd)
                            pruned.append(name)
                        finally:
                            os.close(reader_fd)
                    finally:
                        os.close(gen_fd)
                except (OSError, ValueError):
                    if moved:
                        raise
                    corrupt.append(name)
            return BackupRetentionReceipt(
                tuple(retained), tuple(pruned), tuple(corrupt), tuple(pinned), tuple(leased)
            )
        finally:
            os.close(lock_fd)
    finally:
        os.close(root_fd)


def _check_trusted_directory(path: Path, *, forbidden_mode: int) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
            or info.st_mode & forbidden_mode
        ):
            raise PermissionError("untrusted drill directory")
    finally:
        os.close(fd)


def _check_source_identity(source: Path, info: os.stat_result) -> None:
    try:
        current = os.stat(source, follow_symlinks=False)
    except FileNotFoundError:
        raise ValueError("source database inode changed during drill") from None
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
        info.st_dev,
        info.st_ino,
    ):
        raise ValueError("source database inode changed during drill")


def run_encrypted_sqlite_backup_restore_drill(
    source_db_path: Path,
    drill_workdir: Path,
    encryption_key: BackupKeyMaterial | bytes | bytearray | memoryview,
    *,
    inject_tamper_byte: bool = False,
    max_database_bytes: int = 64 * 1024 * 1024,
    backup_timeout_seconds: float = 10.0,
    allow_empty_database: bool = False,
    preflight_env: Mapping[str, str] | None = None,
    require_trusted_key_source: bool = True,
    retain_generations: int | None = None,
) -> BackupDrillEvidence:
    """Execute a local SQLite backup and restore, never a production remote backup.

    The source directory and work directory must be controlled by the caller:
    Python's SQLite API opens paths independently of our no-follow descriptor.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _validate_size_limit(max_database_bytes)
    if retain_generations is not None and (
        type(retain_generations) is not int or retain_generations < 1
    ):
        raise ValueError("keep_generations must be >= 1")
    material = _material(encryption_key, require_trusted=require_trusted_key_source)
    aes_key = _key(material.key_bytes)
    if (
        isinstance(backup_timeout_seconds, bool)
        or not isinstance(backup_timeout_seconds, (int, float))
        or not math.isfinite(backup_timeout_seconds)
        or backup_timeout_seconds <= 0
    ):
        raise ValueError("invalid backup timeout")
    if not isinstance(inject_tamper_byte, bool) or not isinstance(allow_empty_database, bool):
        raise ValueError("drill flags must be boolean")
    if preflight_env is not None:
        verify_production_backup_preflight(preflight_env)
        status = "LOCAL_DRILL_VERIFIED_REMOTE_PREFLIGHT_OK"
    else:
        status = "LOCAL_DRILL_VERIFIED_REMOTE_NOT_CHECKED"
    source = Path(source_db_path).absolute()
    work = Path(drill_workdir).absolute()
    _check_trusted_directory(source.parent, forbidden_mode=stat.S_IWOTH)
    _check_trusted_directory(work, forbidden_mode=stat.S_IWGRP | stat.S_IWOTH)
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise ValueError("source is not a regular file")
        if source_info.st_size > max_database_bytes:
            raise ValueError("database size exceeds drill limit")
        _check_source_identity(source, source_info)
        deadline = time.monotonic() + backup_timeout_seconds

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() > deadline:
                raise TimeoutError("sqlite backup timed out")

        with tempfile.TemporaryDirectory(prefix=".nexus-drill-", dir=work) as temp:
            snapshot = Path(temp) / "snapshot.sqlite"
            with closing(
                sqlite3.connect(
                    source.as_uri() + "?mode=ro", uri=True, timeout=backup_timeout_seconds
                )
            ) as src:
                with closing(sqlite3.connect(snapshot, timeout=backup_timeout_seconds)) as dst:
                    src.backup(dst, pages=128, progress=progress, sleep=0.005)
                    _check_source_identity(source, source_info)
                    counts = _inventory(dst)
                    if not counts and not allow_empty_database:
                        raise ValueError("source database has no user tables")
            plaintext = _read_regular(snapshot, max_database_bytes)
            source_hash = hashlib.sha256(plaintext).hexdigest()
            nonce = os.urandom(12)
            aad = {
                "schema_version": 3,
                "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "key_source": material.source,
                "key_fingerprint": material.fingerprint_sha256,
                "source_sha256": source_hash,
                "row_counts": counts,
                "nonce_hex": nonce.hex(),
            }
            encrypted = AESGCM(aes_key).encrypt(nonce, plaintext, _canonical(aad))
            ciphertext, tag = encrypted[:-16], encrypted[-16:]
            manifest = {
                **aad,
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
                "merkle_root": _merkle(ciphertext),
                "auth_tag_hex": tag.hex(),
            }
            stage = Path(temp)
            archive = stage / "backup.enc"
            manifest_path = stage / "manifest.json"
            restored = stage / "restored.sqlite"
            _exclusive_write(archive, ciphertext)
            _exclusive_write(manifest_path, _canonical(manifest))
            if inject_tamper_byte:
                fd = os.open(archive, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise BackupIntegrityTamperError("invalid archive")
                    first = os.read(fd, 1)
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, bytes([first[0] ^ 1]))
                finally:
                    os.close(fd)
            verified = verify_encrypted_sqlite_backup(
                archive,
                manifest_path,
                material,
                max_database_bytes=max_database_bytes,
                require_trusted_key_source=require_trusted_key_source,
            )
            _exclusive_write(restored, verified)
            with closing(sqlite3.connect(restored.as_uri() + "?mode=ro", uri=True)) as connection:
                if _inventory(connection) != counts:
                    raise BackupIntegrityTamperError("restored database inventory mismatch")
            snapshot.unlink()
            stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(stage_fd)
            finally:
                os.close(stage_fd)
            final = work / ("drill-" + secrets.token_hex(16))
            # A unique generation is made visible by one directory rename, not
            # three independent file replacements. The work directory is trusted.
            os.rename(stage, final)
            work_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(work_fd)
            finally:
                os.close(work_fd)
            retention_receipt = (
                enforce_backup_retention(
                    work,
                    keep_generations=retain_generations,
                    key_material=material,
                    active_pins=frozenset({final.name}),
                    require_trusted_key_source=require_trusted_key_source,
                )
                if retain_generations is not None
                else None
            )
            return BackupDrillEvidence(
                status,
                source_hash,
                str(manifest["ciphertext_sha256"]),
                str(manifest["merkle_root"]),
                MappingProxyType(dict(counts)),
                final / archive.name,
                final / manifest_path.name,
                final / restored.name,
                material.source,
                material.fingerprint_sha256,
                retention_receipt,
            )
    finally:
        os.close(source_fd)
