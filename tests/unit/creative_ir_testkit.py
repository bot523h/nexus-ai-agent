"""Shared builder for Creative IR test documents.

One code path builds the documents that all three IR test modules assert on, so
a change to what "a realistic creative document" means cannot silently desync
the determinism suite from the validation suite. Same flat-helper convention as
``tests/unit/ed25519_testkit.py`` and ``tests/unit/surface_fakes.py``.

:func:`product_teaser` is not a toy: it is a 15-second social teaser with a hook,
a subject shot, a title, a music bed, a voiceover and a call to action, carrying
five constraints of five different kinds. It is deliberately the shape a real
strategy layer would emit, so the IR is exercised against something that could
actually be rendered.
"""

from __future__ import annotations

from typing import Literal

from nexus_ai_agent.creative.intelligence import (
    Asset,
    AssetKind,
    AudioIntent,
    Constraint,
    ConstraintTarget,
    CreativeBrief,
    CreativeWork,
    Effect,
    EffectFamily,
    EffectParam,
    EmphasisConstraint,
    ExclusionConstraint,
    Layer,
    MediaContent,
    NarrativeRole,
    OrderConstraint,
    Origin,
    OutputRequirement,
    Priority,
    QualityConstraint,
    Scene,
    Segment,
    SemanticRole,
    StyleIntent,
    TextContent,
    TextSpec,
    Timing,
    TimingConstraint,
    Transition,
    TransitionKind,
    TypographicStyle,
)

ONE_SECOND_US = 1_000_000

#: Mirrors ``TypographicStyle.hierarchy``; kept as an alias so the builder stays
#: fully typed without importing a ``Literal`` it would otherwise repeat.
Hierarchy = Literal["display", "title", "subtitle", "body", "caption"]


def _span(start_s: float, duration_s: float) -> Timing:
    """A timing span written in seconds, stored in integer microseconds."""
    return Timing(
        start_us=round(start_s * ONE_SECOND_US),
        duration_us=round(duration_s * ONE_SECOND_US),
    )


def _media_layer(
    layer_id: str,
    role: SemanticRole,
    asset_ref: str,
    start_s: float,
    duration_s: float,
    *,
    emphasis: int = 500,
    effects: tuple[str, ...] = (),
    style: StyleIntent | None = None,
    audio: AudioIntent | None = None,
    source_in_s: float = 0.0,
    detail: str = "",
) -> Layer:
    span = _span(start_s, duration_s)
    return Layer(
        layer_id=layer_id,  # provisional; seal() replaces it with the content address
        role=role,
        content=MediaContent(
            asset_ref=asset_ref,
            segment=Segment(
                source_in_us=round(source_in_s * ONE_SECOND_US),
                source_duration_us=span.duration_us,
                timeline=span,
            ),
            audio=audio,
        ),
        style=style or StyleIntent(),
        emphasis=emphasis,
        effects=effects,
        origin=Origin(source="strategy", detail=detail or f"{role.value} layer"),
    )


def _text_layer(
    layer_id: str,
    role: SemanticRole,
    text: str,
    start_s: float,
    duration_s: float,
    *,
    emphasis: int = 500,
    hierarchy: Hierarchy = "title",
    rtl: bool = False,
    font_asset_ref: str | None = None,
) -> Layer:
    span = _span(start_s, duration_s)
    return Layer(
        layer_id=layer_id,
        role=role,
        content=TextContent(
            text=TextSpec(
                text=text,
                typography=TypographicStyle(hierarchy=hierarchy, rtl=rtl),
                font_asset_ref=font_asset_ref,
            ),
            timeline=span,
        ),
        emphasis=emphasis,
        origin=Origin(source="strategy", detail=f"{role.value} typography"),
    )


def product_teaser(*, sealed: bool = True) -> CreativeWork:
    """A 15-second premium product teaser, valid and fully constrained.

    Timeline::

        0s ───── 3s ───── 8s ───── 12s ───── 15s
        │  HOOK  │ DEVELOP │ CLIMAX │  CTA   │
        music bed runs the whole piece, ducked under the voiceover
    """
    assets = (
        Asset(
            asset_id="hero",
            kind=AssetKind.VIDEO,
            uri="asset:hero-shot",
            role=SemanticRole.SUBJECT,
            duration_us=20 * ONE_SECOND_US,
            width_px=3840,
            height_px=2160,
            frame_rate_milli=24000,
            origin=Origin(source="user", detail="uploaded hero shot"),
        ),
        Asset(
            asset_id="broll",
            kind=AssetKind.VIDEO,
            uri="asset:broll-city",
            role=SemanticRole.BROLL,
            duration_us=12 * ONE_SECOND_US,
            width_px=1920,
            height_px=1080,
            frame_rate_milli=24000,
            origin=Origin(source="user", detail="uploaded b-roll"),
        ),
        Asset(
            asset_id="bed",
            kind=AssetKind.AUDIO,
            uri="asset:music-bed",
            role=SemanticRole.MUSIC,
            duration_us=30 * ONE_SECOND_US,
            sample_rate_hz=48000,
            channels=2,
            origin=Origin(source="strategy", detail="picked for a premium feel"),
        ),
        Asset(
            asset_id="vo",
            kind=AssetKind.AUDIO,
            uri="asset:voiceover-fa",
            role=SemanticRole.VOICEOVER,
            duration_us=9 * ONE_SECOND_US,
            sample_rate_hz=48000,
            channels=1,
            origin=Origin(source="user", detail="recorded Persian voiceover"),
        ),
        Asset(
            asset_id="vazirmatn",
            kind=AssetKind.FONT,
            uri="asset:font-vazirmatn",
            role=SemanticRole.TITLE,
            origin=Origin(source="system", detail="bundled Persian UI font"),
        ),
    )

    effects = (
        Effect(
            effect_id="glow",
            family=EffectFamily.MOTION,
            operation="motion.add_glow",
            intensity=400,
            params=(EffectParam(name="radius_milli", value_milli=1500),),
            origin=Origin(source="strategy", detail="premium highlight on the reveal"),
        ),
        Effect(
            effect_id="grade",
            family=EffectFamily.COLOR,
            operation="color.adjust_exposure",
            intensity=1000,
            params=(
                EffectParam(name="exposure_milli", value_milli=300),
                EffectParam(name="temperature_k", value_milli=5600000),
            ),
            origin=Origin(source="strategy", detail="warm, slightly lifted"),
        ),
    )

    scenes = (
        Scene(
            scene_id="hook",
            label="cold open on the product",
            narrative_role=NarrativeRole.HOOK,
            timing=_span(0, 3),
            layers=(
                _media_layer(
                    "hook-subject",
                    SemanticRole.SUBJECT,
                    "hero",
                    0,
                    3,
                    emphasis=900,
                    effects=("grade",),
                    style=StyleIntent(energy=850, warmth=650, motion=800),
                    detail="product fills the frame in the first second",
                ),
                _text_layer(
                    "hook-title",
                    SemanticRole.TITLE,
                    "نکسوس",
                    0.4,
                    2.2,
                    emphasis=700,
                    hierarchy="display",
                    rtl=True,
                    font_asset_ref="vazirmatn",
                ),
                _media_layer(
                    "hook-music",
                    SemanticRole.MUSIC,
                    "bed",
                    0,
                    3,
                    emphasis=300,
                    audio=AudioIntent(duck_under_voiceover=False),
                ),
            ),
            origin=Origin(source="strategy", detail="a hook that resolves inside three seconds"),
        ),
        Scene(
            scene_id="develop",
            label="what it does",
            narrative_role=NarrativeRole.DEVELOPMENT,
            timing=_span(3, 5),
            layers=(
                _media_layer(
                    "develop-broll",
                    SemanticRole.BROLL,
                    "broll",
                    3,
                    5,
                    emphasis=500,
                    source_in_s=1.0,
                    style=StyleIntent(energy=600, density=700),
                ),
                _media_layer(
                    "develop-vo",
                    SemanticRole.VOICEOVER,
                    "vo",
                    3,
                    5,
                    emphasis=600,
                    audio=AudioIntent(loudness_target_lufs_milli=-16000),
                ),
                _media_layer(
                    "develop-music",
                    SemanticRole.MUSIC,
                    "bed",
                    3,
                    5,
                    emphasis=200,
                    source_in_s=3.0,
                    audio=AudioIntent(duck_under_voiceover=True),
                ),
                _text_layer(
                    "develop-caption",
                    SemanticRole.CAPTION,
                    "ساخته شده برای سرعت",
                    3.5,
                    4.0,
                    hierarchy="caption",
                    rtl=True,
                    font_asset_ref="vazirmatn",
                ),
            ),
            origin=Origin(source="strategy", detail="voiceover carries the argument"),
        ),
        Scene(
            scene_id="climax",
            label="dramatic reveal",
            narrative_role=NarrativeRole.CLIMAX,
            timing=_span(8, 4),
            layers=(
                _media_layer(
                    "climax-subject",
                    SemanticRole.SUBJECT,
                    "hero",
                    8,
                    4,
                    emphasis=1000,
                    effects=("glow", "grade"),
                    source_in_s=6.0,
                    style=StyleIntent(energy=950, warmth=700, motion=900),
                ),
                _media_layer(
                    "climax-music",
                    SemanticRole.MUSIC,
                    "bed",
                    8,
                    4,
                    emphasis=400,
                    source_in_s=8.0,
                ),
            ),
            origin=Origin(source="user", detail="«یک لحظه دراماتیک برای رونمایی»"),
        ),
        Scene(
            scene_id="cta",
            label="logo and call to action",
            narrative_role=NarrativeRole.CALL_TO_ACTION,
            timing=_span(12, 3),
            layers=(
                _text_layer(
                    "cta-logo", SemanticRole.LOGO, "NEXUS", 12, 1.5, emphasis=400, hierarchy="title"
                ),
                _text_layer(
                    "cta-ask",
                    SemanticRole.CALL_TO_ACTION,
                    "همین حالا ببینید",
                    13.2,
                    1.8,
                    emphasis=800,
                    hierarchy="title",
                    rtl=True,
                    font_asset_ref="vazirmatn",
                ),
                _media_layer(
                    "cta-music", SemanticRole.MUSIC, "bed", 12, 3, emphasis=200, source_in_s=12.0
                ),
            ),
            origin=Origin(source="strategy", detail="close on the ask"),
        ),
    )

    transitions = (
        Transition(
            transition_id="t-hook-develop",
            kind=TransitionKind.CUT,
            from_scene_ref="hook",
            to_scene_ref="develop",
        ),
        Transition(
            transition_id="t-develop-climax",
            kind=TransitionKind.DIP_TO_BLACK,
            duration_us=400_000,
            from_scene_ref="develop",
            to_scene_ref="climax",
            origin=Origin(source="user", detail="«یک مکث سیاه قبل از رونمایی»"),
        ),
        Transition(
            transition_id="t-climax-cta",
            kind=TransitionKind.CROSSFADE,
            duration_us=300_000,
            from_scene_ref="climax",
            to_scene_ref="cta",
        ),
    )

    constraints = (
        Constraint(
            constraint_id="c-duration",
            priority=Priority.HARD,
            spec=TimingConstraint(
                target=ConstraintTarget(kind="work"),
                min_us=10 * ONE_SECOND_US,
                max_us=15 * ONE_SECOND_US,
            ),
            origin=Origin(source="user", detail="«زیر پانزده ثانیه»"),
        ),
        Constraint(
            constraint_id="c-hook",
            priority=Priority.HARD,
            spec=TimingConstraint(
                target=ConstraintTarget(kind="scene", ref="hook"),
                max_us=3 * ONE_SECOND_US,
            ),
            origin=Origin(source="strategy", detail="social retention: resolve the hook fast"),
        ),
        Constraint(
            constraint_id="c-order",
            priority=Priority.HARD,
            spec=OrderConstraint(
                before=ConstraintTarget(kind="scene", ref="hook"),
                after=ConstraintTarget(kind="scene", ref="cta"),
            ),
            origin=Origin(source="strategy", detail="the ask comes after the reveal"),
        ),
        Constraint(
            constraint_id="c-no-particles",
            priority=Priority.SOFT,
            spec=ExclusionConstraint(
                target=ConstraintTarget(kind="work"),
                forbidden=("motion.add_particles", "motion.warp"),
            ),
            origin=Origin(source="user", detail="«بدون ذرات و وارپ»"),
        ),
        Constraint(
            constraint_id="c-subject-focus",
            priority=Priority.HARD,
            spec=EmphasisConstraint(
                target=ConstraintTarget(kind="role", role=SemanticRole.SUBJECT),
                min_share_permille=250,
            ),
            origin=Origin(source="user", detail="«تمرکز روی خود محصول»"),
        ),
        Constraint(
            constraint_id="c-quality",
            priority=Priority.HARD,
            spec=QualityConstraint(
                min_width_px=1920,
                min_height_px=1080,
                min_frame_rate_milli=24000,
            ),
            origin=Origin(source="user", detail="«حداقل فول‌اچ‌دی»"),
        ),
    )

    work = CreativeWork(
        brief=CreativeBrief(
            goal="a premium 15-second teaser for the Nexus launch",
            audience="existing subscribers on social",
            semantic_intents=("cinematic", "premium", "fast paced", "dramatic reveal"),
            origin=Origin(source="user", detail="original request, Persian"),
        ),
        assets=assets,
        effects=effects,
        scenes=scenes,
        transitions=transitions,
        constraints=constraints,
        output=OutputRequirement(
            container="mp4",
            width_px=1920,
            height_px=1080,
            frame_rate_milli=24000,
            max_duration_us=15 * ONE_SECOND_US,
            loudness_target_lufs_milli=-14000,
        ),
    )

    # Every identity above is provisional ("hero", "hook", "c-duration", ...).
    # ``seal_work`` rewrites them bottom-up into content addresses and remaps
    # every reference -- including the scene refs inside the order constraint --
    # so the fixture needs no second pass. ``sealed=False`` therefore returns a
    # *genuinely* unsealed document: structurally sound, identity-pending.
    return work.seal() if sealed else work
