"""Deterministic, registry-backed compilation from sealed CreativeWork to plan."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable

from nexus_ai_agent.creative.intelligence.ir import (
    AssetKind,
    CreativeWork,
    MediaContent,
    TimingConstraint,
)
from nexus_ai_agent.creative.spine.models import (
    CompilationError,
    CompiledIntent,
    CreativeGraph,
    GraphNode,
    Intent,
    PlannedOperation,
    PlanProvenance,
)
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.creative.studio.models import UnknownOperationError


@runtime_checkable
class CapabilityCompiler(Protocol):
    """Compile the one canonical authoring IR against the live registry."""

    def compile(
        self, work: CreativeWork, intent: Intent, registry: CapabilityRegistry
    ) -> CompiledIntent: ...


@runtime_checkable
class IntentPlanner(Protocol):
    """Record already-compiled steps in the creative graph."""

    def plan(
        self, graph: CreativeGraph, intent_node: GraphNode, compiled: CompiledIntent
    ) -> tuple[GraphNode, ...]: ...


TRIM_OPERATION = "timeline.trim"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(prefix: str, value: object) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:32]


def request_idempotency_key(request_id: str) -> str:
    """Stable queue idempotency key, derived from trusted request identity."""
    if not request_id.strip():
        raise CompilationError("a stable request_id is required before compilation")
    return "agent-" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]


def _frame_aligned(time_us: int, fps: int) -> bool:
    frame_index = (time_us * fps + 500_000) // 1_000_000
    nearest_us = (frame_index * 1_000_000 + fps // 2) // fps
    return abs(time_us - nearest_us) <= 1


class CreativeWorkCompiler:
    """Narrow, fail-closed compiler for the registered ``timeline.trim`` slice.

    It does not authorize or execute.  The resulting single typed operation is
    handed to the existing creative job queue, whose worker validates it again
    through the canonical CommandBus and execution policy.
    """

    def compile(
        self, work: CreativeWork, intent: Intent, registry: CapabilityRegistry
    ) -> CompiledIntent:
        if intent.objective != "video_trim" or intent.ambiguity_state != "clear":
            raise CompilationError("only a clear video_trim intent may be compiled")
        if intent.unresolved_requirements:
            raise CompilationError("unresolved requirements cannot be dropped by compilation")
        if not intent.request_id:
            raise CompilationError("request identity is required for deterministic compilation")
        try:
            work.verify_identity()
            work.assert_valid()
            work.assert_constraints()
        except Exception as exc:
            raise CompilationError(f"CreativeWork validation failed: {exc}") from exc

        if work.brief.unresolved_intents:
            raise CompilationError(
                "unresolved semantic intents cannot be silently discarded: "
                + "; ".join(work.brief.unresolved_intents)
            )
        if len(work.assets) != 1 or len(work.scenes) != 1 or len(work.all_layers()) != 1:
            raise CompilationError(
                "timeline.trim slice requires one source, one scene, and one layer"
            )
        if work.effects or work.transitions:
            raise CompilationError("timeline.trim cannot satisfy effects or transitions")
        if (
            work.output.container != "mp4"
            or work.output.width_px != 1280
            or work.output.height_px != 720
            or work.output.frame_rate_milli != 30000
        ):
            raise CompilationError(
                "requested output profile is not implemented by the live trim lane"
            )

        asset = work.assets[0]
        layer = work.all_layers()[0]
        if asset.kind is not AssetKind.VIDEO:
            raise CompilationError("timeline.trim requires one video source")
        if not asset.uri.startswith("asset://"):
            raise CompilationError("source Asset must use an opaque asset:// reference")
        if not isinstance(layer.content, MediaContent) or layer.content.asset_ref != asset.asset_id:
            raise CompilationError("trim layer must reference the sole source video")
        if layer.effects:
            raise CompilationError("timeline.trim cannot realize layer effects")

        segment = layer.content.segment
        if intent.source_range is None or (
            segment.source_in_us != intent.source_range.in_point_us
            or segment.source_in_us + segment.source_duration_us != intent.source_range.out_point_us
        ):
            raise CompilationError(
                "compiled Work range does not preserve the user's explicit range"
            )
        if work.brief.goal != intent.goal or work.brief.semantic_intents != intent.semantic_intents:
            raise CompilationError("CreativeWork brief does not preserve canonical intent meaning")
        if segment.speed_milli != 1000:
            raise CompilationError("timeline.trim does not include retiming")
        if segment.source_in_us + segment.source_duration_us > asset.duration_us:
            raise CompilationError("trim source range exceeds the measured asset duration")
        if not _frame_aligned(segment.source_in_us, 30) or not _frame_aligned(
            segment.source_in_us + segment.source_duration_us, 30
        ):
            raise CompilationError(
                "trim points must align to the live 30 fps render profile; no rounding is allowed"
            )

        unsupported_constraints = [
            constraint
            for constraint in work.constraints
            if not isinstance(constraint.spec, TimingConstraint)
        ]
        if unsupported_constraints:
            kinds = sorted({constraint.spec.kind for constraint in unsupported_constraints})
            raise CompilationError(
                "trim compiler cannot prove these CreativeWork constraint kinds: "
                + ", ".join(kinds)
            )

        duration_us = segment.source_duration_us
        if work.duration_us != duration_us:
            raise CompilationError("CreativeWork timeline duration does not match trim duration")
        if work.output.max_duration_us and duration_us > work.output.max_duration_us:
            raise CompilationError("trim result would violate output.max_duration_us")

        input_model = {
            "clip_asset_id": "src",
            "in_point_us": segment.source_in_us,
            "out_point_us": segment.source_in_us + segment.source_duration_us,
        }
        try:
            spec = registry.get_spec(TRIM_OPERATION)
        except UnknownOperationError as exc:
            raise CompilationError(
                "live capability registry does not expose timeline.trim"
            ) from exc
        description = registry.describe(TRIM_OPERATION)
        if not description.available:
            raise CompilationError("timeline.trim capability is present but unavailable")
        try:
            validated = spec.input_model.model_validate(input_model)
        except Exception as exc:
            raise CompilationError(f"registry schema rejected trim input: {exc}") from exc
        operation_input = validated.model_dump(mode="json")

        key = request_idempotency_key(intent.request_id)
        command_id = f"cmd-{key}-{TRIM_OPERATION}"
        provenance = self._provenance(work)
        constraint_ids = tuple(constraint.constraint_id for constraint in work.constraints)
        step_id = _digest(
            "step_",
            {
                "work_id": work.work_id,
                "operation": TRIM_OPERATION,
                "input": operation_input,
            },
        )
        step = PlannedOperation(
            step_id=step_id,
            command_id=command_id,
            operation=TRIM_OPERATION,
            input=operation_input,
            target={"creative_work_id": work.work_id, "source_asset_id": asset.asset_id},
            depends_on=(),
            provenance_refs=tuple(
                origin.reference_id or _digest("origin_", origin.model_dump(mode="json"))
                for origin in provenance
            ),
            description="Trim the measured source to the explicit CreativeWork segment.",
        )
        plan_payload = {
            "intent_id": intent.intent_id,
            "project_id": intent.project_id,
            "work_id": work.work_id,
            "operations": [TRIM_OPERATION],
            "steps": [step.model_dump(mode="json")],
            "constraint_ids": list(constraint_ids),
            "provenance": [entry.model_dump(mode="json") for entry in provenance],
            "max_replans": 0,
        }
        return CompiledIntent(
            intent_id=intent.intent_id,
            operations=(TRIM_OPERATION,),
            plan=(step,),
            work_id=work.work_id,
            plan_id=_digest("plan_", plan_payload),
            constraint_ids=constraint_ids,
            provenance=provenance,
            max_replans=0,
            rationale="One registered timeline.trim operation preserves the sealed source range.",
        )

    @staticmethod
    def _provenance(work: CreativeWork) -> tuple[PlanProvenance, ...]:
        origins = [work.brief.origin]
        origins.extend(asset.origin for asset in work.assets)
        origins.extend(scene.origin for scene in work.scenes)
        origins.extend(layer.origin for layer in work.all_layers())
        origins.extend(constraint.origin for constraint in work.constraints)
        result: list[PlanProvenance] = []
        seen: set[str] = set()
        for origin in origins:
            entry = PlanProvenance(
                source=origin.source,
                detail=origin.detail,
                reference_id=origin.reference_id,
            )
            key = _canonical(entry.model_dump(mode="json"))
            if key not in seen:
                seen.add(key)
                result.append(entry)
        return tuple(result)


class GraphIntentPlanner:
    """Append typed intent and capability nodes without authorizing or executing."""

    def plan(
        self, graph: CreativeGraph, intent_node: GraphNode, compiled: CompiledIntent
    ) -> tuple[GraphNode, ...]:
        nodes: list[GraphNode] = []
        for step in compiled.plan:
            nodes.append(
                graph.add_node(
                    "capability",
                    label=step.operation,
                    data={
                        "plan_id": compiled.plan_id,
                        "step_id": step.step_id,
                        "command_id": step.command_id,
                        "operation": step.operation,
                        "input": step.input,
                        "depends_on": list(step.depends_on),
                        "provenance_refs": list(step.provenance_refs),
                    },
                    parents=(intent_node.node_id,),
                )
            )
        return tuple(nodes)


__all__ = [
    "CapabilityCompiler",
    "CreativeWorkCompiler",
    "GraphIntentPlanner",
    "IntentPlanner",
    "request_idempotency_key",
]
