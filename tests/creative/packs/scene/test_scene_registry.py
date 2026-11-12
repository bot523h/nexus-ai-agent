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
