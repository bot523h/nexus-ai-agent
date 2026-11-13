"""Integrity hardening: Merkle index binding, codeword cross-check, digest-gated healing."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from nexus_ai_agent.storage.artifact_vault import galois_erasure as gf
from nexus_ai_agent.storage.artifact_vault import vault_manager as vm


def _leaves(n: int) -> list[bytes]:
    return [bytes([i % 256]) * (i % 7 + 1) for i in range(n)]


@pytest.mark.parametrize("n", [1, 2, 3, 5, 8, 13, 31])
def test_merkle_index_binding_accepts_the_true_index_and_refuses_every_other(n: int) -> None:
    leaves = _leaves(n)
    root = vm.merkle_root(leaves)
    for i in range(n):
        proof = vm.merkle_proof(leaves, i)
        assert vm.verify_merkle_proof(leaves[i], i, proof, root, total_leaves=n)
        for j in range(n):
            if j != i and leaves[j] != leaves[i]:
                assert not vm.verify_merkle_proof(leaves[j], i, proof, root, total_leaves=n)


def test_merkle_negative_index_is_refused() -> None:
    leaves = _leaves(4)
    root = vm.merkle_root(leaves)
    assert not vm.verify_merkle_proof(leaves[0], -1, vm.merkle_proof(leaves, 0), root)


def test_merkle_wrong_parity_index_is_refused_by_the_bit_path() -> None:
    # Leaf 0 sits on the left, so its first step is a "right" sibling.  Claiming index 1
    # (a right child) contradicts that step, whatever the tree size.
    leaves = _leaves(4)
    root = vm.merkle_root(leaves)
    proof = vm.merkle_proof(leaves, 0)
    assert proof[0][0] == "right"
    assert not vm.verify_merkle_proof(leaves[0], 1, proof, root, total_leaves=4)


def test_merkle_index_out_of_range_is_refused_when_the_size_is_known() -> None:
    leaves = _leaves(5)
    root = vm.merkle_root(leaves)
    proof = vm.merkle_proof(leaves, 4)
    assert vm.verify_merkle_proof(leaves[4], 4, proof, root, total_leaves=5)
    assert not vm.verify_merkle_proof(leaves[4], 5, proof, root, total_leaves=5)
    assert not vm.verify_merkle_proof(leaves[4], 4, proof, root, total_leaves=4)


def test_merkle_truncated_or_extended_proof_is_refused_when_the_size_is_known() -> None:
    leaves = _leaves(8)
    root = vm.merkle_root(leaves)
    proof = vm.merkle_proof(leaves, 3)
    assert not vm.verify_merkle_proof(leaves[3], 3, proof[:-1], root, total_leaves=8)
    extra = (*proof, proof[-1])
    assert not vm.verify_merkle_proof(leaves[3], 3, list(extra), root, total_leaves=8)


def test_merkle_unknown_side_label_is_refused() -> None:
    leaves = _leaves(4)
    root = vm.merkle_root(leaves)
    proof = vm.merkle_proof(leaves, 1)
    bad = [("middle", proof[0][1]), *proof[1:]]
    assert not vm.verify_merkle_proof(leaves[1], 1, bad, root, total_leaves=4)


def test_reconstruct_refuses_a_k_plus_one_set_with_one_flipped_byte() -> None:
    data = os.urandom(300)
    shards = list(gf.encode(data, 4, 2))
    assert gf.reconstruct(shards, len(data)) == data
    victim = shards[5]
    corrupt = replace(victim, data=bytes([victim.data[0] ^ 0x01]) + victim.data[1:])
    with pytest.raises(ValueError, match="inconsistent codeword"):
        gf.reconstruct([*shards[:4], corrupt], len(data))


def test_reconstruct_accepts_every_consistent_redundant_set() -> None:
    data = os.urandom(129)
    shards = list(gf.encode(data, 3, 3))
    assert gf.reconstruct(shards, len(data)) == data
    assert gf.reconstruct(shards[2:], len(data)) == data


def _corrupt(path: Path) -> None:
    raw = bytearray(path.read_bytes())
    raw[0] ^= 0xFF
    path.write_bytes(bytes(raw))


def test_vault_heals_up_to_m_corrupted_shards_from_the_remaining_verified_ones(
    tmp_path: Path,
) -> None:
    data = os.urandom(4096)
    vault = vm.ArtifactVault(tmp_path)
    vault.store_artifact("art", data, 8, 4)
    directory = vault._dir("art")
    shard_files = sorted(p for p in directory.iterdir() if p.name.startswith("shard-"))
    for path in shard_files[:4]:
        _corrupt(path)
    receipt = vault.verify_and_heal_artifact("art")
    assert receipt.verified
    assert len(receipt.healed) == 4
    assert vault.read_artifact("art") == data


def test_vault_refuses_fail_closed_when_m_plus_one_shards_are_corrupted(tmp_path: Path) -> None:
    data = os.urandom(4096)
    vault = vm.ArtifactVault(tmp_path)
    vault.store_artifact("art", data, 8, 4)
    directory = vault._dir("art")
    shard_files = sorted(p for p in directory.iterdir() if p.name.startswith("shard-"))
    for path in shard_files[:5]:
        _corrupt(path)
    with pytest.raises(ValueError):
        vault.verify_and_heal_artifact("art")
    with pytest.raises(ValueError):
        vault.read_artifact("art")


def test_vault_read_refuses_a_manifest_whose_merkle_root_no_longer_matches(tmp_path: Path) -> None:
    data = os.urandom(512)
    vault = vm.ArtifactVault(tmp_path)
    vault.store_artifact("art", data, 4, 2)
    manifest_path = vault._dir("art") / "manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["merkle_root"] = "00" * 32
    manifest_path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    with pytest.raises(ValueError, match="Merkle root"):
        vault.read_artifact("art")
    with pytest.raises(ValueError, match="Merkle root"):
        vault.verify_and_heal_artifact("art")


def test_merkle_proof_with_missing_trailing_step_is_refused_when_size_is_known() -> None:
    leaves = _leaves(3)
    root = vm.merkle_root(leaves)
    full = vm.merkle_proof(leaves, 2)
    assert vm.verify_merkle_proof(leaves[2], 2, full, root, total_leaves=3)
    assert not vm.verify_merkle_proof(leaves[2], 2, [], root, total_leaves=3)
    assert not vm.verify_merkle_proof(leaves[2], 2, [], root, total_leaves=1)
