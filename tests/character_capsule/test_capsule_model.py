"""Capsule model: immutable identity, mutable revisions, wardrobe timeline, canon.

The point of the model is that *some* edits are impossible by construction. These
tests try the forbidden edits and require a typed refusal rather than silent
mutation — a capsule that can be quietly rewritten is not an identity record.
"""

# ruff: noqa: E402 - _support.ensure_path() must run before these imports
from __future__ import annotations

import _support

_support.ensure_path()

import pytest

from experiments.character_capsule import dataset
from experiments.character_capsule.canon import CanonEngine, CanonRuleViolation, FactKind
from experiments.character_capsule.errors import ValidationError
from experiments.character_capsule.model import (
    SCOPE_CAPSULE,
    ChangeIntent,
    ScopeViolation,
)
from experiments.character_capsule.scene import ShotReference  # noqa: F401  (typing context)
from experiments.character_capsule.wardrobe import (
    WardrobeChangeEvent,
    check_wardrobe,
    expected_outfit,
    validate_outfit_era,
)


@pytest.fixture(scope="module")
def capsule():
    return dataset.build_capsule(dataset.CHARACTERS[0])


def test_capsule_seals_and_verifies(capsule):
    assert capsule.digest.startswith("sha256:")
    capsule.verify()
    assert set(capsule.module_digests()) == {
        "face",
        "body",
        "hair",
        "motion",
        "voice",
        "wardrobe",
        "canon",
    }


def test_digest_changes_when_anything_changes(capsule):
    wardrobe = capsule.module("wardrobe")
    updated = capsule.with_module(
        wardrobe.with_sequence_baseline("SEQ-EP2-A", "OUT-0001"), note="baseline swap"
    )
    assert updated.revision_no == capsule.revision_no + 1
    assert updated.digest != capsule.digest
    assert updated.identity_version == capsule.identity_version


def test_identity_fields_cannot_be_revised(capsule):
    with pytest.raises(ScopeViolation):
        capsule.apply_changes([ChangeIntent(field_path="face.jaw_width_ratio", value=0.31)])
    with pytest.raises(ScopeViolation):
        capsule.apply_changes([ChangeIntent(field_path="body.shoulder_width_ratio", value=0.5)])
    # REVISION-scoped face fields are allowed to move
    revised = capsule.apply_changes([ChangeIntent(field_path="face.freckles", value=True)])
    assert revised.module("face").freckles is True


def test_mutable_fields_can_be_revised(capsule):
    revised = capsule.apply_changes(
        [ChangeIntent(field_path="voice.speaking_rate", value=0.97)],
        expected_revision=capsule.revision_no,
    )
    assert revised.revision_no == capsule.revision_no + 1
    assert revised.module("voice").speaking_rate == pytest.approx(0.97)


def test_revision_conflict_is_refused(capsule):
    from experiments.character_capsule.errors import RevisionConflict

    with pytest.raises(RevisionConflict):
        capsule.apply_changes(
            [ChangeIntent(field_path="voice.speaking_rate", value=1.02)],
            expected_revision=capsule.revision_no + 5,
        )


def test_new_identity_version_is_the_only_door_for_the_face(capsule):
    minted = capsule.new_identity_version(face_changes={"jaw_width_ratio": 0.31})
    assert minted.identity_version == capsule.identity_version + 1
    assert minted.module("face").jaw_width_ratio == pytest.approx(0.31)
    assert minted.digest != capsule.digest


def test_wardrobe_plan_is_ordered(capsule):
    wardrobe = capsule.module("wardrobe")
    assert expected_outfit(wardrobe, sequence_id="SEQ-EP1-A", shot_index=0) == "OUT-0001"
    assert expected_outfit(wardrobe, sequence_id="SEQ-EP1-A", shot_index=3) == "OUT-0002"
    finding = check_wardrobe(
        wardrobe, sequence_id="SEQ-EP1-A", shot_index=0, actual_outfit_id="OUT-0002"
    )
    assert finding is not None
    assert finding.code == "WARDROBE_DRIFT"


def test_outfit_era_is_enforced(capsule):
    wardrobe = capsule.module("wardrobe")
    with pytest.raises(ValidationError):
        validate_outfit_era(wardrobe, outfit_id="OUT-0002", scene_order=2)
    validate_outfit_era(wardrobe, outfit_id="OUT-0002", scene_order=3)


def test_unknown_outfit_is_refused():
    empty = dataset.CHARACTERS[0]
    wardrobe = dataset.build_capsule(empty).module("wardrobe")
    with pytest.raises(ValidationError):
        validate_outfit_era(wardrobe, outfit_id="OUT-9999", scene_order=1)


def test_wardrobe_change_event_must_reference_a_known_outfit(capsule):
    wardrobe = capsule.module("wardrobe")
    with pytest.raises(ValidationError):
        wardrobe.with_change_event(
            WardrobeChangeEvent(
                event_id="WCE-9999",
                sequence_id="SEQ-EP1-A",
                from_shot_index=2,
                outfit_id="OUT-9999",
                reason="unknown costume",
            )
        )


def test_immutable_canon_fact_cannot_be_edited(capsule):
    canon = capsule.module("canon")
    fact = next(iter(canon.facts.values()))
    assert fact.kind is FactKind.IMMUTABLE
    with pytest.raises(CanonRuleViolation):
        canon.with_fact(
            type(fact)(
                fact_id=fact.fact_id,
                predicate=fact.predicate,
                value="something else",
                kind=FactKind.IMMUTABLE,
            )
        )


def test_canon_adds_new_facts_but_not_rewrites(capsule):
    canon = capsule.module("canon")
    addition = canon.with_fact(
        type(next(iter(canon.facts.values())))(
            fact_id="FACT-010",
            predicate="languages",
            value="coastal dialect",
            kind=FactKind.IMMUTABLE,
        )
    )
    assert "FACT-010" in addition.facts
    assert addition.digest != canon.digest


def test_story_effect_persists_for_later_scenes(capsule):
    engine = CanonEngine(capsule)
    later = engine.check_shot(shot_id="SH-1", scene_order=4, assertions={"scar": "none"})
    assert "STORY_EVENT_NOT_HONOURED" in {conflict.code for conflict in later}
    earlier = engine.check_shot(shot_id="SH-2", scene_order=1, assertions={"scar": "none"})
    assert earlier == ()


def test_death_is_terminal(capsule):
    engine = CanonEngine(capsule)
    after_death = engine.check_shot(shot_id="SH-3", scene_order=12, assertions={})
    assert "POST_DEATH_PRESENCE" in {conflict.code for conflict in after_death}


def test_outfit_must_exist_in_the_timeline():
    wardrobe = dataset.build_capsule(dataset.CHARACTERS[0]).module("wardrobe")
    assert {outfit.outfit_id for outfit in wardrobe.outfits.values()} == {"OUT-0001", "OUT-0002"}
    # a baseline that names an outfit the wardrobe does not contain is refused at
    # construction: the plan can never point at a costume the character never had
    with pytest.raises(ValidationError):
        type(wardrobe)(
            revision=wardrobe.revision + 1,
            outfits=dict(wardrobe.outfits),
            sequence_baselines={"SEQ-EP9-A": "OUT-0404"},
            change_events=wardrobe.change_events,
            scene_rules=dict(wardrobe.scene_rules),
        )


def test_capsule_scope_prefix_is_versioned(capsule):
    from experiments.character_capsule.canonical import scoped_digest

    assert capsule.digest == scoped_digest(SCOPE_CAPSULE, capsule.payload())
