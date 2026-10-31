"""``nexus.vision.portrait`` — TDD §1.3 portrait pack (pure contract substrate)."""

from __future__ import annotations

from nexus_ai_agent.creative.packs.portrait.models import (
    DOMAIN,
    OPERATION_BACKGROUND_BLUR,
    OPERATION_CORRECT_GAZE,
    OPERATION_DETECT_LANDMARKS,
    OPERATION_ENHANCE_EYES,
    OPERATION_MASK_HAIR,
    OPERATION_RELIGHT_FACE,
    OPERATION_RETOUCH_BLEMISH,
    OPERATION_SMOOTH_SKIN,
    OPERATION_STABILIZE_FACE,
    OPERATION_WHITEN_TEETH,
    PORTRAIT_PACKAGE_ID,
)
from nexus_ai_agent.creative.packs.portrait.operations import (
    build_portrait_registry,
    register_portrait_operations,
)

__all__ = [
    "DOMAIN",
    "OPERATION_BACKGROUND_BLUR",
    "OPERATION_CORRECT_GAZE",
    "OPERATION_DETECT_LANDMARKS",
    "OPERATION_ENHANCE_EYES",
    "OPERATION_MASK_HAIR",
    "OPERATION_RELIGHT_FACE",
    "OPERATION_RETOUCH_BLEMISH",
    "OPERATION_SMOOTH_SKIN",
    "OPERATION_STABILIZE_FACE",
    "OPERATION_WHITEN_TEETH",
    "PORTRAIT_PACKAGE_ID",
    "build_portrait_registry",
    "register_portrait_operations",
]
