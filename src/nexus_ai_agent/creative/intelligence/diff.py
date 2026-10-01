"""Semantic diff for the Creative Intelligence Plane.

The Creative IR is content-addressed, so two unchanged subtrees share the same
identities and can be pruned in O(1). What the Merkle ids do *not* tell a diff
by themselves is which changed node in ``after`` descends from which node in
``before`` once the content address changes. That provenance comes from the
caller: tonight's revision slice supplies the explicit lineage for the nodes it
rewrote, and the diff uses that to separate **real semantic change** from the
reference churn that sealing introduces.

Two consequences follow:

* identical works diff to nothing immediately via the root identity;
* a scene revision can update that scene and its ancestors without reporting
  adjacent transitions or untouched constraints as modified merely because a
  scene id was re-sealed.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.creative.intelligence.errors import CreativeIRError
from nexus_ai_agent.creative.intelligence.identity import canonical_json, content_id
from nexus_ai_agent.creative.intelligence.ir import (
    Constraint,
    ConstraintTarget,
    CreativeWork,
    EmphasisConstraint,
    ExclusionConstraint,
    Layer,
    MediaContent,
    OrderConstraint,
    PacingConstraint,
    QualityConstraint,
    TextContent,
    TimingConstraint,
    Transition,
)

__all__ = [
    "DiffLineage",
    "DiffLineageError",
    "IRDelta",
    "IRDeltaEntry",
    "IRDeltaKind",
    "LineagePair",
    "SemanticDiff",
    "SceneChange",
    "ConstraintChange",
    "TransitionChange",
    "TextChange",
    "AudioChange",
    "EffectChange",
    "AttentionChange",
    "WorkDiff",
    "diff_works",
]


class DiffLineageError(CreativeIRError):
    """The caller supplied impossible or internally contradictory lineage."""


class IRDeltaKind(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


class NodeKind(str, Enum):
    WORK = "work"
    SCENE = "scene"
    LAYER = "layer"
    TRANSITION = "transition"
    CONSTRAINT = "constraint"
    EFFECT = "effect"


class LineagePair(BaseModel):
    """One explicit before→after identity relation supplied by the caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scene", "layer", "transition", "constraint", "effect"]
    before_ref: str = Field(min_length=1, max_length=256)
    after_ref: str = Field(min_length=1, max_length=256)


class DiffLineage(BaseModel):
    """Explicit lineage the diff can trust when a content address changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pairs: tuple[LineagePair, ...] = ()

    def after_to_before(self, kind: str) -> dict[str, str]:
        return {pair.after_ref: pair.before_ref for pair in self.pairs if pair.kind == kind}

    def before_to_after(self, kind: str) -> dict[str, str]:
        return {pair.before_ref: pair.after_ref for pair in self.pairs if pair.kind == kind}


class IRDeltaEntry(BaseModel):
    """One node-level change in the IR after lineage normalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change: IRDeltaKind
    node_kind: NodeKind
    before_ref: str | None = Field(default=None, max_length=256)
    after_ref: str | None = Field(default=None, max_length=256)


class IRDelta(BaseModel):
    """Normalized IR delta: added / removed / modified nodes only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    added: tuple[IRDeltaEntry, ...] = ()
    removed: tuple[IRDeltaEntry, ...] = ()
    modified: tuple[IRDeltaEntry, ...] = ()


class SceneChange(BaseModel):
    """A scene's semantics changed, or the scene appeared/disappeared."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change: IRDeltaKind
    before_ref: str | None = Field(default=None, max_length=256)
    after_ref: str | None = Field(default=None, max_length=256)
    before_label: str | None = Field(default=None, max_length=200)
    after_label: str | None = Field(default=None, max_length=200)
    before_narrative_role: str | None = None
    after_narrative_role: str | None = None
    before_start_us: int | None = Field(default=None, ge=0)
    after_start_us: int | None = Field(default=None, ge=0)
    before_duration_us: int | None = Field(default=None, ge=0)
    after_duration_us: int | None = Field(default=None, ge=0)


class ConstraintChange(BaseModel):
    """A constraint was added, removed or modified semantically."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change: IRDeltaKind
    before_ref: str | None = Field(default=None, max_length=256)
    after_ref: str | None = Field(default=None, max_length=256)
    constraint_kind: str
    before_payload: dict[str, Any] | None = None
    after_payload: dict[str, Any] | None = None


class TransitionChange(BaseModel):
    """A transition was added, removed or modified semantically."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change: IRDeltaKind
    before_ref: str | None = Field(default=None, max_length=256)
    after_ref: str | None = Field(default=None, max_length=256)
    before_kind: str | None = None
    after_kind: str | None = None
    before_duration_us: int | None = Field(default=None, ge=0)
    after_duration_us: int | None = Field(default=None, ge=0)
    before_from_scene_ref: str | None = Field(default=None, max_length=256)
    after_from_scene_ref: str | None = Field(default=None, max_length=256)
    before_to_scene_ref: str | None = Field(default=None, max_length=256)
    after_to_scene_ref: str | None = Field(default=None, max_length=256)


class TextChange(BaseModel):
    """A text layer's explicit text changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    before_ref: str | None = Field(default=None, max_length=256)
    after_ref: str | None = Field(default=None, max_length=256)
    role: str
    before_text: str
    after_text: str


class AudioChange(BaseModel):
    """A media layer's audio intent changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    before_ref: str | None = Field(default=None, max_length=256)
    after_ref: str | None = Field(default=None, max_length=256)
    role: str
    before_payload: dict[str, Any] | None = None
    after_payload: dict[str, Any] | None = None


class EffectChange(BaseModel):
    """An effect's semantics changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    change: IRDeltaKind
    before_ref: str | None = Field(default=None, max_length=256)
    after_ref: str | None = Field(default=None, max_length=256)
    before_operation: str | None = None
    after_operation: str | None = None
    before_intensity: int | None = Field(default=None, ge=0, le=1000)
    after_intensity: int | None = Field(default=None, ge=0, le=1000)


class AttentionChange(BaseModel):
    """A layer's claimed attention share changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    before_ref: str | None = Field(default=None, max_length=256)
    after_ref: str | None = Field(default=None, max_length=256)
    role: str
    before_emphasis: int = Field(ge=0, le=1000)
    after_emphasis: int = Field(ge=0, le=1000)


class SemanticDiff(BaseModel):
    """Human-meaning diff derived from the normalized IR delta."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diff_id: str = Field(min_length=1, max_length=256)
    before_work_id: str = Field(min_length=1, max_length=256)
    after_work_id: str = Field(min_length=1, max_length=256)
    scenes: tuple[SceneChange, ...] = ()
    constraints: tuple[ConstraintChange, ...] = ()
    transitions: tuple[TransitionChange, ...] = ()
    texts: tuple[TextChange, ...] = ()
    audio: tuple[AudioChange, ...] = ()
    effects: tuple[EffectChange, ...] = ()
    attention: tuple[AttentionChange, ...] = ()


class WorkDiff(BaseModel):
    """The full diff contract: normalized IR delta plus semantic statements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ir_delta: IRDelta
    semantic_diff: SemanticDiff


class _PairState(BaseModel):
    """Internal deterministic pairing state for one node kind."""

    model_config = ConfigDict(extra="forbid")

    pairs: list[tuple[Any, Any]] = Field(default_factory=list)
    remaining_before: list[Any] = Field(default_factory=list)
    remaining_after: list[Any] = Field(default_factory=list)


def diff_works(
    before: CreativeWork, after: CreativeWork, *, lineage: DiffLineage | None = None
) -> WorkDiff:
    """Return the normalized IR delta and semantic diff between two works.

    The function is deterministic and fail-closed. It trusts only three facts:

    * identical ids mean identical content;
    * explicit lineage supplied by the caller;
    * for transitions / constraints only, a normalized semantic payload means
      the change was reference churn rather than a semantic edit.
    """
    line = lineage or DiffLineage()
    _validate_lineage(before, after, line)
    if before.work_id == after.work_id:
        empty = SemanticDiff(
            diff_id=content_id(
                "dif",
                {"before": before.work_id, "after": after.work_id, "changes": {}},
            ),
            before_work_id=before.work_id,
            after_work_id=after.work_id,
        )
        return WorkDiff(ir_delta=IRDelta(), semantic_diff=empty)

    scene_pairs = _pair_nodes(
        list(before.scenes),
        list(after.scenes),
        line,
        kind="scene",
    )
    layer_pairs = _pair_nodes(
        list(before.all_layers()),
        list(after.all_layers()),
        line,
        kind="layer",
    )
    effect_pairs = _pair_nodes(
        list(before.effects),
        list(after.effects),
        line,
        kind="effect",
    )
    transition_pairs = _pair_nodes(
        list(before.transitions),
        list(after.transitions),
        line,
        kind="transition",
        signature=lambda t: _transition_signature(t, line.after_to_before("scene")),
    )
    constraint_pairs = _pair_nodes(
        list(before.constraints),
        list(after.constraints),
        line,
        kind="constraint",
        signature=lambda c: _constraint_signature(c, line.after_to_before("scene")),
    )

    added: list[IRDeltaEntry] = []
    removed: list[IRDeltaEntry] = []
    modified: list[IRDeltaEntry] = []
    scenes: list[SceneChange] = []
    constraints: list[ConstraintChange] = []
    transitions: list[TransitionChange] = []
    texts: list[TextChange] = []
    audio: list[AudioChange] = []
    effects: list[EffectChange] = []
    attention: list[AttentionChange] = []

    if before.work_id != after.work_id:
        modified.append(
            IRDeltaEntry(
                change=IRDeltaKind.MODIFIED,
                node_kind=NodeKind.WORK,
                before_ref=before.work_id,
                after_ref=after.work_id,
            )
        )

    for old, new in scene_pairs.pairs:
        if old.scene_id != new.scene_id:
            modified.append(
                IRDeltaEntry(
                    change=IRDeltaKind.MODIFIED,
                    node_kind=NodeKind.SCENE,
                    before_ref=old.scene_id,
                    after_ref=new.scene_id,
                )
            )
        if old.semantic_payload() != new.semantic_payload():
            scenes.append(
                SceneChange(
                    change=IRDeltaKind.MODIFIED,
                    before_ref=old.scene_id,
                    after_ref=new.scene_id,
                    before_label=old.label,
                    after_label=new.label,
                    before_narrative_role=old.narrative_role.value,
                    after_narrative_role=new.narrative_role.value,
                    before_start_us=old.timing.start_us,
                    after_start_us=new.timing.start_us,
                    before_duration_us=old.timing.duration_us,
                    after_duration_us=new.timing.duration_us,
                )
            )
    for old in scene_pairs.remaining_before:
        removed.append(
            IRDeltaEntry(
                change=IRDeltaKind.REMOVED,
                node_kind=NodeKind.SCENE,
                before_ref=old.scene_id,
            )
        )
        scenes.append(
            SceneChange(
                change=IRDeltaKind.REMOVED,
                before_ref=old.scene_id,
                before_label=old.label,
                before_narrative_role=old.narrative_role.value,
                before_start_us=old.timing.start_us,
                before_duration_us=old.timing.duration_us,
            )
        )
    for new in scene_pairs.remaining_after:
        added.append(
            IRDeltaEntry(
                change=IRDeltaKind.ADDED,
                node_kind=NodeKind.SCENE,
                after_ref=new.scene_id,
            )
        )
        scenes.append(
            SceneChange(
                change=IRDeltaKind.ADDED,
                after_ref=new.scene_id,
                after_label=new.label,
                after_narrative_role=new.narrative_role.value,
                after_start_us=new.timing.start_us,
                after_duration_us=new.timing.duration_us,
            )
        )

    for old, new in layer_pairs.pairs:
        if old.layer_id != new.layer_id:
            modified.append(
                IRDeltaEntry(
                    change=IRDeltaKind.MODIFIED,
                    node_kind=NodeKind.LAYER,
                    before_ref=old.layer_id,
                    after_ref=new.layer_id,
                )
            )
        _collect_layer_semantics(old, new, texts, audio, attention)
    for old in layer_pairs.remaining_before:
        removed.append(
            IRDeltaEntry(
                change=IRDeltaKind.REMOVED,
                node_kind=NodeKind.LAYER,
                before_ref=old.layer_id,
            )
        )
    for new in layer_pairs.remaining_after:
        added.append(
            IRDeltaEntry(
                change=IRDeltaKind.ADDED,
                node_kind=NodeKind.LAYER,
                after_ref=new.layer_id,
            )
        )

    for old, new in effect_pairs.pairs:
        if old.effect_id != new.effect_id or old.semantic_payload() != new.semantic_payload():
            modified.append(
                IRDeltaEntry(
                    change=IRDeltaKind.MODIFIED,
                    node_kind=NodeKind.EFFECT,
                    before_ref=old.effect_id,
                    after_ref=new.effect_id,
                )
            )
        if old.semantic_payload() != new.semantic_payload():
            effects.append(
                EffectChange(
                    change=IRDeltaKind.MODIFIED,
                    before_ref=old.effect_id,
                    after_ref=new.effect_id,
                    before_operation=old.operation,
                    after_operation=new.operation,
                    before_intensity=old.intensity,
                    after_intensity=new.intensity,
                )
            )
    for old in effect_pairs.remaining_before:
        removed.append(
            IRDeltaEntry(
                change=IRDeltaKind.REMOVED,
                node_kind=NodeKind.EFFECT,
                before_ref=old.effect_id,
            )
        )
        effects.append(
            EffectChange(
                change=IRDeltaKind.REMOVED,
                before_ref=old.effect_id,
                before_operation=old.operation,
                before_intensity=old.intensity,
            )
        )
    for new in effect_pairs.remaining_after:
        added.append(
            IRDeltaEntry(
                change=IRDeltaKind.ADDED,
                node_kind=NodeKind.EFFECT,
                after_ref=new.effect_id,
            )
        )
        effects.append(
            EffectChange(
                change=IRDeltaKind.ADDED,
                after_ref=new.effect_id,
                after_operation=new.operation,
                after_intensity=new.intensity,
            )
        )

    scene_after_to_before = line.after_to_before("scene")
    for old, new in transition_pairs.pairs:
        if old.transition_id != new.transition_id and (
            _transition_signature(old, {}) != _transition_signature(new, scene_after_to_before)
            or old.transition_id in line.before_to_after("transition")
        ):
            modified.append(
                IRDeltaEntry(
                    change=IRDeltaKind.MODIFIED,
                    node_kind=NodeKind.TRANSITION,
                    before_ref=old.transition_id,
                    after_ref=new.transition_id,
                )
            )
        if _transition_signature(old, {}) != _transition_signature(new, scene_after_to_before):
            transitions.append(
                TransitionChange(
                    change=IRDeltaKind.MODIFIED,
                    before_ref=old.transition_id,
                    after_ref=new.transition_id,
                    before_kind=old.kind.value,
                    after_kind=new.kind.value,
                    before_duration_us=old.duration_us,
                    after_duration_us=new.duration_us,
                    before_from_scene_ref=old.from_scene_ref,
                    after_from_scene_ref=scene_after_to_before.get(
                        new.from_scene_ref, new.from_scene_ref
                    ),
                    before_to_scene_ref=old.to_scene_ref,
                    after_to_scene_ref=scene_after_to_before.get(
                        new.to_scene_ref, new.to_scene_ref
                    ),
                )
            )
    for old in transition_pairs.remaining_before:
        removed.append(
            IRDeltaEntry(
                change=IRDeltaKind.REMOVED,
                node_kind=NodeKind.TRANSITION,
                before_ref=old.transition_id,
            )
        )
        transitions.append(
            TransitionChange(
                change=IRDeltaKind.REMOVED,
                before_ref=old.transition_id,
                before_kind=old.kind.value,
                before_duration_us=old.duration_us,
                before_from_scene_ref=old.from_scene_ref,
                before_to_scene_ref=old.to_scene_ref,
            )
        )
    for new in transition_pairs.remaining_after:
        added.append(
            IRDeltaEntry(
                change=IRDeltaKind.ADDED,
                node_kind=NodeKind.TRANSITION,
                after_ref=new.transition_id,
            )
        )
        transitions.append(
            TransitionChange(
                change=IRDeltaKind.ADDED,
                after_ref=new.transition_id,
                after_kind=new.kind.value,
                after_duration_us=new.duration_us,
                after_from_scene_ref=scene_after_to_before.get(
                    new.from_scene_ref, new.from_scene_ref
                ),
                after_to_scene_ref=scene_after_to_before.get(new.to_scene_ref, new.to_scene_ref),
            )
        )

    for old, new in constraint_pairs.pairs:
        old_sig = _constraint_signature(old, {})
        new_sig = _constraint_signature(new, scene_after_to_before)
        if old.constraint_id != new.constraint_id and (
            old_sig != new_sig or old.constraint_id in line.before_to_after("constraint")
        ):
            modified.append(
                IRDeltaEntry(
                    change=IRDeltaKind.MODIFIED,
                    node_kind=NodeKind.CONSTRAINT,
                    before_ref=old.constraint_id,
                    after_ref=new.constraint_id,
                )
            )
        if old_sig != new_sig:
            constraints.append(
                ConstraintChange(
                    change=IRDeltaKind.MODIFIED,
                    before_ref=old.constraint_id,
                    after_ref=new.constraint_id,
                    constraint_kind=old.spec.kind,
                    before_payload=_normalized_constraint_payload(old, {}),
                    after_payload=_normalized_constraint_payload(new, scene_after_to_before),
                )
            )
    for old in constraint_pairs.remaining_before:
        removed.append(
            IRDeltaEntry(
                change=IRDeltaKind.REMOVED,
                node_kind=NodeKind.CONSTRAINT,
                before_ref=old.constraint_id,
            )
        )
        constraints.append(
            ConstraintChange(
                change=IRDeltaKind.REMOVED,
                before_ref=old.constraint_id,
                constraint_kind=old.spec.kind,
                before_payload=_normalized_constraint_payload(old, {}),
            )
        )
    for new in constraint_pairs.remaining_after:
        added.append(
            IRDeltaEntry(
                change=IRDeltaKind.ADDED,
                node_kind=NodeKind.CONSTRAINT,
                after_ref=new.constraint_id,
            )
        )
        constraints.append(
            ConstraintChange(
                change=IRDeltaKind.ADDED,
                after_ref=new.constraint_id,
                constraint_kind=new.spec.kind,
                after_payload=_normalized_constraint_payload(new, scene_after_to_before),
            )
        )

    semantic_payload = {
        "before_work_id": before.work_id,
        "after_work_id": after.work_id,
        "scenes": [change.model_dump(mode="json") for change in scenes],
        "constraints": [change.model_dump(mode="json") for change in constraints],
        "transitions": [change.model_dump(mode="json") for change in transitions],
        "texts": [change.model_dump(mode="json") for change in texts],
        "audio": [change.model_dump(mode="json") for change in audio],
        "effects": [change.model_dump(mode="json") for change in effects],
        "attention": [change.model_dump(mode="json") for change in attention],
    }
    semantic = SemanticDiff(
        diff_id=content_id("dif", semantic_payload),
        before_work_id=before.work_id,
        after_work_id=after.work_id,
        scenes=tuple(scenes),
        constraints=tuple(constraints),
        transitions=tuple(transitions),
        texts=tuple(texts),
        audio=tuple(audio),
        effects=tuple(effects),
        attention=tuple(attention),
    )
    return WorkDiff(
        ir_delta=IRDelta(
            added=tuple(added),
            removed=tuple(removed),
            modified=tuple(modified),
        ),
        semantic_diff=semantic,
    )


def _validate_lineage(before: CreativeWork, after: CreativeWork, lineage: DiffLineage) -> None:
    """Every lineage pair must name real nodes and must not duplicate either side."""
    before_index = {
        "scene": {scene.scene_id for scene in before.scenes},
        "layer": {layer.layer_id for layer in before.all_layers()},
        "transition": {transition.transition_id for transition in before.transitions},
        "constraint": {constraint.constraint_id for constraint in before.constraints},
        "effect": {effect.effect_id for effect in before.effects},
    }
    after_index = {
        "scene": {scene.scene_id for scene in after.scenes},
        "layer": {layer.layer_id for layer in after.all_layers()},
        "transition": {transition.transition_id for transition in after.transitions},
        "constraint": {constraint.constraint_id for constraint in after.constraints},
        "effect": {effect.effect_id for effect in after.effects},
    }
    seen_before: dict[str, set[str]] = {kind: set() for kind in before_index}
    seen_after: dict[str, set[str]] = {kind: set() for kind in before_index}
    for pair in lineage.pairs:
        if pair.before_ref not in before_index[pair.kind]:
            raise DiffLineageError(
                f"lineage names missing before {pair.kind} ref {pair.before_ref!r}"
            )
        if pair.after_ref not in after_index[pair.kind]:
            raise DiffLineageError(
                f"lineage names missing after {pair.kind} ref {pair.after_ref!r}"
            )
        if pair.before_ref in seen_before[pair.kind]:
            raise DiffLineageError(
                f"duplicate before lineage for {pair.kind} ref {pair.before_ref!r}"
            )
        if pair.after_ref in seen_after[pair.kind]:
            raise DiffLineageError(
                f"duplicate after lineage for {pair.kind} ref {pair.after_ref!r}"
            )
        seen_before[pair.kind].add(pair.before_ref)
        seen_after[pair.kind].add(pair.after_ref)


def _pair_nodes(
    before_items: list[Any],
    after_items: list[Any],
    lineage: DiffLineage,
    *,
    kind: Literal["scene", "layer", "transition", "constraint", "effect"],
    signature: Any | None = None,
) -> _PairState:
    """Deterministically pair same-id, lineage-linked and signature-equal nodes."""
    state = _PairState(
        remaining_before=list(before_items),
        remaining_after=list(after_items),
    )
    before_by_id = {getattr(item, _id_attr(kind)): item for item in before_items}
    after_by_id = {getattr(item, _id_attr(kind)): item for item in after_items}

    # 1. explicit lineage -- caller provenance wins
    for pair in lineage.pairs:
        if pair.kind != kind:
            continue
        before_item = before_by_id[pair.before_ref]
        after_item = after_by_id[pair.after_ref]
        state.pairs.append((before_item, after_item))
        state.remaining_before.remove(before_item)
        state.remaining_after.remove(after_item)

    # 2. identical ids not already consumed
    consumed_before = {getattr(item, _id_attr(kind)) for item, _item2 in state.pairs}
    consumed_after = {getattr(item2, _id_attr(kind)) for _item1, item2 in state.pairs}
    for ref in sorted(set(before_by_id) & set(after_by_id)):
        if ref in consumed_before or ref in consumed_after:
            continue
        before_item = before_by_id[ref]
        after_item = after_by_id[ref]
        state.pairs.append((before_item, after_item))
        state.remaining_before.remove(before_item)
        state.remaining_after.remove(after_item)

    # 3. semantic signature for unchanged transitions / constraints whose ids
    # changed only because a referenced scene was re-sealed.
    if signature is not None:
        buckets: dict[str, list[Any]] = {}
        for item in state.remaining_before:
            buckets.setdefault(signature(item), []).append(item)
        keep_before: list[Any] = []
        new_pairs: list[tuple[Any, Any]] = []
        unmatched_after: list[Any] = []
        for item in state.remaining_after:
            key = signature(item)
            bucket = buckets.get(key)
            if bucket:
                new_pairs.append((bucket.pop(0), item))
                if not bucket:
                    del buckets[key]
            else:
                unmatched_after.append(item)
        for leftovers in buckets.values():
            keep_before.extend(leftovers)
        state.pairs.extend(new_pairs)
        state.remaining_before = keep_before
        state.remaining_after = unmatched_after

    return state


def _collect_layer_semantics(
    before: Layer,
    after: Layer,
    texts: list[TextChange],
    audio: list[AudioChange],
    attention: list[AttentionChange],
) -> None:
    if before.emphasis != after.emphasis:
        attention.append(
            AttentionChange(
                before_ref=before.layer_id,
                after_ref=after.layer_id,
                role=before.role.value,
                before_emphasis=before.emphasis,
                after_emphasis=after.emphasis,
            )
        )
    if isinstance(before.content, TextContent) and isinstance(after.content, TextContent):
        if before.content.text.text != after.content.text.text:
            texts.append(
                TextChange(
                    before_ref=before.layer_id,
                    after_ref=after.layer_id,
                    role=before.role.value,
                    before_text=before.content.text.text,
                    after_text=after.content.text.text,
                )
            )
        return
    if isinstance(before.content, MediaContent) and isinstance(after.content, MediaContent):
        before_audio = before.content.audio.semantic_payload() if before.content.audio else None
        after_audio = after.content.audio.semantic_payload() if after.content.audio else None
        if before_audio != after_audio:
            audio.append(
                AudioChange(
                    before_ref=before.layer_id,
                    after_ref=after.layer_id,
                    role=before.role.value,
                    before_payload=before_audio,
                    after_payload=after_audio,
                )
            )


def _id_attr(kind: str) -> str:
    return {
        "scene": "scene_id",
        "layer": "layer_id",
        "transition": "transition_id",
        "constraint": "constraint_id",
        "effect": "effect_id",
    }[kind]


def _normalize_target(
    target: ConstraintTarget, scene_after_to_before: dict[str, str]
) -> dict[str, Any]:
    return {
        "kind": target.kind,
        "ref": scene_after_to_before.get(target.ref, target.ref) if target.ref else None,
        "role": target.role.value if target.role else None,
    }


def _normalized_constraint_payload(
    constraint: Constraint, scene_after_to_before: dict[str, str]
) -> dict[str, Any]:
    spec = constraint.spec
    if isinstance(spec, TimingConstraint):
        normalized_spec = {
            "kind": spec.kind,
            "target": _normalize_target(spec.target, scene_after_to_before),
            "min_us": spec.min_us,
            "max_us": spec.max_us,
        }
    elif isinstance(spec, PacingConstraint):
        normalized_spec = {
            "kind": spec.kind,
            "max_scene_duration_us": spec.max_scene_duration_us,
            "min_scene_count": spec.min_scene_count,
        }
    elif isinstance(spec, OrderConstraint):
        normalized_spec = {
            "kind": spec.kind,
            "before": _normalize_target(spec.before, scene_after_to_before),
            "after": _normalize_target(spec.after, scene_after_to_before),
        }
    elif isinstance(spec, ExclusionConstraint):
        normalized_spec = {
            "kind": spec.kind,
            "target": _normalize_target(spec.target, scene_after_to_before),
            "forbidden": list(spec.forbidden),
        }
    elif isinstance(spec, EmphasisConstraint):
        normalized_spec = {
            "kind": spec.kind,
            "target": _normalize_target(spec.target, scene_after_to_before),
            "min_share_permille": spec.min_share_permille,
        }
    elif isinstance(spec, QualityConstraint):
        normalized_spec = {
            "kind": spec.kind,
            "min_width_px": spec.min_width_px,
            "min_height_px": spec.min_height_px,
            "min_frame_rate_milli": spec.min_frame_rate_milli,
            "max_duration_us": spec.max_duration_us,
        }
    else:  # pragma: no cover - closed union
        raise DiffLineageError(f"unsupported constraint spec {type(spec).__name__}")
    return {
        "priority": constraint.priority.value,
        "spec": normalized_spec,
        "origin": constraint.origin.semantic_payload(),
    }


def _constraint_signature(constraint: Constraint, scene_after_to_before: dict[str, str]) -> str:
    return canonical_json(_normalized_constraint_payload(constraint, scene_after_to_before))


def _transition_payload(
    transition: Transition, scene_after_to_before: dict[str, str]
) -> dict[str, Any]:
    return {
        "kind": transition.kind.value,
        "duration_us": transition.duration_us,
        "from_scene_ref": scene_after_to_before.get(
            transition.from_scene_ref, transition.from_scene_ref
        ),
        "to_scene_ref": scene_after_to_before.get(transition.to_scene_ref, transition.to_scene_ref),
        "origin": transition.origin.semantic_payload(),
    }


def _transition_signature(transition: Transition, scene_after_to_before: dict[str, str]) -> str:
    return canonical_json(_transition_payload(transition, scene_after_to_before))
