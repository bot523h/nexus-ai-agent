"""Nagar Gate 2 — Operation Contract Reconciliation.

This package owns the canonical boundary between Product Intent and Runtime Execution:

* Operation Contract Matrix (70 product catalog vs 57 runtime reality)
* Capability Contract (what the runtime can execute, with policy)
* Command Envelope (typed, versioned, validated, authorized)

Design constraints (Gate 2):

* No rendering implementation — only contract, validation, and evidence classification.
* No dependency on storage/llm/bot layers — stdlib + pydantic + studio L1 only.
* No fake implementation to inflate registry counts — gaps are recorded as NOT_VERIFIED.

Owned by Agent 2, read-only dependencies on Agent 1 runtime and Agent 3 TDD.
"""

from nexus_ai_agent.creative.contracts.capability import (
    AvailabilityState,
    CapabilityContract,
    ExecutionClass,
    LocalityPolicy,
    Reversibility,
)
from nexus_ai_agent.creative.contracts.command_envelope import (
    ActorContext,
    AuthorizationContext,
    CommandEnvelope,
    PolicyContext,
    Provenance,
)
from nexus_ai_agent.creative.contracts.command_envelope import (
    TargetRef as EnvelopeTargetRef,
)
from nexus_ai_agent.creative.contracts.l0_l4 import (
    EvidenceClass,
    LLevel,
    LLevelDefinition,
    canonical_l_levels,
)
from nexus_ai_agent.creative.contracts.operation_matrix import (
    OperationContractRow,
    build_canonical_matrix,
    canonical_70_catalog,
    runtime_57_snapshot,
)

__all__ = [
    "AvailabilityState",
    "ActorContext",
    "AuthorizationContext",
    "CapabilityContract",
    "CommandEnvelope",
    "EvidenceClass",
    "ExecutionClass",
    "LLevel",
    "LLevelDefinition",
    "LocalityPolicy",
    "OperationContractRow",
    "PolicyContext",
    "Provenance",
    "Reversibility",
    "EnvelopeTargetRef",
    "build_canonical_matrix",
    "canonical_70_catalog",
    "canonical_l_levels",
    "runtime_57_snapshot",
]
