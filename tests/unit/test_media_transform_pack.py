"""Unit tests for the ``nexus.media.transform`` builtin pack.

The pack is the **canonical** home of the three media capabilities.  These
tests assert the properties the rest of the runtime relies on:

* the pack registers through the ordinary pack path (registry, manifest,
  lifecycle, activation completeness);
* the operations are **pure state recorders** — they never touch the
  filesystem, because the pack substrate is data-only;
* the handlers refuse a source whose content hash no longer matches the
  registered asset (a changed input is never silently re-used);
* the permission ladder is the canonical one (B for the still transforms,
  C for the clip export).
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.media import PACK_VERSION, PACKAGE_ID
from nexus_ai_agent.creative.packs.media.operations import (
    CAPABILITY_GRAYSCALE,
    CAPABILITY_IMAGE_TO_VIDEO,
    CAPABILITY_RESIZE,
    OPERATION_GRAYSCALE,
    OPERATION_IMAGE_TO_VIDEO,
    OPERATION_RESIZE,
    GrayscaleImageInput,
    ImageToVideoInput,
    ResizeImageInput,
    build_media_registry,
    register_media_operations,
)
from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    CommandValidationError,
    PermissionLevel,
    Project,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _project_with(asset_id: str, digest: str) -> Project:
    from nexus_ai_agent.creative.studio.models import Playhead, Timeline, new_project

    project = new_project(
        project_id="proj_test",
        name="t",
        timeline=Timeline(timeline_id="tl_test", duration_us=0, playhead=Playhead(timecode_us=0)),
    )
    record = AssetRecord(
        asset_id=asset_id,
        media_kind="image",
        content_sha256=digest,
        duration_us=0,
        parent_asset_ids=(),
        provenance={},
    )
    return project.model_copy(update={"assets": [record]})


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def test_the_pack_registers_three_operations() -> None:
    registry = build_media_registry()
    for operation in (OPERATION_RESIZE, OPERATION_GRAYSCALE, OPERATION_IMAGE_TO_VIDEO):
        assert operation in registry.list_operations()


def test_the_pack_is_registered_in_the_live_runtime_registry() -> None:
    registry = build_runtime_registry()
    assert OPERATION_RESIZE in registry.list_operations()
    assert OPERATION_GRAYSCALE in registry.list_operations()
    assert OPERATION_IMAGE_TO_VIDEO in registry.list_operations()


def test_the_capability_version_is_the_pack_version() -> None:
    registry = build_runtime_registry()
    descriptor = registry.describe(OPERATION_RESIZE)
    assert descriptor.version == PACK_VERSION
    assert descriptor.capability_id == "media.resize_image"
    assert descriptor.required_packs == (PACKAGE_ID,)


def test_the_permission_ladder_is_the_canonical_one() -> None:
    registry = build_runtime_registry()
    assert registry.get_spec(OPERATION_RESIZE).permission_level is PermissionLevel.REVERSIBLE
    assert registry.get_spec(OPERATION_GRAYSCALE).permission_level is PermissionLevel.REVERSIBLE
    assert (
        registry.get_spec(OPERATION_IMAGE_TO_VIDEO).permission_level is PermissionLevel.CONFIRMATION
    )


def test_the_pack_needs_no_level_d_confirmation_for_the_still_transforms() -> None:
    registry = build_runtime_registry()
    decision = registry.check_permission(OPERATION_RESIZE, confirmed=False)
    assert decision.allowed is True
    decision = registry.check_permission(OPERATION_IMAGE_TO_VIDEO, confirmed=False)
    assert decision.allowed is False
    decision = registry.check_permission(OPERATION_IMAGE_TO_VIDEO, confirmed=True)
    assert decision.allowed is True


def test_a_duplicate_media_port_name_is_refused_at_registration() -> None:
    from nexus_ai_agent.creative.studio.capabilities import OperationSpec
    from nexus_ai_agent.creative.studio.models import MediaContract, MediaPort

    registry = CapabilityRegistry()
    spec = OperationSpec(
        operation_id="media.test",
        description="x",
        permission_level=PermissionLevel.IMMEDIATE,
        input_model=ResizeImageInput,
        handler=lambda project, context: None,  # type: ignore[arg-type,return-value]
        media_inputs=(
            MediaPort(name="source", contract=MediaContract(kind="image")),
            MediaPort(name="source", contract=MediaContract(kind="image")),
        ),
    )
    with pytest.raises(ValueError):
        registry.register_operation("media", "test", spec)


# ---------------------------------------------------------------------------
# the typed input schemas
# ---------------------------------------------------------------------------


def test_the_resize_input_refuses_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ResizeImageInput(
            source_asset_id="a",
            source_sha256=_sha256("x"),
            output_path="/tmp/a.png",
            output_sha256=_sha256("y"),
            width=10,
            height=10,
            nonsense=1,
        )


def test_the_resize_input_refuses_an_impossible_geometry() -> None:
    with pytest.raises(ValidationError):
        ResizeImageInput(
            source_asset_id="a",
            source_sha256=_sha256("x"),
            output_path="/tmp/a.png",
            output_sha256=_sha256("y"),
            width=0,
            height=10,
        )


def test_the_video_input_refuses_a_zero_duration() -> None:
    with pytest.raises(ValidationError):
        ImageToVideoInput(
            source_asset_id="a",
            source_sha256=_sha256("x"),
            output_path="/tmp/a.mp4",
            output_sha256=_sha256("y"),
            width=64,
            height=64,
            duration_us=0,
            fps=25,
            encoder="libx264",
        )


def test_the_grayscale_input_carries_the_measured_luma() -> None:
    with pytest.raises(ValidationError):
        GrayscaleImageInput(
            source_asset_id="a",
            source_sha256=_sha256("x"),
            output_path="/tmp/a.png",
            output_sha256=_sha256("y"),
            width=10,
            height=10,
            mean_luma=2.0,
        )


# ---------------------------------------------------------------------------
# the handlers (pure state recorders)
# ---------------------------------------------------------------------------


def _resize_payload(digest: str, **overrides) -> dict:
    payload = {
        "source_asset_id": "asset_1",
        "source_sha256": digest,
        "output_path": "/tmp/out.png",
        "output_sha256": _sha256("out"),
        "width": 1280,
        "height": 720,
        "source_width": 320,
        "source_height": 240,
    }
    payload.update(overrides)
    return payload


def _dispatch(
    registry: CapabilityRegistry,
    operation: str,
    payload: dict,
    *,
    registered_digest: str | None = None,
):
    from nexus_ai_agent.creative.studio.capabilities import OperationContext
    from nexus_ai_agent.creative.studio.models import (
        ActorIdentity,
        TargetRef,
        TypedCommand,
    )

    spec = registry.get_spec(operation)
    command = TypedCommand(
        protocol_version="nagar.command.v1",
        command_id="cmd_test",
        actor=ActorIdentity(kind="service", actor_id="test"),
        session_id="s",
        operation=operation,
        target=TargetRef(project_id="proj_test", track_id="main"),
        input=payload,
    )
    context = OperationContext(command=command, input_data=payload, history=())
    digest = registered_digest or payload["source_sha256"]
    return spec.handler(_project_with("asset_1", digest), context)


def test_the_resize_handler_records_a_derived_asset() -> None:
    registry = build_media_registry()
    digest = _sha256("src")
    outcome = _dispatch(registry, OPERATION_RESIZE, _resize_payload(digest))
    assert outcome.project.assets[-1].asset_id != "asset_1"
    assert outcome.project.assets[-1].media_kind == "image"
    assert outcome.project.assets[-1].parent_asset_ids == ("asset_1",)
    assert outcome.output["width"] == 1280
    assert outcome.output["is_derived"] is True


def test_the_handler_refuses_a_source_whose_hash_changed() -> None:
    """The registered asset carries the *old* digest; the payload claims a new one."""
    registry = build_media_registry()
    with pytest.raises(CommandValidationError):
        _dispatch(
            registry,
            OPERATION_RESIZE,
            _resize_payload(_sha256("new")),
            registered_digest=_sha256("old"),
        )


def test_the_handler_refuses_an_unregistered_source() -> None:
    registry = build_media_registry()
    with pytest.raises(CommandValidationError):
        _dispatch(
            registry,
            OPERATION_RESIZE,
            _resize_payload(_sha256("src"), source_asset_id="not_registered"),
        )


def test_the_handler_refuses_a_source_of_the_wrong_kind() -> None:
    registry = build_media_registry()
    from nexus_ai_agent.creative.studio.capabilities import OperationContext
    from nexus_ai_agent.creative.studio.models import (
        ActorIdentity,
        Playhead,
        Timeline,
        new_project,
    )

    project = new_project(
        project_id="p",
        name="t",
        timeline=Timeline(timeline_id="tl_test", duration_us=0, playhead=Playhead(timecode_us=0)),
    )
    project = project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="asset_1",
                    media_kind="video",
                    content_sha256=_sha256("src"),
                    duration_us=1,
                    parent_asset_ids=(),
                    provenance={},
                )
            ]
        }
    )
    spec = registry.get_spec(OPERATION_RESIZE)
    from nexus_ai_agent.creative.studio.models import TargetRef, TypedCommand

    command = TypedCommand(
        protocol_version="nagar.command.v1",
        command_id="cmd_test",
        actor=ActorIdentity(kind="service", actor_id="test"),
        session_id="s",
        operation=OPERATION_RESIZE,
        target=TargetRef(project_id="p", track_id="main"),
        input=_resize_payload(_sha256("src")),
    )
    context = OperationContext(
        command=command, input_data=_resize_payload(_sha256("src")), history=()
    )
    with pytest.raises(CommandValidationError):
        spec.handler(project, context)


def test_the_grayscale_handler_records_the_measured_luma() -> None:
    registry = build_media_registry()
    digest = _sha256("src")
    payload = {
        "source_asset_id": "asset_1",
        "source_sha256": digest,
        "output_path": "/tmp/out.png",
        "output_sha256": _sha256("out"),
        "width": 1280,
        "height": 720,
        "mean_luma": 0.42,
    }
    outcome = _dispatch(registry, OPERATION_GRAYSCALE, payload)
    assert outcome.output["mean_luma"] == 0.42
    assert outcome.project.assets[-1].provenance["color_space"] == "gray"


def test_the_video_handler_records_the_clip() -> None:
    registry = build_media_registry()
    digest = _sha256("src")
    payload = {
        "source_asset_id": "asset_1",
        "source_sha256": digest,
        "output_path": "/tmp/out.mp4",
        "output_sha256": _sha256("out"),
        "width": 1280,
        "height": 720,
        "duration_us": 2_000_000,
        "fps": 25,
        "encoder": "libx264",
    }
    outcome = _dispatch(registry, OPERATION_IMAGE_TO_VIDEO, payload)
    assert outcome.project.assets[-1].media_kind == "video"
    assert outcome.project.assets[-1].duration_us == 2_000_000
    assert outcome.output["fps"] == 25


def test_the_handler_never_mutates_the_source_asset() -> None:
    registry = build_media_registry()
    digest = _sha256("src")
    outcome = _dispatch(registry, OPERATION_RESIZE, _resize_payload(digest))
    assert len(outcome.project.assets) == 2
    assert outcome.project.assets[0].asset_id == "asset_1"
    assert outcome.project.assets[0].parent_asset_ids == ()


def test_registering_the_pack_twice_is_refused() -> None:
    registry = build_media_registry()
    with pytest.raises(ValueError):
        register_media_operations(registry)


def test_the_capability_names_are_stable() -> None:
    assert CAPABILITY_RESIZE == "resize_image"
    assert CAPABILITY_GRAYSCALE == "grayscale_image"
    assert CAPABILITY_IMAGE_TO_VIDEO == "image_to_video"
    assert PACKAGE_ID == "nexus.media.transform"
