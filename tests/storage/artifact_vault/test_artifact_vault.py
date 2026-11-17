from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.storage.artifact_vault import (
    ArtifactVault,
    GearChunker,
    encode,
    gf_inverse,
    gf_multiply,
    matrix_invert,
    merkle_proof,
    merkle_root,
    reconstruct,
    verify_merkle_proof,
)


def test_chunking_is_deterministic_and_hashed() -> None:
    data = (b"abc123" * 1000) + b"tail"
    first = GearChunker(32, 64, 128).chunks(data)
    second = GearChunker(32, 64, 128).chunks(data)
    assert first == second
    assert b"".join(c.data for c in first) == data
    assert all(len(c.data) <= 128 and c.sha256 for c in first)


def test_cdc_validation() -> None:
    with pytest.raises(ValueError):
        GearChunker(10, 8, 16)
    with pytest.raises(ValueError):
        GearChunker(1, 3, 4)


def test_gf_and_matrix_inverse() -> None:
    assert gf_multiply(0x53, 0xCA) == 0x8F
    assert gf_inverse(0x53) == 0x8C
    matrix = np.array([[1, 2], [3, 4]], dtype=np.uint8)
    inverse = matrix_invert(matrix)
    product = np.zeros((2, 2), dtype=np.uint8)
    for i in range(2):
        for j in range(2):
            for k in range(2):
                product[i, j] ^= gf_multiply(int(matrix[i, k]), int(inverse[k, j]))
    assert np.array_equal(product, np.eye(2, dtype=np.uint8))


def test_erasure_reconstruct_any_k() -> None:
    data = bytes(range(251)) * 17 + b"end"
    shards = encode(data, 5, 3)
    recovered = reconstruct([shards[i] for i in (0, 2, 4, 6, 7)], len(data))
    assert recovered == data


def test_merkle_proof() -> None:
    leaves = [b"a", b"b", b"c", b"d", b"e"]
    root = merkle_root(leaves)
    for index, leaf in enumerate(leaves):
        assert verify_merkle_proof(leaf, index, merkle_proof(leaves, index), root)
    assert not verify_merkle_proof(b"x", 0, merkle_proof(leaves, 0), root)


def test_vault_heals_corrupted_and_missing_shards(tmp_path: Path) -> None:
    vault = ArtifactVault(tmp_path)
    data = b"Hyperion artifact" * 100
    receipt = vault.store_artifact("demo", data, k=3, m=2)
    directory = tmp_path / "demo"
    (directory / "shard-0000.bin").write_bytes(b"corrupt")
    (directory / "shard-0001.bin").unlink()
    healed = vault.verify_and_heal_artifact("demo")
    assert healed.verified and set(healed.healed) == {0, 1}
    assert vault.read_artifact("demo") == data
    assert receipt.merkle_root == healed.merkle_root
