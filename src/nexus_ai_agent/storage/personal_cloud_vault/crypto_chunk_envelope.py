"""Client-side chunking, Encrypt-then-MAC envelope, and RFC 6962 Merkle manifests.

All primitives in this module rely exclusively on the Python standard library:

* **Key derivation**: ``PBKDF2-HMAC-SHA256`` (default ``iterations=600_000`` per
  OWASP 2026 guidance, minimum enforced ``MIN_PBKDF2_ITERATIONS=100_000``,
  ``dklen=64``) followed by RFC 5869 ``HKDF-SHA256`` domain separation for
  ``K_enc``, ``K_mac``, convergent ``K_chunk_id``, and non-secret ``key_id``.
* **Encrypt-then-MAC (EtM)**: SHA-256 CTR stream cipher
  (``SHA256(K_enc || nonce || counter_be64)``) paired with ``HMAC-SHA256`` over
  ``(header || nonce || ciphertext || chunk_index_be64 || plaintext_len_be64)``
  verified in constant time via :func:`hmac.compare_digest`.
* **RFC 6962 Merkle tree**: domain-separated leaf prefix ``0x00``, interior node
  prefix ``0x01`` without odd-node duplication (immune to CVE-2012-2459), and
  length-bound root envelope ``SHA256(0x02 || total_leaves_be64 || raw_root)``.

Threat Model
------------
* **Untrusted storage provider**: Cloud buckets and portable media are treated as
  adversarial (read/modify/reorder/replay/truncate). Confidentiality is provided by
  128-bit random-nonce SHA-256-CTR; integrity and ordering are enforced fail-closed
  via per-chunk EtM HMAC-SHA256, per-file HMAC-SHA256, and signed RFC 6962 Merkle
  snapshot manifests before any plaintext is emitted to disk.
* **Cross-user deduplication leakage**: ``chunk_id`` is keyed with ``K_chunk_id``
  (HMAC-SHA256), preventing cross-vault confirmation-of-file attacks while
  preserving intra-vault deduplication across edits.
* **Ciphertext length leakage**: Stream ciphers preserve plaintext length by
  default (``pad_to_block_size=1``). Callers requiring length-hiding against size
  fingerprinting may set ``pad_to_block_size`` (e.g. ``4096`` or ``65536``) to
  round ``len(ciphertext)`` up to a block multiple while preserving the exact
  authenticated ``plaintext_len``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import posixpath
import secrets
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from nexus_ai_agent.storage.personal_cloud_vault.errors import (
    PersonalVaultError,
    VaultAuthenticationError,
    VaultTamperDetectedError,
    VaultTransportError,
)

MIN_CHUNK_SIZE = 1
DEFAULT_CHUNK_SIZE = 1_048_576  # 1 MiB
MAX_DEFAULT_CHUNK_SIZE = 4_194_304  # 4 MiB
MAX_CHUNK_SIZE = 16_777_216  # 16 MiB

MIN_PBKDF2_ITERATIONS = 100_000
PBKDF2_ITERATIONS = 600_000  # OWASP recommendation for PBKDF2-HMAC-SHA256
PBKDF2_DKLEN = 64
MIN_SALT_BYTES = 16
DEFAULT_SALT_BYTES = 32

NONCE_BYTES = 16
KEY_ID_BYTES = 16
MAC_TAG_BYTES = 32
SHA256_BLOCK_BYTES = 32

ENVELOPE_MAGIC = b"NPCV"
ENVELOPE_VERSION = 1
_FLAG_NONE = 0x00
_FLAG_PADDED = 0x01
_WIRE_FIXED_STRUCT = struct.Struct(">H")
_WIRE_META_STRUCT = struct.Struct(">16s16sQQ32s")
_WIRE_MAC_STRUCT = struct.Struct(">32s")


def validate_chunk_size(chunk_size: int) -> int:
    """Validate ``chunk_size`` bounds and return it."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
        raise PersonalVaultError("chunk_size must be an integer")
    if chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
        raise PersonalVaultError(
            f"chunk_size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE} bytes, "
            f"got {chunk_size}"
        )
    return chunk_size


def validate_pad_block_size(pad_to_block_size: int) -> int:
    """Validate ``pad_to_block_size`` bounds (``1`` = unpadded stream length)."""
    if isinstance(pad_to_block_size, bool) or not isinstance(pad_to_block_size, int):
        raise PersonalVaultError("pad_to_block_size must be an integer")
    if pad_to_block_size < 1 or pad_to_block_size > MAX_CHUNK_SIZE:
        raise PersonalVaultError(
            f"pad_to_block_size must be between 1 and {MAX_CHUNK_SIZE} bytes, "
            f"got {pad_to_block_size}"
        )
    return pad_to_block_size


def normalize_relative_vault_path(rel_path: str) -> str:
    """Validate and normalize a relative POSIX file path inside a snapshot."""
    if not rel_path or "\x00" in rel_path or "\\" in rel_path:
        raise PersonalVaultError(f"Invalid relative vault path: {rel_path!r}")
    if rel_path.startswith("/"):
        raise PersonalVaultError(f"Absolute vault path is forbidden: {rel_path!r}")
    parts = rel_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PersonalVaultError(f"Non-canonical or traversal vault path: {rel_path!r}")
    normalized = posixpath.normpath(rel_path)
    return normalized


def generate_vault_salt(num_bytes: int = DEFAULT_SALT_BYTES) -> bytes:
    """Generate a cryptographically random PBKDF2 salt."""
    if num_bytes < MIN_SALT_BYTES:
        raise VaultAuthenticationError(
            f"Vault salt must be at least {MIN_SALT_BYTES} bytes, got {num_bytes}"
        )
    return secrets.token_bytes(num_bytes)


def hkdf_extract_sha256(salt: bytes, ikm: bytes) -> bytes:
    """RFC 5869 HKDF-Extract using HMAC-SHA256."""
    effective_salt = salt if salt else (b"\x00" * SHA256_BLOCK_BYTES)
    return hmac.new(effective_salt, ikm, hashlib.sha256).digest()


def hkdf_expand_sha256(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-Expand using HMAC-SHA256."""
    if len(prk) < SHA256_BLOCK_BYTES:
        raise VaultAuthenticationError("HKDF PRK must be at least 32 bytes")
    if length <= 0 or length > 255 * SHA256_BLOCK_BYTES:
        raise PersonalVaultError(f"Invalid HKDF output length: {length}")
    blocks: list[bytes] = []
    previous = b""
    produced = 0
    counter = 1
    while produced < length:
        previous = hmac.new(prk, previous + info + bytes([counter]), hashlib.sha256).digest()
        blocks.append(previous)
        produced += len(previous)
        counter += 1
    return b"".join(blocks)[:length]


@dataclass(frozen=True)
class VaultKeyMaterial:
    """Domain-separated encryption and authentication keys derived from a master secret."""

    enc_key: bytes = field(repr=False)
    mac_key: bytes = field(repr=False)
    chunk_id_key: bytes = field(repr=False)
    key_id: bytes
    salt: bytes
    iterations: int = PBKDF2_ITERATIONS


def derive_vault_keys(
    master_secret: str | bytes,
    salt: bytes,
    *,
    iterations: int = PBKDF2_ITERATIONS,
) -> VaultKeyMaterial:
    """Derive separated ``K_enc`` and ``K_mac`` keys via PBKDF2-HMAC-SHA256 + HKDF-SHA256."""
    secret_bytes = (
        master_secret.encode("utf-8") if isinstance(master_secret, str) else bytes(master_secret)
    )
    if not secret_bytes:
        raise VaultAuthenticationError("master_secret must not be empty")
    if len(salt) < MIN_SALT_BYTES:
        raise VaultAuthenticationError(
            f"salt must be at least {MIN_SALT_BYTES} bytes, got {len(salt)}"
        )
    if iterations < MIN_PBKDF2_ITERATIONS:
        raise VaultAuthenticationError(
            f"PBKDF2 iterations must be at least {MIN_PBKDF2_ITERATIONS}, got {iterations}"
        )

    ikm = hashlib.pbkdf2_hmac(
        "sha256",
        secret_bytes,
        salt,
        iterations=iterations,
        dklen=PBKDF2_DKLEN,
    )
    prk = hkdf_extract_sha256(salt, ikm)
    enc_key = hkdf_expand_sha256(prk, b"nagar-pcv-v1/K_enc", 32)
    mac_key = hkdf_expand_sha256(prk, b"nagar-pcv-v1/K_mac", 32)
    chunk_id_key = hkdf_expand_sha256(prk, b"nagar-pcv-v1/K_chunk_id", 32)
    key_id = hkdf_expand_sha256(prk, b"nagar-pcv-v1/key_id", KEY_ID_BYTES)
    return VaultKeyMaterial(
        enc_key=enc_key,
        mac_key=mac_key,
        chunk_id_key=chunk_id_key,
        key_id=key_id,
        salt=bytes(salt),
        iterations=iterations,
    )


def compute_chunk_id(
    plaintext: bytes | bytearray | memoryview,
    key_material: VaultKeyMaterial,
) -> str:
    """Compute zero-knowledge convergent content-addressed chunk ID (64 hex chars)."""
    mac = hmac.new(key_material.chunk_id_key, b"nagar-pcv-chunk-id-v1|", hashlib.sha256)
    mac.update(plaintext)
    return mac.hexdigest()


def init_streaming_file_mac(
    rel_path: str,
    total_size: int,
    key_material: VaultKeyMaterial,
) -> hmac.HMAC:
    """Initialize a streaming HMAC-SHA256 context matching :func:`compute_file_mac`."""
    normalized = normalize_relative_vault_path(rel_path)
    if total_size < 0:
        raise PersonalVaultError(f"total_size must be non-negative, got {total_size}")
    mac = hmac.new(key_material.mac_key, b"nagar-pcv-file-v1|", hashlib.sha256)
    path_bytes = normalized.encode("utf-8")
    mac.update(struct.pack(">I", len(path_bytes)))
    mac.update(path_bytes)
    mac.update(struct.pack(">Q", total_size))
    return mac


def compute_file_mac(rel_path: str, file_bytes: bytes, key_material: VaultKeyMaterial) -> str:
    """Compute keyed HMAC-SHA256 over a file's normalized path and full plaintext."""
    mac = init_streaming_file_mac(rel_path, len(file_bytes), key_material)
    mac.update(file_bytes)
    return mac.hexdigest()


def split_into_chunks(data: bytes, chunk_size: int = DEFAULT_CHUNK_SIZE) -> tuple[bytes, ...]:
    """Split ``data`` into sequential content chunks of at most ``chunk_size`` bytes."""
    validate_chunk_size(chunk_size)
    if not data:
        return ()
    return tuple(data[offset : offset + chunk_size] for offset in range(0, len(data), chunk_size))


_CTR_STRIDE_BLOCKS = 128
_CTR_STRIDE_BYTES = _CTR_STRIDE_BLOCKS * SHA256_BLOCK_BYTES  # 4 KiB scratch buffer


def sha256_ctr_crypt(enc_key: bytes, nonce: bytes, payload: bytes) -> bytes:
    """Encrypt or decrypt ``payload`` using SHA-256 CTR mode with O(1) scratch memory."""
    if len(enc_key) != 32:
        raise VaultAuthenticationError("enc_key must be 32 bytes")
    if len(nonce) != NONCE_BYTES:
        raise VaultTamperDetectedError(f"nonce must be {NONCE_BYTES} bytes, got {len(nonce)}")
    if not payload:
        return b""

    total_len = len(payload)
    base_hasher = hashlib.sha256(enc_key + nonce)
    pack_u64 = struct.Struct(">Q").pack
    out = bytearray(total_len)
    view = memoryview(payload)
    counter = 0

    for offset in range(0, total_len, _CTR_STRIDE_BYTES):
        slice_view = view[offset : offset + _CTR_STRIDE_BYTES]
        slice_len = len(slice_view)
        blocks_needed = (slice_len + SHA256_BLOCK_BYTES - 1) // SHA256_BLOCK_BYTES
        ks_parts: list[bytes] = []
        for _ in range(blocks_needed):
            block_hasher = base_hasher.copy()
            block_hasher.update(pack_u64(counter))
            ks_parts.append(block_hasher.digest())
            counter += 1
        ks = b"".join(ks_parts)[:slice_len]
        xored = int.from_bytes(slice_view, "big") ^ int.from_bytes(ks, "big")
        out[offset : offset + slice_len] = xored.to_bytes(slice_len, "big")

    return bytes(out)


def sha256_ctr_crypt_inplace(
    enc_key: bytes,
    nonce: bytes,
    buffer: bytearray,
    *,
    start: int = 0,
) -> None:
    """XOR the SHA-256 CTR keystream over ``buffer[start:]`` in place.

    The counter always restarts at zero for the given slice, so encryption and
    decryption must use the same ``start``. Only a 4 KiB keystream scratch is
    allocated: no payload-sized output copy is ever created.
    """
    if len(enc_key) != 32:
        raise VaultAuthenticationError("enc_key must be 32 bytes")
    if len(nonce) != NONCE_BYTES:
        raise VaultTamperDetectedError(f"nonce must be {NONCE_BYTES} bytes, got {len(nonce)}")
    total_len = len(buffer) - start
    if total_len <= 0:
        return
    base_hasher = hashlib.sha256(enc_key + nonce)
    pack_u64 = struct.Struct(">Q").pack
    view = memoryview(buffer)
    counter = 0
    try:
        for offset in range(0, total_len, _CTR_STRIDE_BYTES):
            slice_start = start + offset
            slice_view = view[slice_start : slice_start + _CTR_STRIDE_BYTES]
            slice_len = len(slice_view)
            blocks_needed = (slice_len + SHA256_BLOCK_BYTES - 1) // SHA256_BLOCK_BYTES
            ks_parts: list[bytes] = []
            for _ in range(blocks_needed):
                block_hasher = base_hasher.copy()
                block_hasher.update(pack_u64(counter))
                ks_parts.append(block_hasher.digest())
                counter += 1
            ks = b"".join(ks_parts)[:slice_len]
            xored = int.from_bytes(slice_view, "big") ^ int.from_bytes(ks, "big")
            slice_view[:] = xored.to_bytes(slice_len, "big")
    finally:
        view.release()


def _build_envelope_header(chunk_id: str, *, flags: int = _FLAG_NONE) -> bytes:
    """Build deterministic authenticated header binding protocol magic, version, and chunk ID."""
    return ENVELOPE_MAGIC + bytes([ENVELOPE_VERSION, flags]) + bytes.fromhex(chunk_id)


def _compute_etm_mac(
    mac_key: bytes,
    *,
    header: bytes,
    nonce: bytes,
    ciphertext: bytes | bytearray | memoryview,
    chunk_index: int,
    plaintext_len: int,
) -> bytes:
    """Compute ``HMAC-SHA256(K_mac, header || nonce || ciphertext || chunk_index || len)``."""
    mac = hmac.new(mac_key, digestmod=hashlib.sha256)
    mac.update(header)
    mac.update(nonce)
    mac.update(ciphertext)
    mac.update(struct.pack(">QQ", chunk_index, plaintext_len))
    return mac.digest()


@dataclass(frozen=True)
class EncryptedChunkEnvelope:
    """Authenticated Encrypt-then-MAC chunk container."""

    chunk_id: str
    chunk_index: int
    plaintext_len: int
    header: bytes
    nonce: bytes
    ciphertext: bytes
    mac_tag: bytes
    key_id: bytes

    def to_bytes(self) -> bytes:
        """Serialize the envelope into a self-contained binary wire representation."""
        return (
            _WIRE_FIXED_STRUCT.pack(len(self.header))
            + self.header
            + _WIRE_META_STRUCT.pack(
                self.key_id,
                self.nonce,
                self.chunk_index,
                self.plaintext_len,
                self.mac_tag,
            )
            + self.ciphertext
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> EncryptedChunkEnvelope:
        """Parse a binary chunk envelope fail-closed."""
        if len(raw) < _WIRE_FIXED_STRUCT.size:
            raise VaultTamperDetectedError("Truncated chunk envelope: missing header length")
        (header_len,) = _WIRE_FIXED_STRUCT.unpack_from(raw, 0)
        meta_offset = _WIRE_FIXED_STRUCT.size + header_len
        cipher_offset = meta_offset + _WIRE_META_STRUCT.size
        if header_len < 38 or len(raw) < cipher_offset:
            raise VaultTamperDetectedError("Truncated or malformed chunk envelope metadata")

        header = raw[_WIRE_FIXED_STRUCT.size : meta_offset]
        key_id, nonce, chunk_index, plaintext_len, mac_tag = _WIRE_META_STRUCT.unpack_from(
            raw, meta_offset
        )
        ciphertext = raw[cipher_offset:]
        flags = header[5]
        if flags == _FLAG_PADDED:
            if len(ciphertext) <= plaintext_len:
                raise VaultTamperDetectedError(
                    f"Padded ciphertext length {len(ciphertext)} must exceed {plaintext_len}"
                )
        elif len(ciphertext) != plaintext_len:
            raise VaultTamperDetectedError(
                f"Ciphertext length {len(ciphertext)} does not match declared {plaintext_len}"
            )
        chunk_id = header[6:38].hex()
        return cls(
            chunk_id=chunk_id,
            chunk_index=chunk_index,
            plaintext_len=plaintext_len,
            header=header,
            nonce=nonce,
            ciphertext=ciphertext,
            mac_tag=mac_tag,
            key_id=key_id,
        )


def encrypt_chunk(
    plaintext: bytes,
    key_material: VaultKeyMaterial,
    *,
    chunk_index: int = 0,
    nonce: bytes | None = None,
    pad_to_block_size: int = 1,
) -> EncryptedChunkEnvelope:
    """Encrypt one chunk using SHA-256 CTR + Encrypt-then-MAC (HMAC-SHA256).

    When ``pad_to_block_size > 1``, ``ciphertext`` is zero-padded prior to CTR
    encryption so ``len(ciphertext)`` is rounded up to a positive multiple of
    ``pad_to_block_size`` without altering ``plaintext_len``.
    """
    if chunk_index < 0:
        raise PersonalVaultError(f"chunk_index must be non-negative, got {chunk_index}")
    validate_pad_block_size(pad_to_block_size)
    effective_nonce = secrets.token_bytes(NONCE_BYTES) if nonce is None else bytes(nonce)
    if len(effective_nonce) != NONCE_BYTES:
        raise PersonalVaultError(f"nonce must be {NONCE_BYTES} bytes, got {len(effective_nonce)}")

    plaintext_len = len(plaintext)
    if pad_to_block_size == 1:
        padded_len = plaintext_len
    elif plaintext_len == 0:
        padded_len = pad_to_block_size
    else:
        padded_len = (
            (plaintext_len + pad_to_block_size - 1) // pad_to_block_size
        ) * pad_to_block_size

    pad_len = padded_len - plaintext_len
    flags = _FLAG_PADDED if pad_len > 0 else _FLAG_NONE
    padded_plaintext = plaintext if pad_len == 0 else (plaintext + (b"\x00" * pad_len))

    chunk_id = compute_chunk_id(plaintext, key_material)
    header = _build_envelope_header(chunk_id, flags=flags)
    ciphertext = sha256_ctr_crypt(key_material.enc_key, effective_nonce, padded_plaintext)
    mac_tag = _compute_etm_mac(
        key_material.mac_key,
        header=header,
        nonce=effective_nonce,
        ciphertext=ciphertext,
        chunk_index=chunk_index,
        plaintext_len=plaintext_len,
    )
    return EncryptedChunkEnvelope(
        chunk_id=chunk_id,
        chunk_index=chunk_index,
        plaintext_len=plaintext_len,
        header=header,
        nonce=effective_nonce,
        ciphertext=ciphertext,
        mac_tag=mac_tag,
        key_id=key_material.key_id,
    )


def _resolve_nonce(nonce: bytes | None) -> bytes:
    """Return a validated 16-byte nonce (fresh random when ``nonce`` is ``None``)."""
    effective_nonce = secrets.token_bytes(NONCE_BYTES) if nonce is None else bytes(nonce)
    if len(effective_nonce) != NONCE_BYTES:
        raise PersonalVaultError(f"nonce must be {NONCE_BYTES} bytes, got {len(effective_nonce)}")
    return effective_nonce


def _wire_geometry(plaintext_len: int, pad_to_block_size: int) -> tuple[int, int, int, int]:
    """Return ``(prefix_len, padded_len, pad_len, flags)`` for the wire layout."""
    validate_pad_block_size(pad_to_block_size)
    if plaintext_len < 0:
        raise PersonalVaultError(f"plaintext_len must be non-negative, got {plaintext_len}")
    if pad_to_block_size == 1:
        padded_len = plaintext_len
    elif plaintext_len == 0:
        padded_len = pad_to_block_size
    else:
        padded_len = (
            (plaintext_len + pad_to_block_size - 1) // pad_to_block_size
        ) * pad_to_block_size
    pad_len = padded_len - plaintext_len
    flags = _FLAG_PADDED if pad_len > 0 else _FLAG_NONE
    header_len = len(ENVELOPE_MAGIC) + 2 + 32  # magic + version/flags + hex chunk id
    prefix_len = _WIRE_FIXED_STRUCT.size + header_len + _WIRE_META_STRUCT.size
    return prefix_len, padded_len, pad_len, flags


def _finalize_wire(
    wire: bytearray,
    *,
    prefix_len: int,
    chunk_id: str,
    flags: int,
    nonce: bytes,
    key_material: VaultKeyMaterial,
    plaintext_len: int,
) -> None:
    """Write the authenticated prefix, CTR-encrypt in place, then patch the MAC tag."""
    header = _build_envelope_header(chunk_id, flags=flags)
    prefix = (
        _WIRE_FIXED_STRUCT.pack(len(header))
        + header
        + _WIRE_META_STRUCT.pack(
            key_material.key_id,
            nonce,
            0,
            plaintext_len,
            b"\x00" * MAC_TAG_BYTES,
        )
    )
    wire[0:prefix_len] = prefix
    sha256_ctr_crypt_inplace(key_material.enc_key, nonce, wire, start=prefix_len)
    wire_view = memoryview(wire)
    try:
        mac_tag = _compute_etm_mac(
            key_material.mac_key,
            header=header,
            nonce=nonce,
            ciphertext=wire_view[prefix_len:],
            chunk_index=0,
            plaintext_len=plaintext_len,
        )
    finally:
        wire_view.release()
    _WIRE_MAC_STRUCT.pack_into(wire, prefix_len - MAC_TAG_BYTES, mac_tag)


def encrypt_plaintext_to_wire(
    plaintext: bytes | bytearray | memoryview,
    key_material: VaultKeyMaterial,
    *,
    nonce: bytes | None = None,
    pad_to_block_size: int = 1,
    plaintext_observers: Sequence[Any] = (),
) -> tuple[bytearray, str]:
    """Encrypt an in-memory chunk into its wire buffer without extra copies.

    Returns ``(wire_bytes, chunk_id)``. The plaintext is copied once into the
    ciphertext slot of the final wire buffer and then overwritten in place by the
    keystream, so only ONE payload-sized extra buffer is live (the ~4 of
    ``encrypt_chunk(...).to_bytes()`` collapse to a single one).
    """
    plaintext_len = len(plaintext)
    effective_nonce = _resolve_nonce(nonce)
    prefix_len, padded_len, pad_len, flags = _wire_geometry(plaintext_len, pad_to_block_size)

    wire = bytearray(prefix_len + padded_len)
    wire_view = memoryview(wire)
    slot_view = wire_view[prefix_len : prefix_len + plaintext_len]
    try:
        slot_view[:] = plaintext
        chunk_id = compute_chunk_id(slot_view, key_material)
        for observer in plaintext_observers:
            observer.update(slot_view)
    finally:
        slot_view.release()
        wire_view.release()
    if pad_len > 0:
        wire[prefix_len + plaintext_len :] = b"\x00" * pad_len

    _finalize_wire(
        wire,
        prefix_len=prefix_len,
        chunk_id=chunk_id,
        flags=flags,
        nonce=effective_nonce,
        key_material=key_material,
        plaintext_len=plaintext_len,
    )
    return wire, chunk_id


def encrypt_chunk_from_fd_to_wire(
    *,
    file_handle: Any,
    plaintext_len: int,
    key_material: VaultKeyMaterial,
    nonce: bytes | None = None,
    pad_to_block_size: int = 1,
    plaintext_observers: Sequence[Any] = (),
) -> tuple[bytearray, str]:
    """Read a chunk straight into its wire buffer and encrypt it in place.

    Returns ``(wire_bytes, chunk_id)``. The plaintext is ``readinto`` the ciphertext
    slot of the final wire buffer, so exactly ONE payload-sized buffer exists at any
    instant — this is the memory-optimal path used by the streaming pusher.
    """
    effective_nonce = _resolve_nonce(nonce)
    prefix_len, padded_len, pad_len, flags = _wire_geometry(plaintext_len, pad_to_block_size)

    wire = bytearray(prefix_len + padded_len)
    wire_view = memoryview(wire)
    slot_view = wire_view[prefix_len : prefix_len + plaintext_len]
    try:
        read_len = file_handle.readinto(slot_view)
        if read_len != plaintext_len:
            raise VaultTransportError(
                f"Short read while encrypting chunk: expected {plaintext_len} bytes, got {read_len}"
            )
        chunk_id = compute_chunk_id(slot_view, key_material)
        for observer in plaintext_observers:
            observer.update(slot_view)
    finally:
        slot_view.release()
        wire_view.release()
    if pad_len > 0:
        wire[prefix_len + plaintext_len :] = b"\x00" * pad_len

    _finalize_wire(
        wire,
        prefix_len=prefix_len,
        chunk_id=chunk_id,
        flags=flags,
        nonce=effective_nonce,
        key_material=key_material,
        plaintext_len=plaintext_len,
    )
    return wire, chunk_id


def decrypt_chunk(
    envelope: EncryptedChunkEnvelope,
    key_material: VaultKeyMaterial,
    *,
    expected_chunk_index: int | None = None,
) -> bytes:
    """Verify EtM HMAC-SHA256 in constant time and decrypt ``envelope`` fail-closed."""
    verify_index = envelope.chunk_index if expected_chunk_index is None else expected_chunk_index
    if verify_index < 0 or envelope.plaintext_len < 0:
        raise VaultTamperDetectedError("Negative chunk index or plaintext length")

    expected_mac = _compute_etm_mac(
        key_material.mac_key,
        header=envelope.header,
        nonce=envelope.nonce,
        ciphertext=envelope.ciphertext,
        chunk_index=verify_index,
        plaintext_len=envelope.plaintext_len,
    )
    mac_ok = hmac.compare_digest(expected_mac, envelope.mac_tag)
    key_id_ok = hmac.compare_digest(envelope.key_id, key_material.key_id)

    if not mac_ok and not key_id_ok:
        raise VaultAuthenticationError("Chunk authentication failed: invalid vault key material")
    if not mac_ok or not key_id_ok:
        raise VaultTamperDetectedError(
            "Chunk integrity verification failed: ciphertext, header, nonce, or order tampered"
        )

    if (
        len(envelope.nonce) != NONCE_BYTES
        or len(envelope.ciphertext) < envelope.plaintext_len
        or len(envelope.chunk_id) != 64
    ):
        raise VaultTamperDetectedError("Malformed envelope geometry")

    expected_flags = (
        _FLAG_PADDED if len(envelope.ciphertext) > envelope.plaintext_len else _FLAG_NONE
    )
    try:
        expected_header = _build_envelope_header(envelope.chunk_id, flags=expected_flags)
    except ValueError as exc:
        raise VaultTamperDetectedError("Invalid hex chunk_id in envelope") from exc

    if not hmac.compare_digest(envelope.header, expected_header):
        raise VaultTamperDetectedError("Envelope header does not match chunk_id binding")

    decrypted_full = sha256_ctr_crypt(key_material.enc_key, envelope.nonce, envelope.ciphertext)
    if expected_flags == _FLAG_PADDED:
        pad_tail = decrypted_full[envelope.plaintext_len :]
        if not hmac.compare_digest(pad_tail, b"\x00" * len(pad_tail)):
            raise VaultTamperDetectedError("Corrupted non-zero padding bytes in chunk envelope")
    plaintext = decrypted_full[: envelope.plaintext_len]

    recomputed_id = compute_chunk_id(plaintext, key_material)
    if not hmac.compare_digest(recomputed_id, envelope.chunk_id):
        raise VaultTamperDetectedError("Decrypted chunk content does not match chunk_id")
    return plaintext


def encrypt_stream_chunks(
    data: bytes,
    key_material: VaultKeyMaterial,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    pad_to_block_size: int = 1,
) -> tuple[EncryptedChunkEnvelope, ...]:
    """Split ``data`` into chunks and encrypt each with sequential ``chunk_index``."""
    validate_pad_block_size(pad_to_block_size)
    raw_chunks = split_into_chunks(data, chunk_size=chunk_size)
    return tuple(
        encrypt_chunk(
            chunk,
            key_material,
            chunk_index=idx,
            pad_to_block_size=pad_to_block_size,
        )
        for idx, chunk in enumerate(raw_chunks)
    )


def decrypt_stream_chunks(
    envelopes: Sequence[EncryptedChunkEnvelope],
    key_material: VaultKeyMaterial,
) -> bytes:
    """Verify sequential ordering and EtM tags of ``envelopes`` before reassembly."""
    parts = [
        decrypt_chunk(envelope, key_material, expected_chunk_index=idx)
        for idx, envelope in enumerate(envelopes)
    ]
    return b"".join(parts)


# --------------------------------------------------------------------------- #
# RFC 6962 Merkle Tree (0x00 leaf, 0x01 interior, 0x02 + be64(count) + root)
# --------------------------------------------------------------------------- #


def compute_rfc6962_raw_root(leaves: Sequence[bytes]) -> bytes:
    """Compute RFC 6962 §2.1 Merkle Tree Hash without odd-node duplication."""
    count = len(leaves)
    if count == 0:
        return hashlib.sha256(b"").digest()
    if count == 1:
        return hashlib.sha256(b"\x00" + leaves[0]).digest()

    # Largest power of two strictly less than `count` (RFC 6962 §2.1 `k`).
    split = 1 << ((count - 1).bit_length() - 1)
    left_hash = compute_rfc6962_raw_root(leaves[:split])
    right_hash = compute_rfc6962_raw_root(leaves[split:])
    return hashlib.sha256(b"\x01" + left_hash + right_hash).digest()


def compute_rfc6962_merkle_root(leaves: Sequence[bytes]) -> str:
    """Compute length-bound RFC 6962 root ``SHA256(0x02 || total_leaves_be64 || root)``."""
    raw_root = compute_rfc6962_raw_root(leaves)
    envelope = b"\x02" + struct.pack(">Q", len(leaves)) + raw_root
    return hashlib.sha256(envelope).hexdigest()


# --------------------------------------------------------------------------- #
# Project Snapshot Manifest
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VaultChunkRef:
    """Reference to one encrypted content-addressed chunk within a file entry."""

    chunk_index: int
    chunk_id: str
    plaintext_len: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_index": self.chunk_index,
            "chunk_id": self.chunk_id,
            "plaintext_len": self.plaintext_len,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VaultChunkRef:
        try:
            chunk_index = int(data["chunk_index"])
            chunk_id = str(data["chunk_id"])
            plaintext_len = int(data["plaintext_len"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VaultTamperDetectedError("Malformed chunk reference in manifest") from exc
        if chunk_index < 0 or plaintext_len < 0 or len(chunk_id) != 64:
            raise VaultTamperDetectedError("Invalid chunk reference bounds in manifest")
        return cls(chunk_index=chunk_index, chunk_id=chunk_id, plaintext_len=plaintext_len)


@dataclass(frozen=True)
class VaultFileEntry:
    """Manifest entry describing one logical project file and its ordered chunks."""

    rel_path: str
    total_size: int
    file_mac_hex: str
    chunks: tuple[VaultChunkRef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "total_size": self.total_size,
            "file_mac_hex": self.file_mac_hex,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VaultFileEntry:
        try:
            rel_path = normalize_relative_vault_path(str(data["rel_path"]))
            total_size = int(data["total_size"])
            file_mac_hex = str(data["file_mac_hex"])
            raw_chunks = data["chunks"]
            if not isinstance(raw_chunks, list):
                raise VaultTamperDetectedError("Manifest file chunks must be a list")
            chunks = tuple(VaultChunkRef.from_dict(item) for item in raw_chunks)
        except VaultTamperDetectedError:
            raise
        except (PersonalVaultError, KeyError, TypeError, ValueError) as exc:
            raise VaultTamperDetectedError("Malformed file entry in manifest") from exc
        if total_size < 0 or len(file_mac_hex) != 64:
            raise VaultTamperDetectedError("Invalid file size or MAC in manifest")
        return cls(
            rel_path=rel_path,
            total_size=total_size,
            file_mac_hex=file_mac_hex,
            chunks=chunks,
        )


def _manifest_leaves_for_files(files: Sequence[VaultFileEntry]) -> list[bytes]:
    """Construct canonical RFC 6962 leaf payloads covering every file and chunk."""
    leaves: list[bytes] = []
    for entry in files:
        if not entry.chunks:
            payload = json.dumps(
                {
                    "kind": "empty_file",
                    "rel_path": entry.rel_path,
                    "file_mac_hex": entry.file_mac_hex,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            leaves.append(payload)
        else:
            for chunk in entry.chunks:
                payload = json.dumps(
                    {
                        "kind": "file_chunk",
                        "rel_path": entry.rel_path,
                        "chunk_index": chunk.chunk_index,
                        "chunk_id": chunk.chunk_id,
                        "plaintext_len": chunk.plaintext_len,
                        "file_mac_hex": entry.file_mac_hex,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                leaves.append(payload)
    return leaves


@dataclass(frozen=True)
class VaultSnapshotManifest:
    """Cryptographically authenticated project snapshot manifest with RFC 6962 root."""

    schema_version: int
    project_id: str
    snapshot_id: str
    parent_snapshot_id: str | None
    created_at_utc: str
    chunk_size: int
    salt_hex: str
    kdf_iterations: int
    key_id_hex: str
    files: tuple[VaultFileEntry, ...]
    metadata: dict[str, str]
    total_leaves: int
    merkle_root: str
    manifest_mac: str

    def _canonical_signing_bytes(self) -> bytes:
        body = {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "snapshot_id": self.snapshot_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "created_at_utc": self.created_at_utc,
            "chunk_size": self.chunk_size,
            "salt_hex": self.salt_hex,
            "kdf_iterations": self.kdf_iterations,
            "key_id_hex": self.key_id_hex,
            "files": [entry.to_dict() for entry in self.files],
            "metadata": dict(sorted(self.metadata.items())),
            "total_leaves": self.total_leaves,
            "merkle_root": self.merkle_root,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def referenced_chunk_ids(self) -> set[str]:
        """Return every ``chunk_id`` referenced across all files in this snapshot."""
        return {chunk.chunk_id for entry in self.files for chunk in entry.chunks}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "snapshot_id": self.snapshot_id,
            "parent_snapshot_id": self.parent_snapshot_id,
            "created_at_utc": self.created_at_utc,
            "chunk_size": self.chunk_size,
            "salt_hex": self.salt_hex,
            "kdf_iterations": self.kdf_iterations,
            "key_id_hex": self.key_id_hex,
            "files": [entry.to_dict() for entry in self.files],
            "metadata": dict(sorted(self.metadata.items())),
            "total_leaves": self.total_leaves,
            "merkle_root": self.merkle_root,
            "manifest_mac": self.manifest_mac,
        }

    def to_json_bytes(self) -> bytes:
        """Serialize the manifest deterministically as UTF-8 JSON bytes."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VaultSnapshotManifest:
        try:
            schema_version = int(data["schema_version"])
            project_id = str(data["project_id"])
            snapshot_id = str(data["snapshot_id"])
            raw_parent = data.get("parent_snapshot_id")
            parent_snapshot_id = None if raw_parent is None else str(raw_parent)
            created_at_utc = str(data["created_at_utc"])
            chunk_size = validate_chunk_size(int(data["chunk_size"]))
            salt_hex = str(data["salt_hex"])
            bytes.fromhex(salt_hex)
            kdf_iterations = int(data["kdf_iterations"])
            key_id_hex = str(data["key_id_hex"])
            bytes.fromhex(key_id_hex)
            raw_files = data["files"]
            if not isinstance(raw_files, list):
                raise VaultTamperDetectedError("Manifest files field must be a list")
            files = tuple(VaultFileEntry.from_dict(item) for item in raw_files)
            raw_meta = data.get("metadata", {})
            if not isinstance(raw_meta, Mapping):
                raise VaultTamperDetectedError("Manifest metadata must be a mapping")
            metadata = {str(k): str(v) for k, v in raw_meta.items()}
            total_leaves = int(data["total_leaves"])
            merkle_root = str(data["merkle_root"])
            manifest_mac = str(data["manifest_mac"])
        except VaultTamperDetectedError:
            raise
        except (PersonalVaultError, KeyError, TypeError, ValueError) as exc:
            raise VaultTamperDetectedError("Malformed snapshot manifest payload") from exc

        if (
            schema_version != 1
            or not project_id
            or not snapshot_id
            or total_leaves < 0
            or len(merkle_root) != 64
            or len(manifest_mac) != 64
        ):
            raise VaultTamperDetectedError("Invalid snapshot manifest header fields")

        return cls(
            schema_version=schema_version,
            project_id=project_id,
            snapshot_id=snapshot_id,
            parent_snapshot_id=parent_snapshot_id,
            created_at_utc=created_at_utc,
            chunk_size=chunk_size,
            salt_hex=salt_hex,
            kdf_iterations=kdf_iterations,
            key_id_hex=key_id_hex,
            files=files,
            metadata=metadata,
            total_leaves=total_leaves,
            merkle_root=merkle_root,
            manifest_mac=manifest_mac,
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> VaultSnapshotManifest:
        """Parse UTF-8 JSON bytes into a :class:`VaultSnapshotManifest` fail-closed."""
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VaultTamperDetectedError("Corrupted JSON in snapshot manifest") from exc
        if not isinstance(decoded, Mapping):
            raise VaultTamperDetectedError("Snapshot manifest root must be a JSON object")
        return cls.from_dict(decoded)


def build_snapshot_manifest(
    *,
    project_id: str,
    snapshot_id: str,
    parent_snapshot_id: str | None,
    created_at_utc: str,
    chunk_size: int,
    files: Sequence[VaultFileEntry],
    key_material: VaultKeyMaterial,
    metadata: Mapping[str, str] | None = None,
) -> VaultSnapshotManifest:
    """Build and authenticate a :class:`VaultSnapshotManifest` with RFC 6962 Merkle root."""
    if not project_id or not snapshot_id:
        raise PersonalVaultError("project_id and snapshot_id must be non-empty")
    validate_chunk_size(chunk_size)
    sorted_files = tuple(sorted(files, key=lambda entry: entry.rel_path))
    if len({entry.rel_path for entry in sorted_files}) != len(sorted_files):
        raise PersonalVaultError("Duplicate relative file path in snapshot manifest")

    leaves = _manifest_leaves_for_files(sorted_files)
    merkle_root = compute_rfc6962_merkle_root(leaves)
    clean_meta = {} if metadata is None else {str(k): str(v) for k, v in metadata.items()}

    unsigned = VaultSnapshotManifest(
        schema_version=1,
        project_id=project_id,
        snapshot_id=snapshot_id,
        parent_snapshot_id=parent_snapshot_id,
        created_at_utc=created_at_utc,
        chunk_size=chunk_size,
        salt_hex=key_material.salt.hex(),
        kdf_iterations=key_material.iterations,
        key_id_hex=key_material.key_id.hex(),
        files=sorted_files,
        metadata=clean_meta,
        total_leaves=len(leaves),
        merkle_root=merkle_root,
        manifest_mac="0" * 64,
    )
    manifest_mac = hmac.new(
        key_material.mac_key,
        b"nagar-pcv-manifest-v1|" + unsigned._canonical_signing_bytes(),
        hashlib.sha256,
    ).hexdigest()
    return VaultSnapshotManifest(
        schema_version=unsigned.schema_version,
        project_id=unsigned.project_id,
        snapshot_id=unsigned.snapshot_id,
        parent_snapshot_id=unsigned.parent_snapshot_id,
        created_at_utc=unsigned.created_at_utc,
        chunk_size=unsigned.chunk_size,
        salt_hex=unsigned.salt_hex,
        kdf_iterations=unsigned.kdf_iterations,
        key_id_hex=unsigned.key_id_hex,
        files=unsigned.files,
        metadata=unsigned.metadata,
        total_leaves=unsigned.total_leaves,
        merkle_root=unsigned.merkle_root,
        manifest_mac=manifest_mac,
    )


def verify_manifest_integrity(
    manifest: VaultSnapshotManifest,
    key_material: VaultKeyMaterial,
) -> None:
    """Verify manifest HMAC-SHA256, file chunk bounds, and RFC 6962 Merkle root fail-closed."""
    expected_mac = hmac.new(
        key_material.mac_key,
        b"nagar-pcv-manifest-v1|" + manifest._canonical_signing_bytes(),
        hashlib.sha256,
    ).hexdigest()
    mac_ok = hmac.compare_digest(expected_mac, manifest.manifest_mac)
    key_id_ok = hmac.compare_digest(manifest.key_id_hex, key_material.key_id.hex())

    if not mac_ok and not key_id_ok:
        raise VaultAuthenticationError("Manifest authentication failed: invalid vault key material")
    if not mac_ok or not key_id_ok:
        raise VaultTamperDetectedError("Manifest HMAC or key binding verification failed")

    sorted_paths = [entry.rel_path for entry in manifest.files]
    if sorted_paths != sorted(sorted_paths) or len(set(sorted_paths)) != len(sorted_paths):
        raise VaultTamperDetectedError("Manifest file list is unsorted or contains duplicates")

    for entry in manifest.files:
        expected_indices = list(range(len(entry.chunks)))
        actual_indices = [chunk.chunk_index for chunk in entry.chunks]
        if actual_indices != expected_indices:
            raise VaultTamperDetectedError(f"Non-contiguous chunk indices in {entry.rel_path!r}")
        if sum(chunk.plaintext_len for chunk in entry.chunks) != entry.total_size:
            raise VaultTamperDetectedError(f"File size mismatch in {entry.rel_path!r}")

    leaves = _manifest_leaves_for_files(manifest.files)
    if len(leaves) != manifest.total_leaves:
        raise VaultTamperDetectedError("Manifest total_leaves does not match reconstructed leaves")
    recomputed_root = compute_rfc6962_merkle_root(leaves)
    if not hmac.compare_digest(recomputed_root, manifest.merkle_root):
        raise VaultTamperDetectedError("Manifest RFC 6962 Merkle root mismatch")


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_SALT_BYTES",
    "ENVELOPE_MAGIC",
    "ENVELOPE_VERSION",
    "EncryptedChunkEnvelope",
    "KEY_ID_BYTES",
    "MAC_TAG_BYTES",
    "MAX_CHUNK_SIZE",
    "MAX_DEFAULT_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "MIN_PBKDF2_ITERATIONS",
    "MIN_SALT_BYTES",
    "NONCE_BYTES",
    "PBKDF2_DKLEN",
    "PBKDF2_ITERATIONS",
    "VaultChunkRef",
    "VaultFileEntry",
    "VaultKeyMaterial",
    "VaultSnapshotManifest",
    "build_snapshot_manifest",
    "compute_chunk_id",
    "compute_file_mac",
    "compute_rfc6962_merkle_root",
    "compute_rfc6962_raw_root",
    "decrypt_chunk",
    "decrypt_stream_chunks",
    "derive_vault_keys",
    "encrypt_chunk",
    "encrypt_chunk_from_fd_to_wire",
    "encrypt_plaintext_to_wire",
    "encrypt_stream_chunks",
    "generate_vault_salt",
    "hkdf_expand_sha256",
    "hkdf_extract_sha256",
    "init_streaming_file_mac",
    "normalize_relative_vault_path",
    "sha256_ctr_crypt",
    "sha256_ctr_crypt_inplace",
    "split_into_chunks",
    "validate_chunk_size",
    "validate_pad_block_size",
    "verify_manifest_integrity",
]
