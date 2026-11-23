"""W6 CP1: character continuity fail-closed precedence regression.

Regression coverage for the W6.5 fail-closed fix: the overall continuity
verdict may only be ``VERIFIED`` when every measured dimension is VERIFIED.
Any dimension reported ``INSUFFICIENT_EVIDENCE`` or ``NOT_MEASURED`` must
outrank ``VERIFIED`` in the aggregate; an empty measurement set must refuse
itself (it never yields a VERIFIED report).

Note: :func:`verify_dimensions` cannot emit an ``INSUFFICIENT_EVIDENCE``
dimension state (it only emits ``VERIFIED`` / ``REVIEW_REQUIRED`` /
``IDENTITY_DRIFT`` / ``NOT_MEASURED`` per measured dimension), so the
INSUFFICIENT_EVIDENCE sentences assert the aggregate precedence contract
directly, mirroring the ``next(...)`` selection logic of
``verify_dimensions`` against the shipped ``STATE_PRECEDENCE`` tuple.
"""

from __future__ import annotations

from nexus_ai_agent.creative.character.continuity import (
    STATE_PRECEDENCE,
    ContinuityThreshold,
    VerificationState,
    verify_dimensions,
)

# ---------------------------------------------------------------------------
# Character continuity: fail-closed overall verdict (W6.5)
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
        states = {VerificationState.VERIFIED, VerificationState.INSUFFICIENT_EVIDENCE}
        verdict = next(
            (state for state in STATE_PRECEDENCE if state in states),
            VerificationState.INSUFFICIENT_EVIDENCE,
        )
        assert verdict == VerificationState.INSUFFICIENT_EVIDENCE

    def test_not_measured_and_insufficient_evidence_yields_insufficient_evidence(self):
        states = {VerificationState.NOT_MEASURED, VerificationState.INSUFFICIENT_EVIDENCE}
        verdict = next(
            (state for state in STATE_PRECEDENCE if state in states),
            VerificationState.INSUFFICIENT_EVIDENCE,
        )
        assert verdict == VerificationState.INSUFFICIENT_EVIDENCE
