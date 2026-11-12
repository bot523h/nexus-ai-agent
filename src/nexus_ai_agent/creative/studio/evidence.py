"""Evidence-based operation states (OMEGA-01 §lifecycle) — a second axis.

The studio already has a maturity axis (:mod:`nexus_ai_agent.creative.studio.lifecycle`:
``STUB < EXPERIMENTAL < AVAILABLE``) that answers *"may this pack execute here at
all?"*.  This module adds the orthogonal axis the OMEGA contract needs: *"what,
exactly, has been proven about this operation, and by which evidence?"*

    PLANNED → IMPLEMENTED → EXECUTION_PROVEN → ARTIFACT_VERIFIED → PRODUCTION_PROVEN

The rules that make the ladder meaningful rather than decorative:

* **promotion is evidence-driven, never asserted.**  A caller cannot set
  ``ARTIFACT_VERIFIED``; it must present an :class:`EvidenceBundle` whose
  verifier corroborations reference the *same* artifact hash the execution
  claimed.  A mismatched bundle cannot promote — it reports ``CONTRADICTED``.
* **contradiction is a red state.**  If the verifier says the artifact does not
  decode / does not hash to the claim, the state is ``CONTRADICTED`` regardless
  of what the caller asked for, and :func:`assert_verified` refuses.
* **PRODUCTION_PROVEN needs independent corroboration** (two distinct verifier
  key ids over the same artifact hash), because "it worked once on my laptop" is
  not production evidence.
* **demotion is silent and total.**  There is no "verified with a warning".

This module is a leaf: it imports only the evidence models, so it can never
become a second capability registry (pinned by
``tests/architecture/test_omega_boundaries.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "OperationEvidenceState",
    "EvidenceStateError",
    "VerifierCorroboration",
    "EvidenceBundle",
    "StateAssessment",
    "assess_state",
    "assert_verified",
    "STATE_ORDER",
]


class EvidenceStateError(ValueError):
    """A requested state is not supported by the evidence presented."""


class OperationEvidenceState(str, Enum):
    """How much has actually been proven about one operation."""

    PLANNED = "planned"
    IMPLEMENTED = "implemented"
    EXECUTION_PROVEN = "execution_proven"
    ARTIFACT_VERIFIED = "artifact_verified"
    PRODUCTION_PROVEN = "production_proven"
    #: The evidence contradicts the claim (a red state, never promotable).
    CONTRADICTED = "contradicted"


STATE_ORDER: tuple[OperationEvidenceState, ...] = (
    OperationEvidenceState.PLANNED,
    OperationEvidenceState.IMPLEMENTED,
    OperationEvidenceState.EXECUTION_PROVEN,
    OperationEvidenceState.ARTIFACT_VERIFIED,
    OperationEvidenceState.PRODUCTION_PROVEN,
)


class VerifierCorroboration(BaseModel):
    """One independent verifier's verdict over one artifact hash.

    ``verified=False`` with a ``code`` is a *contradiction*: the verifier looked
    at the artifact and refused it.  ``verified=True`` requires a signature from
    a named key — an unsigned "I checked it myself" is not corroboration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verifier_key_id: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verified: bool
    code: str | None = None
    detail: str | None = None
    signed: bool = False

    @model_validator(mode="after")
    def _signed_means_verified(self) -> VerifierCorroboration:
        if self.signed and not self.verified:
            raise ValueError("a signed corroboration cannot be a refusal")
        return self


class EvidenceBundle(BaseModel):
    """Everything a caller claims about one execution, to be assessed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(min_length=1)
    #: The operation is registered and reachable (handler exists, gate passes).
    implemented: bool = False
    #: The process ran to completion (a real return code, not a plan).
    executed: bool = False
    execution_evidence: dict[str, object] = Field(default_factory=dict)
    artifact_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    corroborations: tuple[VerifierCorroboration, ...] = ()
    #: Distinct episodes/runs the artifact was reproduced in (production breadth).
    production_runs: tuple[str, ...] = ()


class StateAssessment(BaseModel):
    """The assessed state plus exactly why it is not higher."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    state: OperationEvidenceState
    reasons: tuple[str, ...] = ()
    artifact_sha256: str | None = None
    corroborating_verifiers: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return self.state in (
            OperationEvidenceState.ARTIFACT_VERIFIED,
            OperationEvidenceState.PRODUCTION_PROVEN,
        )

    @property
    def attested(self) -> bool:
        """Proven *and* independently signed (the only state worth shipping on)."""
        return bool(self.verified and self.corroborating_verifiers)


def assess_state(bundle: EvidenceBundle) -> StateAssessment:
    """Compute the highest state the presented evidence actually supports."""
    if not bundle.implemented:
        return StateAssessment(
            operation=bundle.operation,
            state=OperationEvidenceState.PLANNED,
            reasons=("no registered handler / gate pass was claimed",),
        )
    if not bundle.executed:
        return StateAssessment(
            operation=bundle.operation,
            state=OperationEvidenceState.IMPLEMENTED,
            reasons=("implementation exists but no execution evidence was presented",),
        )
    if bundle.artifact_sha256 is None:
        return StateAssessment(
            operation=bundle.operation,
            state=OperationEvidenceState.EXECUTION_PROVEN,
            reasons=("the process completed but no artifact hash was claimed",),
        )

    matching = tuple(
        c for c in bundle.corroborations if c.artifact_sha256 == bundle.artifact_sha256
    )
    refusals = tuple(c for c in matching if not c.verified)
    if refusals:
        return StateAssessment(
            operation=bundle.operation,
            state=OperationEvidenceState.CONTRADICTED,
            artifact_sha256=bundle.artifact_sha256,
            reasons=tuple(
                f"{c.verifier_key_id} refused the artifact: {c.code or 'no code'}"
                + (f" ({c.detail})" if c.detail else "")
                for c in refusals
            ),
        )
    signed = tuple(c for c in matching if c.verified and c.signed)
    if not signed:
        reasons = [
            "the execution claimed an artifact, but no signed verifier "
            "corroboration of that exact hash was presented"
        ]
        if bundle.corroborations and not matching:
            reasons.append("corroborations exist, but for a different artifact hash")
        elif matching:
            reasons.append("matching corroborations are unsigned and prove nothing")
        return StateAssessment(
            operation=bundle.operation,
            state=OperationEvidenceState.EXECUTION_PROVEN,
            artifact_sha256=bundle.artifact_sha256,
            reasons=tuple(reasons),
        )
    verifier_ids = tuple(sorted({c.verifier_key_id for c in signed}))
    if len(verifier_ids) >= 2 and len(set(bundle.production_runs)) >= 1:
        return StateAssessment(
            operation=bundle.operation,
            state=OperationEvidenceState.PRODUCTION_PROVEN,
            artifact_sha256=bundle.artifact_sha256,
            corroborating_verifiers=verifier_ids,
        )
    return StateAssessment(
        operation=bundle.operation,
        state=OperationEvidenceState.ARTIFACT_VERIFIED,
        artifact_sha256=bundle.artifact_sha256,
        corroborating_verifiers=verifier_ids,
        reasons=()
        if len(verifier_ids) >= 2
        else ("production proof needs two independent verifier key ids",),
    )


def assert_verified(bundle: EvidenceBundle, *, at_least: OperationEvidenceState) -> StateAssessment:
    """Refuse unless the evidence reaches ``at_least`` (the caller's gate)."""
    assessment = assess_state(bundle)
    if assessment.state is OperationEvidenceState.CONTRADICTED:
        raise EvidenceStateError(
            f"{bundle.operation}: evidence contradicts the claim — "
            + "; ".join(assessment.reasons)
        )
    if at_least not in STATE_ORDER:
        raise EvidenceStateError(f"unknown required state {at_least!r}")
    if STATE_ORDER.index(assessment.state) < STATE_ORDER.index(at_least):
        raise EvidenceStateError(
            f"{bundle.operation}: {assessment.state.value} does not reach "
            f"{at_least.value}: " + "; ".join(assessment.reasons)
        )
    return assessment


def states_for(names: Sequence[str]) -> dict[str, OperationEvidenceState]:
    """Helper for reporting: fresh assessments start at ``PLANNED``."""
    return {name: OperationEvidenceState.PLANNED for name in names}

