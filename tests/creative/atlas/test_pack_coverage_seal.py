"""WAVE 33 — tests for the Pack Coverage Seal.

Covers the pure decision function (accepted only when every shipped pack has a
manifest, a canonical target and coverage at or above the threshold), the
canonical seal with its Merkle root and digest, the sealing helpers into the
Wave 30 vault and Wave 31 ledger, and the typed refusals.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.atlas import holographic_equation_vault as hv
from nexus_ai_agent.creative.atlas import pack_coverage_seal as pcs
from nexus_ai_agent.creative.atlas import self_healing_linear_ledger as shl


def _entry(
    pack: str, coverage: float, *, manifest: bool = True, target: bool = True
) -> pcs.PackCoverageEntry:
    return pcs.PackCoverageEntry(
        pack=pack,
        coverage_percent=coverage,
        has_manifest=manifest,
        has_canonical_target=target,
    )


def _clean() -> tuple[pcs.PackCoverageEntry, ...]:
    return (_entry("audio", 96.56), _entry("caption", 96.22), _entry("slideshow", 97.81))


# --- decision --------------------------------------------------------------------


def test_clean_measurement_is_accepted() -> None:
    verdict = pcs.evaluate_pack_coverage(_clean())
    assert verdict.accepted is True
    assert verdict.reasons == ()
    assert verdict.threshold == pcs.DEFAULT_COVERAGE_THRESHOLD


def test_missing_manifest_and_target_are_refused() -> None:
    verdict = pcs.evaluate_pack_coverage((_entry("portrait", 99.0, manifest=False, target=False),))
    assert verdict.accepted is False
    assert verdict.reasons == (
        "shipped pack 'portrait' has no pack.manifest.json",
        "shipped pack 'portrait' has no canonical test target",
    )


def test_coverage_below_threshold_is_refused() -> None:
    verdict = pcs.evaluate_pack_coverage((_entry("scene", 0.0),))
    assert verdict.accepted is False
    assert verdict.reasons == ("pack 'scene': 0.00% < 95.00%",)


def test_custom_threshold_is_honoured() -> None:
    assert pcs.evaluate_pack_coverage((_entry("x", 90.0),), threshold=90.0).accepted is True
    assert pcs.evaluate_pack_coverage((_entry("x", 89.99),), threshold=90.0).accepted is False


def test_decision_validates_its_inputs() -> None:
    with pytest.raises(pcs.PackCoverageError):
        pcs.evaluate_pack_coverage(_clean(), threshold=200.0)
    with pytest.raises(pcs.PackCoverageError):
        pcs.evaluate_pack_coverage((_entry("", 99.0),))
    with pytest.raises(pcs.PackCoverageError):
        pcs.evaluate_pack_coverage((_entry("a", 99.0), _entry("a", 98.0)))
    with pytest.raises(pcs.PackCoverageError):
        pcs.evaluate_pack_coverage((_entry("a", 101.0),))


def test_build_entries_pins_the_four_facts_together() -> None:
    entries = pcs.build_entries(
        ["audio", "portrait", "scene"],
        {"audio": 96.0, "portrait": 99.0},
        manifests=["audio", "portrait"],
        canonical_targets=["audio", "portrait"],
    )
    assert [e.pack for e in entries] == ["audio", "portrait", "scene"]
    scene = entries[2]
    # scene has no measurement, no manifest, no target: refused, never dropped.
    assert scene.coverage_percent == 0.0
    assert scene.has_manifest is False
    assert scene.has_canonical_target is False


# --- seal ------------------------------------------------------------------------


def test_seal_is_canonical_and_deterministic() -> None:
    first = pcs.seal_pack_coverage(_clean(), git_commit="abc123")
    second = pcs.seal_pack_coverage(_clean(), git_commit="abc123")
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.digest == second.digest
    assert first.merkle_root == second.merkle_root
    assert first.to_dict()["schema"] == pcs.SEAL_SCHEMA


def test_merkle_root_changes_when_one_pack_changes() -> None:
    base = pcs.seal_pack_coverage(_clean(), git_commit="abc123")
    changed = pcs.seal_pack_coverage((_entry("audio", 96.57), *_clean()[1:]), git_commit="abc123")
    assert base.merkle_root != changed.merkle_root
    assert base.digest != changed.digest


def test_seal_records_the_refusal_reasons() -> None:
    seal = pcs.seal_pack_coverage(
        (_entry("portrait", 0.0, manifest=False, target=False),), git_commit="abc123"
    )
    assert seal.to_dict()["accepted"] is False
    assert len(seal.to_dict()["reasons"]) == 3


def test_seal_validates_git_commit() -> None:
    with pytest.raises(pcs.PackCoverageError):
        pcs.seal_pack_coverage(_clean(), git_commit="")


def test_seal_appends_to_the_ledger() -> None:
    seal = pcs.seal_pack_coverage(_clean(), git_commit="abc123")
    ledger = shl.SelfHealingLinearLedger(k=2, parity=1)
    seal.append_to_ledger(ledger)
    assert ledger.payload(0) == seal.canonical_bytes()
    assert ledger.verify().intact is True


def test_seal_projects_into_the_vault() -> None:
    seal = pcs.seal_pack_coverage(_clean(), git_commit="abc123")
    vault = hv.HolographicEquationVault(k=2, parity=1)
    seal.seal_into_vault(vault)
    assert {r.equation_id for r in vault.equations} == {
        "coverage:audio",
        "coverage:caption",
        "coverage:slideshow",
    }
    assert vault.audit().all_verified is True


def test_entry_leaf_is_canonical() -> None:
    from nexus_ai_agent.creative.atlas.atlas_spine_governance import jcs_canonical_bytes

    entry = _entry("audio", 96.559999)
    assert entry.leaf() == jcs_canonical_bytes(entry.to_dict())
    assert entry.to_dict()["coverage_percent"] == 96.56


def test_merkle_root_of_no_entries_is_the_empty_digest() -> None:
    import hashlib

    seal = pcs.PackCoverageSeal(verdict=pcs.evaluate_pack_coverage(()), git_commit="abc123")
    assert seal.merkle_root == hashlib.sha256(b"").hexdigest()


def test_evaluate_refuses_a_non_numeric_threshold() -> None:
    with pytest.raises(pcs.PackCoverageError):
        pcs.evaluate_pack_coverage(_clean(), threshold="high")  # type: ignore[arg-type]


def test_verdict_serialises() -> None:
    verdict = pcs.evaluate_pack_coverage((_entry("scene", 0.0),))
    assert verdict.to_dict()["accepted"] is False
    assert verdict.to_dict()["entries"][0]["pack"] == "scene"


def test_public_surface_matches_declared_exports() -> None:
    for name in pcs.__all__:
        assert hasattr(pcs, name), name
