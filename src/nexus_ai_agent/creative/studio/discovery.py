"""Capability discovery contract — the Assistant-facing capability surface.

The command bus is the *write* authority; this module is the *read* authority.
``build_capability_surface`` projects a :class:`CapabilityRegistry` into a
typed, JSON-serializable, deterministic :class:`CapabilitySurface` — the only
capability view an Assistant (Agent 2) is allowed to plan against.

Law 4 projection (fail-closed, runtime truth only):

* an operation whose capability is marked unavailable is never advertised;
* an operation whose required packs include an unknown id, a ``STUB`` or a
  ``RETIRED`` pack is never advertised;
* an operation on an ``EXPERIMENTAL`` pack is advertised only when the caller
  passes ``include_experimental=True`` — mirroring the bus composition flag
  (``CommandBus(allow_experimental=...)``). The flag is composition-root
  state; a client cannot opt itself in through the envelope;
* every exclusion is recorded with a typed reason code and **no permission
  data** — the surface tells the Assistant what it can run, never what it
  would need to become someone else.

The surface is deterministic: same registry + same flag ⇒ byte-identical
canonical JSON and an identical :func:`surface_identity` hash (no clock, no
randomness, sorted iteration). Versioning: ``nagar.discovery.v1``, additive
only — new optional fields may be added, existing keys never renamed.

This module also publishes the typed error contract
(:func:`error_code_of` / :data:`ERROR_CONTRACT`): every :class:`NagarError`
raised by the dispatch pipeline maps to a stable string code a downstream
agent can branch on, most-derived class first.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.studio.capabilities import (
    CapabilityRegistry,
    ExecutionMode,
    PreviewSemantics,
)
from nexus_ai_agent.creative.studio.lifecycle import (
    LifecycleState,
    PackRequirementError,
    pack_lifecycle,
)
from nexus_ai_agent.creative.studio.models import (
    AuthorizationError,
    CapabilityError,
    CapabilityVersionError,
    CommandExecutionError,
    CommandResult,
    CommandValidationError,
    ExecutionPolicyError,
    IdempotencyConflictError,
    InputReferenceError,
    NagarError,
    PermissionDeniedError,
    PermissionLevel,
    PreconditionError,
    ReferenceResolutionError,
    UndoStackEmptyError,
    UnknownCapabilityError,
    UnknownOperationError,
)

DISCOVERY_PROTOCOL_VERSION: Literal["nagar.discovery.v1"] = "nagar.discovery.v1"


class ExclusionReason(str, Enum):
    """Why an operation is hidden from the Assistant surface (typed, no secrets)."""

    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    PACK_UNKNOWN = "pack_unknown"
    PACK_STUB = "pack_stub"
    PACK_RETIRED = "pack_retired"
    PACK_EXPERIMENTAL_NOT_OPTED_IN = "pack_experimental_not_opted_in"


class PackStateReport(BaseModel):
    """Lifecycle truth for one required pack, as resolved at discovery time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str
    state: LifecycleState


class OperationSurface(BaseModel):
    """One advertised operation: everything an Assistant needs to plan a command.

    Fields mirror the authoritative registry view (``describe()``) plus the
    resolved pack lifecycle states — the exact facts the bus will re-verify
    at dispatch. A snapshot built here can never authorize anything: dispatch
    re-runs every gate against the live registry.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    capability_id: str
    capability_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str
    permission_level: PermissionLevel
    required_permissions: tuple[str, ...]
    execution_modes: tuple[ExecutionMode, ...]
    preview_semantics: PreviewSemantics | None
    operation_schema_version: int = Field(ge=1)
    input_schema: dict[str, Any]
    required_packs: tuple[str, ...]
    pack_lifecycle_states: tuple[PackStateReport, ...]
    pack_provider: str
    deterministic: bool


class ExcludedOperation(BaseModel):
    """A hidden operation with its typed exclusion reason (never permission data)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    reason: ExclusionReason
    detail: str = ""


class CapabilitySurface(BaseModel):
    """The versioned, deterministic read projection of one registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["nagar.discovery.v1"] = DISCOVERY_PROTOCOL_VERSION
    include_experimental: bool
    operations: tuple[OperationSurface, ...]
    excluded: tuple[ExcludedOperation, ...]

    @property
    def operation_count(self) -> int:
        return len(self.operations)


def build_capability_surface(
    registry: CapabilityRegistry, *, include_experimental: bool = False
) -> CapabilitySurface:
    """Project ``registry`` into the Assistant-facing surface (Law 4).

    Deterministic and fail-closed: anything the runtime cannot guarantee is
    excluded with a typed reason instead of being advertised as a maybe.
    """
    operations: list[OperationSurface] = []
    excluded: list[ExcludedOperation] = []

    for operation_id in registry.list_operations():
        spec = registry.get_spec(operation_id)
        descriptor = registry.describe(operation_id)
        capability = registry.get_capability(descriptor.capability_id)

        if not descriptor.available or not capability.available:
            excluded.append(
                ExcludedOperation(
                    operation_id=operation_id,
                    reason=ExclusionReason.CAPABILITY_UNAVAILABLE,
                    detail=descriptor.capability_id,
                )
            )
            continue

        pack_states: list[PackStateReport] = []
        refusal: ExcludedOperation | None = None
        for pack_id in descriptor.required_packs:
            try:
                record = pack_lifecycle(pack_id)
            except PackRequirementError:
                refusal = ExcludedOperation(
                    operation_id=operation_id,
                    reason=ExclusionReason.PACK_UNKNOWN,
                    detail=pack_id,
                )
                break
            if record.state is LifecycleState.STUB:
                refusal = ExcludedOperation(
                    operation_id=operation_id,
                    reason=ExclusionReason.PACK_STUB,
                    detail=pack_id,
                )
                break
            if record.state is LifecycleState.RETIRED:
                refusal = ExcludedOperation(
                    operation_id=operation_id,
                    reason=ExclusionReason.PACK_RETIRED,
                    detail=pack_id,
                )
                break
            if record.state is LifecycleState.EXPERIMENTAL and not include_experimental:
                refusal = ExcludedOperation(
                    operation_id=operation_id,
                    reason=ExclusionReason.PACK_EXPERIMENTAL_NOT_OPTED_IN,
                    detail=pack_id,
                )
                break
            pack_states.append(PackStateReport(pack_id=pack_id, state=record.state))

        if refusal is not None:
            excluded.append(refusal)
            continue

        operations.append(
            OperationSurface(
                operation_id=operation_id,
                capability_id=descriptor.capability_id,
                capability_version=descriptor.version,
                description=spec.description,
                permission_level=spec.permission_level,
                required_permissions=spec.effective_permissions,
                execution_modes=spec.execution_modes,
                preview_semantics=spec.preview_semantics,
                operation_schema_version=descriptor.operation_schema_version,
                input_schema=descriptor.operation_schema,
                required_packs=descriptor.required_packs,
                pack_lifecycle_states=tuple(pack_states),
                pack_provider=descriptor.pack_provider,
                deterministic=spec.deterministic,
            )
        )

    return CapabilitySurface(
        include_experimental=include_experimental,
        operations=tuple(operations),
        excluded=tuple(excluded),
    )


def surface_canonical_json(surface: CapabilitySurface) -> str:
    """Canonical JSON of the surface (sorted keys, no whitespace, no NaN)."""
    return json.dumps(
        surface.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def surface_identity(surface: CapabilitySurface) -> str:
    """Content hash of the surface: identical registries ⇒ identical identity."""
    return "sha256:" + hashlib.sha256(surface_canonical_json(surface).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Typed error contract: every dispatch failure maps to a stable string code.
# ---------------------------------------------------------------------------

#: Ordered (exception type → code); the first ``isinstance`` match wins, so
#: the most-derived classes come first. Codes are a contract: once published
#: they are never renamed (additive evolution only).
ERROR_CONTRACT: tuple[tuple[type[NagarError], str], ...] = (
    (UnknownOperationError, "unknown_operation"),
    (IdempotencyConflictError, "idempotency_conflict"),
    (CommandValidationError, "command_validation"),
    (AuthorizationError, "authorization"),
    (ExecutionPolicyError, "execution_policy"),
    (PermissionDeniedError, "permission_denied"),
    (UnknownCapabilityError, "unknown_capability"),
    (CapabilityVersionError, "capability_version"),
    (CapabilityError, "capability_unavailable"),
    (InputReferenceError, "input_reference"),
    (ReferenceResolutionError, "reference_resolution"),
    (PreconditionError, "precondition"),
    (UndoStackEmptyError, "undo_stack_empty"),
    (CommandExecutionError, "execution_failed"),
    (PackRequirementError, "pack_requirement"),
    (NagarError, "nagar_error"),
)


def error_code_of(error: NagarError) -> str:
    """The stable contract code for one dispatch failure (most-derived first)."""
    for exc_type, code in ERROR_CONTRACT:
        if isinstance(error, exc_type):
            return code
    return "nagar_error"  # unreachable today; kept fail-safe, never silent


def is_master_evidence(result: CommandResult) -> bool:
    """May this result be used as master evidence (contract doc §13)?

    Fail-closed on the bus stamp: only a ``CommandResult`` whose diagnostics
    carry ``authoritative is True`` qualifies. A preview execution of a
    ``non_authoritative_realization`` operation is stamped ``False``; an
    unstamped (hand-built or legacy) result is never evidence. There is no
    promotion path — master authority requires a ``local`` execution.
    """
    return result.diagnostics.get("authoritative") is True


__all__ = [
    "DISCOVERY_PROTOCOL_VERSION",
    "ERROR_CONTRACT",
    "CapabilitySurface",
    "ExcludedOperation",
    "ExclusionReason",
    "OperationSurface",
    "PackStateReport",
    "build_capability_surface",
    "error_code_of",
    "is_master_evidence",
    "surface_canonical_json",
    "surface_identity",
]
