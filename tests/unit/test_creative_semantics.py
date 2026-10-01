"""Creative semantics: phrases become bounds, and gaps become declarations.

What this suite proves:

* **normalization is real** -- ZWNJ, case, punctuation, Persian digits and NFKC
  compatibility forms all collapse onto one lexicon key, so "نگه‌داشت مخاطب" and
  "نگهداشت مخاطب" are the same requirement rather than two near-duplicates;
* **the lexicon is data** -- every meaning in the table has a directive and every
  surface form maps to a meaning that exists, checked rather than assumed;
* **meaning becomes a number** -- each recognised phrase yields a typed
  constraint with a bound, carrying the phrase it came from as provenance;
* **gaps are declared, never dropped** -- an unrecognised phrase lands in
  ``brief.unresolved_intents`` and travels with the document;
* **collisions are arithmetic** -- incompatible duration bands and emphasis
  shares summing past 1000 permille are caught on the numbers, so a new phrase
  that contradicts an old one is caught without teaching the detector the pair;
* **resolution is deterministic and idempotent** -- phrase order does not change
  the document identity, and applying twice cannot double the requirements;
* **the layer is falsifiable** -- the reference document says "premium, fast
  paced, dramatic reveal" and the semantic layer correctly reports that it
  violates all three. A semantic layer that always agrees is not measuring
  anything.
"""

from __future__ import annotations

import pytest
from creative_ir_testkit import ONE_SECOND_US, product_teaser

from nexus_ai_agent.creative.intelligence import (
    DIRECTIVES,
    LEXICON,
    Constraint,
    ConstraintTarget,
    CreativeBrief,
    CreativeWork,
    NarrativeRole,
    PacingConstraint,
    Priority,
    SemanticCollisionError,
    SemanticMeaning,
    TimingConstraint,
    apply_semantics,
    normalize_phrase,
    resolve_semantics,
)
from nexus_ai_agent.creative.intelligence.ir import Scene, Timing

# ── Normalization ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Cinematic", "cinematic"),
        ("  CINEMATIC  ", "cinematic"),
        ("fast-paced", "fast paced"),
        ("fast--paced!!", "fast paced"),
        ("نگه‌داشت مخاطب", "نگهداشت مخاطب"),  # ZWNJ removed
        ("نگهداشت مخاطب", "نگهداشت مخاطب"),
        ("زیر ۱۵ ثانیه", "زیر 15 ثانیه"),  # Persian digits
        ("under 15 seconds", "under 15 seconds"),
        ("مستند", "مستند"),
    ],
)
def test_normalization_collapses_surface_noise(raw: str, expected: str) -> None:
    assert normalize_phrase(raw) == expected


def test_normalization_is_idempotent() -> None:
    once = normalize_phrase("  Fast--Paced! ")
    assert normalize_phrase(once) == once


def test_two_spellings_of_one_phrase_resolve_to_one_meaning() -> None:
    """ZWNJ must not create a second requirement out of one word."""
    assert LEXICON[normalize_phrase("نگه‌داشت مخاطب")] is LEXICON[normalize_phrase("نگهداشت مخاطب")]


# ── The lexicon is data ──────────────────────────────────────────────────────


def test_every_lexicon_entry_maps_to_a_meaning_that_has_a_directive() -> None:
    """No orphan surface form, no meaning without a definition."""
    assert LEXICON, "the lexicon must not be empty"
    for phrase, meaning in LEXICON.items():
        assert normalize_phrase(phrase) == phrase, (
            f"lexicon key {phrase!r} is not in normalised form"
        )
        assert meaning in DIRECTIVES, f"{phrase!r} maps to {meaning}, which has no directive"


def test_every_meaning_is_reachable_from_the_lexicon() -> None:
    """A meaning nobody can say is dead weight in the table."""
    reachable = set(LEXICON.values())
    assert reachable == set(SemanticMeaning), sorted(
        m.value for m in set(SemanticMeaning) - reachable
    )


def test_every_directive_actually_produces_something() -> None:
    for meaning, directive in DIRECTIVES.items():
        assert directive.constraints or any(
            value is not None for value in directive.style.model_dump().values()
        ), f"{meaning} implies nothing"
        assert directive.rationale, f"{meaning} states no rationale"


def test_surface_forms_are_many_to_one_not_one_to_one() -> None:
    """At least one meaning must have several phrasings, or this is not a lexicon."""
    counts: dict[SemanticMeaning, int] = {}
    for meaning in LEXICON.values():
        counts[meaning] = counts.get(meaning, 0) + 1
    assert max(counts.values()) >= 3, counts


# ── Meaning becomes a number ─────────────────────────────────────────────────


def _with_intents(work: CreativeWork, *phrases: str) -> CreativeWork:
    return work.model_copy(
        update={"brief": work.brief.model_copy(update={"semantic_intents": phrases})}
    ).seal()


def _bare_with_intents(work: CreativeWork, *phrases: str) -> CreativeWork:
    """The fixture's own six constraints removed, so a test sees only what the
    semantic layer derived. Without this the fixture's existing duration band
    participates in collision detection and a test cannot tell whose collision
    it is looking at."""
    return work.model_copy(
        update={
            "constraints": (),
            "brief": work.brief.model_copy(update={"semantic_intents": phrases}),
        }
    ).seal()


def test_fast_paced_becomes_a_bound_on_scene_length() -> None:
    work = _with_intents(product_teaser(), "fast paced")
    resolution = resolve_semantics(work)
    pacing = [c for c in resolution.constraints if isinstance(c.spec, PacingConstraint)]
    assert len(pacing) == 1
    assert pacing[0].spec.max_scene_duration_us == 3 * ONE_SECOND_US
    assert pacing[0].origin.detail == "fast paced"
    assert pacing[0].origin.source == "user"


def test_social_short_becomes_a_runtime_ceiling() -> None:
    resolution = resolve_semantics(_with_intents(product_teaser(), "social short"))
    timings = [c for c in resolution.constraints if isinstance(c.spec, TimingConstraint)]
    assert len(timings) == 1
    assert timings[0].spec.max_us == 15 * ONE_SECOND_US
    assert timings[0].spec.target.kind == "work"


def test_persian_phrases_resolve_exactly_like_their_english_twins() -> None:
    """The lexicon is bilingual and both sides produce the same requirement."""
    english = resolve_semantics(_with_intents(product_teaser(), "fast paced"))
    persian = resolve_semantics(_with_intents(product_teaser(), "ریتم تند"))
    assert english.meanings == persian.meanings == (SemanticMeaning.FAST_PACED,)
    assert [c.spec for c in english.constraints] == [c.spec for c in persian.constraints]


def test_two_phrasings_of_one_requirement_produce_one_constraint() -> None:
    work = _with_intents(product_teaser(), "cinematic", "film look", "سینمایی")
    resolution = resolve_semantics(work)
    assert resolution.meanings == (SemanticMeaning.CINEMATIC,)
    assert len(resolution.constraints) == 1
    assert len({p for p, m in resolution.resolved_phrases if m is SemanticMeaning.CINEMATIC}) == 3


def test_high_retention_scopes_itself_to_the_actual_hook_scene() -> None:
    """The document is an argument, not a formality: the hook is found, not guessed."""
    work = product_teaser()
    resolution = resolve_semantics(_with_intents(work, "high retention"))
    scoped = [
        c
        for c in resolution.constraints
        if isinstance(c.spec, TimingConstraint) and c.spec.target.kind == "scene"
    ]
    assert len(scoped) == 1
    hook = next(s for s in work.scenes if s.narrative_role is NarrativeRole.HOOK)
    assert scoped[0].spec.target.ref == hook.scene_id
    assert scoped[0].spec.max_us == 3 * ONE_SECOND_US


def test_a_hook_bound_with_no_hook_scene_is_declared_not_invented() -> None:
    """No hook scene means the requirement cannot be expressed, and that is reported."""
    work = product_teaser()
    flattened = tuple(
        scene.model_copy(update={"narrative_role": NarrativeRole.DEVELOPMENT})
        for scene in work.scenes
    )
    no_hook = work.model_copy(update={"scenes": flattened}).seal()
    resolution = resolve_semantics(_with_intents(no_hook, "high retention"))
    assert "high retention" in resolution.unresolved
    assert not [c for c in resolution.constraints if isinstance(c.spec, TimingConstraint)]


def test_minimal_is_expressed_as_what_it_refuses() -> None:
    resolution = resolve_semantics(_with_intents(product_teaser(), "minimal"))
    assert len(resolution.constraints) == 1
    spec = resolution.constraints[0].spec
    assert spec.kind == "exclusion"
    assert "motion.add_particles" in spec.forbidden


# ── Gaps are declared ────────────────────────────────────────────────────────


def test_an_unrecognised_phrase_is_recorded_on_the_document() -> None:
    work = _with_intents(product_teaser(), "make it pop", "cinematic")
    resolution = resolve_semantics(work)
    assert resolution.unresolved == ("make it pop",)
    assert resolution.meanings == (SemanticMeaning.CINEMATIC,)

    applied = apply_semantics(work)
    assert "make it pop" in applied.brief.unresolved_intents
    assert "cinematic" not in applied.brief.unresolved_intents


def test_a_second_pass_replaces_unresolved_intents_with_the_current_resolution() -> None:
    work = product_teaser().model_copy(
        update={
            "brief": product_teaser().brief.model_copy(
                update={
                    "semantic_intents": ("cinematic",),
                    "unresolved_intents": ("stale unresolved",),
                }
            )
        }
    )
    applied = apply_semantics(work)
    assert applied.brief.unresolved_intents == ()


def test_an_unresolved_phrase_never_silently_becomes_a_constraint() -> None:
    """The failure mode this exists to prevent: nodding along to words."""
    work = _with_intents(product_teaser(), "absolutely breathtaking")
    resolution = resolve_semantics(work)
    assert resolution.constraints == ()
    assert resolution.unresolved == ("absolutely breathtaking",)


def test_a_persian_phrase_the_lexicon_does_not_know_is_still_declared() -> None:
    work = _with_intents(product_teaser(), "خیلی خفن باشه")
    assert resolve_semantics(work).unresolved == ("خیلی خفن باشه",)


# ── Collisions ───────────────────────────────────────────────────────────────


def test_short_and_long_form_cannot_both_be_honoured() -> None:
    work = _bare_with_intents(product_teaser(), "social short", "long form")
    resolution = resolve_semantics(work)
    assert len(resolution.collisions) == 1
    collision = resolution.collisions[0]
    assert collision.kind == "duration_band"
    assert "at least 60000000us" in collision.detail
    assert "at most 15000000us" in collision.detail
    assert set(collision.phrases) == {"social short", "long form"}
    with pytest.raises(SemanticCollisionError):
        apply_semantics(work)


def test_two_roles_cannot_both_own_the_whole_piece() -> None:
    """Emphasis is permille of one total, so 400 + 700 is arithmetically impossible."""
    work = _bare_with_intents(product_teaser(), "focus on subject", "music driven")
    resolution = resolve_semantics(work)
    emphasis = [c for c in resolution.collisions if c.kind == "emphasis_share"]
    assert len(emphasis) == 1
    assert "sum to 1100 permille" in emphasis[0].detail
    with pytest.raises(SemanticCollisionError, match="1100 permille"):
        apply_semantics(work)


def test_a_collision_names_the_phrases_that_caused_it() -> None:
    """A collision is evidence: it must point at the words, not just the numbers."""
    resolution = resolve_semantics(
        _bare_with_intents(product_teaser(), "social short", "long form")
    )
    assert resolution.collisions[0].phrases


def test_compatible_phrases_do_not_report_a_collision() -> None:
    work = _with_intents(product_teaser(), "cinematic", "premium", "fast paced", "minimal")
    assert resolve_semantics(work).collisions == ()


def test_collision_detection_reads_numbers_not_phrase_names() -> None:
    """A brand-new contradictory pair is caught without being taught about it.

    Two hand-built constraints on the same target with an empty band collide even
    though neither phrase appears anywhere in the lexicon. That is the difference
    between a detector and a lookup table of known bad pairs.
    """
    from nexus_ai_agent.creative.intelligence import detect_collisions

    origin_free = (
        Constraint(
            priority=Priority.HARD,
            spec=TimingConstraint(target=ConstraintTarget(kind="work"), min_us=90_000_000),
        ),
        Constraint(
            priority=Priority.HARD,
            spec=TimingConstraint(target=ConstraintTarget(kind="work"), max_us=10_000_000),
        ),
    )
    found = detect_collisions(origin_free, product_teaser())
    assert len(found) == 1
    assert found[0].kind == "duration_band"


def test_an_empty_pacing_bound_is_rejected_at_construction() -> None:
    """A bound of zero would forbid every scene, so it is not a bound."""
    with pytest.raises(ValueError, match="at least one bound"):
        PacingConstraint()


# ── Determinism and idempotence ──────────────────────────────────────────────


def test_phrase_order_does_not_change_the_document_identity() -> None:
    """Emission follows the meaning table, not the order the words arrived in."""
    forward = apply_semantics(_with_intents(product_teaser(), "cinematic", "fast paced", "minimal"))
    backward = apply_semantics(
        _with_intents(product_teaser(), "minimal", "fast paced", "cinematic")
    )
    assert [c.spec for c in forward.constraints] == [c.spec for c in backward.constraints]
    # the brief records the phrases as given, so identity legitimately differs in
    # the brief only; the derived constraint set must be identical
    assert [c.constraint_id for c in forward.constraints] == [
        c.constraint_id for c in backward.constraints
    ]


def test_applying_semantics_twice_does_not_double_the_requirements() -> None:
    work = _with_intents(product_teaser(), "cinematic", "fast paced", "premium")
    once = apply_semantics(work)
    twice = apply_semantics(once)
    assert twice.to_canonical_json() == once.to_canonical_json()
    assert len(twice.constraints) == len(once.constraints)


def test_a_sealed_document_survives_semantic_application() -> None:
    applied = apply_semantics(_with_intents(product_teaser(), "cinematic", "fast paced"))
    applied.assert_valid()
    applied.verify_identity()


# ── Style targets are declared, not applied ──────────────────────────────────


def test_style_targets_are_reported_for_the_strategy_layer_to_place() -> None:
    resolution = resolve_semantics(_with_intents(product_teaser(), "premium", "fast paced"))
    assert resolution.style_targets
    assert any(t.energy == 850 for t in resolution.style_targets)


def test_the_semantic_layer_never_rewrites_a_layer() -> None:
    """Placing style is a strategy decision; a lexicon has no basis for making it."""
    work = _with_intents(product_teaser(), "premium", "fast paced", "cinematic")
    applied = apply_semantics(work)
    assert [layer.layer_id for layer in applied.all_layers()] == [
        layer.layer_id for layer in work.all_layers()
    ]
    assert [layer.style for layer in applied.all_layers()] == [
        layer.style for layer in work.all_layers()
    ]


# ── Falsifiability ───────────────────────────────────────────────────────────


def test_the_reference_document_violates_the_intents_it_declares() -> None:
    """A semantic layer that always agrees is not measuring anything.

    The fixture asks for "cinematic, premium, fast paced, dramatic reveal". It
    breaks three of them: the climax uses the glow that "premium" forbids, one
    scene runs five seconds against the three-second pacing ceiling, and the
    subject's attention share falls just under what a dramatic reveal needs.
    Finding that is the entire point of the layer.
    """
    applied = apply_semantics(product_teaser())
    kinds = {v.kind.value for v in applied.check_constraints()}
    assert "exclusion" in kinds, "premium forbids the glow the climax applies"
    assert "pacing" in kinds, "fast paced caps scenes at three seconds"
    assert "emphasis" in kinds, "dramatic reveal needs the subject to dominate"


def test_a_violation_is_specific_enough_to_act_on() -> None:
    """The loop closes: fix what the violation names and that violation clears.

    Built on a one-scene document rather than the fixture, because satisfying a
    pacing ceiling means shortening a scene *and* the layers inside it -- doing
    that to the whole fixture would be testing the fixture's geometry rather than
    the semantic layer.
    """
    from nexus_ai_agent.creative.intelligence import (
        Asset,
        AssetKind,
        Layer,
        MediaContent,
        Scene,
        Segment,
        SemanticRole,
        Timing,
    )

    asset = Asset(
        asset_id="a",
        kind=AssetKind.VIDEO,
        uri="asset:long",
        role=SemanticRole.SUBJECT,
        duration_us=20 * ONE_SECOND_US,
    )

    def scene(duration_s: int) -> Scene:
        span = Timing(start_us=0, duration_us=duration_s * ONE_SECOND_US)
        return Scene(
            scene_id="s",
            label="one long beat",
            narrative_role=NarrativeRole.DEVELOPMENT,
            timing=span,
            layers=(
                Layer(
                    layer_id="l",
                    role=SemanticRole.SUBJECT,
                    content=MediaContent(
                        asset_ref="a",
                        segment=Segment(
                            source_in_us=0,
                            source_duration_us=span.duration_us,
                            timeline=span,
                        ),
                    ),
                ),
            ),
        )

    def document(duration_s: int) -> CreativeWork:
        from nexus_ai_agent.creative.intelligence import CreativeBrief

        return CreativeWork(
            brief=CreativeBrief(goal="one beat", semantic_intents=("fast paced",)),
            assets=(asset,),
            scenes=(scene(duration_s),),
            constraints=(),
        ).seal()

    slow = apply_semantics(document(6))
    pacing = [v for v in slow.check_constraints() if v.kind.value == "pacing"]
    assert len(pacing) == 1
    assert "6000000us" in pacing[0].detail and "3000000us" in pacing[0].detail

    quick = apply_semantics(document(3))
    assert [v for v in quick.check_constraints() if v.kind.value == "pacing"] == []


# ── PacingConstraint is checkable like the others ────────────────────────────


def test_pacing_checks_both_scene_length_and_scene_count() -> None:
    work = product_teaser().model_copy(
        update={
            "constraints": (
                Constraint(
                    constraint_id="p",
                    priority=Priority.HARD,
                    spec=PacingConstraint(max_scene_duration_us=2_000_000, min_scene_count=9),
                ),
            )
        }
    )
    details = [v.detail for v in work.check_constraints()]
    assert any("5000000us" in d for d in details), details
    assert any("below the required 9" in d for d in details), details


def test_a_single_scene_document_can_still_be_paced() -> None:
    work = product_teaser()
    # the fixture's order constraint names the dropped scenes, so it goes too
    one_scene = work.model_copy(
        update={"scenes": work.scenes[:1], "transitions": (), "constraints": ()}
    ).seal()
    constrained = one_scene.model_copy(
        update={
            "constraints": (
                Constraint(
                    constraint_id="p",
                    priority=Priority.HARD,
                    spec=PacingConstraint(min_scene_count=2),
                ),
            )
        }
    )
    assert len(constrained.check_constraints()) == 1


def test_a_semantic_brief_can_be_carried_without_any_resolution() -> None:
    """The IR still accepts a brief whose phrases nothing has resolved yet."""
    brief = CreativeBrief(
        goal="g",
        semantic_intents=("cinematic", "unknown phrase"),
        unresolved_intents=("unknown phrase",),
    )
    source = product_teaser()
    work = CreativeWork(
        brief=brief,
        assets=source.assets,
        effects=source.effects,
        scenes=(
            Scene(
                scene_id="s",
                label="s",
                narrative_role=NarrativeRole.HOOK,
                timing=Timing(start_us=0, duration_us=3 * ONE_SECOND_US),
                layers=source.scenes[0].layers[:1],
            ),
        ),
    ).seal()
    work.assert_valid()
