"""Revision contract tests for the Creative Intelligence Plane."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from creative_ir_testkit import ONE_SECOND_US, product_teaser

from nexus_ai_agent.creative.intelligence import (
    Constraint,
    ConstraintTarget,
    ConstraintTimingChange,
    CreativeWork,
    NarrativeRole,
    NodeTarget,
    Origin,
    Priority,
    RevisionAmbiguityError,
    RevisionIntent,
    SceneMetadataChange,
    SceneRoleTarget,
    SceneTimingChange,
    TextSpecChange,
    TimingConstraint,
    apply_revision,
)


def _duplicate_role_work() -> CreativeWork:
    base = product_teaser()
    duplicate = base.scenes[3].model_copy(
        update={"label": "development reprise", "narrative_role": NarrativeRole.DEVELOPMENT}
    )
    return base.model_copy(update={"scenes": (*base.scenes[:3], duplicate)}).seal()


def _duplicate_constraint_work() -> CreativeWork:
    base = product_teaser()
    extra = Constraint(
        constraint_id="c-hook-floor",
        priority=Priority.HARD,
        spec=TimingConstraint(
            target=ConstraintTarget(kind="scene", ref=base.scenes[0].scene_id),
            min_us=2 * ONE_SECOND_US,
            max_us=4 * ONE_SECOND_US,
        ),
        origin=Origin(source="user", detail="keep the hook readable"),
    )
    return base.model_copy(update={"constraints": (*base.constraints, extra)}).seal()


def test_scene_duration_revision_is_typed_and_deterministic() -> None:
    before = product_teaser()
    result = apply_revision(
        before,
        RevisionIntent(
            source_work_id=before.work_id,
            target=NodeTarget(kind="scene", ref=before.scenes[2].scene_id),
            change=SceneTimingChange(duration_us=3 * ONE_SECOND_US),
        ),
    )
    assert result.revision_id.startswith("rev_")
    assert result.work.scenes[2].timing.duration_us == 3 * ONE_SECOND_US
    assert all(
        layer.timeline().end_us <= result.work.scenes[2].timing.end_us
        for layer in result.work.scenes[2].layers
    )
    assert len(result.semantic_diff.scenes) == 1
    assert result.semantic_diff.scenes[0].before_duration_us == 4 * ONE_SECOND_US
    assert result.semantic_diff.scenes[0].after_duration_us == 3 * ONE_SECOND_US


def test_constraint_revision_changes_only_the_targeted_bound() -> None:
    before = product_teaser()
    result = apply_revision(
        before,
        RevisionIntent(
            source_work_id=before.work_id,
            target=NodeTarget(kind="constraint", ref=before.constraints[1].constraint_id),
            change=ConstraintTimingChange(max_us=2 * ONE_SECOND_US),
        ),
    )
    assert len(result.semantic_diff.constraints) == 1
    change = result.semantic_diff.constraints[0]
    assert change.before_payload is not None
    assert change.after_payload is not None
    assert change.before_payload["spec"]["max_us"] == 3 * ONE_SECOND_US
    assert change.after_payload["spec"]["max_us"] == 2 * ONE_SECOND_US


def test_editing_one_scene_does_not_report_adjacent_transitions_as_changed() -> None:
    before = product_teaser()
    result = apply_revision(
        before,
        RevisionIntent(
            source_work_id=before.work_id,
            target=NodeTarget(kind="scene", ref=before.scenes[2].scene_id),
            change=SceneMetadataChange(label="reveal, tightened"),
        ),
    )
    assert result.semantic_diff.transitions == ()
    assert all(entry.node_kind.value != "transition" for entry in result.ir_delta.modified)


def test_scene_revision_only_changes_constraints_when_their_semantics_change() -> None:
    before = product_teaser()
    result = apply_revision(
        before,
        RevisionIntent(
            source_work_id=before.work_id,
            target=NodeTarget(kind="scene", ref=before.scenes[0].scene_id),
            change=SceneMetadataChange(label="hook, sharpened"),
        ),
    )
    assert result.semantic_diff.constraints == ()


def test_two_constraints_with_the_same_kind_and_scope_both_survive() -> None:
    before = _duplicate_constraint_work()
    target = before.constraints[-1]
    result = apply_revision(
        before,
        RevisionIntent(
            source_work_id=before.work_id,
            target=NodeTarget(kind="constraint", ref=target.constraint_id),
            change=ConstraintTimingChange(min_us=1 * ONE_SECOND_US),
        ),
    )
    assert len(result.work.constraints) == len(before.constraints)
    assert len(result.semantic_diff.constraints) == 1
    untouched = before.constraints[-2]
    assert any(
        constraint.constraint_id == untouched.constraint_id
        for constraint in result.work.constraints
    )


def test_role_target_with_a_disambiguator_picks_the_named_scene_only() -> None:
    before = _duplicate_role_work()
    chosen = next(scene for scene in before.scenes if scene.label == "what it does")
    other = next(scene for scene in before.scenes if scene.label == "development reprise")
    result = apply_revision(
        before,
        RevisionIntent(
            source_work_id=before.work_id,
            target=SceneRoleTarget(narrative_role=NarrativeRole.DEVELOPMENT, label="what it does"),
            change=SceneMetadataChange(label="develop, tightened"),
        ),
    )
    changed = next(scene for scene in result.work.scenes if scene.label == "develop, tightened")
    untouched = next(scene for scene in result.work.scenes if scene.label == "development reprise")
    assert changed.scene_id != chosen.scene_id
    assert untouched.scene_id == other.scene_id


def test_ambiguous_role_target_fails_closed() -> None:
    before = _duplicate_role_work()
    with pytest.raises(RevisionAmbiguityError, match="ambiguous"):
        apply_revision(
            before,
            RevisionIntent(
                source_work_id=before.work_id,
                target=SceneRoleTarget(narrative_role=NarrativeRole.DEVELOPMENT),
                change=SceneMetadataChange(label="should never land"),
            ),
        )


def test_rename_and_re_role_is_explicitly_reported() -> None:
    before = product_teaser()
    result = apply_revision(
        before,
        RevisionIntent(
            source_work_id=before.work_id,
            target=NodeTarget(kind="scene", ref=before.scenes[3].scene_id),
            change=SceneMetadataChange(
                label="closing reveal",
                narrative_role=NarrativeRole.CLIMAX,
            ),
        ),
    )
    assert len(result.semantic_diff.scenes) == 1
    change = result.semantic_diff.scenes[0]
    assert change.before_label == "logo and call to action"
    assert change.after_label == "closing reveal"
    assert change.before_narrative_role == NarrativeRole.CALL_TO_ACTION.value
    assert change.after_narrative_role == NarrativeRole.CLIMAX.value


def test_identical_revision_twice_yields_the_same_identity_and_diff_digest() -> None:
    before = product_teaser()
    intent = RevisionIntent(
        source_work_id=before.work_id,
        target=NodeTarget(kind="layer", ref=before.scenes[3].layers[1].layer_id),
        change=TextSpecChange(text="همین الان ببینید"),
    )
    first = apply_revision(before, intent)
    second = apply_revision(before, intent)
    assert first.revision_id == second.revision_id
    assert first.resulting_work_id == second.resulting_work_id
    assert first.semantic_diff.diff_id == second.semantic_diff.diff_id


def test_changing_one_subtree_preserves_unrelated_identities_byte_for_byte() -> None:
    before = product_teaser()
    result = apply_revision(
        before,
        RevisionIntent(
            source_work_id=before.work_id,
            target=NodeTarget(kind="layer", ref=before.scenes[3].layers[1].layer_id),
            change=TextSpecChange(text="همین الان ببینید"),
        ),
    )
    assert result.work.scenes[0].scene_id == before.scenes[0].scene_id
    assert result.work.scenes[1].scene_id == before.scenes[1].scene_id
    assert result.work.effects == before.effects


def test_revision_determinism_survives_python_hash_randomization() -> None:
    program = (
        "import sys; sys.path.insert(0, 'tests/unit');"
        "from creative_ir_testkit import product_teaser;"
        "from nexus_ai_agent.creative.intelligence import ("
        "NodeTarget, RevisionIntent, TextSpecChange, apply_revision"
        ");"
        "w = product_teaser();"
        "intent = RevisionIntent("
        "source_work_id=w.work_id,"
        "target=NodeTarget(kind='layer', ref=w.scenes[3].layers[1].layer_id),"
        "change=TextSpecChange(text='همین الان ببینید')"
        ");"
        "r = apply_revision(w, intent);"
        "print(r.resulting_work_id);"
        "print(r.revision_id);"
        "print(r.semantic_diff.diff_id);"
        "print(r.work.to_canonical_json());"
        "print(r.semantic_diff.model_dump_json())"
    )
    repo = Path(__file__).resolve().parents[2]
    outputs: set[str] = set()
    for seed in ("0", "1", "12345", "999"):
        proc = subprocess.run(
            [sys.executable, "-c", program],
            cwd=repo,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        outputs.add(proc.stdout)
    assert len(outputs) == 1


# ── Adversarial regressions (audit M10) ──────────────────────────────────────


def test_a_text_revision_cannot_exceed_the_text_spec_ceiling() -> None:
    """The change bound must match the target model's, or the diff lies.

    ``_apply_text_change`` uses ``model_copy``, which skips validation; a change
    model that allowed 5000 chars would let a 4500-char revision pass and then
    produce a document whose ``TextSpec`` invariant (max 4000) is violated.
    """
    from pydantic import ValidationError

    base = product_teaser()
    target = base.scenes[3].layers[1].layer_id  # the CTA caption
    with pytest.raises(ValidationError):
        TextSpecChange(text="x" * 4001)
    # the boundary is inclusive on both sides
    ok = apply_revision(
        base,
        RevisionIntent(
            source_work_id=base.work_id,
            target=NodeTarget(kind="layer", ref=target),
            change=TextSpecChange(text="x" * 4000),
        ),
    )
    assert any(
        layer.content.text.text == "x" * 4000
        for scene in ok.work.scenes
        for layer in scene.layers
        if hasattr(layer.content, "text")
    )


def test_retiming_a_speed_changed_layer_preserves_the_source_ratio() -> None:
    """A retimed segment must keep ``source = timeline * speed``, not collapse it."""
    from creative_ir_testkit import _media_layer, _span

    base = product_teaser()
    fast = _media_layer(
        "fast", base.scenes[1].layers[0].role, base.assets[1].asset_id, 3, 1
    ).model_copy(
        update={
            "content": base.scenes[1]
            .layers[0]
            .content.model_copy(
                update={
                    "segment": _segment_2x(_span(3, 1)),
                }
            )
        }
    )
    scene = base.scenes[1]
    work = base.model_copy(
        update={
            "scenes": (
                base.scenes[0],
                scene.model_copy(update={"layers": (*scene.layers, fast)}),
                *base.scenes[2:],
            )
        }
    ).seal()
    fast_ref = work.scenes[1].layers[-1].layer_id
    result = apply_revision(
        work,
        RevisionIntent(
            source_work_id=work.work_id,
            target=NodeTarget(kind="scene", ref=work.scenes[1].scene_id),
            change=SceneTimingChange(start_us=3 * ONE_SECOND_US, duration_us=ONE_SECOND_US),
        ),
    )
    retimed = next(
        layer
        for scene in result.work.scenes
        for layer in scene.layers
        if layer.layer_id == fast_ref
    )
    segment = retimed.content.segment
    assert segment.speed_milli == 2000
    assert segment.source_duration_us == segment.timeline.duration_us * 2


def _segment_2x(span):
    from nexus_ai_agent.creative.intelligence import Segment

    return Segment(
        source_in_us=0,
        source_duration_us=span.duration_us * 2,
        timeline=span,
        speed_milli=2000,
    )


def test_an_audio_revision_preserves_gain_it_does_not_own() -> None:
    """``gain_milli`` is outside the audio change contract and must survive."""
    from nexus_ai_agent.creative.intelligence import AudioIntentChange

    base = product_teaser()
    music_layer = next(
        layer
        for scene in base.scenes
        for layer in scene.layers
        if hasattr(layer.content, "audio") and layer.content.audio is not None
    )
    boosted = base.model_copy(update={"scenes": _with_audio_gain(base, music_layer, 5000)}).seal()
    target = next(
        layer
        for scene in boosted.scenes
        for layer in scene.layers
        if hasattr(layer.content, "audio")
        and layer.content.audio is not None
        and layer.content.audio.gain_milli == 5000
    )
    result = apply_revision(
        boosted,
        RevisionIntent(
            source_work_id=boosted.work_id,
            target=NodeTarget(kind="layer", ref=target.layer_id),
            change=AudioIntentChange(duck_under_voiceover=True),
        ),
    )
    revised = next(
        layer
        for scene in result.work.scenes
        for layer in scene.layers
        if hasattr(layer.content, "audio")
        and layer.content.audio is not None
        and layer.content.audio.duck_under_voiceover is True
    )
    assert revised.content.audio.gain_milli == 5000


def _with_audio_gain(base, target_layer, gain_milli):

    scenes = []
    for scene in base.scenes:
        layers = []
        for layer in scene.layers:
            if layer.layer_id == target_layer.layer_id:
                audio = layer.content.audio.model_copy(update={"gain_milli": gain_milli})
                layer = layer.model_copy(
                    update={"content": layer.content.model_copy(update={"audio": audio})}
                )
            layers.append(layer)
        scenes.append(scene.model_copy(update={"layers": tuple(layers)}))
    return tuple(scenes)


def test_revising_an_effect_does_not_report_its_layers_or_scenes_as_replaced() -> None:
    """Effect intensity flows into every layer that uses the effect.

    A layer commits its effect ids through ``child_identities``, so the layer and
    its scene get new ids. ``diff_works`` pairs them only by lineage, so the
    revision must emit lineage for them -- otherwise the scene is reported as
    both removed and added, contradicting the revision contract.
    """
    from nexus_ai_agent.creative.intelligence import EffectIntentChange

    work = product_teaser()
    glow = next(e for e in work.effects if e.operation == "motion.add_glow")
    result = apply_revision(
        work,
        RevisionIntent(
            source_work_id=work.work_id,
            target=NodeTarget(kind="effect", ref=glow.effect_id),
            change=EffectIntentChange(intensity=900),
        ),
    )
    assert result.semantic_diff.scenes == ()
    assert result.semantic_diff.transitions == ()
    kinds = {node.kind for node in result.affected_nodes}
    assert {"effect", "layer", "scene"} <= kinds
