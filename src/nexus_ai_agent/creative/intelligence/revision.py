"""Typed, deterministic revision for the Creative Intelligence Plane.

A revision is not a string replace and not a free mutation over a dict. It is a
closed contract:

``RevisionIntent -> explicit target -> deterministic transformation ->
RevisedCreativeWork -> IR delta -> semantic diff``

The caller must name a target the plane can prove. When the target is ambiguous,
revision fails closed. When the change is representable, the plane applies a
pure transformation, re-seals the document, and reports exactly what changed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai_agent.creative.intelligence.diff import (
    DiffLineage,
    IRDelta,
    LineagePair,
    SemanticDiff,
    diff_works,
)
from nexus_ai_agent.creative.intelligence.errors import (
    IRValidationError,
    RevisionAmbiguityError,
    RevisionError,
    RevisionTargetError,
    UnsupportedRevisionError,
)
from nexus_ai_agent.creative.intelligence.identity import content_id
from nexus_ai_agent.creative.intelligence.ir import (
    AudioIntent,
    CreativeWork,
    Effect,
    Layer,
    MediaContent,
    NarrativeRole,
    PacingConstraint,
    QualityConstraint,
    Scene,
    TextContent,
    Timing,
    TimingConstraint,
    Transition,
    TransitionKind,
)

__all__ = [
    "AudioIntentChange",
    "AffectedNode",
    "ConstraintPacingChange",
    "ConstraintQualityChange",
    "ConstraintTimingChange",
    "EffectIntentChange",
    "NodeTarget",
    "RevisionIntent",
    "RevisionResult",
    "RevisionTarget",
    "SceneMetadataChange",
    "SceneRoleTarget",
    "SceneTimingChange",
    "TextSpecChange",
    "TransitionIntentChange",
    "apply_revision",
]


class NodeTarget(BaseModel):
    """Target a node by its sealed, explicit identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scene", "layer", "transition", "constraint", "effect"]
    ref: str = Field(min_length=1, max_length=256)


class SceneRoleTarget(BaseModel):
    """Target a scene by role, with an optional explicit disambiguator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scene_role"] = "scene_role"
    narrative_role: NarrativeRole
    label: str | None = Field(default=None, min_length=1, max_length=200)


RevisionTarget = NodeTarget | SceneRoleTarget


class SceneTimingChange(BaseModel):
    """Change a scene's timing and deterministically keep its layers inside it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scene_timing"] = "scene_timing"
    start_us: int | None = Field(default=None, ge=0)
    duration_us: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _states_something(self) -> SceneTimingChange:
        if self.start_us is None and self.duration_us is None:
            raise ValueError("a scene timing revision must state start_us and/or duration_us")
        return self


class SceneMetadataChange(BaseModel):
    """Change the scene label and/or its narrative role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scene_metadata"] = "scene_metadata"
    label: str | None = Field(default=None, min_length=1, max_length=200)
    narrative_role: NarrativeRole | None = None

    @model_validator(mode="after")
    def _states_something(self) -> SceneMetadataChange:
        if self.label is None and self.narrative_role is None:
            raise ValueError("a scene metadata revision must state a label and/or a narrative_role")
        return self


class ConstraintTimingChange(BaseModel):
    """Change the numeric band of a TimingConstraint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["timing_constraint"] = "timing_constraint"
    min_us: int | None = Field(default=None, ge=0)
    max_us: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _states_something(self) -> ConstraintTimingChange:
        if self.min_us is None and self.max_us is None:
            raise ValueError("a timing-constraint revision must state min_us and/or max_us")
        return self


class ConstraintPacingChange(BaseModel):
    """Change the numeric bounds of a PacingConstraint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["pacing_constraint"] = "pacing_constraint"
    max_scene_duration_us: int | None = Field(default=None, ge=0)
    min_scene_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _states_something(self) -> ConstraintPacingChange:
        if self.max_scene_duration_us is None and self.min_scene_count is None:
            raise ValueError(
                "a pacing-constraint revision must state max_scene_duration_us and/or "
                "min_scene_count"
            )
        return self


class ConstraintQualityChange(BaseModel):
    """Change the bounds of a QualityConstraint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["quality_constraint"] = "quality_constraint"
    min_width_px: int | None = Field(default=None, ge=0)
    min_height_px: int | None = Field(default=None, ge=0)
    min_frame_rate_milli: int | None = Field(default=None, ge=0)
    max_duration_us: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _states_something(self) -> ConstraintQualityChange:
        if (
            self.min_width_px is None
            and self.min_height_px is None
            and self.min_frame_rate_milli is None
            and self.max_duration_us is None
        ):
            raise ValueError("a quality-constraint revision must state at least one bound")
        return self


class TextSpecChange(BaseModel):
    """Change the explicit text of a typography layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["text_spec"] = "text_spec"
    text: str = Field(min_length=1, max_length=5000)


class TransitionIntentChange(BaseModel):
    """Change the transition semantics the IR can really express."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["transition"] = "transition"
    transition_kind: TransitionKind | None = None
    duration_us: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _states_something(self) -> TransitionIntentChange:
        if self.transition_kind is None and self.duration_us is None:
            raise ValueError("a transition revision must state transition_kind and/or duration_us")
        return self


class AudioIntentChange(BaseModel):
    """Change the explicit audio intent on a media layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["audio_intent"] = "audio_intent"
    duck_under_voiceover: bool | None = None
    loudness_target_lufs_milli: int | None = Field(default=None, ge=-70000, le=0)

    @model_validator(mode="after")
    def _states_something(self) -> AudioIntentChange:
        if self.duck_under_voiceover is None and self.loudness_target_lufs_milli is None:
            raise ValueError(
                "an audio-intent revision must state duck_under_voiceover and/or "
                "loudness_target_lufs_milli"
            )
        return self


class EffectIntentChange(BaseModel):
    """Change the effect intensity of an explicit effect node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["effect"] = "effect"
    intensity: int = Field(ge=0, le=1000)


RevisionChange = (
    SceneTimingChange
    | SceneMetadataChange
    | ConstraintTimingChange
    | ConstraintPacingChange
    | ConstraintQualityChange
    | TextSpecChange
    | TransitionIntentChange
    | AudioIntentChange
    | EffectIntentChange
)


class RevisionIntent(BaseModel):
    """One typed, deterministic request to revise a CreativeWork."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_work_id: str = Field(min_length=1, max_length=256)
    target: RevisionTarget
    change: RevisionChange


class AffectedNode(BaseModel):
    """A node the revision rewrote explicitly or because its parent changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scene", "layer", "transition", "constraint", "effect"]
    before_ref: str = Field(min_length=1, max_length=256)
    after_ref: str = Field(min_length=1, max_length=256)


class RevisionResult(BaseModel):
    """The full revision contract returned by :func:`apply_revision`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_id: str = Field(min_length=1, max_length=256)
    source_work_id: str = Field(min_length=1, max_length=256)
    target: RevisionTarget
    change: RevisionChange
    affected_nodes: tuple[AffectedNode, ...]
    work: CreativeWork
    resulting_work_id: str = Field(min_length=1, max_length=256)
    ir_delta: IRDelta
    semantic_diff: SemanticDiff


class _ResolvedNode(BaseModel):
    """Internal resolved target with positional provenance."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["scene", "layer", "transition", "constraint", "effect"]
    index: int = Field(ge=0)
    scene_index: int | None = Field(default=None, ge=0)
    layer_index: int | None = Field(default=None, ge=0)


class _PendingPair(BaseModel):
    """How to recover an after-ref from the sealed revised document."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["scene", "layer", "transition", "constraint", "effect"]
    before_ref: str = Field(min_length=1, max_length=256)
    index: int = Field(ge=0)
    scene_index: int | None = Field(default=None, ge=0)
    layer_index: int | None = Field(default=None, ge=0)


def apply_revision(work: CreativeWork, intent: RevisionIntent) -> RevisionResult:
    """Apply a typed revision to ``work`` and report the exact resulting delta."""
    source = work if work.work_id else work.seal()
    if source.work_id != intent.source_work_id:
        raise RevisionTargetError(
            f"revision names source_work_id {intent.source_work_id!r} but the document is "
            f"{source.work_id!r}"
        )
    resolved = _resolve_target(source, intent.target)
    revised_unsealed, pending = _apply_change(source, resolved, intent.change)
    revised = revised_unsealed.seal()
    revised.assert_valid()
    revised.verify_identity()

    lineage = DiffLineage(pairs=tuple(_resolve_pending_pairs(revised, pending)))
    diff = diff_works(source, revised, lineage=lineage)
    revision_id = content_id(
        "rev",
        {
            "source_work_id": intent.source_work_id,
            "target": intent.target.model_dump(mode="json"),
            "change": intent.change.model_dump(mode="json"),
        },
    )
    affected = tuple(
        AffectedNode(
            kind=pair.kind,
            before_ref=pair.before_ref,
            after_ref=pair.after_ref,
        )
        for pair in lineage.pairs
    )
    return RevisionResult(
        revision_id=revision_id,
        source_work_id=source.work_id,
        target=intent.target,
        change=intent.change,
        affected_nodes=affected,
        work=revised,
        resulting_work_id=revised.work_id,
        ir_delta=diff.ir_delta,
        semantic_diff=diff.semantic_diff,
    )


def _resolve_target(work: CreativeWork, target: RevisionTarget) -> _ResolvedNode:
    if isinstance(target, NodeTarget):
        if target.kind == "scene":
            for index, scene in enumerate(work.scenes):
                if scene.scene_id == target.ref:
                    return _ResolvedNode(kind="scene", index=index)
        elif target.kind == "layer":
            for scene_index, scene in enumerate(work.scenes):
                for layer_index, layer in enumerate(scene.layers):
                    if layer.layer_id == target.ref:
                        return _ResolvedNode(
                            kind="layer",
                            index=layer_index,
                            scene_index=scene_index,
                            layer_index=layer_index,
                        )
        elif target.kind == "transition":
            for index, transition in enumerate(work.transitions):
                if transition.transition_id == target.ref:
                    return _ResolvedNode(kind="transition", index=index)
        elif target.kind == "constraint":
            for index, constraint in enumerate(work.constraints):
                if constraint.constraint_id == target.ref:
                    return _ResolvedNode(kind="constraint", index=index)
        elif target.kind == "effect":
            for index, effect in enumerate(work.effects):
                if effect.effect_id == target.ref:
                    return _ResolvedNode(kind="effect", index=index)
        raise RevisionTargetError(f"target {target.kind} ref {target.ref!r} is not present")

    matches = [scene for scene in work.scenes if scene.narrative_role is target.narrative_role]
    if target.label is not None:
        matches = [scene for scene in matches if scene.label == target.label]
    if not matches:
        detail = f"role {target.narrative_role.value!r}"
        if target.label is not None:
            detail += f" with label {target.label!r}"
        raise RevisionTargetError(f"no scene matches {detail}")
    if len(matches) > 1:
        refs = ", ".join(scene.scene_id for scene in matches)
        raise RevisionAmbiguityError(
            f"scene role target is ambiguous; add an explicit disambiguator (matching refs: {refs})"
        )
    scene = matches[0]
    index = next(index for index, candidate in enumerate(work.scenes) if candidate is scene)
    return _ResolvedNode(kind="scene", index=index)


def _apply_change(
    work: CreativeWork, resolved: _ResolvedNode, change: RevisionChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    if isinstance(change, SceneTimingChange):
        if resolved.kind != "scene":
            raise UnsupportedRevisionError("scene_timing revisions require a scene target")
        return _apply_scene_timing_change(work, resolved.index, change)
    if isinstance(change, SceneMetadataChange):
        if resolved.kind != "scene":
            raise UnsupportedRevisionError("scene_metadata revisions require a scene target")
        return _apply_scene_metadata_change(work, resolved.index, change)
    if isinstance(change, ConstraintTimingChange):
        if resolved.kind != "constraint":
            raise UnsupportedRevisionError(
                "timing_constraint revisions require a constraint target"
            )
        return _apply_constraint_timing_change(work, resolved.index, change)
    if isinstance(change, ConstraintPacingChange):
        if resolved.kind != "constraint":
            raise UnsupportedRevisionError(
                "pacing_constraint revisions require a constraint target"
            )
        return _apply_constraint_pacing_change(work, resolved.index, change)
    if isinstance(change, ConstraintQualityChange):
        if resolved.kind != "constraint":
            raise UnsupportedRevisionError(
                "quality_constraint revisions require a constraint target"
            )
        return _apply_constraint_quality_change(work, resolved.index, change)
    if isinstance(change, TextSpecChange):
        if resolved.kind != "layer" or resolved.scene_index is None or resolved.layer_index is None:
            raise UnsupportedRevisionError("text_spec revisions require a layer target")
        return _apply_text_change(work, resolved.scene_index, resolved.layer_index, change)
    if isinstance(change, TransitionIntentChange):
        if resolved.kind != "transition":
            raise UnsupportedRevisionError("transition revisions require a transition target")
        return _apply_transition_change(work, resolved.index, change)
    if isinstance(change, AudioIntentChange):
        if resolved.kind != "layer" or resolved.scene_index is None or resolved.layer_index is None:
            raise UnsupportedRevisionError("audio_intent revisions require a layer target")
        return _apply_audio_change(work, resolved.scene_index, resolved.layer_index, change)
    if isinstance(change, EffectIntentChange):
        if resolved.kind != "effect":
            raise UnsupportedRevisionError("effect revisions require an effect target")
        return _apply_effect_change(work, resolved.index, change)
    raise UnsupportedRevisionError(f"unsupported revision change {type(change).__name__}")


def _apply_scene_timing_change(
    work: CreativeWork, scene_index: int, change: SceneTimingChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    scene = work.scenes[scene_index]
    new_timing = Timing(
        start_us=scene.timing.start_us if change.start_us is None else change.start_us,
        duration_us=scene.timing.duration_us if change.duration_us is None else change.duration_us,
    )
    rewritten_layers: list[Layer] = []
    pending: list[_PendingPair] = [
        _PendingPair(kind="scene", before_ref=scene.scene_id, index=scene_index)
    ]
    for layer_index, layer in enumerate(scene.layers):
        rewritten_layers.append(_retime_layer(layer, old_scene=scene, new_timing=new_timing))
        pending.append(
            _PendingPair(
                kind="layer",
                before_ref=layer.layer_id,
                index=layer_index,
                scene_index=scene_index,
                layer_index=layer_index,
            )
        )
    rewritten_scene = scene.model_copy(
        update={"timing": new_timing, "layers": tuple(rewritten_layers)}
    )
    revised = work.model_copy(
        update={
            "scenes": (
                *work.scenes[:scene_index],
                rewritten_scene,
                *work.scenes[scene_index + 1 :],
            )
        }
    )
    revised.assert_valid()
    return revised, tuple(pending)


def _retime_layer(layer: Layer, *, old_scene: Scene, new_timing: Timing) -> Layer:
    old_timing = layer.timeline()
    new_start = new_timing.start_us + (old_timing.start_us - old_scene.timing.start_us)
    max_duration = new_timing.end_us - new_start
    if max_duration <= 0:
        raise IRValidationError(
            "retiming scene "
            f"{old_scene.scene_id} would push layer {layer.layer_id} outside the scene"
        )
    new_duration = min(old_timing.duration_us, max_duration)
    if isinstance(layer.content, MediaContent):
        new_span = layer.content.segment.timeline.model_copy(
            update={"start_us": new_start, "duration_us": new_duration}
        )
        new_segment = layer.content.segment.model_copy(
            update={"timeline": new_span, "source_duration_us": new_duration}
        )
        return layer.model_copy(
            update={"content": layer.content.model_copy(update={"segment": new_segment})}
        )
    new_span = layer.content.timeline.model_copy(
        update={"start_us": new_start, "duration_us": new_duration}
    )
    return layer.model_copy(
        update={"content": layer.content.model_copy(update={"timeline": new_span})}
    )


def _apply_scene_metadata_change(
    work: CreativeWork, scene_index: int, change: SceneMetadataChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    scene = work.scenes[scene_index]
    rewritten_scene = scene.model_copy(
        update={
            "label": scene.label if change.label is None else change.label,
            "narrative_role": (
                scene.narrative_role if change.narrative_role is None else change.narrative_role
            ),
        }
    )
    revised = work.model_copy(
        update={
            "scenes": (
                *work.scenes[:scene_index],
                rewritten_scene,
                *work.scenes[scene_index + 1 :],
            )
        }
    )
    revised.assert_valid()
    return revised, (_PendingPair(kind="scene", before_ref=scene.scene_id, index=scene_index),)


def _apply_constraint_timing_change(
    work: CreativeWork, index: int, change: ConstraintTimingChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    constraint = work.constraints[index]
    if not isinstance(constraint.spec, TimingConstraint):
        raise UnsupportedRevisionError("the targeted constraint is not a TimingConstraint")
    spec = constraint.spec
    updated = constraint.model_copy(
        update={
            "spec": TimingConstraint(
                target=spec.target,
                min_us=spec.min_us if change.min_us is None else change.min_us,
                max_us=spec.max_us if change.max_us is None else change.max_us,
            )
        }
    )
    revised = work.model_copy(
        update={
            "constraints": (
                *work.constraints[:index],
                updated,
                *work.constraints[index + 1 :],
            )
        }
    )
    revised.assert_valid()
    return revised, (
        _PendingPair(kind="constraint", before_ref=constraint.constraint_id, index=index),
    )


def _apply_constraint_pacing_change(
    work: CreativeWork, index: int, change: ConstraintPacingChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    constraint = work.constraints[index]
    if not isinstance(constraint.spec, PacingConstraint):
        raise UnsupportedRevisionError("the targeted constraint is not a PacingConstraint")
    spec = constraint.spec
    updated = constraint.model_copy(
        update={
            "spec": PacingConstraint(
                max_scene_duration_us=(
                    spec.max_scene_duration_us
                    if change.max_scene_duration_us is None
                    else change.max_scene_duration_us
                ),
                min_scene_count=(
                    spec.min_scene_count
                    if change.min_scene_count is None
                    else change.min_scene_count
                ),
            )
        }
    )
    revised = work.model_copy(
        update={
            "constraints": (
                *work.constraints[:index],
                updated,
                *work.constraints[index + 1 :],
            )
        }
    )
    revised.assert_valid()
    return revised, (
        _PendingPair(kind="constraint", before_ref=constraint.constraint_id, index=index),
    )


def _apply_constraint_quality_change(
    work: CreativeWork, index: int, change: ConstraintQualityChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    constraint = work.constraints[index]
    if not isinstance(constraint.spec, QualityConstraint):
        raise UnsupportedRevisionError("the targeted constraint is not a QualityConstraint")
    spec = constraint.spec
    updated = constraint.model_copy(
        update={
            "spec": QualityConstraint(
                min_width_px=(
                    spec.min_width_px if change.min_width_px is None else change.min_width_px
                ),
                min_height_px=(
                    spec.min_height_px if change.min_height_px is None else change.min_height_px
                ),
                min_frame_rate_milli=(
                    spec.min_frame_rate_milli
                    if change.min_frame_rate_milli is None
                    else change.min_frame_rate_milli
                ),
                max_duration_us=(
                    spec.max_duration_us
                    if change.max_duration_us is None
                    else change.max_duration_us
                ),
            )
        }
    )
    revised = work.model_copy(
        update={
            "constraints": (
                *work.constraints[:index],
                updated,
                *work.constraints[index + 1 :],
            )
        }
    )
    revised.assert_valid()
    return revised, (
        _PendingPair(kind="constraint", before_ref=constraint.constraint_id, index=index),
    )


def _apply_text_change(
    work: CreativeWork, scene_index: int, layer_index: int, change: TextSpecChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    scene = work.scenes[scene_index]
    layer = scene.layers[layer_index]
    if not isinstance(layer.content, TextContent):
        raise UnsupportedRevisionError("the targeted layer is not a TextContent layer")
    rewritten_layer = layer.model_copy(
        update={
            "content": layer.content.model_copy(
                update={"text": layer.content.text.model_copy(update={"text": change.text})}
            )
        }
    )
    rewritten_scene = scene.model_copy(
        update={
            "layers": (
                *scene.layers[:layer_index],
                rewritten_layer,
                *scene.layers[layer_index + 1 :],
            )
        }
    )
    revised = work.model_copy(
        update={
            "scenes": (
                *work.scenes[:scene_index],
                rewritten_scene,
                *work.scenes[scene_index + 1 :],
            )
        }
    )
    revised.assert_valid()
    return revised, (
        _PendingPair(kind="scene", before_ref=scene.scene_id, index=scene_index),
        _PendingPair(
            kind="layer",
            before_ref=layer.layer_id,
            index=layer_index,
            scene_index=scene_index,
            layer_index=layer_index,
        ),
    )


def _apply_transition_change(
    work: CreativeWork, index: int, change: TransitionIntentChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    transition = work.transitions[index]
    updated = transition.model_copy(
        update={
            "kind": transition.kind if change.transition_kind is None else change.transition_kind,
            "duration_us": (
                transition.duration_us if change.duration_us is None else change.duration_us
            ),
        }
    )
    # re-construct so validators run (e.g. CUT <-> duration invariant)
    rewritten = Transition.model_validate(updated.model_dump(mode="json"))
    revised = work.model_copy(
        update={
            "transitions": (
                *work.transitions[:index],
                rewritten,
                *work.transitions[index + 1 :],
            )
        }
    )
    revised.assert_valid()
    return revised, (
        _PendingPair(kind="transition", before_ref=transition.transition_id, index=index),
    )


def _apply_audio_change(
    work: CreativeWork, scene_index: int, layer_index: int, change: AudioIntentChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    scene = work.scenes[scene_index]
    layer = scene.layers[layer_index]
    if not isinstance(layer.content, MediaContent):
        raise UnsupportedRevisionError("the targeted layer is not media-backed")
    if layer.content.audio is None:
        raise UnsupportedRevisionError(
            "the targeted media layer has no AudioIntent to revise; create one in a later slice"
        )
    audio = layer.content.audio
    revised_audio = AudioIntent(
        duck_under_voiceover=(
            audio.duck_under_voiceover
            if change.duck_under_voiceover is None
            else change.duck_under_voiceover
        ),
        loudness_target_lufs_milli=(
            audio.loudness_target_lufs_milli
            if change.loudness_target_lufs_milli is None
            else change.loudness_target_lufs_milli
        ),
    )
    rewritten_layer = layer.model_copy(
        update={"content": layer.content.model_copy(update={"audio": revised_audio})}
    )
    rewritten_scene = scene.model_copy(
        update={
            "layers": (
                *scene.layers[:layer_index],
                rewritten_layer,
                *scene.layers[layer_index + 1 :],
            )
        }
    )
    revised = work.model_copy(
        update={
            "scenes": (
                *work.scenes[:scene_index],
                rewritten_scene,
                *work.scenes[scene_index + 1 :],
            )
        }
    )
    revised.assert_valid()
    return revised, (
        _PendingPair(kind="scene", before_ref=scene.scene_id, index=scene_index),
        _PendingPair(
            kind="layer",
            before_ref=layer.layer_id,
            index=layer_index,
            scene_index=scene_index,
            layer_index=layer_index,
        ),
    )


def _apply_effect_change(
    work: CreativeWork, index: int, change: EffectIntentChange
) -> tuple[CreativeWork, tuple[_PendingPair, ...]]:
    effect = work.effects[index]
    rewritten = Effect.model_validate(
        effect.model_copy(update={"intensity": change.intensity}).model_dump(mode="json")
    )
    revised = work.model_copy(
        update={
            "effects": (
                *work.effects[:index],
                rewritten,
                *work.effects[index + 1 :],
            )
        }
    )
    revised.assert_valid()
    return revised, (_PendingPair(kind="effect", before_ref=effect.effect_id, index=index),)


def _resolve_pending_pairs(
    revised: CreativeWork, pending: tuple[_PendingPair, ...]
) -> tuple[LineagePair, ...]:
    pairs: list[LineagePair] = []
    for entry in pending:
        if entry.kind == "scene":
            after_ref = revised.scenes[entry.index].scene_id
        elif entry.kind == "layer":
            if entry.scene_index is None or entry.layer_index is None:
                raise RevisionError("layer lineage is missing scene/layer indices")
            after_ref = revised.scenes[entry.scene_index].layers[entry.layer_index].layer_id
        elif entry.kind == "transition":
            after_ref = revised.transitions[entry.index].transition_id
        elif entry.kind == "constraint":
            after_ref = revised.constraints[entry.index].constraint_id
        elif entry.kind == "effect":
            after_ref = revised.effects[entry.index].effect_id
        else:  # pragma: no cover - closed union
            raise RevisionError(f"unsupported pending lineage kind {entry.kind!r}")
        pairs.append(LineagePair(kind=entry.kind, before_ref=entry.before_ref, after_ref=after_ref))
    return tuple(_dedupe_pairs(pairs))


def _dedupe_pairs(pairs: list[LineagePair]) -> list[LineagePair]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[LineagePair] = []
    for pair in pairs:
        key = (pair.kind, pair.before_ref, pair.after_ref)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pair)
    return deduped
