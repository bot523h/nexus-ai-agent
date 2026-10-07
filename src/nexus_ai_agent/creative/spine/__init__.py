"""Nagar Creative Execution Spine -- the intent-first backbone (D-0025).

This package is the *execution* side of the direction recorded in
``docs/architecture/CREATIVE_DIRECTION.md`` and decision ``D-0025``. It turns a
user's stated **intent** into authorized, typed studio commands, runs them on
the existing single write path
(:class:`~nexus_ai_agent.creative.studio.bus.CommandBus`), and returns an
**artifact** whose lineage and evidence are recorded in a creative graph::

    Intent -> IntentResolver -> CreativeGraph
           -> CapabilityCompiler -> IntentPlanner
           -> CreativeExecutionSpine -> CommandBus (Policy/Authority)
           -> Execution -> Artifact -> Lineage/Evidence -> CreativeGraph

Constraints: stdlib + pydantic + ``creative.studio`` only (no shell/media/ML
imports), enforced by ``tests/architecture/test_spine_boundary.py``. The spine
never bypasses the bus, so Policy and Authority stay authoritative. It is
deterministic for the rules implementation shipped here; LLM-backed resolvers,
compilers and (media-capable) recipe analyzers are drop-ins behind the same
Protocols.
"""

from __future__ import annotations

from nexus_ai_agent.creative.spine.compiler import (
    CapabilityCompiler,
    GraphIntentPlanner,
    IntentPlanner,
    RulesCapabilityCompiler,
)
from nexus_ai_agent.creative.spine.execution import (
    CompiledIntent,
    CreativeExecutionSpine,
    SpineRun,
)
from nexus_ai_agent.creative.spine.models import (
    ArtifactRecord,
    CompilationError,
    CreativeGraph,
    EvidenceRecord,
    GraphError,
    GraphNode,
    Intent,
    IntentError,
    IntentResolver,
    PlannedOperation,
    RulesIntentResolver,
    SpineError,
    SpineRollbackError,
)
from nexus_ai_agent.creative.spine.reference import (
    CreativeRecipe,
    RecipeAnalyzer,
    RecipeExtraction,
    ReferenceAnalysis,
    RulesRecipeAnalyzer,
    abstract_recipe,
    recipe_to_intent,
)

__all__ = [
    "ArtifactRecord",
    "CapabilityCompiler",
    "CompilationError",
    "CompiledIntent",
    "CreativeExecutionSpine",
    "CreativeGraph",
    "CreativeRecipe",
    "EvidenceRecord",
    "GraphError",
    "GraphIntentPlanner",
    "GraphNode",
    "Intent",
    "IntentError",
    "IntentPlanner",
    "IntentResolver",
    "PlannedOperation",
    "RecipeAnalyzer",
    "RecipeExtraction",
    "ReferenceAnalysis",
    "RulesCapabilityCompiler",
    "RulesIntentResolver",
    "RulesRecipeAnalyzer",
    "SpineError",
    "SpineRollbackError",
    "SpineRun",
    "abstract_recipe",
    "recipe_to_intent",
]
