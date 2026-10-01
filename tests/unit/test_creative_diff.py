"""Focused regression tests for semantic diff in the Creative Intelligence Plane."""

from __future__ import annotations

from creative_ir_testkit import ONE_SECOND_US, product_teaser

from nexus_ai_agent.creative.intelligence import (
    Constraint,
    ConstraintTarget,
    DiffLineage,
    LineagePair,
    Origin,
    Priority,
    QualityConstraint,
    TimingConstraint,
    diff_works,
)


def test_identical_documents_diff_to_nothing() -> None:
    work = product_teaser()
    diff = diff_works(work, work)
    assert diff.ir_delta.added == ()
    assert diff.ir_delta.removed == ()
    assert diff.ir_delta.modified == ()
    assert diff.semantic_diff.scenes == ()
    assert diff.semantic_diff.constraints == ()
    assert diff.semantic_diff.transitions == ()


def test_an_edited_scene_does_not_report_adjacent_transitions_as_modified() -> None:
    before = product_teaser()
    after = before.model_copy(
        update={
            "scenes": (
                before.scenes[0],
                before.scenes[1],
                before.scenes[2].model_copy(update={"label": "reveal, tightened"}),
                before.scenes[3],
            )
        }
    ).seal()
    diff = diff_works(
        before,
        after,
        lineage=DiffLineage(
            pairs=(
                LineagePair(
                    kind="scene",
                    before_ref=before.scenes[2].scene_id,
                    after_ref=after.scenes[2].scene_id,
                ),
            )
        ),
    )
    assert len(diff.semantic_diff.scenes) == 1
    assert diff.semantic_diff.scenes[0].before_label == "dramatic reveal"
    assert diff.semantic_diff.scenes[0].after_label == "reveal, tightened"
    assert diff.semantic_diff.transitions == ()
    assert all(entry.node_kind.value != "transition" for entry in diff.ir_delta.modified)


def test_only_the_constraint_that_actually_changed_is_reported() -> None:
    before = product_teaser()
    hook_constraint = before.constraints[1]
    assert isinstance(hook_constraint.spec, TimingConstraint)
    after = before.model_copy(
        update={
            "scenes": (
                before.scenes[0].model_copy(update={"label": "hook, sharpened"}),
                *before.scenes[1:],
            ),
            "constraints": (
                before.constraints[0],
                hook_constraint.model_copy(
                    update={
                        "spec": TimingConstraint(
                            target=hook_constraint.spec.target,
                            max_us=2 * ONE_SECOND_US,
                        )
                    }
                ),
                *before.constraints[2:],
            ),
        }
    ).seal()
    diff = diff_works(
        before,
        after,
        lineage=DiffLineage(
            pairs=(
                LineagePair(
                    kind="scene",
                    before_ref=before.scenes[0].scene_id,
                    after_ref=after.scenes[0].scene_id,
                ),
                LineagePair(
                    kind="constraint",
                    before_ref=before.constraints[1].constraint_id,
                    after_ref=after.constraints[1].constraint_id,
                ),
            )
        ),
    )
    assert len(diff.semantic_diff.constraints) == 1
    change = diff.semantic_diff.constraints[0]
    assert change.before_payload is not None
    assert change.after_payload is not None
    assert change.before_payload["spec"]["max_us"] == 3 * ONE_SECOND_US
    assert change.after_payload["spec"]["max_us"] == 2 * ONE_SECOND_US


def test_two_constraints_with_the_same_kind_and_scope_do_not_collide() -> None:
    base = product_teaser()
    before = base.model_copy(
        update={
            "constraints": (
                *base.constraints,
                Constraint(
                    constraint_id="c-hook-floor",
                    priority=Priority.HARD,
                    spec=TimingConstraint(
                        target=ConstraintTarget(kind="scene", ref=base.scenes[0].scene_id),
                        min_us=2 * ONE_SECOND_US,
                        max_us=4 * ONE_SECOND_US,
                    ),
                    origin=Origin(source="user", detail="keep the hook readable"),
                ),
                Constraint(
                    constraint_id="c-quality-cap",
                    priority=Priority.SOFT,
                    spec=QualityConstraint(max_duration_us=15 * ONE_SECOND_US),
                    origin=Origin(source="user", detail="a soft runtime ceiling"),
                ),
            )
        }
    ).seal()
    original = before.constraints[-2]
    assert isinstance(original.spec, TimingConstraint)
    after = before.model_copy(
        update={
            "constraints": (
                before.constraints[0],
                before.constraints[1],
                *before.constraints[2:-2],
                original.model_copy(
                    update={
                        "spec": TimingConstraint(
                            target=original.spec.target,
                            min_us=1 * ONE_SECOND_US,
                            max_us=4 * ONE_SECOND_US,
                        )
                    }
                ),
                before.constraints[-1],
            )
        }
    ).seal()
    diff = diff_works(
        before,
        after,
        lineage=DiffLineage(
            pairs=(
                LineagePair(
                    kind="constraint",
                    before_ref=before.constraints[-2].constraint_id,
                    after_ref=after.constraints[-2].constraint_id,
                ),
            )
        ),
    )
    assert len(after.constraints) == len(before.constraints)
    assert len(diff.semantic_diff.constraints) == 1
    assert diff.semantic_diff.constraints[0].before_ref == before.constraints[-2].constraint_id
    assert diff.semantic_diff.constraints[0].after_ref == after.constraints[-2].constraint_id
