"""Optional local still-image upscale above the pure slideshow command bus."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from nexus_ai_agent.creative.packs.slideshow.operations import (
    OPERATION_SCAN,
    OPERATION_UPSCALE,
    build_slideshow_registry,
)
from nexus_ai_agent.creative.slideshow.ffmpeg import UpscaleArtifact, upscale_image
from nexus_ai_agent.creative.slideshow.probe import probe_image
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    ActorIdentity,
    InputRef,
    InputRefMetadata,
    Playhead,
    Timeline,
    new_project,
)

_LOCAL_ACTOR = ActorIdentity(kind="service", actor_id="nagar.slideshow-upscale")


@dataclass(frozen=True)
class UpscaleOutcome:
    """Measured file evidence plus the audited state transition."""

    artifact: UpscaleArtifact
    asset_id: str
    source_asset_id: str
    state_revision: int
    state_hash: str


def _command(
    bus: CommandBus,
    operation: str,
    payload: dict[str, object],
    *,
    input_refs: tuple[InputRef, ...] = (),
) -> dict[str, object]:
    return {
        "protocol_version": "nagar.command.v1",
        "schema_version": 2,
        "command_id": f"cmd_{uuid4().hex[:16]}",
        "actor": _LOCAL_ACTOR.model_dump(mode="json"),
        "target": {"project_id": bus.project.project_id},
        "provenance": {"source": "service", "source_id": "slideshow-upscale"},
        "session_id": "slideshow-upscale",
        "operation": operation,
        "input": payload,
        "input_refs": [ref.model_dump(mode="json") for ref in input_refs],
        "idempotency_key": f"{operation}:{uuid4().hex[:16]}",
    }


def _target_dimensions(
    source_width: int,
    source_height: int,
    *,
    scale_factor: float | None,
    target_resolution: str | None,
) -> tuple[int, int]:
    if (scale_factor is None) == (target_resolution is None):
        raise ValueError("provide exactly one of scale_factor or target_resolution")
    if scale_factor is not None:
        if not 1.0 < scale_factor <= 4.0:
            raise ValueError("scale_factor must be greater than 1 and at most 4")
        return round(source_width * scale_factor), round(source_height * scale_factor)
    raw_width, separator, raw_height = (target_resolution or "").partition("x")
    if not separator or not raw_width.isdigit() or not raw_height.isdigit():
        raise ValueError("target_resolution must use WIDTHxHEIGHT")
    return int(raw_width), int(raw_height)


def upscale_from_file(
    input_path: Path,
    output_path: Path,
    *,
    scale_factor: float | None = None,
    target_resolution: str | None = None,
    ffmpeg_bin: str | None = None,
    timeout: int = 120,
    overwrite: bool = False,
) -> UpscaleOutcome:
    """Probe, genuinely upscale, measure, then record one level-B operation."""
    source = probe_image(input_path)
    assert source.width is not None and source.height is not None
    width, height = _target_dimensions(
        source.width,
        source.height,
        scale_factor=scale_factor,
        target_resolution=target_resolution,
    )
    project = new_project(
        project_id=f"proj_{uuid4().hex[:12]}",
        name="local-upscale",
        timeline=Timeline(
            timeline_id=f"tl_{uuid4().hex[:12]}",
            duration_us=0,
            playhead=Playhead(timecode_us=0),
        ),
    )
    bus = CommandBus(
        project,
        registry=build_slideshow_registry(),
        authorizer=ProjectAccess(
            actor=_LOCAL_ACTOR,
            project_id=project.project_id,
            permissions=frozenset({"project:read", "project:write"}),
        ),
    )
    bus.dispatch(_command(bus, OPERATION_SCAN, {"assets": [source.model_dump(mode="json")]}))
    artifact = upscale_image(
        input_path,
        output_path,
        width=width,
        height=height,
        binary=ffmpeg_bin,
        timeout=timeout,
        overwrite=overwrite,
    )
    try:
        measured = probe_image(output_path)
        result = bus.dispatch(
            _command(
                bus,
                OPERATION_UPSCALE,
                {
                    "source_asset_id": source.evidence_id,
                    "source_sha256": source.content_sha256,
                    "output_path": artifact.path,
                    "output_sha256": measured.content_sha256,
                    "source_width": source.width,
                    "source_height": source.height,
                    "width": measured.width,
                    "height": measured.height,
                    "scale_factor": scale_factor,
                    "target_resolution": target_resolution,
                    "filter_flags": "lanczos",
                },
                input_refs=(
                    InputRef(
                        ref_type="asset",
                        project_id=project.project_id,
                        ref_id=source.evidence_id,
                        metadata=InputRefMetadata(
                            media_kind="image", content_sha256=source.content_sha256
                        ),
                    ),
                ),
            )
        )
    except Exception:
        Path(artifact.path).unlink(missing_ok=True)
        raise
    return UpscaleOutcome(
        artifact=artifact,
        asset_id=str(result.output["asset_id"]),
        source_asset_id=source.evidence_id,
        state_revision=bus.project.state_revision,
        state_hash=bus.project.state_hash,
    )


__all__ = ["UpscaleOutcome", "upscale_from_file"]
