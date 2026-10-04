"""Creative spine: canonical Intent, sealed Work compilation, queue-backed lineage."""

from __future__ import annotations

from nexus_ai_agent.creative.spine.authoring import AuthoringError, build_creative_work
from nexus_ai_agent.creative.spine.compiler import (
    CapabilityCompiler,
    CreativeWorkCompiler,
    GraphIntentPlanner,
    IntentPlanner,
    request_idempotency_key,
)
from nexus_ai_agent.creative.spine.intent import (
    IntentProposal,
    IntentResolverPort,
    LLMIntentResolver,
)
from nexus_ai_agent.creative.spine.lineage import CreativeWorkLineage, PreparedCreativeWork
from nexus_ai_agent.creative.spine.models import (
    CompilationError,
    CompiledIntent,
    CreativeGraph,
    GraphError,
    GraphNode,
    Intent,
    IntentError,
    IntentSourceRange,
    PlannedOperation,
    PlanProvenance,
    SpineError,
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
from nexus_ai_agent.creative.spine.strategy import (
    DeterministicTrimStrategy,
    SourceAssetDescriptor,
    StrategyError,
    StrategyProposal,
    StrategyProvider,
)

__all__ = [
    "AuthoringError",
    "CapabilityCompiler",
    "CompilationError",
    "CompiledIntent",
    "CreativeWorkLineage",
    "CreativeGraph",
    "CreativeRecipe",
    "CreativeWorkCompiler",
    "DeterministicTrimStrategy",
    "GraphError",
    "GraphIntentPlanner",
    "GraphNode",
    "Intent",
    "IntentError",
    "IntentPlanner",
    "IntentProposal",
    "IntentResolverPort",
    "IntentSourceRange",
    "LLMIntentResolver",
    "PlanProvenance",
    "PlannedOperation",
    "RecipeAnalyzer",
    "RecipeExtraction",
    "ReferenceAnalysis",
    "RulesRecipeAnalyzer",
    "SpineError",
    "PreparedCreativeWork",
    "SourceAssetDescriptor",
    "StrategyError",
    "StrategyProposal",
    "StrategyProvider",
    "abstract_recipe",
    "build_creative_work",
    "recipe_to_intent",
    "request_idempotency_key",
]
