"""Canonical Typed Command Envelope — Gate 2.

This is the boundary between AI intent and runtime execution.

Design principles (from mission):
* Every field must have: necessity, consumer, validation, security impact, test.
* No raw LLM output may reach executor directly.
* Must support: command_id, operation_id, capability_id, schema_version,
  project_id, revision_id/base_revision, actor/principal, authorization context,
  input, target, moment/range, policy context, locality policy, idempotency key,
  dry_run/preview intent, trace/provenance.

Versioning strategy:
* envelope_version = "nagar.command.v2" (new canonical, backward compatible with v1)
* operation schema version is carried per operation in CapabilityContract.schema_versions
* capability contract version is CapabilityContract.version

Authorization boundary (enforced order):
    LLM output
      ↓ Parse
      ↓ Schema validation
      ↓ Capability existence
      ↓ Authorization
      ↓ Policy (locality, reversibility, confirmation)
      ↓ Resource / locality checks
      ↓ Idempotency
      ↓ Command Bus
      ↓ Execution

This module implements the data contract and validation; the bus enforces the order.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.contracts.capability import LocalityPolicy

# ---------------------------------------------------------------------------
# Supporting contexts — each field has documented necessity/consumer/security
# ---------------------------------------------------------------------------


class ActorContext(BaseModel):
    """Who is requesting the command.

    Necessity: audit, ownership, permission checks.
    Consumer: authorization gate, bus, provenance.
    Security: prevents privilege escalation via guessed IDs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(min_length=1, description="User or agent principal")
    actor_type: Literal["user", "agent", "system"] = Field(default="user")
    roles: tuple[str, ...] = Field(default=(), description="RBAC roles if any")


class AuthorizationContext(BaseModel):
    """Explicit authorization payload.

    Necessity: C-level operations require explicit confirmation.
    Consumer: Permission gate in CapabilityRegistry.
    Security: confirmation flag must be explicit, not inferred.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmed: bool = Field(default=False, description="Has user confirmed?")
    confirmation_token: str | None = Field(
        default=None, description="Optional token for confirmation flow"
    )
    permission_level: Literal["A", "B", "C", "D"] | None = Field(default=None)


class PolicyContext(BaseModel):
    """Policy and locality constraints.

    Necessity: product rule — private media must not silently go to cloud.
    Consumer: pack registry, execution adapter, SSRF guard.
    Security: LOCAL_ONLY must be enforced before any network call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    locality: LocalityPolicy = Field(default=LocalityPolicy.LOCAL_ONLY)
    allow_cloud: bool = Field(default=False)
    require_preview: bool = Field(default=False)
    reversible_only: bool = Field(default=False)

    @model_validator(mode="after")
    def _check_locality_consistency(self) -> PolicyContext:
        # If LOCAL_ONLY, allow_cloud must be False
        if self.locality == LocalityPolicy.LOCAL_ONLY and self.allow_cloud:
            raise ValueError("LOCAL_ONLY cannot have allow_cloud=True")
        # If EXPLICIT_CLOUD, allow_cloud must be True
        if self.locality == LocalityPolicy.EXPLICIT_CLOUD and not self.allow_cloud:
            raise ValueError("EXPLICIT_CLOUD requires allow_cloud=True")
        return self


class TargetRef(BaseModel):
    """Target of the command — project, clip, track, etc.

    Necessity: operations are non-destructive and target-specific.
    Consumer: reducer handler.
    Security: must not allow path traversal or arbitrary file writes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str | None = Field(default=None)
    timeline_id: str | None = Field(default=None)
    track_id: str | None = Field(default=None)
    clip_id: str | None = Field(default=None)
    asset_id: str | None = Field(default=None)
    # Generic extra targets for future packs
    subject_id: str | None = Field(default=None)
    extra: dict[str, str] = Field(default_factory=dict)


class MomentRange(BaseModel):
    """Time reference — canonical microseconds, frame derived.

    Necessity: VFR safety, immutable reference capture.
    Consumer: reference resolver, reducer.
    Security: timecode must be >=0, range ordered.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    timecode_us: int | None = Field(default=None, ge=0)
    start_us: int | None = Field(default=None, ge=0)
    end_us: int | None = Field(default=None, ge=0)
    frame_number: int | None = Field(default=None, ge=0)
    captured_at_command: bool = Field(default=True)

    @model_validator(mode="after")
    def _validate_range(self) -> MomentRange:
        if self.start_us is not None and self.end_us is not None:
            if self.start_us >= self.end_us:
                raise ValueError("start_us must be < end_us")
        return self


class Provenance(BaseModel):
    """Trace / provenance for audit.

    Necessity: evidence, debugging, idempotency replay.
    Consumer: observability, logs.
    Security: no PII, no secrets.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str = Field(default_factory=lambda: f"tr_{uuid4().hex[:12]}")
    session_id: str | None = Field(default=None)
    parent_command_id: str | None = Field(default=None)
    llm_model: str | None = Field(default=None)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Canonical Command Envelope
# ---------------------------------------------------------------------------


class CommandEnvelope(BaseModel):
    """Canonical Typed Command Envelope — the only input the studio accepts.

    Versioning:
    * envelope_version: "nagar.command.v2" (canonical), accepts "nagar.command.v1" for compat
    * operation schema version: implicit via operation's input model (Pydantic)
    * capability contract version: CapabilityContract.version

    Field necessity matrix (each field has consumer/validation/security/test):
    See docs/contracts/COMMAND_ENVELOPE.md for full table.

    Summary: command_id, operation_id, capability_id, schema_version,
    project_id, base_revision, actor, authorization, input, target,
    moment_range, policy_context, idempotency_key, dry_run/preview,
    provenance — each has necessity, consumer, validation, security, test.
    """

    model_config = ConfigDict(extra="forbid")

    # Identity
    command_id: str = Field(
        min_length=1,
        default_factory=lambda: f"cmd_{uuid4().hex[:12]}",
        description="Unique command ID — consumer: bus idempotency, history",
    )
    operation_id: str = Field(
        min_length=1, description="Operation to execute — must exist in registry"
    )
    capability_id: str | None = Field(
        default=None, description="Capability providing operation — must match registry"
    )

    # Versioning
    envelope_version: Literal["nagar.command.v1", "nagar.command.v2"] = Field(
        default="nagar.command.v2", description="Envelope schema version"
    )
    schema_version: str = Field(
        default="1.0.0", description="Operation input schema version — for future evolution"
    )

    # Scoping
    project_id: str | None = Field(default=None, description="Project scope — for auth")
    base_revision: int | None = Field(
        default=None, ge=0, description="Optimistic concurrency — base revision"
    )
    base_state_hash: str | None = Field(
        default=None, description="Optimistic concurrency — base state hash"
    )

    # Actor & Auth
    actor: ActorContext = Field(default_factory=ActorContext)
    authorization: AuthorizationContext = Field(default_factory=AuthorizationContext)

    # Payload
    input: dict[str, Any] = Field(
        default_factory=dict, description="Operation input — validated per op"
    )
    target: TargetRef = Field(default_factory=TargetRef)
    moment_range: MomentRange | None = Field(default=None, description="Time reference")

    # Policy
    policy_context: PolicyContext = Field(default_factory=PolicyContext)

    # Idempotency & Execution mode
    idempotency_key: str | None = Field(default=None, description="Idempotency — deterministic")
    dry_run: bool = Field(default=False, description="If true, validate but do not commit")
    preview_intent: bool = Field(default=False, description="If true, preview only")

    # Trace
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="after")
    def _validate_locality_policy(self) -> CommandEnvelope:
        # If policy says LOCAL_ONLY, envelope must not request cloud
        if self.policy_context.locality == LocalityPolicy.LOCAL_ONLY:
            # No explicit cloud egress allowed
            if self.policy_context.allow_cloud:
                raise ValueError("LOCAL_ONLY policy cannot allow cloud")
        return self

    def to_legacy_typed_command(self) -> dict[str, Any]:
        """Convert to legacy TypedCommand dict for backward compat with existing bus."""
        return {
            "protocol_version": "nagar.command.v1",
            "command_id": self.command_id,
            "session_id": self.provenance.session_id,
            "operation": self.operation_id,
            "target": {
                "project_id": self.target.project_id,
                "track_id": self.target.track_id,
                "clip_id": self.target.clip_id,
            },
            "input": self.input,
            "preconditions": {
                "state_revision": self.base_revision,
                "state_hash": self.base_state_hash,
            },
            "idempotency_key": self.idempotency_key,
            "confirmed": self.authorization.confirmed,
        }


# ---------------------------------------------------------------------------
# Validation functions — explicit boundary checks (no fake impl)
# ---------------------------------------------------------------------------


class CommandValidationError(Exception):
    pass


class UnknownOperationError(CommandValidationError):
    pass


class UnavailableCapabilityError(CommandValidationError):
    pass


class AuthorizationError(CommandValidationError):
    pass


class LocalityViolationError(CommandValidationError):
    pass


class RevisionConflictError(CommandValidationError):
    pass


def validate_envelope(
    envelope: CommandEnvelope,
    *,
    known_operations: set[str],
    known_capabilities: set[str] | None = None,
    operation_to_capability: dict[str, str] | None = None,
    current_revision: int | None = None,
    current_hash: str | None = None,
    allowed_locality: set[LocalityPolicy] | None = None,
) -> None:
    """Validate envelope against known reality — pure, no I/O.

    Raises typed errors for each contract violation.
    This is the explicit boundary before bus dispatch.
    """
    # Unknown operation
    if envelope.operation_id not in known_operations:
        raise UnknownOperationError(f"unknown operation: {envelope.operation_id!r}")

    # Unavailable capability
    if known_capabilities is not None and envelope.capability_id is not None:
        if envelope.capability_id not in known_capabilities:
            raise UnavailableCapabilityError(
                f"capability {envelope.capability_id!r} not available "
                f"for operation {envelope.operation_id!r}"
            )

    # Capability mapping check
    if operation_to_capability is not None:
        expected_cap = operation_to_capability.get(envelope.operation_id)
        if expected_cap and envelope.capability_id and envelope.capability_id != expected_cap:
            raise UnavailableCapabilityError(
                f"operation {envelope.operation_id!r} belongs to "
                f"{expected_cap!r}, not {envelope.capability_id!r}"
            )

    # Authorization — for now, just check principal exists for B/C levels
    # Real auth would check RBAC; here we ensure actor is present
    # Handle both model and dict (model_construct bypasses validation)
    principal_id = None
    if isinstance(envelope.actor, dict):
        principal_id = envelope.actor.get("principal_id")
    else:
        principal_id = getattr(envelope.actor, "principal_id", None)
    if not principal_id:
        raise AuthorizationError("actor.principal_id required")

    # Locality violation
    if allowed_locality is not None:
        locality = None
        if isinstance(envelope.policy_context, dict):
            locality = envelope.policy_context.get("locality")
            # If locality is string, convert to enum if possible
            if isinstance(locality, str):
                try:
                    locality = LocalityPolicy(locality)
                except ValueError:
                    pass
        else:
            locality = getattr(envelope.policy_context, "locality", None)
        if locality not in allowed_locality:
            raise LocalityViolationError(
                f"locality {locality} not allowed, allowed={allowed_locality}"
            )

    # Revision conflict
    if current_revision is not None and envelope.base_revision is not None:
        if envelope.base_revision != current_revision:
            raise RevisionConflictError(
                f"stale base_revision: envelope expects "
                f"{envelope.base_revision}, current is {current_revision}"
            )
    if current_hash is not None and envelope.base_state_hash is not None:
        if envelope.base_state_hash != current_hash:
            raise RevisionConflictError("stale base_state_hash")
