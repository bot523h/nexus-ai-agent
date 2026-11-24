"""Character capsule: stable identity, versioned modules, continuity verdicts.

Re-engineered from old PR #326's core contracts.  Pure and framework-free.
"""

from __future__ import annotations

from nexus_ai_agent.creative.character.continuity import (
    ContinuityReport,
    ContinuityThreshold,
    DimensionResult,
    VerificationState,
    verify_dimensions,
)
from nexus_ai_agent.creative.character.model import (
    CHARACTER_ID_RE,
    MODULE_KINDS,
    CharacterCapsule,
    FaceModule,
    Scope,
    ScopeViolation,
    ValidationError,
    VoiceModule,
    WardrobeModule,
    next_character_id,
    validate_character_id,
)

__all__ = [
    "CHARACTER_ID_RE",
    "MODULE_KINDS",
    "FaceModule",
    "Scope",
    "ScopeViolation",
    "ValidationError",
    "CharacterCapsule",
    "VoiceModule",
    "WardrobeModule",
    "next_character_id",
    "validate_character_id",
    "ContinuityReport",
    "ContinuityThreshold",
    "DimensionResult",
    "VerificationState",
    "verify_dimensions",
]
