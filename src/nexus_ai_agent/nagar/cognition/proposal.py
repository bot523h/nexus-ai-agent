"""Typed proposal / refusal — the only two things a producer may return.

A producer's output is **untrusted**.  It is parsed through one fail-closed
gate, :func:`parse_proposal`, which always returns either a validated
:class:`TypedProposal` or an explicit :class:`Refusal` — never a partial
object, never an exception the caller must guess at.

The proposal carries no authority: it names an *operation the caller already
offered* and an input payload.  It cannot carry an actor, a permission, an
execution mode, a confirmation flag or a shell — those keys are refused by
name (:data:`_AUTHORITY_FIELDS`) and, more fundamentally, by
``extra="forbid"``.  Turning a proposal into an executable command is a
*separate*, caller-owned step (:func:`nexus_ai_agent.nagar.cognition.bridge.proposal_to_command`)
that still has to pass the CommandBus authorization pipeline.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from nexus_ai_agent.nagar.cognition.context import ProducerIdentity, ProposalSchema

PROPOSAL_SCHEMA_VERSION: int = 1

#: Hard cap on a proposal payload, matching the command bus input bound
#: (``studio/models.py::_MAX_COMMAND_INPUT_BYTES``).  A producer cannot make
#: the boundary allocate an unbounded amount of memory.
_MAX_PROPOSAL_INPUT_BYTES: int = 512 * 1024

#: Keys that would turn a proposal into authority.  Refused by name so the
#: refusal reason is specific (and testable), not just "schema violation".
_AUTHORITY_FIELDS: frozenset[str] = frozenset(
    {
        "actor",
        "permissions",
        "permission",
        "grants",
        "grant",
        "authorization",
        "authorizer",
        "execution_policy",
        "confirmed",
        "role",
        "privilege",
        "privileges",
        "capability_snapshot",
        "shell",
        "command",
        "argv",
        "sudo",
        "env",
        "network_access",
    }
)


class RefusalReason(str, Enum):
    """Why a proposal was refused.  Deterministic, stable, testable."""

    PRODUCER_REFUSED = "producer_refused"  # explicit, honest "I cannot"
    MALFORMED = "malformed"  # not parseable as the declared structure
    SCHEMA_VIOLATION = "schema_violation"  # failed typed validation
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    DISALLOWED_OPERATION = "disallowed_operation"  # outside the offered set
    AUTHORITY_FIELD = "authority_field"  # tried to carry authority
    NON_FINITE = "non_finite"  # NaN / Infinity in the payload
    BUDGET_EXHAUSTED = "budget_exhausted"  # producer exceeded its budget


class ProposalProvenance(BaseModel):
    """Who/what produced the proposal and when.  Descriptive, not evidential."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    producer: ProducerIdentity
    created_at: str = Field(min_length=1, max_length=64)
    #: Free-form origin label (model id, "null", "rule:trim", ...).
    source: str = Field(default="", max_length=256)


class TypedProposal(BaseModel):
    """A validated proposal.  Still **not** an executable command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: str = Field(min_length=1, max_length=128)
    schema_version: int = Field(strict=True, ge=1)
    operation: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=2_000)
    confidence: float = Field(strict=True, default=0.0, ge=0.0, le=1.0)
    provenance: ProposalProvenance

    @field_validator("input")
    @classmethod
    def _bounded_finite_input(cls, value: dict[str, Any]) -> dict[str, Any]:
        # ``strict`` on the scalar fields stops bool→int/float coercion; this
        # guards the free-form payload: it must be finite JSON within the same
        # 512 KiB bound the command bus enforces, so a producer cannot make
        # the boundary allocate unbounded memory or smuggle NaN/Infinity.
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("proposal input must be finite JSON data") from exc
        if len(encoded.encode("utf-8")) > _MAX_PROPOSAL_INPUT_BYTES:
            raise ValueError("proposal input exceeds 512 KiB")
        return value


class Refusal(BaseModel):
    """An explicit, structured non-answer.  Never a fabricated proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: RefusalReason
    detail: str = Field(default="", max_length=2_000)
    provenance: ProposalProvenance | None = None

    @property
    def refused(self) -> bool:
        return True


def _refusal(
    reason: RefusalReason,
    detail: str,
    provenance: ProposalProvenance | None,
) -> Refusal:
    return Refusal(reason=reason, detail=detail, provenance=provenance)


class _NonFiniteJSON(ValueError):
    """Raised when JSON contains NaN/Infinity (which no bound can survive)."""


def _reject_constant(value: str) -> None:
    raise _NonFiniteJSON(f"non-finite JSON number: {value}")


def _loads(raw: str | bytes) -> Any:
    return json.loads(raw, parse_constant=_reject_constant)


def _assert_finite(payload: Any) -> None:
    """Reject NaN/Infinity anywhere in an already-parsed payload."""
    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise _NonFiniteJSON(str(exc)) from exc


def parse_proposal(
    raw: object,
    schema: ProposalSchema,
    *,
    provenance: ProposalProvenance | None = None,
) -> TypedProposal | Refusal:
    """Parse a producer's raw output against ``schema`` — fail closed.

    ``raw`` is typed ``object`` on purpose: producer output is untrusted and
    may be *anything*.  Accepts an already-typed object (revalidated, never
    trusted blindly), a mapping, or JSON text/bytes.  Any problem yields a
    :class:`Refusal` with a specific reason; this function never raises for
    bad *producer* output (programming errors on ``schema`` still surface as
    normal exceptions).
    """
    # 1. An explicit refusal passes through (it is a valid, honest answer).
    if isinstance(raw, Refusal):
        return raw

    # 2. Normalize to a mapping, catching malformed/JSON and non-finite input.
    payload: Any
    if isinstance(raw, TypedProposal):
        payload = raw.model_dump(mode="json")
    elif isinstance(raw, (str, bytes)):
        try:
            payload = _loads(raw)
        except _NonFiniteJSON as exc:
            return _refusal(RefusalReason.NON_FINITE, f"non-finite number: {exc}", provenance)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            return _refusal(RefusalReason.MALFORMED, f"not valid JSON: {exc}", provenance)
    elif isinstance(raw, dict):
        payload = raw
    else:
        return _refusal(
            RefusalReason.MALFORMED, f"unsupported proposal type: {type(raw).__name__}", provenance
        )

    if not isinstance(payload, dict):
        return _refusal(RefusalReason.MALFORMED, "proposal must be a JSON object", provenance)

    # 3. Named authority fields are refused *before* generic validation so the
    #    reason is specific: a model may never smuggle authority through.
    smuggled = sorted(_AUTHORITY_FIELDS.intersection(payload))
    if smuggled:
        return _refusal(
            RefusalReason.AUTHORITY_FIELD,
            f"proposal carries authority fields: {', '.join(smuggled)}",
            provenance,
        )

    # 4. Non-finite numbers defeat every bounded claim; refuse them by name.
    try:
        _assert_finite(payload)
    except _NonFiniteJSON as exc:
        return _refusal(RefusalReason.NON_FINITE, f"non-finite number: {exc}", provenance)

    # 5. Version gate: anything that is not the exact supported int is refused,
    #    never coerced.  ``bool`` is excluded explicitly (``True == 1``).
    version = payload.get("schema_version")
    if version is not None and (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != PROPOSAL_SCHEMA_VERSION
    ):
        return _refusal(
            RefusalReason.UNSUPPORTED_SCHEMA_VERSION,
            f"unsupported proposal schema_version: {version!r}",
            provenance,
        )

    # 6. Typed validation (extra="forbid" rejects any unlisted key).
    try:
        proposal = TypedProposal.model_validate(payload)
    except ValidationError as exc:
        detail = exc.errors()[0].get("msg", "validation failed") if exc.errors() else "invalid"
        return _refusal(
            RefusalReason.SCHEMA_VIOLATION,
            f"proposal failed validation: {detail}",
            provenance,
        )

    # 7. The operation must be one the caller offered; the model cannot widen
    #    the candidate set.  An empty allow-set means "nothing is offered".
    if not schema.permits(proposal.operation):
        return _refusal(
            RefusalReason.DISALLOWED_OPERATION,
            f"operation {proposal.operation!r} is not in schema {schema.schema_id!r}",
            provenance,
        )

    # 8. The proposal's schema id must match the schema it was validated
    #    against; a mismatch means the producer answered a different question.
    if proposal.schema_id != schema.schema_id:
        return _refusal(
            RefusalReason.SCHEMA_VIOLATION,
            f"schema_id {proposal.schema_id!r} does not match {schema.schema_id!r}",
            provenance,
        )

    return proposal


__all__ = [
    "PROPOSAL_SCHEMA_VERSION",
    "ProposalProvenance",
    "Refusal",
    "RefusalReason",
    "TypedProposal",
    "parse_proposal",
]
