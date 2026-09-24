"""Wave 3 local upscale: pure catalog semantics and a genuine FFmpeg run."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from nagar_helpers import TEST_ACTOR, TEST_PROVENANCE, authorized_bus
from PIL import Image

from nexus_ai_agent.creative.packs.slideshow.models import AssetEvidence
from nexus_ai_agent.creative.packs.slideshow.operations import (
    OPERATION_SCAN,
    OPERATION_UPSCALE,
    build_slideshow_registry,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import (
    RenderError,
    build_upscale_command,
    resolve_ffmpeg_bin,
)
from nexus_ai_agent.creative.slideshow.upscale import upscale_from_file
from nexus_ai_agent.creative.studio.models import PermissionLevel, Playhead, Timeline, new_project


def _command(operation: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol_version": "nagar.command.v1",
        "schema_version": 2,
        "command_id": f"cmd_{uuid4().hex}",
        "actor": TEST_ACTOR.model_dump(mode="json"),
        "target": {"project_id": "upscale-project"},
        "provenance": TEST_PROVENANCE.model_dump(mode="json"),
        "session_id": "upscale-test",
        "operation": operation,
        "input": payload,
        "idempotency_key": f"{operation}:{uuid4().hex}",
    }


def test_upscale_command_is_allow_listed_lanczos_argv() -> None:
    command = build_upscale_command(
        Path("input.jpg"),
        width=16,
        height=12,
        output_path=Path(".output.part.png"),
        binary="/usr/bin/ffmpeg",
    )
    assert command[0] == "/usr/bin/ffmpeg"
    assert "scale=16:12:flags=lanczos" in command
    assert command[-1] == ".output.part.png"
    assert "-c:v" in command and "png" in command


def test_real_ffmpeg_upscale_writes_a_measured_lossless_png(tmp_path: Path) -> None:
    source = tmp_path / "tiny.png"
    output = tmp_path / "upscaled.png"
    Image.new("RGB", (8, 6), (30, 120, 210)).save(source)

    result = upscale_from_file(
        source,
        output,
        scale_factor=2.0,
        ffmpeg_bin=resolve_ffmpeg_bin(),
    )

    assert output.is_file()
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.size == (16, 12)
    assert result.artifact.width == 16
    assert result.artifact.height == 12
    assert result.artifact.sha256.startswith("sha256:")
    assert result.asset_id.startswith("derived_")
    assert result.state_revision == 2  # scan + level-B derived-image record


def test_upscale_supports_an_exact_target_resolution(tmp_path: Path) -> None:
    source = tmp_path / "tiny.png"
    output = tmp_path / "exact.png"
    Image.new("RGB", (8, 6), (200, 50, 70)).save(source)
    result = upscale_from_file(source, output, target_resolution="20x10")
    assert (result.artifact.width, result.artifact.height) == (20, 10)
    with Image.open(output) as image:
        assert image.size == (20, 10)


def test_upscale_refuses_overwrite_and_unbounded_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "tiny.png"
    output = tmp_path / "existing.png"
    Image.new("RGB", (8, 6), "navy").save(source)
    output.write_bytes(b"keep")
    with pytest.raises(RenderError, match="already exists"):
        upscale_from_file(source, output, scale_factor=2.0)
    assert output.read_bytes() == b"keep"
    with pytest.raises(RenderError, match="outside|budget"):
        upscale_from_file(source, tmp_path / "huge.png", target_resolution="9000x9000")


def test_catalog_operation_is_level_b_and_undo_restores_state() -> None:
    registry = build_slideshow_registry()
    spec = registry.get_spec(OPERATION_UPSCALE)
    assert spec.permission_level is PermissionLevel.REVERSIBLE
    project = new_project(
        project_id="upscale-project",
        name="upscale",
        timeline=Timeline(timeline_id="timeline", duration_us=0, playhead=Playhead(timecode_us=0)),
    )
    bus = authorized_bus(project, registry=registry)
    source = AssetEvidence(
        evidence_id="source-image",
        path="/tmp/source.png",
        content_sha256="sha256:" + "11" * 32,
        media_kind="image",
        width=8,
        height=6,
    )
    bus.dispatch(_command(OPERATION_SCAN, {"assets": [source.model_dump(mode="json")]}))
    before = bus.project.state_hash
    result = bus.dispatch(
        _command(
            OPERATION_UPSCALE,
            {
                "source_asset_id": source.evidence_id,
                "source_sha256": source.content_sha256,
                "output_path": "/tmp/upscaled.png",
                "output_sha256": "sha256:" + "22" * 32,
                "source_width": 8,
                "source_height": 6,
                "width": 16,
                "height": 12,
                "scale_factor": 2.0,
            },
        )
    )
    assert result.undo_available is True
    assert len(bus.project.assets) == 2
    assert bus.project.assets[-1].parent_asset_ids == (source.evidence_id,)
    bus.dispatch(_command("system.undo", {}))
    assert bus.project.state_hash == before
    assert len(bus.project.assets) == 1
