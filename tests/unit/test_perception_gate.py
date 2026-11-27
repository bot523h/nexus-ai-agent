"""The perception admission gate: license supremum, guard band, plain-data port."""

from __future__ import annotations

import math
from typing import Any

import pytest

from nexus_ai_agent.creative.composition.license_lattice import LicenseRegime
from nexus_ai_agent.creative.packs.perception.models import LICENSE_REGIME_NAMES
from nexus_ai_agent.creative.verification.adapters import (
    scene_cut_uncertainty,
    synthetic_scene_cut_histograms,
)
from nexus_ai_agent.creative.verification.perception_gate import (
    PerceptionAdmission,
    PerceptionAdmissionRequest,
    admit_perception,
    gate_licenses,
    gate_loudness,
    gate_perception,
    gate_scene_cut,
    gate_silence,
    parse_regimes,
)
from nexus_ai_agent.creative.verification.uncertainty_gum import (
    GUMInputError,
    SpecificationLimits,
    VerificationOutcome,
    evaluate_guard_band,
)

_PERMISSIVE = (LicenseRegime.PERMISSIVE_MIT_APACHE,)


def _context(**overrides: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "kind": "loudness",
        "licenses": ["PERMISSIVE_MIT_APACHE"],
        "commercial_intent": False,
        "integrated_lufs": -14.0,
        "target_lufs": -14.0,
        "tolerance_lu": 1.0,
    }
    context.update(overrides)
    return context


def _request(**overrides: Any) -> PerceptionAdmissionRequest:
    values: dict[str, Any] = {
        "licenses": _PERMISSIVE,
        "commercial_intent": False,
        "integrated_lkfs": -14.0,
        "target_lkfs": -14.0,
        "tolerance_lu": 1.0,
    }
    values.update(overrides)
    return PerceptionAdmissionRequest(**values)


# ---------------------------------------------------------------------------
# the lattice mirror stays identical to the lattice itself
# ---------------------------------------------------------------------------


def test_the_regime_names_match_the_lattice() -> None:
    assert tuple(member.value for member in LicenseRegime) == LICENSE_REGIME_NAMES


def test_parse_regimes_maps_names_and_refuses_unknown_ones() -> None:
    assert parse_regimes(["PUBLIC_DOMAIN"]) == (LicenseRegime.PUBLIC_DOMAIN,)
    with pytest.raises(GUMInputError, match="unknown license regime"):
        parse_regimes(["NOT_A_LICENSE"])
    with pytest.raises(GUMInputError, match="must be a tuple of license names"):
        parse_regimes("PUBLIC_DOMAIN")
    with pytest.raises(GUMInputError, match="license name string"):
        parse_regimes([1])


# ---------------------------------------------------------------------------
# the plain-data port the perception pack calls
# ---------------------------------------------------------------------------


def test_a_measurement_inside_the_band_passes_without_a_gain() -> None:
    record = gate_perception(_context())
    assert record["admitted"] is True
    assert record["outcome"] == "pass"
    assert record["recommended_gain_db"] == 0.0
    assert record["refusal"] is None
    assert record["license"]["supremum"] == "PERMISSIVE_MIT_APACHE"


def test_a_confidently_out_of_band_measurement_fails_with_the_exact_gain() -> None:
    record = gate_loudness(_context(integrated_lufs=-23.0))
    assert record["admitted"] is True
    assert record["outcome"] == "fail"
    assert record["recommended_gain_db"] == pytest.approx(9.0)


def test_a_straddling_measurement_abstains_instead_of_guessing() -> None:
    """The interval [y - k u, y + k u] crosses the band edge -> no decision."""
    record = gate_loudness(_context(integrated_lufs=-15.0, tolerance_lu=1.0))
    assert record["admitted"] is False
    assert record["outcome"] == VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE.value
    assert record["recommended_gain_db"] is None
    assert "straddles the tolerance band" in record["refusal"]


def test_a_license_conflict_refuses_before_the_gain_is_considered() -> None:
    record = gate_loudness(
        _context(licenses=["NON_COMMERCIAL_ONLY"], commercial_intent=True, integrated_lufs=-14.0)
    )
    assert record["admitted"] is False
    assert record["outcome"] == "license_conflict"
    assert record["license"]["supremum"] == "INCOMPATIBLE_CONFLICT"
    assert record["recommended_gain_db"] is None


def test_no_declared_license_means_no_license_claim() -> None:
    record = gate_loudness(_context(licenses=[]))
    assert record["admitted"] is True
    assert record["license"] is None


def test_the_license_port_admits_and_refuses() -> None:
    admitted = gate_licenses({"licenses": ["PERMISSIVE_MIT_APACHE"]})
    assert admitted["admitted"] is True
    assert admitted["outcome"] == "license_ok"
    assert admitted["refusal"] is None

    strict = gate_licenses({"licenses": ["NON_COMMERCIAL_ONLY"], "commercial_intent": True})
    assert strict["admitted"] is False
    assert strict["outcome"] == "license_conflict"
    assert "INCOMPATIBLE_CONFLICT" in strict["refusal"]


def test_the_license_port_requires_a_declared_regime() -> None:
    with pytest.raises(GUMInputError, match="missing"):
        gate_licenses({})
    with pytest.raises(GUMInputError, match="at least one license regime"):
        gate_licenses({"licenses": ()})


def test_the_port_fails_closed_on_missing_or_invalid_keys() -> None:
    with pytest.raises(GUMInputError, match="missing"):
        gate_loudness({"integrated_lufs": -14.0})
    with pytest.raises(GUMInputError, match="must be a mapping"):
        gate_perception(["not", "a", "mapping"])
    with pytest.raises(GUMInputError, match="unknown gate kind"):
        gate_perception({"kind": "wallpaper"})
    with pytest.raises(GUMInputError, match="must be positive"):
        gate_loudness(_context(tolerance_lu=0.0))
    with pytest.raises(GUMInputError, match="coverage_factor must be positive"):
        gate_loudness(_context(coverage_factor=-1.0))
    with pytest.raises(GUMInputError, match="bit_depth must be an int"):
        gate_loudness(_context(bit_depth=16.5))
    with pytest.raises(GUMInputError, match="sample_count must be an int"):
        gate_loudness(_context(sample_count=True))


def test_the_port_records_the_interval_it_decided_on() -> None:
    record = gate_loudness(_context())
    low, high = record["interval"]
    assert record["limits"] == [-15.0, -13.0]
    assert low < record["integrated_lufs"] < high
    assert high - low == pytest.approx(2.0 * record["expanded_uncertainty"])


# ---------------------------------------------------------------------------
# the typed wrapper runs the same arithmetic
# ---------------------------------------------------------------------------


def test_the_typed_wrapper_agrees_with_the_port() -> None:
    admission = admit_perception(_request(integrated_lkfs=-23.0))
    assert isinstance(admission, PerceptionAdmission)
    assert admission.admitted is True
    assert admission.recommended_gain_db == pytest.approx(9.0)
    assert admission.loudness.outcome is VerificationOutcome.FAIL
    assert admission.license.supremum is LicenseRegime.PERMISSIVE_MIT_APACHE


def test_the_typed_wrapper_abstains_on_a_straddle() -> None:
    admission = admit_perception(_request(integrated_lkfs=-15.0))
    assert admission.admitted is False
    assert admission.recommended_gain_db is None
    assert admission.loudness.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize(
    "overrides",
    [
        {"licenses": ()},
        {"licenses": ("MIT",)},
        {"limites": None},
        {"tolerance_lu": 0.0},
        {"tolerance_lu": math.nan},
        {"target_lkfs": math.inf},
        {"target_lkfs": True},
        {"coverage_factor": 0.0},
        {"commercial_intent": 1},
        {"integrated_lkfs": "loud"},
    ],
)
def test_the_typed_request_refuses_unusable_inputs(overrides: dict[str, Any]) -> None:
    with pytest.raises((GUMInputError, TypeError)):
        _request(**overrides)


def test_the_typed_record_refuses_a_contradictory_verdict() -> None:
    decision = evaluate_guard_band(
        -14.0, _combined(), SpecificationLimits(lower=-15.0, upper=-13.0), 2.0
    )
    combination = admit_perception(_request()).license
    with pytest.raises(GUMInputError, match="cannot carry a refusal"):
        PerceptionAdmission(
            admitted=True,
            license=combination,
            loudness=decision,
            recommended_gain_db=0.0,
            refusal="no",
        )
    with pytest.raises(GUMInputError, match="must carry a refusal"):
        PerceptionAdmission(
            admitted=False,
            license=combination,
            loudness=decision,
            recommended_gain_db=None,
            refusal=None,
        )
    with pytest.raises(GUMInputError, match="must recommend a gain"):
        PerceptionAdmission(
            admitted=True,
            license=combination,
            loudness=decision,
            recommended_gain_db=None,
            refusal=None,
        )


def _combined() -> Any:
    from nexus_ai_agent.creative.verification.uncertainty_gum import (
        UncertaintyInput,
        combine,
    )

    return combine((1.0,), UncertaintyInput.from_standard((1.0,), (0.1,)))


def test_the_typed_record_refuses_wrong_port_types_and_a_foreign_request() -> None:
    """Construction validates the port types; ``admit_perception`` validates its request."""
    decision = evaluate_guard_band(
        -14.0, _combined(), SpecificationLimits(lower=-15.0, upper=-13.0), 2.0
    )
    combination = admit_perception(_request()).license
    with pytest.raises(GUMInputError, match="license must be a LicenseCombination"):
        PerceptionAdmission(
            admitted=False,
            license=object(),  # type: ignore[arg-type]
            loudness=decision,
            recommended_gain_db=None,
            refusal="no",
        )
    with pytest.raises(GUMInputError, match="loudness must be a GuardBandDecision"):
        PerceptionAdmission(
            admitted=False,
            license=combination,
            loudness=object(),  # type: ignore[arg-type]
            recommended_gain_db=None,
            refusal="no",
        )
    with pytest.raises(GUMInputError, match="must be a PerceptionAdmissionRequest"):
        admit_perception("not a request")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# scene-cut and silence GUM gates (Wave B)
# ---------------------------------------------------------------------------


_SCENE_BEFORE, _SCENE_AFTER = synthetic_scene_cut_histograms(0.3)


def _scene_context(**overrides: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "hist_before": list(_SCENE_BEFORE),
        "hist_after": list(_SCENE_AFTER),
        "cut_threshold_bits": 0.02,
        "samples_per_histogram": 4096,
    }
    context.update(overrides)
    return context


def _silence_context(**overrides: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "measured_rms_db": -48.0,
        "threshold_db": -40.0,
        "sample_count": 72_000,
        "window_duration_s": 1.5,
        "min_silence_duration_s": 0.5,
    }
    context.update(overrides)
    return context


def test_scene_cut_gate_admits_a_cut_above_the_threshold() -> None:
    record = gate_scene_cut(_scene_context())
    assert record["admitted"] is True
    assert record["outcome"] == "pass"
    assert record["combined_u"] > 0.0
    assert record["expanded_U"] == pytest.approx(2.0 * record["combined_u"])


def test_scene_cut_gate_abstains_without_empirical_histograms() -> None:
    record = gate_scene_cut({"cut_threshold_bits": 0.02})
    assert record["admitted"] is False
    assert record["outcome"] == "abstain_insufficient_evidence"
    assert record["refusal"] == "insufficient_evidence: missing_frame_histograms"


def test_scene_cut_gate_never_uses_the_detector_threshold_as_a_histogram() -> None:
    """A detector ``threshold`` is a claim, not a measurement: the gate abstains."""
    record = gate_scene_cut({"threshold": 0.3, "cut_threshold_bits": 0.02})
    assert record["admitted"] is False
    assert record["outcome"] == "abstain_insufficient_evidence"
    assert "missing_frame_histograms" in record["refusal"]


def test_a_borderline_scene_cut_abstains_instead_of_admitting() -> None:
    limit = scene_cut_uncertainty(
        _SCENE_BEFORE, _SCENE_AFTER, samples_per_histogram=4096
    ).js_divergence
    record = gate_scene_cut(_scene_context(cut_threshold_bits=limit))
    assert record["admitted"] is False
    assert record["outcome"] == "abstain_insufficient_evidence"
    assert record["refusal"]


def test_scene_cut_gate_fails_a_weak_cut() -> None:
    record = gate_scene_cut(_scene_context(cut_threshold_bits=0.5))
    assert record["admitted"] is False
    assert record["outcome"] == "fail"


def test_scene_cut_gate_refuses_inconsistent_histograms() -> None:
    record = gate_scene_cut({"hist_before": [1.0, 0.0], "hist_after": [0.5, 0.25, 0.25]})
    assert record["admitted"] is False
    assert record["outcome"] == "abstain_insufficient_evidence"
    assert "inconsistent_frame_histograms" in record["refusal"]


def test_scene_cut_gate_uses_explicit_histograms() -> None:
    record = gate_scene_cut(
        {
            "hist_before": [1.0, 0.0],
            "hist_after": [0.0, 1.0],
            "cut_threshold_bits": 0.5,
            "samples_per_histogram": 4096,
        }
    )
    assert record["js_divergence"] == pytest.approx(1.0)
    assert record["admitted"] is True


def test_silence_gate_admits_a_segment_below_the_threshold() -> None:
    record = gate_silence(_silence_context())
    assert record["admitted"] is True
    assert record["outcome"] == "pass"
    assert record["rms_db"] == pytest.approx(-48.0)
    assert record["threshold_db"] == pytest.approx(-40.0)


def test_silence_gate_accepts_the_legacy_rms_db_alias_as_measured() -> None:
    record = gate_silence({"rms_db": -48.0, "threshold_db": -40.0, "sample_count": 72_000})
    assert record["admitted"] is True


def test_silence_gate_abstains_without_a_measured_level() -> None:
    record = gate_silence({"threshold_db": -40.0, "sample_count": 72_000})
    assert record["admitted"] is False
    assert record["outcome"] == "abstain_insufficient_evidence"
    assert record["refusal"] == "insufficient_evidence: missing_measured_rms_db"


def test_silence_gate_never_uses_the_detector_threshold_as_the_measurement() -> None:
    record = gate_silence({"noise_floor_db": -40.0, "threshold_db": -40.0, "sample_count": 72_000})
    assert record["admitted"] is False
    assert "missing_measured_rms_db" in record["refusal"]


def test_silence_gate_abstains_without_a_sample_count() -> None:
    record = gate_silence({"measured_rms_db": -48.0, "threshold_db": -40.0})
    assert record["admitted"] is False
    assert record["refusal"] == "insufficient_evidence: missing_sample_count"


def test_silence_gate_abstains_on_a_non_positive_sample_count() -> None:
    record = gate_silence({"measured_rms_db": -48.0, "threshold_db": -40.0, "sample_count": 0})
    assert record["admitted"] is False
    assert "non_positive_sample_count" in record["refusal"]


def test_a_borderline_silence_abstains_instead_of_admitting() -> None:
    record = gate_silence(_silence_context(measured_rms_db=-40.0))
    assert record["admitted"] is False
    assert record["outcome"] == "abstain_insufficient_evidence"
    assert record["refusal"]


def test_silence_gate_fails_a_loud_segment() -> None:
    record = gate_silence(_silence_context(measured_rms_db=-32.0))
    assert record["admitted"] is False
    assert record["outcome"] == "fail"


def test_gate_perception_dispatches_scene_cut_and_silence() -> None:
    assert gate_perception({"kind": "scene_cut", **_scene_context()})["admitted"] is True
    assert gate_perception({"kind": "silence", **_silence_context()})["admitted"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "scene_cut", "cut_threshold_bits": 0.0},
        {"kind": "scene_cut", "coverage_factor": 0.0},
        {"kind": "scene_cut", "samples_per_histogram": True},
        {
            "kind": "silence",
            "measured_rms_db": -48.0,
            "threshold_db": -40.0,
            "sample_count": True,
        },
        {
            "kind": "silence",
            "measured_rms_db": -48.0,
            "threshold_db": -40.0,
            "sample_count": 48_000,
            "bit_depth": True,
        },
    ],
)
def test_the_new_gate_kinds_refuse_malformed_context(payload: dict[str, Any]) -> None:
    with pytest.raises(GUMInputError):
        gate_perception(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "scene_cut"},
        {"kind": "scene_cut", "threshold": 0.3},
        {"kind": "silence"},
        {"kind": "silence", "measured_rms_db": -48.0, "threshold_db": -40.0},
    ],
)
def test_the_new_gate_kinds_abstain_on_missing_evidence(payload: dict[str, Any]) -> None:
    record = gate_perception(payload)
    assert record["admitted"] is False
    assert record["outcome"] == "abstain_insufficient_evidence"
    assert record["refusal"].startswith("insufficient_evidence:")


def test_scene_cut_gate_refuses_non_positive_limits() -> None:
    with pytest.raises(GUMInputError):
        gate_scene_cut({"cut_threshold_bits": 0.0})
    with pytest.raises(GUMInputError):
        gate_scene_cut({"coverage_factor": 0.0})
    with pytest.raises(GUMInputError):
        gate_scene_cut({"samples_per_histogram": True})


def test_silence_gate_refuses_non_int_counts() -> None:
    with pytest.raises(GUMInputError):
        gate_silence({"measured_rms_db": -48.0, "threshold_db": -40.0, "sample_count": True})
    with pytest.raises(GUMInputError):
        gate_silence(
            {
                "measured_rms_db": -48.0,
                "threshold_db": -40.0,
                "sample_count": 48_000,
                "bit_depth": True,
            }
        )


def test_scene_cut_gate_refuses_a_non_sequence_histogram() -> None:
    with pytest.raises(GUMInputError, match="sequences of bin probabilities"):
        gate_scene_cut({"hist_before": 0.5, "hist_after": [0.0, 1.0]})
