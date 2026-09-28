"""Memory package — W3 Masterpieces.

- long_term: legacy vector memory (IS_LEGACY=True, needs aclose)
- short_term: window + summarization
- trust: W3 Masterpiece 2 — provenance-aware trust store
- unified_trust: W3 Masterpiece 3 — unified trust plane integrating W1+W2
"""

from .trust import (  # noqa: F401
    MemoryPolicy,
    MemoryStatus,
    MemoryTrustRecord,
    MemoryTrustService,
    MemoryTrustStore,
    MemoryType,
    ScopeType,
    SourceType,
    TrustedScope,
    check_policy,
    delimit_untrusted_memory,
    is_authority_claim,
    resolve_trusted_scope,
)
from .unified_trust import AuthenticatedProducer, UnifiedTrustPlane  # noqa: F401

__all__ = [
    "AuthenticatedProducer",
    "MemoryPolicy",
    "MemoryStatus",
    "MemoryTrustRecord",
    "MemoryTrustService",
    "MemoryTrustStore",
    "MemoryType",
    "ScopeType",
    "SourceType",
    "TrustedScope",
    "UnifiedTrustPlane",
    "check_policy",
    "delimit_untrusted_memory",
    "is_authority_claim",
    "resolve_trusted_scope",
]
