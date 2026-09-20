"""Nagar creative studio -- Wave 1: Green Cockpit core.

A typed, UI-free command surface for the studio: state models, the
hierarchical capability registry, the semantic reference resolver and the
atomic command bus.

Wave 1 guarantees:

* no React/DOM/Canvas -- input is a typed JSON command, output is an
  in-memory state update;
* no heavy dependencies (no torch/transformers/CV);
* no ``storage/`` or ``llm/`` imports from this package.
"""

from __future__ import annotations

from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.capabilities import (
    Capability,
    CapabilityRegistry,
    Domain,
    OperationContext,
    OperationOutcome,
    OperationSpec,
    PermissionDecision,
    build_wave1_registry,
)
from nexus_ai_agent.creative.studio.models import (
    Clip,
    CommandExecutionError,
    CommandResult,
    CommandValidationError,
    EditTransaction,
    Marker,
    MediaRef,
    NagarError,
    PermissionDeniedError,
    PermissionLevel,
    Playhead,
    PreconditionError,
    Project,
    ReferenceResolutionError,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    UndoStackEmptyError,
    UnknownOperationError,
    compute_state_hash,
    frame_number_for,
    new_project,
)
from nexus_ai_agent.creative.studio.references import (
    ReferenceExpr,
    ReferenceInput,
    ReferenceResolver,
)

__all__ = [
    "Capability",
    "CapabilityRegistry",
    "CommandBus",
    "CommandExecutionError",
    "CommandResult",
    "CommandValidationError",
    "Clip",
    "Domain",
    "EditTransaction",
    "Marker",
    "MediaRef",
    "NagarError",
    "OperationContext",
    "OperationOutcome",
    "OperationSpec",
    "PermissionDecision",
    "PermissionDeniedError",
    "PermissionLevel",
    "Playhead",
    "PreconditionError",
    "Project",
    "ReferenceExpr",
    "ReferenceInput",
    "ReferenceResolutionError",
    "ReferenceResolver",
    "Timeline",
    "TimeBase",
    "TimeRangeUS",
    "Track",
    "TypedCommand",
    "UndoStackEmptyError",
    "UnknownOperationError",
    "build_wave1_registry",
    "compute_state_hash",
    "frame_number_for",
    "new_project",
]
