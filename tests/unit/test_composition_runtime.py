"""Unit tests for the composition runtime: the executor and the queue adapter.

The executor is exercised against the **live** canonical registry and the
**canonical** command bus, so these are statements about the real runtime.
The queue adapter is exercised as a worker function, exactly as the canonical
queue calls it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError as PydanticValidationError

from nexus_ai_agent.creative.composition.compiler import CompositionCompiler
from nexus_ai_agent.creative.composition.contracts import (
    CompositionPlan,
    ExpectedOutput,
    ExternalInput,
    PlanNode,
    PortBinding,
)
from nexus_ai_agent.creative.composition.executor import (
    CompositionExecutionError,
    CompositionExecutor,
)
from nexus_ai_agent.creative.composition.job import (
    COMPOSITION_JOB_TYPE,
    CompositionJobPayload,
    plan_from_dict,
    plan_to_dict,
)
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry

RESIZE = "media.resize_image"
GRAYSCALE = "media.grayscale_image"
IMAGE_TO_VIDEO = "media.image_to_video"


@pytest.fixture(scope="module")
def registry():
    return build_runtime_registry()


def _pin(registry, operation: str) -> tuple[str, str, int]:
    descriptor = registry.describe(operation)
    return descriptor.capability_id, descriptor.version, descriptor.operation_schema_version


def _source_image(path: Path, size=(320, 240)) -> Path:
    Image.new("RGB", size, (200, 60, 30)).save(path)
    return path


def _plan(registry, *, with_video: bool = True) -> CompositionPlan:
    rc, rv, rs = _pin(registry, RESIZE)
    gc, gv, gs = _pin(registry, GRAYSCALE)
    nodes = [
        PlanNode(
            node_id="resize",
            operation=RESIZE,
            capability_id=rc,
            capability_version=rv,
            operation_schema_version=rs,
            params={"width": 1280, "height": 720},
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
        vc, vv, vs = _pin(registry, IMAGE_TO_VIDEO)
        nodes.append(
            PlanNode(
                node_id="video",
                operation=IMAGE_TO_VIDEO,
                capability_id=vc,
                capability_version=vv,
                operation_schema_version=vs,
                params={
                    "width": 1280,
                    "height": 720,
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


# ---------------------------------------------------------------------------
# the executor
# ---------------------------------------------------------------------------


def test_the_executor_runs_a_plan_on_the_canonical_bus(registry, tmp_path: Path) -> None:
    source = _source_image(tmp_path / "source.png")
    compiled = CompositionCompiler(registry).compile(_plan(registry))
    result = CompositionExecutor().execute(
        compiled, external_inputs={"src": source}, workspace=tmp_path / "work"
    )
    assert result.verified is True
    assert result.state_revision > 0
    assert len(result.receipts) == 3
    assert result.outputs["final"].endswith(".mp4")


def test_every_node_is_verified_not_only_the_output(registry, tmp_path: Path) -> None:
    source = _source_image(tmp_path / "source.png")
    compiled = CompositionCompiler(registry).compile(_plan(registry))
    result = CompositionExecutor().execute(
        compiled, external_inputs={"src": source}, workspace=tmp_path / "work"
    )
    for receipt in result.receipts:
        assert receipt.semantic is not None
        assert receipt.semantic.verified is True, receipt.semantic.findings
    # the intermediate grayscale is really grayscale
    gray = next(r for r in result.receipts if r.node_id == "gray")
    assert gray.semantic.measurements["grayscale"] is True
    # the intermediate resize really is the requested geometry
    resize = next(r for r in result.receipts if r.node_id == "resize")
    assert resize.semantic.measurements["width"] == 1280
    assert resize.semantic.measurements["height"] == 720


def test_the_executor_records_the_derived_assets_on_the_canonical_bus(
    registry, tmp_path: Path
) -> None:
    source = _source_image(tmp_path / "source.png")
    compiled = CompositionCompiler(registry).compile(_plan(registry))
    executor = CompositionExecutor()
    result = executor.execute(
        compiled, external_inputs={"src": source}, workspace=tmp_path / "work"
    )
    assets = executor.bus.project.assets
    assert len(assets) >= 4  # the source + three derived assets
    derived = [asset for asset in assets if asset.parent_asset_ids]
    assert len(derived) == 3
    assert len(result.receipts) == 3


def test_the_lineage_names_every_node_and_its_capability_version(registry, tmp_path: Path) -> None:
    source = _source_image(tmp_path / "source.png")
    compiled = CompositionCompiler(registry).compile(_plan(registry))
    result = CompositionExecutor().execute(
        compiled, external_inputs={"src": source}, workspace=tmp_path / "work"
    )
    artifact = result.artifacts["final"]
    assert artifact.lineage == (
        "resize:media.resize_image@1.0.0",
        "gray:media.grayscale_image@1.0.0",
        "video:media.image_to_video@1.0.0",
    )
    assert artifact.plan_fingerprint == compiled.fingerprint
    assert artifact.registry_fingerprint == compiled.registry_fingerprint


def test_each_node_gets_its_own_scratch_directory(registry, tmp_path: Path) -> None:
    source = _source_image(tmp_path / "source.png")
    compiled = CompositionCompiler(registry).compile(_plan(registry))
    CompositionExecutor().execute(
        compiled, external_inputs={"src": source}, workspace=tmp_path / "work"
    )
    node_dirs = sorted(p.name for p in (tmp_path / "work").iterdir() if p.is_dir())
    assert node_dirs == ["node_gray", "node_resize", "node_video"]
    for name in node_dirs:
        scratch = list((tmp_path / "work" / name).glob("scratch_*"))
        assert len(scratch) == 1, f"{name} must have exactly one scratch directory"


def test_a_missing_external_input_is_refused(registry, tmp_path: Path) -> None:
    compiled = CompositionCompiler(registry).compile(_plan(registry))
    with pytest.raises(CompositionExecutionError):
        CompositionExecutor().execute(compiled, external_inputs={}, workspace=tmp_path / "work")


def test_the_engine_refuses_an_operation_it_cannot_realize(tmp_path: Path) -> None:
    """A resolved operation with no realization fails loudly, not silently.

    The compiler and the engine agree today — every composable operation in
    the live registry has a realization — so this asserts the *engine's*
    fail-closed behaviour directly rather than through a plan the compiler
    would (correctly) refuse first.
    """
    from nexus_ai_agent.creative.composition.engine import MediaEngine, MediaEngineError

    source = _source_image(tmp_path / "source.png")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    engine = MediaEngine()
    with pytest.raises(MediaEngineError) as excinfo:
        engine.realize(
            "media.not_realized",
            source=source,
            output_path=scratch / "out.png",
            params={},
            scratch_dir=scratch,
        )
    assert "no media realization" in str(excinfo.value)


def test_the_compiler_refuses_a_node_whose_capability_does_not_match_its_operation(
    registry,
) -> None:
    """A node may not claim another operation's capability."""
    from nexus_ai_agent.creative.composition.compiler import CompositionError

    plan = _plan(registry, with_video=False)
    forged = plan.nodes[0].model_copy(
        update={
            "operation": "media.image_to_video",
            "params": {
                "width": 64,
                "height": 64,
                "duration_us": 1000,
                "fps": 25,
                "encoder": "libx264",
            },
        }
    )
    tampered = plan.model_copy(update={"nodes": (forged,)})
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(tampered)
    assert excinfo.value.code == "capability_version_drift"


# ---------------------------------------------------------------------------
# the queue adapter
# ---------------------------------------------------------------------------


def test_the_plan_round_trips_through_its_durable_form(registry) -> None:
    plan = _plan(registry)
    decoded = plan_from_dict(plan_to_dict(plan))
    assert decoded.fingerprint() == plan.fingerprint()


def test_a_malformed_plan_is_a_typed_failure() -> None:
    from nexus_ai_agent.creative.composition.job import CompositionJobError

    with pytest.raises(CompositionJobError) as excinfo:
        plan_from_dict({"nodes": [{"node_id": "x"}]})
    assert excinfo.value.code == "invalid_request"


def test_a_binding_without_a_source_is_a_typed_failure() -> None:
    from nexus_ai_agent.creative.composition.job import CompositionJobError

    with pytest.raises(CompositionJobError):
        plan_from_dict(
            {
                "nodes": [
                    {
                        "node_id": "x",
                        "operation": "media.resize_image",
                        "capability_id": "media.resize_image",
                        "capability_version": "1.0.0",
                        "operation_schema_version": 1,
                        "bindings": {"source": {}},
                    }
                ],
                "expected_outputs": [{"name": "o", "node": "x", "output_port": "resized"}],
            }
        )


def test_the_payload_refuses_an_extra_field() -> None:
    with pytest.raises(PydanticValidationError):
        CompositionJobPayload.model_validate(
            {
                "plan": {},
                "workspace_dir": "/tmp/x",
                "idempotency_key": "k",
                "surprise": 1,
            }
        )


def test_the_worker_refuses_an_unknown_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed before dispatch: an unknown operation never reaches a process."""
    from nexus_ai_agent.config import settings as settings_module
    from nexus_ai_agent.creative.composition.job import composition_render_job

    root = tmp_path / "creative"
    root.mkdir()
    workspace = root / "creative_test"
    workspace.mkdir()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(root))
    settings_module.get_settings.cache_clear()
    payload = {
        "plan": {
            "nodes": [
                {
                    "node_id": "x",
                    "operation": "media.does_not_exist",
                    "capability_id": "media.does_not_exist",
                    "capability_version": "1.0.0",
                    "operation_schema_version": 1,
                    "params": {},
                    "bindings": {"source": {"external": "src"}},
                }
            ],
            "external_inputs": [{"name": "src", "media_kind": "image"}],
            "expected_outputs": [{"name": "o", "node": "x", "output_port": "resized"}],
        },
        "external_inputs": {},
        "workspace_dir": str(workspace),
        "idempotency_key": "k",
    }
    try:
        result = asyncio.run(composition_render_job(payload))
    finally:
        settings_module.get_settings.cache_clear()
    assert result["success"] is False
    assert result["error_code"] == "unknown_capability"


def test_the_job_type_name_is_stable() -> None:
    assert COMPOSITION_JOB_TYPE == "creative_composition"


def test_the_worker_returns_the_canonical_artifact_claim_dialect(
    registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.config import settings as settings_module
    from nexus_ai_agent.creative.composition.job import composition_render_job

    root = tmp_path / "creative"
    root.mkdir()
    workspace = root / "creative_test"
    workspace.mkdir()
    source = _source_image(workspace / "source.png")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(root))
    settings_module.get_settings.cache_clear()
    try:
        plan = _plan(registry)
        payload = {
            "plan": plan_to_dict(plan),
            "external_inputs": {"src": {"path": str(source), "sha256": digest}},
            "workspace_dir": str(workspace),
            "idempotency_key": "composition:1",
        }
        result = asyncio.run(composition_render_job(payload))
        assert result["success"] is True, result
        assert result["artifact_path"].endswith(".mp4")
        assert result["sha256"].startswith("sha256:")
        assert result["size_bytes"] > 0
        assert result["artifact_kind"] == "video"
        assert result["integrity_verified"] is True
        assert result["semantic_verified"] is True
        assert result["plan_fingerprint"].startswith("sha256:")
        assert result["registry_fingerprint"].startswith("sha256:")
        assert result["evidence"]["receipts"]
        assert json.dumps(result, default=str)
    finally:
        settings_module.get_settings.cache_clear()
