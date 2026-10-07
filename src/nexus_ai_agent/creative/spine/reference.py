"""Reference -> Creative Recipe -> Intent (the first killer capability).

Per D-0025 this is **not** the backbone: it is the first capability that rides
the spine. A reference video is analysed into a :class:`CreativeRecipe` (an
abstract *strategy*, never a copy), and a recipe becomes an :class:`Intent` that
the spine compiles and executes like any other intent.

Honest scope: the analysis ports are declared here, but the only implementation
shipped is :class:`RulesRecipeAnalyzer`, which is **deterministic and heuristic**
-- it derives a recipe from supplied structural hints, not from pixels. Real
perceptual analysis (composition, colour, pacing, motion) requires media tooling
that is out of this package's boundary; that implementation plugs in behind
:class:`RecipeAnalyzer` and is a recorded gap, not a claim.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.spine.models import Intent

#: Analysis dimensions a recipe can carry (matches the direction document).
Dimension = Literal[
    "composition",
    "timing",
    "layers",
    "transitions",
    "text_placement",
    "audio_relationship",
    "visual_style",
    "color_intent",
    "motion_intent",
    "editorial_structure",
]


class ReferenceAnalysis(BaseModel):
    """Structured facts extracted from a reference (hints or real measurement)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1, max_length=2000)
    hints: dict[str, Any] = Field(default_factory=dict)
    measured: bool = False


class CreativeRecipe(BaseModel):
    """A strategy abstracted from a reference -- the semantic decomposition.

    A recipe is deliberately *not* a copy: each entry is a dimension and an
    abstracted strategy phrase, so the same recipe can be re-applied to any
    project's own assets. ``recipe_hash`` is content-addressed for evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe_id: str = Field(default_factory=lambda: "recipe_" + hashlib.sha256(b"").hexdigest()[:12])
    source: str = Field(min_length=1, max_length=2000)
    dimensions: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    recipe_hash: str = ""

    def model_post_init(self, _context: Any) -> None:  # noqa: D105 - pydantic hook
        payload = {
            "source": self.source,
            "dimensions": self.dimensions,
            "confidence": self.confidence,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        object.__setattr__(self, "recipe_hash", f"sha256:{digest}")


class RecipeExtraction(BaseModel):
    """A recipe bound to the intent it becomes -- the hand-off into the spine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe: CreativeRecipe
    intent: Intent


@runtime_checkable
class RecipeAnalyzer(Protocol):
    """Port: a reference -> a :class:`ReferenceAnalysis`."""

    def analyze(self, reference: str) -> ReferenceAnalysis: ...


class RulesRecipeAnalyzer:
    """Deterministic analyzer over supplied structural hints.

    ``analyze`` reads only ``hints`` (e.g. ``{"pacing": "fast", "contrast":
    "high"}``); it performs **no** perceptual analysis. This is intentional: it
    gives the spine a real, testable reference->recipe path today, while making
    clear that the "understand the video" half is still a port awaiting a
    media-capable implementation.
    """

    #: hint value -> abstracted strategy per dimension.
    _ABSTRACTIONS: dict[str, dict[str, str]] = {
        "pacing": {
            "fast": "fast hook strategy: front-load the payoff",
            "slow": "slow-build strategy: earn attention gradually",
        },
        "contrast": {
            "high": "high-contrast grade intent",
            "low": "low-contrast, muted grade intent",
        },
        "captions": {
            "rhythmic": "caption timing strategy: beat-synced captions",
            "minimal": "caption timing strategy: sparse captions",
        },
        "hook": {
            "immediate": "cold-open hook strategy",
        },
    }

    def analyze(self, reference: str) -> ReferenceAnalysis:
        return ReferenceAnalysis(source=reference, hints={}, measured=False)

    def analyze_hints(self, reference: str, hints: dict[str, Any]) -> ReferenceAnalysis:
        return ReferenceAnalysis(source=reference, hints=dict(hints), measured=False)


def abstract_recipe(analysis: ReferenceAnalysis) -> CreativeRecipe:
    """Turn a reference analysis into a strategy recipe (never a copy)."""
    dimensions: dict[str, str] = {}
    for key, value in analysis.hints.items():
        table = RulesRecipeAnalyzer._ABSTRACTIONS.get(key)
        if table and str(value) in table:
            dimensions[key] = table[str(value)]
        else:
            dimensions[key] = f"reference strategy for {key}: {value}"
    confidence = 0.6 if analysis.measured else 0.3
    return CreativeRecipe(
        source=analysis.source,
        dimensions=dimensions,
        confidence=confidence,
    )


def recipe_to_intent(recipe: CreativeRecipe, *, project_id: str) -> Intent:
    """Render a recipe as an :class:`Intent` the spine can compile and execute.

    The recipe stays a *strategy*: it becomes an intent goal that names the
    abstracted dimensions. It does not carry pixel-level parameters, so applying
    it can never blindly copy the reference.
    """
    goal_parts = [recipe.dimensions[key] for key in sorted(recipe.dimensions)]
    if goal_parts:
        goal = "apply reference recipe: " + "; ".join(goal_parts)
    else:
        goal = "apply reference recipe"
    return Intent(
        project_id=project_id,
        goal=goal,
        constraints=("do not copy the reference; re-apply the strategy to own assets",),
        source="reference",
    )
