"""Cryptographic, chunking, and RFC 6962 Merkle tests for personal_cloud_vault."""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import replace

import pytest

from nexus_ai_agent.storage.personal_cloud_vault import (
    AEAD_TAG_BYTES,
    CIPHER_AES_256_GCM,
    CIPHER_CHACHA20_POLY1305,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CIPHER_SUITE,
    MAX_CHUNK_SIZE,
    MAX_DEFAULT_CHUNK_SIZE,
    MIN_PBKDF2_ITERATIONS,
    PBKDF2_ITERATIONS,
    EncryptedChunkEnvelope,
    PersonalVaultError,
    VaultAuthenticationError,
    VaultChunkRef,
    VaultCryptoError,
    VaultFileEntry,
    VaultKeyMaterial,
    VaultKeyRing,
    VaultSnapshotManifest,
    VaultTamperDetectedError,
    VaultTransportError,
    build_snapshot_manifest,
    compute_chunk_id,
    compute_file_mac,
    compute_rfc6962_merkle_root,
    compute_rfc6962_raw_root,
    decrypt_chunk,
    decrypt_stream_chunks,
    derive_vault_keys,
    encrypt_chunk,
    encrypt_stream_chunks,
    envelope_wire_version,
    generate_vault_salt,
    hkdf_expand_sha256,
    hkdf_extract_sha256,
    migrate_legacy_v1_envelope_bytes,
    normalize_relative_vault_path,
    split_into_chunks,
    validate_chunk_size,
    validate_pad_block_size,
    verify_manifest_integrity,
)


def _flip_byte_bit(buf: bytes, byte_idx: int, bit_mask: int = 0x01) -> bytes:
    mutable = bytearray(buf)
    mutable[byte_idx] ^= bit_mask
    return bytes(mutable)


def test_key_derivation_pbkdf2_hkdf_domain_separation_and_secret_redaction() -> None:
    assert DEFAULT_CHUNK_SIZE == 1_048_576
    assert MAX_DEFAULT_CHUNK_SIZE == 4_194_304
    assert PBKDF2_ITERATIONS == 600_000
    assert MIN_PBKDF2_ITERATIONS == 100_000

    salt = generate_vault_salt()
    assert len(salt) == 32

    # Default call uses OWASP 600,000 PBKDF2-HMAC-SHA256 iterations
    km_str = derive_vault_keys("correct-horse-battery-staple", salt)
    km_bytes = derive_vault_keys(b"correct-horse-battery-staple", salt)
    assert km_str == km_bytes
    assert km_str.iterations == 600_000
    assert len(km_str.enc_key) == 32
    assert len(km_str.mac_key) == 32
    assert len(km_str.chunk_id_key) == 32
    assert len(km_str.key_id) == 16
    assert len({km_str.enc_key, km_str.mac_key, km_str.chunk_id_key}) == 3

    # Explicit minimum 100,000 iterations remains supported
    km_min = derive_vault_keys("correct-horse-battery-staple", salt, iterations=100_000)
    assert km_min.iterations == 100_000
    assert km_min.enc_key != km_str.enc_key

    rendered = repr(km_str)
    assert km_str.enc_key.hex() not in rendered
    assert km_str.mac_key.hex() not in rendered
    assert "enc_key=" not in rendered
    assert "mac_key=" not in rendered


def test_known_answer_vector_kat_aead_seal_and_open() -> None:
    """Immutable Known-Answer Test (KAT) locking byte-for-byte AEAD seal outputs."""
    enc_key = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    mac_key = bytes.fromhex("202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f")
    cid_key = bytes.fromhex("404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f")
    key_id = bytes.fromhex("606162636465666768696a6b6c6d6e6f")
    salt = bytes.fromhex("707172737475767778797a7b7c7d7e7f808182838485868788898a8b8c8d8e8f")
    nonce = bytes.fromhex("a0a1a2a3a4a5a6a7a8a9aaab")
    plaintext = b"NAGAR-PCV-KAT-v2: AEAD seal & open vector"

    km = VaultKeyMaterial(
        enc_key=enc_key,
        mac_key=mac_key,
        chunk_id_key=cid_key,
        key_id=key_id,
        salt=salt,
        iterations=600_000,
    )
    expected_cid = "f99a8dc418e3907cf1f4409ffbfaea0a8c27000b5eb9ea309554f07990c9d856"
    expected_hdr = "4e5043560200f99a8dc418e3907cf1f4409ffbfaea0a8c27000b5eb9ea309554f07990c9d856"
    expected_aes_ct = (
        "9b2c7f5d921004e56198b89f7f885fd7772a70bffbd00c8b0de503e5c10a66da20464be39dfa1e22c5"
    )
    expected_aes_tag = "f80e5ebe88853b5a5a4f6438f7e3bdbe"
    expected_chacha_ct = (
        "98ddec032aecc1adabdefbba0ac84fac149615d590531bc229efa9eb28d421c24116810cc9b220e5f7"
    )
    expected_chacha_tag = "db8f5709e58f8dc3bc162a85f79b5177"

    env_aes = encrypt_chunk(plaintext, km, chunk_index=7, nonce=nonce)
    assert env_aes.chunk_id == expected_cid
    assert env_aes.header.hex() == expected_hdr
    assert env_aes.ciphertext.hex() == expected_aes_ct
    assert env_aes.aead_tag.hex() == expected_aes_tag
    assert env_aes.cipher_suite == CIPHER_AES_256_GCM == DEFAULT_CIPHER_SUITE

    env_chacha = encrypt_chunk(
        plaintext, km, chunk_index=7, nonce=nonce, cipher_suite=CIPHER_CHACHA20_POLY1305
    )
    assert env_chacha.chunk_id == expected_cid
    assert env_chacha.header.hex() == expected_hdr
    assert env_chacha.ciphertext.hex() == expected_chacha_ct
    assert env_chacha.aead_tag.hex() == expected_chacha_tag

    for env in (env_aes, env_chacha):
        assert (
            decrypt_chunk(
                EncryptedChunkEnvelope.from_bytes(env.to_bytes()),
                km,
                expected_chunk_index=7,
            )
            == plaintext
        )

    # KDF + HKDF KAT lock
    km_kdf = derive_vault_keys(
        "nagar-kat-passphrase",
        b"0123456789abcdef0123456789abcdef",
        iterations=100_000,
    )
    assert km_kdf.enc_key.hex() == (
        "5ae6947133de9492ef0c72eee0cd8d2b17e8fd76b667f460d940fc69f7d7781b"
    )
    assert km_kdf.mac_key.hex() == (
        "70413441af86b7d126b093eeadfad4490a4a79fbb8fc59d871a54015837235c4"
    )
    assert km_kdf.key_id.hex() == "b504f12bf4c67829be1e78149c84944e"


def test_optional_pad_to_block_size_length_hiding_and_tamper_guards() -> None:
    km = derive_vault_keys("pad-secret", b"P" * 32, iterations=100_000)

    assert validate_pad_block_size(4096) == 4096
    with pytest.raises(PersonalVaultError):
        validate_pad_block_size(True)
    with pytest.raises(PersonalVaultError):
        validate_pad_block_size(0)
    with pytest.raises(PersonalVaultError):
        validate_pad_block_size(MAX_CHUNK_SIZE + 1)

    cases = [
        (b"", 64, 0, 64),
        (b"short-17-bytes!!!", 64, 17, 64),
        (b"A" * 64, 64, 64, 64),
        (b"A" * 65, 64, 65, 128),
    ]
    for sample, block_sz, expected_plain_len, expected_ct_len in cases:
        env = encrypt_chunk(sample, km, chunk_index=2, pad_to_block_size=block_sz)
        assert env.plaintext_len == expected_plain_len
        assert len(env.ciphertext) == expected_ct_len
        assert len(env.ciphertext) % block_sz == 0
        parsed = EncryptedChunkEnvelope.from_bytes(env.to_bytes())
        assert decrypt_chunk(parsed, km, expected_chunk_index=2) == sample

    # Stream encryption with block padding preserves exact payload length upon reassembly
    stream_payload = b"stream-data-" * 15  # 180 bytes
    padded_stream = encrypt_stream_chunks(stream_payload, km, chunk_size=100, pad_to_block_size=64)
    assert [len(e.ciphertext) for e in padded_stream] == [128, 128]
    assert [e.plaintext_len for e in padded_stream] == [100, 80]
    assert decrypt_stream_chunks(padded_stream, km) == stream_payload

    # Tamper 1 bit in encrypted padding tail -> VaultTamperDetectedError
    padded_env = encrypt_chunk(b"hello", km, pad_to_block_size=64)
    tampered_tail = replace(padded_env, ciphertext=_flip_byte_bit(padded_env.ciphertext, 63, 0x01))
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(tampered_tail, km)

    # Truncated padded wire envelope where flags=_FLAG_PADDED but len(ciphertext) <= plaintext_len
    raw_padded = bytearray(padded_env.to_bytes())
    with pytest.raises(VaultTamperDetectedError):
        EncryptedChunkEnvelope.from_bytes(bytes(raw_padded[:-60]))

    # Forged envelope: a validly-sealed payload whose decrypted padding tail is
    # non-zero is still rejected by the structural padding guard (defense in depth).
    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        _FLAG_PADDED,
        _aead_seal,
        _build_envelope_header,
    )

    bad_padded_plain = b"hello" + (b"\xff" * 59)
    hdr = _build_envelope_header(padded_env.chunk_id, flags=_FLAG_PADDED)
    bad_ct, bad_tag = _aead_seal(
        km.enc_key,
        padded_env.nonce,
        padded_env.cipher_suite,
        header=hdr,
        chunk_index=0,
        plaintext_len=5,
        plaintext=bad_padded_plain,
    )
    forged_pad_env = EncryptedChunkEnvelope(
        chunk_id=padded_env.chunk_id,
        chunk_index=0,
        plaintext_len=5,
        header=hdr,
        nonce=padded_env.nonce,
        ciphertext=bad_ct,
        aead_tag=bad_tag,
        key_id=km.key_id,
    )
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_pad_env, km)


def test_rfc5869_hkdf_test_vector_and_validation_guards() -> None:
    # RFC 5869 Appendix A.1 Test Case 1 (SHA-256)
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    prk = hkdf_extract_sha256(salt, ikm)
    assert prk.hex() == "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5"
    okm = hkdf_expand_sha256(prk, info, 42)
    assert okm.hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )
    # Empty salt uses 32 zero bytes per RFC 5869 §2.2
    assert hkdf_extract_sha256(b"", ikm) == hkdf_extract_sha256(b"\x00" * 32, ikm)

    with pytest.raises(VaultAuthenticationError):
        hkdf_expand_sha256(b"short", b"info", 32)
    with pytest.raises(PersonalVaultError):
        hkdf_expand_sha256(prk, b"info", 0)
    with pytest.raises(PersonalVaultError):
        hkdf_expand_sha256(prk, b"info", 255 * 32 + 1)

    with pytest.raises(VaultAuthenticationError):
        generate_vault_salt(8)
    with pytest.raises(VaultAuthenticationError):
        derive_vault_keys("", generate_vault_salt())
    with pytest.raises(VaultAuthenticationError):
        derive_vault_keys("secret", b"short-salt")
    with pytest.raises(VaultAuthenticationError):
        derive_vault_keys("secret", generate_vault_salt(), iterations=99_999)


def test_chunk_slicing_and_validation_bounds() -> None:
    assert split_into_chunks(b"", chunk_size=1024) == ()
    payload = bytes(range(250)) * 5  # 1250 bytes
    chunks = split_into_chunks(payload, chunk_size=300)
    assert [len(c) for c in chunks] == [300, 300, 300, 300, 50]
    assert b"".join(chunks) == payload

    assert validate_chunk_size(MAX_DEFAULT_CHUNK_SIZE) == MAX_DEFAULT_CHUNK_SIZE
    with pytest.raises(PersonalVaultError):
        validate_chunk_size(True)
    with pytest.raises(PersonalVaultError):
        validate_chunk_size(0)
    with pytest.raises(PersonalVaultError):
        validate_chunk_size(MAX_CHUNK_SIZE + 1)


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "/abs/path.txt",
        "../escape.txt",
        "a/../b.txt",
        "./a.txt",
        "a\\b.txt",
        "a\x00b.txt",
        "a//b",
    ],
)
def test_normalize_relative_vault_path_rejects_unsafe_paths(bad_path: str) -> None:
    with pytest.raises(PersonalVaultError):
        normalize_relative_vault_path(bad_path)


def test_encrypt_decrypt_roundtrip_and_wire_serialization() -> None:
    km = derive_vault_keys("vault-passphrase-alpha", b"S" * 32)

    # Empty chunk, exact 32B block, and multi-block non-aligned payload
    for idx, sample in enumerate([b"", b"A" * 32, b"NAGAR-Creative-OS-" * 77 + b"!"]):
        env = encrypt_chunk(sample, km, chunk_index=idx)
        wire = env.to_bytes()
        parsed = EncryptedChunkEnvelope.from_bytes(wire)
        assert parsed == env
        restored = decrypt_chunk(parsed, km, expected_chunk_index=idx)
        assert restored == sample

    # 1 MiB chunk roundtrip at default chunk size
    one_mib = b"\x5a\xa5" * (DEFAULT_CHUNK_SIZE // 2)
    stream_envs = encrypt_stream_chunks(one_mib + b"tail", km, chunk_size=DEFAULT_CHUNK_SIZE)
    assert len(stream_envs) == 2
    assert decrypt_stream_chunks(stream_envs, km) == one_mib + b"tail"


def test_anti_forgery_1bit_tamper_wrong_key_and_chunk_reordering() -> None:
    salt = b"1234567890abcdef1234567890abcdef"
    km_right = derive_vault_keys("owner-master-key", salt)
    km_wrong = derive_vault_keys("attacker-wrong-key", salt)

    plaintext = b"Confidential timeline track and keyframes payload" * 8
    env = encrypt_chunk(plaintext, km_right, chunk_index=3)

    # 1) 1-bit flip in ciphertext -> VaultTamperDetectedError
    tampered_ct = replace(env, ciphertext=_flip_byte_bit(env.ciphertext, len(env.ciphertext) // 2))
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(tampered_ct, km_right, expected_chunk_index=3)

    # 2) 1-bit flip in nonce -> VaultTamperDetectedError
    tampered_nonce = replace(env, nonce=_flip_byte_bit(env.nonce, 0, 0x80))
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(tampered_nonce, km_right, expected_chunk_index=3)

    # 3) 1-bit flip in header -> VaultTamperDetectedError
    tampered_header = replace(env, header=_flip_byte_bit(env.header, 0, 0x01))
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(tampered_header, km_right, expected_chunk_index=3)

    # 4) 1-bit flip in the AEAD tag -> VaultTamperDetectedError
    tampered_tag = replace(env, aead_tag=_flip_byte_bit(env.aead_tag, 15, 0x04))
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(tampered_tag, km_right, expected_chunk_index=3)

    # 5) 1-bit flip in key_id -> VaultAuthenticationError (unrecognised key material)
    tampered_kid = replace(env, key_id=_flip_byte_bit(env.key_id, 5, 0x02))
    with pytest.raises(VaultAuthenticationError):
        decrypt_chunk(tampered_kid, km_right, expected_chunk_index=3)

    # 6) Wrong chunk order / swapped sequence -> VaultTamperDetectedError
    stream = encrypt_stream_chunks(b"0123456789" * 30, km_right, chunk_size=64)
    assert len(stream) >= 4
    reordered = (stream[1], stream[0], *stream[2:])
    with pytest.raises(VaultTamperDetectedError):
        decrypt_stream_chunks(reordered, km_right)

    # 7) Wrong key -> VaultAuthenticationError
    with pytest.raises(VaultAuthenticationError):
        decrypt_chunk(env, km_wrong, expected_chunk_index=3)


def test_envelope_edge_guards_and_wire_corruption_cases() -> None:
    km = derive_vault_keys("edge-guard-secret", b"E" * 32)

    with pytest.raises(PersonalVaultError):
        encrypt_chunk(b"abc", km, chunk_index=-1)
    with pytest.raises(PersonalVaultError):
        encrypt_chunk(b"abc", km, nonce=b"short")
    with pytest.raises(VaultCryptoError):
        encrypt_chunk(b"abc", km, cipher_suite=99)

    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        _WIRE_META_STRUCT,
        _aead_seal,
    )

    with pytest.raises(VaultAuthenticationError):
        _aead_seal(
            b"short",
            b"N" * 12,
            CIPHER_AES_256_GCM,
            header=b"h",
            chunk_index=0,
            plaintext_len=0,
            plaintext=b"",
        )
    with pytest.raises(VaultCryptoError):
        _aead_seal(
            km.enc_key,
            b"N" * 12,
            99,
            header=b"h",
            chunk_index=0,
            plaintext_len=0,
            plaintext=b"",
        )

    env = encrypt_chunk(b"hello world", km, chunk_index=0)
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(env, km, expected_chunk_index=-1)

    with pytest.raises(VaultTamperDetectedError):
        EncryptedChunkEnvelope.from_bytes(b"\x00")
    with pytest.raises(VaultTamperDetectedError):
        EncryptedChunkEnvelope.from_bytes(b"\x00\x10" + b"x" * 20)
    wire = env.to_bytes()
    with pytest.raises(VaultTamperDetectedError):
        EncryptedChunkEnvelope.from_bytes(wire + b"\xff")

    # Forged geometry: a short nonce is rejected before any AEAD work.
    forged_geom = EncryptedChunkEnvelope(
        chunk_id=env.chunk_id,
        chunk_index=0,
        plaintext_len=env.plaintext_len,
        header=env.header,
        nonce=b"short",
        ciphertext=env.ciphertext,
        aead_tag=env.aead_tag,
        key_id=km.key_id,
    )
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_geom, km)

    # A forged short AEAD tag is rejected by the geometry guard.
    forged_short_tag = replace(env, aead_tag=b"\x00" * 8)
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_short_tag, km)

    # An unsupported cipher suite on a parsed envelope is rejected fail-closed.
    forged_suite = replace(env, cipher_suite=99)
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_suite, km)

    forged_non_hex_id = EncryptedChunkEnvelope(
        chunk_id="z" * 64,
        chunk_index=0,
        plaintext_len=env.plaintext_len,
        header=env.header,
        nonce=env.nonce,
        ciphertext=env.ciphertext,
        aead_tag=env.aead_tag,
        key_id=km.key_id,
    )
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_non_hex_id, km)

    other_id = "0" * 64
    forged_mismatched_id = EncryptedChunkEnvelope(
        chunk_id=other_id,
        chunk_index=0,
        plaintext_len=env.plaintext_len,
        header=env.header,
        nonce=env.nonce,
        ciphertext=env.ciphertext,
        aead_tag=env.aead_tag,
        key_id=km.key_id,
    )
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_mismatched_id, km)

    # Header bound to a fake chunk_id: the version/flags header mismatch is rejected.
    fake_header = b"NPCV\x01\x00" + bytes.fromhex(other_id)
    forged_content_mismatch = EncryptedChunkEnvelope(
        chunk_id=other_id,
        chunk_index=0,
        plaintext_len=env.plaintext_len,
        header=fake_header,
        nonce=env.nonce,
        ciphertext=env.ciphertext,
        aead_tag=env.aead_tag,
        key_id=km.key_id,
    )
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_content_mismatch, km)

    # A wire envelope carrying an unsupported suite byte is rejected at parse time.
    bad_suite_wire = bytearray(env.to_bytes())
    meta_offset = 2 + len(env.header)
    suite_index = meta_offset + _WIRE_META_STRUCT.size - AEAD_TAG_BYTES - 1
    bad_suite_wire[suite_index] = 42
    with pytest.raises(VaultTamperDetectedError):
        EncryptedChunkEnvelope.from_bytes(bytes(bad_suite_wire))


def test_rfc6962_merkle_tree_domain_separation_and_cve_2012_2459_immunity() -> None:
    # 0 leaves
    raw0 = compute_rfc6962_raw_root([])
    assert raw0 == hashlib.sha256(b"").digest()
    root0 = compute_rfc6962_merkle_root([])
    assert root0 == hashlib.sha256(b"\x02" + (0).to_bytes(8, "big") + raw0).hexdigest()

    # 1 leaf: 0x00 domain separator
    leaf_a = b"chunk-leaf-a"
    raw1 = compute_rfc6962_raw_root([leaf_a])
    assert raw1 == hashlib.sha256(b"\x00" + leaf_a).digest()
    root1 = compute_rfc6962_merkle_root([leaf_a])
    assert root1 == hashlib.sha256(b"\x02" + (1).to_bytes(8, "big") + raw1).hexdigest()

    # 3 leaves vs 4 leaves with trailing duplicate: MUST NOT collide (CVE-2012-2459 defense)
    leaves_3 = [b"L0", b"L1", b"L2"]
    leaves_4_dup = [b"L0", b"L1", b"L2", b"L2"]
    assert compute_rfc6962_raw_root(leaves_3) != compute_rfc6962_raw_root(leaves_4_dup)
    assert compute_rfc6962_merkle_root(leaves_3) != compute_rfc6962_merkle_root(leaves_4_dup)

    # Verify exact 3-leaf structure: H(0x01 || H(0x01 || H(0x00||L0) || H(0x00||L1)) || H(0x00||L2))
    h0 = hashlib.sha256(b"\x00L0").digest()
    h1 = hashlib.sha256(b"\x00L1").digest()
    h2 = hashlib.sha256(b"\x00L2").digest()
    h01 = hashlib.sha256(b"\x01" + h0 + h1).digest()
    expected_raw3 = hashlib.sha256(b"\x01" + h01 + h2).digest()
    assert compute_rfc6962_raw_root(leaves_3) == expected_raw3


def test_snapshot_manifest_roundtrip_and_tamper_detection() -> None:
    km = derive_vault_keys("manifest-secret", b"M" * 32)
    km_other = derive_vault_keys("other-secret", b"M" * 32)

    data_a = b"track-audio-bytes"
    cid_a = compute_chunk_id(data_a, km)
    entry_a = VaultFileEntry(
        rel_path="tracks/audio.bin",
        total_size=len(data_a),
        file_mac_hex=compute_file_mac("tracks/audio.bin", data_a, km),
        chunks=(VaultChunkRef(chunk_index=0, chunk_id=cid_a, plaintext_len=len(data_a)),),
    )
    entry_empty = VaultFileEntry(
        rel_path="notes/empty.txt",
        total_size=0,
        file_mac_hex=compute_file_mac("notes/empty.txt", b"", km),
        chunks=(),
    )

    manifest = build_snapshot_manifest(
        project_id="proj-nagar-1",
        snapshot_id="snap-001",
        parent_snapshot_id=None,
        created_at_utc="2026-11-16T08:00:00Z",
        chunk_size=1024,
        files=[entry_a, entry_empty],
        key_material=km,
        metadata={"device": "workstation-1"},
    )
    assert [f.rel_path for f in manifest.files] == ["notes/empty.txt", "tracks/audio.bin"]
    assert manifest.referenced_chunk_ids() == {cid_a}

    restored = VaultSnapshotManifest.from_json_bytes(manifest.to_json_bytes())
    verify_manifest_integrity(restored, km)

    # Wrong key -> VaultAuthenticationError
    with pytest.raises(VaultAuthenticationError):
        verify_manifest_integrity(restored, km_other)

    # Tampered manifest MAC -> VaultTamperDetectedError
    bad_mac = replace(restored, manifest_mac="f" * 64)
    with pytest.raises(VaultTamperDetectedError):
        verify_manifest_integrity(bad_mac, km)

    # Validation & structural tamper guards
    with pytest.raises(PersonalVaultError):
        build_snapshot_manifest(
            project_id="",
            snapshot_id="snap-x",
            parent_snapshot_id=None,
            created_at_utc="2026-11-16T08:00:00Z",
            chunk_size=1024,
            files=[],
            key_material=km,
        )
    with pytest.raises(PersonalVaultError):
        build_snapshot_manifest(
            project_id="proj",
            snapshot_id="snap-x",
            parent_snapshot_id=None,
            created_at_utc="2026-11-16T08:00:00Z",
            chunk_size=1024,
            files=[entry_empty, entry_empty],
            key_material=km,
        )


def test_manifest_deserialization_and_integrity_structural_branches() -> None:
    km: VaultKeyMaterial = derive_vault_keys("struct-secret", b"Z" * 32)
    entry_a = VaultFileEntry(
        rel_path="a.bin",
        total_size=3,
        file_mac_hex=compute_file_mac("a.bin", b"abc", km),
        chunks=(
            VaultChunkRef(chunk_index=0, chunk_id=compute_chunk_id(b"abc", km), plaintext_len=3),
        ),
    )
    manifest = build_snapshot_manifest(
        project_id="p1",
        snapshot_id="s1",
        parent_snapshot_id="s0",
        created_at_utc="2026-11-16T08:00:00Z",
        chunk_size=64,
        files=[entry_a],
        key_material=km,
    )

    with pytest.raises(VaultTamperDetectedError):
        VaultSnapshotManifest.from_json_bytes(b"{not-json")
    with pytest.raises(VaultTamperDetectedError):
        VaultSnapshotManifest.from_json_bytes(b"[1, 2, 3]")

    base = manifest.to_dict()
    with pytest.raises(VaultTamperDetectedError):
        VaultSnapshotManifest.from_dict({**base, "schema_version": 99})
    with pytest.raises(VaultTamperDetectedError):
        VaultSnapshotManifest.from_dict({**base, "files": "not-a-list"})
    with pytest.raises(VaultTamperDetectedError):
        VaultSnapshotManifest.from_dict({**base, "metadata": ["bad"]})
    with pytest.raises(VaultTamperDetectedError):
        VaultSnapshotManifest.from_dict({"schema_version": 1})

    with pytest.raises(VaultTamperDetectedError):
        VaultChunkRef.from_dict({"chunk_index": "bad"})
    with pytest.raises(VaultTamperDetectedError):
        VaultChunkRef.from_dict({"chunk_index": -1, "chunk_id": "a" * 64, "plaintext_len": 1})

    with pytest.raises(VaultTamperDetectedError):
        VaultFileEntry.from_dict(
            {"rel_path": "x.bin", "total_size": 0, "file_mac_hex": "a" * 64, "chunks": "bad"}
        )
    with pytest.raises(VaultTamperDetectedError):
        VaultFileEntry.from_dict(
            {"rel_path": "../x.bin", "total_size": 0, "file_mac_hex": "a" * 64, "chunks": []}
        )
    with pytest.raises(VaultTamperDetectedError):
        VaultFileEntry.from_dict({"rel_path": "x.bin"})
    with pytest.raises(VaultTamperDetectedError):
        VaultFileEntry.from_dict(
            {"rel_path": "x.bin", "total_size": -1, "file_mac_hex": "a" * 64, "chunks": []}
        )

    # Re-sign malformed internal invariants to test defense-in-depth checks
    import hmac as _hmac

    def _resign(m: VaultSnapshotManifest) -> VaultSnapshotManifest:
        sig = _hmac.new(
            km.mac_key,
            b"nagar-pcv-manifest-v1|" + m._canonical_signing_bytes(),
            hashlib.sha256,
        ).hexdigest()
        return replace(m, manifest_mac=sig)

    entry_b = VaultFileEntry(
        rel_path="b.bin",
        total_size=0,
        file_mac_hex=compute_file_mac("b.bin", b"", km),
        chunks=(),
    )
    unsorted_m = _resign(replace(manifest, files=(entry_b, entry_a)))
    with pytest.raises(VaultTamperDetectedError):
        verify_manifest_integrity(unsorted_m, km)

    bad_idx_entry = replace(
        entry_a,
        chunks=(
            VaultChunkRef(chunk_index=2, chunk_id=entry_a.chunks[0].chunk_id, plaintext_len=3),
        ),
    )
    with pytest.raises(VaultTamperDetectedError):
        verify_manifest_integrity(_resign(replace(manifest, files=(bad_idx_entry,))), km)

    bad_size_entry = replace(entry_a, total_size=999)
    with pytest.raises(VaultTamperDetectedError):
        verify_manifest_integrity(_resign(replace(manifest, files=(bad_size_entry,))), km)

    with pytest.raises(VaultTamperDetectedError):
        verify_manifest_integrity(_resign(replace(manifest, total_leaves=99)), km)

    with pytest.raises(VaultTamperDetectedError):
        verify_manifest_integrity(_resign(replace(manifest, merkle_root="0" * 64)), km)


def test_n4_n5_official_rfc5869_hkdf_and_pbkdf2_hmac_sha256_standard_vectors() -> None:
    """Verify HKDF-SHA256 against IETF RFC 5869 and PBKDF2-HMAC-SHA256 standard vectors."""
    from nexus_ai_agent.storage.personal_cloud_vault import init_streaming_file_mac

    # IETF RFC 5869 Appendix A.1 — Test Case 1 (Basic test case with SHA-256)
    ikm_tc1 = bytes([0x0B] * 22)
    salt_tc1 = bytes.fromhex("000102030405060708090a0b0c")
    info_tc1 = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    prk_tc1 = hkdf_extract_sha256(salt_tc1, ikm_tc1)
    okm_tc1 = hkdf_expand_sha256(prk_tc1, info_tc1, 42)
    assert prk_tc1.hex() == ("077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5")
    assert okm_tc1.hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )

    # IETF RFC 5869 Appendix A.3 — Test Case 3 (Zero-length salt and info with SHA-256)
    prk_tc3 = hkdf_extract_sha256(b"", ikm_tc1)
    okm_tc3 = hkdf_expand_sha256(prk_tc3, b"", 42)
    assert prk_tc3.hex() == ("19ef24a32c717b167f33a91d6f648bdf96596776afdb6377ac434c1c293ccb04")
    assert okm_tc3.hex() == (
        "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8"
    )

    # Official IETF RFC 7914 §11 (passwd, salt, c=1, dkLen=64) & NIST PBKDF2-HMAC-SHA256 vectors
    pbkdf2_vectors = (
        (
            b"passwd",
            b"salt",
            1,
            64,
            "55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc"
            "49ca9cccf179b645991664b39d77ef317c71b845b1e30bd509112041d3a19783",
        ),
        (
            b"password",
            b"salt",
            1,
            32,
            "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b",
        ),
        (
            b"password",
            b"salt",
            2,
            32,
            "ae4d0c95af6b46d32d0adff928f06dd02a303f8ef3c251dfd6e2d85a95474c43",
        ),
        (
            b"password",
            b"salt",
            4096,
            32,
            "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a",
        ),
        (
            b"passwordPASSWORDpassword",
            b"saltSALTsaltSALTsaltSALTsaltSALTsalt",
            4096,
            40,
            "348c89dbcbd32b2f32d814b8116e84cf2b17347ebc1800181c4e2a1fb8dd53e1c635518c7dac47e9",
        ),
    )
    for pwd, slt, rounds, dk_len, expected_hex in pbkdf2_vectors:
        derived = hashlib.pbkdf2_hmac("sha256", pwd, slt, rounds, dk_len)
        assert derived.hex() == expected_hex

    # Verify derive_vault_keys feeds standard PBKDF2-HMAC-SHA256 directly into RFC 5869 HKDF
    vault_salt = b"saltSALTsaltSALTsaltSALTsaltSALT"  # 32 bytes
    km = derive_vault_keys(b"passwordPASSWORDpassword", vault_salt, iterations=100_000)
    expected_ikm = hashlib.pbkdf2_hmac(
        "sha256", b"passwordPASSWORDpassword", vault_salt, 100_000, 64
    )
    expected_prk = hkdf_extract_sha256(vault_salt, expected_ikm)
    assert km.enc_key == hkdf_expand_sha256(expected_prk, b"nagar-pcv-v1/K_enc", 32)
    assert km.mac_key == hkdf_expand_sha256(expected_prk, b"nagar-pcv-v1/K_mac", 32)
    assert km.chunk_id_key == hkdf_expand_sha256(expected_prk, b"nagar-pcv-v1/K_chunk_id", 32)

    # Multi-block (>4 KiB) AEAD round-trip & streaming file MAC consistency
    multi_stride = bytes((i * 37) & 0xFF for i in range(9000))
    env_multi = encrypt_chunk(multi_stride, km, nonce=b"\xaa" * 12)
    assert decrypt_chunk(env_multi, km) == multi_stride

    smac = init_streaming_file_mac("clips/scene.mp4", len(multi_stride), km)
    smac.update(multi_stride[:4096])
    smac.update(multi_stride[4096:])
    assert smac.hexdigest() == compute_file_mac("clips/scene.mp4", multi_stride, km)
    with pytest.raises(PersonalVaultError):
        init_streaming_file_mac("clips/scene.mp4", -1, km)


def test_streaming_wire_encoders_guard_invalid_inputs_fail_closed() -> None:
    """The in-place wire encoders reject bad geometry/keys instead of emitting bytes."""
    import io
    import os

    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        encrypt_chunk_from_fd_to_wire,
        encrypt_plaintext_to_wire,
    )

    salt = generate_vault_salt()
    key_material = derive_vault_keys("streaming-guard-passphrase", salt, iterations=100_000)
    payload = os.urandom(4_000)

    # An unsupported AEAD suite fails closed before any buffer is built.
    with pytest.raises(VaultCryptoError, match="Unsupported AEAD cipher suite"):
        encrypt_plaintext_to_wire(payload, key_material, cipher_suite=99)
    with pytest.raises(VaultCryptoError, match="Unsupported AEAD cipher suite"):
        encrypt_chunk_from_fd_to_wire(
            file_handle=io.BytesIO(payload),
            plaintext_len=len(payload),
            key_material=key_material,
            cipher_suite=99,
        )

    # A Bad nonce supplied to the encoders fails closed before any buffer is built.
    for bad_nonce in (b"short", b"X" * 13):
        with pytest.raises(PersonalVaultError, match="nonce must be 12 bytes"):
            encrypt_plaintext_to_wire(payload, key_material, nonce=bad_nonce)
        with pytest.raises(PersonalVaultError, match="nonce must be 12 bytes"):
            encrypt_chunk_from_fd_to_wire(
                file_handle=io.BytesIO(payload),
                plaintext_len=len(payload),
                key_material=key_material,
                nonce=bad_nonce,
            )

    # Negative lengths are rejected by the shared wire-geometry validator.
    with pytest.raises(PersonalVaultError, match="plaintext_len must be non-negative"):
        encrypt_chunk_from_fd_to_wire(
            file_handle=io.BytesIO(b"x"),
            plaintext_len=-1,
            key_material=key_material,
        )

    # A short read means the source changed under us: fail closed, never upload.
    with pytest.raises(VaultTransportError, match="Short read while encrypting chunk"):
        encrypt_chunk_from_fd_to_wire(
            file_handle=io.BytesIO(b"only-ten!!"),
            plaintext_len=32,
            key_material=key_material,
        )


def test_streaming_wire_encoders_match_envelope_bytes_and_roundtrip() -> None:
    """``*_to_wire`` output is byte-identical to ``encrypt_chunk(...).to_bytes()``."""
    import io
    import os

    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        encrypt_chunk_from_fd_to_wire,
        encrypt_plaintext_to_wire,
    )

    salt = generate_vault_salt()
    key_material = derive_vault_keys("streaming-equivalence-passphrase", salt, iterations=100_000)
    nonce = b"E" * 12

    for size in (0, 1, 1_024, 65_535, 65_536, 100_000):
        plaintext = os.urandom(size)
        expected = encrypt_chunk(
            plaintext,
            key_material,
            chunk_index=0,
            nonce=nonce,
            pad_to_block_size=16,
        ).to_bytes()

        in_memory, memory_id = encrypt_plaintext_to_wire(
            plaintext, key_material, nonce=nonce, pad_to_block_size=16
        )
        from_fd, fd_id = encrypt_chunk_from_fd_to_wire(
            file_handle=io.BytesIO(plaintext),
            plaintext_len=size,
            key_material=key_material,
            nonce=nonce,
            pad_to_block_size=16,
        )
        assert bytes(in_memory) == expected
        assert bytes(from_fd) == expected
        assert memory_id == fd_id == compute_chunk_id(plaintext, key_material)

        envelope = EncryptedChunkEnvelope.from_bytes(bytes(from_fd))
        assert decrypt_chunk(envelope, key_material) == plaintext


def test_streaming_wire_encoders_feed_observers_plaintext_before_encryption() -> None:
    """Observers see the exact plaintext while it still occupies the ciphertext slot."""
    import io
    import os

    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        encrypt_chunk_from_fd_to_wire,
        encrypt_plaintext_to_wire,
    )

    salt = generate_vault_salt()
    key_material = derive_vault_keys("streaming-observer-passphrase", salt, iterations=100_000)
    plaintext = os.urandom(3_000)

    file_hasher = hashlib.sha256()
    chunk_hasher_cases: list[str] = []
    for chunk_hasher in (hashlib.sha256(), hashlib.sha256()):
        encrypt_plaintext_to_wire(
            plaintext,
            key_material,
            plaintext_observers=(hasher := chunk_hasher,),
        )
        chunk_hasher_cases.append(hasher.hexdigest())
    encrypt_chunk_from_fd_to_wire(
        file_handle=io.BytesIO(plaintext),
        plaintext_len=len(plaintext),
        key_material=key_material,
        plaintext_observers=(file_hasher,),
    )
    expected = hashlib.sha256(plaintext).hexdigest()
    assert chunk_hasher_cases == [expected, expected]
    assert file_hasher.hexdigest() == expected


def test_aead_inplace_seal_matches_high_level_encrypt_for_both_suites() -> None:
    """The in-place wire seal is byte-identical to ``encrypt_chunk(...).to_bytes()``.

    This is the memory-critical path: AES-256-GCM is sealed in place with no second
    payload-sized buffer, and it must produce exactly the same wire bytes as the
    high-level envelope API for both supported AEAD suites.
    """
    import io
    import os

    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        encrypt_chunk_from_fd_to_wire,
        encrypt_plaintext_to_wire,
    )

    salt = generate_vault_salt()
    km = derive_vault_keys("aead-inplace-parity", salt, iterations=100_000)
    nonce = b"F" * 12

    for suite in (CIPHER_AES_256_GCM, CIPHER_CHACHA20_POLY1305):
        for size in (0, 1, 1_024, 65_536):
            plaintext = os.urandom(size)
            expected = encrypt_chunk(
                plaintext,
                km,
                nonce=nonce,
                pad_to_block_size=16,
                cipher_suite=suite,
            ).to_bytes()

            in_memory, mem_id = encrypt_plaintext_to_wire(
                plaintext, km, nonce=nonce, pad_to_block_size=16, cipher_suite=suite
            )
            from_fd, fd_id = encrypt_chunk_from_fd_to_wire(
                file_handle=io.BytesIO(plaintext),
                plaintext_len=size,
                key_material=km,
                nonce=nonce,
                pad_to_block_size=16,
                cipher_suite=suite,
            )
            assert bytes(in_memory) == expected
            assert bytes(from_fd) == expected
            assert mem_id == fd_id == compute_chunk_id(plaintext, km)
            parsed = EncryptedChunkEnvelope.from_bytes(bytes(in_memory))
            assert decrypt_chunk(parsed, km) == plaintext


def test_vault_key_ring_matching_and_fail_closed_branches() -> None:
    from nexus_ai_agent.storage.personal_cloud_vault import (
        VaultIntegrityError,
        VaultKeyRing,
    )

    km = derive_vault_keys("ring-secret", b"R" * 32)
    data = b"ring-payload"
    cid = compute_chunk_id(data, km)
    manifest = build_snapshot_manifest(
        project_id="ring-proj",
        snapshot_id="ring-snap",
        parent_snapshot_id=None,
        created_at_utc="2026-11-16T08:00:00Z",
        chunk_size=1024,
        files=[
            VaultFileEntry(
                rel_path="a.bin",
                total_size=len(data),
                file_mac_hex=compute_file_mac("a.bin", data, km),
                chunks=(VaultChunkRef(chunk_index=0, chunk_id=cid, plaintext_len=len(data)),),
            )
        ],
        key_material=km,
    )

    # An empty ring holds nothing, and an empty master secret is refused outright.
    assert not VaultKeyRing.from_secrets()
    with pytest.raises(VaultAuthenticationError):
        VaultKeyRing.from_secrets("")
    with pytest.raises(VaultAuthenticationError):
        VaultKeyRing.from_secrets(b"")

    # A ring holding the right secret authenticates the manifest.
    ring = VaultKeyRing.from_secrets("other", "ring-secret", "ring-secret")
    assert ring.master_secrets == (b"other", b"ring-secret")
    ring.verify_manifest(manifest)
    assert ring.key_material_for(manifest) is not None

    # A ring that holds no matching key returns None and refuses fail-closed.
    stranger = VaultKeyRing.from_secrets("nope")
    assert stranger.key_material_for(manifest) is None
    with pytest.raises(VaultIntegrityError, match="No key in the vault key ring"):
        stranger.verify_manifest(manifest)

    # A manifest with an unparseable salt is treated as unheld, not a crash.
    bad_salt = replace(manifest, salt_hex="not-hex")
    assert stranger.key_material_for(bad_salt) is None


def test_aead_unsupported_cipher_suite_and_chunk_id_mismatch_fail_closed() -> None:
    from nexus_ai_agent.storage.personal_cloud_vault import crypto_chunk_envelope as cce

    km = derive_vault_keys("aead-guard-secret", b"G" * 32)
    plaintext = b"guard-payload"
    envelope = encrypt_chunk(plaintext, km)

    # An unknown cipher suite has no encryptor implementation.
    with pytest.raises(VaultCryptoError, match="Unsupported AEAD cipher suite"):
        cce._aead_encryptor(999, b"\x00" * 32)

    # A cipher suite the module does not support is refused before decryption.
    unsupported = replace(envelope, cipher_suite=999)
    with pytest.raises(VaultTamperDetectedError, match="Unsupported AEAD cipher suite"):
        decrypt_chunk(unsupported, km)

    # The envelope's own chunk_id no longer matches the decrypted plaintext.
    mismatched = replace(envelope, chunk_id="0" * 64)
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(mismatched, km)


def test_decrypt_rejects_valid_tag_with_mislabeled_chunk_id() -> None:
    """A sealed body whose recorded chunk_id is not its content's id is refused."""
    from nexus_ai_agent.storage.personal_cloud_vault import crypto_chunk_envelope as cce

    km = derive_vault_keys("mislabel-secret", b"L" * 32)
    plaintext = b"genuine-body"
    real_id = compute_chunk_id(plaintext, km)
    forged_id = "f" * 64
    assert forged_id != real_id

    nonce = cce._resolve_nonce(None)
    header = cce._build_envelope_header(forged_id, flags=cce._FLAG_NONE)
    ciphertext, tag = cce._aead_seal(
        km.enc_key,
        nonce,
        DEFAULT_CIPHER_SUITE,
        header=header,
        chunk_index=0,
        plaintext_len=len(plaintext),
        plaintext=plaintext,
    )
    forged = cce.EncryptedChunkEnvelope(
        chunk_id=forged_id,
        chunk_index=0,
        plaintext_len=len(plaintext),
        header=header,
        nonce=nonce,
        ciphertext=ciphertext,
        aead_tag=tag,
        key_id=km.key_id,
    )

    # The AEAD tag verifies and the header matches, yet the body's content does
    # not hash to the recorded chunk_id, so decryption must fail closed.
    with pytest.raises(VaultTamperDetectedError, match="does not match chunk_id"):
        decrypt_chunk(forged, km)


# --------------------------------------------------------------------------- #
# Checkpoint 1: DoS resource caps + legacy v1 -> v2 migration
# --------------------------------------------------------------------------- #


def _build_legacy_v1_envelope(
    plaintext: bytes,
    km: VaultKeyMaterial,
    *,
    chunk_index: int = 0,
    nonce: bytes | None = None,
    pad_to: int | None = None,
    pad_fill: bytes = b"\x00",
    chunk_id_override: str | None = None,
) -> bytes:
    """Reconstruct the exact v1 wire bytes (SHA-256 CTR + Encrypt-then-MAC).

    ``pad_to`` zero-pads the plaintext to a block boundary before encryption
    (v1's ``_FLAG_PADDED`` path); ``chunk_id_override`` lets a caller forge the
    recorded content id while keeping a valid MAC.
    """
    effective_nonce = nonce if nonce is not None else b"N" * 16
    chunk_id = (
        chunk_id_override if chunk_id_override is not None else compute_chunk_id(plaintext, km)
    )
    flags = 0x01 if pad_to is not None else 0x00
    body = plaintext if pad_to is None else plaintext + pad_fill * (-len(plaintext) % pad_to)
    header = b"NPCV" + bytes([1, flags]) + bytes.fromhex(chunk_id)
    # v1 SHA-256 CTR keystream over the (possibly padded) body.
    base = hashlib.sha256(km.enc_key + effective_nonce)
    out = bytearray(len(body))
    counter = 0
    for offset in range(0, len(body), 32):
        block = base.copy()
        block.update(struct.pack(">Q", counter))
        counter += 1
        ks = block.digest()
        for i in range(min(32, len(body) - offset)):
            out[offset + i] = body[offset + i] ^ ks[i]
    ciphertext = bytes(out)
    mac = hmac.new(km.mac_key, digestmod=hashlib.sha256)
    mac.update(header)
    mac.update(effective_nonce)
    mac.update(ciphertext)
    mac.update(struct.pack(">QQ", chunk_index, len(plaintext)))
    mac_tag = mac.digest()
    return (
        struct.pack(">H", len(header))
        + header
        + struct.pack(
            ">16s16sQQ32s", km.key_id, effective_nonce, chunk_index, len(plaintext), mac_tag
        )
        + ciphertext
    )


def _minimal_manifest(km: VaultKeyMaterial) -> VaultSnapshotManifest:
    """Build one authentic single-file manifest for the given key material."""
    data = b"manifest-body"
    cid = compute_chunk_id(data, km)
    return build_snapshot_manifest(
        project_id="cap-proj",
        snapshot_id="cap-snap",
        parent_snapshot_id=None,
        created_at_utc="2026-11-16T08:00:00Z",
        chunk_size=1024,
        files=[
            VaultFileEntry(
                rel_path="a.bin",
                total_size=len(data),
                file_mac_hex=compute_file_mac("a.bin", data, km),
                chunks=(VaultChunkRef(chunk_index=0, chunk_id=cid, plaintext_len=len(data)),),
            )
        ],
        key_material=km,
    )


def test_derive_vault_keys_rejects_excessive_kdf_iterations() -> None:
    salt = generate_vault_salt()
    with pytest.raises(VaultAuthenticationError, match="must not exceed"):
        derive_vault_keys("secret", salt, iterations=2_000_001)


def test_key_material_for_rejects_hostile_kdf_iterations() -> None:
    km = derive_vault_keys("ring-secret", generate_vault_salt())
    ring = VaultKeyRing.from_secrets("ring-secret")
    manifest = _minimal_manifest(km)
    hostile = replace(manifest, kdf_iterations=99_999_999)
    with pytest.raises(VaultAuthenticationError, match="exceeds the"):
        ring.key_material_for(hostile)


def test_manifest_from_json_bytes_rejects_oversized_payload() -> None:
    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        MAX_MANIFEST_JSON_BYTES,
    )

    oversized = b"{" + b"a" * (MAX_MANIFEST_JSON_BYTES + 1)
    with pytest.raises(VaultTamperDetectedError, match="too large"):
        VaultSnapshotManifest.from_json_bytes(oversized)


def test_envelope_from_bytes_rejects_oversized_payload() -> None:
    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        MAX_ENVELOPE_BYTES,
    )

    oversized = b"\x00" * (MAX_ENVELOPE_BYTES + 1)
    with pytest.raises(VaultTamperDetectedError, match="too large"):
        EncryptedChunkEnvelope.from_bytes(oversized)


def test_migrate_legacy_v1_envelope_round_trips_to_v2() -> None:
    km = derive_vault_keys("legacy-secret", generate_vault_salt())
    plaintext = b"legacy chunk payload that must survive migration"
    v1_wire = _build_legacy_v1_envelope(plaintext, km)

    # Sanity: the raw v1 bytes are recognised as version 1 and rejected by from_bytes.
    assert envelope_wire_version(v1_wire) == 1
    with pytest.raises(VaultTamperDetectedError, match="Legacy v1 envelope rejected"):
        EncryptedChunkEnvelope.from_bytes(v1_wire)

    migrated = migrate_legacy_v1_envelope_bytes(v1_wire, km)
    assert migrated.cipher_suite in (1, 2)
    # The migrated envelope is a genuine v2 AEAD container that decrypts to the plaintext.
    assert decrypt_chunk(migrated, km) == plaintext
    assert envelope_wire_version(migrated.to_bytes()) == 2


def test_migrate_legacy_v1_rejects_tampered_mac() -> None:
    km = derive_vault_keys("legacy-secret", generate_vault_salt())
    v1_wire = bytearray(_build_legacy_v1_envelope(b"tamper-me-please", km))
    v1_wire[-1] ^= 0x01  # flip a ciphertext bit -> MAC must fail
    with pytest.raises(VaultTamperDetectedError, match="MAC verification failed"):
        migrate_legacy_v1_envelope_bytes(bytes(v1_wire), km)


def test_migrate_legacy_v1_rejects_wrong_key_and_wrong_chunk_id() -> None:
    km = derive_vault_keys("legacy-secret", generate_vault_salt())
    other = derive_vault_keys("other-secret", generate_vault_salt())
    v1_wire = _build_legacy_v1_envelope(b"payload", km)
    with pytest.raises(VaultAuthenticationError, match="invalid key material"):
        migrate_legacy_v1_envelope_bytes(v1_wire, other)
    with pytest.raises(VaultTamperDetectedError, match="substitution detected"):
        migrate_legacy_v1_envelope_bytes(v1_wire, km, expected_chunk_id="0" * 64)


def test_migrate_legacy_v1_rejects_non_v1_and_malformed() -> None:
    km = derive_vault_keys("legacy-secret", generate_vault_salt())
    # A v2 envelope is not a legacy envelope.
    v2_wire = encrypt_chunk(b"modern", km).to_bytes()
    with pytest.raises(VaultTamperDetectedError, match="Not a legacy v1 envelope"):
        migrate_legacy_v1_envelope_bytes(v2_wire, km)
    # Wrong magic.
    bad_magic = b"XXXX" + b"\x01\x00" + b"\x00" * 32
    with pytest.raises(VaultTamperDetectedError, match="magic mismatch"):
        migrate_legacy_v1_envelope_bytes(struct.pack(">H", 38) + bad_magic, km)
    # Truncated.
    with pytest.raises(VaultTamperDetectedError, match="Truncated legacy envelope"):
        migrate_legacy_v1_envelope_bytes(b"\x00", km)
    # Header length claims fewer than the mandatory 38 bytes.
    with pytest.raises(VaultTamperDetectedError, match="Truncated or malformed"):
        migrate_legacy_v1_envelope_bytes(struct.pack(">H", 10) + b"\x00" * 10, km)
    # Metadata block is present but the ciphertext region is cut short.
    short_header = b"NPCV" + bytes([1, 0]) + b"\x00" * 32
    with pytest.raises(VaultTamperDetectedError, match="Truncated or malformed"):
        migrate_legacy_v1_envelope_bytes(struct.pack(">H", 38) + short_header, km)


def test_migrate_legacy_v1_enforces_envelope_size_cap() -> None:
    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        MAX_ENVELOPE_BYTES,
    )

    km = derive_vault_keys("legacy-secret", generate_vault_salt())
    with pytest.raises(VaultTamperDetectedError, match="Legacy envelope is too large"):
        migrate_legacy_v1_envelope_bytes(b"\x00" * (MAX_ENVELOPE_BYTES + 1), km)


def test_migrate_legacy_v1_rejects_declared_length_mismatch() -> None:
    km = derive_vault_keys("legacy-secret", generate_vault_salt())
    # Declare a plaintext length that disagrees with the unpadded ciphertext.
    wire = bytearray(_build_legacy_v1_envelope(b"abcdef", km))
    struct.pack_into(">Q", wire, 2 + 38 + 16 + 16 + 8, 999)
    with pytest.raises(VaultTamperDetectedError, match="length does not match"):
        migrate_legacy_v1_envelope_bytes(bytes(wire), km)


def test_migrate_legacy_v1_padded_round_trip_and_nonzero_padding() -> None:
    km = derive_vault_keys("legacy-secret", generate_vault_salt())
    # A genuinely padded v1 chunk (valid MAC) migrates back to its plaintext.
    padded = _build_legacy_v1_envelope(b"padded-body!", km, pad_to=16)
    migrated = migrate_legacy_v1_envelope_bytes(padded, km)
    assert decrypt_chunk(migrated, km) == b"padded-body!"

    # Non-zero padding bytes are rejected even though the MAC is valid.
    dirty = _build_legacy_v1_envelope(b"padded-body!", km, pad_to=16, pad_fill=b"\xaa")
    with pytest.raises(VaultTamperDetectedError, match="non-zero padding"):
        migrate_legacy_v1_envelope_bytes(dirty, km)


def test_migrate_legacy_v1_rejects_padded_shorter_than_plaintext() -> None:
    km = derive_vault_keys("legacy-secret", generate_vault_salt())
    wire = bytearray(_build_legacy_v1_envelope(b"abcdef", km))
    wire[2 + 5] = 0x01  # mark the padded flag while ciphertext == plaintext length
    struct.pack_into(">Q", wire, 2 + 38 + 16 + 16 + 8, 99)
    with pytest.raises(VaultTamperDetectedError, match="padded ciphertext shorter"):
        migrate_legacy_v1_envelope_bytes(bytes(wire), km)


def test_migrate_legacy_v1_rejects_forged_content_id_with_valid_mac() -> None:
    km = derive_vault_keys("legacy-secret", generate_vault_salt())
    forged = _build_legacy_v1_envelope(b"real-body", km, chunk_id_override="a" * 64)
    with pytest.raises(VaultTamperDetectedError, match="does not match chunk_id"):
        migrate_legacy_v1_envelope_bytes(forged, km)


def test_legacy_v1_ctr_crypt_rejects_bad_key_and_nonce() -> None:
    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        _legacy_v1_sha256_ctr_crypt,
    )

    with pytest.raises(VaultAuthenticationError, match="enc_key must be 32 bytes"):
        _legacy_v1_sha256_ctr_crypt(b"short", b"N" * 16, b"payload")
    with pytest.raises(VaultTamperDetectedError, match="legacy v1 nonce must be"):
        _legacy_v1_sha256_ctr_crypt(b"k" * 32, b"short", b"payload")
    assert _legacy_v1_sha256_ctr_crypt(b"k" * 32, b"N" * 16, b"") == b""


def test_envelope_wire_version_returns_none_for_short_or_foreign_bytes() -> None:
    assert envelope_wire_version(b"\x00") is None
    assert envelope_wire_version(b"\x00\x01\x02\x03\x04\x05\x06\x07") is None
    # Correct magic, unknown version -> reported as that version.
    assert envelope_wire_version(struct.pack(">H", 38) + b"NPCV" + bytes([7, 0])) == 7


def test_from_bytes_rejects_unknown_envelope_version() -> None:
    unknown = struct.pack(">H", 38) + b"NPCV" + bytes([9, 0]) + b"\x00" * 32
    with pytest.raises(VaultTamperDetectedError, match="Unsupported envelope version"):
        EncryptedChunkEnvelope.from_bytes(unknown)
