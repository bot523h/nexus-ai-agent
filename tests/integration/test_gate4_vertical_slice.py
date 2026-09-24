"""Gate 4 — cross-layer vertical slice verification (the deterministic harness).

One real creative journey, cross-layer verified:

    Intent → Typed Command → Capability → Validation → Authorization/Policy
      → Job → Runtime → Artifact → Verification → Result → Persist → Reopen

Slice (T-ids resolved from the TDD catalogue every run — see
``gate4_slice`` for the engineered rule):

    T01 split_at_playhead → T02 trim → T22 remove_object
      → T31 add_transition → T64 match_shot

Baseline (owner-directed full-stack): merged main ``035a896`` + Agent-1
runtime PR#67 ``9c3a34f`` + Agent-2 command boundary PR#68 ``eef8276``.
Nothing is rebuilt here: executor, compiler, LaneIR, LUT, burn-in, OTIO,
artifact hashing, ffprobe verification, capability four-state and the
rendering pipeline are consumed as-is. Failures observed in another layer
are reported to that layer's owner, never patched in this gate.

Run the whole file (the Truth-Matrix writer at the end consumes evidence
recorded by the tests above it):

    pytest -q tests/integration/test_gate4_vertical_slice.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.bot.creative_surface import CreativeRequest, CreativeSurfaceMapper
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.render_jobs import SURFACE_TO_CANONICAL
from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.lifecycle import PackRequirementError
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    AssetRecord,
    AuthorizationError,
    Clip,
    CommandProvenance,
    CommandValidationError,
    MediaRef,
    Playhead,
    PreconditionError,
    Preconditions,
    TargetRef,
    TimeBase,
    Timeline,
    TimeRangeUS,
    Track,
    TypedCommand,
    UnknownOperationError,
    new_project,
)
from nexus_ai_agent.worker import default_job_handlers

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate4_slice import (  # noqa: E402 - path-bootstrapped sibling module
    BASELINE,
    EXPECTED_SLICE,
    LAYERS,
    LEGAL_VERDICTS,
    SLICE_IDS,
    StepEvidence,
    build_matrix,
    load_tdd_catalog,
    new_step_evidence,
    render_markdown,
    resolve_slice,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_PATH = REPO_ROOT / "docs" / "audits" / "GATE4_TRUTH_MATRIX.json"

TEST_ACTOR = ActorIdentity(kind="service", actor_id="nagar.contract-test")
TEST_PROVENANCE = CommandProvenance(source="service", source_id="gate4-slice")
GRANT = frozenset({"project:read", "project:write"})

#: Evidence recorded across the tests of this module; the final test turns it
#: into the Truth Matrix. Whole-file run is the contract (see module docstring).
EVIDENCE: dict[str, Any] = {"steps": new_step_evidence(), "failure_matrix": {}, "reopen": {}}


def _step(t_id: str) -> StepEvidence:
    return EVIDENCE["steps"][t_id]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _clip(path: Path, seconds: int = 2) -> None:
    """Real two-second test clip (the media is the truth — Agent-1 tooling)."""
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
            f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


@pytest.fixture()
def slice_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh creative temp + durable queue per test (no delivery hook, so
    artifacts survive for the reopen proof — cleanup is delivery's job)."""
    creative_tmp = tmp_path / "creative_tmp"
    creative_tmp.mkdir(parents=True)
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(creative_tmp))
    settings_module.get_settings.cache_clear()
    queue = InProcessJobQueue(tmp_path / "jobs.sqlite3")
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    yield queue, creative_tmp  # type: ignore[misc]
    settings_module.get_settings.cache_clear()


async def _drain(queue: InProcessJobQueue, job_id: str, timeout: float = 120.0) -> JobStatus:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        status = await queue.get_status(job_id)
        if status in {JobStatus.COMPLETED, JobStatus.FAILED}:
            return status
        await asyncio.sleep(0.05)
    raise TimeoutError(f"job {job_id} did not reach a terminal state")


def _render_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count real lane executions without replacing Agent-1's renderer."""
    import nexus_ai_agent.creative.rendering.executor as executor

    calls = {"count": 0}
    real = executor.render_lane

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(executor, "render_lane", spy)
    return calls


def _journey_project():
    """One project carrying everything the five slice steps lawfully need."""
    media = MediaRef(
        asset_id="src",
        content_sha256="sha256:" + "a" * 64,
        media_kind="video",
        duration_us=4_000_000,
        timebase=TimeBase(numerator=30, denominator=1),
    )
    project = new_project(
        "p_gate4",
        "Gate4 Vertical Slice",
        Timeline(
            timeline_id="main",
            duration_us=4_000_000,
            tracks=[
                Track(
                    track_id="video_01",
                    name="V1",
                    kind="video",
                    clips=[
                        Clip(
                            clip_id="clip_a",
                            media_ref=media,
                            source_range=TimeRangeUS(start_us=0, end_us=4_000_000),
                            timeline_range=TimeRangeUS(start_us=0, end_us=4_000_000),
                        )
                    ],
                )
            ],
            playhead=Playhead(timecode_us=0),
        ),
    )
    return project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="src",
                    media_kind="video",
                    content_sha256="sha256:" + "a" * 64,
                    duration_us=4_000_000,
                ),
                AssetRecord(
                    asset_id="broll",
                    media_kind="video",
                    content_sha256="sha256:" + "b" * 64,
                    duration_us=4_000_000,
                ),
            ]
        }
    )


def _journey_bus(*, allow_experimental: bool = True, authorizer=True) -> CommandBus:
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    return CommandBus(
        _journey_project(),
        registry=build_runtime_registry(),
        authorizer=ProjectAccess(actor=TEST_ACTOR, project_id="p_gate4", permissions=GRANT)
        if authorizer
        else None,
        allow_experimental=allow_experimental,
    )


def _command(bus: CommandBus, *, command_id: str, operation: str, **kwargs: Any) -> TypedCommand:
    target = kwargs.pop("target", None) or TargetRef(project_id=bus.project.project_id)
    preconditions = kwargs.pop("preconditions", None)
    return TypedCommand(
        command_id=command_id,
        actor=TEST_ACTOR,
        provenance=TEST_PROVENANCE,
        operation=operation,
        target=target,
        preconditions=Preconditions.model_validate(preconditions)
        if preconditions
        else Preconditions(),
        **kwargs,
    )


def _payload(
    workspace: Path,
    key: str,
    *,
    command: str = "edit",
    operation: str = "trim",
    args: list[str] | None = None,
    **over: Any,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "command": command,
        "operation": operation,
        "args": args if args is not None else ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2_000_000,
        "user_id": 42,
        "chat_id": 4242,
        "lang": "en",
        "idempotency_key": key,
    }
    base.update(over)
    return base


def _note(t_id: str, distinction: str, verdict: str, note: str = "") -> None:
    step = _step(t_id)
    step.set(distinction, verdict, note)


def _record_failure(name: str, observed: str) -> None:
    EVIDENCE["failure_matrix"][name] = {"verdict": "PASS", "observed": observed}


# ---------------------------------------------------------------------------
# 1. definitions — the T-ids are re-derived from the TDD catalogue
# ---------------------------------------------------------------------------


def test_slice_ids_resolve_from_the_tdd_catalog() -> None:
    catalog = load_tdd_catalog()
    assert len(catalog) == 70, "the catalogue is the 70-operation TDD"
    resolved = resolve_slice(catalog)
    assert resolved == EXPECTED_SLICE
    # every slice op must exist somewhere in the tree's runtime registry too
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

    registry = build_runtime_registry()
    for t_id, operation in resolved.items():
        assert operation in registry, f"{t_id} {operation} not registered on this baseline"


# ---------------------------------------------------------------------------
# 2. domain journey — one project, five chained schema-2 commands
# ---------------------------------------------------------------------------


def test_domain_journey_chained_commands_pass_contract_and_capability() -> None:
    bus = _journey_bus()
    inputs: dict[str, dict[str, Any]] = {
        "T01": {
            "target": TargetRef(project_id="p_gate4", track_id="video_01", clip_id="clip_a"),
            "input": {"at": {"timecode_us": 1_500_000, "captured_at_command": True}},
        },
        "T02": {"input": {"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 2_000_000}},
        "T22": {"input": {"clip_asset_id": "src", "object_track_id": "obj_1"}},
        "T31": {"input": {"left_clip_id": "src", "right_clip_id": "broll"}},
        "T64": {"input": {"source_clip_id": "src", "reference_clip_id": "broll"}},
    }

    expected_revisions = {t: i for i, t in enumerate(SLICE_IDS, start=1)}
    previous: dict[str, Any] | None = None
    for t_id in SLICE_IDS:
        operation = EXPECTED_SLICE[t_id]
        kwargs = dict(inputs[t_id])
        preconditions = None
        if previous is not None:
            # optimistic concurrency: every step proves the previous revision+hash
            preconditions = {
                "state_revision": previous["revision"],
                "state_hash": previous["hash"],
            }
        command = _command(
            bus,
            command_id=f"cmd-gate4-{t_id}",
            operation=operation,
            preconditions=preconditions,
            **kwargs,
        )
        # CONTRACT: the schema-2 envelope itself revalidates
        TypedCommand.model_validate(command.model_dump(mode="json"))
        result = bus.dispatch(command)

        assert result.state_revision == expected_revisions[t_id]
        step = _step(t_id)
        step.set("CONTRACT", "PASS", "schema-2 envelope revalidated and dispatched")
        step.set("DOMAIN", "PASS", f"registered op applied at revision {result.state_revision}")
        step.provenance.update(
            {
                "command_id": command.command_id,
                "operation_id": operation,
                "revision": result.state_revision,
                "transaction_id": result.transaction_id,
                "state_hash": result.state_hash,
            }
        )
        previous = {"revision": result.state_revision, "hash": result.state_hash}

    assert len(bus.history) == 5, "one EditTransaction per slice step"
    # capability distinction also records lifecycle resolution (Agent-1 four-state)
    for t_id in SLICE_IDS:
        assert _step(t_id).distinctions["DOMAIN"] == "PASS"


def test_product_surface_has_exactly_one_slice_entry_point() -> None:
    """Product row: only T02 trim has an intent entry; the other four steps
    have no surface mapping — MISSING is proven, never faked."""
    mapper = CreativeSurfaceMapper()
    accepted = mapper.map(
        CreativeRequest(
            command="edit",
            operation="trim",
            args=("0", "1"),
            media_file_id="m1",
            media_duration_s=2.0,
        )
    )
    assert isinstance(accepted, CreativeRequest)
    assert SURFACE_TO_CANONICAL[("edit", "trim")] == "timeline.trim"
    _note("T02", "PRODUCT", "PASS", "mapper + worker map accept edit/trim")

    mapped_canonical = set(SURFACE_TO_CANONICAL.values())
    for t_id in SLICE_IDS:
        if t_id == "T02":
            continue
        operation = EXPECTED_SLICE[t_id]
        assert operation not in mapped_canonical, f"{operation} unexpectedly product-mapped"
        # a foreign operation name is refused typed at the mapper
        refused = mapper.map(
            CreativeRequest(
                command="edit",
                operation=operation.split(".")[-1],
                args=(),
                media_file_id="m1",
                media_duration_s=2.0,
            )
        )
        assert refused.__class__.__name__ == "CreativeFailure"
        _note(t_id, "PRODUCT", "MISSING", "no surface intent entry (typed refusal proven)")
        # downstream layers are unreachable *by design* without a product
        # entry — recorded as MISSING with this probe as the evidence, never
        # left as an unverified default and never faked as PASS.
        for distinction in ("JOB", "REAL_RUNTIME", "ARTIFACT", "E2E", "REOPEN"):
            _note(
                t_id,
                distinction,
                "MISSING",
                "no product intent entry → no job/runtime/artifact path exists "
                "for this step (typed refusal proven, nothing faked)",
            )


# ---------------------------------------------------------------------------
# 3. the vertical take — product → job → runtime → artifact → verification
# ---------------------------------------------------------------------------


async def test_vertical_take_trim_to_verified_artifact_and_reopen(
    slice_env, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    queue, creative_tmp = slice_env
    spy = _render_spy(monkeypatch)

    # — Product intent: the REAL mapper builds the job payload —
    key = "gate:42:4242:1001"
    workspace = creative_tmp / "creative_gate4_take"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")

    mapper = CreativeSurfaceMapper()
    request = CreativeRequest(
        command="edit",
        operation="trim",
        args=("0", "1"),
        media_file_id="m1",
        media_duration_s=2.0,
        media_file_size=(workspace / "input.mp4").stat().st_size,
    )
    mapped = mapper.map(request)
    assert isinstance(mapped, CreativeRequest), "product must accept the T02 intent"
    payload = mapper.job_payload(
        mapped,
        user_id=42,
        chat_id=4242,
        lang="en",
        idempotency_key=key,
        workspace_dir=str(workspace),
        input_path=str(workspace / "input.mp4"),
    )

    # — Job: durable enqueue through the real queue —
    job_id = await queue.enqueue(job_type="creative_render", idempotency_key=key, payload=payload)
    status = await _drain(queue, job_id)
    assert status is JobStatus.COMPLETED, "job must reach a terminal COMPLETED"

    # — Runtime + Artifact + Verification (measured facts, not flags) —
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is True
    assert result["operation"] == "timeline.trim"
    assert spy["count"] == 1, "exactly one real lane execution"

    artifact_path = Path(str(result["artifact_path"]))
    assert artifact_path.is_file(), "artifact must exist"
    size = artifact_path.stat().st_size
    assert size > 0, "artifact must be non-empty"
    recomputed = "sha256:" + hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    assert recomputed == result["sha256"], "reported sha256 must match the bytes on disk"
    assert 700_000 <= int(result["duration_us"]) <= 1_300_000, "trimmed duration is measured"
    assert int(result["height"]) > 0, "probe-derived height"

    # ffprobe-derived validity via Agent-1's probe (fallback recorded honestly)
    from nexus_ai_agent.creative.slideshow.ffmpeg import probe_video

    probed = probe_video(artifact_path, binary=resolve_ffmpeg_bin())
    assert probed.duration_us > 0 and probed.height and probed.height > 0

    # — provenance: command_id ↔ operation_id ↔ job_id ↔ artifact —
    derived_command_id = f"cmd-{key}-timeline.trim"  # the worker's derivation rule
    provenance = {
        "command_id": derived_command_id,
        "operation_id": "timeline.trim",
        "job_id": job_id,
        "idempotency_key": key,
        "artifact": {
            "sha256": result["sha256"],
            "size_bytes": size,
            "duration_us": int(result["duration_us"]),
            "height": int(result["height"]),
        },
    }
    assert derived_command_id.startswith("cmd-") and derived_command_id.endswith("-timeline.trim")

    # — Persist → Reopen: fresh queue instance in this process —
    reopened = InProcessJobQueue(queue.db_path)
    reopened_status = await reopened.get_status(job_id)
    reopened_result = await reopened.get_result(job_id)
    assert reopened_status is JobStatus.COMPLETED
    assert reopened_result is not None and reopened_result["sha256"] == result["sha256"]

    # — Reopen in a genuinely fresh interpreter (process-restart equivalent) —
    script = (
        "import asyncio, hashlib, json, sys;\n"
        "from pathlib import Path;\n"
        "from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue\n"
        "from nexus_ai_agent.application.ports.job_queue import JobStatus\n"
        f"q = InProcessJobQueue({str(queue.db_path)!r})\n"
        f"status = asyncio.run(q.get_status({job_id!r}))\n"
        f"res = asyncio.run(q.get_result({job_id!r}))\n"
        "p = Path(res['artifact_path'])\n"
        "out = {'status': status.value, 'sha256': res['sha256'], 'operation': res['operation'],\n"
        "       'file_sha256': 'sha256:' + hashlib.sha256(p.read_bytes()).hexdigest(),\n"
        "       'size': p.stat().st_size, 'exists': p.is_file()}\n"
        "print(json.dumps(out))\n"
    )
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    fresh = json.loads(proc.stdout.strip().splitlines()[-1])
    assert fresh["status"] == "completed"
    assert fresh["operation"] == "timeline.trim"
    assert fresh["sha256"] == fresh["file_sha256"] == result["sha256"]
    assert fresh["size"] == size

    # crash-recovery semantics: terminal rows are never re-executed on resume
    assert await reopened.resume_pending() == []

    # — distinctions: three DIFFERENT facts, all recorded separately —
    EVIDENCE["runtime_completed"] = True
    EVIDENCE["artifact_verified"] = True
    EVIDENCE["job_succeeded"] = True
    EVIDENCE["provenance"] = provenance

    step = _step("T02")
    step.set("JOB", "PASS", f"durable job {job_id} terminal COMPLETED")
    step.set("REAL_RUNTIME", "PASS", "one real FFmpeg lane execution (spy count == 1)")
    step.set(
        "ARTIFACT",
        "PASS",
        "exists ∧ size>0 ∧ sha256(bytes) == reported ∧ probe duration/height > 0",
    )
    step.set(
        "REOPEN",
        "PASS",
        "fresh queue instance + fresh interpreter re-read identical job/artifact truth",
    )
    step.set("E2E", "PASS", "mapper intent → queue → bus → lane → verified artifact → reopen")
    step.provenance.update(provenance)

    EVIDENCE["reopen"] = {
        "fresh_instance_status": reopened_status.value,
        "fresh_interpreter": fresh,
        "resume_pending_reexecuted": 0,
        "bus_state_limitation": (
            "Project/EditTransaction state is in-memory per CommandBus by contract "
            "(PR#68): durable reopen covers job rows + artifact facts; bus history "
            "reopen across restart is NOT_VERIFIED (no persistence layer exists)."
        ),
    }

    # NB: the workspace is intentionally NOT cleaned here — production cleanup
    # (cleanup_workspace) runs after delivery, which is outside this gate.


# ---------------------------------------------------------------------------
# 4. failure matrix — every rejection cross-layer and side-effect free
# ---------------------------------------------------------------------------


async def test_f1_unknown_operation_rejected_before_job_execution(
    slice_env, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    queue, creative_tmp = slice_env
    spy = _render_spy(monkeypatch)

    # bus face: unregistered operation never reaches a handler
    bus = _journey_bus()
    before = (bus.state_revision, len(bus.history))
    with pytest.raises(UnknownOperationError):
        bus.dispatch(
            _command(bus, command_id="cmd-unknown", operation="timeline.does_not_exist", input={})
        )
    assert (bus.state_revision, len(bus.history)) == before

    # surface/worker face: unmapped operation completes typed, zero runtime
    workspace = creative_tmp / "creative_gate4_unknown"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")
    key = "gate:42:4242:2001"
    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key,
        payload=_payload(workspace, key, command="edit", operation="split"),
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is False
    assert result["error_code"] == "unsupported_operation"
    assert spy["count"] == 0, "unknown operation must never reach the runtime"
    status = await queue.get_status(job_id)
    assert status is JobStatus.COMPLETED  # typed refusal completes; no FAILED noise
    _record_failure(
        "unknown_operation",
        "UnknownOperationError @ bus; unsupported_operation @ worker; runtime calls=0",
    )


async def test_f2_invalid_schema_rejected_before_execution(
    slice_env,
) -> None:  # noqa: ANN001
    queue, creative_tmp = slice_env
    bus = _journey_bus()
    before = (bus.state_revision, bus.state_hash, len(bus.history))

    # envelope: missing actor/provenance (schema 2) fails closed before gates
    with pytest.raises(CommandValidationError):
        bus.dispatch({"command_id": "bad", "operation": "timeline.trim", "input": {}})

    # input schema: extra fields are forbidden (extra="forbid")
    with pytest.raises(CommandValidationError):
        bus.dispatch(
            _command(
                bus,
                command_id="cmd-bad-input",
                operation="timeline.trim",
                input={"clip_asset_id": "src", "in_point_us": 0, "bogus_extra": 1},
            )
        )
    assert (bus.state_revision, bus.state_hash, len(bus.history)) == before

    # worker face: malformed payload completes typed, zero runtime
    workspace = creative_tmp / "creative_gate4_schema"
    workspace.mkdir(parents=True)
    key = "gate:42:4242:2002"
    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key,
        payload={
            "command": "edit",
            "operation": "trim",
            "args": ["0", "1"],
            "workspace_dir": str(workspace),
            "user_id": 42,
            # chat_id missing → CreativeRenderPayload validation fails
            "idempotency_key": key,
        },
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is False
    assert result["error_code"] == "invalid_request"
    _record_failure(
        "invalid_schema", "CommandValidationError @ bus; invalid_request @ worker; state untouched"
    )


def test_f3_capability_unavailable_rejected_before_runtime() -> None:
    # lifecycle face (Agent-1 four-state consumed): EXPERIMENTAL without opt-in
    bus = _journey_bus(allow_experimental=False)
    before = (bus.state_revision, len(bus.history))
    with pytest.raises(PackRequirementError):
        bus.dispatch(
            _command(
                bus,
                command_id="cmd-exp",
                operation="motion.add_transition",
                input={"left_clip_id": "src", "right_clip_id": "broll"},
            )
        )
    assert (bus.state_revision, len(bus.history)) == before
    _record_failure(
        "capability_unavailable_lifecycle",
        "PackRequirementError before handler; state untouched",
    )


async def test_f3b_caption_engine_unavailable_completes_typed_without_runtime(
    slice_env, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    queue, creative_tmp = slice_env
    spy = _render_spy(monkeypatch)
    workspace = creative_tmp / "creative_gate4_caption"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")
    key = "gate:42:4242:2003"
    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key,
        payload=_payload(workspace, key, command="caption", operation="transcribe", args=[]),
    )
    assert await _drain(queue, job_id) is JobStatus.COMPLETED
    result = await queue.get_result(job_id)
    assert result is not None and result["success"] is False
    assert result["error_code"] == "caption_profile_unavailable"
    assert spy["count"] == 0, "no runtime for an unavailable capability"
    assert not list(workspace.glob("captions.*")), "no artifact was produced"
    _record_failure(
        "capability_unavailable_caption",
        "caption_profile_unavailable typed; runtime calls=0; no artifact",
    )


def test_f4_authorization_failure_rejects_before_side_effect() -> None:
    # intruder actor: grant is bound to TEST_ACTOR, not to the envelope claim
    bus = _journey_bus()
    before = (bus.state_revision, bus.state_hash, len(bus.history))
    intruder = ActorIdentity(kind="service", actor_id="nagar.intruder")
    with pytest.raises(AuthorizationError):
        bus.dispatch(
            TypedCommand(
                command_id="cmd-intruder",
                actor=intruder,
                provenance=TEST_PROVENANCE,
                operation="timeline.trim",
                target=TargetRef(project_id="p_gate4"),
                input={"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
            )
        )
    # project claim mismatch: target project is only a claim
    with pytest.raises(AuthorizationError):
        bus.dispatch(
            _command(
                bus,
                command_id="cmd-other-project",
                operation="timeline.trim",
                target=TargetRef(project_id="p_someone_else"),
                input={"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
            )
        )
    # fail-closed composition: no authorizer means no dispatch at all
    untrusted = _journey_bus(authorizer=False)
    with pytest.raises(AuthorizationError):
        untrusted.dispatch(
            _command(
                untrusted,
                command_id="cmd-anon",
                operation="timeline.trim",
                input={"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
            )
        )
    assert (bus.state_revision, bus.state_hash, len(bus.history)) == before
    _record_failure(
        "authorization_failure",
        "AuthorizationError for intruder/mismatched-project/absent-authorizer; zero side effects",
    )


async def test_f5_runtime_failure_never_reports_success(
    slice_env, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    queue, creative_tmp = slice_env
    workspace = creative_tmp / "creative_gate4_rt_fail"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")

    # (a) typed render failure (lane raises) → durable COMPLETED with
    #     success=False + typed code — Agent-3 semantics on this baseline.
    import nexus_ai_agent.creative.rendering.executor as executor

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("ffmpeg exploded")

    lane_patch = pytest.MonkeyPatch()
    lane_patch.setattr(executor, "render_lane", _boom)
    key_a = "gate:42:4242:2004"
    job_a = await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key_a,
        payload=_payload(workspace, key_a),
    )
    assert await _drain(queue, job_a) is JobStatus.COMPLETED
    result_a = await queue.get_result(job_a)
    assert result_a is not None
    assert result_a["success"] is False and result_a["error_code"] == "render_failed"
    assert "artifact_path" not in result_a, "typed failure carries no artifact claim"
    lane_patch.undo()

    # (b) unexpected post-typed failure (output probe blows up) → durable
    #     FAILED — never COMPLETED, never success.
    from nexus_ai_agent.creative.slideshow import ffmpeg as ffmpeg_mod

    real_probe = ffmpeg_mod.probe_video

    def _probe_tamper(path: Path, **kwargs: Any):
        if path.name == "output.mp4":
            raise RuntimeError("probe exploded: corrupt container")
        return real_probe(path, **kwargs)

    monkeypatch.setattr(ffmpeg_mod, "probe_video", _probe_tamper)
    key_b = "gate:42:4242:2005"
    payload_b = _payload(workspace, key_b)
    payload_b["input_path"] = str(workspace / "input.mp4")
    job_b = await queue.enqueue(
        job_type="creative_render", idempotency_key=key_b, payload=payload_b
    )
    assert await _drain(queue, job_b) is JobStatus.FAILED, "crash path must persist FAILED"
    assert await queue.get_result(job_b) is None, "FAILED rows carry no success payload"

    _record_failure(
        "runtime_failure",
        "typed lane failure → COMPLETED+success=False+render_failed; "
        "post-typed crash → durable FAILED; (FAILED_RETRYABLE/TERMINAL_FAILED "
        "split does not exist in this JobStatus enum → see report limitations)",
    )


async def test_f6_artifact_verification_failure_never_succeeds(
    slice_env, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    queue, creative_tmp = slice_env
    workspace = creative_tmp / "creative_gate4_verify"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")

    # component face (Agent-1's verifier consumed, not rebuilt):
    from nexus_ai_agent.creative.artifacts import ArtifactVerificationError, verify_artifact

    component = workspace / "component.mp4"
    _clip(component)  # the verifier demands real, probe-able bytes — not a stub
    logical = "sha256:" + hashlib.sha256(b"source").hexdigest()
    physical = verify_artifact(component, logical_content_identity=logical)
    assert Path(physical.artifact_path).is_file()
    assert physical.size_bytes > 0 and physical.probe.duration_us > 0
    assert physical.physical_sha256.startswith("sha256:")
    tampered = "sha256:" + hashlib.sha256(b"something-else").hexdigest()
    with pytest.raises(ArtifactVerificationError):
        verify_artifact(component, logical_content_identity=logical, expected_sha256=tampered)

    # cross-layer face: verification (output probe) failing inside the job
    # path → FAILED, never COMPLETED-with-success.
    from nexus_ai_agent.creative.slideshow import ffmpeg as ffmpeg_mod

    real_probe = ffmpeg_mod.probe_video

    def _probe_tamper(path: Path, **kwargs: Any):
        if path.name == "output.mp4":
            raise RuntimeError("verification failed: unreadable artifact")
        return real_probe(path, **kwargs)

    monkeypatch.setattr(ffmpeg_mod, "probe_video", _probe_tamper)
    key = "gate:42:4242:2006"
    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key,
        payload=_payload(workspace, key),
    )
    assert await _drain(queue, job_id) is JobStatus.FAILED
    row_result = await queue.get_result(job_id)
    assert row_result is None, "an unverifiable artifact can never be a success payload"
    _record_failure(
        "artifact_verification_failure",
        "verify_artifact raises on tampered bytes; in-job probe failure → FAILED, "
        "never COMPLETED-with-success",
    )


async def test_f7_duplicate_command_is_idempotent(
    slice_env, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    queue, creative_tmp = slice_env
    spy = _render_spy(monkeypatch)
    workspace = creative_tmp / "creative_gate4_dup"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")
    key = "gate:42:4242:2007"
    payload = _payload(workspace, key)

    job_1 = await queue.enqueue(job_type="creative_render", idempotency_key=key, payload=payload)
    job_2 = await queue.enqueue(job_type="creative_render", idempotency_key=key, payload=payload)
    assert job_1 == job_2, "same payload must collapse onto the durable key"
    assert await _drain(queue, job_1) is JobStatus.COMPLETED
    job_3 = await queue.enqueue(job_type="creative_render", idempotency_key=key, payload=payload)
    assert job_3 == job_1
    await asyncio.sleep(0.2)
    assert spy["count"] == 1, "exactly one runtime execution for three enqueues"

    # bus face: idempotency key replays the ORIGINAL result, one transaction
    bus = _journey_bus()
    first = bus.dispatch(
        _command(
            bus,
            command_id="cmd-idem-1",
            operation="timeline.trim",
            input={"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
            idempotency_key="idem-gate4",
        )
    )
    history_after_first = len(bus.history)
    replay = bus.dispatch(
        _command(
            bus,
            command_id="cmd-idem-2",
            operation="timeline.trim",
            input={"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
            idempotency_key="idem-gate4",
        )
    )
    assert replay.transaction_id == first.transaction_id
    assert len(bus.history) == history_after_first, "replay must not add a transaction"
    _record_failure(
        "duplicate_command", "queue: 3 enqueues → 1 job/1 render; bus: replay → same tx"
    )


async def test_f8_revision_and_payload_conflicts_are_explicit(
    slice_env,
) -> None:  # noqa: ANN001
    queue, creative_tmp = slice_env

    # bus face: stale optimistic preconditions raise explicitly, state intact
    bus = _journey_bus()
    before = (bus.state_revision, bus.state_hash, len(bus.history))
    with pytest.raises(PreconditionError):
        bus.dispatch(
            _command(
                bus,
                command_id="cmd-stale-rev",
                operation="timeline.trim",
                input={"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
                preconditions={"state_revision": 999},
            )
        )
    with pytest.raises(PreconditionError):
        bus.dispatch(
            _command(
                bus,
                command_id="cmd-stale-hash",
                operation="timeline.trim",
                input={"clip_asset_id": "src", "in_point_us": 0, "out_point_us": 1_000_000},
                preconditions={"state_hash": "sha256:" + "0" * 64},
            )
        )
    assert (bus.state_revision, bus.state_hash, len(bus.history)) == before

    # queue face (Agent-2 contract): same key + different payload = conflict
    workspace = creative_tmp / "creative_gate4_conflict"
    workspace.mkdir(parents=True)
    key = "gate:42:4242:2008"
    await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key,
        payload=_payload(workspace, key, args=["0", "1"]),
    )
    with pytest.raises(ValueError, match="different job type or payload"):
        await queue.enqueue(
            job_type="creative_render",
            idempotency_key=key,
            payload=_payload(workspace, key, args=["0", "1.5"]),
        )
    _record_failure(
        "revision_conflict",
        "PreconditionError(stale revision/hash) + queue payload-conflict ValueError",
    )


# ---------------------------------------------------------------------------
# 5. the Truth Matrix (writer consumes the evidence recorded above)
# ---------------------------------------------------------------------------


def test_truth_matrix_is_complete_legal_and_written() -> None:
    steps: dict[str, StepEvidence] = EVIDENCE["steps"]  # type: ignore[assignment]

    # every failure-matrix case must have been exercised and passed
    expected_failures = {
        "unknown_operation",
        "invalid_schema",
        "capability_unavailable_lifecycle",
        "capability_unavailable_caption",
        "authorization_failure",
        "runtime_failure",
        "artifact_verification_failure",
        "duplicate_command",
        "revision_conflict",
    }
    assert expected_failures <= set(EVIDENCE["failure_matrix"]), (
        f"missing failure cases: {expected_failures - set(EVIDENCE['failure_matrix'])}"
    )
    for name, record in EVIDENCE["failure_matrix"].items():  # type: ignore[union-attr]
        assert record["verdict"] == "PASS", f"failure case {name} did not hold"

    # the three-way distinction is recorded as three separate facts
    assert EVIDENCE["runtime_completed"] is True
    assert EVIDENCE["artifact_verified"] is True
    assert EVIDENCE["job_succeeded"] is True

    matrix = build_matrix(steps)

    # — the expected shape of this gate's truth, cell by cell —
    for t_id in SLICE_IDS:
        assert matrix["Command"][t_id] == "PASS"
        assert matrix["Capability"][t_id] == "PASS"
    for t_id in SLICE_IDS:
        if t_id == "T02":
            for layer in ("Product", "Job", "Runtime", "Artifact", "Reopen", "E2E"):
                assert matrix[layer][t_id] == "PASS", f"{layer}/{t_id} should be PASS"
        else:
            for layer in ("Product", "Job", "Runtime", "Artifact", "Reopen", "E2E"):
                assert matrix[layer][t_id] == "MISSING", f"{layer}/{t_id} should be MISSING"
    # no faked cells anywhere
    for layer in LAYERS:
        for t_id in SLICE_IDS:
            assert matrix[layer][t_id] in LEGAL_VERDICTS
            assert matrix[layer][t_id] != "FAIL"

    # Per-run volatile ids/hashes (queue job_id = uuid4, transaction_id =
    # tx_{uuid4}, and project state_hash which digests that tx uuid) are
    # asserted in the tests above — including full precondition chaining
    # (each step's precondition equals the previously observed hash/revision)
    # — but excluded here so this committed artifact is byte-deterministic
    # across runs on the same baseline. Revisions (1..5), command_ids,
    # operation_ids and artifact sha256/size/duration are content-derived
    # and stable across runs.
    def _stable_provenance(record: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v for k, v in record.items() if k not in {"job_id", "transaction_id", "state_hash"}
        }

    document = {
        "schema": "gate4-truth-matrix-v1",
        "gate": 4,
        "baseline": BASELINE,
        "volatile_ids_note": (
            "Per-run values asserted in-test but excluded from this "
            "deterministic artifact: queue job_id (uuid4), bus transaction_id "
            "(tx_{uuid4}), project state_hash (digests that tx uuid). Stable "
            "provenance kept here: command_id → operation_id → revision 1..5 "
            "→ artifact sha256/size/duration."
        ),
        "tdd_catalog_rows": len(load_tdd_catalog()),
        "tdd_catalog_rule": (
            "T01..T70 = rows of the seven pack tables in "
            "docs/NAGAR_70_OPERATIONS_TDD.md, document order (engineered rule; "
            "the ids were undefined in-tree before this gate)"
        ),
        "slice": dict(EXPECTED_SLICE),
        "cell_legend": sorted(LEGAL_VERDICTS),
        "matrix": matrix,
        "per_step_distinctions": {t_id: dict(steps[t_id].distinctions) for t_id in SLICE_IDS},
        "per_step_provenance": {
            t_id: _stable_provenance(dict(steps[t_id].provenance)) for t_id in SLICE_IDS
        },
        "provenance": _stable_provenance(dict(EVIDENCE.get("provenance", {}))),
        "failure_matrix": EVIDENCE["failure_matrix"],
        "reopen": EVIDENCE["reopen"],
        "distinctions_held": {
            "runtime_completed": EVIDENCE["runtime_completed"],
            "artifact_verified": EVIDENCE["artifact_verified"],
            "job_succeeded": EVIDENCE["job_succeeded"],
            "note": (
                "the three facts are asserted independently; failure cases F5/F6 "
                "prove they can diverge (runtime can complete while artifact "
                "verification or job success fails)"
            ),
        },
    }
    rendered = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    MATRIX_PATH.write_text(rendered, encoding="utf-8")
    # markdown view stays consistent with the JSON (same four-value cells)
    markdown = render_markdown(matrix)
    for line in markdown.splitlines()[2:]:
        for cell in line.split("|")[1:]:
            assert cell.strip() in LEGAL_VERDICTS
