"""Wave 2.5 — the queue-side adapter between the durable job queue and the render lane.

This module is the only sanctioned path by which the ``slideshow_render`` job
type reaches the Wave 2c engine.  It deliberately knows nothing about
Telegram: payloads arrive as plain dicts from ``InProcessJobQueue``, and every
failure is reported as a typed ``error_code`` so the bot's completion hook can
map it to a user-facing message (decision-log Wave 2.5 + revision r7).

The render itself is the same pure ``render_from_files`` the CLI calls — one
planning pass, one FFmpeg process, measured evidence back through the bus.  It
runs inside ``asyncio.to_thread`` so the bot's event loop stays responsive,
and the handler never spawns a second render path.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.creative.packs.slideshow.models import TARGET_DURATIONS_US
from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, RenderError
from nexus_ai_agent.creative.slideshow.probe import IMAGE_SUFFIXES, ProbeError
from nexus_ai_agent.creative.slideshow.service import PlanningRequest, render_from_files

#: The job type registered in ``nexus_ai_agent.worker.default_job_handlers``.
SLIDESHOW_JOB_TYPE = "slideshow_render"

#: Hard envelope for the queue path (Wave 2.5 item 2, r7 item 6): the bot
#: collects at most this many images, and no payload may plan above 30 s.
MAX_IMAGES = 5
MAX_TARGET_DURATION_US = 30_000_000

#: The surface renders at half a minute — the pack target this flow uses.
BOT_TARGET_DURATION_US = MAX_TARGET_DURATION_US

#: Bot-side default; the pack validates the same grammar in ``ComposeInput``.
DEFAULT_RESOLUTION = "1280x720"

#: Workspace directories are created by the bot under ``settings.creative_temp_dir``
#: with this prefix; the prune sweep knows the pattern.
WORKSPACE_PREFIX = "slideshow_"

#: Orphaned workspaces (crash between render and notify) expire on the next job.
STALE_WORKSPACE_MAX_AGE_S = 24 * 3600.0

#: Closed vocabulary of user-mappable failures (r7 item 7).
ERROR_CODES = frozenset(
    {"ffmpeg_unavailable", "render_failed", "unusable_image", "invalid_request", "internal"}
)


class SlideshowJobError(ValueError):
    """A payload problem the queue should record as a typed invalid request."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


class SlideshowRenderPayload(BaseModel):
    """The validated job envelope.

    The bot handler enforces the limits as UX; they are re-asserted here on
    purpose (r7 item 6): the queue is a trust boundary — a hand-crafted or
    CLI-drained payload must not exceed what the product allows.  All paths
    live inside one freshly created workspace directory, which makes cleanup
    unconditionally safe and rejects ``../``-style payloads outright.
    """

    model_config = ConfigDict(extra="ignore")

    image_paths: tuple[str, ...] = Field(min_length=1, max_length=MAX_IMAGES)
    output_path: str = Field(min_length=5)
    workspace_dir: str = Field(min_length=1)
    target_duration_us: int
    resolution: str = Field(pattern=r"^\d{2,5}x\d{2,5}$", default=DEFAULT_RESOLUTION)
    project_name: str = Field(
        pattern=r"[\w][\w .\-:]{0,59}",
        default="telegram-slideshow",
    )

    @field_validator("target_duration_us")
    @classmethod
    def _duration_is_an_approved_short_target(cls, value: int) -> int:
        if value not in TARGET_DURATIONS_US:
            raise ValueError("target duration is not an approved pack target")
        if value > MAX_TARGET_DURATION_US:
            raise ValueError(f"the queue path is capped at {MAX_TARGET_DURATION_US}us")
        return value


@dataclass(frozen=True)
class _JobSpec:
    """Typed view of one validated render job."""

    images: tuple[Path, ...]
    output_path: Path
    workspace_dir: Path
    duration_us: int
    resolution: str
    project_name: str


def _inside(workspace: Path, raw: str, label: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise SlideshowJobError("invalid_request", f"{label} must be an absolute path")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace):
        raise SlideshowJobError("invalid_request", f"{label} must stay inside the job workspace")
    return resolved


def _parse_payload(payload: Mapping[str, Any]) -> _JobSpec:
    try:
        model = SlideshowRenderPayload.model_validate(dict(payload))
    except ValidationError as exc:
        raise SlideshowJobError(
            "invalid_request", f"payload rejected ({exc.error_count()} errors)"
        ) from exc
    workspace = Path(model.workspace_dir).resolve()
    images = tuple(_inside(workspace, item, "image path") for item in model.image_paths)
    for image in images:
        if image.suffix.lower() not in IMAGE_SUFFIXES:
            raise SlideshowJobError("unusable_image", "unsupported image type")
    output = _inside(workspace, model.output_path, "output path")
    return _JobSpec(
        images=images,
        output_path=output,
        workspace_dir=workspace,
        duration_us=model.target_duration_us,
        resolution=model.resolution,
        project_name=model.project_name,
    )


def _code_for(exc: Exception) -> str:
    if isinstance(exc, FfmpegUnavailableError):
        return "ffmpeg_unavailable"
    if isinstance(exc, ProbeError):
        return "unusable_image"
    if isinstance(exc, RenderError):
        return "render_failed"
    if isinstance(exc, (ValidationError, ValueError)):
        return "invalid_request"
    if isinstance(exc, OSError):
        return "internal"
    return "internal"


def _render_sync(spec: _JobSpec) -> dict[str, Any]:
    """Plan + encode in this thread; the engine does one FFmpeg process."""
    settings = get_settings()
    spec.output_path.parent.mkdir(parents=True, exist_ok=True)
    request = PlanningRequest(
        images=spec.images,
        target_duration_us=spec.duration_us,
        mode="auto",
        # Free lane by default: both switches come from the recorded fail-closed
        # settings, so no bot request can reach a billable or egressing path (r7 item 6).
        provider=settings.slideshow_analysis_provider,
        allow_image_upload=settings.slideshow_allow_image_upload,
        resolution=spec.resolution,
        project_name=spec.project_name,
    )
    outcome = render_from_files(
        request,
        output_path=spec.output_path,
        ffmpeg_bin=settings.ffmpeg_bin,
        timeout=settings.slideshow_render_timeout_seconds,
    )
    artifact = outcome.artifact
    return {
        "success": True,
        "output_path": str(artifact["output_path"]),
        "duration_us": artifact["duration_us"],
        "width": artifact["width"],
        "height": artifact["height"],
        "size_bytes": artifact["size_bytes"],
        "content_sha256": artifact["output_sha256"],
        "template_id": outcome.template_id,
        "shot_count": len(outcome.plan["shots"]),
    }


def _prune_stale_workspaces(base: Path) -> None:
    """Best-effort sweep for workspaces a crashed run orphaned (r7 item 4)."""
    try:
        if not base.is_dir():
            return
        cutoff = time.time() - STALE_WORKSPACE_MAX_AGE_S
        for child in base.glob(f"{WORKSPACE_PREFIX}*"):
            if not child.is_dir():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        return


async def slideshow_render_job(payload: dict[str, object]) -> dict[str, object]:
    """Queue handler for ``slideshow_render``.

    Expected failures never raise to the queue: they come back as
    ``{"success": false, "error_code": ...}`` so the D4 completion hook can
    map a code to a plain user message (the queue's generic failure text is
    reserved for genuine bugs).  Cleanup ownership per r7 item 4: the input
    images and any failed/partial output are deleted in ``finally``; a
    successful master is left for the notifier, which deletes it after
    ``send_document``.
    """
    try:
        spec = _parse_payload(payload)
    except SlideshowJobError as exc:
        return {"success": False, "error_code": exc.code}

    succeeded = False
    try:
        result = await asyncio.to_thread(_render_sync, spec)
        succeeded = True
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the queue boundary maps to typed codes
        return {"success": False, "error_code": _code_for(exc)}
    finally:
        for image in spec.images:
            image.unlink(missing_ok=True)
        if not succeeded:
            spec.output_path.unlink(missing_ok=True)
            shutil.rmtree(spec.workspace_dir, ignore_errors=True)
        _prune_stale_workspaces(spec.workspace_dir.parent)


__all__ = [
    "BOT_TARGET_DURATION_US",
    "DEFAULT_RESOLUTION",
    "ERROR_CODES",
    "MAX_IMAGES",
    "MAX_TARGET_DURATION_US",
    "SLIDESHOW_JOB_TYPE",
    "WORKSPACE_PREFIX",
    "SlideshowJobError",
    "SlideshowRenderPayload",
    "slideshow_render_job",
]
