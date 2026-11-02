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
    "journal": {
        "record_count": 3,
        "chain_verified": True,
        "head_hash": "sha256:" + "a" * 64,
    },
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
            "journal": _journal(),
            "passport": {"error": "boom"},
        },
    )
    assert "passport_digest_missing" in gate.reasons


# -- empty-journal / record-count boundary (independent-review P0) -------------


def _journal(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "record_count": 1,
        "chain_verified": True,
        "head_hash": "sha256:" + "a" * 64,
    }
    base.update(overrides)
    return base


def test_zero_record_count_fails_closed() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": _journal(record_count=0, head_hash=None),
            "passport": {"passport_digest": "sha256:" + "b" * 64},
        },
    )
    assert gate.proof_complete is False
    assert "journal_empty" in gate.reasons
    assert gate.outcome_status == EXECUTED_UNVERIFIED


def test_missing_record_count_fails_closed() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": {"chain_verified": True, "head_hash": "sha256:" + "a" * 64},
            "passport": {"passport_digest": "sha256:" + "b" * 64},
        },
    )
    assert gate.proof_complete is False
    assert "journal_empty" in gate.reasons


def test_null_record_count_fails_closed() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": _journal(record_count=None),
            "passport": {"passport_digest": "sha256:" + "b" * 64},
        },
    )
    assert "journal_empty" in gate.reasons


def test_boolean_true_record_count_fails_closed() -> None:
    # ``True`` is an ``int`` subclass; it must never masquerade as a positive count.
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": _journal(record_count=True),
            "passport": {"passport_digest": "sha256:" + "b" * 64},
        },
    )
    assert "journal_empty" in gate.reasons


def test_negative_record_count_fails_closed() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": _journal(record_count=-1),
            "passport": {"passport_digest": "sha256:" + "b" * 64},
        },
    )
    assert "journal_empty" in gate.reasons


def test_positive_count_without_chain_head_fails_closed() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": _journal(record_count=2, head_hash=None),
            "passport": {"passport_digest": "sha256:" + "b" * 64},
        },
    )
    assert gate.proof_complete is False
    assert "journal_head_missing" in gate.reasons


def test_valid_non_empty_journal_passes() -> None:
    gate = evaluate_proof_gate(
        verdict=CheckVerdict.PASS,
        attempt_identity="job-1:attempt1",
        provenance_reference={
            "journal": _journal(record_count=4),
            "passport": {"passport_digest": "sha256:" + "b" * 64},
        },
    )
    assert gate.proof_complete is True
    assert gate.reasons == ()


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
