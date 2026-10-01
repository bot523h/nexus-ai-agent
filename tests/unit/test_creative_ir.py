"""The Typed Creative IR: structure, invalid states, constraints, round-trip.

What this suite proves, and why each group exists:

* **the IR is strongly typed** -- unknown fields, wrong units and incoherent
  combinations are rejected at construction, not discovered downstream;
* **invalid states are unreachable** -- dangling references, duplicate
  identities, escaped layers, over-read segments and inconsistent layouts are
  all reported by ``assert_valid()``, with *every* problem at once;
* **constraints are predicates, not metadata** -- each of the five kinds is
  shown holding on a document that satisfies it and failing on one that does
  not. A constraint that cannot fail is not a constraint;
* **the document round-trips** -- canonical JSON survives a parse and reproduces
  the same root identity, which is what makes the IR storable and diffable;
* **identity is self-verifying** -- a hand-edited id is caught, which is the
  property that keeps a compiler from acting on a document that lies about
  itself.
"""

from __future__ import annotations

import pytest
from creative_ir_testkit import ONE_SECOND_US, product_teaser
from pydantic import ValidationError

from nexus_ai_agent.creative.intelligence import (
    IR_VERSION,
    Asset,
    AssetKind,
    Constraint,
    ConstraintTarget,
    ConstraintViolationError,
    CreativeBrief,
    CreativeWork,
    DanglingReferenceError,
    Effect,
    EffectFamily,
    EmphasisConstraint,
    ExclusionConstraint,
    IdentityError,
    IRValidationError,
    Layer,
    MediaContent,
    NarrativeRole,
    OrderConstraint,
    Origin,
    Priority,
    QualityConstraint,
    Scene,
    Segment,
    SemanticRole,
    TextContent,
    TextSpec,
    Timing,
    TimingConstraint,
    TimingError,
    Track,
    Transition,
    TransitionKind,
    Violation,
)

# ── The IR is strongly typed ─────────────────────────────────────────────────


def test_the_reference_document_is_valid_and_satisfies_every_constraint() -> None:
    """The baseline: a realistic document passes every gate the IR has."""
    work = product_teaser()
    work.assert_valid()
    work.verify_identity()
    assert work.check_constraints() == ()
    work.assert_constraints()


def test_unknown_fields_are_rejected_at_construction() -> None:
    """``extra='forbid'`` everywhere: a typo is an error, not a silent drop."""
    with pytest.raises(ValidationError):
        Timing.model_validate({"start_us": 0, "duration_us": 1, "start_ms": 0})


def test_models_are_frozen() -> None:
    """The IR is immutable, so a compiled plan cannot be mutated underneath it."""
    work = product_teaser()
    with pytest.raises(ValidationError):
        work.brief.goal = "changed"  # type: ignore[misc]


def test_negative_and_zero_durations_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Timing(start_us=0, duration_us=0)
    with pytest.raises(ValidationError):
        Timing(start_us=-1, duration_us=10)


def test_permille_axes_cannot_leave_their_band() -> None:
    with pytest.raises(ValidationError):
        product_teaser().scenes[0].layers[0].style.model_copy(
            update={"energy": 1001}
        ).model_validate({"energy": 1001, "warmth": 500, "density": 500, "motion": 500})


def test_an_effect_parameter_carries_exactly_one_value() -> None:
    from nexus_ai_agent.creative.intelligence import EffectParam

    with pytest.raises(ValidationError):
        EffectParam(name="radius")
    with pytest.raises(ValidationError):
        EffectParam(name="radius", value_milli=1, value_text="x")


def test_effect_parameters_must_be_declared_in_name_order() -> None:
    """Identity must not depend on the order a caller happened to write params."""
    from nexus_ai_agent.creative.intelligence import EffectParam

    with pytest.raises(ValidationError, match="name order"):
        Effect(
            family=EffectFamily.COLOR,
            operation="color.adjust_exposure",
            params=(
                EffectParam(name="zeta", value_milli=1),
                EffectParam(name="alpha", value_milli=2),
            ),
        )


def test_a_segment_cannot_retime_without_declaring_a_speed() -> None:
    """An implied rate is a rate the compiler would have to guess."""
    with pytest.raises(TimingError, match="speed_milli"):
        Segment(
            source_in_us=0,
            source_duration_us=4 * ONE_SECOND_US,
            timeline=Timing(start_us=0, duration_us=2 * ONE_SECOND_US),
        )
    # ...but saying so explicitly is legal.
    ok = Segment(
        source_in_us=0,
        source_duration_us=4 * ONE_SECOND_US,
        timeline=Timing(start_us=0, duration_us=2 * ONE_SECOND_US),
        speed_milli=2000,
    )
    assert ok.speed_milli == 2000


def test_a_cut_is_instantaneous_and_a_dissolve_is_not() -> None:
    with pytest.raises(ValidationError, match="instantaneous"):
        Transition(kind=TransitionKind.CUT, duration_us=1, from_scene_ref="a", to_scene_ref="b")
    with pytest.raises(ValidationError, match="positive duration"):
        Transition(kind=TransitionKind.CROSSFADE, from_scene_ref="a", to_scene_ref="b")


def test_a_transition_cannot_join_a_scene_to_itself() -> None:
    with pytest.raises(ValidationError, match="distinct scenes"):
        Transition(kind=TransitionKind.CUT, from_scene_ref="same", to_scene_ref="same")


def test_a_voiceover_layer_must_be_media_backed() -> None:
    """Synthesized text has no waveform; the IR refuses the contradiction."""
    with pytest.raises(ValidationError, match="media-backed"):
        Layer(
            role=SemanticRole.VOICEOVER,
            content=TextContent(
                text=TextSpec(text="hello"), timeline=Timing(start_us=0, duration_us=1)
            ),
        )


def test_constraint_scopes_are_coherent() -> None:
    with pytest.raises(ValidationError, match="scene ref"):
        ConstraintTarget(kind="scene")
    with pytest.raises(ValidationError, match="needs a role"):
        ConstraintTarget(kind="role")
    with pytest.raises(ValidationError, match="neither ref nor role"):
        ConstraintTarget(kind="work", ref="x")


def test_a_timing_constraint_cannot_be_scoped_to_a_role() -> None:
    """A constraint that could never be checked is worse than none at all."""
    with pytest.raises(ValidationError, match="work or to a scene"):
        TimingConstraint(target=ConstraintTarget(kind="role", role=SemanticRole.MUSIC), max_us=1000)


def test_an_emphasis_constraint_needs_a_subset_to_measure() -> None:
    with pytest.raises(ValidationError, match="role or a scene"):
        EmphasisConstraint(target=ConstraintTarget(kind="work"), min_share_permille=100)


def test_unresolved_intents_must_have_been_declared() -> None:
    """An unresolved phrase is a declared gap, never a silent disappearance."""
    with pytest.raises(ValidationError, match="never declared"):
        CreativeBrief(goal="g", semantic_intents=("cinematic",), unresolved_intents=("premium",))


# ── Invalid states are unreachable ───────────────────────────────────────────


def test_validate_reports_every_problem_at_once() -> None:
    """One actionable list, not one rebuild per discovery.

    Runs on an *unsealed* document, which is the real authoring flow: a strategy
    layer builds a document, validates it, then seals it.
    """
    work = product_teaser()
    scene = work.scenes[0]
    bad_layer = scene.layers[0].model_copy(
        update={
            "content": MediaContent(
                asset_ref="no-such-asset",
                segment=Segment(
                    source_in_us=0,
                    source_duration_us=3 * ONE_SECOND_US,
                    timeline=Timing(start_us=0, duration_us=3 * ONE_SECOND_US),
                ),
            ),
            "effects": ("no-such-effect",),
        }
    )
    broken = work.model_copy(
        update={
            "scenes": (
                scene.model_copy(update={"layers": (bad_layer, *scene.layers[1:])}),
                *work.scenes[1:],
            )
        }
    )
    with pytest.raises(IRValidationError) as exc:
        broken.assert_valid()
    problems = exc.value.problems
    assert len(problems) >= 2, problems
    assert any("unknown asset no-such-asset" in p for p in problems)
    assert any("unknown effect no-such-effect" in p for p in problems)


def test_seal_is_the_stricter_gate_and_fails_fast_on_a_dangling_reference() -> None:
    """The two gates have different contracts, and both are reachable.

    ``assert_valid()`` reports structure (and tolerates provisional identities, so it
    can run before sealing); ``seal()`` resolves references while assigning
    identity and therefore cannot proceed past an unresolved one. Neither
    silently accepts the document.
    """
    work = product_teaser()
    dropped = work.model_copy(update={"assets": work.assets[1:]})
    with pytest.raises(DanglingReferenceError, match="does not declare"):
        dropped.seal()
    with pytest.raises(IRValidationError, match="unknown asset"):
        dropped.assert_valid()


def test_a_dangling_scene_reference_is_rejected_on_seal() -> None:
    work = product_teaser()
    with pytest.raises(DanglingReferenceError, match="does not declare"):
        work.model_copy(update={"scenes": work.scenes[:2]}).seal()


def test_a_dangling_constraint_scope_is_a_structural_fault_not_a_violation() -> None:
    """The two failure modes stay apart: validate() owns structure, not checks.

    A constraint scoped to a scene that is not there is a malformed document, not
    a broken promise. Reporting it as a violation would blur exactly the
    distinction the error hierarchy exists to draw.
    """
    work = product_teaser()
    ghost = Constraint(
        constraint_id="c-ghost",
        spec=TimingConstraint(
            target=ConstraintTarget(kind="scene", ref="scene-that-is-not-there"), max_us=1
        ),
    )
    broken = work.model_copy(update={"constraints": (ghost,)})
    assert broken.check_constraints() == ()
    with pytest.raises(IRValidationError, match="unknown scene"):
        broken.assert_valid()


def test_duplicate_identities_are_reported() -> None:
    """One identity namespace across every element kind.

    A flat namespace is deliberate: an id that means two different things makes
    every log line, trace and compiled reference ambiguous. It also catches the
    easy builder mistake of reusing a scratch name across kinds.
    """
    work = product_teaser()
    doubled = work.model_copy(update={"assets": (work.assets[0], work.assets[0])})
    with pytest.raises(IRValidationError, match="collides"):
        doubled.assert_valid()

    # the same name on two *different* kinds is a collision too
    scene = work.scenes[0]
    squatted = work.model_copy(
        update={
            "assets": (
                work.assets[0].model_copy(update={"asset_id": scene.scene_id}),
                *work.assets[1:],
            )
        }
    )
    with pytest.raises(IRValidationError) as exc:
        squatted.assert_valid()
    # the *scene* is what reports it, because assets are enumerated first
    assert any(
        problem.startswith("scene identity") and "collides with asset identity" in problem
        for problem in exc.value.problems
    ), exc.value.problems


def test_an_unsealed_document_fails_validation() -> None:
    """Forgetting to seal is loud, not a document full of empty ids."""
    work = product_teaser(sealed=False)
    with pytest.raises(IRValidationError, match="empty"):
        work.assert_valid()


def test_a_layer_cannot_escape_its_scene() -> None:
    work = product_teaser()
    scene = work.scenes[0]
    runaway = scene.layers[0].model_copy(
        update={
            "content": MediaContent(
                asset_ref=scene.layers[0].content.asset_ref,  # type: ignore[attr-defined]
                segment=Segment(
                    source_in_us=0,
                    source_duration_us=9 * ONE_SECOND_US,
                    timeline=Timing(start_us=0, duration_us=9 * ONE_SECOND_US),
                ),
            )
        }
    )
    broken = work.model_copy(
        update={"scenes": (scene.model_copy(update={"layers": (runaway,)}), *work.scenes[1:])}
    ).seal()
    with pytest.raises(IRValidationError, match="escapes"):
        broken.assert_valid()


def test_a_layer_cannot_read_past_the_end_of_its_source() -> None:
    work = product_teaser()
    short = work.assets[0].model_copy(update={"duration_us": 2 * ONE_SECOND_US})
    with pytest.raises(IRValidationError, match="only 2000000us long"):
        work.model_copy(update={"assets": (short, *work.assets[1:])}).seal().assert_valid()


def test_an_unknown_duration_skips_the_over_read_rule() -> None:
    """``duration_us == 0`` means 'unknown', and the IR refuses to invent a bound."""
    work = product_teaser()
    unknown = work.assets[0].model_copy(update={"duration_us": 0})
    work.model_copy(update={"assets": (unknown, *work.assets[1:])}).seal().assert_valid()


def test_overlapping_scenes_are_reported() -> None:
    work = product_teaser()
    stretched = work.scenes[0].model_copy(
        update={"timing": Timing(start_us=0, duration_us=6 * ONE_SECOND_US)}
    )
    with pytest.raises(IRValidationError, match="overlaps scene"):
        work.model_copy(update={"scenes": (stretched, *work.scenes[1:])}).seal().assert_valid()


def test_a_backwards_transition_is_reported() -> None:
    work = product_teaser()
    flipped = work.transitions[0].model_copy(
        update={
            "from_scene_ref": work.transitions[0].to_scene_ref,
            "to_scene_ref": work.transitions[0].from_scene_ref,
        }
    )
    flipped_doc = work.model_copy(update={"transitions": (flipped, *work.transitions[1:])}).seal()
    with pytest.raises(IRValidationError, match="runs backwards"):
        flipped_doc.assert_valid()


def test_a_piece_longer_than_its_own_ceiling_is_reported() -> None:
    work = product_teaser()
    with pytest.raises(IRValidationError, match="max_duration_us"):
        work.model_copy(
            update={"output": work.output.model_copy(update={"max_duration_us": 10_000_000})}
        ).assert_valid()


def test_a_quality_duration_cap_is_checked_as_a_constraint_not_a_structural_error() -> None:
    work = product_teaser()
    capped = work.model_copy(
        update={
            "constraints": (
                *work.constraints,
                Constraint(
                    constraint_id="c-quality-cap",
                    priority=Priority.HARD,
                    spec=QualityConstraint(max_duration_us=10_000_000),
                    origin=Origin(source="user", detail="under ten seconds if possible"),
                ),
            )
        }
    ).seal()
    capped.assert_valid()
    violations = capped.check_constraints()
    assert any(v.constraint_id == capped.constraints[-1].constraint_id for v in violations)


def test_an_incomplete_layout_is_reported() -> None:
    """A layout is all-or-nothing: partial track assignment is a defect."""
    work = product_teaser()
    first_layer = work.scenes[0].layers[0]
    partial = work.model_copy(
        update={"layout": (Track(track_id="v0", kind="video", layer_ids=(first_layer.layer_id,)),)}
    )
    with pytest.raises(IRValidationError, match="not on any track"):
        partial.assert_valid()


def test_a_layer_on_two_tracks_is_reported() -> None:
    work = product_teaser()
    first = work.scenes[0].layers[0].layer_id
    with pytest.raises(IRValidationError, match="both track"):
        work.model_copy(
            update={
                "layout": (
                    Track(track_id="v0", kind="video", layer_ids=(first,)),
                    Track(track_id="v1", kind="video", layer_ids=(first,)),
                )
            }
        ).assert_valid()


def test_a_layout_must_agree_with_the_layer_track_it_claims() -> None:
    work = product_teaser()
    layer_ids = tuple(layer.layer_id for layer in work.all_layers())
    claimed = work.scenes[0].layers[0].model_copy(update={"track_id": "v9"})
    relaid = work.model_copy(
        update={
            "scenes": (
                work.scenes[0].model_copy(update={"layers": (claimed, *work.scenes[0].layers[1:])}),
                *work.scenes[1:],
            ),
            "layout": (Track(track_id="v0", kind="video", layer_ids=layer_ids),),
        }
    ).seal()
    with pytest.raises(IRValidationError, match="claims track"):
        relaid.assert_valid()


def test_a_layer_placed_on_an_unknown_track_is_reported() -> None:
    work = product_teaser()
    with pytest.raises(IRValidationError, match="unknown layer"):
        work.model_copy(
            update={"layout": (Track(track_id="v0", kind="video", layer_ids=("nope",)),)}
        ).assert_valid()


def test_the_plane_does_not_shadow_the_pydantic_api() -> None:
    """``CreativeWork.validate`` must stay pydantic's classmethod.

    ``BaseModel.validate`` is a classmethod with a different signature. An
    instance method of the same name on a model would break Liskov substitution
    for anyone reaching for the pydantic API, so the plane's structural gate is
    named ``assert_valid`` instead. This test is what keeps it that way.
    """
    assert isinstance(
        CreativeWork.__dict__.get("validate"), (classmethod, staticmethod, type(None))
    )
    assert callable(CreativeWork.assert_valid)
    assert "assert_valid" in CreativeWork.__dict__


# ── Identity ─────────────────────────────────────────────────────────────────


def test_identity_is_derived_from_content_not_from_a_counter() -> None:
    work = product_teaser()
    assert work.work_id.startswith("wrk_")
    assert work.scenes[0].scene_id.startswith("scn_")
    assert work.scenes[0].layers[0].layer_id.startswith("lay_")
    assert work.assets[0].asset_id.startswith("ast_")


def test_a_hand_edited_identity_is_caught() -> None:
    """The self-verification property: a document cannot lie about itself."""
    work = product_teaser()
    lied = work.model_copy(
        update={
            "scenes": (work.scenes[0].model_copy(update={"label": "relabeled"}), *work.scenes[1:])
        }
    )
    with pytest.raises(IdentityError) as exc:
        lied.verify_identity()
    assert any("should be" in p for p in exc.value.problems)


def test_sealing_is_idempotent() -> None:
    work = product_teaser()
    assert work.seal().to_canonical_json() == work.to_canonical_json()


def test_the_ir_version_is_part_of_the_root_identity() -> None:
    """A v2 document must never hash to a v1 identity."""
    work = product_teaser()
    assert work.ir_version == IR_VERSION
    assert IR_VERSION in work.semantic_payload()["ir_version"]


# ── Constraints are predicates ───────────────────────────────────────────────


def _only(work: CreativeWork, constraint_id: str) -> Constraint:
    for constraint in work.constraints:
        if constraint.constraint_id == constraint_id:
            return constraint
    raise AssertionError(f"fixture has no constraint {constraint_id}")


def _with_only(work: CreativeWork, *constraints: Constraint) -> CreativeWork:
    return work.model_copy(update={"constraints": constraints}).seal()


def test_a_timing_constraint_holds_and_then_fails() -> None:
    work = product_teaser()
    tight = _with_only(
        work,
        Constraint(
            priority=Priority.HARD,
            spec=TimingConstraint(target=ConstraintTarget(kind="work"), max_us=10_000_000),
            origin=Origin(source="user", detail="too short a ceiling"),
        ),
    )
    violations = tight.check_constraints()
    assert len(violations) == 1
    assert violations[0].detail.startswith("work is 15000000us")


def test_a_scene_scoped_timing_constraint_targets_that_scene() -> None:
    work = product_teaser()
    hook = work.scenes[0]
    tight = _with_only(
        work,
        Constraint(
            priority=Priority.HARD,
            spec=TimingConstraint(
                target=ConstraintTarget(kind="scene", ref=hook.scene_id), max_us=2_000_000
            ),
            origin=Origin(source="strategy", detail="hook must be shorter"),
        ),
    )
    detail = tight.check_constraints()[0].detail
    assert "cold open on the product" in detail


def test_an_order_constraint_holds_and_then_fails() -> None:
    work = product_teaser()
    hook, cta = work.scenes[0].scene_id, work.scenes[3].scene_id
    forwards = _with_only(
        work,
        Constraint(
            spec=OrderConstraint(
                before=ConstraintTarget(kind="scene", ref=hook),
                after=ConstraintTarget(kind="scene", ref=cta),
            )
        ),
    )
    assert forwards.check_constraints() == ()
    backwards = _with_only(
        work,
        Constraint(
            spec=OrderConstraint(
                before=ConstraintTarget(kind="scene", ref=cta),
                after=ConstraintTarget(kind="scene", ref=hook),
            )
        ),
    )
    assert "must precede" in backwards.check_constraints()[0].detail


def test_an_exclusion_constraint_catches_a_forbidden_operation() -> None:
    work = product_teaser()
    forbidden = _with_only(
        work,
        Constraint(
            spec=ExclusionConstraint(
                target=ConstraintTarget(kind="work"), forbidden=("motion.add_glow",)
            ),
            origin=Origin(source="user", detail="no glow"),
        ),
    )
    assert "forbidden operation motion.add_glow" in forbidden.check_constraints()[0].detail


def test_an_exclusion_constraint_catches_a_forbidden_asset_kind() -> None:
    work = product_teaser()
    forbidden = _with_only(
        work,
        Constraint(
            spec=ExclusionConstraint(target=ConstraintTarget(kind="work"), forbidden=("font",)),
            origin=Origin(source="user", detail="no bundled font"),
        ),
    )
    assert "forbidden asset kind font" in forbidden.check_constraints()[0].detail


def test_an_exclusion_constraint_can_be_scoped_to_one_scene() -> None:
    """Scope matters: the glow is fine everywhere except the call to action."""
    work = product_teaser()
    climax = work.scenes[2]
    scoped = _with_only(
        work,
        Constraint(
            spec=ExclusionConstraint(
                target=ConstraintTarget(kind="scene", ref=climax.scene_id),
                forbidden=("motion.add_glow",),
            )
        ),
    )
    assert "forbidden operation motion.add_glow" in scoped.check_constraints()[0].detail
    hook_only = _with_only(
        work,
        Constraint(
            spec=ExclusionConstraint(
                target=ConstraintTarget(kind="scene", ref=work.scenes[0].scene_id),
                forbidden=("motion.add_glow",),
            )
        ),
    )
    assert hook_only.check_constraints() == ()


def test_an_emphasis_constraint_holds_and_then_fails() -> None:
    work = product_teaser()
    subject = ConstraintTarget(kind="role", role=SemanticRole.SUBJECT)
    assert (
        _with_only(
            work,
            Constraint(spec=EmphasisConstraint(target=subject, min_share_permille=250)),
        ).check_constraints()
        == ()
    )
    greedy = _with_only(
        work,
        Constraint(spec=EmphasisConstraint(target=subject, min_share_permille=900)),
    )
    detail = greedy.check_constraints()[0].detail
    assert "below the required 900 permille" in detail


def test_a_quality_constraint_checks_the_declared_output() -> None:
    work = product_teaser()
    demanding = _with_only(
        work,
        Constraint(
            spec=QualityConstraint(min_width_px=3840, min_height_px=2160),
            origin=Origin(source="user", detail="4K master"),
        ),
    )
    violations = demanding.check_constraints()
    assert len(violations) == 1
    assert "width 1920px" in violations[0].detail


def test_only_hard_violations_raise() -> None:
    """A soft constraint is a preference to trade away, not a reason to refuse."""
    work = product_teaser()
    soft = _with_only(
        work,
        Constraint(
            priority=Priority.SOFT,
            spec=QualityConstraint(min_width_px=3840, min_height_px=2160),
        ),
    )
    assert len(soft.check_constraints()) == 1
    soft.assert_constraints()  # must not raise
    hard = _with_only(
        work,
        Constraint(
            priority=Priority.HARD,
            spec=QualityConstraint(min_width_px=3840, min_height_px=2160),
        ),
    )
    with pytest.raises(ConstraintViolationError) as exc:
        hard.assert_constraints()
    assert exc.value.violations


def test_a_violation_names_its_constraint_and_kind() -> None:
    """A violation is evidence: it must point back at what was asked for."""
    work = product_teaser()
    constraint = Constraint(
        priority=Priority.HARD,
        spec=QualityConstraint(min_frame_rate_milli=60000),
        origin=Origin(source="user", detail="60fps please"),
    )
    checked = _with_only(work, constraint)
    violation = checked.check_constraints()[0]
    assert isinstance(violation, Violation)
    assert violation.constraint_id == checked.constraints[0].constraint_id
    assert violation.constraint_id  # seal() assigned a real content address
    assert violation.kind.value == "quality"
    assert violation.priority is Priority.HARD


# ── Serialization ────────────────────────────────────────────────────────────


def test_canonical_json_round_trips_to_an_equal_document() -> None:
    work = product_teaser()
    restored = CreativeWork.from_canonical_json(work.to_canonical_json())
    assert restored == work
    assert restored.work_id == work.work_id


def test_canonical_json_is_stable_across_rebuilds() -> None:
    assert product_teaser().to_canonical_json() == product_teaser().to_canonical_json()


def test_canonical_json_keeps_non_ascii_readable() -> None:
    """``ensure_ascii=False``: a Persian brief hashes as itself, not as escapes."""
    raw = product_teaser().to_canonical_json()
    assert "نکسوس" in raw
    assert "\\u" not in raw


def test_canonical_json_rejects_garbage() -> None:
    from nexus_ai_agent.creative.intelligence import SerializationError

    with pytest.raises(SerializationError, match="not parseable"):
        CreativeWork.from_canonical_json("{not json")
    with pytest.raises(SerializationError, match="must be an object"):
        CreativeWork.from_canonical_json("[1, 2, 3]")
    with pytest.raises(SerializationError, match="not a valid CreativeWork"):
        CreativeWork.from_canonical_json('{"goal": "x"}')


# ── Inspection ───────────────────────────────────────────────────────────────


def test_the_document_is_inspectable_by_role_and_duration() -> None:
    work = product_teaser()
    assert work.duration_us == 15 * ONE_SECOND_US
    assert len(work.all_layers()) == 12
    music = [layer for layer in work.all_layers() if layer.role is SemanticRole.MUSIC]
    assert len(music) == 4  # the bed runs under every scene
    assert all(layer.timeline().end_us <= work.duration_us for layer in work.all_layers())


def test_every_element_carries_its_provenance() -> None:
    """Explainability is data: each element can answer 'why are you here'."""
    work = product_teaser()
    assert work.brief.origin.source == "user"
    assert all(scene.origin.detail for scene in work.scenes)
    assert all(layer.origin.detail for layer in work.all_layers())
    assert all(constraint.origin.detail for constraint in work.constraints)


def test_a_minimal_document_is_still_a_document() -> None:
    """The IR does not force ceremony: one scene with one layer is valid."""
    asset = Asset(asset_id="a", kind=AssetKind.IMAGE, uri="asset:still", role=SemanticRole.SUBJECT)
    layer = Layer(
        layer_id="l",
        role=SemanticRole.SUBJECT,
        content=MediaContent(
            asset_ref="a",
            segment=Segment(
                source_in_us=0,
                source_duration_us=ONE_SECOND_US,
                timeline=Timing(start_us=0, duration_us=ONE_SECOND_US),
            ),
        ),
    )
    scene = Scene(
        scene_id="s",
        label="only",
        narrative_role=NarrativeRole.HOOK,
        timing=Timing(start_us=0, duration_us=ONE_SECOND_US),
        layers=(layer,),
    )
    work = CreativeWork(
        brief=CreativeBrief(goal="one still, one second"), assets=(asset,), scenes=(scene,)
    ).seal()
    work.assert_valid()
    work.verify_identity()
    assert work.duration_us == ONE_SECOND_US


def test_a_document_must_have_at_least_one_scene() -> None:
    with pytest.raises(ValidationError):
        CreativeWork(brief=CreativeBrief(goal="nothing"), scenes=())
