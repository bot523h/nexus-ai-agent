"""Wide-coverage invariant and adversarial tests for the Hyperion vault, colour and board code.

Covers the real modules on this branch: GF(256) Reed-Solomon erasure coding and the RFC 6962
Merkle tree (``storage/artifact_vault``), Gear content-defined chunking, Bradford adaptation and
the filmic tonemap (``creative/speech_cinema/color_imaging``), and the semantic board merger
(``scripts/merge_agent_board.py``).  Run with ``pytest -p tests.creative.structlog_shim`` where
the package's optional dependencies are unavailable.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import random
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.speech_cinema import color_imaging as ci
from nexus_ai_agent.storage.artifact_vault import cdc_chunker as cdc
from nexus_ai_agent.storage.artifact_vault import galois_erasure as gf
from nexus_ai_agent.storage.artifact_vault import vault_manager as vm

ROOT = Path(__file__).resolve().parents[3]

# ----------------------------------------------------------------------------- Reed-Solomon

RS_CONFIGS = [(1, 1), (2, 1), (3, 2), (4, 3), (5, 2), (6, 4), (8, 4)]


def _payload(size: int, seed: int = 0) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(256) for _ in range(size))


@pytest.mark.parametrize(("k", "m"), RS_CONFIGS)
def test_rs_every_k_subset_reconstructs_exactly(k: int, m: int) -> None:
    data = _payload(173, seed=k * 10 + m)
    shards = gf.encode(data, k, m)
    for keep in itertools.combinations(range(k + m), k):
        assert gf.reconstruct([shards[i] for i in keep], len(data)) == data, keep


@pytest.mark.parametrize(("k", "m"), RS_CONFIGS)
def test_rs_generator_top_block_is_identity(k: int, m: int) -> None:
    gen = gf._generator(k, m)
    assert np.array_equal(gen[:k], np.eye(k, dtype=np.uint8))


@pytest.mark.parametrize(("k", "m"), [(3, 2), (4, 3), (8, 4)])
@pytest.mark.parametrize("pattern", ["first", "last", "alternating", "random"])
def test_rs_m_erasures_reconstruct_byte_for_byte(k: int, m: int, pattern: str) -> None:
    n = k + m
    data = _payload(409, seed=7)
    shards = gf.encode(data, k, m)
    if pattern == "first":
        erased = list(range(m))
    elif pattern == "last":
        erased = list(range(n - m, n))
    elif pattern == "alternating":
        erased = list(range(0, n, 2))[:m]
        erased += [i for i in range(1, n, 2) if len(erased) < m][: m - len(erased)]
    else:
        erased = random.Random(99).sample(range(n), m)
    survivors = [s for s in shards if s.index not in set(erased)]
    assert len(survivors) == k
    assert gf.reconstruct(survivors, len(data)) == data


@pytest.mark.parametrize(("k", "m"), RS_CONFIGS)
def test_rs_m_plus_one_erasures_fail_closed(k: int, m: int) -> None:
    data = _payload(64, seed=3)
    shards = gf.encode(data, k, m)
    with pytest.raises(ValueError, match="at least k"):
        gf.reconstruct(shards[: k - 1], len(data))


@pytest.mark.parametrize("payload", [b"\x00", b"\xff", b"\x00" * 257, b"\xff" * 257, _payload(997)])
def test_rs_edge_payloads_round_trip(payload: bytes) -> None:
    k, m = 4, 2
    shards = gf.encode(payload, k, m)
    assert gf.reconstruct(shards[-k:], len(payload)) == payload
    assert gf.reconstruct(shards[:k], len(payload)) == payload


def test_rs_prime_length_payload_round_trips_under_padding() -> None:
    data = _payload(1009, seed=5)  # prime length: forces padding in every configuration
    for k, m in [(3, 2), (7, 3)]:
        shards = gf.encode(data, k, m)
        assert gf.reconstruct(shards[m:], len(data)) == data


def test_rs_empty_payload_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        gf.encode(b"", 2, 1)


@pytest.mark.parametrize(("k", "m"), [(0, 1), (1, 0), (200, 56)])
def test_rs_invalid_parameters_are_refused(k: int, m: int) -> None:
    with pytest.raises(ValueError):
        gf.encode(b"x", k, m)


def test_rs_conflicting_shards_sharing_an_index_are_refused() -> None:
    data = _payload(200, seed=11)
    shards = gf.encode(data, 4, 3)
    forged = type(shards[0])(0, bytes(len(shards[0].data)), 7, 4, 3)
    with pytest.raises(ValueError, match="conflicting shards"):
        gf.reconstruct([shards[0], forged, shards[1], shards[2], shards[3]], len(data))


def test_rs_identical_duplicate_shards_are_tolerated() -> None:
    data = _payload(200, seed=12)
    shards = gf.encode(data, 4, 3)
    assert gf.reconstruct([shards[0], shards[0], *shards[1:4]], len(data)) == data


def test_rs_oversized_original_size_is_refused_not_truncated() -> None:
    data = _payload(200, seed=13)
    shards = gf.encode(data, 4, 3)
    with pytest.raises(ValueError, match="exceeds"):
        gf.reconstruct(shards[:4], len(data) + 50)


def test_rs_inconsistent_shard_sizes_are_refused() -> None:
    shards = gf.encode(_payload(200, seed=14), 4, 3)
    short = type(shards[1])(1, shards[1].data[:-1], 7, 4, 3)
    with pytest.raises(ValueError, match="inconsistent sizes"):
        gf.reconstruct([shards[0], short, shards[2], shards[3]])


# ------------------------------------------------------------------------ CDC chunking


def _chunker() -> cdc.GearChunker:
    return cdc.GearChunker(min_chunk_bytes=64, avg_chunk_bytes=256, max_chunk_bytes=4096)


def test_cdc_prefix_insertion_resynchronises_downstream_chunks() -> None:
    data = _payload(60_000, seed=21)
    base = [c.sha256 for c in _chunker().chunks(data)]
    shifted = [c.sha256 for c in _chunker().chunks(b"HEADER-INSERTED!" + data)]
    shared = len(set(base) & set(shifted))
    assert shared >= len(base) - 2, (shared, len(base))


@pytest.mark.parametrize("size", [1, 63, 64, 4096, 4097, 100_000])
def test_cdc_concatenated_chunks_reproduce_the_input(size: int) -> None:
    data = _payload(size, seed=size)
    chunks = _chunker().chunks(data)
    assert b"".join(c.data for c in chunks) == data
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_cdc_respects_min_and_max_chunk_bounds() -> None:
    chunks = _chunker().chunks(_payload(80_000, seed=22))
    assert all(64 <= c.length <= 4096 for c in chunks[:-1])
    assert 0 < chunks[-1].length <= 4096


def test_cdc_is_deterministic() -> None:
    data = _payload(30_000, seed=23)
    assert [c.sha256 for c in _chunker().chunks(data)] == [
        c.sha256 for c in _chunker().chunks(data)
    ]


def test_cdc_empty_input_yields_one_empty_chunk() -> None:
    chunks = _chunker().chunks(b"")
    assert len(chunks) == 1 and chunks[0].length == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"min_chunk_bytes": 0, "avg_chunk_bytes": 256, "max_chunk_bytes": 512}, "min_size"),
        ({"min_chunk_bytes": 64, "avg_chunk_bytes": 300, "max_chunk_bytes": 512}, "power of two"),
        ({"min_chunk_bytes": 512, "avg_chunk_bytes": 256, "max_chunk_bytes": 1024}, "min_size"),
    ],
)
def test_cdc_invalid_bounds_are_refused(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        cdc.GearChunker(**kwargs)


# ----------------------------------------------------------------- RFC 6962 Merkle tree


@pytest.mark.parametrize("n", [1, 2, 3, 5, 7, 8, 9, 16, 17, 33])
def test_merkle_every_leaf_proof_verifies_for_odd_and_even_trees(n: int) -> None:
    leaves = [bytes([i % 256]) * (i % 5 + 1) for i in range(n)]
    root = vm.merkle_root(leaves)
    for index, leaf in enumerate(leaves):
        assert vm.verify_merkle_proof(leaf, index, vm.merkle_proof(leaves, index), root)


def test_merkle_leaf_and_node_domains_are_separated() -> None:
    a, b = b"alpha", b"beta"
    two_leaf_root = vm.merkle_root([a, b])
    concatenated_leaf_root = vm.merkle_root([vm._leaf(a) + vm._leaf(b)])
    assert two_leaf_root != concatenated_leaf_root


def test_merkle_second_preimage_of_an_interior_node_is_rejected() -> None:
    leaves = [b"a", b"b", b"c", b"d"]
    interior = vm._node(vm._leaf(b"a"), vm._leaf(b"b")).hex()
    forged_leaf = vm._leaf(b"a") + vm._leaf(b"b")
    proof = vm.merkle_proof(leaves, 0)
    assert not vm.verify_merkle_proof(forged_leaf, 0, proof, vm.merkle_root(leaves))
    assert interior != vm.merkle_root(leaves)


def test_merkle_out_of_range_leaf_index_is_refused() -> None:
    leaves = [b"x", b"y", b"z"]
    with pytest.raises(IndexError):
        vm.merkle_proof(leaves, 3)
    with pytest.raises(IndexError):
        vm.merkle_proof(leaves, -1)


def test_merkle_one_bit_sibling_corruption_is_refused() -> None:
    leaves = [bytes([i]) * 4 for i in range(8)]
    root = vm.merkle_root(leaves)
    proof = vm.merkle_proof(leaves, 5)
    side, sibling = proof[0]
    flipped = bytearray(bytes.fromhex(sibling))
    flipped[0] ^= 0x01
    corrupted = [(side, flipped.hex()), *proof[1:]]
    assert not vm.verify_merkle_proof(leaves[5], 5, corrupted, root)


def test_merkle_wrong_leaf_is_refused() -> None:
    leaves = [b"p", b"q", b"r"]
    proof = vm.merkle_proof(leaves, 1)
    assert not vm.verify_merkle_proof(b"not-q", 1, proof, vm.merkle_root(leaves))


def test_merkle_empty_tree_root_is_the_empty_digest() -> None:
    import hashlib

    assert vm.merkle_root([]) == hashlib.sha256(b"").hexdigest()


# -------------------------------------------------------------- Bradford adaptation

TEMPERATURES = [2700.0, 3200.0, 4300.0, 5500.0, 6500.0, 9300.0]


ROUND_TRIP_PAIRS = [(t1, t2) for t1 in TEMPERATURES for t2 in TEMPERATURES if t1 != t2]


@pytest.mark.parametrize(("t1", "t2"), ROUND_TRIP_PAIRS)
def test_bradford_round_trip_recovers_linear_rgb_within_1e_12(t1: float, t2: float) -> None:
    image = np.random.default_rng(int(t1)).random((5, 5, 3)) + 0.02
    there = ci.bradford_adapt(image, t1, t2)
    back = ci.bradford_adapt(there, t2, t1)
    assert float(np.max(np.abs(back - image))) < 1e-12


@pytest.mark.parametrize("t", TEMPERATURES)
def test_bradford_equal_temperatures_are_identity(t: float) -> None:
    image = np.random.default_rng(3).random((4, 4, 3))
    assert float(np.max(np.abs(ci.bradford_adapt(image, t, t) - image))) < 1e-12


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_colour_nonfinite_pixels_are_refused(bad: float) -> None:
    image = np.full((2, 2, 3), 0.5)
    image[0, 0, 0] = bad
    with pytest.raises(ValueError, match="finite"):
        ci.bradford_adapt(image, 3200.0)


@pytest.mark.parametrize("shape", [(3,), (4, 4, 4), (2, 2)])
def test_colour_degenerate_shapes_are_refused(shape: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="RGB"):
        ci.bradford_adapt(np.ones(shape), 3200.0)


@pytest.mark.parametrize("kelvin", [999.9, 40000.1, float("inf")])
def test_colour_out_of_range_kelvin_is_refused(kelvin: float) -> None:
    with pytest.raises(ValueError):
        ci.bradford_adapt(np.ones((2, 2, 3)), kelvin)


def test_tonemap_refuses_negative_and_nonfinite_radiance() -> None:
    with pytest.raises(ValueError):
        ci.hdr_to_sdr_filmic_tonemap(np.array([[[-0.1, 0.2, 0.3]]]))
    with pytest.raises(ValueError):
        ci.hdr_to_sdr_filmic_tonemap(np.array([[[float("nan"), 0.2, 0.3]]]))


def test_tonemap_output_is_bounded_for_extreme_radiance() -> None:
    rng = np.random.default_rng(31)
    hdr = rng.random((16, 16, 3)) ** 4 * 1e6
    sdr = ci.hdr_to_sdr_filmic_tonemap(hdr)
    assert np.all(np.isfinite(sdr))
    assert float(sdr.min()) >= 0.0 and float(sdr.max()) <= 1.0


def test_tonemap_is_monotone_in_luminance() -> None:
    ramp = np.linspace(0.0, 50.0, 64)
    rgb = np.stack([ramp, ramp, ramp], axis=-1).reshape(1, 64, 3)
    out = ci.hdr_to_sdr_filmic_tonemap(rgb)[0, :, 0]
    assert np.all(np.diff(out) >= -1e-12)


# --------------------------------------------------------------- board merger stress


def _load_merger() -> Any:
    spec = importlib.util.spec_from_file_location(
        "merge_agent_board_wide", ROOT / "scripts" / "merge_agent_board.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mab = _load_merger()
STATUSES = sorted(mab.STATUS_RANK)


def _board(stamp: str, claims: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema": 2, "updated_at": stamp, "claims": claims}


def _synthetic_boards(count: int, seed: int) -> list[tuple[str, dict[str, Any]]]:
    rng = random.Random(seed)
    tasks = [f"task-{i:03d}" for i in range(25)]
    boards = []
    for b in range(count):
        chosen = rng.sample(tasks, rng.randint(5, 20))
        claims = [
            {
                "task": t,
                "status": rng.choice(STATUSES),
                "agent_branch": f"arena/b{b}",
                "files": sorted(rng.sample(["a.py", "b.py", "c.py", "d.py"], 2)),
            }
            for t in chosen
        ]
        boards.append((f"board{b}", _board(f"2026-11-13T0{b}:00:00Z", claims)))
    return boards


@pytest.mark.parametrize("count", [3, 6])
def test_merger_is_byte_identical_under_every_input_permutation(count: int) -> None:
    boards = _synthetic_boards(count, seed=count)
    reference = mab.render(mab.merge_boards(boards))
    orders = list(itertools.permutations(boards))
    for order in random.Random(1).sample(orders, min(24, len(orders))):
        assert mab.render(mab.merge_boards(list(order))) == reference


@pytest.mark.parametrize("count", [3, 6])
def test_merger_loses_no_claim_in_multi_way_merges(count: int) -> None:
    boards = _synthetic_boards(count, seed=10 + count)
    merged = mab.merge_boards(boards)
    assert mab.verify_no_loss(boards, merged) == sum(len(b["claims"]) for _, b in boards)


def test_merger_refuses_unknown_status() -> None:
    with pytest.raises(mab.MergeError, match="unknown claim status"):
        mab.merge_boards([("x", _board("t", [{"task": "t", "status": "hallucinated"}]))])


def test_merger_refuses_duplicate_task_within_one_board() -> None:
    claims = [{"task": "t", "status": "done"}, {"task": "t", "status": "queued"}]
    with pytest.raises(mab.MergeError, match="duplicate task"):
        mab.merge_boards([("x", _board("t", claims))])


def test_merger_refuses_claims_without_a_task_id() -> None:
    with pytest.raises(mab.MergeError, match="without a task id"):
        mab.merge_boards([("x", _board("t", [{"status": "done"}]))])


def test_merger_conflicting_transitions_resolve_monotonically_and_deterministically() -> None:
    expired = _board("2026-11-13T01:00:00Z", [{"task": "t", "status": "expired"}])
    done = _board("2026-11-13T00:00:00Z", [{"task": "t", "status": "done"}])
    forward = mab.merge_boards([("e", expired), ("d", done)])
    reverse = mab.merge_boards([("d", done), ("e", expired)])
    assert forward["claims"][0]["status"] == "done"
    assert mab.render(forward) == mab.render(reverse)


def test_merger_cli_refuses_malformed_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert mab.main([str(bad)]) == 1


def test_merger_cli_refuses_missing_file(tmp_path: Path) -> None:
    assert mab.main([str(tmp_path / "absent.json")]) == 1


def test_merger_cli_writes_canonical_utf8_output(tmp_path: Path) -> None:
    src = tmp_path / "board.json"
    src.write_text(
        json.dumps(_board("t", [{"task": "fa", "status": "done", "title_fa": "آزمون"}])),
        encoding="utf-8",
    )
    out = tmp_path / "merged.json"
    assert mab.main([str(src), "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n") and "آزمون" in text
    assert json.loads(text)["claims"][0]["task"] == "fa"


def test_default_target_is_continuous_across_6500_kelvin() -> None:
    # Regression: a 6500 K special case made the default-target output jump by ~0.04 (linear
    # RGB) between 6499.9 K and 6500 K.  The default must vary continuously with kelvin.
    img = np.random.default_rng(5).random((64, 64, 3)) * 0.8 + 0.1
    at_6500 = ci.white_balance_chromatic_adaptation(img, kelvin=6500.0)
    near = ci.white_balance_chromatic_adaptation(img, kelvin=6499.9)
    assert float(np.max(np.abs(at_6500 - near))) < 1e-3


def test_gray_world_on_a_neutral_image_is_near_identity() -> None:
    gray = np.full((8, 8, 3), 0.4)
    out = ci.white_balance_chromatic_adaptation(gray)
    assert np.allclose(out, gray, atol=1e-6)


def test_gray_world_refuses_singular_source_white() -> None:
    with pytest.raises(ValueError, match="unsuitable"):
        ci._adaptation_rgb(np.zeros(3), ci._D65)


def test_gf_scale_by_zero_is_the_zero_vector() -> None:
    out = gf._gf_scale(np.array([1, 2, 255], dtype=np.uint8), 0)
    assert out.tolist() == [0, 0, 0]


def test_merger_union_of_non_claim_lists_is_sorted_and_deduplicated() -> None:
    a = {"schema": 2, "updated_at": "1", "zones": [{"id": "z2"}, {"id": "z1"}]}
    b = {"schema": 2, "updated_at": "2", "zones": [{"id": "z1"}, {"id": "z3"}]}
    merged = mab.merge_boards([("a", a), ("b", b)])
    assert [z["id"] for z in merged["zones"]] == ["z1", "z2", "z3"]


def test_merger_refuses_empty_input_and_non_object_payloads() -> None:
    with pytest.raises(mab.MergeError, match="at least one board"):
        mab.merge_boards([])
    with pytest.raises(mab.MergeError, match="JSON object"):
        mab.merge_boards([("list", [1, 2])])  # type: ignore[list-item]


def test_verify_no_loss_detects_a_replaced_claim_record() -> None:
    board = _board("t", [{"task": "t", "status": "done", "agent_branch": "arena/x"}])
    merged = mab.merge_boards([("a", board)])
    merged["claims"][0]["status"] = "queued"
    with pytest.raises(mab.MergeError, match="a record for task"):
        mab.verify_no_loss([("a", board)], merged)


def test_verify_no_loss_detects_a_dropped_list_item() -> None:
    board = {"schema": 2, "updated_at": "t", "zones": [{"id": "z1"}, {"id": "z2"}]}
    merged = mab.merge_boards([("a", board)])
    merged["zones"] = merged["zones"][:1]
    with pytest.raises(mab.MergeError, match="zones"):
        mab.verify_no_loss([("a", board)], merged)


def test_merger_cli_prints_to_stdout_without_an_output_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "board.json"
    src.write_text(json.dumps(_board("t", [{"task": "q", "status": "done"}])), encoding="utf-8")
    assert mab.main([str(src)]) == 0
    assert json.loads(capsys.readouterr().out)["claims"][0]["task"] == "q"
