"""One request through LangGraph, the durable queue, real render and verifier."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    probe_video,
    resolve_ffmpeg_bin,
    sha256_file,
)
from nexus_ai_agent.creative.spine.compiler import request_idempotency_key
from nexus_ai_agent.creative.spine.intent import IntentResolverPort
from nexus_ai_agent.creative.spine.models import Intent, IntentSourceRange
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.orchestration.agent_intelligence import (
    AgentInputContext,
    AgentIntelligenceRuntime,
)
from nexus_ai_agent.orchestration.graph import compile_graph
from nexus_ai_agent.tools.registry import ToolRegistry
from nexus_ai_agent.worker import default_job_handlers


class _ExplicitTrimResolver(IntentResolverPort):
    """Deterministic test proposal at the LLM boundary, not a fake executor."""

    async def resolve(self, text: str, *, project_id: str, request_id: str) -> Intent:
        assert text == "Keep the video from 0 to 1 second."
        return Intent(
            project_id=project_id,
            request_id=request_id,
            goal="Keep the explicitly requested video section",
            objective="video_trim",
            source_range=IntentSourceRange(in_point_us=0, out_point_us=1_000_000),
            confidence=0.99,
            ambiguity_state="clear",
            source_text=text,
            source="user",
        )


def _make_clip(path: Path) -> None:
    binary = resolve_ffmpeg_bin()
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _state(*, request_id: str, context: AgentInputContext) -> dict[str, Any]:
    return {
        "thread_id": "tg:4242",
        "chat_id": 4242,
        "user_id": 42,
        "correlation_id": request_id,
        "messages": [{"role": "user", "content": "Keep the video from 0 to 1 second."}],
        "intent": "unknown",
        "active_persona": "",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
        "creative_asset": context.model_dump(mode="json"),
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_graph_to_durable_command_verified_artifact_and_reopened_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creative_root = tmp_path / "creative-root"
    creative_root.mkdir()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_root))
    settings_module.get_settings.cache_clear()

    request_id = "agent-integration-request-0001"
    queue_key = request_idempotency_key(request_id)
    workspace = creative_root / "creative_agent_integration"
    workspace.mkdir()
    source_path = workspace / "input.mp4"
    _make_clip(source_path)
    measured_source = probe_video(source_path, binary=resolve_ffmpeg_bin())
    source_hash = sha256_file(source_path)
    context = AgentInputContext(
        project_id=f"shot-{queue_key}",
        source={
            "asset_id": "asset_e2e_source",
            "media_kind": "video",
            "duration_us": measured_source.duration_us,
            "content_sha256": source_hash,
            "width_px": measured_source.width or 0,
            "height_px": measured_source.height or 0,
        },
        workspace_dir=str(workspace),
        input_path=str(source_path),
        user_id=42,
        chat_id=4242,
        lang="en",
    )

    db_path = tmp_path / "agent-jobs.sqlite3"
    queue = InProcessJobQueue(db_path)
    handlers = default_job_handlers()
    for job_type, handler in handlers.items():
        queue.register_handler(job_type, handler)

    runtime = AgentIntelligenceRuntime(
        intent_resolver=_ExplicitTrimResolver(),
        queue=queue,
        registry=build_runtime_registry(),
        wait_timeout_s=120,
        poll_interval_s=0.05,
    )
    graph = compile_graph(
        FakeLLMProvider(),
        checkpointer=None,
        long_term_memory=None,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(enable_shell=False),
        agent_intelligence=runtime,
    )

    try:
        result = await graph.ainvoke(
            _state(request_id=request_id, context=context),
            config={"configurable": {"thread_id": "tg:4242"}},
        )
        outcome = result["agent_intelligence"]
        assert outcome["status"] == "completed", outcome
        assert outcome["max_replans"] == 0
        assert result["creative_asset"] is None
        assert result["response"].startswith("Verified trim complete")

        receipt = outcome["receipt"]
        assert receipt["execution_status"] == "completed"
        assert receipt["verification_status"] == "verified"
        assert receipt["request_id"] == request_id
        assert receipt["command_id"] == f"cmd-{queue_key}-timeline.trim"
        assert receipt["transaction_id"]
        assert receipt["state_hash"]
        assert receipt["attempt"] == 1
        assert receipt["duration_us"] == 1_000_000

        job_id = outcome["job_id"]
        assert await queue.get_status(job_id) is JobStatus.COMPLETED
        durable_result = await queue.get_result(job_id)
        assert durable_result is not None
        assert durable_result["success"] is True
        assert durable_result["operation"] == "timeline.trim"
        assert durable_result["command_id"] == receipt["command_id"]
        assert durable_result["agent_lineage"]["max_replans"] == 0
        assert durable_result["agent_lineage"]["source_sha256"] == source_hash
        assert (
            json.loads(durable_result["agent_lineage"]["intent_snapshot_json"])["request_id"]
            == request_id
        )
        assert (
            json.loads(durable_result["agent_lineage"]["work_snapshot_json"])["work_id"]
            == receipt["work_id"]
        )

        verification = durable_result["artifact_verification"]
        assert verification["status"] == "verified"
        assert verification["sha256"] == receipt["artifact_sha256"]
        assert verification["size_bytes"] == receipt["size_bytes"]
        assert verification["probe"]["duration_us"] == receipt["duration_us"]
        artifact_path = Path(receipt["artifact_path"])
        assert artifact_path.is_file()
        assert (
            "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            == (receipt["artifact_sha256"])
        )
        assert receipt["physical_identity"]["path"] == str(artifact_path)

        chain = await queue.get_result_chain(job_id)
        assert chain["execution_status"] == JobStatus.COMPLETED.value
        assert chain["verification_status"] == "verified"
        assert chain["command_id"] == receipt["command_id"]
        assert chain["physical_identity"] == verification["physical_identity"]
        assert chain["logical_identity"] == verification["logical_identity"]
        assert chain["spec_identity"] == verification["spec_identity"]
        assert chain["attempt"] == 1

        # A duplicate through the same graph is a queue-idempotent replay; it
        # returns the original receipt without a second worker attempt.
        duplicate = await graph.ainvoke(
            _state(request_id=request_id, context=context),
            config={"configurable": {"thread_id": "tg:4242"}},
        )
        duplicate_outcome = duplicate["agent_intelligence"]
        assert duplicate_outcome["status"] == "completed"
        assert duplicate_outcome["job_id"] == job_id
        assert duplicate_outcome["receipt"] == receipt
        assert (await queue.get_result_chain(job_id))["attempt"] == 1

        # Reopening the existing sidecar and submitting the same request again
        # must recover the same verified receipt, not execute a second trim.
        reopened = InProcessJobQueue(db_path)
        for job_type, handler in handlers.items():
            reopened.register_handler(job_type, handler)
        reopened_runtime = AgentIntelligenceRuntime(
            intent_resolver=_ExplicitTrimResolver(),
            queue=reopened,
            registry=build_runtime_registry(),
            wait_timeout_s=120,
            poll_interval_s=0.05,
        )
        reopened_graph = compile_graph(
            FakeLLMProvider(),
            checkpointer=None,
            long_term_memory=None,  # type: ignore[arg-type]
            tool_registry=ToolRegistry(enable_shell=False),
            agent_intelligence=reopened_runtime,
        )
        recovered = await reopened_graph.ainvoke(
            _state(request_id=request_id, context=context),
            config={"configurable": {"thread_id": "tg:4242"}},
        )
        recovered_outcome = recovered["agent_intelligence"]
        assert recovered_outcome["status"] == "completed"
        assert recovered_outcome["job_id"] == job_id
        assert recovered_outcome["receipt"] == receipt
        assert await reopened.get_status(job_id) is JobStatus.COMPLETED
        reopened_result = await reopened.get_result(job_id)
        assert reopened_result == durable_result
        assert await reopened.get_result_chain(job_id) == chain
        await reopened.shutdown()
    finally:
        await queue.shutdown()
        settings_module.get_settings.cache_clear()
