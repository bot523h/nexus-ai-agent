"""Creative semantics: the phrases a person says, turned into bounds a compiler can meet.

The problem this solves
-----------------------
``"cinematic"``, ``"fast paced"``, ``"minimal"``, ``"dramatic reveal"``,
``"focus on subject"``, ``"social short"``, ``"premium"``, ``"high retention"``
are how people actually ask for things. None of them is compilable. Left as
strings they are worse than useless: they read like requirements while being
unfalsifiable, so a plan can ignore them and nothing notices.

This module is the meaning-preserving half of the pipeline:

``Semantic Intent -> Constraints -> Creative Operations -> IR``

A recognised phrase becomes a typed :class:`~nexus_ai_agent.creative.intelligence
.ir.Constraint` with a numeric bound -- "fast paced" becomes "no scene longer
than three seconds" -- which the IR can check, a compiler can satisfy, and a test
can falsify. An unrecognised phrase becomes an entry in
``brief.unresolved_intents``: a **declared gap**, never a silent disappearance.

Three deliberate boundaries
---------------------------
**The lexicon is data, not conditionals.** :data:`LEXICON` maps normalised
surface forms onto :class:`SemanticMeaning` values and :data:`DIRECTIVES` maps
meanings onto what they imply. Adding a phrase is a table entry; adding a meaning
is a table entry plus a directive. Neither is an ``if`` buried in a resolver, so
the vocabulary stays inspectable and reviewable.

**Semantic collisions are detected, not resolved.** When two phrases imply
incompatible duration bands, or emphasis shares that sum past 1000 permille, the
resolver reports the collision and refuses to guess a winner. Picking silently
would be the exact failure mode this plane exists to prevent -- the system doing
something other than what was asked and reporting success.

**Style targets are declared, not applied.** A directive may state that "premium"
means a warmer, denser look, and the resolution reports those axis targets. Which
*layers* they land on, and how strongly, is a strategy decision; a lexicon that
rewrote layer styles would be making creative choices it has no basis for. So the
targets are returned for the strategy layer to place, and this module never
mutates a layer.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.intelligence.errors import CreativeIRError
from nexus_ai_agent.creative.intelligence.identity import MICROSECONDS_PER_SECOND
from nexus_ai_agent.creative.intelligence.ir import (
    Constraint,
    ConstraintTarget,
    CreativeWork,
    EmphasisConstraint,
    ExclusionConstraint,
    NarrativeRole,
    Origin,
    PacingConstraint,
    Priority,
    QualityConstraint,
    SemanticRole,
    TimingConstraint,
)

__all__ = [
    "DIRECTIVES",
    "LEXICON",
    "SemanticCollision",
    "SemanticDirective",
    "SemanticMeaning",
    "SemanticResolution",
    "SemanticResolutionError",
    "StyleTarget",
    "apply_semantics",
    "normalize_phrase",
    "resolve_semantics",
]


class SemanticResolutionError(CreativeIRError):
    """The semantic layer was asked for something it cannot do honestly."""


class SemanticCollisionError(SemanticResolutionError):
    """Recognised phrases imply contradictory requirements.

    Raised by :meth:`SemanticResolution.assert_no_collision`. The resolver
    reports collisions rather than resolving them because choosing silently is
    the failure this plane exists to prevent.
    """


# ── Meanings ─────────────────────────────────────────────────────────────────


class SemanticMeaning(str, Enum):
    """The resolved sense of a phrase, independent of the words used for it.

    Declaration order is meaningful: it is the deterministic emission order of
    derived constraints, so the same brief always produces the same document
    identity regardless of the order the phrases arrived in.
    """

    CINEMATIC = "cinematic"
    PREMIUM = "premium"
    FAST_PACED = "fast_paced"
    HIGH_RETENTION = "high_retention"
    MINIMAL = "minimal"
    DRAMATIC_REVEAL = "dramatic_reveal"
    FOCUS_ON_SUBJECT = "focus_on_subject"
    MUSIC_DRIVEN = "music_driven"
    SOCIAL_SHORT = "social_short"
    LONG_FORM = "long_form"


#: Surface forms as a person would type them, mapped onto the meaning they
#: express. Many-to-one on purpose: "سینمایی" and "film look" are the same
#: requirement and must produce the same constraint, not two near-duplicates.
#:
#: This table is *not* the lookup key. :data:`LEXICON` below is built by running
#: every key through :func:`normalize_phrase`, so a surface form can never be
#: written in a spelling the normaliser would never produce -- a defect class
#: that is invisible in review and makes the entry silently unreachable.
_SURFACE_FORMS: dict[str, SemanticMeaning] = {
    # cinematic
    "cinematic": SemanticMeaning.CINEMATIC,
    "film look": SemanticMeaning.CINEMATIC,
    "cinema style": SemanticMeaning.CINEMATIC,
    "سینمایی": SemanticMeaning.CINEMATIC,
    "حالت سینمایی": SemanticMeaning.CINEMATIC,
    # premium
    "premium": SemanticMeaning.PREMIUM,
    "luxury": SemanticMeaning.PREMIUM,
    "high end": SemanticMeaning.PREMIUM,
    "پریمیوم": SemanticMeaning.PREMIUM,
    "لوکس": SemanticMeaning.PREMIUM,
    "لاکچری": SemanticMeaning.PREMIUM,
    # fast paced
    "fast paced": SemanticMeaning.FAST_PACED,
    "fast cuts": SemanticMeaning.FAST_PACED,
    "quick cuts": SemanticMeaning.FAST_PACED,
    "ریتم تند": SemanticMeaning.FAST_PACED,
    "تدوین سریع": SemanticMeaning.FAST_PACED,
    "سریع": SemanticMeaning.FAST_PACED,
    # high retention
    "high retention": SemanticMeaning.HIGH_RETENTION,
    "hook first": SemanticMeaning.HIGH_RETENTION,
    "نگه‌داشت مخاطب": SemanticMeaning.HIGH_RETENTION,
    "نگهداشت مخاطب": SemanticMeaning.HIGH_RETENTION,
    "قلاب اول": SemanticMeaning.HIGH_RETENTION,
    # minimal
    "minimal": SemanticMeaning.MINIMAL,
    "clean": SemanticMeaning.MINIMAL,
    "simple": SemanticMeaning.MINIMAL,
    "مینیمال": SemanticMeaning.MINIMAL,
    "ساده": SemanticMeaning.MINIMAL,
    "تمیز": SemanticMeaning.MINIMAL,
    # dramatic reveal
    "dramatic reveal": SemanticMeaning.DRAMATIC_REVEAL,
    "big reveal": SemanticMeaning.DRAMATIC_REVEAL,
    "رونمایی دراماتیک": SemanticMeaning.DRAMATIC_REVEAL,
    "لحظه دراماتیک": SemanticMeaning.DRAMATIC_REVEAL,
    # focus on subject
    "focus on subject": SemanticMeaning.FOCUS_ON_SUBJECT,
    "subject first": SemanticMeaning.FOCUS_ON_SUBJECT,
    "focus on the product": SemanticMeaning.FOCUS_ON_SUBJECT,
    "تمرکز روی سوژه": SemanticMeaning.FOCUS_ON_SUBJECT,
    "تمرکز روی محصول": SemanticMeaning.FOCUS_ON_SUBJECT,
    # music driven
    "music driven": SemanticMeaning.MUSIC_DRIVEN,
    "music first": SemanticMeaning.MUSIC_DRIVEN,
    "موزیک محور": SemanticMeaning.MUSIC_DRIVEN,
    "موسیقی محور": SemanticMeaning.MUSIC_DRIVEN,
    # social short
    "social short": SemanticMeaning.SOCIAL_SHORT,
    "short form": SemanticMeaning.SOCIAL_SHORT,
    "under 15 seconds": SemanticMeaning.SOCIAL_SHORT,
    "کوتاه": SemanticMeaning.SOCIAL_SHORT,
    "زیر ۱۵ ثانیه": SemanticMeaning.SOCIAL_SHORT,
    "برای سوشال": SemanticMeaning.SOCIAL_SHORT,
    # long form
    "long form": SemanticMeaning.LONG_FORM,
    "documentary": SemanticMeaning.LONG_FORM,
    "مستند": SemanticMeaning.LONG_FORM,
    "ویدیو بلند": SemanticMeaning.LONG_FORM,
}


def _build_lexicon(forms: dict[str, SemanticMeaning]) -> dict[str, SemanticMeaning]:
    """Normalise every surface form into the one key the resolver looks up.

    Two spellings that normalise onto the same key are fine when they mean the
    same thing and a defect when they do not, so a disagreement raises at import
    time rather than letting one entry silently shadow another.
    """
    lexicon: dict[str, SemanticMeaning] = {}
    for surface, meaning in forms.items():
        key = normalize_phrase(surface)
        existing = lexicon.get(key)
        if existing is not None and existing is not meaning:
            raise SemanticResolutionError(
                f"surface forms for {existing.value!r} and {meaning.value!r} both "
                f"normalise to {key!r}; the lexicon cannot tell them apart"
            )
        lexicon[key] = meaning
    return lexicon


class StyleTarget(BaseModel):
    """Where a meaning wants the style axes to move, on a 0..1000 scale."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    energy: int | None = Field(default=None, ge=0, le=1000)
    warmth: int | None = Field(default=None, ge=0, le=1000)
    density: int | None = Field(default=None, ge=0, le=1000)
    motion: int | None = Field(default=None, ge=0, le=1000)


class SemanticDirective(BaseModel):
    """What one resolved meaning implies.

    ``constraints`` is a payload description rather than built models, because
    the constraint a meaning implies can depend on the document (``HIGH_RETENTION``
    scopes to *the hook scene*, which only exists once there is a document).
    Building them in :func:`resolve_semantics` keeps that dependency in one place
    instead of smuggling the document into the table.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    meaning: SemanticMeaning
    #: ``(kind, payload)`` pairs, resolved against the document at resolution time.
    constraints: tuple[tuple[str, dict[str, Any]], ...] = ()
    style: StyleTarget = Field(default_factory=StyleTarget)
    #: Why the meaning implies this, kept as data so a reviewer can audit the
    #: mapping without reading resolver code.
    rationale: str = Field(default="", max_length=300)


#: The meaning table. Every entry is a claim about what a phrase requires, and
#: each one is checkable -- which is the difference between a semantic layer and
#: a list of adjectives.
DIRECTIVES: dict[SemanticMeaning, SemanticDirective] = {
    SemanticMeaning.CINEMATIC: SemanticDirective(
        meaning=SemanticMeaning.CINEMATIC,
        constraints=(
            (
                "quality",
                {"min_width_px": 1920, "min_height_px": 1080, "min_frame_rate_milli": 24000},
            ),
        ),
        style=StyleTarget(warmth=700, motion=700, energy=650),
        rationale="a film look is not deliverable below full HD at a cinema frame rate",
    ),
    SemanticMeaning.PREMIUM: SemanticDirective(
        meaning=SemanticMeaning.PREMIUM,
        constraints=(
            ("quality", {"min_width_px": 1920, "min_height_px": 1080}),
            ("exclusion", {"forbidden": ("motion.add_particles", "motion.add_glow")}),
        ),
        style=StyleTarget(warmth=650, density=350),
        rationale="premium reads as restraint and resolution, not as particle overlays",
    ),
    SemanticMeaning.FAST_PACED: SemanticDirective(
        meaning=SemanticMeaning.FAST_PACED,
        constraints=(("pacing", {"max_scene_duration_us": 3 * MICROSECONDS_PER_SECOND}),),
        style=StyleTarget(energy=850, motion=800),
        rationale="fast pacing is a ceiling on how long any one beat may hold",
    ),
    SemanticMeaning.HIGH_RETENTION: SemanticDirective(
        meaning=SemanticMeaning.HIGH_RETENTION,
        constraints=(("hook_timing", {"max_us": 3 * MICROSECONDS_PER_SECOND}),),
        style=StyleTarget(energy=900),
        rationale="retention is decided in the hook, so the hook gets its own bound",
    ),
    SemanticMeaning.MINIMAL: SemanticDirective(
        meaning=SemanticMeaning.MINIMAL,
        constraints=(
            (
                "exclusion",
                {"forbidden": ("motion.add_particles", "motion.add_glow", "motion.warp")},
            ),
        ),
        style=StyleTarget(density=250, motion=350),
        rationale="minimal is defined by what it refuses to add",
    ),
    SemanticMeaning.DRAMATIC_REVEAL: SemanticDirective(
        meaning=SemanticMeaning.DRAMATIC_REVEAL,
        constraints=(("emphasis", {"role": SemanticRole.SUBJECT, "min_share_permille": 300}),),
        style=StyleTarget(energy=900, motion=850),
        rationale="a reveal only lands if the subject actually dominates attention",
    ),
    SemanticMeaning.FOCUS_ON_SUBJECT: SemanticDirective(
        meaning=SemanticMeaning.FOCUS_ON_SUBJECT,
        constraints=(("emphasis", {"role": SemanticRole.SUBJECT, "min_share_permille": 400}),),
        style=StyleTarget(density=400),
        rationale="focus is a share of attention, which is exactly what emphasis measures",
    ),
    SemanticMeaning.MUSIC_DRIVEN: SemanticDirective(
        meaning=SemanticMeaning.MUSIC_DRIVEN,
        constraints=(("emphasis", {"role": SemanticRole.MUSIC, "min_share_permille": 700}),),
        style=StyleTarget(energy=750),
        rationale="music-led means the bed carries most of the piece's weight",
    ),
    SemanticMeaning.SOCIAL_SHORT: SemanticDirective(
        meaning=SemanticMeaning.SOCIAL_SHORT,
        constraints=(("timing", {"max_us": 15 * MICROSECONDS_PER_SECOND}),),
        style=StyleTarget(energy=800, density=600),
        rationale="social placement imposes a hard ceiling on runtime",
    ),
    SemanticMeaning.LONG_FORM: SemanticDirective(
        meaning=SemanticMeaning.LONG_FORM,
        constraints=(("timing", {"min_us": 60 * MICROSECONDS_PER_SECOND}),),
        style=StyleTarget(energy=350, density=650),
        rationale="long form is a floor on runtime, the opposite bound to social short",
    ),
}


# ── Normalization ────────────────────────────────────────────────────────────

#: Persian ZWNJ and its look-alikes. "نگه‌داشت" and "نگهداشت" are the same word
#: written two ways and must resolve identically.
_ZWNJ = "\u200c\u200d\u200e\u200f"
_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
#: Arabic/Persian digit forms, so "۱۵" and "15" normalise the same.
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_phrase(phrase: str) -> str:
    """Reduce a phrase to the one key the lexicon is written against.

    NFKC (so compatibility forms collapse), case fold, ZWNJ removed, Arabic and
    Persian digits mapped to ASCII, punctuation dropped, whitespace collapsed.
    Normalization lives in exactly one function so the lexicon can be written in
    plain words and still match what people type.
    """
    text = unicodedata.normalize("NFKC", phrase)
    text = text.translate(_DIGITS)
    text = "".join(ch for ch in text if ch not in _ZWNJ)
    text = _PUNCTUATION.sub(" ", text.casefold())
    return _WHITESPACE.sub(" ", text).strip()


#: The lookup table: every surface form reduced to its normalised key. Built
#: rather than written, so an unreachable entry cannot exist.
LEXICON: dict[str, SemanticMeaning] = _build_lexicon(_SURFACE_FORMS)


# ── Resolution ───────────────────────────────────────────────────────────────


class SemanticCollision(BaseModel):
    """Two recognised phrases that cannot both be honoured."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    detail: str
    phrases: tuple[str, ...]


class SemanticResolution(BaseModel):
    """What the brief's phrases turned into, and what they could not.

    ``unresolved`` is not an error channel. It is the honest record of an
    instruction the plane received and could not act on, and it is written back
    onto ``CreativeBrief.unresolved_intents`` by :func:`apply_semantics` so the
    gap travels with the document instead of vanishing into a log line.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    meanings: tuple[SemanticMeaning, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    style_targets: tuple[StyleTarget, ...] = ()
    unresolved: tuple[str, ...] = ()
    collisions: tuple[SemanticCollision, ...] = ()
    #: phrase -> meaning, so provenance stays traceable from word to constraint
    resolved_phrases: tuple[tuple[str, SemanticMeaning], ...] = ()

    def assert_no_collision(self) -> None:
        if self.collisions:
            raise SemanticCollisionError(
                f"{len(self.collisions)} semantic collision(s): "
                + "; ".join(c.detail for c in self.collisions)
            )


def _build_constraint(
    kind: str, payload: dict[str, Any], work: CreativeWork, phrase: str
) -> Constraint | None:
    """Turn one directive entry into a real constraint against a real document.

    Returns ``None`` when the document cannot support the constraint -- a hook
    bound on a piece with no hook scene, for instance. That is reported through
    ``unresolved`` by the caller rather than being silently dropped, because
    "you asked for a fast hook and this piece has no hook" is information the
    user needs.
    """
    origin = Origin(source="user", detail=phrase)
    if kind == "quality":
        return Constraint(priority=Priority.HARD, spec=QualityConstraint(**payload), origin=origin)
    if kind == "exclusion":
        return Constraint(
            priority=Priority.HARD,
            spec=ExclusionConstraint(target=ConstraintTarget(kind="work"), **payload),
            origin=origin,
        )
    if kind == "pacing":
        return Constraint(priority=Priority.HARD, spec=PacingConstraint(**payload), origin=origin)
    if kind == "timing":
        return Constraint(
            priority=Priority.HARD,
            spec=TimingConstraint(target=ConstraintTarget(kind="work"), **payload),
            origin=origin,
        )
    if kind == "emphasis":
        role = payload["role"]
        return Constraint(
            priority=Priority.HARD,
            spec=EmphasisConstraint(
                target=ConstraintTarget(kind="role", role=role),
                min_share_permille=payload["min_share_permille"],
            ),
            origin=origin,
        )
    if kind == "hook_timing":
        hook = _scene_with_role(work, NarrativeRole.HOOK)
        if hook is None:
            return None
        return Constraint(
            priority=Priority.HARD,
            spec=TimingConstraint(
                target=ConstraintTarget(kind="scene", ref=hook.scene_id), **payload
            ),
            origin=origin,
        )
    raise SemanticResolutionError(f"unknown directive constraint kind: {kind!r}")


def _scene_with_role(work: CreativeWork, role: NarrativeRole) -> Any:
    """The first scene carrying a narrative role, in timeline order.

    Deterministic by construction: scenes are sorted by start time, so a piece
    with two hooks always resolves to the same one.
    """
    ordered = sorted(work.scenes, key=lambda scene: scene.timing.start_us)
    for scene in ordered:
        if scene.narrative_role is role:
            return scene
    return None


def resolve_semantics(work: CreativeWork) -> SemanticResolution:
    """Resolve a brief's phrases into constraints against a specific document.

    The document is an argument, not a formality: ``HIGH_RETENTION`` scopes to
    *the hook scene*, which is only knowable once there is one.
    """
    phrases = work.brief.semantic_intents
    meanings: list[SemanticMeaning] = []
    resolved: list[tuple[str, SemanticMeaning]] = []
    unresolved: list[str] = []
    constraints: list[Constraint] = []
    styles: list[StyleTarget] = []
    unsatisfiable: list[str] = []

    seen: set[SemanticMeaning] = set()
    for phrase in phrases:
        meaning = LEXICON.get(normalize_phrase(phrase))
        if meaning is None:
            if phrase not in unresolved:
                unresolved.append(phrase)
            continue
        if phrase not in [p for p, _ in resolved]:
            resolved.append((phrase, meaning))
        if meaning in seen:
            continue  # two phrasings of one requirement state it once
        seen.add(meaning)
        meanings.append(meaning)

    # Emission order follows the enum declaration, never the order the phrases
    # happened to arrive in -- otherwise the same request would produce two
    # different document identities.
    for meaning in sorted(seen, key=lambda m: list(SemanticMeaning).index(m)):
        directive = DIRECTIVES[meaning]
        phrase = next(p for p, m in resolved if m is meaning)
        if any(value is not None for value in directive.style.model_dump().values()):
            styles.append(directive.style)
        for kind, payload in directive.constraints:
            built = _build_constraint(kind, payload, work, phrase)
            if built is None:
                unsatisfiable.append(phrase)
            else:
                constraints.append(built)

    for phrase in dict.fromkeys(unsatisfiable):
        if phrase not in unresolved:
            unresolved.append(phrase)

    resolution = SemanticResolution(
        meanings=tuple(meanings),
        constraints=tuple(constraints),
        style_targets=tuple(styles),
        unresolved=tuple(unresolved),
        resolved_phrases=tuple(resolved),
    )
    return resolution.model_copy(
        update={"collisions": detect_collisions((*work.constraints, *constraints), work)}
    )


def detect_collisions(
    constraints: tuple[Constraint, ...], work: CreativeWork
) -> tuple[SemanticCollision, ...]:
    """Find requirements that cannot all hold, by arithmetic rather than by name.

    Two families are decidable here:

    * **duration bands on one target** -- if the strongest lower bound exceeds
      the weakest upper bound, no duration satisfies both;
    * **emphasis shares** -- shares are permille of one total, so a set of
      distinct roles demanding more than 1000 permille between them is
      unsatisfiable whatever the document looks like.

    Both are checked on the *numbers*, never on which phrases produced them, so a
    new phrase that contradicts an old one is caught without teaching the
    detector about the pair.
    """
    collisions: list[SemanticCollision] = []

    # -- duration bands, grouped by target ---------------------------------
    bands: dict[str, list[TimingConstraint]] = {}
    for constraint in constraints:
        spec = constraint.spec
        if not isinstance(spec, TimingConstraint):
            continue
        key = spec.target.kind if spec.target.ref is None else f"scene:{spec.target.ref}"
        bands.setdefault(key, []).append(spec)
    for key, specs in sorted(bands.items()):
        lower = max((s.min_us for s in specs), default=0)
        ceilings = [s.max_us for s in specs if s.max_us]
        upper = min(ceilings) if ceilings else 0
        if upper and lower > upper:
            label = "the work" if key == "work" else f"scene {_scene_label(work, key)}"
            collisions.append(
                SemanticCollision(
                    kind="duration_band",
                    detail=(
                        f"{label} is required to be at least {lower}us and at most "
                        f"{upper}us, which nothing can satisfy"
                    ),
                    phrases=tuple(
                        _phrase_for(constraints, s) for s in specs if _phrase_for(constraints, s)
                    ),
                )
            )

    # -- emphasis shares ----------------------------------------------------
    shares: list[tuple[SemanticRole, int, str]] = []
    for constraint in constraints:
        spec = constraint.spec
        if isinstance(spec, EmphasisConstraint) and spec.target.role is not None:
            shares.append(
                (spec.target.role, spec.min_share_permille, _phrase_for(constraints, spec))
            )
    distinct: dict[SemanticRole, tuple[int, str]] = {}
    for role, share, phrase in shares:
        current = distinct.get(role)
        if current is None or share > current[0]:
            distinct[role] = (share, phrase)
    total = sum(share for share, _phrase in distinct.values())
    if total > 1000:
        detail = ", ".join(
            f"{role.value} at {share} permille"
            for role, (share, _origin) in sorted(distinct.items(), key=lambda kv: kv[0].value)
        )
        collisions.append(
            SemanticCollision(
                kind="emphasis_share",
                detail=f"emphasis demands sum to {total} permille of one total: {detail}",
                phrases=tuple(phrase for _role, phrase in distinct.values() if phrase),
            )
        )
    return tuple(collisions)


def _scene_label(work: CreativeWork, key: str) -> str:
    ref = key.removeprefix("scene:")
    for scene in work.scenes:
        if scene.scene_id == ref:
            return scene.label
    return ref


def _phrase_for(constraints: tuple[Constraint, ...], spec: Any) -> str:
    for constraint in constraints:
        if constraint.spec is spec:
            return constraint.origin.detail
    return ""


# ── Application ──────────────────────────────────────────────────────────────


def apply_semantics(work: CreativeWork) -> CreativeWork:
    """Return the document with its brief's meaning expressed as constraints.

    Idempotent, and provably so: derived constraints are content-addressed, so
    re-deriving them from an unchanged brief produces the same identities and the
    de-duplication below removes them. Applying twice cannot double the
    requirements -- a property worth having, because a revision loop will call
    this on every pass.

    Raises :class:`SemanticCollisionError` when recognised phrases contradict
    each other. The alternative -- silently keeping one and dropping the other --
    is the exact failure this plane exists to prevent.
    """
    resolution = resolve_semantics(work)
    resolution.assert_no_collision()

    brief = work.brief
    merged = tuple(dict.fromkeys((*work.constraints, *resolution.constraints)))
    sealed = work.model_copy(
        update={
            "constraints": merged,
            "brief": brief.model_copy(
                update={
                    "unresolved_intents": tuple(
                        dict.fromkeys((*brief.unresolved_intents, *resolution.unresolved))
                    )
                }
            ),
        }
    ).seal()

    # Identity is content, so de-duplicating by sealed id is exact: the same
    # phrase re-derived yields the same constraint identity.
    unique: dict[str, Constraint] = {}
    for constraint in sealed.constraints:
        unique.setdefault(constraint.constraint_id, constraint)
    deduped = sealed.model_copy(update={"constraints": tuple(unique.values())})
    return deduped.seal()
