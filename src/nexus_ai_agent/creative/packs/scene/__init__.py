"""``nexus.vision.scene`` — TDD §1.4 scene pack (pure contract substrate)."""

from __future__ import annotations

from nexus_ai_agent.creative.packs.scene.models import (
    DOMAIN,
    OPERATION_AUTO_REFRAME_SUBJECT,
    OPERATION_DETECT_SHOT_BOUNDARIES,
    OPERATION_FIND_SUBJECT_MOMENT,
    OPERATION_REMOVE_BACKGROUND,
    OPERATION_REMOVE_LOGO,
    OPERATION_REMOVE_OBJECT,
    OPERATION_REPLACE_SKY,
    OPERATION_SEGMENT_SUBJECT,
    OPERATION_TRACK_FACE,
    OPERATION_TRACK_OBJECT,
    SCENE_PACKAGE_ID,
)
from nexus_ai_agent.creative.packs.scene.operations import (
    build_scene_registry,
    register_scene_operations,
)

__all__ = [
    "DOMAIN",
    "OPERATION_AUTO_REFRAME_SUBJECT",
    "OPERATION_DETECT_SHOT_BOUNDARIES",
    "OPERATION_FIND_SUBJECT_MOMENT",
    "OPERATION_REMOVE_BACKGROUND",
    "OPERATION_REMOVE_LOGO",
    "OPERATION_REMOVE_OBJECT",
    "OPERATION_REPLACE_SKY",
    "OPERATION_SEGMENT_SUBJECT",
    "OPERATION_TRACK_FACE",
    "OPERATION_TRACK_OBJECT",
    "SCENE_PACKAGE_ID",
    "build_scene_registry",
    "register_scene_operations",
]
