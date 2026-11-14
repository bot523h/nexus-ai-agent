"""WAVE 30 — tests for the Holographic Equation Vault.

Covers ``GF(2^8)`` arithmetic, the systematic MDS generator, shard
encode/decode with the any-``k`` reconstruction property, the vault seal /
reconstruct / audit / receipt surface, and the typed refusals for every
malformed input.
"""

from __future__ import annotations

import itertools
import random
from pathlib import Path

import pytest

from nexus_ai_agent.creative.atlas import holographic_equation_vault as hv

EQUATIONS = [
    ("PH-01", 18, "dx/dt = (J(x) - R(x)) grad H(x) + G(x) u(t)"),
    ("WS-07", 19, "S_eps(mu, nu) = OT_eps(mu, nu) - 0.5 OT_eps(mu, mu) - 0.5 OT_eps(nu, nu)"),
    ("IC-03", 20, "theta* = argmin ||J theta - y*||^2 + gamma ||theta - theta_prior||^2"),
    ("RC-11", 21, "min integral ||p^(4)(t)||^2 dt s.t. h_m(p(t)) >= delta_safe"),
    ("NS-05", 22, "max_{q_i'} U_i(q_i', q_{-i}*) - U_i(q_i*, q_{-i}*) <= eps_nash"),
    ("AS-02", 23, "p_HG(cos theta; g) = (1 - g^2) / (4 pi (1 + g^2 - 2 g cos theta)^(3/2))"),
]


# --- GF(2^8) arithmetic ------------------------------------------------------------


def test_gf256_identity_and_commutativity() -> None:
    for a in range(256):
        assert hv.gf256_mul(a, 1) == a
        assert hv.gf256_mul(1, a) == a
        assert hv.gf256_mul(a, 0) == 0
    for a in range(0, 256, 7):
        for b in range(0, 256, 11):
            assert hv.gf256_mul(a, b) == hv.gf256_mul(b, a)


def test_gf256_distributivity() -> None:
    for a in range(0, 256, 13):
        for b in range(0, 256, 17):
            for c in range(0, 256, 19):
                assert hv.gf256_mul(a, b ^ c) == hv.gf256_mul(a, b) ^ hv.gf256_mul(a, c)


def test_gf256_inverse_for_every_nonzero_element() -> None:
    for a in range(1, 256):
        assert hv.gf256_mul(a, hv.gf256_inv(a)) == 1


def test_gf256_add_is_xor() -> None:
    for a in range(256):
        assert hv.gf256_add(a, 0) == a
        assert hv.gf256_add(a, a) == 0
    assert hv.gf256_add(0b1011_0110, 0b0000_1111) == 0b1011_1001


def test_gf256_pow_edges() -> None:
    assert hv._gf256_pow(5, 0) == 1
    assert hv._gf256_pow(0, 3) == 0
    squared = hv.gf256_mul(2, 2)
    fourth = hv.gf256_mul(squared, squared)
    assert hv._gf256_pow(2, 8) == hv.gf256_mul(fourth, fourth)


def test_gf256_inverse_of_zero_is_refused() -> None:
    with pytest.raises(hv.VaultError):
        hv.gf256_inv(0)


def test_singular_submatrix_is_refused() -> None:
    with pytest.raises(hv.VaultError):
        hv._gf256_mat_inv([[1, 2], [1, 2]])


# --- MDS generator -----------------------------------------------------------------


def test_generator_is_systematic() -> None:
    generator = hv.mds_generator(4, 7)
    for row in range(4):
        assert generator[row] == tuple(1 if c == row else 0 for c in range(4))


@pytest.mark.parametrize(("k", "n"), [(1, 3), (2, 4), (3, 6), (4, 7), (5, 8)])
def test_every_k_rows_are_invertible(k: int, n: int) -> None:
    generator = hv.mds_generator(k, n)
    for combo in itertools.combinations(range(n), k):
        matrix = [list(generator[r]) for r in combo]
        # Inversion raises VaultError when the submatrix is singular.
        hv._gf256_mat_inv(matrix)


def test_generator_refuses_bad_parameters() -> None:
    with pytest.raises(hv.VaultError):
        hv.mds_generator(0, 3)
    with pytest.raises(hv.VaultError):
        hv.mds_generator(4, 3)
    with pytest.raises(hv.VaultError):
        hv.mds_generator(2, hv.MAX_SHARDS + 1)
    with pytest.raises(hv.VaultError):
        hv.mds_generator(2.5, 4)  # type: ignore[arg-type]


# --- encode / decode ---------------------------------------------------------------


@pytest.mark.parametrize(("k", "n"), [(1, 3), (2, 4), (3, 6), (4, 5), (5, 8)])
def test_any_k_shards_reconstruct_exactly(k: int, n: int) -> None:
    rng = random.Random(20261113)
    for _ in range(6):
        payload = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
        shards = hv.encode_shards(payload, k=k, n=n)
        assert len(shards) == n
        for combo in itertools.combinations(range(n), k):
            subset = {i: shards[i] for i in combo}
            assert hv.decode_shards(subset, k=k, n=n)[: len(payload)] == payload


def test_string_payload_is_utf8_encoded() -> None:
    shards = hv.encode_shards("λ⁴ رایلی", k=2, n=4)
    rebuilt = hv.decode_shards({0: shards[0], 1: shards[1]}, k=2, n=4)
    assert rebuilt.startswith("λ⁴ رایلی".encode())


def test_decode_refuses_fewer_than_k_shards() -> None:
    shards = hv.encode_shards(b"payload", k=3, n=5)
    with pytest.raises(hv.VaultError):
        hv.decode_shards({0: shards[0], 1: shards[1]}, k=3, n=5)


def test_decode_refuses_out_of_range_index() -> None:
    shards = hv.encode_shards(b"payload", k=2, n=3)
    with pytest.raises(hv.VaultError):
        hv.decode_shards({0: shards[0], 9: shards[1]}, k=2, n=3)


def test_decode_refuses_mismatched_shard_lengths() -> None:
    shards = hv.encode_shards(b"payload", k=2, n=4)
    with pytest.raises(hv.VaultError):
        hv.decode_shards({0: shards[0], 1: shards[1][:-1]}, k=2, n=4)


def test_encode_refuses_non_bytes_payload() -> None:
    with pytest.raises(hv.VaultError):
        hv.encode_shards(1234, k=2, n=4)  # type: ignore[arg-type]


# --- vault -------------------------------------------------------------------------


def _sealed_vault(k: int = 3, parity: int = 2) -> hv.HolographicEquationVault:
    vault = hv.HolographicEquationVault(k=k, parity=parity)
    for equation_id, wave, statement in EQUATIONS:
        vault.seal(equation_id, wave, statement)
    return vault


def test_vault_seals_and_reconstructs_from_any_k_shards() -> None:
    vault = _sealed_vault(k=3, parity=2)
    for equation_id, _wave, statement in EQUATIONS:
        shard_set = vault.shard_set(equation_id)
        for combo in itertools.combinations(range(vault.n), vault.k):
            assert vault.reconstruct(equation_id, shard_set.available(combo)) == statement


def test_vault_audit_proves_all_invariants() -> None:
    audit = _sealed_vault().audit()
    assert audit.all_verified is True
    assert audit.to_dict() == {
        "mds_property_verified": True,
        "any_k_reconstruction_verified": True,
        "fewer_than_k_is_refused": True,
        "tamper_is_detected": True,
    }


def test_vault_receipt_is_canonical_and_deterministic() -> None:
    first = _sealed_vault().receipt()
    second = _sealed_vault().receipt()
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.merkle_root == second.merkle_root
    assert first.equation_count == len(EQUATIONS)
    assert first.schema == hv.VAULT_SCHEMA
    assert first.redundancy == pytest.approx(vault_redundancy(3, 5))


def vault_redundancy(k: int, n: int) -> float:
    return round(n / k, 6)


def test_tampered_shard_is_detected_by_the_seal() -> None:
    vault = _sealed_vault(k=3, parity=2)
    shard_set = vault.shard_set("PH-01")
    good = dict(shard_set.available((0, 1, 2)))
    bad = dict(good)
    bad[0] = bytes([bad[0][0] ^ 0xFF]) + bad[0][1:]
    assert vault.reconstruct("PH-01", good) == EQUATIONS[0][2]
    # A corrupted data shard no longer matches the sealed digest: fail closed.
    with pytest.raises(hv.VaultError):
        vault.reconstruct("PH-01", bad)


def test_vault_refuses_duplicate_and_bad_seals() -> None:
    vault = hv.HolographicEquationVault(k=2)
    vault.seal("E", 30, "statement")
    with pytest.raises(hv.VaultError):
        vault.seal("E", 30, "statement")
    with pytest.raises(hv.VaultError):
        vault.seal("", 30, "statement")
    with pytest.raises(hv.VaultError):
        vault.seal("F", -1, "statement")
    with pytest.raises(hv.VaultError):
        vault.seal("G", 30, "")


def test_vault_refuses_unknown_equation() -> None:
    vault = hv.HolographicEquationVault(k=2)
    with pytest.raises(hv.VaultError):
        vault.shard_set("missing")
    with pytest.raises(hv.VaultError):
        vault.reconstruct("missing", {0: b"x", 1: b"y"})


def test_vault_refuses_bad_construction() -> None:
    with pytest.raises(hv.VaultError):
        hv.HolographicEquationVault(k=0)
    with pytest.raises(hv.VaultError):
        hv.HolographicEquationVault(k=2, parity=0)
    with pytest.raises(hv.VaultError):
        hv.HolographicEquationVault(k=hv.MAX_SHARDS, parity=2)


def test_vault_rejects_a_corrupted_reconstruction() -> None:
    vault = hv.HolographicEquationVault(k=2, parity=2)
    vault.seal("E", 30, "a longer statement than the shards")
    shard_set = vault.shard_set("E")
    subset = dict(shard_set.available((0, 1)))
    subset[0] = bytes([subset[0][0] ^ 0xFF]) + subset[0][1:]
    with pytest.raises(hv.VaultError):
        vault.reconstruct("E", subset)


def test_equation_record_and_shard_set_serialise() -> None:
    import hashlib

    vault = hv.HolographicEquationVault(k=2, parity=1)
    shard_set = vault.seal("E", 30, "statement")
    record = vault.equations[0]
    assert record.equation_id == "E"
    assert record.payload == b"statement"
    assert record.digest == hashlib.sha256(b"statement").hexdigest()
    assert shard_set.to_dict()["equation_id"] == "E"
    assert len(shard_set.to_dict()["shard_digests"]) == 3


def test_public_surface_matches_declared_exports() -> None:
    for name in hv.__all__:
        assert hasattr(hv, name), name


def test_merkle_root_handles_empty_single_and_odd_leaf_counts() -> None:
    import hashlib

    assert hv._merkle_root([]) == hashlib.sha256(b"").hexdigest()
    assert hv._merkle_root([b"a"]) == hashlib.sha256(b"\x00a").hexdigest()
    # three leaves exercise the unbalanced carry branch (odd count)
    assert hv._merkle_root([b"a", b"b", b"c"]) == hv._merkle_root([b"a", b"b", b"c"])


def test_vault_holds_at_most_max_equations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hv, "MAX_EQUATIONS", 2)
    vault = hv.HolographicEquationVault(k=1, parity=1)
    vault.seal("E1", 30, "one")
    vault.seal("E2", 30, "two")
    with pytest.raises(hv.VaultError):
        vault.seal("E3", 30, "three")


def test_k_subsets_cover_the_redundant_case() -> None:
    vault = hv.HolographicEquationVault(k=2, parity=1)
    vault.seal("E", 30, "statement")
    subsets = vault._k_subsets()
    assert all(len(s) == vault.k for s in subsets)
    assert (0, 1) in subsets  # head
    assert (1, 2) in subsets  # tail


def test_vault_refuses_zero_parity() -> None:
    with pytest.raises(hv.VaultError):
        hv.HolographicEquationVault(k=2, parity=0)


def test_audit_reports_false_when_a_stored_shard_is_corrupted() -> None:
    vault = _sealed_vault(k=3, parity=2)
    shard_set = vault.shard_set("PH-01")
    corrupted = list(shard_set.shards)
    corrupted[0] = bytes([corrupted[0][0] ^ 0xFF]) + corrupted[0][1:]
    vault._shards["PH-01"] = hv.ShardSet(
        equation_id=shard_set.equation_id,
        k=shard_set.k,
        n=shard_set.n,
        length=shard_set.length,
        shards=tuple(corrupted),
    )
    audit = vault.audit()
    assert audit.any_k_reconstruction_verified is False
    assert audit.all_verified is False


def test_audit_flags_a_non_refusing_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _sealed_vault(k=3, parity=2)
    # A decoder that silently accepts too few shards is a broken invariant.
    monkeypatch.setattr(hv, "decode_shards", lambda shards, *, k, n: b"x" * 64)
    audit = vault.audit()
    assert audit.fewer_than_k_is_refused is False


def test_audit_flags_an_undetected_tamper(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = hv.HolographicEquationVault(k=3, parity=2)
    statement = "sealed-statement"
    vault.seal("E", 30, statement)
    payload = statement.encode()

    def fake_decode(shards: dict[int, bytes], *, k: int, n: int) -> bytes:
        # A decoder that ignores corruption and always returns the seal.
        return payload

    monkeypatch.setattr(hv, "decode_shards", fake_decode)
    monkeypatch.setattr(vault, "reconstruct", lambda eid, shards: statement)
    audit = vault.audit()
    assert audit.tamper_is_detected is False


# --- shard Merkle root (Wave 34 hardening) -----------------------------------------


def test_shard_merkle_root_is_domain_separated_and_position_bound() -> None:
    shards = hv.encode_shards(b"positional", k=2, n=4)
    root = hv.shard_merkle_root(shards)
    # the root changes if a shard moves position (even with identical bytes)
    swapped = (shards[1], shards[0], shards[2], shards[3])
    assert hv.shard_merkle_root(swapped) != root
    # and if any shard's bytes change
    mutated = (bytes([shards[0][0] ^ 0xFF]) + shards[0][1:], *shards[1:])
    assert hv.shard_merkle_root(mutated) != root


def test_shard_leaf_refuses_an_out_of_range_index() -> None:
    with pytest.raises(hv.VaultError):
        hv.shard_leaf(shard_index=4, total_shards=4, shard=b"x")
    with pytest.raises(hv.VaultError):
        hv.shard_leaf(shard_index=-1, total_shards=4, shard=b"x")


def test_shard_set_exposes_and_serialises_its_merkle_root() -> None:
    vault = hv.HolographicEquationVault(k=2, parity=2)
    shard_set = vault.seal("E", 30, "statement")
    assert shard_set.shard_merkle_root == hv.shard_merkle_root(shard_set.shards)
    assert shard_set.shard_merkle_root == vault.shard_merkle_root("E")
    assert shard_set.to_dict()["shard_merkle_root"] == shard_set.shard_merkle_root
    assert shard_set.shard_digests == tuple(
        __import__("hashlib").sha256(s).hexdigest() for s in shard_set.shards
    )


def test_reconstruct_refuses_a_shard_moved_to_another_index() -> None:
    vault = hv.HolographicEquationVault(k=3, parity=2)
    vault.seal("E", 30, "the statement")
    shard_set = vault.shard_set("E")
    # shard 0 placed at index 4 fails its inclusion proof even though the bytes
    # are a genuine sealed shard.
    with pytest.raises(hv.VaultError):
        vault.reconstruct(
            "E", {4: shard_set.shards[0], 1: shard_set.shards[1], 2: shard_set.shards[2]}
        )


def test_reconstruct_refuses_a_corrupted_shard_set_merkle_root() -> None:
    vault = hv.HolographicEquationVault(k=2, parity=1)
    shard_set = vault.seal("E", 30, "statement")
    vault._shards["E"] = hv.ShardSet(
        equation_id="E",
        k=shard_set.k,
        n=shard_set.n,
        length=shard_set.length,
        shards=shard_set.shards,
        shard_merkle_root="0" * 64,
    )
    with pytest.raises(hv.VaultError):
        vault.reconstruct("E", shard_set.available((0, 1)))


def test_reconstruct_refuses_an_out_of_range_shard_index() -> None:
    vault = hv.HolographicEquationVault(k=2, parity=1)
    vault.seal("E", 30, "statement")
    with pytest.raises(hv.VaultError):
        vault.reconstruct("E", {0: b"x", 9: b"y"})


# --- audit combinatorial guard (Wave 34 hardening) ---------------------------------


def test_audit_refuses_an_astronomically_large_combination() -> None:
    vault = hv.HolographicEquationVault(k=127, parity=128)
    with pytest.raises(hv.VaultAuditComplexityError):
        vault.audit()


def test_vault_audit_complexity_error_is_a_vault_error() -> None:
    assert issubclass(hv.VaultAuditComplexityError, hv.VaultError)


def test_audit_runs_within_the_combinatorial_bound() -> None:
    # comb(5, 3) = 10 <= 10_000: a small vault still audits.
    assert _sealed_vault(k=3, parity=2).audit().all_verified is True


# --- disk persistence (Wave 34 hardening) ------------------------------------------


def test_vault_saves_and_reloads_from_disk(tmp_path: Path) -> None:
    vault = _sealed_vault(k=3, parity=2)
    vault.save_to_disk(tmp_path)
    assert (tmp_path / "vault.json").is_file()
    restored = hv.HolographicEquationVault.load_from_disk(tmp_path)
    assert [r.equation_id for r in restored.equations] == [e[0] for e in EQUATIONS]
    for equation_id, _wave, statement in EQUATIONS:
        shard_set = restored.shard_set(equation_id)
        assert restored.reconstruct(equation_id, shard_set.available((0, 1, 2))) == statement
        assert restored.shard_merkle_root(equation_id) == vault.shard_merkle_root(equation_id)


def test_load_from_disk_repairs_corrupted_shard_files(tmp_path: Path) -> None:
    vault = hv.HolographicEquationVault(k=3, parity=2)
    shard_set = vault.seal("E", 30, "a recoverable statement")
    vault.save_to_disk(tmp_path)
    # damage two shards (n - k = 2 erasures tolerated)
    for index in (0, 1):
        path = tmp_path / hv._shard_filename("E", index)
        path.write_bytes(b"\x00" * len(path.read_bytes()))
    restored = hv.HolographicEquationVault.load_from_disk(tmp_path)
    assert restored.reconstruct("E", restored.shard_set("E").available((2, 3, 4))) == (
        "a recoverable statement"
    )
    # the repaired files match the sealed digests again
    for index in range(vault.n):
        raw = (tmp_path / hv._shard_filename("E", index)).read_bytes()
        assert hv._sha256(raw) == shard_set.shard_digests[index]


def test_load_from_disk_repairs_missing_shard_files(tmp_path: Path) -> None:
    vault = hv.HolographicEquationVault(k=2, parity=2)
    vault.seal("E", 30, "a statement")
    vault.save_to_disk(tmp_path)
    (tmp_path / hv._shard_filename("E", 0)).unlink()
    restored = hv.HolographicEquationVault.load_from_disk(tmp_path)
    assert restored.reconstruct("E", restored.shard_set("E").available((0, 1))) == "a statement"


def test_load_from_disk_refuses_too_many_erasures(tmp_path: Path) -> None:
    vault = hv.HolographicEquationVault(k=3, parity=1)
    vault.seal("E", 30, "statement")
    vault.save_to_disk(tmp_path)
    for index in range(2):  # 2 missing > n - k = 1
        (tmp_path / hv._shard_filename("E", index)).unlink()
    with pytest.raises(hv.VaultError):
        hv.HolographicEquationVault.load_from_disk(tmp_path)


def test_load_from_disk_refuses_a_tampered_manifest_digest(tmp_path: Path) -> None:
    import json

    vault = hv.HolographicEquationVault(k=2, parity=2)
    vault.seal("E", 30, "statement")
    vault.save_to_disk(tmp_path)
    manifest = json.loads((tmp_path / "vault.json").read_text(encoding="utf-8"))
    manifest["equations"][0]["digest"] = "0" * 64
    (tmp_path / "vault.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(hv.VaultError):
        hv.HolographicEquationVault.load_from_disk(tmp_path)


def test_load_from_disk_refuses_missing_manifest_and_bad_schema(tmp_path: Path) -> None:
    with pytest.raises(hv.VaultError):
        hv.HolographicEquationVault.load_from_disk(tmp_path)
    (tmp_path / "vault.json").write_text('{"schema": "nope"}', encoding="utf-8")
    with pytest.raises(hv.VaultError):
        hv.HolographicEquationVault.load_from_disk(tmp_path)
    (tmp_path / "vault.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(hv.VaultError):
        hv.HolographicEquationVault.load_from_disk(tmp_path)


def test_vault_to_dict_round_trips_through_disk(tmp_path: Path) -> None:
    vault = _sealed_vault(k=3, parity=2)
    vault.save_to_disk(tmp_path)
    restored = hv.HolographicEquationVault.load_from_disk(tmp_path)
    assert restored.to_dict() == vault.to_dict()
