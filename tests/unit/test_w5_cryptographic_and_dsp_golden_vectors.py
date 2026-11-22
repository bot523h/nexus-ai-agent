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
