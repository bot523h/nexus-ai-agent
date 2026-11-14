"""Registry-integration tests for the scene.imaging pack (Wave 3)."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError
from scene_fixtures import image, make_context, make_project, mask

from nexus_ai_agent.creative.composition.scene_ops import (
    build_scene_registry,
    register_scene_operations,
)
from nexus_ai_agent.creative.packs.scene import SCENE_OPERATIONS
from nexus_ai_agent.creative.packs.scene.errors import InvalidImageError
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry


def test_all_ten_operations_registered() -> None:
    reg = build_scene_registry()
    for op in SCENE_OPERATIONS:
        assert reg.get_spec(op).operation_id == op
    assert len(SCENE_OPERATIONS) == 10


def test_registration_is_idempotent() -> None:
    reg = build_scene_registry()
    before = len(reg._index)
    register_scene_operations(reg)
    assert len(reg._index) == before


def test_register_on_bare_registry() -> None:
    reg = CapabilityRegistry()
    register_scene_operations(reg)
    assert reg.get_spec("scene.lens_distortion_correct")


def _run(op: str, payload: dict) -> dict:
    reg = build_scene_registry()
    spec = reg.get_spec(op)
    project = make_project()
    context = make_context(payload)
    outcome = spec.handler(project, context)
    return outcome.output


def test_handler_insert_object() -> None:
    img = image(1)
    out = _run(
        "scene.insert_object_with_shadow",
        {
            "image_id": "i",
            "object_id": "o",
            "mask_id": "m",
            "_images": {"i": img, "o": np.ones_like(img), "m": mask()},
        },
    )
    assert out["image"].shape == img.shape


def test_handler_missing_buffer_refused() -> None:
    with pytest.raises(InvalidImageError):
        _run(
            "scene.insert_object_with_shadow",
            {"image_id": "i", "object_id": "o", "mask_id": "m", "_images": {"i": image(1)}},
        )


def test_handler_no_store_refused() -> None:
    with pytest.raises(InvalidImageError):
        _run(
            "scene.insert_object_with_shadow",
            {"image_id": "i", "object_id": "o", "mask_id": "m"},
        )


def test_handler_remove_object() -> None:
    img = image(2, size=32)
    out = _run(
        "scene.remove_object_inpaint",
        {"image_id": "i", "mask_id": "m", "iters": 20, "_images": {"i": img, "m": mask(size=32)}},
    )
    assert out["image"].shape == img.shape


def test_handler_outpaint() -> None:
    out = _run(
        "scene.outpaint_canvas_expand",
        {"image_id": "i", "pad": 4, "_images": {"i": image(3, size=16)}},
    )
    assert out["image"].shape == (24, 24, 3)


def test_handler_dof() -> None:
    img = image(4, size=32)
    out = _run(
        "scene.synthesize_depth_of_field",
        {
            "image_id": "i",
            "depth_id": "d",
            "_images": {
                "i": img,
                "d": np.random.default_rng(0).random((32, 32)).astype(np.float32),
            },
        },
    )
    assert out["image"].shape == img.shape


def test_handler_fog() -> None:
    img = image(5, size=32)
    out = _run(
        "scene.add_volumetric_fog_haze",
        {
            "image_id": "i",
            "depth_id": "d",
            "beta": 0.3,
            "_images": {
                "i": img,
                "d": np.random.default_rng(0).random((32, 32)).astype(np.float32),
            },
        },
    )
    assert out["image"].shape == img.shape


def test_handler_lens_and_chromatic() -> None:
    img = image(6, size=32)
    out_lens = _run(
        "scene.lens_distortion_correct", {"image_id": "i", "k1": 0.05, "_images": {"i": img}}
    )
    assert out_lens["image"].shape == img.shape
    out_ca = _run(
        "scene.chromatic_aberration_control",
        {"image_id": "i", "scale_r": 1.01, "_images": {"i": img}},
    )
    assert out_ca["image"].shape == img.shape


def test_handler_horizon_returns_angle() -> None:
    # a clean horizontal-horizon image (brightness varies along y only)
    rows = np.linspace(0, 1, 24).reshape(24, 1, 1)
    img = np.repeat(np.repeat(rows, 24, axis=1), 3, axis=2).astype(np.float32)
    out = _run("scene.perspective_horizon_level", {"image_id": "i", "_images": {"i": img}})
    assert out["angle_deg"] == pytest.approx(0.0, abs=1e-6)


def test_handler_sky_relit() -> None:
    img = image(8)
    out = _run(
        "scene.sky_replacement_relit",
        {
            "image_id": "i",
            "mask_id": "m",
            "sky_id": "s",
            "_images": {"i": img, "m": mask(), "s": np.full((24, 24, 3), 0.5, np.float32)},
        },
    )
    assert out["image"].shape == img.shape


def test_handler_reflection() -> None:
    img = image(9, size=16)
    out = _run(
        "scene.reflection_water_glass_synth",
        {"image_id": "i", "fresnel": 0.1, "waves": 1.0, "_images": {"i": img}},
    )
    assert out["image"].shape == img.shape


def test_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _run(
            "scene.lens_distortion_correct",
            {"image_id": "i", "bogus": 1, "_images": {"i": image(1)}},
        )


def test_wave9_radiative_transfer_op_registered() -> None:
    from nexus_ai_agent.creative.packs.scene import RadiativeTransferHarmonizeInput

    reg = build_scene_registry()
    spec = reg.get_spec("scene.harmonize_radiative_transfer")
    assert spec.input_model is RadiativeTransferHarmonizeInput


def test_wave9_radiative_transfer_handler_relights() -> None:
    size = 24
    y, x = np.mgrid[0:size, 0:size]
    nx = (x - size / 2) / (size / 2)
    ny = (y - size / 2) / (size / 2)
    nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.05, None))
    normals = np.stack([nx, ny, nz], axis=-1)
    normals = normals / np.linalg.norm(normals, axis=-1, keepdims=True)
    img = np.clip(normals[..., 2:3] * np.array([0.8, 0.7, 0.6]), 0.0, 1.0).astype(np.float32)
    out = _run(
        "scene.harmonize_radiative_transfer",
        {
            "image_id": "i",
            "normal_id": "n",
            "mask_id": "m",
            "target_ambient": 0.6,
            "_images": {"i": img, "n": normals, "m": mask(size=size, box=(0, 0, size, size))},
        },
    )
    assert out["image"].shape == img.shape
    assert np.isfinite(out["image"]).all()


def test_wave10_optical_flow_retime_op_registered() -> None:
    from nexus_ai_agent.creative.packs.scene import OpticalFlowRetimeInput

    reg = build_scene_registry()
    spec = reg.get_spec("scene.retime_optical_flow")
    assert spec.input_model is OpticalFlowRetimeInput


def test_wave10_optical_flow_retime_interpolates() -> None:
    size = 32
    y, x = np.mgrid[0:size, 0:size].astype(np.float64)
    a = (0.5 + 0.3 * np.sin(0.3 * x) + 0.2 * np.cos(0.4 * y)).astype(np.float32)
    shift = 2.0
    b = np.roll(a, int(shift), axis=1)
    out = _run(
        "scene.retime_optical_flow",
        {"image_a_id": "a", "image_b_id": "b", "t": 0.5, "_images": {"a": a, "b": b}},
    )
    assert out["image"].shape == a.shape
    assert np.isfinite(out["image"]).all()
    assert float(out["image"].min()) >= 0.0
    assert float(out["image"].max()) <= 1.0


def test_wave11_tonemap_hdr_op_registered() -> None:
    from nexus_ai_agent.creative.packs.scene import HdrTonemapInput

    reg = build_scene_registry()
    spec = reg.get_spec("scene.tonemap_hdr")
    assert spec.input_model is HdrTonemapInput


def test_wave11_tonemap_hdr_compresses_hdr() -> None:
    size = 16
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float64)
    hdr_img = (
        np.stack([xs / (size - 1), ys / (size - 1), np.full((size, size), 0.5)], -1) * 4.0
    ).astype(np.float32)
    out = _run("scene.tonemap_hdr", {"image_id": "i", "curve": "aces", "_images": {"i": hdr_img}})
    assert out["image"].shape == hdr_img.shape
    assert float(out["image"].min()) >= 0.0
    assert float(out["image"].max()) <= 1.0


def test_wave11_tonemap_hdr_adapts_temperature() -> None:
    size = 8
    neutral = np.full((size, size, 3), 0.5, dtype=np.float32)
    out = _run(
        "scene.tonemap_hdr",
        {
            "image_id": "i",
            "curve": "reinhard",
            "white": 1.0,
            "adapt_temperature_k": 3000.0,
            "_images": {"i": neutral},
        },
    )
    assert float(out["image"][..., 0].mean()) > float(out["image"][..., 2].mean())


def test_wave12_film_emulation_op_registered() -> None:
    from nexus_ai_agent.creative.packs.scene import FilmEmulationInput

    reg = build_scene_registry()
    spec = reg.get_spec("scene.film_emulation")
    assert spec.input_model is FilmEmulationInput


def test_wave12_film_emulation_is_deterministic() -> None:
    size = 24
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float64)
    img = np.stack([xs / (size - 1), ys / (size - 1), np.full((size, size), 0.5)], -1).astype(
        np.float32
    )
    payload = {"image_id": "i", "frame_index": 3, "seed": 11, "_images": {"i": img}}
    out1 = _run("scene.film_emulation", payload)
    out2 = _run("scene.film_emulation", payload)
    assert np.array_equal(out1["image"], out2["image"])
    assert float(out1["image"].min()) >= 0.0
    assert float(out1["image"].max()) <= 1.0


class _StubSceneKernel:
    """Records the delegation; returns a sentinel outcome."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def execute(self, operation_id, project, context):  # noqa: ANN001, ANN201
        from nexus_ai_agent.creative.studio.capabilities import OperationOutcome

        self.calls.append((operation_id, project, context))
        return OperationOutcome(project=project, history=context.history, output={"sentinel": True})


def test_pack_registry_registers_fourteen_and_is_idempotent() -> None:
    from nexus_ai_agent.creative.packs.scene.operations import build_scene_registry as pack_build
    from nexus_ai_agent.creative.packs.scene.operations import register_scene_operations

    reg = pack_build()
    scene_ops = sorted(op for op in reg.list_operations() if op.startswith("scene."))
    assert len(scene_ops) == 14
    before = len(reg._index)
    register_scene_operations(reg)
    assert len(reg._index) == before


def test_pack_delegate_fails_closed_without_kernel() -> None:
    from nexus_ai_agent.creative.packs.scene.operations import (
        CODE_REQUIRES_SCENE_KERNEL,
        build_scene_registry as pack_build,
    )
    from nexus_ai_agent.creative.studio.models import CommandValidationError

    reg = pack_build()
    spec = reg.get_spec("scene.lens_distortion_correct")
    payload = {"image_id": "i", "_images": {}}
    with pytest.raises(CommandValidationError, match=CODE_REQUIRES_SCENE_KERNEL):
        spec.handler(make_project(), make_context(payload))


def test_pack_delegate_delegates_to_injected_kernel() -> None:
    from nexus_ai_agent.creative.packs.scene.operations import build_scene_registry as pack_build

    reg = pack_build()
    spec = reg.get_spec("scene.lens_distortion_correct")
    project = make_project()
    kernel = _StubSceneKernel()
    ctx = make_context({"image_id": "i", "_images": {}})
    ctx = type(ctx)(
        command=ctx.command,
        input_data=ctx.input_data,
        history=ctx.history,
        scene_kernel=kernel,
    )
    out = spec.handler(project, ctx)
    assert out.output == {"sentinel": True}
    assert kernel.calls and kernel.calls[0][0] == "scene.lens_distortion_correct"
