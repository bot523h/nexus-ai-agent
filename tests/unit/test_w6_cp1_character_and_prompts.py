"""W6 CP1: character continuity fail-closed precedence regression.

Regression coverage for the W6.5 fail-closed fix: the overall continuity
verdict may only be ``VERIFIED`` when every measured dimension is VERIFIED.
Any dimension reported ``INSUFFICIENT_EVIDENCE`` or ``NOT_MEASURED`` must
outrank ``VERIFIED`` in the aggregate; an empty measurement set must refuse
itself (it never yields a VERIFIED report).

W6.6 closes the production gap behind that contract: ``verify_dimensions`` now
emits ``INSUFFICIENT_EVIDENCE`` at the *dimension* level through its
``evidence_counts`` / ``min_evidence_samples`` floor, so these assertions drive
the real verifier end to end instead of re-deriving the aggregate verdict from
``STATE_PRECEDENCE`` by hand.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.character.continuity import (
    ContinuityThreshold,
    VerificationState,
    verify_dimensions,
)

THRESHOLDS = {
    "face": ContinuityThreshold("face", 0.2, 0.6),
    "body": ContinuityThreshold("body", 0.2, 0.6),
}

# ---------------------------------------------------------------------------
# Character continuity: fail-closed overall verdict (W6.5 / W6.6)
# ---------------------------------------------------------------------------


class TestCharacterContinuityFailClosed:
    def test_verified_and_not_measured_never_yields_verified(self):
        report = verify_dimensions(
            {"face": 0.10, "body": None},
            {
                "face": ContinuityThreshold("face", 0.2, 0.6),
                "body": ContinuityThreshold("body", 0.2, 0.6),
            },
        )
        assert report.verdict == VerificationState.NOT_MEASURED
        assert report.verdict != VerificationState.VERIFIED
        assert report.dimensions["face"].state == VerificationState.VERIFIED
        assert report.dimensions["body"].state == VerificationState.NOT_MEASURED

    def test_verified_and_insufficient_evidence_yields_insufficient_evidence(self):
        """One VERIFIED dimension + one thin-evidence dimension => INSUFFICIENT_EVIDENCE.

        ``body`` is measured at 0.10 (inside the verified band) but rests on 2
        samples while the caller requires 5, so the verifier itself downgrades
        that dimension to INSUFFICIENT_EVIDENCE and the aggregate verdict must
        follow — no hand-rolled ``STATE_PRECEDENCE`` scan.
        """
        report = verify_dimensions(
            {"face": 0.10, "body": 0.10},
            THRESHOLDS,
            evidence_counts={"face": 10, "body": 2},
            min_evidence_samples=5,
        )
        assert report.dimensions["face"].state == VerificationState.VERIFIED
        assert report.dimensions["body"].state == VerificationState.INSUFFICIENT_EVIDENCE
        assert report.verdict == VerificationState.INSUFFICIENT_EVIDENCE
        assert report.verdict != VerificationState.VERIFIED

    def test_not_measured_and_insufficient_evidence_yields_insufficient_evidence(self):
        report = verify_dimensions(
            {"face": None, "body": 0.10},
            THRESHOLDS,
            evidence_counts={"body": 1},
            min_evidence_samples=4,
        )
        assert report.dimensions["face"].state == VerificationState.NOT_MEASURED
        assert report.dimensions["body"].state == VerificationState.INSUFFICIENT_EVIDENCE
        assert report.verdict == VerificationState.INSUFFICIENT_EVIDENCE

    def test_thin_evidence_cannot_accuse_identity_drift(self):
        """A drift-sized distance on too few samples is unproven evidence, not drift."""
        report = verify_dimensions(
            {"face": 0.90},
            {"face": ContinuityThreshold("face", 0.2, 0.6)},
            evidence_counts={"face": 0},
            min_evidence_samples=3,
        )
        assert report.dimensions["face"].distance == pytest.approx(0.90)
        assert report.dimensions["face"].state == VerificationState.INSUFFICIENT_EVIDENCE
        assert report.verdict == VerificationState.INSUFFICIENT_EVIDENCE
        assert report.verdict != VerificationState.IDENTITY_DRIFT

    def test_thin_evidence_outranks_verified_but_not_real_drift(self):
        report = verify_dimensions(
            {"face": 0.10, "body": 0.80},
            THRESHOLDS,
            evidence_counts={"face": 1},
            min_evidence_samples=5,
        )
        assert report.dimensions["face"].state == VerificationState.INSUFFICIENT_EVIDENCE
        assert report.dimensions["body"].state == VerificationState.IDENTITY_DRIFT
        assert report.verdict == VerificationState.IDENTITY_DRIFT

    def test_evidence_floor_defaults_keep_previous_behaviour(self):
        """No evidence_counts => scoring identical to the pre-W6.6 verifier."""
        report = verify_dimensions({"face": 0.10, "body": 0.40}, THRESHOLDS)
        assert report.dimensions["face"].state == VerificationState.VERIFIED
        assert report.dimensions["body"].state == VerificationState.REVIEW_REQUIRED
        assert report.verdict == VerificationState.REVIEW_REQUIRED

    def test_evidence_floor_at_exactly_the_minimum_is_scored(self):
        report = verify_dimensions(
            {"face": 0.10},
            {"face": ContinuityThreshold("face", 0.2, 0.6)},
            evidence_counts={"face": 5},
            min_evidence_samples=5,
        )
        assert report.dimensions["face"].state == VerificationState.VERIFIED
        assert report.verdict == VerificationState.VERIFIED

    @pytest.mark.parametrize("floor", [0, -1, True, 1.5])
    def test_invalid_evidence_floor_is_refused(self, floor):
        with pytest.raises(ValueError):
            verify_dimensions({"face": 0.10}, THRESHOLDS, min_evidence_samples=floor)

    def test_evidence_count_without_measurement_is_refused(self):
        with pytest.raises(ValueError):
            verify_dimensions(
                {"face": 0.10},
                THRESHOLDS,
                evidence_counts={"face": 5, "body": 5},
            )

    @pytest.mark.parametrize("count", [-1, True, 2.0])
    def test_invalid_evidence_count_is_refused(self, count):
        with pytest.raises(ValueError):
            verify_dimensions(
                {"face": 0.10},
                THRESHOLDS,
                evidence_counts={"face": count},
            )

    def test_empty_measurements_never_yield_a_verified_report(self):
        with pytest.raises(ValueError):
            verify_dimensions({}, THRESHOLDS)
