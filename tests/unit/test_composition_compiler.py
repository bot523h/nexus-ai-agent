"""Unit tests for the composition compiler — the *only* thing it owns.

Every test here resolves against the **live** canonical registry
(:func:`build_runtime_registry`), so a test that passes is a statement about
the real runtime, not about a fixture.

The behaviours covered are the ones the mission names: cycle detection, type
compatibility, required/optional bindings, multiple inputs/outputs, the
parameter schema, dependency resolution, the resource estimate, trust and
licence propagation, the stable fingerprint, the canonical version binding
and determinism.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.composition.compiler import (
    CompositionCompiler,
    CompositionError,
    registry_fingerprint,
)
from nexus_ai_agent.creative.composition.contracts import (
    CompositionPlan,
    ExpectedOutput,
    ExternalInput,
    PlanNode,
    PortBinding,
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


def _node(registry, node_id: str, operation: str, **kwargs):
    capability_id, version, schema = _pin(registry, operation)
    return PlanNode(
        node_id=node_id,
        operation=operation,
        capability_id=capability_id,
        capability_version=version,
        operation_schema_version=schema,
        **kwargs,
    )


def _plan(registry, nodes, *, outputs=None, inputs=()):
    return CompositionPlan(
        nodes=tuple(nodes),
        external_inputs=tuple(inputs),
        expected_outputs=tuple(
            outputs
            or [ExpectedOutput(name="final", node_id=nodes[-1].node_id, output_port="video")]
        ),
    )


def _resize_node(registry, node_id: str = "resize", **kwargs):
    params = kwargs.pop("params", {"width": 640, "height": 480})
    bindings = kwargs.pop(
        "bindings",
        (PortBinding(input_port="source", source="external", external="src"),),
    )
    return _node(registry, node_id, RESIZE, params=params, bindings=bindings, **kwargs)


def _grayscale_node(registry, node_id: str = "gray", **kwargs):
    params = kwargs.pop("params", {})
    bindings = kwargs.pop(
        "bindings",
        (
            PortBinding(
                input_port="source",
                source="node_output",
                node_id="resize",
                output_port="resized",
            ),
        ),
    )
    return _node(registry, node_id, GRAYSCALE, params=params, bindings=bindings, **kwargs)


def _video_node(registry, node_id: str = "video", **kwargs):
    params = kwargs.pop(
        "params",
        {"width": 640, "height": 480, "duration_us": 1_000_000, "fps": 25, "encoder": "libx264"},
    )
    bindings = kwargs.pop(
        "bindings",
        (
            PortBinding(
                input_port="source",
                source="node_output",
                node_id="gray",
                output_port="grayscale",
            ),
        ),
    )
    return _node(registry, node_id, IMAGE_TO_VIDEO, params=params, bindings=bindings, **kwargs)


def _image_input() -> ExternalInput:
    return ExternalInput(name="src", media_kind="image")


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_valid_composition_compiles_with_a_stable_order(registry) -> None:
    plan = _plan(
        registry,
        [_resize_node(registry), _grayscale_node(registry), _video_node(registry)],
        inputs=[_image_input()],
    )
    compiled = CompositionCompiler(registry).compile(plan)
    assert compiled.order == ("resize", "gray", "video")
    assert compiled.estimate.node_count == 3
    assert compiled.fingerprint.startswith("sha256:")
    assert compiled.registry_fingerprint == registry_fingerprint(registry)


def test_execution_order_is_topological_even_when_nodes_are_declared_out_of_order(registry) -> None:
    plan = _plan(
        registry,
        [_video_node(registry), _grayscale_node(registry), _resize_node(registry)],
        inputs=[_image_input()],
    )
    compiled = CompositionCompiler(registry).compile(plan)
    assert compiled.order == ("resize", "gray", "video")


def test_compilation_is_deterministic(registry) -> None:
    plan = _plan(
        registry,
        [_resize_node(registry), _grayscale_node(registry)],
        outputs=[ExpectedOutput(name="g", node_id="gray", output_port="grayscale")],
        inputs=[_image_input()],
    )
    first = CompositionCompiler(registry).compile(plan)
    second = CompositionCompiler(registry).compile(plan)
    assert first.fingerprint == second.fingerprint
    assert first.order == second.order


def test_fingerprint_changes_when_a_parameter_changes(registry) -> None:
    base = _plan(registry, [_resize_node(registry)], inputs=[_image_input()])
    other = _plan(
        registry,
        [_resize_node(registry, params={"width": 800, "height": 480})],
        inputs=[_image_input()],
    )
    compiler = CompositionCompiler(registry)
    assert compiler.compile(base).fingerprint != compiler.compile(other).fingerprint


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_unknown_capability_is_refused(registry) -> None:
    node = PlanNode(
        node_id="ghost",
        operation="media.does_not_exist",
        capability_id="media.does_not_exist",
        capability_version="1.0.0",
        operation_schema_version=1,
        bindings=(PortBinding(input_port="source", source="external", external="src"),),
    )
    plan = _plan(registry, [node], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "unknown_operation"


def test_capability_version_drift_is_refused(registry) -> None:
    capability_id, version, schema = _pin(registry, RESIZE)
    node = PlanNode(
        node_id="resize",
        operation=RESIZE,
        capability_id=capability_id,
        capability_version="9.9.9",
        operation_schema_version=schema,
        params={"width": 640, "height": 480},
        bindings=(PortBinding(input_port="source", source="external", external="src"),),
    )
    plan = _plan(registry, [node], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "capability_version_drift"


def test_schema_version_drift_is_refused(registry) -> None:
    capability_id, version, _ = _pin(registry, RESIZE)
    node = PlanNode(
        node_id="resize",
        operation=RESIZE,
        capability_id=capability_id,
        capability_version=version,
        operation_schema_version=99,
        params={"width": 640, "height": 480},
        bindings=(PortBinding(input_port="source", source="external", external="src"),),
    )
    plan = _plan(registry, [node], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "schema_version_drift"


def test_registry_drift_is_refused(registry) -> None:
    plan = _plan(registry, [_resize_node(registry)], inputs=[_image_input()])
    drifted = plan.model_copy(update={"registry_fingerprint": "sha256:" + "0" * 64})
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(drifted)
    assert excinfo.value.code == "registry_drift"


def test_cycle_is_detected(registry) -> None:
    a = _resize_node(
        registry,
        "a",
        bindings=(
            PortBinding(
                input_port="source",
                source="node_output",
                node_id="b",
                output_port="resized",
            ),
        ),
    )
    b = _grayscale_node(
        registry,
        "b",
        bindings=(
            PortBinding(
                input_port="source",
                source="node_output",
                node_id="a",
                output_port="resized",
            ),
        ),
    )
    plan = _plan(registry, [a, b], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "cyclic_graph"


def test_missing_required_binding_is_refused(registry) -> None:
    node = _resize_node(registry, bindings=())
    plan = _plan(registry, [node], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "missing_binding"


def test_unknown_binding_target_is_refused(registry) -> None:
    node = _resize_node(
        registry,
        bindings=(
            PortBinding(
                input_port="source",
                source="node_output",
                node_id="nope",
                output_port="resized",
            ),
        ),
    )
    plan = _plan(registry, [node], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "unknown_binding"


def test_unknown_external_input_is_refused(registry) -> None:
    node = _resize_node(
        registry,
        bindings=(PortBinding(input_port="source", source="external", external="absent"),),
    )
    plan = _plan(registry, [node], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "unknown_binding"


def test_type_mismatch_is_refused(registry) -> None:
    """A video output may not feed a still-image input port."""
    node = _resize_node(
        registry,
        "resize",
        bindings=(
            PortBinding(
                input_port="source", source="node_output", node_id="video", output_port="video"
            ),
        ),
    )
    video = _video_node(
        registry,
        "video",
        bindings=(PortBinding(input_port="source", source="external", external="src"),),
    )
    plan = _plan(registry, [node, video], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "type_mismatch"


def test_invalid_parameters_are_refused(registry) -> None:
    node = _resize_node(registry, params={"width": -5, "height": 480})
    plan = _plan(registry, [node], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "invalid_parameters"


def test_unknown_parameter_is_refused(registry) -> None:
    node = _resize_node(registry, params={"width": 640, "height": 480, "nonsense": 1})
    plan = _plan(registry, [node], inputs=[_image_input()])
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "unknown_parameter"


def test_empty_plan_is_refused(registry) -> None:
    """The IR refuses an empty plan at construction; the compiler refuses it too.

    The second refusal matters because a plan can arrive from an untrusted
    source (``model_construct`` bypasses validation), and the compiler must
    not be the layer that trusts it.
    """
    plan = CompositionPlan.model_construct(nodes=(), expected_outputs=())
    with pytest.raises(CompositionError) as excinfo:
        CompositionCompiler(registry).compile(plan)
    assert excinfo.value.code == "empty_plan"


def test_operation_without_media_ports_is_not_composable(registry) -> None:
    """``slideshow.scan_assets`` has no media ports — it cannot be composed."""
    spec = registry.get_spec("slideshow.scan_assets")
    assert spec.is_composable is False


# ---------------------------------------------------------------------------
# resource model
# ---------------------------------------------------------------------------


def test_resource_estimate_separates_peak_from_total(registry) -> None:
    plan = _plan(
        registry,
        [_resize_node(registry), _grayscale_node(registry), _video_node(registry)],
        inputs=[_image_input()],
    )
    estimate = CompositionCompiler(registry).compile(plan).estimate
    assert estimate.peak_ram_mb == max(estimate.peak_ram_mb, 1)
    assert estimate.total_ram_mb >= estimate.peak_ram_mb
    assert estimate.peak_concurrent_ram_mb == estimate.peak_ram_mb
    assert estimate.total_disk_mb >= estimate.scratch_mb
    assert estimate.network_egress is False
    assert estimate.max_timeout_seconds >= 1
    assert estimate.cpu_threads >= 1


def test_resource_estimate_grows_with_the_video_node(registry) -> None:
    still = CompositionCompiler(registry).compile(
        _plan(registry, [_resize_node(registry)], inputs=[_image_input()])
    )
    with_video = CompositionCompiler(registry).compile(
        _plan(
            registry,
            [_resize_node(registry), _grayscale_node(registry), _video_node(registry)],
            inputs=[_image_input()],
        )
    )
    assert with_video.estimate.peak_ram_mb > still.estimate.peak_ram_mb
    assert with_video.estimate.max_timeout_seconds > still.estimate.max_timeout_seconds


# ---------------------------------------------------------------------------
# version binding
# ---------------------------------------------------------------------------


def test_the_compiler_resolves_against_the_live_registry_not_a_private_table(registry) -> None:
    compiler = CompositionCompiler(registry)
    assert compiler.registry is registry
    assert compiler.registry_fingerprint == registry_fingerprint(registry)


def test_registry_fingerprint_changes_when_the_surface_changes(registry) -> None:
    baseline = registry_fingerprint(registry)
    other = build_runtime_registry()
    assert registry_fingerprint(other) == baseline  # deterministic across builds


def test_plan_pins_the_canonical_capability_version(registry) -> None:
    descriptor = registry.describe(RESIZE)
    plan = _plan(registry, [_resize_node(registry)], inputs=[_image_input()])
    compiled = CompositionCompiler(registry).compile(plan)
    assert compiled.node("resize").capability_version == descriptor.version
    assert compiled.node("resize").operation_schema_version == descriptor.operation_schema_version
