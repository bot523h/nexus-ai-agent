"""Deterministic render-plan bridge: ``Project`` timeline state → ``LaneIR`` segments.

Answer to the mission's compiler question (§9) with the evidence in the module
docstring:

* :func:`~nexus_ai_agent.creative.rendering.ir.lane_ir_from_project` already
  *binds* assets + staged paths + caller-chosen :class:`LaneOp` values into the
  one canonical render lane (``LaneIR → filtergraph → argv → FFmpeg``);
* **nothing derived the ops from the timeline state** — a project edited by the
  pure packs could not become an execution plan without a human re-typing the
  ops.  This module is the minimal missing step: a pure, deterministic
  ``compile_execution_plan`` over one track.

Proven limitation (deferred, not papered over): one :class:`LaneIR` compiles
exactly **one main source** plus xfade/duck extras — the IR has no concat op, so
a K-clip track yields K segment IRs and ``concat_required=True``.  Segment
assembly is the documented next step (task in ``next_work``); nothing here
forks a second pipeline in the meantime — segments are compiled by the *same*
compiler and executed by the *same* executor.

Determinism contract: same project + same track → identical segments and
identical ``plan_hash`` (canonical JSON, sorted keys, integer microseconds).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from nexus_ai_agent.creative.rendering.ir import (
    AssemblyGap,
    ExposureOp,
    LaneAssembly,
    LaneError,
    LaneIR,
    LaneProfile,
    lane_ir_from_project,
)

#: Effects with a 1:1 executable twin in the lane IR (per ir.py docstrings).
#: ``ExposureOp`` is explicitly "the executable twin of ``color.adjust_exposure``".
EFFECT_TO_LANE_OP: dict[str, str] = {
    "color.adjust_exposure": "exposure",
}


class PlanError(LaneError):
    """The timeline cannot be compiled into a render plan without corruption."""


@dataclass(frozen=True)
class SegmentPlan:
    """One clip window compiled into ordered lane ops (trim + mapped effects)."""

    clip_id: str
    asset_id: str
    source_in_us: int
    source_out_us: int
    timeline_start_us: int
    timeline_end_us: int
    ops: tuple[Any, ...]
    unmapped_effects: tuple[str, ...]

    @property
    def trim_us(self) -> int:
        return self.source_out_us - self.source_in_us


@dataclass(frozen=True)
class ExecutionPlan:
    """A deterministic, inspectable plan for one timeline track."""

    track_id: str
    segments: tuple[SegmentPlan, ...]
    concat_required: bool
    unmapped_effects: tuple[str, ...]
    plan_hash: str

    def to_json(self) -> str:
        """Canonical JSON of the plan content (ops included) for hashing/tests."""
        payload = {
            "track_id": self.track_id,
            "concat_required": self.concat_required,
            "segments": [
                {
                    "clip_id": segment.clip_id,
                    "asset_id": segment.asset_id,
                    "source_in_us": segment.source_in_us,
                    "source_out_us": segment.source_out_us,
                    "timeline_start_us": segment.timeline_start_us,
                    "timeline_end_us": segment.timeline_end_us,
                    "ops": [op.model_dump(mode="json") for op in segment.ops],
                    "unmapped_effects": list(segment.unmapped_effects),
                }
                for segment in self.segments
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _map_effect(effect: Any) -> tuple[object | None, str | None]:
    """Map one ``EffectLayerRef`` onto a lane op, or report it as unmapped."""
    operation = effect.operation
    twin = EFFECT_TO_LANE_OP.get(operation)
    if twin == "exposure":
        params = effect.parameters or {}
        return (
            ExposureOp(
                exposure_ev=float(params.get("exposure_ev", 0.0)),
                contrast=float(params.get("contrast", 1.0)),
                temperature_k=int(params.get("temperature_k", 6500)),
                tint=float(params.get("tint", 0.0)),
            ),
            None,
        )
    return None, operation


def compile_execution_plan(project: Any, *, track_id: str) -> ExecutionPlan:
    """Compile one track of the project timeline into a deterministic plan."""
    timeline = project.timeline
    track = next((t for t in timeline.tracks if t.track_id == track_id), None)
    if track is None:
        raise PlanError(f"unknown track: {track_id!r}")

    segments: list[SegmentPlan] = []
    unmapped: list[str] = []
    cursor_us = 0
    for clip in sorted(track.clips, key=lambda c: (c.timeline_range.start_us, c.clip_id)):
        placement = clip.timeline_range
        if placement.start_us < cursor_us:
            raise PlanError(
                f"track {track_id!r} clips overlap at {placement.start_us}µs (cursor {cursor_us}µs)"
            )
        cursor_us = placement.end_us

        source_in_us = clip.source_range.start_us
        source_out_us = clip.source_range.end_us
        ops: list[object] = []
        effect_unmapped: list[str] = []
        for effect in clip.effects:
            lane_op, unmapped_name = _map_effect(effect)
            if lane_op is not None:
                ops.append(lane_op)
            elif unmapped_name is not None:
                effect_unmapped.append(unmapped_name)
        segments.append(
            SegmentPlan(
                clip_id=clip.clip_id,
                asset_id=clip.media_ref.asset_id,
                source_in_us=source_in_us,
                source_out_us=source_out_us,
                timeline_start_us=placement.start_us,
                timeline_end_us=placement.end_us,
                ops=tuple(ops),
                unmapped_effects=tuple(effect_unmapped),
            )
        )
        unmapped.extend(f"{clip.clip_id}:{name}" for name in effect_unmapped)

    plan = ExecutionPlan(
        track_id=track_id,
        segments=tuple(segments),
        concat_required=len(segments) > 1,
        unmapped_effects=tuple(unmapped),
        plan_hash="",
    )
    digest = hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()
    return ExecutionPlan(
        track_id=plan.track_id,
        segments=plan.segments,
        concat_required=plan.concat_required,
        unmapped_effects=plan.unmapped_effects,
        plan_hash="sha256:" + digest,
    )


def segment_lane_ir(
    plan: ExecutionPlan,
    segment: SegmentPlan,
    media_paths: dict[str, str],
    *,
    profile: LaneProfile | None = None,
    project: Any = None,
) -> LaneIR:
    """Bind one planned segment onto the canonical lane (compile-ready LaneIR).

    The trim travels as the lane's ``TrimOp`` so the ``-t``/``-ss`` math stays in
    the lane's integer-microsecond algebra.  Multi-segment assembly is the
    documented limitation (``plan.concat_required``).
    """
    from nexus_ai_agent.creative.rendering.ir import TrimOp

    if project is None:
        raise PlanError("segment_lane_ir needs the source project for asset binding")
    ops: list[Any] = [TrimOp(in_us=segment.source_in_us, out_us=segment.source_out_us)]
    ops.extend(segment.ops)
    return lane_ir_from_project(project, media_paths, segment.asset_id, ops, profile)


def assemble_execution_plan(
    plan: ExecutionPlan,
    media_paths: dict[str, str],
    *,
    project: Any,
    profile: LaneProfile | None = None,
) -> LaneAssembly:
    """Bind every planned segment into one timeline-ordered :class:`LaneAssembly`.

    Segments keep their trim + mapped effects; the editorial space *between*
    two consecutive segments becomes an :class:`AssemblyGap` (black video +
    silence), so ``Segment A, Gap, Segment B`` renders with its timeline
    positions intact.  Adjacent segments (zero space) produce no gap piece.

    The assembly compiles through :func:`compile_assembly` and executes through
    the unchanged :func:`encode_lane` — one filtergraph, one process, no second
    pipeline.  This closes the ``concat_required`` limitation the plan bridge
    proved: ``concat_required=True`` now means "assemble, then render".
    """
    if not plan.segments:
        raise PlanError(f"track {plan.track_id!r} has no segments to assemble")
    lane_profile = profile or LaneProfile()
    pieces: list[LaneIR | AssemblyGap] = []
    previous_end_us: int | None = None
    for segment in plan.segments:
        if previous_end_us is not None:
            gap_us = segment.timeline_start_us - previous_end_us
            if gap_us < 0:
                raise PlanError(
                    f"track {plan.track_id!r} segments overlap at "
                    f"{segment.timeline_start_us}µs (previous ends {previous_end_us}µs)"
                )
            if gap_us > 0:
                pieces.append(AssemblyGap(duration_us=gap_us))
        pieces.append(
            segment_lane_ir(plan, segment, media_paths, profile=lane_profile, project=project)
        )
        previous_end_us = segment.timeline_end_us
    return LaneAssembly(pieces=tuple(pieces), profile=lane_profile)


__all__ = [
    "EFFECT_TO_LANE_OP",
    "ExecutionPlan",
    "PlanError",
    "SegmentPlan",
    "assemble_execution_plan",
    "compile_execution_plan",
    "segment_lane_ir",
]
