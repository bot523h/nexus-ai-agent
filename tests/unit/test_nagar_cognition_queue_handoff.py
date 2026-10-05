"""Unit contract for the durable cognition -> creative-queue handoff.

The handoff is trustworthy only if it is *typed and bounded* end to end: the
model supplies the trim points and nothing else, the operation is a closed
mapping, and every failure is a typed refusal that enqueues **nothing**.  These
tests pin that contract with a recording queue (no I/O) and a scripted producer
(the external model seam is the only substituted dependency).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nexus_ai_agent.creative.render_jobs import CreativeRenderPayload, build_job_bus
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AssetRecord,
    Timeline,
    new_project,
)
from nexus_ai_agent.nagar.cognition import build_cognition_gateway
from nexus_ai_agent.nagar.creative import (
    derive_intent_idempotency_key,
    run_free_text_intent_durable,
)
from nexus_ai_agent.nagar.creative.handoff import (
    HandoffRefusal,
    build_creative_payload,
    normalize_intent,
    source_asset_facts,
)

PROJECT_ID = "nagar-handoff-unit"


class ScriptedProducer:
    """A declared test double for the external model seam only."""

    consults_model = True

    def __init__(self, response: object) -> None:
        self._response = response
        self.calls = 0

    async def complete(self, prompt: str, *, idempotency_key: str | None = None) -> str:
        self.calls += 1
        return self._response  # type: ignore[return-value]


class RecordingQueue:
    """A JobQueuePort that records enqueues and nothing else."""

    def __init__(self) -> None:
        self.enqueues: list[tuple[str, str, dict]] = []

    async def enqueue(self, *, job_type: str, idempotency_key: str, payload: dict) -> str:
        self.enqueues.append((job_type, idempotency_key, payload))
        return f"job-{idempotency_key[-8:]}"


def _proposal(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_id": "nagar.gateway.proposal.v1",
        "schema_version": 1,
        "operation": "timeline.trim",
        "input": {"clip_asset_id": "src", "in_point_us": 1_000_000, "out_point_us": 8_000_000},
        "rationale": "trim 1s..8s",
        "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _project(duration_us: int = 9_000_000):
    actor = ActorIdentity(kind="user", actor_id="operator")
    project = new_project(
        PROJECT_ID, "handoff", Timeline(timeline_id="tl", duration_us=duration_us)
    ).model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="src",
                    media_kind="video",
                    content_sha256="sha256:" + "0" * 64,
                    duration_us=duration_us,
                )
            ]
        }
    )
    return actor, project


def _gateway(actor, project, producer, *, enabled=True):  # noqa: ANN001
    access = ProjectAccess(
        actor=actor, project_id=PROJECT_ID, permissions=frozenset({"project:read", "project:write"})
    )
    # The canonical, server-policy bus factory (runtime registry: timeline.trim
    # is AVAILABLE there).  No test-built registry, no caller opt-in.
    bus = build_job_bus(project, authorizer=access)
    return build_cognition_gateway(
        bus=bus,
        actor=actor,
        project_id=PROJECT_ID,
        enabled=enabled,
        completion=producer,
    )


def _run(text: str, *, producer, project, queue, source_path, workspace_dir):  # noqa: ANN001
    actor, _ = _project()
    gateway = _gateway(actor, project, producer)
    return asyncio.run(
        run_free_text_intent_durable(
            text,
            gateway=gateway,
            queue=queue,
            project=project,
            source_path=source_path,
            workspace_dir=workspace_dir,
            user_id=7,
            chat_id=70,
        )
    )


@pytest.fixture()
def source(tmp_path):  # noqa: ANN001
    path = tmp_path / "real_source.mp4"
    path.write_bytes(b"\x00\x01\x02\x03real-bytes")
    return path


# ── the model supplies points only; the payload is canonical ────────────────


def test_valid_proposal_enqueues_one_typed_canonical_payload(tmp_path, source) -> None:  # noqa: ANN001
    _, project = _project()
    queue = RecordingQueue()
    outcome = _run(
        "trim from 1s to 8s",
        producer=ScriptedProducer(_proposal()),
        project=project,
        queue=queue,
        source_path=str(source),
        workspace_dir=str(tmp_path / "creative_ws"),
    )
    assert outcome.status == "enqueued"
    assert len(queue.enqueues) == 1
    job_type, key, payload = queue.enqueues[0]
    assert job_type == "creative_render"
    assert key == outcome.idempotency_key
    # The payload is the canonical, extra-forbidden worker contract.
    typed = CreativeRenderPayload.model_validate(payload)
    assert typed.command == "edit"
    assert typed.operation == "trim"
    assert typed.idempotency_key == key
    # The model's 1s..8s became the worker argv; the operation is the closed map.
    assert typed.args == ["1", "8"]


def test_payload_carries_no_authority_and_no_raw_text(tmp_path, source) -> None:  # noqa: ANN001
    _, project = _project()
    queue = RecordingQueue()
    _run(
        "trim from 1s to 8s",
        producer=ScriptedProducer(_proposal()),
        project=project,
        queue=queue,
        source_path=str(source),
        workspace_dir=str(tmp_path / "creative_ws"),
    )
    payload = queue.enqueues[0][2]
    for forbidden in ("actor", "permissions", "allow_experimental", "worker", "handler", "shell"):
        assert forbidden not in payload
    # The free text never enters the queue.
    assert "trim from 1s to 8s" not in json.dumps(payload)


# ── refusals enqueue NOTHING ────────────────────────────────────────────────


def test_missing_model_enqueues_nothing(tmp_path, source) -> None:  # noqa: ANN001
    actor, project = _project()
    # No model configured -> the gateway is disabled and selects NullCognition
    # (the fail-closed default).  A null producer refuses and nothing enqueues.
    gateway = _gateway(actor, project, None, enabled=False)
    queue = RecordingQueue()
    outcome = asyncio.run(
        run_free_text_intent_durable(
            "trim from 1s to 8s",
            gateway=gateway,
            queue=queue,
            project=project,
            source_path=str(source),
            workspace_dir=str(tmp_path / "creative_ws"),
            user_id=1,
            chat_id=1,
        )
    )
    assert outcome.status == "clarification_required"
    assert queue.enqueues == []


@pytest.mark.parametrize(
    "hostile",
    [
        "{not json",
        json.dumps(
            {
                "schema_id": "x",
                "schema_version": 1,
                "operation": "timeline.trim",
                "input": {},
                "actor": {"kind": "admin", "actor_id": "root"},
            }
        ),
        _proposal(operation="timeline.speed_ramp"),
        _proposal(input={"clip_asset_id": "src", "in_point_us": 5_000_000, "out_point_us": 1}),
    ],
)
def test_hostile_or_invalid_proposal_enqueues_nothing(tmp_path, source, hostile) -> None:  # noqa: ANN001
    _, project = _project()
    queue = RecordingQueue()
    outcome = _run(
        "do something",
        producer=ScriptedProducer(hostile),
        project=project,
        queue=queue,
        source_path=str(source),
        workspace_dir=str(tmp_path / "creative_ws"),
    )
    assert outcome.status == "refused"
    assert queue.enqueues == []


def test_missing_source_is_refused_before_enqueue(tmp_path) -> None:  # noqa: ANN001
    _, project = _project()
    queue = RecordingQueue()
    outcome = _run(
        "trim from 1s to 8s",
        producer=ScriptedProducer(_proposal()),
        project=project,
        queue=queue,
        source_path=str(tmp_path / "does_not_exist.mp4"),
        workspace_dir=str(tmp_path / "creative_ws"),
    )
    assert outcome.status == "refused"
    assert outcome.refusal_reason == "missing_source"
    assert queue.enqueues == []


def test_no_source_asset_is_refused(tmp_path, source) -> None:  # noqa: ANN001
    actor, _ = _project()
    empty = new_project("p", "n", Timeline(timeline_id="tl", duration_us=0))
    gateway = _gateway(actor, empty, ScriptedProducer(_proposal()))
    queue = RecordingQueue()
    outcome = asyncio.run(
        run_free_text_intent_durable(
            "trim",
            gateway=gateway,
            queue=queue,
            project=empty,
            source_path=str(source),
            workspace_dir=str(tmp_path / "creative_ws"),
            user_id=1,
            chat_id=1,
        )
    )
    assert outcome.status == "refused"
    assert outcome.refusal_reason == "missing_source"
    assert queue.enqueues == []


# ── identity: intent key is deterministic, distinct, and not the job id ─────


def test_same_intent_same_key_different_intent_different_key() -> None:
    actor, _ = _project()
    key_a = derive_intent_idempotency_key(PROJECT_ID, actor, "trim first 5 seconds")
    key_b = derive_intent_idempotency_key(PROJECT_ID, actor, "trim first 5 seconds")
    key_c = derive_intent_idempotency_key(PROJECT_ID, actor, "trim first 8 seconds")
    assert key_a == key_b
    assert key_a != key_c
    assert key_a.startswith("cognition:")


def test_contradictory_payload_under_same_key_fails_closed(tmp_path, source) -> None:  # noqa: ANN001
    """Same intent + a model that changes its points ⇒ typed refusal, never overwrite.

    The intent key is deterministic, so a *non-deterministic* model returning a
    different trim for the same text collides on the key with a different payload.
    The queue must reject that (Case 5), and the slice must surface it as a typed
    refusal rather than raising or silently overwriting the durable row.
    """
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue

    actor, project = _project()
    queue = InProcessJobQueue(tmp_path / "contradiction.sqlite3")

    async def _noop(payload: dict) -> dict:  # pragma: no cover - handler never asserted
        return {"success": True}

    queue.register_handler("creative_render", _noop)

    def _gateway_with(in_us: int, out_us: int):
        producer = ScriptedProducer(
            _proposal(input={"clip_asset_id": "src", "in_point_us": in_us, "out_point_us": out_us})
        )
        return _gateway(actor, project, producer)

    async def _attempt(gateway):  # noqa: ANN001
        return await run_free_text_intent_durable(
            "trim it",
            gateway=gateway,
            queue=queue,
            project=project,
            source_path=str(source),
            workspace_dir=str(tmp_path / "creative_ws"),
            user_id=1,
            chat_id=1,
        )

    first = asyncio.run(_attempt(_gateway_with(0, 1_000_000)))
    assert first.status == "enqueued"
    second = asyncio.run(_attempt(_gateway_with(0, 2_000_000)))
    assert second.status == "refused"
    assert second.refusal_reason == "idempotency_conflict"
    assert queue.job_ids() == [first.job_id]
    asyncio.run(queue.shutdown())


# ── the typed handoff builder is strict ─────────────────────────────────────


class _FakeProposal:
    def __init__(self, operation: str, inputs: dict) -> None:
        self.operation = operation
        self.input = inputs


def test_build_payload_rejects_unsupported_operation() -> None:
    with pytest.raises(HandoffRefusal):
        build_creative_payload(
            _FakeProposal("timeline.reverse_segment", {"clip_asset_id": "src"}),
            workspace_dir="/w/creative_x",
            input_path="/w/creative_x/input.mp4",
            media_duration_us=1_000_000,
            user_id=1,
            chat_id=1,
            lang="en",
            idempotency_key="k",
        )


@pytest.mark.parametrize(
    "inputs",
    [
        {"in_point_us": -1, "out_point_us": 100},
        {"in_point_us": 100, "out_point_us": 100},
        {"in_point_us": True, "out_point_us": 100},
        {"in_point_us": "0", "out_point_us": 100},
        {"in_point_us": None, "out_point_us": 100},
    ],
)
def test_build_payload_rejects_bad_points(inputs) -> None:  # noqa: ANN001
    with pytest.raises(HandoffRefusal):
        build_creative_payload(
            _FakeProposal("timeline.trim", inputs),
            workspace_dir="/w/creative_x",
            input_path="/w/creative_x/input.mp4",
            media_duration_us=1_000_000,
            user_id=1,
            chat_id=1,
            lang="en",
            idempotency_key="k",
        )


def test_build_payload_clamps_points_to_the_measured_duration() -> None:
    payload = build_creative_payload(
        _FakeProposal("timeline.trim", {"in_point_us": 0, "out_point_us": 99_000_000}),
        workspace_dir="/w/creative_x",
        input_path="/w/creative_x/input.mp4",
        media_duration_us=2_000_000,
        user_id=1,
        chat_id=1,
        lang="en",
        idempotency_key="k",
    )
    assert payload["args"] == ["0", "2"]


def test_source_asset_facts_requires_a_measured_duration() -> None:
    project = new_project("p", "n", Timeline(timeline_id="tl", duration_us=0)).model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="src",
                    media_kind="video",
                    content_sha256="sha256:" + "0" * 64,
                    duration_us=0,
                )
            ]
        }
    )
    with pytest.raises(KeyError):
        source_asset_facts(project, None)


def test_normalize_intent_bounds_and_collapses() -> None:
    assert normalize_intent("  trim   from 1s \n to 8s  ") == "trim from 1s to 8s"
    assert len(normalize_intent("x" * 10_000)) == 500
