"""Product-facing adapters for the Nagar Studio experience.

This package is deliberately outside the creative core: it presents facts from
existing contracts and never dispatches commands or invents lifecycle state.
"""

from nexus_ai_agent.product.studio_experience import (
    ArtifactPassport,
    ExecutionView,
    IntentDraft,
    LineageView,
    PlanPreview,
    SceneView,
    present_artifact,
    present_execution,
    present_plan,
)

__all__ = [
    "ArtifactPassport",
    "ExecutionView",
    "IntentDraft",
    "LineageView",
    "PlanPreview",
    "SceneView",
    "present_artifact",
    "present_execution",
    "present_plan",
]
