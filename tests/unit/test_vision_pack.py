"""20/20 contract, runtime, provenance and CommandBus coverage for Vision."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.runtime import build_pack_runtime
from nexus_ai_agent.creative.packs.vision.operations import PORTRAIT, SCENE
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.lifecycle import PackRequirementError
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    ExecutionPolicyError,
    PermissionLevel,
    Timeline,
    TypedCommand,
    new_project,
)

DIGEST = "sha256:" + "ab" * 32


def project():
    p = new_project("vision_project", "Vision", Timeline(timeline_id="tl", duration_us=10_000_000))
    assets = [
        AssetRecord(
            asset_id="clip", media_kind="video", content_sha256=DIGEST, duration_us=10_000_000
        ),
        AssetRecord(asset_id="sky", media_kind="image", content_sha256="sha256:" + "cd" * 32),
    ]
    return p.model_copy(update={"assets": assets})


def base():
    return {
        "clip": {"asset_id": "clip", "range": {"start_us": 0, "end_us": 10_000_000}},
        "minimum_confidence": 0.5,
    }


def payload(operation: str):
    p = base()
    source = "clip"
    face = {"track_set_id": "faces", "source_asset_id": source, "confidence": 0.9}
    mask = {"mask_id": "mask", "source_asset_id": source}
    track = {"track_id": "track", "source_asset_id": source, "confidence": 0.9}
    p.update(
        {
            "portrait.detect_landmarks": {"sample_interval": 1},
            "portrait.smooth_skin": {"face_tracks": face},
            "portrait.retouch_blemish": {"mask": mask},
            "portrait.relight_face": {"face_tracks": face},
            "portrait.whiten_teeth": {"teeth_mask": mask},
            "portrait.correct_gaze": {"face_tracks": face},
            "portrait.enhance_eyes": {"face_tracks": face},
            "portrait.mask_hair": {"face_tracks": face},
            "portrait.background_blur": {"subject_mask": mask},
            "portrait.stabilize_face": {"face_tracks": face},
            "scene.segment_subject": {"query": "person"},
            "scene.remove_object": {"object_track": track, "policy": {"temporal_radius": 3}},
            "scene.replace_sky": {"sky_asset_id": "sky"},
            "scene.remove_background": {"subject_mask": mask},
            "scene.track_object": {"semantic_seed": "car"},
            "scene.track_face": {"subject": {"subject_id": "person"}},
            "scene.detect_shot_boundaries": {"threshold": 0.5},
            "scene.find_subject_moment": {
                "subject": {"subject_id": "person"},
                "event": "first_visible",
            },
            "scene.remove_logo": {"logo_mask": mask, "legal_basis": "owned"},
            "scene.auto_reframe_subject": {
                "subject_track": {**track, "subject": {"subject_id": "person"}},
                "profile": {"aspect": "9:16"},
            },
        }[operation]
    )
    return p


ALL = {**PORTRAIT, **SCENE}


@pytest.mark.parametrize("operation", ALL)
def test_every_operation_has_strict_schema_registry_and_bus_execution(operation):
    runtime = build_pack_runtime(activate=True)
    spec = runtime.registry.get_spec(operation)
    assert spec.input_model is ALL[operation].model
    assert spec.input_model.model_config.get("extra") == "forbid"
    with pytest.raises(ValidationError):
        spec.input_model.model_validate({**payload(operation), "unknown": 1})
    bus = CommandBus(project(), registry=runtime.registry, allow_experimental=True)
    confirmed = spec.permission_level == PermissionLevel.CONFIRMATION
    result = bus.dispatch(
        TypedCommand(
            command_id="cmd", operation=operation, input=payload(operation), confirmed=confirmed
        )
    )
    assert result.status == "applied" and result.output["pixel_execution"] is False
    asset = next(a for a in bus.project.assets if a.asset_id == result.output["asset_id"])
    assert asset.provenance["operation"] == operation
    assert asset.provenance["source_asset_id"] == "clip"
    assert asset.provenance["content_hash"] == asset.content_sha256


@pytest.mark.parametrize("operation", ALL)
def test_every_operation_is_deterministic_and_parameter_sensitive(operation):
    runtime = build_pack_runtime()
    spec = runtime.registry.get_spec(operation)

    def run(data):
        bus = CommandBus(project(), registry=runtime.registry, allow_experimental=True)
        return bus.dispatch(
            TypedCommand(
                command_id="cmd",
                operation=operation,
                input=data,
                confirmed=spec.permission_level == PermissionLevel.CONFIRMATION,
            )
        ).output["content_sha256"]

    assert run(payload(operation)) == run(payload(operation))
    changed = payload(operation)
    changed["minimum_confidence"] = 0.6
    assert run(changed) != run(payload(operation))


@pytest.mark.parametrize("operation", ALL)
def test_unknown_wrong_kind_range_collision_lifecycle_and_permission(operation):
    runtime = build_pack_runtime()
    spec = runtime.registry.get_spec(operation)
    data = payload(operation)
    bad = payload(operation)
    bad["clip"] = {"asset_id": "missing"}
    with pytest.raises(CommandValidationError):
        CommandBus(project(), registry=runtime.registry, allow_experimental=True).dispatch(
            TypedCommand(command_id="x", operation=operation, input=bad, confirmed=True)
        )
    audio = project().model_copy(
        update={"assets": [AssetRecord(asset_id="clip", media_kind="audio", content_sha256=DIGEST)]}
    )
    with pytest.raises(CommandValidationError):
        CommandBus(audio, registry=runtime.registry, allow_experimental=True).dispatch(
            TypedCommand(command_id="x", operation=operation, input=data, confirmed=True)
        )
    with pytest.raises(PackRequirementError):
        CommandBus(project(), registry=runtime.registry).dispatch(
            TypedCommand(command_id="x", operation=operation, input=data, confirmed=True)
        )
    if spec.permission_level == PermissionLevel.CONFIRMATION:
        with pytest.raises(ExecutionPolicyError):
            CommandBus(project(), registry=runtime.registry, allow_experimental=True).dispatch(
                TypedCommand(command_id="x", operation=operation, input=data)
            )
    collision = payload(operation)
    collision["output_asset_id"] = "clip"
    with pytest.raises(CommandValidationError):
        CommandBus(project(), registry=runtime.registry, allow_experimental=True).dispatch(
            TypedCommand(command_id="x", operation=operation, input=collision, confirmed=True)
        )


def test_identity_sensitive_operations_keep_confirmation_policy():
    runtime = build_pack_runtime()
    assert (
        runtime.registry.get_spec("portrait.correct_gaze").permission_level
        == PermissionLevel.CONFIRMATION
    )
    assert (
        runtime.registry.get_spec("scene.remove_logo").permission_level
        == PermissionLevel.CONFIRMATION
    )


def test_confidence_and_range_boundaries_are_finite_and_closed():
    model = ALL["scene.segment_subject"].model
    model.model_validate({**payload("scene.segment_subject"), "minimum_confidence": 0.0})
    model.model_validate({**payload("scene.segment_subject"), "minimum_confidence": 1.0})
    for value in (-0.01, 1.01, math.nan, math.inf):
        with pytest.raises(ValidationError):
            model.model_validate({**payload("scene.segment_subject"), "minimum_confidence": value})
    with pytest.raises(ValidationError):
        model.model_validate(
            {
                **payload("scene.segment_subject"),
                "clip": {"asset_id": "clip", "range": {"start_us": 2, "end_us": 1}},
            }
        )


def test_runtime_manifests_are_complete_and_active():
    runtime = build_pack_runtime(activate=True)
    rows = {x.package_id: x for x in runtime.status()}
    for package in ("nexus.vision.portrait", "nexus.vision.scene"):
        assert (
            len(rows[package].capabilities) == 10
            and rows[package].pending == ()
            and rows[package].active
        )
