"""``nexus.motion.graphics`` — Local motion graphics, video transitions, keyframe
animation, and kinetic titles (Wave 6 substrate).

This package provides the pure substrate for Nagar motion graphics:
* typed data models (:class:`AddTransitionInput`, :class:`TransformKeyframe`,
  :class:`KeyframeTransformInput`, :class:`AddGlowInput`, :class:`AddMotionBlurInput`,
  :class:`AddTitleInput`);
* pure capability operations: ``motion.add_transition``, ``motion.keyframe_transform``,
  ``motion.add_glow``, ``motion.add_motion_blur``, and ``motion.add_title``.

In accordance with Nagar substrate architecture, this package has zero heavy dependencies
(no PyTorch, WebGPU binary bindings, or external C-extensions).
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.motion.models import (
    DOMAIN,
    MOTION_PACKAGE_ID,
    OPERATION_ADD_GLOW,
    OPERATION_ADD_MOTION_BLUR,
    OPERATION_ADD_TITLE,
    OPERATION_ADD_TRANSITION,
    OPERATION_KEYFRAME_TRANSFORM,
    AddGlowInput,
    AddMotionBlurInput,
    AddTitleInput,
    AddTransitionInput,
    EasingKind,
    KeyframeTransformInput,
    TransformKeyframe,
    TransitionKind,
)
from nexus_ai_agent.creative.packs.motion.operations import (
    build_motion_registry,
    register_motion_operations,
)

__all__ = [
    "DOMAIN",
    "MOTION_PACKAGE_ID",
    "OPERATION_ADD_GLOW",
    "OPERATION_ADD_MOTION_BLUR",
    "OPERATION_ADD_PARALLAX",
    "OPERATION_ADD_TITLE",
    "OPERATION_ADD_TRANSITION",
    "OPERATION_KEYFRAME_TRANSFORM",
    "OPERATION_STABILIZE",
    "AddGlowInput",
    "AddMotionBlurInput",
    "AddParallaxInput",
    "AddTitleInput",
    "AddTransitionInput",
    "EasingKind",
    "KeyframeTransformInput",
    "StabilizeInput",
    "TransformKeyframe",
    "TransitionKind",
    "build_motion_registry",
    "register_motion_operations",
]
