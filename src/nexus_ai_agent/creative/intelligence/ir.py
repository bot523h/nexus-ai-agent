"""Typed Creative IR -- the single representation the Creative Intelligence Plane speaks.

Where this sits
---------------
``INTENT -> UNDERSTANDING -> STRATEGY -> TYPED CREATIVE IR -> COMPILATION ->
EXECUTABLE PLAN -> COMMAND BUS -> EXECUTION``

This module is the *Typed Creative IR* box, and it is the only box in that chain
that everything else touches: strategy produces it, the compiler consumes it,
semantic revision edits it, semantic diff compares it. It is therefore pure
data plus total functions over that data. It contains no I/O, no process
spawning, no CommandBus, no transaction and no storage access -- the plane
produces plans, it never executes them, and the IR must not know that execution
exists. Enforced by
``tests/architecture/test_creative_intelligence_boundary.py``.

What it models, and why each concept earns its place
----------------------------------------------------
Every type here is justified by a capability operation the repository already
exposes (``creative/packs/*/pack.manifest.json``, 52 operations):

========================  ====================================================
IR concept                Consumed today by
========================  ====================================================
``Asset``                 ``slideshow.scan_assets``, ``AssetRecord``
``Scene``                 ``slideshow.compose`` / ``ShotPlan``
``Segment`` / ``Timing``  ``TimeRangeUS``, ``timeline.trim``
``Transition``            ``motion.add_transition``
``Effect``                ``motion.add_glow``, ``color.apply_lut``, ...
``AudioIntent``           the 10 ``audio.*`` operations
``TextSpec``              ``motion.add_title``, the 10 ``caption.*`` operations
``OutputRequirement``     ``delivery.render_master_4k``, ``make_proxy_480p``
========================  ====================================================

Two of these have no precedent in the repository and are the actual reason this
slice exists:

* ``SemanticRole`` -- *why* an element is in the piece (subject, hook, cta,
  music, voiceover). Without it "make it more emotional while keeping the
  rhythm" has no addressable target, and a revision can only rebuild blindly.
* ``Constraint`` -- a user requirement that survives compilation and can be
  *checked* afterwards. Every constraint kind here is checkable against the IR
  itself (:meth:`CreativeWork.check_constraints`), so constraints are load
  bearing on day one rather than decorative metadata.

What it deliberately does not model
-----------------------------------
**Tracks are not authoring data.** ``Track`` exists in this IR but only as
*layout*: an authored document carries ``layout=()`` and its layers carry a
role plus a timing; the normaliser decides which track each layer lands on, and
a laid-out document must then be complete, single-assigned and non-overlapping
(:class:`LayoutError`). Track allocation is an implementation concern of
whatever renders the piece; letting a strategy author tracks would bake an NLE's
opinions into the creative intent and freeze every future backend to them.

**No filtergraph, no argv, no codec.** ``Effect.operation`` names a capability
operation (``motion.add_glow``); how that becomes an FFmpeg filter belongs to
``creative/rendering/``, downstream of the compiler, and is none of this IR's
business.

**No floats.** Time is integer microseconds; normalised quantities are integer
per-mille (0..1000); signed continuous parameters are integer milli-units
(``exposure`` +1.5 EV is ``1500``). Every value in the IR therefore hashes
exactly -- see :mod:`nexus_ai_agent.creative.intelligence.identity`.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.intelligence.errors import (
    ConstraintViolationError,
    DanglingReferenceError,
    IdentityError,
    IRValidationError,
    SerializationError,
    TimingError,
)
from nexus_ai_agent.creative.intelligence.identity import (
    IR_VERSION,
    MICROSECONDS_PER_SECOND,
    PREFIX_ASSET,
    PREFIX_CONSTRAINT,
    PREFIX_EFFECT,
    PREFIX_LAYER,
    PREFIX_SCENE,
    PREFIX_TRACK,
    PREFIX_TRANSITION,
    PREFIX_WORK,
    IRVersion,
    canonical_json,
    content_id,
    sealed_id,
)

__all__ = [
    "IR_VERSION",
    "MICROSECONDS_PER_SECOND",
    "Asset",
    "AssetKind",
    "AudioIntent",
    "Constraint",
    "ConstraintKind",
    "ConstraintSpec",
    "ConstraintTarget",
    "CreativeBrief",
    "CreativeWork",
    "Effect",
    "EffectFamily",
    "EffectParam",
    "EmphasisConstraint",
    "ExclusionConstraint",
    "Layer",
    "LayerContent",
    "MediaContent",
    "NarrativeRole",
    "OrderConstraint",
    "Origin",
    "PacingConstraint",
    "OutputRequirement",
    "Priority",
    "QualityConstraint",
    "Scene",
    "Segment",
    "SemanticRole",
    "StyleIntent",
    "TextContent",
    "TextSpec",
    "Timing",
    "TimingConstraint",
    "Track",
    "Transition",
    "TransitionKind",
    "TypographicStyle",
    "Violation",
    "seal_work",
]


# ── Closed vocabularies ──────────────────────────────────────────────────────
#
# These are ``str, Enum`` (not ``StrEnum``, which is 3.11+) because the package
# targets Python 3.10 and because a closed set is a *version contract*: adding a
# member is an IR change, not a local edit, and bumping ``IR_VERSION`` is how
# that change is announced.


class SemanticRole(str, Enum):
    """Why an element is in the piece -- the addressing scheme of the plane.

    This is the concept that makes semantic revision possible: "make it more
    emotional but keep the rhythm" becomes "raise emphasis on ``SUBJECT`` and
    ``MUSIC``, hold every ``Timing``", which is an edit rather than a rebuild.
    """

    SUBJECT = "subject"
    CONTEXT = "context"
    BROLL = "broll"
    BACKGROUND = "background"
    HOOK = "hook"
    TITLE = "title"
    SUBTITLE = "subtitle"
    CAPTION = "caption"
    LOGO = "logo"
    CALL_TO_ACTION = "cta"
    VOICEOVER = "voiceover"
    MUSIC = "music"
    SFX = "sfx"
    AMBIENCE = "ambience"


class NarrativeRole(str, Enum):
    """The job a scene does in the story, independent of what it shows."""

    HOOK = "hook"
    SETUP = "setup"
    DEVELOPMENT = "development"
    CLIMAX = "climax"
    RESOLUTION = "resolution"
    CALL_TO_ACTION = "cta"


class AssetKind(str, Enum):
    """Media family of a source asset."""

    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    FONT = "font"
    LUT = "lut"


class EffectFamily(str, Enum):
    """Coarse class of an effect, used by exclusion constraints."""

    COLOR = "color"
    MOTION = "motion"
    AUDIO = "audio"
    TYPOGRAPHY = "typography"


class TransitionKind(str, Enum):
    """Transition grammar between two scenes."""

    CUT = "cut"
    CROSSFADE = "crossfade"
    WIPE = "wipe"
    DIP_TO_BLACK = "dip_to_black"
    MATCH_CUT = "match_cut"
    JUMP_CUT = "jump_cut"


class ConstraintKind(str, Enum):
    """The requirement shapes the IR can carry and check."""

    TIMING = "timing"
    PACING = "pacing"
    ORDER = "order"
    EXCLUSION = "exclusion"
    EMPHASIS = "emphasis"
    QUALITY = "quality"


class Priority(str, Enum):
    """Hard constraints block compilation; soft ones are reported."""

    HARD = "hard"
    SOFT = "soft"


#: A normalised 0..1 quantity carried as integer per-mille. No floats, ever.
Permille = Annotated[int, Field(ge=0, le=1000)]


# ── Provenance ───────────────────────────────────────────────────────────────


class Origin(BaseModel):
    """Where an element came from, kept as data rather than as a comment.

    Explainability is a structural property of the IR: any element can answer
    "why are you here", which is what lets a later slice show a user that a
    given scene exists because *they* asked for a dramatic reveal, or because a
    reference recipe implied one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["user", "reference", "strategy", "recipe", "system"] = "system"
    detail: str = Field(default="", max_length=500)
    reference_id: str | None = Field(default=None, max_length=256)

    def semantic_payload(self) -> dict[str, Any]:
        return {"source": self.source, "detail": self.detail, "reference_id": self.reference_id}


# ── Time ─────────────────────────────────────────────────────────────────────


class Timing(BaseModel):
    """A span on the piece timeline, in integer microseconds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_us: int = Field(ge=0)
    duration_us: int = Field(gt=0)

    @property
    def end_us(self) -> int:
        return self.start_us + self.duration_us

    def contains(self, other: Timing) -> bool:
        """True when ``other`` lies entirely inside this span."""
        return self.start_us <= other.start_us and other.end_us <= self.end_us

    def overlaps(self, other: Timing) -> bool:
        """True when the two spans share at least one microsecond."""
        return self.start_us < other.end_us and other.start_us < self.end_us

    def semantic_payload(self) -> dict[str, Any]:
        return {"start_us": self.start_us, "duration_us": self.duration_us}


class Segment(BaseModel):
    """Which part of a source is used, and where it lands on the timeline.

    The two-field shape is the same distinction OTIO makes between
    ``source_range`` and ``range_in_parent``; collapsing them would make a trim
    indistinguishable from a move, and a semantic revision could not tell
    "shorten this shot" from "start it later".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_in_us: int = Field(ge=0)
    source_duration_us: int = Field(gt=0)
    timeline: Timing
    #: Playback rate in integer milli-units: 1000 is normal speed, 2000 is 2x.
    speed_milli: int = Field(default=1000, ge=100, le=16000)

    @model_validator(mode="after")
    def _reject_retime_without_saying_so(self) -> Segment:
        """Source and timeline duration must match unless the layer retimes.

        A segment whose timeline span differs from its source span is a speed
        change. That is legal, but it must be *declared* by ``speed_milli`` so
        the IR never implies a rate the compiler would have to guess.
        """
        if self.speed_milli == 1000 and self.source_duration_us != self.timeline.duration_us:
            raise TimingError(
                "segment retimes without declaring it: "
                f"source {self.source_duration_us}us over timeline "
                f"{self.timeline.duration_us}us needs an explicit speed_milli"
            )
        return self

    @property
    def source_out_us(self) -> int:
        return self.source_in_us + self.source_duration_us

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "source_in_us": self.source_in_us,
            "source_duration_us": self.source_duration_us,
            "speed_milli": self.speed_milli,
            "timeline": self.timeline.semantic_payload(),
        }


# ── Style and text ───────────────────────────────────────────────────────────


class StyleIntent(BaseModel):
    """Style as axes, not as filter parameters.

    ``energy``, ``warmth``, ``density`` and ``motion`` are the vocabulary a
    strategy layer reasons in ("fast paced", "premium", "minimal"). They are
    deliberately *not* FFmpeg inputs: the compiler's job is to decide what a
    high-energy warm look means for a given backend. Keeping them abstract here
    is what lets the same IR target two renderers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    energy: Permille = 500
    warmth: Permille = 500
    density: Permille = 500
    motion: Permille = 500

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "energy": self.energy,
            "warmth": self.warmth,
            "density": self.density,
            "motion": self.motion,
        }


class TypographicStyle(BaseModel):
    """Typographic behaviour of a text layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hierarchy: Literal["display", "title", "subtitle", "body", "caption"] = "title"
    weight_permille: Permille = 500
    size_permille: Permille = 500
    alignment: Literal["start", "center", "end"] = "center"
    rtl: bool = False

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "hierarchy": self.hierarchy,
            "weight_permille": self.weight_permille,
            "size_permille": self.size_permille,
            "alignment": self.alignment,
            "rtl": self.rtl,
        }


class TextSpec(BaseModel):
    """The literal text of a synthesized layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=4000)
    typography: TypographicStyle = Field(default_factory=TypographicStyle)
    #: Optional font asset reference; resolved by :meth:`CreativeWork.assert_valid`.
    font_asset_ref: str | None = Field(default=None, max_length=256)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "typography": self.typography.semantic_payload(),
        }


class AudioIntent(BaseModel):
    """Mixing intent of an audio-bearing layer, in integer units.

    ``loudness_target_lufs_milli`` carries LUFS as integer milli-units, so the
    common social target of -14 LUFS is ``-14000`` -- exact, and hashable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    loudness_target_lufs_milli: int = Field(default=-14000, ge=-70000, le=0)
    gain_milli: int = Field(default=0, ge=-24000, le=24000)
    duck_under_voiceover: bool = False

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "loudness_target_lufs_milli": self.loudness_target_lufs_milli,
            "gain_milli": self.gain_milli,
            "duck_under_voiceover": self.duck_under_voiceover,
        }


# ── Assets ───────────────────────────────────────────────────────────────────


class Asset(BaseModel):
    """A source the piece draws on. A *locator*, never a byte stream.

    ``uri`` is a logical reference; resolving it to a file, a URL or a generated
    artifact is an execution concern and happens strictly downstream of this
    plane. Keeping the IR free of paths is also what makes it serialisable
    across machines and safe to log.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str = Field(default="", max_length=256)
    kind: AssetKind
    uri: str = Field(min_length=1, max_length=2048)
    role: SemanticRole
    #: 0 means "duration unknown at authoring time"; the validator then skips
    #: the segment-fits-in-source rule rather than inventing a duration.
    duration_us: int = Field(default=0, ge=0)
    width_px: int = Field(default=0, ge=0)
    height_px: int = Field(default=0, ge=0)
    #: Frame rate in integer milli-fps: 24 fps is 24000, 29.97 fps is 29970.
    frame_rate_milli: int = Field(default=0, ge=0)
    sample_rate_hz: int = Field(default=0, ge=0)
    channels: int = Field(default=0, ge=0)
    origin: Origin = Field(default_factory=Origin)

    def semantic_payload(self) -> dict[str, Any]:
        """Everything about this asset except its identity."""
        return {
            "kind": self.kind.value,
            "uri": self.uri,
            "role": self.role.value,
            "duration_us": self.duration_us,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "frame_rate_milli": self.frame_rate_milli,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "origin": self.origin.semantic_payload(),
        }

    def child_identities(self) -> tuple[str, ...]:
        return ()


# ── Effects ──────────────────────────────────────────────────────────────────


class EffectParam(BaseModel):
    """One named effect parameter in integer milli-units (or a string/bool).

    ``value_milli`` is the single convention for continuous parameters: an
    exposure shift of +1.5 EV is ``1500``, an intensity of 0.75 is ``750``. One
    rule, no floats, and the compiler owns the mapping to whatever scale the
    target backend actually uses.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    value_milli: int | None = None
    value_text: str | None = Field(default=None, max_length=512)
    value_flag: bool | None = None

    @model_validator(mode="after")
    def _exactly_one_value(self) -> EffectParam:
        supplied = (
            self.value_milli is not None,
            self.value_text is not None,
            self.value_flag is not None,
        )
        if sum(supplied) != 1:
            raise ValueError(
                f"effect parameter {self.name!r} must carry exactly one of "
                "value_milli / value_text / value_flag"
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value_milli": self.value_milli,
            "value_text": self.value_text,
            "value_flag": self.value_flag,
        }


class Effect(BaseModel):
    """A named, parameterised operation applied to a layer.

    ``operation`` is a capability operation id (``motion.add_glow``,
    ``color.apply_lut``). Naming the operation rather than spelling a filter is
    the whole point: the IR states *what creative operation* is wanted and the
    compiler decides how the installed capability pack realises it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    effect_id: str = Field(default="", max_length=256)
    family: EffectFamily
    operation: str = Field(min_length=1, max_length=128)
    intensity: Permille = 1000
    params: tuple[EffectParam, ...] = ()
    origin: Origin = Field(default_factory=Origin)

    @model_validator(mode="after")
    def _params_are_sorted_and_unique(self) -> Effect:
        """Parameter order must not affect identity or meaning."""
        names = [p.name for p in self.params]
        if len(set(names)) != len(names):
            raise ValueError(f"effect {self.operation!r} repeats a parameter name: {names}")
        if names != sorted(names):
            raise ValueError(
                f"effect {self.operation!r} parameters must be declared in name order "
                f"so identity is order-independent, got {names}"
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "operation": self.operation,
            "intensity": self.intensity,
            "params": [p.semantic_payload() for p in self.params],
            "origin": self.origin.semantic_payload(),
        }

    def child_identities(self) -> tuple[str, ...]:
        return ()


# ── Layers ───────────────────────────────────────────────────────────────────


class MediaContent(BaseModel):
    """Layer content drawn from an asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["media"] = "media"
    asset_ref: str = Field(min_length=1, max_length=256)
    segment: Segment
    audio: AudioIntent | None = None

    def semantic_payload(self) -> dict[str, Any]:
        # ``asset_ref`` is deliberately absent: it is committed through
        # ``Layer.child_identities`` so the layer's identity follows the asset's.
        return {
            "kind": self.kind,
            "segment": self.segment.semantic_payload(),
            "audio": self.audio.semantic_payload() if self.audio else None,
        }


class TextContent(BaseModel):
    """Layer content synthesized as typography (no source asset)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["text"] = "text"
    text: TextSpec
    timeline: Timing

    def semantic_payload(self) -> dict[str, Any]:
        payload = self.text.semantic_payload()
        # The font reference is an identity-bearing child, so it is remapped and
        # committed separately rather than hashed as raw text here.
        payload["timeline"] = self.timeline.semantic_payload()
        return payload


LayerContent = Annotated[MediaContent | TextContent, Field(discriminator="kind")]


class Layer(BaseModel):
    """One addressed element of a scene: what it is, why it is there, when."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    layer_id: str = Field(default="", max_length=256)
    role: SemanticRole
    content: LayerContent
    style: StyleIntent = Field(default_factory=StyleIntent)
    #: How much of the piece's attention this layer claims. ``EmphasisConstraint``
    #: reads it, which is what turns "focus on the subject" into something a
    #: compiler can satisfy and a test can check.
    emphasis: Permille = 500
    effects: tuple[str, ...] = ()
    #: Layout, never authoring. Always ``None`` in an authored document; the
    #: normaliser assigns it and :meth:`CreativeWork.assert_valid` then requires the
    #: whole layout to be complete and consistent.
    track_id: str | None = Field(default=None, max_length=256)
    origin: Origin = Field(default_factory=Origin)

    @model_validator(mode="after")
    def _audio_only_where_it_can_apply(self) -> Layer:
        if self.role is SemanticRole.VOICEOVER and isinstance(self.content, TextContent):
            raise ValueError("a voiceover layer must be media-backed, not synthesized text")
        return self

    def timeline(self) -> Timing:
        """The layer's span on the piece timeline, whichever content it carries."""
        if isinstance(self.content, MediaContent):
            return self.content.segment.timeline
        return self.content.timeline

    def asset_refs(self) -> tuple[str, ...]:
        """Every asset identity this layer commits to (including its font)."""
        if isinstance(self.content, MediaContent):
            return (self.content.asset_ref,)
        ref = self.content.text.font_asset_ref
        return () if ref is None else (ref,)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "content": self.content.semantic_payload(),
            "style": self.style.semantic_payload(),
            "emphasis": self.emphasis,
            # track_id is layout, not authored meaning. The compiler may remap it
            # to a sealed track identity later without changing the layer's own
            # semantics, so it is excluded from the identity payload.
            "origin": self.origin.semantic_payload(),
        }

    def child_identities(self) -> tuple[str, ...]:
        return (*self.asset_refs(), *self.effects)


# ── Scenes and transitions ───────────────────────────────────────────────────


class Scene(BaseModel):
    """A temporal beat of the piece: a narrative job plus the layers that do it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scene_id: str = Field(default="", max_length=256)
    label: str = Field(min_length=1, max_length=200)
    narrative_role: NarrativeRole
    timing: Timing
    layers: tuple[Layer, ...] = Field(min_length=1)
    origin: Origin = Field(default_factory=Origin)

    def layer_ids(self) -> tuple[str, ...]:
        return tuple(layer.layer_id for layer in self.layers)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "narrative_role": self.narrative_role.value,
            "timing": self.timing.semantic_payload(),
            "origin": self.origin.semantic_payload(),
        }

    def child_identities(self) -> tuple[str, ...]:
        return self.layer_ids()


class Transition(BaseModel):
    """How the piece moves from one scene to the next."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transition_id: str = Field(default="", max_length=256)
    kind: TransitionKind
    duration_us: int = Field(default=0, ge=0)
    from_scene_ref: str = Field(min_length=1, max_length=256)
    to_scene_ref: str = Field(min_length=1, max_length=256)
    origin: Origin = Field(default_factory=Origin)

    @model_validator(mode="after")
    def _not_a_self_transition(self) -> Transition:
        if self.from_scene_ref == self.to_scene_ref:
            raise ValueError("a transition must join two distinct scenes")
        if self.kind is TransitionKind.CUT and self.duration_us != 0:
            raise ValueError("a cut is instantaneous; give it duration_us=0")
        if self.kind is not TransitionKind.CUT and self.duration_us <= 0:
            raise ValueError(f"a {self.kind.value} needs a positive duration_us")
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "duration_us": self.duration_us,
            "origin": self.origin.semantic_payload(),
        }

    def child_identities(self) -> tuple[str, ...]:
        return (self.from_scene_ref, self.to_scene_ref)


# ── Constraints ──────────────────────────────────────────────────────────────


class ConstraintTarget(BaseModel):
    """What a constraint is about: a specific scene, or every layer of a role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["work", "scene", "role"]
    #: Scene identity for ``kind='scene'``; ``None`` for ``work`` and ``role``.
    ref: str | None = Field(default=None, max_length=256)
    role: SemanticRole | None = None

    @model_validator(mode="after")
    def _target_is_coherent(self) -> ConstraintTarget:
        if self.kind == "scene" and not self.ref:
            raise ValueError("a scene-scoped constraint needs a scene ref")
        if self.kind == "role" and self.role is None:
            raise ValueError("a role-scoped constraint needs a role")
        if self.kind == "work" and (self.ref or self.role is not None):
            raise ValueError("a work-scoped constraint takes neither ref nor role")
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "role": self.role.value if self.role else None,
        }

    def child_identities(self) -> tuple[str, ...]:
        return () if self.ref is None else (self.ref,)


class TimingConstraint(BaseModel):
    """A duration band on the work or on one scene.

    Either bound may stand alone: "under fifteen seconds" states a ceiling and
    "at least a minute" states a floor, and both are real requirements people
    make. ``0`` means "that side is unbounded", which is why a band of
    ``min_us=0, max_us=0`` is rejected as stating nothing at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["timing"] = "timing"
    target: ConstraintTarget
    min_us: int = Field(default=0, ge=0)
    max_us: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _band_is_scoped_and_states_something(self) -> TimingConstraint:
        """A duration band needs something with a duration: the work or a scene.

        Rejecting a role scope at construction (rather than reporting a
        violation later) keeps the constraint set honest -- a constraint that
        could never be checked is worse than no constraint, because it reads
        like a guarantee.
        """
        if self.target.kind == "role":
            raise ValueError("a timing constraint must be scoped to the work or to a scene")
        if self.min_us == 0 and self.max_us == 0:
            raise ValueError("a timing constraint needs at least one bound to state")
        if self.max_us and self.max_us < self.min_us:
            raise ValueError(f"max_us ({self.max_us}) must be >= min_us ({self.min_us})")
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target.semantic_payload(),
            "min_us": self.min_us,
            "max_us": self.max_us,
        }


class PacingConstraint(BaseModel):
    """How fast the piece moves, expressed as structure rather than as an adverb.

    "Fast paced" is not compilable; "no scene longer than three seconds" is. This
    is the whole job of the semantic layer -- turning a word a person says into a
    bound a compiler can satisfy and a test can check -- and pacing had no shape
    in the IR until it existed.

    Work-scoped only: pacing is a property of the cut, not of one beat.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["pacing"] = "pacing"
    #: 0 means "unset"; a bound of 0 would forbid every scene.
    max_scene_duration_us: int = Field(default=0, ge=0)
    min_scene_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _at_least_one_bound(self) -> PacingConstraint:
        if self.max_scene_duration_us == 0 and self.min_scene_count == 0:
            raise ValueError("a pacing constraint needs at least one bound to state")
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "max_scene_duration_us": self.max_scene_duration_us,
            "min_scene_count": self.min_scene_count,
        }


class OrderConstraint(BaseModel):
    """One scene must precede another ("logo before the call to action")."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["order"] = "order"
    before: ConstraintTarget
    after: ConstraintTarget

    @model_validator(mode="after")
    def _both_sides_are_scenes(self) -> OrderConstraint:
        for side in (self.before, self.after):
            if side.kind != "scene":
                raise ValueError("order constraints compare scenes")
        if self.before.ref == self.after.ref:
            raise ValueError("a scene cannot be ordered before itself")
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "before": self.before.semantic_payload(),
            "after": self.after.semantic_payload(),
        }


class ExclusionConstraint(BaseModel):
    """Something that must not appear ("no music", "no particle effects")."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["exclusion"] = "exclusion"
    target: ConstraintTarget
    #: Capability operation ids (``motion.add_particles``) and/or asset kinds
    #: (``audio``). Matching is exact -- the plane never guesses at a synonym.
    forbidden: tuple[str, ...] = Field(min_length=1)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target.semantic_payload(),
            "forbidden": list(self.forbidden),
        }


class EmphasisConstraint(BaseModel):
    """A role must claim at least a share of the piece's attention."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["emphasis"] = "emphasis"
    target: ConstraintTarget
    min_share_permille: Permille

    @model_validator(mode="after")
    def _share_needs_a_subset(self) -> EmphasisConstraint:
        """A share is only meaningful for a *subset* of the piece.

        Work scope would compare the piece against itself and always hold, so it
        is rejected here instead of being silently unsatisfiable-or-trivial.
        """
        if self.target.kind == "work":
            raise ValueError("an emphasis constraint must be scoped to a role or a scene")
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target.semantic_payload(),
            "min_share_permille": self.min_share_permille,
        }


class QualityConstraint(BaseModel):
    """A floor on the delivered output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["quality"] = "quality"
    min_width_px: int = Field(default=0, ge=0)
    min_height_px: int = Field(default=0, ge=0)
    min_frame_rate_milli: int = Field(default=0, ge=0)
    max_duration_us: int = Field(default=0, ge=0)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "min_width_px": self.min_width_px,
            "min_height_px": self.min_height_px,
            "min_frame_rate_milli": self.min_frame_rate_milli,
            "max_duration_us": self.max_duration_us,
        }


ConstraintSpec = Annotated[
    TimingConstraint
    | PacingConstraint
    | OrderConstraint
    | ExclusionConstraint
    | EmphasisConstraint
    | QualityConstraint,
    Field(discriminator="kind"),
]


class Constraint(BaseModel):
    """A user requirement that survives compilation and can be checked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    constraint_id: str = Field(default="", max_length=256)
    priority: Priority = Priority.HARD
    spec: ConstraintSpec
    origin: Origin = Field(default_factory=Origin)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "priority": self.priority.value,
            "spec": self.spec.semantic_payload(),
            "origin": self.origin.semantic_payload(),
        }

    def child_identities(self) -> tuple[str, ...]:
        if isinstance(self.spec, OrderConstraint):
            return (*self.spec.before.child_identities(), *self.spec.after.child_identities())
        if isinstance(self.spec, (TimingConstraint, ExclusionConstraint, EmphasisConstraint)):
            return self.spec.target.child_identities()
        return ()  # OrderConstraint handled above; PacingConstraint / QualityConstraint
        # are work-scoped and commit to no identity


class Violation(BaseModel):
    """One checked constraint that did not hold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    constraint_id: str
    priority: Priority
    kind: ConstraintKind
    detail: str


# ── Brief, output, layout ────────────────────────────────────────────────────


class CreativeBrief(BaseModel):
    """What the piece is for, and the surface language it was asked in.

    ``semantic_intents`` carries the *unresolved* phrases ("cinematic", "fast
    paced", "premium"). They are provenance, not meaning: the semantics layer is
    responsible for turning each one into ``Constraint`` values, and
    ``unresolved_intents`` records the ones it could not, so an unresolved
    phrase is always a declared gap rather than a silently dropped instruction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: str = Field(min_length=1, max_length=2000)
    audience: str | None = Field(default=None, max_length=500)
    semantic_intents: tuple[str, ...] = ()
    unresolved_intents: tuple[str, ...] = ()
    origin: Origin = Field(default_factory=Origin)

    @model_validator(mode="after")
    def _unresolved_must_have_been_declared(self) -> CreativeBrief:
        unknown = [i for i in self.unresolved_intents if i not in self.semantic_intents]
        if unknown:
            raise ValueError(
                "unresolved_intents must be a subset of semantic_intents, "
                f"never declared: {unknown}"
            )
        return self

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "audience": self.audience,
            "semantic_intents": list(self.semantic_intents),
            "unresolved_intents": list(self.unresolved_intents),
            "origin": self.origin.semantic_payload(),
        }


class OutputRequirement(BaseModel):
    """What the delivered artifact must be. Integer units throughout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    container: Literal["mp4", "mov", "webm", "gif"] = "mp4"
    width_px: int = Field(default=1920, gt=0, le=8192)
    height_px: int = Field(default=1080, gt=0, le=8192)
    frame_rate_milli: int = Field(default=24000, ge=1000, le=240000)
    max_duration_us: int = Field(default=0, ge=0)
    loudness_target_lufs_milli: int = Field(default=-14000, ge=-70000, le=0)
    color_space: Literal["bt709", "bt2020", "srgb"] = "bt709"

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "frame_rate_milli": self.frame_rate_milli,
            "max_duration_us": self.max_duration_us,
            "loudness_target_lufs_milli": self.loudness_target_lufs_milli,
            "color_space": self.color_space,
        }


class Track(BaseModel):
    """Layout: an ordered set of layer identities on one render track.

    Produced by the normaliser, never authored. See the module docstring for why
    tracks are excluded from the authoring surface.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    track_id: str = Field(default="", max_length=256)
    kind: Literal["video", "audio", "text"]
    layer_ids: tuple[str, ...] = Field(min_length=1)

    def semantic_payload(self) -> dict[str, Any]:
        return {"kind": self.kind}

    def child_identities(self) -> tuple[str, ...]:
        return self.layer_ids


# ── The document ─────────────────────────────────────────────────────────────


class CreativeWork(BaseModel):
    """A complete creative document: the plane's one exchange type.

    Construct one freely -- ids may be empty or stale on the way in -- then call
    :meth:`seal` to make every identity the true content address. :meth:`assert_valid`
    runs the full structural rule set, and :meth:`verify_identity` proves the
    document has not drifted from its own content.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ir_version: IRVersion = IR_VERSION
    work_id: str = Field(default="", max_length=256)
    brief: CreativeBrief
    assets: tuple[Asset, ...] = ()
    effects: tuple[Effect, ...] = ()
    scenes: tuple[Scene, ...] = Field(min_length=1)
    transitions: tuple[Transition, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    output: OutputRequirement = Field(default_factory=OutputRequirement)
    layout: tuple[Track, ...] = ()

    # -- identity ────────────────────────────────────────────────────────────

    @property
    def duration_us(self) -> int:
        """Length of the piece: the end of the last scene."""
        return max(scene.timing.end_us for scene in self.scenes)

    def all_layers(self) -> tuple[Layer, ...]:
        return tuple(layer for scene in self.scenes for layer in scene.layers)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "ir_version": self.ir_version,
            "brief": self.brief.semantic_payload(),
            "output": self.output.semantic_payload(),
        }

    def child_identities(self) -> tuple[str, ...]:
        return (
            *(a.asset_id for a in self.assets),
            *(e.effect_id for e in self.effects),
            *(s.scene_id for s in self.scenes),
            *(t.transition_id for t in self.transitions),
            *(c.constraint_id for c in self.constraints),
            *(t.track_id for t in self.layout),
        )

    def seal(self) -> CreativeWork:
        """Return this document with every identity set to its content address."""
        return seal_work(self)

    def verify_identity(self) -> None:
        """Raise :class:`IdentityError` unless every id is the true content address.

        Re-deriving identity from content is what makes the document
        self-verifying: a hand-edited id, or a document built by a stale builder,
        is rejected here instead of compiling into a plan nobody asked for.
        """
        expected = seal_work(self)
        drift = _identity_drift(self, expected)
        if drift:
            raise IdentityError(
                "creative document identities do not match their content: " + "; ".join(drift),
                problems=tuple(drift),
            )

    # -- validation ──────────────────────────────────────────────────────────

    def assert_valid(self) -> CreativeWork:
        """Check every structural rule; raise with *all* problems, not the first.

        Named ``assert_valid`` rather than ``validate`` on purpose: pydantic's
        ``BaseModel.validate`` is a *classmethod* with a different signature, and
        shadowing it on a model would break Liskov substitution for anyone who
        reaches for the pydantic API. ``assert_valid`` also mirrors
        :meth:`assert_constraints`, so the two gates read as one family.

        Reporting the whole set at once matters in practice: a strategy layer
        that produces a broken document gets one actionable list instead of
        discovering problems one rebuild at a time.
        """
        problems: list[str] = []
        problems += _check_unique_identities(self)
        problems += _check_references(self)
        problems += _check_scene_timeline(self)
        problems += _check_layer_placement(self)
        problems += _check_segment_bounds(self)
        problems += _check_output_bounds(self)
        problems += _check_layout(self)
        if problems:
            raise IRValidationError(
                f"creative document is invalid ({len(problems)} problem(s)): "
                + "; ".join(problems),
                problems=tuple(problems),
            )
        return self

    # -- constraints ─────────────────────────────────────────────────────────

    def check_constraints(self) -> tuple[Violation, ...]:
        """Check every constraint that the IR itself can decide.

        All five kinds are decidable here, which is the point of the design: a
        constraint is not a comment, it is a predicate over the document. The
        compiler re-checks the same predicates against its plan in a later
        slice, which is what "meaning-preserving compilation" has to mean
        concretely.
        """
        violations: list[Violation] = []
        for constraint in self.constraints:
            detail = _evaluate(self, constraint)
            if detail is not None:
                violations.append(
                    Violation(
                        constraint_id=constraint.constraint_id,
                        priority=constraint.priority,
                        kind=ConstraintKind(constraint.spec.kind),
                        detail=detail,
                    )
                )
        return tuple(violations)

    def assert_constraints(self) -> None:
        """Raise :class:`ConstraintViolationError` if any *hard* constraint fails.

        Soft violations are reported by :meth:`check_constraints` but do not
        raise: a soft constraint is a preference the compiler may trade away and
        must say so, not a reason to refuse the job.
        """
        hard = tuple(v for v in self.check_constraints() if v.priority is Priority.HARD)
        if hard:
            raise ConstraintViolationError(
                f"{len(hard)} hard constraint(s) violated: " + "; ".join(v.detail for v in hard),
                violations=tuple(v.detail for v in hard),
            )

    # -- serialization ───────────────────────────────────────────────────────

    def to_canonical_json(self) -> str:
        """The one byte string for this document: sorted keys, no float noise."""
        return canonical_json(self.model_dump(mode="json"))

    @classmethod
    def from_canonical_json(cls, raw: str) -> CreativeWork:
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise SerializationError(f"canonical JSON is not parseable: {exc}") from exc
        if not isinstance(payload, dict):
            raise SerializationError("canonical JSON must be an object")
        try:
            return cls.model_validate(payload)
        except ValueError as exc:
            raise SerializationError(f"canonical JSON is not a valid CreativeWork: {exc}") from exc


# ── Sealing (the Merkle pass) ────────────────────────────────────────────────
#
# Identity is derived bottom-up over a strict topological order:
#
#   assets, effects  (no IR children)
#     -> layers      (commit asset + effect identities)
#       -> scenes    (commit layer identities)
#         -> transitions, constraints, tracks (commit scene/layer identities)
#           -> work  (commits everything)
#
# Each step first *remaps* references to the identities assigned below it, then
# hashes the remapped element. Remap-then-hash is what makes a parent's identity
# a genuine commitment to its children's content rather than to their old names.


def _identity_drift(actual: CreativeWork, expected: CreativeWork) -> list[str]:
    drift: list[str] = []
    pairs: list[tuple[str, str, str]] = [
        ("work", actual.work_id, expected.work_id),
        *(
            ("asset", a.asset_id, b.asset_id)
            for a, b in zip(actual.assets, expected.assets, strict=True)
        ),
        *(
            ("effect", a.effect_id, b.effect_id)
            for a, b in zip(actual.effects, expected.effects, strict=True)
        ),
        *(
            ("scene", a.scene_id, b.scene_id)
            for a, b in zip(actual.scenes, expected.scenes, strict=True)
        ),
        *(
            ("transition", a.transition_id, b.transition_id)
            for a, b in zip(actual.transitions, expected.transitions, strict=True)
        ),
        *(
            ("constraint", a.constraint_id, b.constraint_id)
            for a, b in zip(actual.constraints, expected.constraints, strict=True)
        ),
        *(
            ("track", a.track_id, b.track_id)
            for a, b in zip(actual.layout, expected.layout, strict=True)
        ),
    ]
    for kind, got, want in pairs:
        if got != want:
            drift.append(f"{kind} {got or '<empty>'} should be {want}")
    for actual_scene, expected_scene in zip(actual.scenes, expected.scenes, strict=True):
        for got_layer, want_layer in zip(actual_scene.layers, expected_scene.layers, strict=True):
            if got_layer.layer_id != want_layer.layer_id:
                drift.append(
                    f"layer {got_layer.layer_id or '<empty>'} should be {want_layer.layer_id}"
                )
    return drift


def _remap(mapping: dict[str, str], reference: str, *, what: str) -> str:
    """Look up a remapped identity, or fail with the typed plane error.

    Sealing resolves references before hashing, so a dangling reference is
    discovered here. Raising :class:`DanglingReferenceError` instead of letting
    a ``KeyError`` escape matters: ``seal`` and ``verify_identity`` are called on
    documents that may well be broken, and the plane's contract is that a broken
    document produces a *typed, actionable* failure.
    """
    try:
        return mapping[reference]
    except KeyError as exc:
        raise DanglingReferenceError(
            f"{what} references {reference!r}, which the document does not declare"
        ) from exc


def seal_work(work: CreativeWork) -> CreativeWork:
    """Return ``work`` with every identity replaced by its content address.

    Pure and idempotent: ``seal_work(seal_work(w)) == seal_work(w)``. A document
    whose ids are already correct is returned unchanged in content (a new frozen
    instance, same identities).

    Raises :class:`DanglingReferenceError` if the document references an asset,
    effect, scene or layer it does not declare -- sealing resolves references,
    so it cannot proceed on an unresolved one.
    """
    # 1. assets -- leaves of the tree
    asset_ids: dict[str, str] = {}
    sealed_assets: list[Asset] = []
    for asset in work.assets:
        new_id = content_id(PREFIX_ASSET, asset.semantic_payload())
        asset_ids[asset.asset_id] = new_id
        sealed_assets.append(asset.model_copy(update={"asset_id": new_id}))

    # 2. effects -- also leaves
    effect_ids: dict[str, str] = {}
    sealed_effects: list[Effect] = []
    for effect in work.effects:
        new_id = content_id(PREFIX_EFFECT, effect.semantic_payload())
        effect_ids[effect.effect_id] = new_id
        sealed_effects.append(effect.model_copy(update={"effect_id": new_id}))

    # 3. scenes: remap each layer's references, hash it, then hash the scene
    layer_ids: dict[str, str] = {}
    sealed_scenes: list[Scene] = []
    for scene in work.scenes:
        sealed_layers: list[Layer] = []
        for layer in scene.layers:
            content = _remap_content(layer.content, asset_ids, layer=layer.layer_id)
            remapped = layer.model_copy(
                update={
                    "content": content,
                    "effects": tuple(
                        _remap(effect_ids, e, what=f"layer {layer.layer_id}") for e in layer.effects
                    ),
                }
            )
            new_id = sealed_id(
                PREFIX_LAYER,
                remapped.semantic_payload(),
                remapped.child_identities(),
                preserve_order=True,
            )
            layer_ids[layer.layer_id] = new_id
            sealed_layers.append(remapped.model_copy(update={"layer_id": new_id}))
        rebuilt = scene.model_copy(update={"layers": tuple(sealed_layers)})
        new_id = sealed_id(
            PREFIX_SCENE,
            rebuilt.semantic_payload(),
            rebuilt.child_identities(),
            preserve_order=True,
        )
        sealed_scenes.append(rebuilt.model_copy(update={"scene_id": new_id}))

    scene_ids = {
        old.scene_id: new.scene_id for old, new in zip(work.scenes, sealed_scenes, strict=True)
    }

    # 4. transitions commit scene identities
    sealed_transitions: list[Transition] = []
    for transition in work.transitions:
        remapped_transition = transition.model_copy(
            update={
                "from_scene_ref": _remap(
                    scene_ids,
                    transition.from_scene_ref,
                    what=f"transition {transition.transition_id}",
                ),
                "to_scene_ref": _remap(
                    scene_ids,
                    transition.to_scene_ref,
                    what=f"transition {transition.transition_id}",
                ),
            }
        )
        new_id = sealed_id(
            PREFIX_TRANSITION,
            remapped_transition.semantic_payload(),
            remapped_transition.child_identities(),
        )
        sealed_transitions.append(remapped_transition.model_copy(update={"transition_id": new_id}))

    # 5. constraints commit whatever scene identity they scope to
    sealed_constraints: list[Constraint] = []
    for constraint in work.constraints:
        remapped_constraint = constraint.model_copy(
            update={
                "spec": _remap_constraint_spec(constraint.spec, scene_ids, constraint.constraint_id)
            }
        )
        new_id = sealed_id(
            PREFIX_CONSTRAINT,
            remapped_constraint.semantic_payload(),
            remapped_constraint.child_identities(),
        )
        sealed_constraints.append(remapped_constraint.model_copy(update={"constraint_id": new_id}))

    # 6. layout commits layer identities
    sealed_tracks: list[Track] = []
    for track in work.layout:
        remapped_track = track.model_copy(
            update={
                "layer_ids": tuple(
                    _remap(layer_ids, i, what=f"track {track.track_id}") for i in track.layer_ids
                )
            }
        )
        new_id = sealed_id(
            PREFIX_TRACK,
            remapped_track.semantic_payload(),
            remapped_track.child_identities(),
            preserve_order=True,
        )
        sealed_tracks.append(remapped_track.model_copy(update={"track_id": new_id}))

    track_ids = {
        old.track_id: new.track_id
        for old, new in zip(work.layout, sealed_tracks, strict=True)
        if old.track_id
    }
    remapped_track_scenes: list[Scene] = []
    for scene in sealed_scenes:
        rewritten_layers: list[Layer] = []
        for layer in scene.layers:
            if layer.track_id and layer.track_id in track_ids:
                rewritten_layers.append(
                    layer.model_copy(update={"track_id": track_ids[layer.track_id]})
                )
            else:
                # Unknown track claims are preserved verbatim so assert_valid can
                # reject them later instead of the sealer guessing a repair.
                rewritten_layers.append(layer)
        remapped_track_scenes.append(scene.model_copy(update={"layers": tuple(rewritten_layers)}))

    # 7. the root commits everything
    rebuilt_work = work.model_copy(
        update={
            "assets": tuple(sealed_assets),
            "effects": tuple(sealed_effects),
            "scenes": tuple(remapped_track_scenes),
            "transitions": tuple(sealed_transitions),
            "constraints": tuple(sealed_constraints),
            "layout": tuple(sealed_tracks),
        }
    )
    work_id = sealed_id(
        PREFIX_WORK, rebuilt_work.semantic_payload(), rebuilt_work.child_identities()
    )
    return rebuilt_work.model_copy(update={"work_id": work_id})


def _remap_content(content: LayerContent, asset_ids: dict[str, str], *, layer: str) -> LayerContent:
    """Rewrite a layer content's asset/font reference to a sealed identity."""
    if isinstance(content, MediaContent):
        return content.model_copy(
            update={"asset_ref": _remap(asset_ids, content.asset_ref, what=f"layer {layer}")}
        )
    ref = content.text.font_asset_ref
    if ref is None:
        return content
    return content.model_copy(
        update={
            "text": content.text.model_copy(
                update={"font_asset_ref": _remap(asset_ids, ref, what=f"layer {layer}")}
            )
        }
    )


def _remap_constraint_spec(
    spec: ConstraintSpec, scene_ids: dict[str, str], constraint_id: str
) -> ConstraintSpec:
    """Rewrite scene references inside a constraint spec to sealed identities."""
    if isinstance(spec, OrderConstraint):
        return spec.model_copy(
            update={
                "before": _remap_target(spec.before, scene_ids, constraint_id),
                "after": _remap_target(spec.after, scene_ids, constraint_id),
            }
        )
    if isinstance(spec, (TimingConstraint, ExclusionConstraint, EmphasisConstraint)):
        return spec.model_copy(
            update={"target": _remap_target(spec.target, scene_ids, constraint_id)}
        )
    return spec  # PacingConstraint and QualityConstraint carry no scene reference


def _remap_target(
    target: ConstraintTarget, scene_ids: dict[str, str], constraint_id: str
) -> ConstraintTarget:
    if target.kind != "scene" or target.ref is None:
        return target
    return target.model_copy(
        update={"ref": _remap(scene_ids, target.ref, what=f"constraint {constraint_id}")}
    )


# ── Structural rules ─────────────────────────────────────────────────────────


def _check_unique_identities(work: CreativeWork) -> list[str]:
    """Every identity in the document must be unique, across all element kinds."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    buckets: list[tuple[str, tuple[str, ...]]] = [
        ("asset", tuple(a.asset_id for a in work.assets)),
        ("effect", tuple(e.effect_id for e in work.effects)),
        ("scene", tuple(s.scene_id for s in work.scenes)),
        ("layer", tuple(layer.layer_id for layer in work.all_layers())),
        ("transition", tuple(t.transition_id for t in work.transitions)),
        ("constraint", tuple(c.constraint_id for c in work.constraints)),
        ("track", tuple(t.track_id for t in work.layout)),
        ("work", (work.work_id,)),
    ]
    for kind, ids in buckets:
        for element_id in ids:
            if not element_id:
                problems.append(f"{kind} identity is empty (was the document sealed?)")
            elif element_id in seen:
                problems.append(
                    f"{kind} identity {element_id} collides with {seen[element_id]} identity"
                )
            else:
                seen[element_id] = kind
    return problems


def _check_references(work: CreativeWork) -> list[str]:
    """Every reference must resolve to a declared identity."""
    problems: list[str] = []
    asset_ids = {a.asset_id for a in work.assets}
    effect_ids = {e.effect_id for e in work.effects}
    scene_ids = {s.scene_id for s in work.scenes}
    layer_ids = {layer.layer_id for layer in work.all_layers()}

    for scene in work.scenes:
        for layer in scene.layers:
            for ref in layer.asset_refs():
                if ref not in asset_ids:
                    problems.append(
                        f"layer {layer.layer_id} in scene {scene.scene_id} references "
                        f"unknown asset {ref}"
                    )
            for ref in layer.effects:
                if ref not in effect_ids:
                    problems.append(
                        f"layer {layer.layer_id} in scene {scene.scene_id} references "
                        f"unknown effect {ref}"
                    )

    for transition in work.transitions:
        for ref in (transition.from_scene_ref, transition.to_scene_ref):
            if ref not in scene_ids:
                problems.append(
                    f"transition {transition.transition_id} references unknown scene {ref}"
                )

    for constraint in work.constraints:
        for ref in _constraint_scene_refs(constraint):
            if ref not in scene_ids:
                problems.append(
                    f"constraint {constraint.constraint_id} references unknown scene {ref}"
                )

    for track in work.layout:
        for ref in track.layer_ids:
            if ref not in layer_ids:
                problems.append(f"track {track.track_id} references unknown layer {ref}")
    return problems


def _constraint_scene_refs(constraint: Constraint) -> tuple[str, ...]:
    spec = constraint.spec
    if isinstance(spec, OrderConstraint):
        return tuple(r for r in (spec.before.ref, spec.after.ref) if r is not None)
    if isinstance(spec, (TimingConstraint, ExclusionConstraint, EmphasisConstraint)):
        return () if spec.target.ref is None else (spec.target.ref,)
    return ()


def _check_scene_timeline(work: CreativeWork) -> list[str]:
    """Scenes must ascend and never overlap; a transition's scenes must abut."""
    problems: list[str] = []
    ordered = sorted(work.scenes, key=lambda s: s.timing.start_us)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous.timing.overlaps(current.timing):
            problems.append(
                f"scene {previous.scene_id} ({previous.timing.start_us}.."
                f"{previous.timing.end_us}us) overlaps scene {current.scene_id} "
                f"({current.timing.start_us}..{current.timing.end_us}us)"
            )
    scene_by_id = {s.scene_id: s for s in work.scenes}
    for transition in work.transitions:
        source = scene_by_id.get(transition.from_scene_ref)
        target = scene_by_id.get(transition.to_scene_ref)
        if source is None or target is None:
            continue  # already reported by _check_references
        if target.timing.start_us < source.timing.start_us:
            problems.append(
                f"transition {transition.transition_id} runs backwards: "
                f"{transition.from_scene_ref} starts after {transition.to_scene_ref}"
            )
    return problems


def _check_layer_placement(work: CreativeWork) -> list[str]:
    """Every layer must live entirely inside the scene that declares it."""
    problems: list[str] = []
    for scene in work.scenes:
        for layer in scene.layers:
            span = layer.timeline()
            if not scene.timing.contains(span):
                problems.append(
                    f"layer {layer.layer_id} ({span.start_us}..{span.end_us}us) escapes "
                    f"scene {scene.scene_id} ({scene.timing.start_us}..{scene.timing.end_us}us)"
                )
    return problems


def _check_segment_bounds(work: CreativeWork) -> list[str]:
    """A media layer must not read past the end of its source asset.

    Assets whose ``duration_us`` is 0 are "duration unknown at authoring time";
    the rule is skipped for those rather than inventing a bound.
    """
    problems: list[str] = []
    assets = {a.asset_id: a for a in work.assets}
    for scene in work.scenes:
        for layer in scene.layers:
            if not isinstance(layer.content, MediaContent):
                continue
            asset = assets.get(layer.content.asset_ref)
            if asset is None or asset.duration_us == 0:
                continue
            if layer.content.segment.source_out_us > asset.duration_us:
                problems.append(
                    f"layer {layer.layer_id} reads {layer.content.segment.source_out_us}us "
                    f"of asset {asset.asset_id} which is only {asset.duration_us}us long"
                )
    return problems


def _check_output_bounds(work: CreativeWork) -> list[str]:
    """The piece must fit the structural duration ceiling it declares for itself."""
    problems: list[str] = []
    ceiling = work.output.max_duration_us
    if ceiling and work.duration_us > ceiling:
        problems.append(
            f"piece is {work.duration_us}us long but output.max_duration_us is {ceiling}us"
        )
    return problems


def _check_layout(work: CreativeWork) -> list[str]:
    """If a layout is present it must be complete, single-assigned and sane."""
    if not work.layout:
        return []  # an authored document legitimately has no layout yet
    problems: list[str] = []
    layers = {layer.layer_id: layer for layer in work.all_layers()}
    assigned: dict[str, str] = {}
    for track in work.layout:
        spans: list[tuple[str, Timing]] = []
        for layer_id in track.layer_ids:
            layer = layers.get(layer_id)
            if layer is None:
                continue  # reported by _check_references
            if layer_id in assigned:
                problems.append(
                    f"layer {layer_id} is on both track {assigned[layer_id]} and {track.track_id}"
                )
            assigned[layer_id] = track.track_id
            spans.append((layer_id, layer.timeline()))
        for (left_id, left), (right_id, right) in zip(spans, spans[1:], strict=False):
            if left.overlaps(right):
                problems.append(f"track {track.track_id} overlaps {left_id} with {right_id}")
    for layer_id in layers:
        if layer_id not in assigned:
            problems.append(f"layer {layer_id} is not on any track in the layout")
    for scene in work.scenes:
        for layer in scene.layers:
            track_id = assigned.get(layer.layer_id)
            if track_id and layer.track_id != track_id:
                problems.append(
                    f"layer {layer.layer_id} claims track {layer.track_id} but the layout "
                    f"places it on {track_id}"
                )
    return list(dict.fromkeys(problems))


# ── Constraint evaluation ────────────────────────────────────────────────────


def _evaluate(work: CreativeWork, constraint: Constraint) -> str | None:
    """Return a human-readable violation detail, or ``None`` when it holds.

    Unknown scene references evaluate to ``None`` here on purpose: a dangling
    reference is a *structural* fault and :meth:`CreativeWork.assert_valid` owns it.
    Double-reporting it as a constraint violation would blur the two failure
    modes the error hierarchy exists to keep apart.
    """
    spec = constraint.spec
    if isinstance(spec, TimingConstraint):
        return _evaluate_timing(work, spec)
    if isinstance(spec, PacingConstraint):
        return _evaluate_pacing(work, spec)
    if isinstance(spec, OrderConstraint):
        return _evaluate_order(work, spec)
    if isinstance(spec, ExclusionConstraint):
        return _evaluate_exclusion(work, spec)
    if isinstance(spec, EmphasisConstraint):
        return _evaluate_emphasis(work, spec)
    return _evaluate_quality(work, spec)


def _evaluate_timing(work: CreativeWork, spec: TimingConstraint) -> str | None:
    target = spec.target
    if target.kind == "work":
        actual = work.duration_us
        label = "work"
    elif target.ref:
        scene = _scene(work, target.ref)
        if scene is None:
            return None
        actual = scene.timing.duration_us
        label = f"scene {scene.label}"
    else:  # pragma: no cover - ConstraintTarget rejects a scene scope with no ref
        return None
    if actual < spec.min_us:
        return f"{label} is {actual}us, below the {spec.min_us}us minimum"
    if spec.max_us and actual > spec.max_us:
        return f"{label} is {actual}us, above the {spec.max_us}us maximum"
    return None


def _evaluate_pacing(work: CreativeWork, spec: PacingConstraint) -> str | None:
    problems: list[str] = []
    if spec.max_scene_duration_us:
        slowest = max(work.scenes, key=lambda scene: scene.timing.duration_us)
        if slowest.timing.duration_us > spec.max_scene_duration_us:
            problems.append(
                f"scene {slowest.label} runs {slowest.timing.duration_us}us, above the "
                f"{spec.max_scene_duration_us}us pacing ceiling"
            )
    if spec.min_scene_count and len(work.scenes) < spec.min_scene_count:
        problems.append(
            f"the piece has {len(work.scenes)} scene(s), below the required {spec.min_scene_count}"
        )
    return "; ".join(problems) or None


def _evaluate_order(work: CreativeWork, spec: OrderConstraint) -> str | None:
    before = _scene(work, spec.before.ref or "")
    after = _scene(work, spec.after.ref or "")
    if before is None or after is None:
        return None
    if before.timing.start_us >= after.timing.start_us:
        return (
            f"scene {before.label} must precede scene {after.label} but starts at "
            f"{before.timing.start_us}us vs {after.timing.start_us}us"
        )
    return None


def _evaluate_exclusion(work: CreativeWork, spec: ExclusionConstraint) -> str | None:
    forbidden = set(spec.forbidden)
    scope_layers = _scoped_layers(work, spec.target)
    if scope_layers is None:
        return None
    assets = {a.asset_id: a for a in work.assets}
    effects = {e.effect_id: e for e in work.effects}
    for layer in scope_layers:
        for ref in layer.asset_refs():
            asset = assets.get(ref)
            if asset is not None and asset.kind.value in forbidden:
                return f"layer {layer.layer_id} uses forbidden asset kind {asset.kind.value}"
        for ref in layer.effects:
            effect = effects.get(ref)
            if effect is not None and effect.operation in forbidden:
                return f"layer {layer.layer_id} applies forbidden operation {effect.operation}"
    return None


def _evaluate_emphasis(work: CreativeWork, spec: EmphasisConstraint) -> str | None:
    scope_layers = _scoped_layers(work, spec.target)
    if scope_layers is None:
        return None
    total = sum(layer.emphasis for layer in work.all_layers())
    if total == 0:
        return "no layer carries any emphasis, so no share can be measured"
    share = 1000 * sum(layer.emphasis for layer in scope_layers) // total
    if share < spec.min_share_permille:
        return f"share is {share} permille, below the required {spec.min_share_permille} permille"
    return None


def _evaluate_quality(work: CreativeWork, spec: QualityConstraint) -> str | None:
    output = work.output
    if output.width_px < spec.min_width_px:
        return f"output width {output.width_px}px is below the {spec.min_width_px}px floor"
    if output.height_px < spec.min_height_px:
        return f"output height {output.height_px}px is below the {spec.min_height_px}px floor"
    if output.frame_rate_milli < spec.min_frame_rate_milli:
        return (
            f"output frame rate {output.frame_rate_milli} milli-fps is below the "
            f"{spec.min_frame_rate_milli} milli-fps floor"
        )
    if spec.max_duration_us and work.duration_us > spec.max_duration_us:
        return (
            f"piece is {work.duration_us}us long, above the {spec.max_duration_us}us "
            "quality ceiling"
        )
    return None


def _scene(work: CreativeWork, scene_id: str) -> Scene | None:
    for scene in work.scenes:
        if scene.scene_id == scene_id:
            return scene
    return None


def _scoped_layers(work: CreativeWork, target: ConstraintTarget) -> tuple[Layer, ...] | None:
    """Layers a constraint applies to, or ``None`` when its scope does not resolve."""
    if target.kind == "work":
        return work.all_layers()
    if target.kind == "role":
        return tuple(layer for layer in work.all_layers() if layer.role is target.role)
    scene = _scene(work, target.ref or "")
    return None if scene is None else scene.layers
