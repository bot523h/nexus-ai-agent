"""Branch coverage for artifact_vault: GF(256), Reed-Solomon, CDC, Merkle and on-disk vault."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from nexus_ai_agent.storage.artifact_vault import cdc_chunker as cdc
from nexus_ai_agent.storage.artifact_vault import galois_erasure as gf
from nexus_ai_agent.storage.artifact_vault import vault_manager as vm


def _payload(size: int, seed: int) -> bytes:
    return np.random.default_rng(seed).integers(0, 256, size, dtype=np.uint8).tobytes()


# -------------------------------------------------------------- GF(256)


def test_gf_multiply_rejects_operands_outside_the_field() -> None:
    with pytest.raises(ValueError, match=r"\[0, 255\]"):
        gf.gf_multiply(256, 1)
    with pytest.raises(ValueError):
        gf.gf_multiply(-1, 2)


def test_matrix_invert_identity_and_inverse_round_trip() -> None:
    eye = np.eye(3, dtype=np.uint8)
    np.testing.assert_array_equal(gf.matrix_invert(eye), eye)
    m = np.array([[1, 2], [3, 4]], dtype=np.uint8)
    inv = gf.matrix_invert(m)
    product = gf._matrix_multiply(m, inv)
    np.testing.assert_array_equal(product, np.eye(2, dtype=np.uint8))


def test_matrix_invert_refuses_non_square_and_singular_matrices() -> None:
    with pytest.raises(ValueError, match="square"):
        gf.matrix_invert(np.ones((2, 3), dtype=np.uint8))
    with pytest.raises(ValueError, match="singular"):
        gf.matrix_invert(np.zeros((2, 2), dtype=np.uint8))


def test_generator_matrix_has_identity_top_and_is_shaped_k_plus_m() -> None:
    gen = gf._generator(4, 2)
    assert gen.shape == (6, 4)
    np.testing.assert_array_equal(gen[:4], np.eye(4, dtype=np.uint8))


# ---------------------------------------------------------- Reed-Solomon


def test_shard_properties_report_parity_and_identity() -> None:
    shards = gf.encode(_payload(300, 21), 4, 2)
    assert shards[0].is_parity is False and shards[0].shard_index == 0
    assert shards[5].is_parity is True and shards[5].shard_index == 5


def test_reconstruct_refuses_too_few_duplicate_and_inconsistent_shards() -> None:
    data = _payload(400, 22)
    shards = gf.encode(data, 4, 2)
    with pytest.raises(ValueError, match="at least k"):
        gf.reconstruct_from_shards(shards[:2], len(data))
    with pytest.raises(ValueError, match="duplicate"):
        gf.reconstruct_from_shards([shards[0], shards[0], shards[1], shards[2]], len(data))
    truncated = dataclasses.replace(shards[1], data=shards[1].data[:-1])
    with pytest.raises(ValueError, match="inconsistent sizes"):
        gf.reconstruct_from_shards([shards[0], truncated, shards[2], shards[3]], len(data))


def test_encode_erasure_shards_is_the_public_alias_of_encode() -> None:
    data = _payload(128, 23)
    a = gf.encode_erasure_shards(data, 3, 2)
    b = gf.encode(data, 3, 2)
    assert [s.data for s in a] == [s.data for s in b]


def test_reconstruct_without_original_length_returns_padded_payload() -> None:
    data = _payload(64, 24)
    shards = gf.encode(data, 4, 2)
    recovered = gf.reconstruct_from_shards(shards)
    assert recovered.startswith(data)


# --------------------------------------------------------------- CDC


def test_chunk_length_and_hash_properties() -> None:
    chunk = cdc.chunk_bytes(b"abc")[0]
    assert chunk.length == 3
    assert chunk.hash == chunk.sha256


def test_gear_chunker_accepts_explicit_chunk_size_keywords() -> None:
    chunker = cdc.GearChunker(min_chunk_bytes=64, avg_chunk_bytes=128, max_chunk_bytes=256)
    data = _payload(4096, 25)
    chunks = chunker.chunks(data)
    assert b"".join(c.data for c in chunks) == data
    assert all(c.length <= 256 for c in chunks)


def test_split_content_defined_chunks_reassembles_the_input() -> None:
    data = _payload(50_000, 26)
    chunks = cdc.split_content_defined_chunks(
        data, min_chunk_bytes=512, avg_chunk_bytes=1024, max_chunk_bytes=4096
    )
    assert b"".join(c.data for c in chunks) == data


# ----------------------------------------------------------- Merkle + vault


def test_merkle_root_of_empty_leaves_and_out_of_range_proof_index() -> None:
    assert isinstance(merkle_root_empty := vm.merkle_root([]), str)
    assert len(merkle_root_empty) == 64
    with pytest.raises(IndexError):
        vm.merkle_proof([b"a", b"b"], 5)


def test_merkle_single_leaf_proof_is_empty_and_verifies() -> None:
    root = vm.merkle_root([b"only"])
    proof = vm.merkle_proof([b"only"], 0)
    assert proof == []
    assert vm.verify_merkle_proof(b"only", 0, proof, root)


def test_vault_round_trip_persists_manifest_and_shards(tmp_path) -> None:
    vault = vm.ArtifactVault(tmp_path)
    data = _payload(3000, 27)
    receipt = vault.store_artifact("roundtrip", data, 4, 2)
    assert len(receipt.root) == 64
    manifest = vault.load_manifest("roundtrip")
    assert manifest is not None
    assert vault.read_artifact("roundtrip") == data


def test_vault_heal_is_a_no_op_for_a_healthy_artifact(tmp_path) -> None:
    vault = vm.ArtifactVault(tmp_path)
    data = _payload(2000, 28)
    vault.store_artifact("healthy", data, 3, 2)
    vault.verify_and_heal_artifact("healthy")
    assert vault.read_artifact("healthy") == data


def test_vault_refuses_to_read_an_unknown_artifact(tmp_path) -> None:
    with pytest.raises((FileNotFoundError, ValueError, OSError)):
        vm.ArtifactVault(tmp_path).read_artifact("does-not-exist")


def _shard_files(directory):
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix != ".json")


def test_heal_refuses_when_more_than_m_shards_are_corrupted(tmp_path) -> None:
    vault = vm.ArtifactVault(tmp_path)
    vault.store_artifact("doomed", _payload(4000, 29), 4, 2)
    for victim in _shard_files(vault._dir("doomed"))[:3]:  # m + 1 = 3 corrupted shards
        victim.write_bytes(b"\x00" * victim.stat().st_size)
    with pytest.raises(ValueError, match="fewer than k"):
        vault.verify_and_heal_artifact("doomed")
    with pytest.raises(ValueError, match="unavailable"):
        vault.read_artifact("doomed")


def test_failed_atomic_write_leaves_no_temporary_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(vm.os, "replace", _boom)
    vault = vm.ArtifactVault(tmp_path)
    with pytest.raises(OSError, match="simulated"):
        vault.store_artifact("atomic", _payload(512, 30), 2, 1)
    leftovers = [p.name for p in tmp_path.rglob("*") if p.is_file() and p.name.endswith(".tmp")]
    assert leftovers == []


def test_reconstruct_entry_point_refuses_fewer_than_k_shards() -> None:
    shards = gf.encode(_payload(200, 31), 4, 2)
    with pytest.raises(ValueError, match="at least k"):
        gf.reconstruct(shards[:2])
