"""Production configuration preflight and an isolated local SQLite drill.

A local drill is not a remote backup. SQLite source and work directories must
be trusted against pathname replacement while the SQLite library opens files.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import tempfile
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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


class BackupPreflightError(ValueError):
    """Production configuration is missing or invalid, without secret values."""


def _valid_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _valid_url(value: str, schemes: frozenset[str], *, database: bool) -> bool:
    try:
        parsed = urlsplit(value)
        # Accessing port validates both its syntax and range.
        port = parsed.port
        return (
            parsed.scheme in schemes
            and bool(parsed.hostname)
            and (port is None or 0 < port <= 65535)
            and not parsed.fragment
            and not any(char.isspace() for char in value)
            and (not database or bool(parsed.path.strip("/")))
            and (database or (not parsed.username and not parsed.password))
        )
    except ValueError:
        return False


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
    names = (_DATABASE,) + _B2 + _R2
    present = {name for name in names if env_mapping.get(name) not in (None, "")}
    if not present and not require_remote_tier:
        return "REMOTE_UNCONFIGURED"
    if any(not _valid_value(env_mapping.get(name)) for name in present):
        raise BackupPreflightError("FAILED_PREFLIGHT_REMOTE_MISSING")
    db = env_mapping.get(_DATABASE, "")
    b2 = all(name in present for name in _B2)
    r2 = all(name in present for name in _R2)
    # A partially configured tier is suspicious even if the other is complete.
    if (
        not _valid_value(db)
        or not _valid_url(
            db, frozenset({"postgres", "postgresql", "postgresql+asyncpg"}), database=True
        )
        or not (b2 or r2)
        or (bool(present.intersection(_B2)) and not b2)
        or (bool(present.intersection(_R2)) and not r2)
        or (b2 and not _valid_url(env_mapping[_B2[3]], frozenset({"https"}), database=False))
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
    row_counts: dict[str, int]
    archive_path: Path
    manifest_path: Path
    restored_path: Path


def _key(key: bytes | bytearray) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < 32 or len(set(key)) < 4:
        raise ValueError("invalid encryption key")
    return hashlib.sha256(b"nexus-local-drill-aesgcm-v1\x00" + bytes(key)).digest()


def _read_regular(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
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
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
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
    archive_path: Path, manifest_path: Path, key: bytes | bytearray
) -> bytes:
    """Authenticate an artifact before returning any plaintext; never restore on failure."""
    aes_key = _key(key)
    try:
        manifest = _parse_manifest(_read_regular(manifest_path))
        ciphertext = _read_regular(archive_path)
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
    encryption_key: bytes | bytearray,
    *,
    inject_tamper_byte: bool = False,
) -> BackupDrillEvidence:
    """Execute a local SQLite backup and restore, never a production remote backup.

    The source directory and work directory must be controlled by the caller:
    Python's SQLite API opens paths independently of our no-follow descriptor.
    """
    aes_key = _key(encryption_key)
    if not isinstance(inject_tamper_byte, bool):
        raise ValueError("tamper flag must be boolean")
    source = Path(source_db_path).absolute()
    work = Path(drill_workdir).absolute()
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise ValueError("source is not a regular file")
        with tempfile.TemporaryDirectory(prefix=".nexus-drill-", dir=work) as temp:
            snapshot = Path(temp) / "snapshot.sqlite"
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(snapshot)) as dst:
                    src.backup(dst)
                    counts = _inventory(dst)
            plaintext = _read_regular(snapshot)
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
            archive = work / "backup.enc"
            manifest_path = work / "manifest.json"
            restored = work / "restored.sqlite"
            _exclusive_write(archive, ciphertext)
            try:
                _exclusive_write(manifest_path, _canonical(manifest))
            except Exception:
                archive.unlink()
                raise
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
            verified = verify_encrypted_sqlite_backup(archive, manifest_path, encryption_key)
            _exclusive_write(restored, verified)
            with closing(sqlite3.connect(restored.as_uri() + "?mode=ro", uri=True)) as connection:
                if _inventory(connection) != counts:
                    raise BackupIntegrityTamperError("restored database inventory mismatch")
            return BackupDrillEvidence(
                "LOCAL_DRILL_VERIFIED_REMOTE_UNCONFIGURED",
                source_hash,
                str(manifest["ciphertext_sha256"]),
                str(manifest["merkle_root"]),
                counts,
                archive,
                manifest_path,
                restored,
            )
    finally:
        os.close(source_fd)
