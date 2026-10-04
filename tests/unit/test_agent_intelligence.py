"""Typed Agent-intelligence boundaries and fail-closed compilation tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.render_contracts import CreativeRenderPayload
from nexus_ai_agent.creative.render_jobs import CreativeRenderError, _seconds_to_us
from nexus_ai_agent.creative.spine.authoring import AuthoringError, build_creative_work
from nexus_ai_agent.creative.spine.compiler import CreativeWorkCompiler, request_idempotency_key
from nexus_ai_agent.creative.spine.intent import LLMIntentResolver
from nexus_ai_agent.creative.spine.models import Intent, IntentSourceRange
from nexus_ai_agent.creative.spine.strategy import (
    DeterministicTrimStrategy,
    SourceAssetDescriptor,
    StrategyError,
)
from nexus_ai_agent.orchestration.agent_intelligence import (
    AgentInputContext,
    AgentIntelligenceRuntime,
)

_SHA = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _source(duration_us: int = 2_000_000) -> SourceAssetDescriptor:
    return SourceAssetDescriptor(
        asset_id="asset_source_01",
        duration_us=duration_us,
        content_sha256=_SHA,
        width_px=320,
        height_px=240,
        frame_rate_milli=15000,
    )


def _intent(
    request_id: str = "request-001",
    source_range: IntentSourceRange | None = None,
    objective: str = "video_trim",
    confidence: float = 0.99,
    ambiguity_state: str = "clear",
    unresolved_requirements: tuple[str, ...] = (),
    semantic_intents: tuple[str, ...] = (),
    constraints: tuple[str, ...] = (),
) -> Intent:
    return Intent(
        project_id=f"shot-{request_idempotency_key(request_id)}",
        request_id=request_id,
        goal="Keep only the requested section",
        objective=objective,
        source_range=source_range,
        confidence=confidence,
        ambiguity_state=ambiguity_state,
        unresolved_requirements=unresolved_requirements,
        semantic_intents=semantic_intents,
        constraints=constraints,
        source_text="trim this video",
    )


class _Model:
    def __init__(self, response: str) -> None:
        self.response = response

    async def generate(self, prompt: str, system: str) -> str:
        return self.response


def _proposal(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "objective": "video_trim",
        "goal": "Keep the requested section",
        "source_range": {"in_point_us": 0, "out_point_us": 1_000_000},
        "confidence": 0.97,
        "ambiguity_state": "clear",
        "unresolved_requirements": [],
        "semantic_intents": [],
        "constraints": [],
    }
    value.update(overrides)
    return value


def test_llm_intent_boundary_stamps_trusted_identity() -> None:
    import asyncio

    resolver = LLMIntentResolver(_Model(json.dumps(_proposal())))
    intent = asyncio.run(
        resolver.resolve(
            "trim from zero to one second",
            project_id="trusted-project",
            request_id="trusted-request",
        )
    )

    assert intent.project_id == "trusted-project"
    assert intent.request_id == "trusted-request"
    assert intent.source_text == "trim from zero to one second"
    assert intent.confidence == pytest.approx(0.97)
    assert intent.ambiguity_state == "clear"
    assert intent.source_range == IntentSourceRange(in_point_us=0, out_point_us=1_000_000)
    assert intent.intent_id.startswith("int_")


def test_llm_intent_boundary_rejects_authority_and_duplicate_json_fields() -> None:
    import asyncio

    for untrusted_authority in (
        {"command_id": "forged"},
        {"shell": "rm -rf /"},
    ):
        with pytest.raises(ValidationError):
            asyncio.run(
                LLMIntentResolver(_Model(json.dumps(_proposal(**untrusted_authority)))).resolve(
                    "trim", project_id="p", request_id="r"
                )
            )

    duplicate = (
        '{"objective":"video_trim","goal":"trim",'
        '"source_range":{"in_point_us":0,"out_point_us":1000000},'
        '"confidence":0.9,"ambiguity_state":"clear","confidence":0.1}'
    )
    with pytest.raises(ValueError, match="strict JSON"):
        asyncio.run(
            LLMIntentResolver(_Model(duplicate)).resolve("trim", project_id="p", request_id="r")
        )


def test_llm_intent_boundary_rejects_nonfinite_and_malformed_output() -> None:
    import asyncio

    for raw in (json.dumps(_proposal(confidence=float("nan"))), "not json", "[]"):
        resolver = LLMIntentResolver(_Model(raw))
        with pytest.raises((ValueError, ValidationError)):
            asyncio.run(resolver.resolve("trim", project_id="p", request_id="r"))


def test_strategy_authoring_and_compilation_preserve_constraints_and_provenance() -> None:
    import asyncio

    source = _source()
    intent = _intent(source_range=IntentSourceRange(in_point_us=0, out_point_us=1_000_000))
    strategy = asyncio.run(DeterministicTrimStrategy().propose(intent, source))
    work = build_creative_work(intent, strategy, source)
    registry = build_runtime_registry()
    compiler = CreativeWorkCompiler()

    plan_a = compiler.compile(work, intent, registry)
    plan_b = compiler.compile(work, intent, registry)

    assert strategy.source_asset_id == source.asset_id
    assert strategy.in_point_us == intent.source_range.in_point_us
    assert strategy.out_point_us == intent.source_range.out_point_us
    work.verify_identity()
    assert work.work_id
    assert work.assert_constraints() is None
    assert work.assets[0].origin.source == "user"
    assert work.scenes[0].origin.source == "strategy"
    assert work.constraints[0].origin.source == "user"
    assert plan_a.model_dump(mode="json") == plan_b.model_dump(mode="json")
    assert plan_a.plan_id == plan_b.plan_id
    assert plan_a.work_id == work.work_id
    assert plan_a.max_replans == 0
    assert plan_a.plan[0].operation == "timeline.trim"
    assert plan_a.plan[0].input == {
        "clip_asset_id": "src",
        "in_point_us": 0,
        "out_point_us": 1_000_000,
        "output_asset_id": None,
    }
    assert request_idempotency_key(intent.request_id) == request_idempotency_key(intent.request_id)


def test_strategy_and_authoring_fail_closed_on_range_or_material_constraints() -> None:
    import asyncio

    source = _source()
    too_long = _intent(source_range=IntentSourceRange(in_point_us=0, out_point_us=3_000_000))
    with pytest.raises(StrategyError, match="no clamping"):
        asyncio.run(DeterministicTrimStrategy().propose(too_long, source))

    constrained = _intent(
        source_range=IntentSourceRange(in_point_us=0, out_point_us=1_000_000),
        constraints=("preserve the original aspect ratio",),
    )
    proposal = asyncio.run(DeterministicTrimStrategy().propose(constrained, source))
    with pytest.raises(AuthoringError, match="cannot prove additional constraints"):
        build_creative_work(constrained, proposal, source)

    intent = _intent(source_range=IntentSourceRange(in_point_us=0, out_point_us=1_000_000))
    proposal = asyncio.run(DeterministicTrimStrategy().propose(intent, source))
    changed = proposal.model_copy(update={"out_point_us": 900_000})
    with pytest.raises(AuthoringError, match="may not alter"):
        build_creative_work(intent, changed, source)


def test_unresolved_semantics_cannot_be_silently_dropped() -> None:
    import asyncio

    source = _source()
    intent = _intent(
        source_range=IntentSourceRange(in_point_us=0, out_point_us=1_000_000),
        semantic_intents=("zzzxqv unsupported editorial requirement",),
    )
    proposal = asyncio.run(DeterministicTrimStrategy().propose(intent, source))
    with pytest.raises(AuthoringError, match="remain unresolved"):
        build_creative_work(intent, proposal, source)


def test_trim_decimal_parser_is_exact_bounded_and_fail_closed() -> None:
    payload = CreativeRenderPayload(
        command="edit",
        operation="trim",
        args=["0", "1"],
        workspace_dir="/tmp/creative_test",
        input_path="/tmp/creative_test/input.mp4",
        media_duration_us=2_000_000,
        user_id=1,
        chat_id=-1001,
        idempotency_key="request-key",
    )
    assert _seconds_to_us(payload, 1, 2_000_000) == 1_000_000

    for raw in ("NaN", "Infinity", "0.0000001", "1e99999999", "not-a-number"):
        candidate = payload.model_copy(update={"args": ["0", raw]})
        with pytest.raises(CreativeRenderError) as error:
            _seconds_to_us(candidate, 1, 2_000_000)
        assert error.value.code == "invalid_request"

    with pytest.raises(ValidationError):
        CreativeRenderPayload.model_validate(
            {
                **payload.model_dump(mode="json"),
                "args": ["x" * 129],
            }
        )


def test_agent_lineage_has_a_literal_zero_replan_budget() -> None:
    from nexus_ai_agent.creative.spine.models import AgentLineageRef

    common = {
        "request_id": "r",
        "intent_id": "i",
        "strategy_id": "s",
        "work_id": "w",
        "plan_id": "p",
        "command_id": "c",
        "source_asset_id": "asset_source_01",
        "source_sha256": _SHA,
        "source_duration_us": 2_000_000,
        "expected_output_duration_us": 1_000_000,
        "intent_snapshot_json": "{}",
        "strategy_snapshot_json": "{}",
        "work_snapshot_json": "{}",
        "plan_snapshot_json": "{}",
    }
    assert AgentLineageRef(**common).max_replans == 0
    with pytest.raises(ValidationError):
        AgentLineageRef(**common, max_replans=1)


class _ClarifyingResolver:
    async def resolve(self, text: str, *, project_id: str, request_id: str) -> Intent:
        return Intent(
            project_id=project_id,
            request_id=request_id,
            goal=text,
            objective="video_trim",
            source_range=None,
            confidence=0.99,
            ambiguity_state="clarify",
            unresolved_requirements=("exact start and end points",),
        )


class _ResolverForIntent:
    def __init__(self, intent: Intent) -> None:
        self.intent = intent

    async def resolve(self, text: str, *, project_id: str, request_id: str) -> Intent:
        return self.intent


class _NoQueue:
    def __init__(self) -> None:
        self.enqueues = 0

    async def enqueue(self, **kwargs: Any) -> str:
        self.enqueues += 1
        raise AssertionError("clarification must not enqueue a job")

    async def get_status(self, job_id: str) -> Any:
        raise AssertionError("clarification must not read job status")

    async def get_result(self, job_id: str) -> Any:
        raise AssertionError("clarification must not read job result")

    async def get_result_chain(self, job_id: str) -> Any:
        raise AssertionError("clarification must not read a result chain")


def test_material_ambiguity_returns_clarification_without_queue_side_effect() -> None:
    import asyncio

    request_id = "ambiguous-request"
    key = request_idempotency_key(request_id)
    context = AgentInputContext(
        project_id=f"shot-{key}",
        source=_source(),
        workspace_dir="/tmp/creative_ambiguous",
        input_path="/tmp/creative_ambiguous/input.mp4",
        user_id=42,
        chat_id=-10042,
        lang="en",
    )
    queue = _NoQueue()
    runtime = AgentIntelligenceRuntime(
        intent_resolver=_ClarifyingResolver(),
        queue=queue,
        registry=build_runtime_registry(),
    )

    outcome = asyncio.run(runtime.run("make it better", request_id=request_id, context=context))

    assert outcome.status == "clarification_required"
    assert outcome.error_code == "unresolved_requirements"
    assert outcome.job_id is None
    assert queue.enqueues == 0


def test_unsupported_objective_fails_closed_without_queue_side_effect() -> None:
    import asyncio

    request_id = "unsupported-request"
    context = AgentInputContext(
        project_id=f"shot-{request_idempotency_key(request_id)}",
        source=_source(),
        workspace_dir="/tmp/creative_unsupported",
        input_path="/tmp/creative_unsupported/input.mp4",
        user_id=42,
        chat_id=-10042,
        lang="en",
    )
    queue = _NoQueue()
    runtime = AgentIntelligenceRuntime(
        intent_resolver=_ResolverForIntent(
            _intent(
                request_id=request_id,
                objective="unsupported",
                ambiguity_state="unsupported",
            )
        ),
        queue=queue,
        registry=build_runtime_registry(),
    )

    outcome = asyncio.run(
        runtime.run("run a shell command", request_id=request_id, context=context)
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "unsupported_objective"
    assert outcome.job_id is None
    assert queue.enqueues == 0


def test_worker_payload_rejects_agent_lineage_on_non_trim_operation() -> None:
    from nexus_ai_agent.creative.spine.models import AgentLineageRef

    lineage = AgentLineageRef(
        request_id="r",
        intent_id="i",
        strategy_id="s",
        work_id="w",
        plan_id="p",
        command_id="c",
        source_asset_id="asset_source_01",
        source_sha256=_SHA,
        source_duration_us=2_000_000,
        expected_output_duration_us=1_000_000,
        intent_snapshot_json="{}",
        strategy_snapshot_json="{}",
        work_snapshot_json="{}",
        plan_snapshot_json="{}",
    )
    with pytest.raises(ValidationError):
        CreativeRenderPayload(
            command="grade",
            operation="exposure",
            workspace_dir="/tmp/creative_test",
            user_id=1,
            chat_id=2,
            idempotency_key="request-key",
            agent_lineage=lineage,
        )


def test_worker_dispatch_refuses_an_operation_outside_its_server_grant() -> None:
    from nexus_ai_agent.creative.render_jobs import _build_project, _dispatch

    payload = CreativeRenderPayload(
        command="edit",
        operation="trim",
        workspace_dir="/tmp/creative_auth",
        user_id=1,
        chat_id=2,
        idempotency_key="request-key",
    )
    project = _build_project(payload=payload, duration_us=2_000_000, sha256=_SHA)
    with pytest.raises(CreativeRenderError, match="worker authority matrix"):
        _dispatch(
            project,
            operation="system.undo",
            input_data={},
            idempotency_key=payload.idempotency_key,
        )


def test_worker_authority_matrix_is_exactly_pinned_to_registry_permissions() -> None:
    from nexus_ai_agent.creative.render_jobs import (
        RENDER_JOB_OPERATION_PERMISSIONS,
        SURFACE_TO_CANONICAL,
    )

    registry = build_runtime_registry()
    assert set(RENDER_JOB_OPERATION_PERMISSIONS) == set(SURFACE_TO_CANONICAL.values())
    for operation, permissions in RENDER_JOB_OPERATION_PERMISSIONS.items():
        assert frozenset(registry.get_spec(operation).effective_permissions) == permissions


def test_worker_recompiles_agent_lineage_and_rejects_a_tampered_plan_snapshot() -> None:
    import asyncio

    from nexus_ai_agent.creative.render_jobs import _validate_agent_lineage
    from nexus_ai_agent.creative.spine.models import AgentLineageRef

    source = _source()
    intent = _intent(source_range=IntentSourceRange(in_point_us=0, out_point_us=1_000_000))
    strategy = asyncio.run(DeterministicTrimStrategy().propose(intent, source))
    work = build_creative_work(intent, strategy, source)
    compiled = CreativeWorkCompiler().compile(work, intent, build_runtime_registry())

    def encode(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    lineage = AgentLineageRef(
        request_id=intent.request_id,
        intent_id=intent.intent_id,
        strategy_id=strategy.strategy_id,
        work_id=work.work_id,
        plan_id=compiled.plan_id,
        command_id=compiled.plan[0].command_id,
        source_asset_id=source.asset_id,
        source_sha256=source.content_sha256,
        source_duration_us=source.duration_us,
        expected_output_duration_us=1_000_000,
        intent_snapshot_json=encode(intent.model_dump(mode="json")),
        strategy_snapshot_json=encode(strategy.model_dump(mode="json")),
        work_snapshot_json=work.to_canonical_json(),
        plan_snapshot_json=encode(compiled.model_dump(mode="json")),
    )
    payload = CreativeRenderPayload(
        command="edit",
        operation="trim",
        args=["0.000000", "1.000000"],
        workspace_dir="/tmp/creative_agent",
        input_path="/tmp/creative_agent/input.mp4",
        media_duration_us=source.duration_us,
        user_id=42,
        chat_id=4242,
        idempotency_key=request_idempotency_key(intent.request_id),
        agent_lineage=lineage,
    )
    _validate_agent_lineage(
        payload,
        canonical_id="timeline.trim",
        operation_input=compiled.plan[0].input,
        source_duration_us=source.duration_us,
        source_sha256=source.content_sha256,
    )

    tampered = json.loads(lineage.plan_snapshot_json)
    tampered["plan"][0]["input"]["out_point_us"] = 900_000
    bad_lineage = lineage.model_copy(update={"plan_snapshot_json": encode(tampered)})
    with pytest.raises(CreativeRenderError, match="snapshot"):
        _validate_agent_lineage(
            payload.model_copy(update={"agent_lineage": bad_lineage}),
            canonical_id="timeline.trim",
            operation_input=compiled.plan[0].input,
            source_duration_us=source.duration_us,
            source_sha256=source.content_sha256,
        )
