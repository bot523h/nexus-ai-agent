"""Proof-completeness gate for an IBPC execution (Phase 2).

Rendering completion is not proof of verified success.  A *proof-bearing*
IBPC capsule may be created only when **all** required evidence is present:

* the independent verifier verdict is exactly ``PASS``;
* an immutable execution attempt identity exists (a real ``attemptN``, never a
  placeholder ``attempt?``);
* the provenance journal is present and its hash chain verifies;
* the passport is present and carries a digest.

Any missing/failed element is a typed reason, and the outcome degrades to
``executed_unverified`` — the artifact is reported honestly, its integrity is
never claimed as proved, and no proof-bearing capsule is minted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexus_ai_agent.nagar.ibpc.models import CheckVerdict

#: Outcome statuses that distinguish artifact execution from verified success.
EXECUTED_VERIFIED = "executed"
EXECUTED_UNVERIFIED = "executed_unverified"

#: The honest unknown-attempt spelling the service uses when no attempt provider
#: can name the immutable attempt.  A proof gate treats it as "missing".
UNKNOWN_ATTEMPT_SUFFIX = ":attempt?"


@dataclass(frozen=True)
class ProofGate:
    """The proof-completeness decision for one execution."""

    proof_complete: bool
    reasons: tuple[str, ...]

    @property
    def outcome_status(self) -> str:
        return EXECUTED_VERIFIED if self.proof_complete else EXECUTED_UNVERIFIED


def evaluate_proof_gate(
    *,
    verdict: CheckVerdict,
    attempt_identity: str | None,
    provenance_reference: dict[str, Any],
) -> ProofGate:
    """Decide whether a proof-bearing capsule may be minted.

    Fail-closed: every unmet requirement adds a typed reason and the capsule is
    withheld.  Never upgrades INCONCLUSIVE/FAIL to a verified success, never
    invents an attempt identity, never treats degraded provenance data as
    complete evidence.
    """
    reasons: list[str] = []

    if verdict is not CheckVerdict.PASS:
        reasons.append(f"verdict_not_pass:{verdict.value}")

    if not attempt_identity or attempt_identity.endswith(UNKNOWN_ATTEMPT_SUFFIX):
        reasons.append("attempt_identity_missing")

    journal = provenance_reference.get("journal")
    if not isinstance(journal, dict):
        reasons.append("journal_missing")
    elif journal.get("chain_verified") is not True:
        reasons.append("journal_chain_unverified")

    passport = provenance_reference.get("passport")
    if not isinstance(passport, dict):
        reasons.append("passport_missing")
    else:
        digest = passport.get("passport_digest")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            reasons.append("passport_digest_missing")

    return ProofGate(proof_complete=not reasons, reasons=tuple(reasons))


__all__ = [
    "EXECUTED_UNVERIFIED",
    "EXECUTED_VERIFIED",
    "UNKNOWN_ATTEMPT_SUFFIX",
    "ProofGate",
    "evaluate_proof_gate",
]
