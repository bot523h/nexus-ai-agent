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
    canonical,
    compiler,
    footprint,
    intent,
    models,
    preview,
    privacy,
    service,
    session,
)
from nexus_ai_agent.nagar.ibpc.approval import ApprovalError, ApprovalLedger
from nexus_ai_agent.nagar.ibpc.compiler import CompilationRefused, compile_ast
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
    IBPCCapsule,
    PreviewManifest,
    PrivacyMode,
    Readback,
    TypedCreativeAST,
    VerifierReport,
    VideoTimeScope,
)
from nexus_ai_agent.nagar.ibpc.preview import build_preview, render_readback
from nexus_ai_agent.nagar.ibpc.privacy import PrivacyPolicy, PrivacyViolation, redact_mapping
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
    "ApprovalBinding",
    "ApprovalError",
    "ApprovalLedger",
    "ApprovalState",
    "CheckVerdict",
    "Clarification",
    "CompilationRefused",
    "CompiledPlan",
    "ExecutionOutcome",
    "IBPCCapsule",
    "IBPCService",
    "IBPCServiceDeps",
    "IntentAST",
    "LiveSessionManager",
    "PipelineResult",
    "PreviewManifest",
    "PrivacyMode",
    "PrivacyPolicy",
    "PrivacyViolation",
    "Readback",
    "Refusal",
    "StaleTurnError",
    "TurnState",
    "TypedCreativeAST",
    "VerifierReport",
    "VideoTimeScope",
    "VoiceTurn",
    "approval",
    "build_preview",
    "canonical",
    "compile_ast",
    "compiler",
    "footprint",
    "intent",
    "models",
    "parse_intent",
    "preview",
    "privacy",
    "redact_mapping",
    "render_readback",
    "service",
    "session",
]
