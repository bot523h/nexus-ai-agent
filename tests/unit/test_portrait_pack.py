"""Unit tests for ``nexus.vision.portrait`` (task-152 — TDD §1.3 contract).

Properties asserted for every operation (the TDD makes them normative):

* **contract** — registered ids/levels/manifest capabilities match the TDD rows;
* **validation** — typed inputs reject unknown fields, bad assets, bad ranges;
* **execution** — deterministic derived assets/evidence (same input → same hash);
* **failure** — Level C confirmation gates, unknown assets, non-video sources;
* **purity** — a failed handler leaves the central state untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.portrait.models import (
    BLEMISH_POLICIES,
    BLUR_PROFILES,
    EDGE_FEATHER_PX,
    OPERATION_BACKGROUND_BLUR,
    OPERATION_CORRECT_GAZE,
    OPERATION_DETECT_LANDMARKS,
    OPERATION_ENHANCE_EYES,
    OPERATION_MASK_HAIR,
    OPERATION_RELIGHT_FACE,
    OPERATION_RETOUCH_BLEMISH,
    OPERATION_SMOOTH_SKIN,
    OPERATION_STABILIZE_FACE,
    OPERATION_WHITEN_TEETH,
    PORTRAIT_PACKAGE_ID,
    BackgroundBlurInput,
    DetectLandmarksInput,
    LightModel,
    SmoothSkinInput,
)
from nexus_ai_agent.creative.packs.portrait.operations import (
    build_portrait_registry,
)
from nexus_ai_agent.creative.packs.vision_common import DETECTOR_ID, MODEL_DIGEST
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandResult,
    CommandValidationError,
    PermissionDeniedError,
    PermissionLevel,
    Timeline,
    UnknownOperationError,
    new_project,
)

REPO_ROOT = Path(__file__).parents[2]
MANIFEST_PATH = (
    REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "portrait" / "pack.manifest.json"
)

CONTRACT_LEVELS: dict[str, PermissionLevel] = {
    OPERATION_DETECT_LANDMARKS: PermissionLevel.IMMEDIATE,
    OPERATION_SMOOTH_SKIN: PermissionLevel.REVERSIBLE,
    OPERATION_RETOUCH_BLEMISH: PermissionLevel.REVERSIBLE,
    OPERATION_RELIGHT_FACE: PermissionLevel.REVERSIBLE,
    OPERATION_WHITEN_TEETH: PermissionLevel.REVERSIBLE,
    OPERATION_CORRECT_GAZE: PermissionLevel.CONFIRMATION,
    OPERATION_ENHANCE_EYES: PermissionLevel.REVERSIBLE,
    OPERATION_MASK_HAIR: PermissionLevel.IMMEDIATE,
    OPERATION_BACKGROUND_BLUR: PermissionLevel.REVERSIBLE,
    OPERATION_STABILIZE_FACE: PermissionLevel.REVERSIBLE,
}


def _setup_bus() -> CommandBus:
    timeline = Timeline(timeline_id="tl_portrait", duration_us=6_000_000)
    project = new_project("proj_portrait", "Portrait Test", timeline)
    project = project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="cam_a",
                    media_kind="video",
                    content_sha256="sha256:camahash",
                    duration_us=6_000_000,
                ),
                AssetRecord(
                    asset_id="mask_01",
                    media_kind="image",
                    content_sha256="sha256:maskhash",
                    duration_us=0,
                ),
                AssetRecord(
                    asset_id="audio_01",
                    media_kind="audio",
                    content_sha256="sha256:audiohash",
                    duration_us=3_000_000,
                ),
            ]
        }
    )
    return CommandBus(project, registry=build_portrait_registry())


def _cmd(
    bus: CommandBus,
    command_id: str,
    operation: str,
    input_data: dict[str, object],
    *,
    confirmed: bool = False,
) -> CommandResult:
    return bus.dispatch(
        {
            "protocol_version": "nagar.command.v1",
            "command_id": command_id,
            "session_id": "session_t",
            "operation": operation,
            "input": input_data,
            "confirmed": confirmed,
        }
    )


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------


def test_ten_tdd_operations_are_registered_with_contract_levels() -> None:
    registry = build_portrait_registry()
    registered = {op for op in registry.list_operations() if op.startswith("portrait.")}
    assert registered == set(CONTRACT_LEVELS)
    for operation_id, level in CONTRACT_LEVELS.items():
        spec = registry.get_spec(operation_id)
        assert spec.permission_level is level, operation_id
        assert spec.required_packs == (PORTRAIT_PACKAGE_ID,), operation_id
        assert spec.deterministic is True, operation_id


def test_manifest_advertises_all_ten_capabilities() -> None:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert data["package_id"] == PORTRAIT_PACKAGE_ID
    assert set(data["capabilities"]) == set(CONTRACT_LEVELS)


def test_input_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        DetectLandmarksInput.model_validate({"clip_asset_id": "cam_a", "bogus": 1})
    with pytest.raises(ValidationError):
        BackgroundBlurInput.model_validate(
            {"clip_asset_id": "cam_a", "mask_asset_id": "m", "extra": True}
        )


def test_smooth_skin_requires_a_subject_reference() -> None:
    with pytest.raises(ValidationError):
        SmoothSkinInput.model_validate({"clip_asset_id": "cam_a"})
    assert (
        SmoothSkinInput.model_validate(
            {"clip_asset_id": "cam_a", "mask_asset_id": "mask_01"}
        ).mask_asset_id
        == "mask_01"
    )


# ---------------------------------------------------------------------------
# analysis (Level A)
# ---------------------------------------------------------------------------


def test_detect_landmarks_returns_honest_face_track_evidence() -> None:
    bus = _setup_bus()
    res = _cmd(bus, "c1", OPERATION_DETECT_LANDMARKS, {"clip_asset_id": "cam_a"})
    assert res.status == "applied"
    track = res.output["face_track"]
    assert track["detector"] == DETECTOR_ID
    assert track["model_digest"] == MODEL_DIGEST
    assert res.output["sample_count"] == len(track["samples"]) == 12  # 6s @ standard policy
    assert len(track["samples"][0]["points"]) == 68
    # time semantics: timecode_us authoritative, frame_number derived (30fps)
    assert track["samples"][0]["timecode_us"] == 0
    assert track["samples"][0]["frame_number"] == 0
    assert track["samples"][1]["timecode_us"] == 500_000
    assert track["samples"][1]["frame_number"] == 15
    assert 0.0 <= res.output["confidence"] <= 1.0


def test_detect_landmarks_is_deterministic_and_policy_sensitive() -> None:
    def digest(policy: str = "standard") -> str:
        bus = _setup_bus()
        res = _cmd(bus, "c1", OPERATION_DETECT_LANDMARKS, {"clip_asset_id": "cam_a"})
        res2 = _cmd(
            bus,
            "c2",
            OPERATION_DETECT_LANDMARKS,
            {"clip_asset_id": "cam_a", "sample_policy": policy},
        )
        return res.output["track_id"] + "|" + res2.output["track_id"]

    assert digest() == digest()
    assert digest("dense") != digest()


def test_mask_hair_derives_an_image_mask_with_evidence() -> None:
    bus = _setup_bus()
    res = _cmd(bus, "c1", OPERATION_MASK_HAIR, {"clip_asset_id": "cam_a", "face_track_id": "ft1"})
    assert res.output["mask_id"] == "cam_a_hairmask"
    mask = res.output["mask"]
    assert mask["coordinate_space"] == "normalized"
    assert mask["detector"] == DETECTOR_ID
    assert mask["confidence"] > 0
    derived = next(a for a in bus.project.assets if a.asset_id == "cam_a_hairmask")
    assert derived.media_kind == "image"
    assert derived.parent_asset_ids == ("cam_a",)
    assert derived.provenance["processor"] == "nagar.portrait.mask_hair.v1"
    assert res.output["feather_px"] == EDGE_FEATHER_PX["balanced"]


# ---------------------------------------------------------------------------
# reversible effect derivations (Level B)
# ---------------------------------------------------------------------------


def test_smooth_skin_derives_a_content_addressed_clip() -> None:
    bus = _setup_bus()
    res = _cmd(
        bus,
        "c1",
        OPERATION_SMOOTH_SKIN,
        {"clip_asset_id": "cam_a", "face_track_id": "ft1", "strength": 0.22},
    )
    assert res.output["asset_id"] == "cam_a_smoothskin"
    derived = next(a for a in bus.project.assets if a.asset_id == "cam_a_smoothskin")
    assert derived.media_kind == "video"
    assert derived.parent_asset_ids == ("cam_a",)
    assert derived.duration_us == 6_000_000
    assert derived.provenance["strength"] == 0.22
    assert derived.content_sha256 == res.output["content_sha256"]


def test_smooth_skin_is_deterministic_and_parameter_sensitive() -> None:
    def digest(*, strength: float = 0.5, stability: str = "high") -> str:
        res = _cmd(
            _setup_bus(),
            "c1",
            OPERATION_SMOOTH_SKIN,
            {
                "clip_asset_id": "cam_a",
                "face_track_id": "ft1",
                "strength": strength,
                "temporal_stability": stability,
            },
        )
        return res.output["content_sha256"]

    assert digest() == digest()
    assert digest(strength=0.3) != digest()
    assert digest(stability="low") != digest()


def test_retouch_whiten_relight_eyes_background_all_derive_video_assets() -> None:
    bus = _setup_bus()
    outputs = {
        OPERATION_RETOUCH_BLEMISH: {"clip_asset_id": "cam_a", "mask_asset_id": "mask_01"},
        OPERATION_WHITEN_TEETH: {"clip_asset_id": "cam_a", "mask_asset_id": "mask_01"},
        OPERATION_RELIGHT_FACE: {"clip_asset_id": "cam_a", "face_track_id": "ft1"},
        OPERATION_ENHANCE_EYES: {
            "clip_asset_id": "cam_a",
            "face_track_id": "ft1",
            "red_eye": True,
        },
        OPERATION_BACKGROUND_BLUR: {
            "clip_asset_id": "cam_a",
            "mask_asset_id": "mask_01",
            "blur_profile": "bokeh",
        },
    }
    for index, (operation, input_data) in enumerate(outputs.items()):
        res = _cmd(bus, f"c{index}", operation, input_data)
        assert res.status == "applied", operation
        derived = next(a for a in bus.project.assets if a.asset_id == res.output["asset_id"])
        assert derived.media_kind == "video", operation
        assert derived.parent_asset_ids, operation
        assert derived.provenance["effect"], operation

    blemish = next(a for a in bus.project.assets if a.asset_id == "cam_a_blemish")
    radius_key = BLEMISH_POLICIES["balanced"]["inpaint_radius_px"]
    assert blemish.provenance["inpaint_radius_px"] == radius_key
    blur = next(a for a in bus.project.assets if a.asset_id == "cam_a_bgblur")
    assert blur.provenance["radius_px"] == BLUR_PROFILES["bokeh"]["radius_px"]


def test_stabilize_face_emits_a_transform_curve_plan() -> None:
    bus = _setup_bus()
    res = _cmd(
        bus,
        "c1",
        OPERATION_STABILIZE_FACE,
        {
            "clip_asset_id": "cam_a",
            "face_track_id": "ft1",
            "stabilization_policy": "lock_position_scale",
        },
    )
    curve = res.output["transform_curve"]
    assert curve["policy"] == "lock_position_scale"
    assert res.output["keyframe_count"] == len(curve["samples"]) == 12
    sample = curve["samples"][0]
    assert sample["frame_number"] == 0
    assert sample["scale"] > 0
    derived = next(a for a in bus.project.assets if a.asset_id == res.output["asset_id"])
    assert derived.provenance["curve_id"] == res.output["curve_id"]


# ---------------------------------------------------------------------------
# Level C + failure paths
# ---------------------------------------------------------------------------


def test_correct_gaze_requires_level_c_confirmation() -> None:
    bus = _setup_bus()
    with pytest.raises(PermissionDeniedError):
        _cmd(bus, "c1", OPERATION_CORRECT_GAZE, {"clip_asset_id": "cam_a", "face_track_id": "ft1"})
    res = _cmd(
        bus,
        "c2",
        OPERATION_CORRECT_GAZE,
        {"clip_asset_id": "cam_a", "face_track_id": "ft1", "confirmed": True},
        confirmed=True,
    )
    assert res.status == "applied"
    derived = next(a for a in bus.project.assets if a.asset_id == res.output["asset_id"])
    assert derived.provenance["identity_sensitive"] is True


def test_unknown_assets_and_wrong_media_kinds_fail_closed() -> None:
    bus = _setup_bus()
    with pytest.raises(CommandValidationError):
        _cmd(bus, "c1", OPERATION_SMOOTH_SKIN, {"clip_asset_id": "ghost", "mask_asset_id": "m"})
    with pytest.raises(CommandValidationError):
        _cmd(
            bus,
            "c2",
            OPERATION_BACKGROUND_BLUR,
            {"clip_asset_id": "cam_a", "mask_asset_id": "cam_a"},
        )
    with pytest.raises(CommandValidationError):
        _cmd(
            bus,
            "c3",
            OPERATION_RETOUCH_BLEMISH,
            {"clip_asset_id": "audio_01", "mask_asset_id": "mask_01"},
        )


def test_a_failed_handler_leaves_the_state_untouched() -> None:
    bus = _setup_bus()
    before = bus.project.state_hash
    revision = bus.project.state_revision
    with pytest.raises((CommandValidationError, PermissionDeniedError)):
        _cmd(bus, "c1", OPERATION_CORRECT_GAZE, {"clip_asset_id": "cam_a", "face_track_id": "ft1"})
    assert bus.project.state_hash == before
    assert bus.project.state_revision == revision


def test_unknown_portrait_operation_is_rejected_by_the_bus() -> None:
    bus = _setup_bus()
    with pytest.raises(UnknownOperationError):
        _cmd(bus, "c1", "portrait.smooth_everything", {})


def test_light_model_is_typed_not_a_shader_string() -> None:
    light = LightModel.model_validate({"direction": "top", "intensity": 1.5, "warmth": -0.5})
    assert light.direction == "top"
    with pytest.raises(ValidationError):
        LightModel.model_validate({"direction": "top", "shader": "gl_FragColor"})


def test_derived_assets_survive_undo_cycles_with_stable_hashes() -> None:
    from nexus_ai_agent.creative.packs.portrait.operations import register_portrait_operations
    from nexus_ai_agent.creative.studio.capabilities import build_wave1_registry

    registry = build_wave1_registry()
    register_portrait_operations(registry)
    timeline = Timeline(timeline_id="tl_portrait", duration_us=6_000_000)
    project = new_project("proj_portrait", "Portrait Test", timeline)
    project = project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="cam_a",
                    media_kind="video",
                    content_sha256="sha256:camahash",
                    duration_us=6_000_000,
                ),
            ]
        }
    )
    bus = CommandBus(project, registry=registry)
    res = _cmd(
        bus,
        "c1",
        OPERATION_SMOOTH_SKIN,
        {"clip_asset_id": "cam_a", "face_track_id": "ft1", "strength": 0.4},
    )
    content_sha = res.output["content_sha256"]
    _cmd(bus, "undo1", "system.undo", {})
    assert all(a.asset_id != "cam_a_smoothskin" for a in bus.project.assets)
    res2 = _cmd(
        bus,
        "c2",
        OPERATION_SMOOTH_SKIN,
        {"clip_asset_id": "cam_a", "face_track_id": "ft1", "strength": 0.4},
    )
    assert res2.output["content_sha256"] == content_sha
