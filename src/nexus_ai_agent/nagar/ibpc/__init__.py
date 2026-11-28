"""Intent-Bound Proof Capsules (IBPC) — proof-carrying live editing (NAGAR).

Mission NAGAR-IBPC-LIVE-EDITING-01. One narrowly bounded slice on top of the
existing canonical chain — no new execution authority, no second bus/queue/
renderer/registry/provenance:

    intent text/audio (untrusted input, digest-only)
      -> deterministic parser            (nagar.ibpc.intent — asks, never guesses)
      -> typed AST                       (nagar.ibpc.models)
      -> registry-validated plan         (nagar.ibpc.compiler — live injected registry)
      -> plan-derived preview + readback (nagar.ibpc.preview)
      -> single-use approval binding     (nagar.ibpc.approval — digests + revision + turn)
      -> fenced live session/turns       (nagar.ibpc.session — voice-turn fencing)
      -> execution ONLY via the injected canonical JobQueuePort
                                     (nagar.ibpc.service)
      -> independent footprint verifier  (nagar.ibpc.footprint — decoded pixels, no metadata)
      -> IBPC capsule referencing the existing causal journal + passport

Privacy/trust modes and consent live in nagar.ibpc.privacy; digests use the
single canonical JSON spelling re-exported from nagar.ibpc.canonical.

Docs: docs/engineering/NAGAR_LIVE_EDITING_LIVE_AUDIT.md (M0 audit) and
docs/engineering/NAGAR_IBPC.md (this slice).
"""

from __future__ import annotations

from nexus_ai_agent.nagar.ibpc import (  # noqa: F401 — reachability (law R20)
    approval,
    approval_mac,
    authority,
    canonical,
    compiler,
    composition,
    delta,
    execution_outbox,
    footprint,
    intent,
    models,
    preview,
    privacy,
    proof,
    service,
    session,
    shadow,
    signature,
)
from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalError,
    ApprovalLedger,
    ApprovalLedgerProtocol,
    DurableApprovalLedger,
    canonical_approval_payload,
    compute_approval_hash,
)
from nexus_ai_agent.nagar.ibpc.authority import (
    AuthorityResolution,
    AuthorizationError,
    BindingStatus,
    CanonicalAuthorityError,
    CanonicalAuthorityPort,
    DirectoryAuthority,
    MembershipRole,
    MembershipStatus,
    PrincipalFacts,
    PrincipalRoleAuthority,
    PrincipalRoleFacts,
    PrincipalStatus,
    StudioAction,
    StudioAuthorizationDecision,
    StudioAuthorizationError,
    StudioAuthorizerPort,
    StudioRole,
)
from nexus_ai_agent.nagar.ibpc.compiler import CompilationRefused, compile_ast
from nexus_ai_agent.nagar.ibpc.composition import build_ibpc_service, create_default_key_provider
from nexus_ai_agent.nagar.ibpc.execution_outbox import (
    DurableExecutionOutboxStore,
    ExecutionOutboxCoordinator,
    ExecutionOutboxState,
)
from nexus_ai_agent.nagar.ibpc.intent import (
    Clarification,
    IntentAST,
    Refusal,
    parse_intent,
)
from nexus_ai_agent.nagar.ibpc.models import (
    ApprovalBinding,
    ApprovalState,
    CheckVerdict,
    CompiledPlan,
    DataCategory,
    DurableConsentGrant,
    IBPCCapsule,
    PreviewManifest,
    PrivacyConsentAuditEntry,
    PrivacyMode,
    ProcessingScope,
    Readback,
    SessionStatus,
    TypedCreativeAST,
    VerifierReport,
    VideoTimeScope,
)
from nexus_ai_agent.nagar.ibpc.preview import build_preview, render_readback
from nexus_ai_agent.nagar.ibpc.privacy import (
    DurablePrivacyConsentStore,
    PrivacyPolicy,
    PrivacyViolation,
    redact_mapping,
)
from nexus_ai_agent.nagar.ibpc.proof import (
    EXECUTED_UNVERIFIED,
    EXECUTED_VERIFIED,
    ProofGate,
    evaluate_proof_gate,
)
from nexus_ai_agent.nagar.ibpc.service import (
    ExecutionOutcome,
    IBPCService,
    IBPCServiceDeps,
    PipelineResult,
)
from nexus_ai_agent.nagar.ibpc.session import (
    DuplicateTurnError,
    DurableLiveSessionStore,
    IdempotencyConflictError,
    LiveSessionManager,
    SessionClosedError,
    SessionEpochConflictError,
    SessionError,
    StaleRevisionError,
    StaleTurnError,
    TransitionError,
    TurnState,
    VoiceTurn,
)

__all__ = [
    "EXECUTED_UNVERIFIED",
    "EXECUTED_VERIFIED",
    "ApprovalBinding",
    "ApprovalError",
    "ApprovalLedger",
    "ApprovalLedgerProtocol",
    "ApprovalState",
    "BindingStatus",
    "AuthorizationError",
    "AuthorityResolution",
    "CanonicalAuthorityError",
    "CanonicalAuthorityPort",
    "CheckVerdict",
    "Clarification",
    "CompilationRefused",
    "CompiledPlan",
    "DataCategory",
    "DirectoryAuthority",
    "DuplicateTurnError",
    "DurableApprovalLedger",
    "DurableConsentGrant",
    "DurableLiveSessionStore",
    "DurablePrivacyConsentStore",
    "ExecutionOutcome",
    "IBPCCapsule",
    "IBPCService",
    "IBPCServiceDeps",
    "IdempotencyConflictError",
    "IntentAST",
    "LiveSessionManager",
    "MembershipRole",
    "MembershipStatus",
    "PipelineResult",
    "PreviewManifest",
    "PrincipalFacts",
    "PrincipalRoleAuthority",
    "PrincipalRoleFacts",
    "PrincipalStatus",
    "PrivacyConsentAuditEntry",
    "PrivacyMode",
    "PrivacyPolicy",
    "PrivacyViolation",
    "ProcessingScope",
    "ProofGate",
    "Readback",
    "Refusal",
    "SessionClosedError",
    "SessionEpochConflictError",
    "SessionError",
    "SessionStatus",
    "StaleRevisionError",
    "StaleTurnError",
    "StudioAction",
    "StudioAuthorizationDecision",
    "StudioAuthorizationError",
    "StudioAuthorizerPort",
    "StudioRole",
    "TransitionError",
    "TurnState",
    "TypedCreativeAST",
    "VerifierReport",
    "VideoTimeScope",
    "VoiceTurn",
    "approval",
    "authority",
    "build_preview",
    "build_ibpc_service",
    "canonical",
    "canonical_approval_payload",
    "compile_ast",
    "compute_approval_hash",
    "create_default_key_provider",
    "compiler",
    "delta",
    "DurableExecutionOutboxStore",
    "evaluate_proof_gate",
    "ExecutionOutboxCoordinator",
    "ExecutionOutboxState",
    "execution_outbox",
    "footprint",
    "intent",
    "models",
    "parse_intent",
    "preview",
    "privacy",
    "proof",
    "redact_mapping",
    "render_readback",
    "service",
    "session",
    "shadow",
    "signature",
]
