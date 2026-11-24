"""Deterministic RFC and existing passport/drill implementation vectors."""

from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nexus_ai_agent.creative.artifact_passport import _seal
from nexus_ai_agent.creative.materializer import (
    merkle_inclusion_proof,
    merkle_root,
    verify_merkle_inclusion_proof,
)
from nexus_ai_agent.creative.packs.ed25519 import Ed25519Error, verify_ed25519
from nexus_ai_agent.storage.encrypted_backup_drill import _canonical, _merkle

ROOTS = {
    1: "305df59f9590c3c9ac63d2b2743c388e3792449078cebf7fb3dbe6471643b2b7",
    2: "60a53eed0de87a90c8e59427c59c46253c33a76a09502a51801300927b7e6bdc",
    3: "cf763a041c81ceef1578a6083f75c61bef2e0014f2a3e683a97fcfca5be7f19a",
    5: "00d21829a5503145348abcf712513eacf2a274211ad83e970202bb5b6d80b286",
    7: "0b007fb915eb9b2a146f54b1c86ec53b664f8e455b7660b0b6ee13edc0d921c0",
}


@pytest.mark.parametrize("count", list(ROOTS))
def test_merkle_fixed_roots_and_inclusion(count: int) -> None:
    leaves = [f"leaf-{i}".encode() for i in range(count)]
    root, size = merkle_root(leaves)
    assert (root, size) == (ROOTS[count], count)
    for i, leaf in enumerate(leaves):
        proof = merkle_inclusion_proof(leaves, i)
        assert verify_merkle_inclusion_proof(
            leaf, index=i, total_leaves=count, proof=proof, expected_root=root
        )
        assert not verify_merkle_inclusion_proof(
            leaf + b"!", index=i, total_leaves=count, proof=proof, expected_root=root
        )
        assert not verify_merkle_inclusion_proof(
            leaf, index=count, total_leaves=count, proof=proof, expected_root=root
        )


def test_merkle_domain_separation_and_leaf_order() -> None:
    a, b = b"a", b"b"
    left = hashlib.sha256(b"\x00" + a).digest()
    right = hashlib.sha256(b"\x00" + b).digest()
    node = hashlib.sha256(b"\x01" + left + right).hexdigest()
    assert merkle_root([a, b])[0] == node
    assert merkle_root([b"\x01" + left + right])[0] != node
    assert merkle_root([b, a])[0] != node


def test_rfc8032_ed25519_vector_and_tamper() -> None:
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert Ed25519PrivateKey.from_private_bytes(seed).sign(b"") == signature
    verify_ed25519(public, b"", signature)
    with pytest.raises(Ed25519Error):
        verify_ed25519(public, b"\x01", signature)
    with pytest.raises(Ed25519Error):
        verify_ed25519(public, b"", bytes([signature[0] ^ 1]) + signature[1:])


def test_passport_hmac_and_drill_manifest_vectors() -> None:
    passport = {"passport_version": 2, "job_id": "golden", "output_sha256": "a" * 64}
    seal = _seal(passport, b"fixed-test-key")
    assert seal == "bb09194ae406afdb92aaeeab236efea2fbe627e9b34d312869c6ff1c04ad6d83"
    assert _seal({**passport, "job_id": "Golden"}, b"fixed-test-key") != seal
    assert _merkle(b"abc") == "609f6e36d2405585188d5cfd761f407c7cc46a7d3f314c88270469dde315fcd1"
    assert (
        _canonical(
            {"schema_version": 1, "source_sha256": "ab", "row_counts": {"a": 2}, "nonce_hex": "00"}
        )
        == b'{"nonce_hex":"00","row_counts":{"a":2},"schema_version":1,"source_sha256":"ab"}'
    )


def _reference_tree(leaves: list[bytes]) -> bytes:
    if len(leaves) == 1:
        return hashlib.sha256(b"\x00" + leaves[0]).digest()
    split = 1 << ((len(leaves) - 1).bit_length() - 1)
    return hashlib.sha256(
        b"\x01" + _reference_tree(leaves[:split]) + _reference_tree(leaves[split:])
    ).digest()


def _reference_verify(leaf: bytes, index: int, count: int, proof: tuple[str, ...]) -> bytes:
    directions: list[bool] = []
    while count > 1:
        split = 1 << ((count - 1).bit_length() - 1)
        left = index < split
        directions.append(left)
        if left:
            count = split
        else:
            count -= split
            index -= split
    assert len(directions) == len(proof)
    result = hashlib.sha256(b"\x00" + leaf).digest()
    for left, sibling in zip(reversed(directions), proof, strict=True):
        other = bytes.fromhex(sibling)
        result = hashlib.sha256(b"\x01" + (result + other if left else other + result)).digest()
    return result


@pytest.mark.parametrize(
    "count,root",
    [
        (4, "bdd1c5ff55b19cb6b0e7c761bf9a6ccaa27fbbfc07b74f1fabb6e911a0bd2ab3"),
        (5, "00d21829a5503145348abcf712513eacf2a274211ad83e970202bb5b6d80b286"),
    ],
)
def test_independent_merkle_reference_and_audit_path(count: int, root: str) -> None:
    leaves = [f"leaf-{i}".encode() for i in range(count)]
    assert _reference_tree(leaves).hex() == root
    assert merkle_root(leaves)[0] == root
    for index, leaf in enumerate(leaves):
        proof = merkle_inclusion_proof(leaves, index)
        assert _reference_verify(leaf, index, count, proof).hex() == root


def test_aes_gcm_authenticates_all_inputs() -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes = AESGCM(bytes(32))
    nonce = bytes(12)
    data = b"W5 authenticated data"
    aad = b"manifest"
    sealed = aes.encrypt(nonce, data, aad)
    assert len(sealed) == len(data) + 16
    assert aes.decrypt(nonce, sealed, aad) == data
    for iv, payload, metadata in (
        (nonce, bytes([sealed[0] ^ 1]) + sealed[1:], aad),
        (nonce, sealed[:-1] + bytes([sealed[-1] ^ 1]), aad),
        (b"\x01" + nonce[1:], sealed, aad),
        (nonce, sealed, b"Manifest"),
    ):
        with pytest.raises(InvalidTag):
            aes.decrypt(iv, payload, metadata)


def test_bt709_luminance_and_fft_parseval() -> None:
    import numpy as np

    weights = np.array([0.2126, 0.7152, 0.0722])
    assert sum(weights) == pytest.approx(1.0, abs=1e-12)
    assert np.eye(3) @ weights == pytest.approx([0.2126, 0.7152, 0.0722], abs=1e-12)
    assert np.ones(3) @ weights == pytest.approx(1.0, abs=1e-12)
    n = np.arange(1024)
    signal = np.sin(2 * np.pi * 17 * n / 1024) + 0.3 * np.cos(2 * np.pi * 71 * n / 1024)
    time_energy = float(np.sum(np.abs(signal) ** 2))
    frequency_energy = float(np.sum(np.abs(np.fft.fft(signal)) ** 2) / signal.size)
    assert time_energy == pytest.approx(frequency_energy, rel=1e-10)


def test_gaussian_kernel_preserves_constant_signal() -> None:
    import numpy as np

    positions = np.arange(-4, 5, dtype=float)
    kernel = np.exp(-(positions**2) / 2.0)
    kernel /= np.sum(kernel)
    assert np.array_equal(kernel, kernel[::-1])
    assert np.all(kernel >= 0)
    assert np.sum(kernel) == pytest.approx(1.0, abs=1e-12)
    assert np.convolve(np.ones(100), kernel, mode="valid") == pytest.approx(np.ones(92), abs=1e-12)


def test_merkle_verifier_accepts_independent_fixed_rfc6962_audit_path() -> None:
    # Five leaves, index two: leaf-3, subtree(leaf-0, leaf-1), leaf-4.
    # Values computed independently from RFC 6962's prefixed SHA-256 rules.
    proof = (
        "f76836325aec5699d8d71f8e42e9d47c5c29b08059ba296384f7ca40ad3a40ae",
        "60a53eed0de87a90c8e59427c59c46253c33a76a09502a51801300927b7e6bdc",
        "ea9fc1a1b6e191b460d0d6306e3e870c173f39330f13cda1b70cfc72bdc398ba",
    )
    root = "00d21829a5503145348abcf712513eacf2a274211ad83e970202bb5b6d80b286"
    assert verify_merkle_inclusion_proof(
        b"leaf-2", index=2, total_leaves=5, proof=proof, expected_root=root
    )
    assert not verify_merkle_inclusion_proof(
        b"leaf-2", index=2, total_leaves=5, proof=tuple(reversed(proof)), expected_root=root
    )
