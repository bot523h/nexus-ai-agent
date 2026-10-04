"""User-intent → CreativeWork → durable verified artifact orchestration.

This application service coordinates existing domain and job components.  It
contains no shell, filesystem, CommandBus, or authorization authority: execution
is handed to the registered creative job, and only the job queue's independent
artifact verifier can yield a successful user-facing receipt.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.creative.render_contracts import (
    CREATIVE_RENDER_JOB_TYPE,
    CreativeRenderPayload,
)
from nexus_ai_agent.creative.spine.authoring import AuthoringError, build_creative_work
from nexus_ai_agent.creative.spine.compiler import (
    CreativeWorkCompiler,
    request_idempotency_key,
)
from nexus_ai_agent.creative.spine.intent import IntentResolverPort, LLMIntentResolver
from nexus_ai_agent.creative.spine.lineage import CreativeWorkLineage
from nexus_ai_agent.creative.spine.models import (
    AgentLineageRef,
    CreativeGraph,
    Intent,
)
from nexus_ai_agent.creative.spine.strategy import (
    DeterministicTrimStrategy,
    SourceAssetDescriptor,
    StrategyError,
    StrategyProvider,
)
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry


class DurableCreativeQueue(Protocol):
    """The existing queue API used to persist, execute, and retrieve evidence."""

    async def enqueue(
        self, *, job_type: str, idempotency_key: str, payload: dict[str, object]
    ) -> str: ...

    async def get_status(self, job_id: str) -> JobStatus: ...

    async def get_result(self, job_id: str) -> dict[str, object] | None: ...

    async def get_result_chain(self, job_id: str) -> dict[str, object]: ...


class AgentInputContext(BaseModel):
    """Trusted host metadata for one staged video; LLMs never receive its paths."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=128)
    source: SourceAssetDescriptor
    workspace_dir: str = Field(min_length=1, max_length=4096)
    input_path: str = Field(min_length=1, max_length=4096)
    user_id: int = Field(ge=0)
    chat_id: int
    lang: str = Field(default="en", min_length=2, max_length=16)

    @field_validator("chat_id")
    @classmethod
    def _nonzero_chat_id(cls, value: int) -> int:
        if value == 0:
            raise ValueError("chat_id must be a Telegram chat identity")
        return value


class AgentReceipt(BaseModel):
    """User-visible receipt assembled only after queue verification succeeded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    intent_id: str
    strategy_id: str
    work_id: str
    plan_id: str
    job_id: str
    command_id: str
    transaction_id: str
    state_hash: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    execution_status: Literal["completed"]
    verification_status: Literal["verified"]
    operation: Literal["timeline.trim"]
    artifact_path: str
    artifact_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    duration_us: int = Field(gt=0)
    output_asset_id: str = Field(min_length=1)
    physical_identity: dict[str, Any]
    probe: dict[str, Any]


@dataclass(frozen=True)
class AgentOutcome:
    status: Literal["completed", "clarification_required", "failed", "pending"]
    response: str
    error_code: str | None = None
    intent: Intent | None = None
    strategy_id: str | None = None
    work_id: str | None = None
    plan_id: str | None = None
    graph_trace: dict[str, Any] | None = None
    receipt: AgentReceipt | None = None
    job_id: str | None = None

    def state_value(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "response": self.response,
            "error_code": self.error_code,
            "intent": self.intent.model_dump(mode="json") if self.intent else None,
            "strategy_id": self.strategy_id,
            "work_id": self.work_id,
            "plan_id": self.plan_id,
            "graph_trace": self.graph_trace,
            "receipt": self.receipt.model_dump(mode="json") if self.receipt else None,
            "job_id": self.job_id,
            "max_replans": 0,
        }


class AgentIntelligenceRuntime:
    """Bounded natural-language-to-artifact slice over live repository services."""

    CONFIDENCE_THRESHOLD = 0.75
    MAX_REPLANS = 0
    TERMINAL = frozenset(
        {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    )

    def __init__(
        self,
        *,
        intent_resolver: IntentResolverPort,
        queue: DurableCreativeQueue,
        registry: CapabilityRegistry,
        strategy: StrategyProvider | None = None,
        wait_timeout_s: float = 90.0,
        poll_interval_s: float = 0.05,
    ) -> None:
        if wait_timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("queue wait bounds must be positive")
        self._intent_resolver = intent_resolver
        self._queue = queue
        self._registry = registry
        self._strategy = strategy or DeterministicTrimStrategy()
        self._wait_timeout_s = wait_timeout_s
        self._poll_interval_s = poll_interval_s

    @classmethod
    def from_llm(
        cls,
        provider: object,
        queue: DurableCreativeQueue,
        *,
        registry: CapabilityRegistry,
    ) -> AgentIntelligenceRuntime:
        return cls(
            intent_resolver=LLMIntentResolver(provider),
            queue=queue,
            registry=registry,
        )

    async def run(
        self,
        text: str,
        *,
        request_id: str,
        context: AgentInputContext,
    ) -> AgentOutcome:
        """Execute one request; every non-verified path returns a non-success state."""
        if not text.strip():
            return self._failure("empty_request", "Please describe the edit you want.")
        if not request_id.strip():
            return self._failure(
                "missing_request_id", "I cannot safely process this request without a stable id."
            )
        try:
            intent = await self._intent_resolver.resolve(
                text,
                project_id=context.project_id,
                request_id=request_id,
            )
        except Exception:
            return self._failure(
                "intent_resolution_failed",
                "I could not safely interpret that request. Please state the exact edit and range.",
            )

        if intent.project_id != context.project_id or intent.request_id != request_id:
            return self._failure(
                "intent_identity_mismatch",
                "The resolved request did not match its trusted context.",
            )
        if intent.confidence < self.CONFIDENCE_THRESHOLD:
            return AgentOutcome(
                status="clarification_required",
                response=(
                    "I’m not confident about the requested edit. "
                    "Please specify an exact start and end point."
                ),
                error_code="low_confidence",
                intent=intent,
            )
        if intent.ambiguity_state == "clarify" or intent.unresolved_requirements:
            details = "; ".join(intent.unresolved_requirements[:4])
            prompt = (
                "Please clarify the missing or conflicting requirements before I edit the video."
            )
            if details:
                prompt += f" Needed: {details}."
            return AgentOutcome(
                status="clarification_required",
                response=prompt,
                error_code="unresolved_requirements",
                intent=intent,
            )
        if intent.ambiguity_state == "unsupported" or intent.objective != "video_trim":
            return AgentOutcome(
                status="failed",
                response=(
                    "This media path currently supports only an explicit video trim; "
                    "no action was run."
                ),
                error_code="unsupported_objective",
                intent=intent,
            )
        if intent.source_range is None:
            return AgentOutcome(
                status="clarification_required",
                response=(
                    "Please give an explicit start and end point for the trim. No edit was run."
                ),
                error_code="missing_source_range",
                intent=intent,
            )

        try:
            strategy = await self._strategy.propose(intent, context.source)
            work = build_creative_work(intent, strategy, context.source)
            graph = CreativeGraph(project_id=intent.project_id)
            spine = CreativeWorkLineage(graph, compiler=CreativeWorkCompiler())
            run = spine.prepare(intent, strategy, work, self._registry)
        except (StrategyError, AuthoringError, ValueError, TypeError):
            return AgentOutcome(
                status="failed",
                response=(
                    "I could not compile this request safely; no edit was queued. "
                    "Please simplify or clarify the edit."
                ),
                error_code="compilation_refused",
                intent=intent,
                strategy_id=getattr(locals().get("strategy"), "strategy_id", None),
                work_id=getattr(locals().get("work"), "work_id", None),
            )
        except Exception:
            return AgentOutcome(
                status="failed",
                response="I could not compile this request safely; no edit was queued.",
                error_code="compilation_failed",
                intent=intent,
            )

        if len(run.compiled.plan) != 1:
            return AgentOutcome(
                status="failed",
                response=(
                    "The trim strategy produced an unsupported plan shape; no edit was queued."
                ),
                error_code="invalid_plan_shape",
                intent=intent,
                strategy_id=strategy.strategy_id,
                work_id=work.work_id,
                plan_id=run.compiled.plan_id,
            )
        step = run.compiled.plan[0]
        idempotency_key = request_idempotency_key(intent.request_id)
        expected_input = {
            "clip_asset_id": "src",
            "in_point_us": strategy.in_point_us,
            "out_point_us": strategy.out_point_us,
            "output_asset_id": None,
        }
        if (
            step.operation != "timeline.trim"
            or step.input != expected_input
            or step.command_id != f"cmd-{idempotency_key}-timeline.trim"
            or run.compiled.max_replans != self.MAX_REPLANS
        ):
            return AgentOutcome(
                status="failed",
                response=(
                    "The compiled plan did not match the validated request; no edit was queued."
                ),
                error_code="plan_identity_mismatch",
                intent=intent,
                strategy_id=strategy.strategy_id,
                work_id=work.work_id,
                plan_id=run.compiled.plan_id,
            )
        lineage = AgentLineageRef(
            request_id=intent.request_id,
            intent_id=intent.intent_id,
            strategy_id=strategy.strategy_id,
            work_id=work.work_id,
            plan_id=run.compiled.plan_id,
            command_id=step.command_id,
            source_asset_id=context.source.asset_id,
            source_sha256=context.source.content_sha256,
            source_duration_us=context.source.duration_us,
            expected_output_duration_us=strategy.out_point_us - strategy.in_point_us,
            intent_snapshot_json=_canonical(intent.model_dump(mode="json")),
            strategy_snapshot_json=_canonical(strategy.model_dump(mode="json")),
            work_snapshot_json=work.to_canonical_json(),
            plan_snapshot_json=_canonical(run.compiled.model_dump(mode="json")),
            max_replans=self.MAX_REPLANS,
        )
        args = [
            _seconds_text(strategy.in_point_us),
            _seconds_text(strategy.out_point_us),
        ]
        payload_model = CreativeRenderPayload(
            command="edit",
            operation="trim",
            args=args,
            workspace_dir=context.workspace_dir,
            input_path=context.input_path,
            media_duration_us=context.source.duration_us,
            user_id=context.user_id,
            chat_id=context.chat_id,
            lang=context.lang,
            idempotency_key=idempotency_key,
            agent_lineage=lineage,
        )
        try:
            job_id = await self._queue.enqueue(
                job_type=CREATIVE_RENDER_JOB_TYPE,
                idempotency_key=idempotency_key,
                payload=payload_model.model_dump(mode="json"),
            )
        except Exception:
            return AgentOutcome(
                status="failed",
                response="The durable render queue refused this request; no success is claimed.",
                error_code="queue_enqueue_failed",
                intent=intent,
                strategy_id=strategy.strategy_id,
                work_id=work.work_id,
                plan_id=run.compiled.plan_id,
                graph_trace=_graph_trace(graph, run),
            )

        try:
            status = await self._wait_for_terminal(job_id)
        except Exception:
            return AgentOutcome(
                status="failed",
                response="The durable job status could not be confirmed; no success is claimed.",
                error_code="job_status_unavailable",
                intent=intent,
                strategy_id=strategy.strategy_id,
                work_id=work.work_id,
                plan_id=run.compiled.plan_id,
                graph_trace=_graph_trace(graph, run),
                job_id=job_id,
            )
        if status is None:
            return AgentOutcome(
                status="pending",
                response=(
                    f"The trim is queued as {job_id} and is still processing. "
                    "I’ll report success only after independent verification."
                ),
                error_code="job_pending",
                intent=intent,
                strategy_id=strategy.strategy_id,
                work_id=work.work_id,
                plan_id=run.compiled.plan_id,
                graph_trace=_graph_trace(graph, run),
                job_id=job_id,
            )
        if status is not JobStatus.COMPLETED:
            chain = await self._safe_result_chain(job_id)
            reason = str(chain.get("failure_reason") or "execution_or_verification_failed")
            return AgentOutcome(
                status="failed",
                response=(
                    f"The trim did not complete successfully ({reason}); no artifact was accepted."
                ),
                error_code="job_failed",
                intent=intent,
                strategy_id=strategy.strategy_id,
                work_id=work.work_id,
                plan_id=run.compiled.plan_id,
                graph_trace=_graph_trace(graph, run),
                job_id=job_id,
            )

        try:
            result = await self._queue.get_result(job_id)
            chain = await self._queue.get_result_chain(job_id)
        except Exception:
            return AgentOutcome(
                status="failed",
                response=(
                    "The job completed but its durable evidence could not be read; "
                    "no success is claimed."
                ),
                error_code="durable_evidence_unavailable",
                intent=intent,
                strategy_id=strategy.strategy_id,
                work_id=work.work_id,
                plan_id=run.compiled.plan_id,
                graph_trace=_graph_trace(graph, run),
                job_id=job_id,
            )
        try:
            receipt, verification = self._verified_receipt(
                result=result,
                chain=chain,
                expected_lineage=lineage,
                intent=intent,
                strategy_id=strategy.strategy_id,
                work_id=work.work_id,
                plan_id=run.compiled.plan_id,
                expected_command_id=step.command_id,
                job_id=job_id,
            )
        except Exception:
            return AgentOutcome(
                status="failed",
                response=(
                    "The render finished, but its independent evidence did not match the request. "
                    "No success is claimed."
                ),
                error_code="receipt_verification_mismatch",
                intent=intent,
                strategy_id=strategy.strategy_id,
                work_id=work.work_id,
                plan_id=run.compiled.plan_id,
                graph_trace=_graph_trace(graph, run),
                job_id=job_id,
            )

        spine.record_receipt(
            run,
            receipt=receipt.model_dump(mode="json"),
            verification=verification,
        )
        return AgentOutcome(
            status="completed",
            response=(
                f"Verified trim complete — {receipt.duration_us / 1_000_000:.3f}s, "
                f"SHA-256 {receipt.artifact_sha256}, job {receipt.job_id}."
            ),
            intent=intent,
            strategy_id=strategy.strategy_id,
            work_id=work.work_id,
            plan_id=run.compiled.plan_id,
            graph_trace=_graph_trace(graph, run),
            receipt=receipt,
            job_id=job_id,
        )

    async def _wait_for_terminal(self, job_id: str) -> JobStatus | None:
        deadline = time.monotonic() + self._wait_timeout_s
        while time.monotonic() < deadline:
            status = await self._queue.get_status(job_id)
            if status in self.TERMINAL:
                return status
            await asyncio.sleep(self._poll_interval_s)
        return None

    async def _safe_result_chain(self, job_id: str) -> dict[str, object]:
        try:
            return await self._queue.get_result_chain(job_id)
        except Exception:
            return {}

    @staticmethod
    def _verified_receipt(
        *,
        result: dict[str, object] | None,
        chain: dict[str, object],
        expected_lineage: AgentLineageRef,
        intent: Intent,
        strategy_id: str,
        work_id: str,
        plan_id: str,
        expected_command_id: str,
        job_id: str,
    ) -> tuple[AgentReceipt, dict[str, Any]]:
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ValueError("successful handler result is absent")
        verification = result.get("artifact_verification")
        if not isinstance(verification, dict) or verification.get("status") != "verified":
            raise ValueError("queue-owned artifact verification is absent or failed")
        if chain.get("verification_status") != "verified":
            raise ValueError("durable result chain does not report verified")
        if (
            chain.get("job_id") != job_id
            or chain.get("execution_status") != JobStatus.COMPLETED.value
        ):
            raise ValueError("durable result chain does not match completed job")
        if chain.get("command_id") != expected_command_id:
            raise ValueError("durable command identity differs from compiled plan")
        if (
            chain.get("operation_id") != "timeline.trim"
            or result.get("operation") != "timeline.trim"
        ):
            raise ValueError("durable operation identity differs from compiled plan")
        if result.get("command_id") != expected_command_id:
            raise ValueError("worker command identity differs from compiled plan")
        if result.get("agent_lineage") != expected_lineage.model_dump(mode="json"):
            raise ValueError("persisted request lineage differs from the compiled request")
        if expected_lineage.intent_id != intent.intent_id:
            raise ValueError("intent identity was lost")
        if expected_lineage.strategy_id != strategy_id or expected_lineage.work_id != work_id:
            raise ValueError("strategy or Work identity was lost")
        if expected_lineage.plan_id != plan_id:
            raise ValueError("plan identity was lost")

        physical = verification.get("physical_identity")
        logical = verification.get("logical_identity")
        spec = verification.get("spec_identity")
        probe = verification.get("probe")
        if not all(isinstance(item, dict) for item in (physical, logical, spec, probe)):
            raise ValueError("verification identities or media probe are missing")
        if spec.get("operation") != "timeline.trim":
            raise ValueError("verifier measured an unexpected operation")
        for key in ("physical_identity", "logical_identity", "spec_identity", "probe"):
            chain_value = chain.get(key)
            verification_value = {
                "physical_identity": physical,
                "logical_identity": logical,
                "spec_identity": spec,
                "probe": probe,
            }[key]
            if chain_value != verification_value:
                raise ValueError(f"durable result chain disagrees on {key}")
        expected_project = f"shot-{request_idempotency_key(intent.request_id)}"
        if logical.get("project_id") != expected_project:
            raise ValueError("verifier logical project identity is unexpected")
        sha = result.get("sha256")
        measured_sha = verification.get("sha256")
        if not isinstance(sha, str) or sha != measured_sha:
            raise ValueError("handler and verifier artifact hashes disagree")
        if chain.get("sha256") != sha:
            raise ValueError("durable result chain and handler artifact hashes disagree")
        size = result.get("size_bytes")
        measured_size = verification.get("size_bytes")
        if not isinstance(size, int) or size <= 0 or size != measured_size:
            raise ValueError("handler and verifier artifact sizes disagree")
        if physical.get("sha256") != sha or physical.get("size_bytes") != size:
            raise ValueError("physical identity does not match measured artifact facts")
        probe_duration = probe.get("duration_us")
        duration = result.get("duration_us")
        if not isinstance(duration, int) or duration <= 0 or probe_duration != duration:
            raise ValueError("handler and verifier media durations disagree")
        if duration != expected_lineage.expected_output_duration_us:
            raise ValueError("verified artifact does not preserve the planned trim duration")
        path = result.get("artifact_path")
        output_asset_id = result.get("output_asset_id")
        transaction_id = result.get("transaction_id")
        state_hash = result.get("state_hash")
        attempt = chain.get("attempt")
        if not all(
            isinstance(value, str) and value
            for value in (path, output_asset_id, transaction_id, state_hash)
        ):
            raise ValueError("durable artifact or CommandBus transaction identity is missing")
        if physical.get("path") != path or logical.get("output_asset_id") != output_asset_id:
            raise ValueError("artifact path or logical asset identity does not match verification")
        if not isinstance(attempt, int) or attempt < 1:
            raise ValueError("durable attempt number is missing")
        receipt = AgentReceipt(
            request_id=intent.request_id,
            intent_id=intent.intent_id,
            strategy_id=strategy_id,
            work_id=work_id,
            plan_id=plan_id,
            job_id=job_id,
            command_id=expected_command_id,
            transaction_id=transaction_id,
            state_hash=state_hash,
            attempt=attempt,
            execution_status="completed",
            verification_status="verified",
            operation="timeline.trim",
            artifact_path=path,
            artifact_sha256=sha,
            size_bytes=size,
            duration_us=duration,
            output_asset_id=output_asset_id,
            physical_identity=physical,
            probe=probe,
        )
        return receipt, verification

    @staticmethod
    def _failure(code: str, response: str) -> AgentOutcome:
        return AgentOutcome(status="failed", response=response, error_code=code)


def _seconds_text(microseconds: int) -> str:
    seconds, remainder = divmod(microseconds, 1_000_000)
    return f"{seconds}.{remainder:06d}"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _graph_trace(graph: CreativeGraph, run: Any) -> dict[str, Any]:
    return {
        "project_id": graph.project_id,
        "intent_node_id": run.intent_node.node_id,
        "strategy_node_id": run.strategy_node.node_id,
        "work_node_id": run.work_node.node_id,
        "plan_node_id": run.plan_node.node_id,
        "capability_node_ids": [node.node_id for node in run.capability_nodes],
        "node_kinds": [node.kind for node in graph.nodes()],
    }


__all__ = [
    "AgentInputContext",
    "AgentIntelligenceRuntime",
    "AgentOutcome",
    "AgentReceipt",
    "DurableCreativeQueue",
]
