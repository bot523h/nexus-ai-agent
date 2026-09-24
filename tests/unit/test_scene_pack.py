"""Unit tests for ``nexus.vision.scene`` (task-153 — TDD §1.4 contract).

Same normative properties as the portrait suite (contract, validation,
execution, failure, purity, determinism), plus the task-153 decision evidence:
both vision packs consume :mod:`nexus_ai_agent.creative.packs.vision_common`
primitives and never import each other.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.scene.models import (
    ASPECT_RATIOS,
    FILL_POLICIES,
    LOGO_POLICIES,
    OPERATION_AUTO_REFRAME_SUBJECT,
    OPERATION_DETECT_SHOT_BOUNDARIES,
    OPERATION_FIND_SUBJECT_MOMENT,
    OPERATION_REMOVE_BACKGROUND,
    OPERATION_REMOVE_LOGO,
    OPERATION_REMOVE_OBJECT,
    OPERATION_REPLACE_SKY,
    OPERATION_SEGMENT_SUBJECT,
    OPERATION_TRACK_FACE,
    OPERATION_TRACK_OBJECT,
    SCENE_PACKAGE_ID,
    RemoveObjectInput,
    SegmentSubjectInput,
)
from nexus_ai_agent.creative.packs.scene.operations import build_scene_registry
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
SCENE_DIR = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "scene"
MANIFEST_PATH = SCENE_DIR / "pack.manifest.json"

CONTRACT_LEVELS: dict[str, PermissionLevel] = {
    OPERATION_SEGMENT_SUBJECT: PermissionLevel.IMMEDIATE,
    OPERATION_REMOVE_OBJECT: PermissionLevel.REVERSIBLE,
    OPERATION_REPLACE_SKY: PermissionLevel.REVERSIBLE,
    OPERATION_REMOVE_BACKGROUND: PermissionLevel.REVERSIBLE,
    OPERATION_TRACK_OBJECT: PermissionLevel.IMMEDIATE,
    OPERATION_TRACK_FACE: PermissionLevel.IMMEDIATE,
    OPERATION_DETECT_SHOT_BOUNDARIES: PermissionLevel.IMMEDIATE,
    OPERATION_FIND_SUBJECT_MOMENT: PermissionLevel.IMMEDIATE,
    OPERATION_REMOVE_LOGO: PermissionLevel.CONFIRMATION,
    OPERATION_AUTO_REFRAME_SUBJECT: PermissionLevel.REVERSIBLE,
}


def _setup_bus() -> CommandBus:
    timeline = Timeline(timeline_id="tl_scene", duration_us=6_000_000)
    project = new_project("proj_scene", "Scene Test", timeline)
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
                    asset_id="sky_01",
                    media_kind="image",
                    content_sha256="sha256:skyhash",
                    duration_us=0,
                ),
            ]
        }
    )
    return CommandBus(project, registry=build_scene_registry(), allow_experimental=True)


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
    registry = build_scene_registry()
    registered = {op for op in registry.list_operations() if op.startswith("scene.")}
    assert registered == set(CONTRACT_LEVELS)
    for operation_id, level in CONTRACT_LEVELS.items():
        spec = registry.get_spec(operation_id)
        assert spec.permission_level is level, operation_id
        assert spec.required_packs == (SCENE_PACKAGE_ID,), operation_id
        assert spec.deterministic is True, operation_id


def test_manifest_advertises_all_ten_capabilities() -> None:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert data["package_id"] == SCENE_PACKAGE_ID
    assert set(data["capabilities"]) == set(CONTRACT_LEVELS)


def test_input_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SegmentSubjectInput.model_validate({"clip_asset_id": "cam_a", "junk": 1})
    with pytest.raises(ValidationError):
        RemoveObjectInput.model_validate({"clip_asset_id": "cam_a", "object_track_id": 5})


# ---------------------------------------------------------------------------
# analysis (Level A)
# ---------------------------------------------------------------------------


def test_segment_subject_derives_a_mask_asset_with_evidence() -> None:
    bus = _setup_bus()
    res = _cmd(
        bus,
        "c1",
        OPERATION_SEGMENT_SUBJECT,
        {"clip_asset_id": "cam_a", "semantic_query": "the person in frame"},
    )
    assert res.output["mask_id"] == "cam_a_mask"
    mask = res.output["mask"]
    assert mask["detector"] == DETECTOR_ID
    assert mask["model_digest"] == MODEL_DIGEST
    assert 0.0 < mask["coverage_ratio"] <= 1.0
    derived = next(a for a in bus.project.assets if a.asset_id == "cam_a_mask")
    assert derived.media_kind == "image"
    assert derived.parent_asset_ids == ("cam_a",)


def test_track_object_and_track_face_emit_honest_tracks() -> None:
    bus = _setup_bus()
    obj = _cmd(
        bus,
        "c1",
        OPERATION_TRACK_OBJECT,
        {"clip_asset_id": "cam_a", "semantic_seed": "the wire in the sky"},
    )
    face = _cmd(bus, "c2", OPERATION_TRACK_FACE, {"clip_asset_id": "cam_a"})
    assert obj.output["object_track"]["detector"] == DETECTOR_ID
    assert face.output["face_track"]["model_digest"] == MODEL_DIGEST
    assert obj.output["sample_count"] == 12  # 6s @ standard policy
    assert face.output["sample_count"] == 12


def test_detect_shot_boundaries_filters_by_threshold() -> None:
    bus = _setup_bus()
    loose = _cmd(
        bus, "c1", OPERATION_DETECT_SHOT_BOUNDARIES, {"clip_asset_id": "cam_a", "threshold": 0.0}
    )
    strict = _cmd(
        bus, "c2", OPERATION_DETECT_SHOT_BOUNDARIES, {"clip_asset_id": "cam_a", "threshold": 1.0}
    )
    assert loose.output["boundary_count"] >= strict.output["boundary_count"]
    assert strict.output["boundary_count"] == 0
    assert loose.output["shot_boundaries"]["detector"] == DETECTOR_ID


def test_find_subject_moment_pins_deterministic_candidates() -> None:
    bus = _setup_bus()
    res = _cmd(
        bus,
        "c1",
        OPERATION_FIND_SUBJECT_MOMENT,
        {
            "clip_asset_id": "cam_a",
            "subject_id": "person_me_01",
            "event": "first_visible",
            "minimum_confidence": 0.5,
        },
    )
    assert res.output["event"] == "first_visible"
    candidates = res.output["moments"]["candidates"]
    assert res.output["candidate_count"] == len(candidates) == 1
    moment = candidates[0]
    assert moment["subject_id"] == "person_me_01"
    assert moment["evidence_start_us"] < moment["evidence_end_us"]
    # identical command on a fresh bus pins the identical moment
    again = _cmd(
        _setup_bus(),
        "c2",
        OPERATION_FIND_SUBJECT_MOMENT,
        {
            "clip_asset_id": "cam_a",
            "subject_id": "person_me_01",
            "event": "first_visible",
            "minimum_confidence": 0.5,
        },
    )
    assert again.output["moments"] == res.output["moments"]


# ---------------------------------------------------------------------------
# reversible effect derivations (Level B)
# ---------------------------------------------------------------------------


def test_remove_object_reports_confidence_and_needs_review() -> None:
    bus = _setup_bus()
    res = _cmd(
        bus,
        "c1",
        OPERATION_REMOVE_OBJECT,
        {
            "clip_asset_id": "cam_a",
            "object_track_id": "object-track-1",
            "minimum_confidence": 0.99,
        },
    )
    assert 0.0 <= res.output["confidence"] <= 1.0
    assert res.output["needs_review"] is True  # 0.99 bar is above the contract band
    plan = res.output["inpaint_plan"]
    assert plan["feather_px"] == FILL_POLICIES["temporal_inpaint"]["feather_px"]
    derived = next(a for a in bus.project.assets if a.asset_id == res.output["asset_id"])
    assert derived.provenance["needs_review"] is True


def test_remove_object_range_must_fit_the_clip() -> None:
    bus = _setup_bus()
    with pytest.raises(CommandValidationError):
        _cmd(
            bus,
            "c1",
            OPERATION_REMOVE_OBJECT,
            {
                "clip_asset_id": "cam_a",
                "object_track_id": "t",
                "start_us": 5_000_000,
                "duration_us": 5_000_000,
            },
        )


def test_replace_sky_and_remove_background_derive_assets() -> None:
    bus = _setup_bus()
    sky = _cmd(
        bus,
        "c1",
        OPERATION_REPLACE_SKY,
        {"clip_asset_id": "cam_a", "sky_asset_id": "sky_01", "horizon_policy": "manual"},
    )
    alpha = _cmd(
        bus,
        "c2",
        OPERATION_REMOVE_BACKGROUND,
        {"clip_asset_id": "cam_a", "mask_asset_id": "mask_01", "alpha_policy": "matte"},
    )
    sky_rec = next(a for a in bus.project.assets if a.asset_id == sky.output["asset_id"])
    alpha_rec = next(a for a in bus.project.assets if a.asset_id == alpha.output["asset_id"])
    assert sky_rec.parent_asset_ids == ("cam_a", "sky_01")
    assert alpha_rec.parent_asset_ids == ("cam_a", "mask_01")
    assert alpha_rec.provenance["alpha_layer"] is True


def test_auto_reframe_subject_emits_a_transform_curve() -> None:
    bus = _setup_bus()
    res = _cmd(
        bus,
        "c1",
        OPERATION_AUTO_REFRAME_SUBJECT,
        {"clip_asset_id": "cam_a", "object_track_id": "object-track-1", "aspect": "9:16"},
    )
    curve = res.output["transform_curve"]
    assert (res.output["width_ratio"], res.output["height_ratio"]) == ASPECT_RATIOS["9:16"]
    assert len(curve["samples"]) == 12
    assert all(s["scale"] >= 1.0 for s in curve["samples"])


def test_scene_derivations_are_deterministic_and_parameter_sensitive() -> None:
    def digest(*, fill_policy: str = "temporal_inpaint") -> str:
        res = _cmd(
            _setup_bus(),
            "c1",
            OPERATION_REMOVE_OBJECT,
            {"clip_asset_id": "cam_a", "object_track_id": "t", "fill_policy": fill_policy},
        )
        return res.output["content_sha256"]

    assert digest() == digest()
    assert digest(fill_policy="blur_fill") != digest()


# ---------------------------------------------------------------------------
# Level C + failure paths
# ---------------------------------------------------------------------------


def test_remove_logo_requires_level_c_confirmation() -> None:
    bus = _setup_bus()
    with pytest.raises(PermissionDeniedError):
        _cmd(
            bus,
            "c1",
            OPERATION_REMOVE_LOGO,
            {"clip_asset_id": "cam_a", "mask_asset_id": "mask_01"},
        )
    res = _cmd(
        bus,
        "c2",
        OPERATION_REMOVE_LOGO,
        {
            "clip_asset_id": "cam_a",
            "mask_asset_id": "mask_01",
            "legal_policy": "inpaint",
            "confirmed": True,
        },
        confirmed=True,
    )
    assert res.output["fill_radius_px"] == LOGO_POLICIES["inpaint"]["fill_radius_px"]
    derived = next(a for a in bus.project.assets if a.asset_id == res.output["asset_id"])
    assert derived.provenance["legal_surface"] is True


def test_masks_must_be_image_assets_and_clips_must_be_video() -> None:
    bus = _setup_bus()
    with pytest.raises(CommandValidationError):
        _cmd(
            bus,
            "c1",
            OPERATION_REMOVE_BACKGROUND,
            {"clip_asset_id": "cam_a", "mask_asset_id": "cam_a"},
        )
    with pytest.raises(CommandValidationError):
        _cmd(bus, "c2", OPERATION_TRACK_OBJECT, {"clip_asset_id": "mask_01", "semantic_seed": "x"})


def test_a_failed_handler_leaves_the_state_untouched() -> None:
    bus = _setup_bus()
    before = bus.project.state_hash
    with pytest.raises((CommandValidationError, PermissionDeniedError)):
        _cmd(bus, "c1", OPERATION_REMOVE_OBJECT, {"clip_asset_id": "ghost", "object_track_id": "t"})
    assert bus.project.state_hash == before


def test_unknown_scene_operation_is_rejected_by_the_bus() -> None:
    bus = _setup_bus()
    with pytest.raises(UnknownOperationError):
        _cmd(bus, "c1", "scene.teleport_subject", {})


# ---------------------------------------------------------------------------
# task-153 decision evidence: shared primitives, no cross-pack imports
# ---------------------------------------------------------------------------


def test_vision_packs_share_primitives_but_never_import_each_other() -> None:
    packs_root = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "packs"
    shared_hits = {"scene": False, "portrait": False}
    for name in ("scene", "portrait"):
        for path in sorted((packs_root / name).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
                elif isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                for module in modules:
                    if module.startswith("nexus_ai_agent.creative.packs.vision_common"):
                        shared_hits[name] = True
                    for sibling in ("portrait", "scene"):
                        if sibling != name and module.startswith(
                            f"nexus_ai_agent.creative.packs.{sibling}"
                        ):
                            raise AssertionError(
                                f"{name}/{path.name} imports sibling pack via {module!r}"
                            )
    assert shared_hits == {"scene": True, "portrait": True}
