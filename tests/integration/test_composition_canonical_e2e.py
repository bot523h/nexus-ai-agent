"""Canonical end-to-end for the NAGAR composition runtime — no mock at any seam.

The chain under test is the **canonical** one, in this order:

    request → typed plan → compiler (live registry) → canonical capability
    resolution → canonical policy/authority → canonical job submission →
    registered worker → approved media execution (real FFmpeg) → real artifact
    → independent verification → evidence → existing Artifact Passport

Proven here:

* a **real** job id, a **real** queue row, an **observable** status, and a
  **real** worker invocation (the handler is the one registered in
  ``worker.py::default_job_handlers()``);
* the produced artifact is a real file whose bytes hash to the claimed digest;
* the queue's registered verifier re-measures it independently;
* the composition's own semantic pass re-decodes it independently of the
  producing path;
* the **existing** ``provenance/passport.py::PassportBuilder`` builds a
  VERIFIED passport over the same job;
* a redelivery under the same idempotency key does not execute twice;
* a tampered artifact flips the passport to COMPROMISED.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.composition.contracts import (
    CompositionPlan,
    ExpectedOutput,
    ExternalInput,
    PlanNode,
    PortBinding,
)
from nexus_ai_agent.creative.composition.job import COMPOSITION_JOB_TYPE, plan_to_dict
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.provenance import (
    CausalJournal,
    EventKind,
    PassportBuilder,
    PassportStatus,
    QueueLedgerObserver,
)
from nexus_ai_agent.worker import default_job_handlers

RESIZE = "media.resize_image"
GRAYSCALE = "media.grayscale_image"
IMAGE_TO_VIDEO = "media.image_to_video"

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative_tmp"))
    (tmp_path / "creative_tmp").mkdir(parents=True)
    settings_module.get_settings.cache_clear()

    journal = CausalJournal(tmp_path / "jobs.sqlite3.causal.sqlite3")
    queue = InProcessJobQueue(
        tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
    )
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    yield queue, journal
    settings_module.get_settings.cache_clear()


def _plan(*, with_video: bool = True) -> CompositionPlan:
    registry = build_runtime_registry()

    def pin(operation: str) -> tuple[str, str, int]:
        descriptor = registry.describe(operation)
        return descriptor.capability_id, descriptor.version, descriptor.operation_schema_version

    rc, rv, rs = pin(RESIZE)
    gc, gv, gs = pin(GRAYSCALE)
    nodes = [
        PlanNode(
            node_id="resize",
            operation=RESIZE,
            capability_id=rc,
            capability_version=rv,
            operation_schema_version=rs,
            params={"width": 640, "height": 480},
            bindings=(PortBinding(input_port="source", source="external", external="src"),),
        ),
        PlanNode(
            node_id="gray",
            operation=GRAYSCALE,
            capability_id=gc,
            capability_version=gv,
            operation_schema_version=gs,
            params={},
            bindings=(
                PortBinding(
                    input_port="source",
                    source="node_output",
                    node_id="resize",
                    output_port="resized",
                ),
            ),
        ),
    ]
    outputs = [ExpectedOutput(name="gray", node_id="gray", output_port="grayscale")]
    if with_video:
        vc, vv, vs = pin(IMAGE_TO_VIDEO)
        nodes.append(
            PlanNode(
                node_id="video",
                operation=IMAGE_TO_VIDEO,
                capability_id=vc,
                capability_version=vv,
                operation_schema_version=vs,
                params={
                    "width": 640,
                    "height": 480,
                    "duration_us": 1_000_000,
                    "fps": 25,
                    "encoder": "libx264",
                },
                bindings=(
                    PortBinding(
                        input_port="source",
                        source="node_output",
                        node_id="gray",
                        output_port="grayscale",
                    ),
                ),
            )
        )
        outputs = [ExpectedOutput(name="final", node_id="video", output_port="video")]
    return CompositionPlan(
        nodes=tuple(nodes),
        external_inputs=(ExternalInput(name="src", media_kind="image"),),
        expected_outputs=tuple(outputs),
    )


def _stage(workspace: Path) -> Path:
    source = workspace / "source.png"
    Image.new("RGB", (320, 240), (210, 70, 40)).save(source)
    return source


def _pinned(plan: CompositionPlan) -> CompositionPlan:
    """Pin a plan to the live registry, the way a real submitter does."""
    from nexus_ai_agent.creative.composition.compiler import registry_fingerprint
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    return plan.pinned(registry_fingerprint(build_runtime_registry()))


def _payload(workspace: Path, source: Path, key: str, *, with_video: bool = True) -> dict:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "plan": plan_to_dict(_pinned(_plan(with_video=with_video))),
        "external_inputs": {"src": {"path": str(source), "sha256": digest}},
        "workspace_dir": str(workspace),
        "idempotency_key": key,
        # The requester is named, and the level-C node is explicitly confirmed.
        # The worker never invents either.
        "actor": {"kind": "user", "actor_id": "e2e-requester"},
        "confirmed": True,
    }


async def _run(queue: InProcessJobQueue, payload: dict) -> tuple[str, dict]:
    job_id = await queue.enqueue(
        job_type=COMPOSITION_JOB_TYPE,
        idempotency_key=payload["idempotency_key"],
        payload=payload,
    )
    terminal = {JobStatus.COMPLETED, JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    for _ in range(600):
        status = await queue.get_status(job_id)
        if status in terminal:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - a hung queue must fail loudly
        raise AssertionError("the composition job never reached a terminal status")
    result = await queue.get_result(job_id)
    assert result is not None
    return job_id, result


async def test_image_to_video_composition_runs_through_the_canonical_chain(
    harness,
) -> None:
    queue, journal = harness
    workspace = queue.db_path.parent / "creative_tmp" / "creative_e2e_video"
    workspace.mkdir(parents=True)
    source = _stage(workspace)
    payload = _payload(workspace, source, "composition:e2e:video")

    job_id, result = await _run(queue, payload)
    assert await queue.get_status(job_id) is JobStatus.COMPLETED, result
    assert result["success"] is True

    # --- a real artifact, with a digest that matches its bytes -------------
    artifact = Path(str(result["artifact_path"]))
    assert artifact.is_file()
    measured = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert measured == result["sha256"]
    assert artifact.stat().st_size == result["size_bytes"]

    # --- the queue's own verifier re-measured it independently ------------
    verification = result["artifact_verification"]
    assert verification["physical_identity"]["sha256"] == measured
    assert verification["physical_identity"]["path"] == str(artifact)

    # --- the composition's semantic pass measured the content ------------
    assert result["semantic_verified"] is True
    assert result["semantic_measurements"]["codec"] == "h264"
    assert result["semantic_measurements"]["width"] == 640
    assert result["semantic_measurements"]["height"] == 480
    assert result["semantic_measurements"]["decoded_frames"] == 25

    # --- the evidence carries the lineage and the fingerprints -------------
    assert result["plan_fingerprint"].startswith("sha256:")
    assert result["registry_fingerprint"].startswith("sha256:")
    assert result["lineage"] == [
        "resize:media.resize_image@1.0.0",
        "gray:media.grayscale_image@1.0.0",
        "video:media.image_to_video@1.0.0",
    ]
    assert len(result["evidence"]["receipts"]) == 3

    # --- the causal journal saw the whole lifecycle ------------------------
    kinds = [record.kind for record in journal.records_for_job(job_id)]
    assert EventKind.JOB_ENQUEUED in kinds
    assert EventKind.JOB_COMPLETED in kinds

    # --- the EXISTING passport authority builds over the same job ---------
    passport = PassportBuilder(journal, queue).build(job_id)
    assert passport.status is PassportStatus.VERIFIED, passport.findings
    subject = passport.content["subject"]
    assert subject["digest"]["sha256"] == measured
    assert subject["remeasurement"]["matches"] is True


async def test_image_to_image_composition_runs_through_the_canonical_chain(
    harness,
) -> None:
    queue, journal = harness
    workspace = queue.db_path.parent / "creative_tmp" / "creative_e2e_image"
    workspace.mkdir(parents=True)
    source = _stage(workspace)
    payload = _payload(workspace, source, "composition:e2e:image", with_video=False)

    job_id, result = await _run(queue, payload)
    assert await queue.get_status(job_id) is JobStatus.COMPLETED, result
    artifact = Path(str(result["artifact_path"]))
    assert artifact.is_file()
    assert artifact.suffix == ".png"
    assert result["artifact_kind"] == "image"
    assert result["semantic_verified"] is True
    # the exposed artifact is the *grayscale* intermediate
    with Image.open(artifact) as image:
        rgb = image.convert("RGB")
        # Pixel-by-pixel equality: a solid-colour palette PNG would satisfy a
        # per-channel extrema check while not being grayscale at all.
        red, green, blue = (channel.tobytes() for channel in rgb.split())
        assert red == green == blue

    passport = PassportBuilder(journal, queue).build(job_id)
    assert passport.status is PassportStatus.VERIFIED, passport.findings


async def test_a_redelivery_under_the_same_key_does_not_execute_twice(harness) -> None:
    queue, journal = harness
    workspace = queue.db_path.parent / "creative_tmp" / "creative_e2e_dupe"
    workspace.mkdir(parents=True)
    source = _stage(workspace)
    payload = _payload(workspace, source, "composition:e2e:dupe")

    job_id, result = await _run(queue, payload)
    assert result["success"] is True

    duplicate = await queue.enqueue(
        job_type=COMPOSITION_JOB_TYPE,
        idempotency_key=payload["idempotency_key"],
        payload=payload,
    )
    assert duplicate == job_id
    history = await queue.get_attempt_history(job_id)
    assert len(history) == 1, "a redelivery must not start a second attempt"


async def test_a_tampered_artifact_flips_the_passport_to_compromised(harness) -> None:
    """The producer is never the judge: the passport re-measures the bytes."""
    queue, journal = harness
    workspace = queue.db_path.parent / "creative_tmp" / "creative_e2e_tamper"
    workspace.mkdir(parents=True)
    source = _stage(workspace)
    payload = _payload(workspace, source, "composition:e2e:tamper")

    job_id, result = await _run(queue, payload)
    assert PassportBuilder(journal, queue).build(job_id).status is PassportStatus.VERIFIED

    artifact = Path(str(result["artifact_path"]))
    raw = artifact.read_bytes()
    artifact.write_bytes(raw[:-1] + bytes([raw[-1] ^ 0xFF]))

    passport = PassportBuilder(journal, queue).build(job_id)
    assert passport.status is PassportStatus.COMPROMISED
    assert any(f.code == "artifact_digest_mismatch" for f in passport.findings)


async def test_a_changed_staged_input_is_refused_before_any_media_work(harness) -> None:
    queue, _ = harness
    workspace = queue.db_path.parent / "creative_tmp" / "creative_e2e_tampered_input"
    workspace.mkdir(parents=True)
    source = _stage(workspace)
    payload = _payload(workspace, source, "composition:e2e:tampered_input")
    # the row claims a digest the bytes no longer have
    payload["external_inputs"]["src"]["sha256"] = "0" * 64

    job_id = await queue.enqueue(
        job_type=COMPOSITION_JOB_TYPE,
        idempotency_key=payload["idempotency_key"],
        payload=payload,
    )
    for _ in range(300):
        status = await queue.get_status(job_id)
        if status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED_RETRYABLE,
            JobStatus.FAILED_TERMINAL,
        }:
            break
        await asyncio.sleep(0.05)
    # The row must reach a **terminal** failure: a job that merely stayed
    # pending would pass a weaker assertion without ever exercising the
    # digest refusal.
    assert status in {JobStatus.FAILED_RETRYABLE, JobStatus.FAILED_TERMINAL}
    result = await queue.get_result(job_id)
    assert result is not None
    assert result.get("success") is not True
    assert result.get("error_code") == "invalid_request"
    assert "digest mismatch" in str(result.get("error_detail", ""))


async def test_the_durable_row_records_the_job(harness) -> None:
    queue, _ = harness
    workspace = queue.db_path.parent / "creative_tmp" / "creative_e2e_row"
    workspace.mkdir(parents=True)
    source = _stage(workspace)
    payload = _payload(workspace, source, "composition:e2e:row")

    job_id, _ = await _run(queue, payload)
    with sqlite3.connect(queue.db_path) as connection:
        row = connection.execute(
            "SELECT job_type, status, idempotency_key FROM nexus_job_queue WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == COMPOSITION_JOB_TYPE
    assert row[1] == "completed"
    assert row[2] == payload["idempotency_key"]
