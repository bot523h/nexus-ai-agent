"""Cryptographic, chunking, and RFC 6962 Merkle tests for personal_cloud_vault."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from nexus_ai_agent.storage.personal_cloud_vault import (
    DEFAULT_CHUNK_SIZE,
    MAX_CHUNK_SIZE,
    MAX_DEFAULT_CHUNK_SIZE,
    MIN_PBKDF2_ITERATIONS,
    PBKDF2_ITERATIONS,
    EncryptedChunkEnvelope,
    PersonalVaultError,
    VaultAuthenticationError,
    VaultChunkRef,
    VaultFileEntry,
    VaultKeyMaterial,
    VaultSnapshotManifest,
    VaultTamperDetectedError,
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
    generate_vault_salt,
    hkdf_expand_sha256,
    hkdf_extract_sha256,
    normalize_relative_vault_path,
    sha256_ctr_crypt,
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


def test_known_answer_vector_kat_sha256_ctr_and_etm_hmac() -> None:
    """Immutable Known-Answer Test (KAT) locking byte-for-byte CTR + EtM HMAC outputs."""
    enc_key = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    mac_key = bytes.fromhex("202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f")
    cid_key = bytes.fromhex("404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f")
    key_id = bytes.fromhex("606162636465666768696a6b6c6d6e6f")
    salt = bytes.fromhex("707172737475767778797a7b7c7d7e7f808182838485868788898a8b8c8d8e8f")
    nonce = bytes.fromhex("a0a1a2a3a4a5a6a7a8a9aaabacadaeaf")
    plaintext = b"NAGAR-PCV-KAT-v1: deterministic stream & EtM vector"

    km = VaultKeyMaterial(
        enc_key=enc_key,
        mac_key=mac_key,
        chunk_id_key=cid_key,
        key_id=key_id,
        salt=salt,
        iterations=600_000,
    )
    env = encrypt_chunk(plaintext, km, chunk_index=7, nonce=nonce)

    expected_cid = "f0049d9c4018daaadc9022e05f15decf7f1de6702c8127f42af28c8e8ce3ec51"
    expected_hdr = "4e5043560100f0049d9c4018daaadc9022e05f15decf7f1de6702c8127f42af28c8e8ce3ec51"
    expected_ct = (
        "52f23a68d23ea92330ac6c61cb38b65e7589e7d26efe19e3a12360d7c053e046"
        "bdfa76273185d0a45bc60209fe4c17a91eac11"
    )
    expected_mac = "8ff987a85decb64cb4705165086932110194b45e3d1d096bda9768630699a186"

    assert env.chunk_id == expected_cid
    assert env.header.hex() == expected_hdr
    assert env.ciphertext.hex() == expected_ct
    assert env.mac_tag.hex() == expected_mac
    assert sha256_ctr_crypt(enc_key, nonce, plaintext).hex() == expected_ct
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

    # Forged non-zero decrypted padding tail signed with valid key -> VaultTamperDetectedError
    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import (
        _FLAG_PADDED,
        _build_envelope_header,
        _compute_etm_mac,
    )

    bad_padded_plain = b"hello" + (b"\xff" * 59)
    bad_ct = sha256_ctr_crypt(km.enc_key, padded_env.nonce, bad_padded_plain)
    hdr = _build_envelope_header(padded_env.chunk_id, flags=_FLAG_PADDED)
    forged_mac = _compute_etm_mac(
        km.mac_key,
        header=hdr,
        nonce=padded_env.nonce,
        ciphertext=bad_ct,
        chunk_index=0,
        plaintext_len=5,
    )
    forged_pad_env = EncryptedChunkEnvelope(
        chunk_id=padded_env.chunk_id,
        chunk_index=0,
        plaintext_len=5,
        header=hdr,
        nonce=padded_env.nonce,
        ciphertext=bad_ct,
        mac_tag=forged_mac,
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

    # 4) 1-bit flip in mac_tag -> VaultTamperDetectedError
    tampered_mac = replace(env, mac_tag=_flip_byte_bit(env.mac_tag, 31, 0x04))
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(tampered_mac, km_right, expected_chunk_index=3)

    # 5) 1-bit flip in key_id -> VaultTamperDetectedError
    tampered_kid = replace(env, key_id=_flip_byte_bit(env.key_id, 5, 0x02))
    with pytest.raises(VaultTamperDetectedError):
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
    with pytest.raises(VaultAuthenticationError):
        sha256_ctr_crypt(b"short", b"N" * 16, b"abc")
    with pytest.raises(VaultTamperDetectedError):
        sha256_ctr_crypt(km.enc_key, b"short", b"abc")

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

    # Crafted envelopes where MAC was forged WITH the key to hit structural defense-in-depth guards
    from nexus_ai_agent.storage.personal_cloud_vault.crypto_chunk_envelope import _compute_etm_mac

    bad_nonce_len_mac = _compute_etm_mac(
        km.mac_key,
        header=env.header,
        nonce=b"short",
        ciphertext=env.ciphertext,
        chunk_index=0,
        plaintext_len=env.plaintext_len,
    )
    forged_geom = EncryptedChunkEnvelope(
        chunk_id=env.chunk_id,
        chunk_index=0,
        plaintext_len=env.plaintext_len,
        header=env.header,
        nonce=b"short",
        ciphertext=env.ciphertext,
        mac_tag=bad_nonce_len_mac,
        key_id=km.key_id,
    )
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_geom, km)

    forged_non_hex_id = EncryptedChunkEnvelope(
        chunk_id="z" * 64,
        chunk_index=0,
        plaintext_len=env.plaintext_len,
        header=env.header,
        nonce=env.nonce,
        ciphertext=env.ciphertext,
        mac_tag=env.mac_tag,
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
        mac_tag=env.mac_tag,
        key_id=km.key_id,
    )
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_mismatched_id, km)

    # Header matches fake chunk_id and MAC signed with km.mac_key, so decrypted content != chunk_id
    fake_header = b"NPCV\x01\x00" + bytes.fromhex(other_id)
    fake_mac = _compute_etm_mac(
        km.mac_key,
        header=fake_header,
        nonce=env.nonce,
        ciphertext=env.ciphertext,
        chunk_index=0,
        plaintext_len=env.plaintext_len,
    )
    forged_content_mismatch = EncryptedChunkEnvelope(
        chunk_id=other_id,
        chunk_index=0,
        plaintext_len=env.plaintext_len,
        header=fake_header,
        nonce=env.nonce,
        ciphertext=env.ciphertext,
        mac_tag=fake_mac,
        key_id=km.key_id,
    )
    with pytest.raises(VaultTamperDetectedError):
        decrypt_chunk(forged_content_mismatch, km)


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
