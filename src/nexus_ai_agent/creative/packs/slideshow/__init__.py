"""``nexus.slideshow.compose`` — the pack that turns stills into a beat-aligned master.

The package is deliberately *pure*: stdlib + pydantic only, no filesystem
probing, no FFmpeg, no hosted-model calls (the architecture gate in
``tests/architecture/test_pack_manifest_is_data_only.py`` enforces this).  The
host-side adapter that *does* touch the world lives in
``nexus_ai_agent.creative.slideshow`` and only feeds evidence into these typed
commands.

Surface:

* :mod:`nexus_ai_agent.creative.packs.slideshow.models` — the typed payloads;
* :mod:`nexus_ai_agent.creative.packs.slideshow.templates` — the tone library;
* :mod:`nexus_ai_agent.creative.packs.slideshow.planning` — the planning rules;
* :mod:`nexus_ai_agent.creative.packs.slideshow.operations` — the five pure
  operations and the Wave 2 registry builder.
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.slideshow.models import (
    RECOMMENDED_IMAGE_COUNT,
    SLIDESHOW_PACKAGE_ID,
    TARGET_DURATIONS_US,
    AssetEvidence,
    BeatGrid,
    ComposeInput,
    ImageScore,
    RenderInput,
    ShotPlan,
    ShotSelection,
    SlideshowAnalysis,
    SlideshowPlan,
)
from nexus_ai_agent.creative.packs.slideshow.operations import (
    LAYER_GRADE,
    LAYER_MOTION,
    LAYER_RENDER_PROFILE,
    LAYER_TEMPLATE,
    LAYER_TRANSITION,
    OPERATION_COMPOSE,
    OPERATION_RENDER,
    OPERATION_SCAN,
    OPERATION_SCORE,
    OPERATION_SUGGEST_TONE,
    ScanAssetsInput,
    ScoreImagesInput,
    SuggestToneInput,
    build_slideshow_registry,
    register_slideshow_operations,
    tone_library,
    use_tone_library,
)
from nexus_ai_agent.creative.packs.slideshow.planning import (
    MIN_SHOT_US,
    distribute_us,
    plan_slideshow,
    suggest_template_id,
)
from nexus_ai_agent.creative.packs.slideshow.templates import (
    TEMPLATES_PATH,
    TemplateError,
    ToneTemplate,
    ToneTemplateLibrary,
    load_tone_templates,
)

__all__ = [
    "AssetEvidence",
    "BeatGrid",
    "ComposeInput",
    "ImageScore",
    "LAYER_GRADE",
    "LAYER_MOTION",
    "LAYER_RENDER_PROFILE",
    "LAYER_TEMPLATE",
    "LAYER_TRANSITION",
    "MIN_SHOT_US",
    "OPERATION_COMPOSE",
    "OPERATION_RENDER",
    "OPERATION_SCAN",
    "OPERATION_SCORE",
    "OPERATION_SUGGEST_TONE",
    "RECOMMENDED_IMAGE_COUNT",
    "RenderInput",
    "SLIDESHOW_PACKAGE_ID",
    "ScanAssetsInput",
    "ScoreImagesInput",
    "ShotPlan",
    "ShotSelection",
    "SlideshowAnalysis",
    "SlideshowPlan",
    "SuggestToneInput",
    "TARGET_DURATIONS_US",
    "TEMPLATES_PATH",
    "TemplateError",
    "ToneTemplate",
    "ToneTemplateLibrary",
    "build_slideshow_registry",
    "distribute_us",
    "load_tone_templates",
    "plan_slideshow",
    "register_slideshow_operations",
    "suggest_template_id",
    "tone_library",
    "use_tone_library",
]
