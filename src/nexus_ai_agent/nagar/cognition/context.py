"""Typed, bounded input to a cognition call.

Everything a producer may see is here, and everything here is *data*: there
is no actor, no permission, no capability grant, no shell, no file handle.
A context can never widen authority because it carries no authority.

The context is also the **schema contract**: a producer is asked for a
``ProposalSchema`` and its output is validated against exactly that schema
by :func:`nexus_ai_agent.nagar.cognition.proposal.parse_proposal`.  The
schema names operations that the *caller* already knows are in the
capability registry; the producer only chooses among them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProducerIdentity(BaseModel):
    """Who produced a proposal.  Descriptive provenance, never a grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)


class ProposalSchema(BaseModel):
    """The closed set of operations a proposal may name, plus its JSON schema.

    ``allowed_operations`` is the *intersection* the caller has already
    authorized for this context.  A producer cannot add to it: a proposal
    naming an operation outside this set is refused at parse time, so the
    model boundary can never introduce an operation the caller did not
    offer.  The registry and the bus remain the authorities; this set only
    narrows what a proposal may even *ask* for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: str = Field(min_length=1, max_length=128)
    schema_version: int = Field(ge=1)
    allowed_operations: frozenset[str] = Field(default_factory=frozenset)
    #: Optional JSON-schema-shaped description of the expected ``input``.
    #: Kept as data (never executed) so a producer can be prompted with it.
    input_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("allowed_operations")
    @classmethod
    def _non_blank_operations(cls, value: frozenset[str]) -> frozenset[str]:
        for operation in value:
            if (
                not operation
                or operation != operation.strip()
                or any(c.isspace() for c in operation)
            ):
                raise ValueError(f"invalid operation id in schema: {operation!r}")
        return value

    def permits(self, operation: str) -> bool:
        return operation in self.allowed_operations


class CognitionBudget(BaseModel):
    """A hard, caller-owned bound on one cognition call.

    The producer must respect it; the port enforces the *wall-clock* bound by
    refusing rather than by trusting the producer.  Budget is expressed as
    attempts (bounded retry), wall-clock seconds and an optional token hint.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(ge=1, le=8, default=1)
    max_wall_clock_seconds: float = Field(gt=0, le=600, default=30.0)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)


class CognitionContext(BaseModel):
    """The typed, authority-free payload handed to a cognition producer.

    ``deterministic_facts`` is intentionally a plain JSON mapping: a caller
    projects the exact project/revision facts a producer needs, and nothing
    more.  There is no channel for the producer to read state it was not
    given, and no field it could set to gain one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Stable id of the thing being reasoned about (project/revision/job).
    subject_id: str = Field(min_length=1, max_length=128)
    #: Human intent text, already normalized by the caller.  Untrusted input.
    intent_text: str = Field(default="", max_length=8_000)
    #: A caller-projected, JSON-only view of the deterministic world.
    deterministic_facts: dict[str, Any] = Field(default_factory=dict)
    #: Advisory hints a producer may use; never authority.
    hints: dict[str, Any] = Field(default_factory=dict)

    @field_validator("deterministic_facts", "hints")
    @classmethod
    def _finite_json_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("context facts must be finite JSON data") from exc
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("context facts exceed 256 KiB")
        return value


__all__ = [
    "CognitionBudget",
    "CognitionContext",
    "ProducerIdentity",
    "ProposalSchema",
]
