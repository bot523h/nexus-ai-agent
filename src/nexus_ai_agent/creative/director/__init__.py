"""NAGAR AETHEL — the episodic director package.

AETHEL turns a Fountain screenplay into a *validated, executable* shot plan and
bridges it directly to the ATLAS ``episode_causality_lab`` engine:

* :mod:`fountain_parser` — a bounded, deterministic Fountain AST parser;
* :mod:`atlas_bridge` — compiles the AST into a versioned ATLAS
  ``StoryDocument`` so the causal/temporal/planner engines are reused, not
  re-implemented;
* :mod:`shot_planner` — the 180-degree axis, 30-degree, eyeline and Hall
  proxemics rules.

The package is a *proposal and analysis layer*: it renders nothing, owns no
project state, mints no passport, and dispatches no command.
"""

from __future__ import annotations

from .atlas_bridge import TICK_PER_SCENE, compile_fountain_to_story_document, slugify_identifier
from .contracts import (
    ActorPlacement,
    BlockKind,
    CameraSetup,
    DialogueBlock,
    EyelineResult,
    HallBand,
    ParsedBlock,
    ParsedScene,
    ParsedScreenplay,
    SceneHeading,
    ShotSequence,
    ShotSize,
    SourceLine,
    hall_band,
)
from .errors import (
    AxisViolationError,
    DirectorError,
    EyelineMismatchError,
    GeometryError,
    InputBoundError,
    JumpCutError,
    ProxemicsError,
    ScreenplayParseError,
)
from .fountain_parser import ESTIMATED_SECONDS_PER_SYLLABLE, estimate_syllables, parse_fountain
from .shot_planner import (
    DEFAULT_FOCAL_MM,
    MIN_ANGLE_DELTA_DEG,
    MIN_FOCAL_RATIO,
    MIN_SEPARATION_M,
    ShotPlanRequest,
    camera_angle_deg,
    check_eyeline_match,
    check_jump_cut,
    check_proxemics,
    plan_shot_sequence,
    side_of_line,
)

__all__ = [
    "TICK_PER_SCENE",
    "ESTIMATED_SECONDS_PER_SYLLABLE",
    "DEFAULT_FOCAL_MM",
    "MIN_ANGLE_DELTA_DEG",
    "MIN_FOCAL_RATIO",
    "MIN_SEPARATION_M",
    "ActorPlacement",
    "BlockKind",
    "CameraSetup",
    "DialogueBlock",
    "EyelineResult",
    "HallBand",
    "ParsedBlock",
    "ParsedScene",
    "ParsedScreenplay",
    "SceneHeading",
    "ShotSequence",
    "ShotSize",
    "SourceLine",
    "ShotPlanRequest",
    "AxisViolationError",
    "DirectorError",
    "EyelineMismatchError",
    "GeometryError",
    "InputBoundError",
    "JumpCutError",
    "ProxemicsError",
    "ScreenplayParseError",
    "camera_angle_deg",
    "check_eyeline_match",
    "check_jump_cut",
    "check_proxemics",
    "compile_fountain_to_story_document",
    "estimate_syllables",
    "hall_band",
    "parse_fountain",
    "plan_shot_sequence",
    "side_of_line",
    "slugify_identifier",
]
