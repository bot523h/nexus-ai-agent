"""Admission gate: license supremum and the GUM guard band, composed."""

from __future__ import annotations

import math

import pytest

from nexus_ai_agent.creative.composition.license_lattice import LicenseRegime
from nexus_ai_agent.creative.perception.analyzer import AudioPerception
from nexus_ai_agent.creative.verification.perception_gate import (
    PerceptionAdmission,
    PerceptionAdmissionRequest,
    admit_perception,
)
from nexus_ai_agent.creative.verification.uncertainty_gum import (
    GUMInputError,
    VerificationOutcome,
)

_PERMISSIVE = (LicenseRegime.PERMISSIVE_MIT_APACHE,)


def _audio(lkfs: float) -> AudioPerception:
    return AudioPerception(
        integrated_lkfs=lkfs,
        momentary_lkfs=(lkfs,),
        short_term_lkfs=(lkfs,),
        loudness_range_lu=1.0,
        true_peak_dbtp=-1.0,
        bark_centroid=100.0,
        silences=(),
    )


def _request(lkfs: float, *, tolerance: float = 0.5, licenses=_PERMISSIVE, commercial=False):
    return PerceptionAdmissionRequest(
        licenses=licenses,
        commercial_intent=commercial,
        loudness=_audio(lkfs),
        target_lkfs=-14.0,
        tolerance_lu=tolerance,
        sample_count=4_800_000,
    )


def test_a_confidently_quiet_measurement_is_admitted_with_a_boost() -> None:
    admission = admit_perception(_request(-20.0))
    assert admission.admitted is True
    assert admission.refusal is None
    assert admission.recommended_gain_db == pytest.approx(6.0)
    assert admission.loudness.outcome is VerificationOutcome.FAIL
    assert admission.license.compatible is True


def test_a_measurement_inside_the_band_needs_no_gain() -> None:
    admission = admit_perception(_request(-14.05, tolerance=0.5))
    assert admission.admitted is True
    assert admission.recommended_gain_db == 0.0
    assert admission.loudness.outcome is VerificationOutcome.PASS


def test_a_straddling_measurement_is_refused_not_guessed() -> None:
    admission = admit_perception(_request(-14.5, tolerance=0.5))
    assert admission.admitted is False
    assert admission.recommended_gain_db is None
    assert admission.loudness.outcome is VerificationOutcome.ABSTAIN_INSUFFICIENT_EVIDENCE
    assert "straddles" in (admission.refusal or "")


def test_a_license_conflict_refuses_before_the_gain_is_considered() -> None:
    admission = admit_perception(
        _request(
            -20.0,
            licenses=(LicenseRegime.NON_COMMERCIAL_ONLY,),
            commercial=True,
        )
    )
    assert admission.admitted is False
    assert admission.recommended_gain_db is None
    assert admission.license.compatible is False
    assert "license supremum" in (admission.refusal or "")


def test_request_rejects_an_empty_license_set() -> None:
    with pytest.raises(GUMInputError, match="at least one license"):
        _request(-14.0, licenses=())


def test_request_rejects_a_non_positive_tolerance() -> None:
    with pytest.raises(GUMInputError, match="tolerance_lu"):
        _request(-14.0, tolerance=0.0)


def test_request_rejects_a_wrong_loudness_type() -> None:
    with pytest.raises(GUMInputError, match="AudioPerception"):
        PerceptionAdmissionRequest(
            licenses=_PERMISSIVE,
            commercial_intent=False,
            loudness="nope",  # type: ignore[arg-type]
            target_lkfs=-14.0,
            tolerance_lu=0.5,
        )


def test_request_rejects_a_non_finite_target() -> None:
    with pytest.raises(GUMInputError, match="target_lkfs"):
        PerceptionAdmissionRequest(
            licenses=_PERMISSIVE,
            commercial_intent=False,
            loudness=_audio(-14.0),
            target_lkfs=math.nan,
            tolerance_lu=0.5,
        )


def test_request_rejects_an_unusable_coverage_factor() -> None:
    with pytest.raises(GUMInputError, match="coverage_factor"):
        PerceptionAdmissionRequest(
            licenses=_PERMISSIVE,
            commercial_intent=False,
            loudness=_audio(-14.0),
            target_lkfs=-14.0,
            tolerance_lu=0.5,
            coverage_factor=0.0,
        )


def test_admission_refuses_a_contradictory_record() -> None:
    admission = admit_perception(_request(-20.0))
    with pytest.raises(GUMInputError, match="cannot carry a refusal"):
        PerceptionAdmission(
            admitted=True,
            license=admission.license,
            loudness=admission.loudness,
            recommended_gain_db=1.0,
            refusal="nope",
        )
    with pytest.raises(GUMInputError, match="must carry a refusal"):
        PerceptionAdmission(
            admitted=False,
            license=admission.license,
            loudness=admission.loudness,
            recommended_gain_db=None,
            refusal=None,
        )
    with pytest.raises(GUMInputError, match="must recommend a gain"):
        PerceptionAdmission(
            admitted=True,
            license=admission.license,
            loudness=admission.loudness,
            recommended_gain_db=None,
            refusal=None,
        )
    with pytest.raises(GUMInputError, match="LicenseCombination"):
        PerceptionAdmission(
            admitted=True,
            license="nope",  # type: ignore[arg-type]
            loudness=admission.loudness,
            recommended_gain_db=1.0,
            refusal=None,
        )
    with pytest.raises(GUMInputError, match="GuardBandDecision"):
        PerceptionAdmission(
            admitted=True,
            license=admission.license,
            loudness="nope",  # type: ignore[arg-type]
            recommended_gain_db=1.0,
            refusal=None,
        )
    with pytest.raises(GUMInputError, match="gain"):
        PerceptionAdmission(
            admitted=True,
            license=admission.license,
            loudness=admission.loudness,
            recommended_gain_db=math.inf,
            refusal=None,
        )


def test_admit_rejects_a_wrong_request_type() -> None:
    with pytest.raises(GUMInputError, match="PerceptionAdmissionRequest"):
        admit_perception("nope")  # type: ignore[arg-type]


def test_request_rejects_a_non_regime_license() -> None:
    with pytest.raises(GUMInputError, match="LicenseRegime"):
        _request(-14.0, licenses=("MIT",))  # type: ignore[arg-type]


def test_request_rejects_a_non_bool_commercial_intent() -> None:
    with pytest.raises(GUMInputError, match="commercial_intent"):
        _request(-14.0, commercial=1)  # type: ignore[arg-type]


def test_request_rejects_a_boolean_target() -> None:
    with pytest.raises(GUMInputError, match="target_lkfs"):
        PerceptionAdmissionRequest(
            licenses=_PERMISSIVE,
            commercial_intent=False,
            loudness=_audio(-14.0),
            target_lkfs=True,  # type: ignore[arg-type]
            tolerance_lu=0.5,
        )
