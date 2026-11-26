"""``nexus.analysis.perception`` -- measured media evidence for the studio.

Pure substrate (stdlib + pydantic): typed, content-bound evidence shapes
(:class:`SilenceEvidence`, :class:`SceneCutEvidence`, :class:`LoudnessEvidence`)
and three operations that *consume* them.  The engine that *produces* evidence
(free, offline FFmpeg) lives in :mod:`nexus_ai_agent.creative.perception`.
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.perception.models import (
    DOMAIN,
    OPERATION_ASSESS_LOUDNESS,
    OPERATION_MARK_SCENE_CUTS,
    OPERATION_MARK_SILENCES,
    PERCEPTION_PACKAGE_ID,
    AssessLoudnessInput,
    LoudnessEvidence,
    MarkSceneCutsInput,
    MarkSilencesInput,
    SceneCutEvidence,
    SilenceEvidence,
    SilenceSpan,
)
from nexus_ai_agent.creative.packs.perception.operations import (
    build_perception_registry,
    register_perception_operations,
)

__all__ = [
    "DOMAIN",
    "OPERATION_ASSESS_LOUDNESS",
    "OPERATION_MARK_SCENE_CUTS",
    "OPERATION_MARK_SILENCES",
    "PERCEPTION_PACKAGE_ID",
    "AssessLoudnessInput",
    "LoudnessEvidence",
    "MarkSceneCutsInput",
    "MarkSilencesInput",
    "SceneCutEvidence",
    "SilenceEvidence",
    "SilenceSpan",
    "build_perception_registry",
    "register_perception_operations",
]
