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
    footprint,
    intent,
    live_session,
    models,
    preview,
    principal_authority,
    privacy,
    proof,
    service,
    session,
    shadow,
    signature,
)
from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalAlreadyConsumedError,
    ApprovalBindingMismatchError,
    ApprovalBindingPayload,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalLedger,
    CanonicalApprovalLedger,
    DurableApprovalLedger,
    DurableCanonicalApprovalLedger,
)
from nexus_ai_agent.nagar.ibpc.authority import (
    CanonicalAuthorityError,
    CanonicalAuthorityPort,
    DirectoryAuthority,
    PrincipalFacts,
)
from nexus_ai_agent.nagar.ibpc.compiler import CompilationRefused, compile_ast
from nexus_ai_agent.nagar.ibpc.composition import (
    build_durable_ibpc_service,
    build_ibpc_service,
    create_default_key_provider,
)
from nexus_ai_agent.nagar.ibpc.intent import (
    Clarification,
    IntentAST,
    Refusal,
    parse_intent,
)
from nexus_ai_agent.nagar.ibpc.live_session import (
    ConcurrentSessionMutationError,
    DurableLiveSessionStore,
    SessionStatus,
    StaleSessionRevisionError,
    TurnRecord,
    TurnStatus,
)
from nexus_ai_agent.nagar.ibpc.models import (
    ApprovalBinding,
    ApprovalState,
    CheckVerdict,
    CompiledPlan,
    IBPCCapsule,
    PreviewManifest,
    PrivacyMode,
    Readback,
    TypedCreativeAST,
    VerifierReport,
    VideoTimeScope,
)
from nexus_ai_agent.nagar.ibpc.preview import build_preview, render_readback
from nexus_ai_agent.nagar.ibpc.principal_authority import (
    DurableStudioAuthorizationService,
    IBPCAuthorizationGate,
    MembershipRole,
    StudioAction,
    StudioAuthorizationError,
)
from nexus_ai_agent.nagar.ibpc.privacy import (
    ConsentAction,
    ConsentScope,
    ConsentState,
    DurablePrivacyConsentStore,
    PrivacyConsentGrant,
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
    LiveSessionManager,
    StaleTurnError,
    TurnState,
    VoiceTurn,
)

__all__ = [
    "EXECUTED_UNVERIFIED",
    "EXECUTED_VERIFIED",
    "ApprovalAlreadyConsumedError",
    "ApprovalBinding",
    "ApprovalBindingMismatchError",
    "ApprovalBindingPayload",
    "ApprovalExpiredError",
    "ApprovalError",
    "ApprovalLedger",
    "ApprovalState",
    "CanonicalApprovalLedger",
    "CanonicalAuthorityError",
    "CanonicalAuthorityPort",
    "CheckVerdict",
    "Clarification",
    "ConcurrentSessionMutationError",
    "ConsentAction",
    "ConsentScope",
    "ConsentState",
    "CompilationRefused",
    "CompiledPlan",
    "DirectoryAuthority",
    "DurableApprovalLedger",
    "DurableCanonicalApprovalLedger",
    "DurableLiveSessionStore",
    "DurablePrivacyConsentStore",
    "DurableStudioAuthorizationService",
    "ExecutionOutcome",
    "IBPCAuthorizationGate",
    "IBPCCapsule",
    "IBPCService",
    "IBPCServiceDeps",
    "IntentAST",
    "LiveSessionManager",
    "PipelineResult",
    "PreviewManifest",
    "MembershipRole",
    "PrincipalFacts",
    "PrivacyConsentGrant",
    "PrivacyMode",
    "PrivacyPolicy",
    "PrivacyViolation",
    "ProofGate",
    "Readback",
    "Refusal",
    "SessionStatus",
    "StaleSessionRevisionError",
    "StaleTurnError",
    "StudioAction",
    "StudioAuthorizationError",
    "TurnRecord",
    "TurnState",
    "TurnStatus",
    "TypedCreativeAST",
    "VerifierReport",
    "VideoTimeScope",
    "VoiceTurn",
    "approval",
    "authority",
    "build_durable_ibpc_service",
    "build_ibpc_service",
    "build_preview",
    "canonical",
    "compile_ast",
    "create_default_key_provider",
    "compiler",
    "delta",
    "evaluate_proof_gate",
    "footprint",
    "intent",
    "live_session",
    "models",
    "parse_intent",
    "preview",
    "principal_authority",
    "privacy",
    "proof",
    "redact_mapping",
    "render_readback",
    "service",
    "session",
    "shadow",
    "signature",
]
