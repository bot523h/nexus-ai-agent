"""Registry-integration tests for the portrait.refine pack (Wave 4)."""

from __future__ import annotations

import pytest
from portrait_fixtures import depth, image, make_context, make_project, region
from pydantic import ValidationError

from nexus_ai_agent.creative.composition.portrait_ops import (
    build_portrait_registry,
    register_portrait_operations,
)
from nexus_ai_agent.creative.packs.portrait import PORTRAIT_OPERATIONS
from nexus_ai_agent.creative.packs.portrait.errors import InvalidImageError
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry


def test_all_ten_operations_registered() -> None:
    reg = build_portrait_registry()
    for op in PORTRAIT_OPERATIONS:
        assert reg.get_spec(op).operation_id == op
    assert len(PORTRAIT_OPERATIONS) == 10


def test_registration_is_idempotent() -> None:
    reg = build_portrait_registry()
    before = len(reg._index)
    register_portrait_operations(reg)
    assert len(reg._index) == before


def test_register_on_bare_registry() -> None:
    reg = CapabilityRegistry()
    register_portrait_operations(reg)
    assert reg.get_spec("portrait.gaze_redirection")


def _run(op: str, payload: dict) -> dict:
    spec = build_portrait_registry().get_spec(op)
    return spec.handler(make_project(), make_context(payload)).output


def test_handler_gaze_and_blink() -> None:
    img = image(1)
    m = region()
    gaze = _run(
        "portrait.gaze_redirection",
        {"image_id": "i", "eye_mask_id": "m", "target_x": 0.2, "_images": {"i": img, "m": m}},
    )
    assert gaze["image"].shape == img.shape
    blink = _run(
        "portrait.eyeblink_synthesis",
        {"image_id": "i", "eye_mask_id": "m", "phase": 0.5, "_images": {"i": img, "m": m}},
    )
    assert blink["image"].shape == img.shape


def test_handler_relight_and_skintone() -> None:
    img = image(2)
    m = region()
    relit = _run("portrait.facial_relighting_gradient", {"image_id": "i", "_images": {"i": img}})
    assert relit["image"].shape == img.shape
    skin = _run(
        "portrait.skintone_harmonization",
        {"image_id": "i", "skin_mask_id": "m", "_images": {"i": img, "m": m}},
    )
    assert skin["image"].shape == img.shape


def test_handler_morph_teeth_beard() -> None:
    img = image(3)
    m = region()
    morph = _run(
        "portrait.micro_expression_morph",
        {
            "image_id": "i",
            "control_points": ((5, 5, 6, 6), (15, 5, 14, 6), (10, 15, 10, 16)),
            "_images": {"i": img},
        },
    )
    assert morph["image"].shape == img.shape
    teeth = _run(
        "portrait.teeth_whitening",
        {"image_id": "i", "teeth_mask_id": "m", "_images": {"i": img, "m": m}},
    )
    assert teeth["image"].shape == img.shape
    beard = _run(
        "portrait.facial_hair_continuity",
        {"image_id": "i", "beard_mask_id": "m", "_images": {"i": img, "m": m}},
    )
    assert beard["image"].shape == img.shape


def test_handler_repose_bokeh_aging() -> None:
    img = image(4, size=32)
    d = depth(size=32)
    repose = _run(
        "portrait.head_repose_3d", {"image_id": "i", "yaw_deg": 5.0, "_images": {"i": img}}
    )
    assert repose["image"].shape == img.shape
    bokeh = _run(
        "portrait.depth_of_field_bokeh",
        {"image_id": "i", "depth_id": "d", "_images": {"i": img, "d": d}},
    )
    assert bokeh["image"].shape == img.shape
    aged = _run(
        "portrait.aging_deaging_geometry", {"image_id": "i", "years": 20.0, "_images": {"i": img}}
    )
    assert aged["image"].shape == img.shape


def test_handler_missing_buffer_refused() -> None:
    with pytest.raises(InvalidImageError):
        _run(
            "portrait.gaze_redirection",
            {"image_id": "i", "eye_mask_id": "m", "_images": {"i": image(1)}},
        )


def test_handler_no_store_refused() -> None:
    with pytest.raises(InvalidImageError):
        _run("portrait.gaze_redirection", {"image_id": "i", "eye_mask_id": "m"})


def test_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _run("portrait.head_repose_3d", {"image_id": "i", "bogus": 1, "_images": {"i": image(1)}})


def test_models_enforce_numeric_bounds() -> None:
    with pytest.raises(ValidationError):
        _run(
            "portrait.head_repose_3d",
            {"image_id": "i", "yaw_deg": 40.0, "_images": {"i": image(1)}},
        )
