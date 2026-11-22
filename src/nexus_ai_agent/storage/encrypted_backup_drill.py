"""Production configuration preflight and an isolated local SQLite drill.

A local drill is not a remote backup. SQLite source and work directories must
be trusted against pathname replacement while the SQLite library opens files.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_B2 = (
    "NEXUS_B2_KEY_ID",
    "NEXUS_B2_APPLICATION_KEY",
    "NEXUS_B2_BUCKET",
    "NEXUS_B2_ENDPOINT",
    "NEXUS_B2_REGION",
)
_R2 = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
_DATABASE = "NEXUS_DATABASE_URL"
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


def _valid_url(value: str, schemes: frozenset[str], *, database: bool) -> bool:
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
                in (["verify-full"], ["verify-ca"])
            )
            and (database or (not parsed.username and not parsed.password))
        )
    except ValueError:
        return False


def _valid_bucket(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value)) and ".." not in value


def verify_production_backup_preflight(
    env_mapping: Mapping[str, str], *, require_remote_tier: bool = True
) -> str:
    """Validate configuration only; never claim a remote backup took place.

    A wholly unconfigured environment is allowed only when explicitly opting
    out. Partial configuration is always an error. Secrets are never included
    in errors, including when URL parsing fails.
    """
    if not isinstance(require_remote_tier, bool):
        raise BackupPreflightError("FAILED_PREFLIGHT_REMOTE_MISSING")
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
        not _valid_value(db)
        or not _valid_url(
            db,
            frozenset({"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"}),
            database=True,
        )
        or not (b2 or r2)
        or (bool(present.intersection(_B2)) and not b2)
        or (bool(present.intersection(_R2)) and not r2)
        or (b2 and not _valid_url(normalized[_B2[3]], frozenset({"https"}), database=False))
        or (b2 and not _valid_bucket(normalized[_B2[2]]))
        or (r2 and not _valid_bucket(normalized[_R2[3]]))
        or (b2 and any(char.isspace() for char in normalized[_B2[4]]))
        or (r2 and any(char.isspace() for char in normalized[_R2[0]]))
        or ("R2_ENDPOINT_URL" in present and not r2)
        or (
            "R2_ENDPOINT_URL" in present
            and not _valid_url(normalized["R2_ENDPOINT_URL"], frozenset({"https"}), database=False)
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


def _read_regular(path: Path, limit: int = 64 * 1024 * 1024) -> bytes:
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
    if not isinstance(obj, dict) or set(obj) != {
        "schema_version",
        "source_sha256",
        "row_counts",
        "nonce_hex",
        "ciphertext_sha256",
        "merkle_root",
        "auth_tag_hex",
    }:
        raise ValueError("invalid manifest")
    return obj


def verify_encrypted_sqlite_backup(
    archive_path: Path,
    manifest_path: Path,
    key: bytes | bytearray | memoryview,
    *,
    max_database_bytes: int = 64 * 1024 * 1024,
) -> bytes:
    """Authenticate an artifact before returning any plaintext; never restore on failure."""
    aes_key = _key(key)
    if type(max_database_bytes) is not int or max_database_bytes <= 0:
        raise ValueError("database size exceeds drill limit")
    try:
        manifest = _parse_manifest(_read_regular(manifest_path, 1024 * 1024))
        ciphertext = _read_regular(archive_path, max_database_bytes)
        aad = {
            k: manifest[k] for k in ("schema_version", "source_sha256", "row_counts", "nonce_hex")
        }
        if manifest["schema_version"] != 1 or type(manifest["schema_version"]) is not int:
            raise ValueError("invalid version")
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
        return plaintext
    except (ValueError, TypeError, OSError, InvalidTag, KeyError) as exc:
        raise BackupIntegrityTamperError("encrypted backup authentication failed") from exc


def run_encrypted_sqlite_backup_restore_drill(
    source_db_path: Path,
    drill_workdir: Path,
    encryption_key: bytes | bytearray | memoryview,
    *,
    inject_tamper_byte: bool = False,
    max_database_bytes: int = 64 * 1024 * 1024,
    backup_timeout_seconds: float = 10.0,
    allow_empty_database: bool = False,
    preflight_env: Mapping[str, str] | None = None,
) -> BackupDrillEvidence:
    """Execute a local SQLite backup and restore, never a production remote backup.

    The source directory and work directory must be controlled by the caller:
    Python's SQLite API opens paths independently of our no-follow descriptor.
    """
    aes_key = _key(encryption_key)
    if type(max_database_bytes) is not int or max_database_bytes <= 0:
        raise ValueError("database size exceeds drill limit")
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
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise ValueError("source is not a regular file")
        if source_info.st_size > max_database_bytes:
            raise ValueError("database size exceeds drill limit")
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
                    counts = _inventory(dst)
                    if not counts and not allow_empty_database:
                        raise ValueError("source database has no user tables")
            plaintext = _read_regular(snapshot, max_database_bytes)
            source_hash = hashlib.sha256(plaintext).hexdigest()
            nonce = os.urandom(12)
            aad = {
                "schema_version": 1,
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
                archive, manifest_path, encryption_key, max_database_bytes=max_database_bytes
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
            return BackupDrillEvidence(
                status,
                source_hash,
                str(manifest["ciphertext_sha256"]),
                str(manifest["merkle_root"]),
                MappingProxyType(dict(counts)),
                final / archive.name,
                final / manifest_path.name,
                final / restored.name,
            )
    finally:
        os.close(source_fd)
