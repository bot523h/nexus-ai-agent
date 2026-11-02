"""Phase 2 — the proof-completeness gate.

Rendering completion is not proof. A proof-bearing capsule may be minted only
when the independent verifier PASSed *and* attempt/journal/passport evidence is
complete; every gap is named, and nothing is ever upgraded to a verified claim.
"""

from __future__ import annotations

from nexus_ai_agent.nagar.ibpc.models import CheckVerdict
from nexus_ai_agent.nagar.ibpc.proof import (
    EXECUTED_UNVERIFIED,
    EXECUTED_VERIFIED,
    evaluate_proof_gate,
)

_COMPLETE_PROVENANCE = {
    "journal": {"chain_verified": True, "head_hash": "sha256:" + "a" * 64},
    "passport": {"passport_digest": "sha256:" + "b" * 64},
}


def test_complete_evidence_passes() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference=_COMPLETE_PROVENANCE,
    )
    assert gate.proof_complete is True
    assert gate.reasons == ()
    assert gate.outcome_status == EXECUTED_VERIFIED


def test_inconclusive_verdict_is_not_proof() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.INCONCLUSIVE,
        attempt_identity="job-1:attempt1",
        provenance_reference=_COMPLETE_PROVENANCE,
    )
    assert gate.proof_complete is False
    assert "verdict_not_pass:INCONCLUSIVE" in gate.reasons
    assert gate.outcome_status == EXECUTED_UNVERIFIED


def test_fail_verdict_is_not_proof() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.FAIL,
        attempt_identity="job-1:attempt1",
        provenance_reference=_COMPLETE_PROVENANCE,
    )
    assert gate.proof_complete is False
    assert "verdict_not_pass:FAIL" in gate.reasons


def test_unknown_attempt_identity_is_a_gap() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt?",
        provenance_reference=_COMPLETE_PROVENANCE,
    )
    assert gate.proof_complete is False
    assert "attempt_identity_missing" in gate.reasons


def test_missing_journal_is_a_gap() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": None,
            "passport": {"passport_digest": "sha256:" + "b" * 64},
        },
    )
    assert "journal_missing" in gate.reasons


def test_unverified_journal_chain_is_a_gap() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": {"chain_verified": False},
            "passport": {"passport_digest": "sha256:" + "b" * 64},
        },
    )
    assert "journal_chain_unverified" in gate.reasons


def test_missing_passport_digest_is_a_gap() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": {"chain_verified": True},
            "passport": {"error": "boom"},
        },
    )
    assert "passport_digest_missing" in gate.reasons


def test_all_gaps_are_reported_together() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.INCONCLUSIVE,
        attempt_identity=None,
        provenance_reference={},
    )
    assert gate.proof_complete is False
    assert set(gate.reasons) == {
        "verdict_not_pass:INCONCLUSIVE",
        "attempt_identity_missing",
        "journal_missing",
        "passport_missing",
    }
