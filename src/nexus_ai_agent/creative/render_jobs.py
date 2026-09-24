"""Worker-side adapter for ``creative_render`` jobs — the canonical chain.

Job lifecycle (task-166, CREATIVE_STUDIO plan §4–§5):

    validated payload
      → workspace containment check (payloads are a queue trust boundary)
      → packs runtime registry (CapabilityRegistry, composed packs)
      → CommandBus.dispatch (typed pack operation, idempotency-keyed)
      → render lane (FFmpeg via the codebase's binary allow-list)
      → measured artifact (probe + sha256) reported back to the queue

Translation and user-facing copy live in the bot completion notifier; this
module returns typed results instead:

* ``{"success": True, ...}`` — measured artifact facts (sha256-only truth),
  including the ``spec_ir_hash`` lane identity; since task-178 the queue
  independently re-verifies every claim before the job may succeed;
* ``{"success": False, "error_code": <typed>}`` — expected, user-typed
  failure (bad args, unavailable caption engine, ...) — the job COMPLETES,
  the notifier translates the code;
* **raises** — unexpected environment failure → job FAILED, operator-visible.

Destination safety (task-178): a render never deletes first. The lane
publishes by staging (``.part``) + atomic rename; ``overwrite=True`` is
scoped to THIS job's own key-derived workspace path, so a retry can replace
only its own previous attempt — never an artifact owned by anyone else —
and a failed re-render leaves the previous bytes in place instead of
deleting them. Document artifacts (``.srt`` / ``.otio``) are materialised
with the same atomic temp-then-rename pattern; a half-written destination
is never visible under the final name.

Nothing here imports Telegram; the worker runs outside the bot process.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

CREATIVE_RENDER_JOB_TYPE = "creative_render"
WORKSPACE_PREFIX = "creative_"
SOURCE_ASSET_ID = "src"

#: Surface (command, operation) → canonical packs operation id (closed set —
#: this map *is* the surface's promise). ``lut``/``burnin`` are deliberately
#: absent: neither has an honest execution path today (CREATIVE_STUDIO §7 —
#: no shipped .cube assets / no lane instrument) and both are refused at the
#: mapper; anything hand-queued dies here as ``unsupported_operation``.
SURFACE_TO_CANONICAL: dict[tuple[str, str], str] = {
    ("edit", "trim"): "timeline.trim",
    ("edit", "speed"): "timeline.speed_ramp",
    ("edit", "reverse"): "timeline.reverse_segment",
    ("caption", "transcribe"): "caption.transcribe",
    ("grade", "exposure"): "color.adjust_exposure",
    ("grade", "proxy"): "delivery.make_proxy_480p",
    ("grade", "otio"): "delivery.export_otio",
}

#: Server-controlled EXPERIMENTAL-pack opt-in (task-183 trust boundary).
#: The canonical operations below run on the ``EXPERIMENTAL``
#: ``nexus.color.delivery`` pack, and the bus lifecycle gate (Gate-2 stage 4b)
#: refuses them unless the composition root opts in. That opt-in is decided
#: *here*, by the worker, from the canonical operation id -- never from the
#: queue row. A payload is an untrusted structure (see the containment
#: section below), so a row cannot carry, widen, or narrow its own lifecycle:
#: ``CreativeRenderPayload`` has no opt-in field and ``extra="forbid"`` rejects
#: one. Adding an operation here is the explicit, reviewed operator act;
#: ``tests/unit/test_creative_render_jobs.py`` pins that the set is exactly the
#: surface operations whose packs are EXPERIMENTAL (no over-grant, no gap).
EXPERIMENTAL_OPT_IN_OPERATIONS: frozenset[str] = frozenset(
    {
        "color.adjust_exposure",
        "delivery.make_proxy_480p",
        "delivery.export_otio",
    }
)

#: Typed failure codes surfaced to users via ``creative.failed.<code>``.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "unsupported_operation",
        "media_missing",
        "caption_profile_unavailable",
        "render_failed",
        "ffmpeg_unavailable",
    }
)


class CreativeRenderError(ValueError):
    """Expected, typed failure. The queue completes the job with
    ``success=False`` so the notifier can translate the code."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown creative render error code: {code}")
        super().__init__(f"[{code}] {detail}".strip())
        self.code = code
        self.detail = detail


class CreativeRenderPayload(BaseModel):
    """Trusty schema for the surface→queue→worker contract."""

    model_config = ConfigDict(extra="forbid")

    command: Literal["edit", "caption", "grade"]
    operation: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    workspace_dir: str = Field(min_length=1)
    input_path: str | None = None
    media_duration_us: int | None = Field(default=None, ge=0)
    user_id: int
    chat_id: int
    lang: str = "en"
    idempotency_key: str = Field(min_length=1)
    # Deliberately no lifecycle opt-in field: the EXPERIMENTAL-pack opt-in is
    # server policy (``EXPERIMENTAL_OPT_IN_OPERATIONS``), and ``extra="forbid"``
    # rejects a row that tries to carry one (task-183 trust boundary).


# ---------------------------------------------------------------------------
# Trust-boundary containment (payloads are queue rows: untrusted structures)
# ---------------------------------------------------------------------------


def _guarded_workspace(raw: str) -> Path:
    try:
        workspace = Path(raw).resolve(strict=False)
        root = Path(get_settings().creative_temp_dir).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise CreativeRenderError("invalid_request", f"workspace resolution failed: {exc}") from exc
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise CreativeRenderError("invalid_request", "workspace outside creative temp dir") from exc
    if not workspace.name.startswith(WORKSPACE_PREFIX):
        raise CreativeRenderError("invalid_request", "workspace name missing reserved prefix")
    return workspace


def _guarded_input(payload: CreativeRenderPayload, workspace: Path) -> Path:
    if payload.input_path is None:
        raise CreativeRenderError("media_missing", "operation requires staged media")
    try:
        candidate = Path(payload.input_path).resolve(strict=False)
    except OSError as exc:
        raise CreativeRenderError("invalid_request", f"bad input path: {exc}") from exc
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise CreativeRenderError("invalid_request", "input path escapes its workspace") from exc
    if not candidate.is_file():
        raise CreativeRenderError("media_missing", str(candidate))
    return candidate


# ---------------------------------------------------------------------------
# Canonical execution helpers
# ---------------------------------------------------------------------------


def _build_project(*, payload: CreativeRenderPayload, duration_us: int, sha256: str) -> Any:  # noqa: ANN401
    """One asset ("src") + an empty main timeline — the minimal central state
    a pack operation can lawfully mutate."""
    from nexus_ai_agent.creative.studio.models import (
        AssetRecord,
        Playhead,
        TimeBase,
        Timeline,
        new_project,
    )

    project = new_project(
        project_id=f"shot-{payload.idempotency_key}",
        name=f"/{payload.command} {payload.operation}",
        timeline=Timeline(
            timeline_id="main",
            duration_us=duration_us,
            tracks=[],
            playhead=Playhead(timecode_us=0, timebase=TimeBase(numerator=30, denominator=1)),
        ),
    )
    src = AssetRecord(
        asset_id=SOURCE_ASSET_ID,
        media_kind="video",
        content_sha256=sha256,
        duration_us=duration_us,
        parent_asset_ids=(),
        provenance={
            "origin": "telegram.one_shot",
            "idempotency_key": payload.idempotency_key,
            "command": payload.command,
            "operation": payload.operation,
        },
    )
    return project.model_copy(update={"assets": [src]})


def _dispatch(
    project: Any,  # noqa: ANN401
    *,
    operation: str,
    input_data: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    """Registry lookup → bus dispatch. Bad args become typed
    ``invalid_request`` (a retry with the same payload fails identically).

    The bus's EXPERIMENTAL opt-in is derived from the canonical operation via
    the server-controlled ``EXPERIMENTAL_OPT_IN_OPERATIONS``; no caller (and no
    queue row) can pass it in."""
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
    from nexus_ai_agent.creative.studio.bus import CommandBus
    from nexus_ai_agent.creative.studio.models import TargetRef, TypedCommand

    bus = CommandBus(
        state=project,
        registry=build_runtime_registry(),
        allow_experimental=operation in EXPERIMENTAL_OPT_IN_OPERATIONS,
    )
    command = TypedCommand(
        command_id=f"cmd-{idempotency_key}-{operation}",
        operation=operation,
        input=input_data,
        target=TargetRef(project_id=project.project_id, track_id="main"),
        idempotency_key=idempotency_key,
    )
    try:
        result = bus.dispatch(command)
    except Exception as exc:
        raise CreativeRenderError("invalid_request", f"{type(exc).__name__}: {exc}") from exc
    return dict(result.output or {})


def _seconds_args(payload: CreativeRenderPayload, index: int, default: float) -> float:
    raw = payload.args[index] if len(payload.args) > index else str(default)
    try:
        return float(raw)
    except ValueError as exc:
        raise CreativeRenderError(
            "invalid_request", f"argument {index + 1} must be a number, got {raw!r}"
        ) from exc


def _operation_inputs(payload: CreativeRenderPayload, duration_us: int) -> dict[str, Any]:
    """Pack-level input record (validated by the bus, journaled in history)."""
    if payload.operation == "trim":
        in_us = int(_seconds_args(payload, 0, 0.0) * 1_000_000)
        out_us = int(_seconds_args(payload, 1, duration_us / 1_000_000) * 1_000_000)
        if in_us >= out_us:
            raise CreativeRenderError("invalid_request", "trim needs in < out (seconds)")
        return {
            "clip_asset_id": SOURCE_ASSET_ID,
            "in_point_us": in_us,
            "out_point_us": out_us,
        }
    if payload.operation == "speed":
        return {"clip_asset_id": SOURCE_ASSET_ID, "speed_factor": _seconds_args(payload, 0, 1.0)}
    if payload.operation == "reverse":
        return {"clip_asset_id": SOURCE_ASSET_ID}
    if payload.operation == "exposure":
        return {"clip_asset_id": SOURCE_ASSET_ID, "exposure_ev": _seconds_args(payload, 0, 0.0)}
    if payload.operation == "proxy":
        return {"video_asset_id": SOURCE_ASSET_ID, "output_asset_id": "proxy-480p"}
    if payload.operation == "otio":
        return {"frame_rate": 24.0}
    raise CreativeRenderError("unsupported_operation", payload.operation)


def _lane_ops(
    payload: CreativeRenderPayload, canonical_id: str, duration_us: int
) -> tuple[list[Any], Any | None]:  # noqa: ANN401
    """Lane-IR instrumentation matching the canonical operation one-to-one.
    Trim is clamped to the measured duration — the media is the truth."""
    from nexus_ai_agent.creative.rendering.ir import (
        ExposureOp,
        LaneProfile,
        ReverseOp,
        SpeedOp,
        TrimOp,
    )

    if canonical_id == "timeline.trim":
        in_us = int(_seconds_args(payload, 0, 0.0) * 1_000_000)
        out_us = (
            int(_seconds_args(payload, 1, duration_us / 1_000_000) * 1_000_000)
            if len(payload.args) > 1
            else duration_us
        )
        out_us = min(out_us, duration_us)
        if out_us <= in_us:
            raise CreativeRenderError("invalid_request", "trim needs in < out (seconds)")
        return [TrimOp(in_us=in_us, out_us=out_us)], None
    if canonical_id == "timeline.speed_ramp":
        return [SpeedOp(factor=_seconds_args(payload, 0, 1.0))], None
    if canonical_id == "timeline.reverse_segment":
        return [ReverseOp()], None
    if canonical_id == "color.adjust_exposure":
        return [ExposureOp(exposure_ev=_seconds_args(payload, 0, 0.0))], None
    if canonical_id == "delivery.make_proxy_480p":
        return [], LaneProfile(width=854, height=480)
    raise CreativeRenderError("unsupported_operation", canonical_id)


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    """Materialise a document artifact atomically: temp file → rename.

    A crash mid-write can never leave a half-written file under the final
    name, so the destination path is only ever a whole document (the same
    guarantee the render lane gives media via its ``.part`` staging).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.stem}.part{path.suffix}")
    staging.write_text(text, encoding="utf-8")
    staging.replace(path)


def _default_caption_engine() -> Any:  # noqa: ANN401 - CaptionEnginePort
    """Optional-extra resolution (SLIDESHOW.md ``[speech]`` row): default
    installs report unavailable and the chain fails *closed*, typed and
    translated — it never silently degrades to a stub."""
    try:
        from nexus_ai_agent.adapters.whisper_local import WhisperLocalCaptionEngine

        engine = WhisperLocalCaptionEngine()
        if engine.is_available():
            return engine
    except Exception:  # pragma: no cover - adapter import failure is env-level
        pass
    from nexus_ai_agent.creative.caption.unavailable_adapter import UnavailableCaptionAdapter

    return UnavailableCaptionAdapter()


async def _run_render_branch(
    payload: CreativeRenderPayload,
    workspace: Path,
    canonical_id: str,
) -> dict[str, Any]:
    from nexus_ai_agent.creative.rendering.executor import render_lane
    from nexus_ai_agent.creative.rendering.ir import lane_ir_from_project
    from nexus_ai_agent.creative.slideshow.ffmpeg import (
        FfmpegUnavailableError,
        probe_video,
        resolve_ffmpeg_bin,
        sha256_file,
    )

    input_path = _guarded_input(payload, workspace)
    try:
        binary = resolve_ffmpeg_bin()
    except FfmpegUnavailableError as exc:
        raise CreativeRenderError("ffmpeg_unavailable", str(exc)) from exc

    try:
        probed = probe_video(input_path, binary=binary)
    except Exception as exc:
        raise CreativeRenderError("invalid_request", f"unreadable media: {exc}") from exc
    duration_us = payload.media_duration_us or probed.duration_us

    sha256 = sha256_file(input_path)
    project = _build_project(payload=payload, duration_us=duration_us, sha256=sha256)

    # 1) canonical operation (state mutation + history + measured record)
    output = _dispatch(
        project,
        operation=canonical_id,
        input_data=_operation_inputs(payload, duration_us),
        idempotency_key=payload.idempotency_key,
    )
    render_project = project  # lane instrumentation below mirrors the SAME op

    # 2) render lane (measured artifact, allow-listed binary)
    lane_ops_raw, profile = _lane_ops(payload, canonical_id, duration_us)
    media_paths = {SOURCE_ASSET_ID: str(input_path)}
    try:
        ir = lane_ir_from_project(
            render_project, media_paths, SOURCE_ASSET_ID, list(lane_ops_raw), profile=profile
        )
    except Exception as exc:
        raise CreativeRenderError("invalid_request", f"lane materialisation failed: {exc}") from exc

    out_path = workspace / "output.mp4"
    try:
        # No unlink: the lane stages to ``.part`` and publishes by atomic
        # rename. ``overwrite=True`` is scoped to this job's own workspace
        # path — a retry replaces only its own previous attempt, and a
        # failed re-render leaves the previous bytes untouched.
        artifact = render_lane(ir, out_path, binary=binary, overwrite=True)
    except Exception as exc:
        raise CreativeRenderError("render_failed", f"{type(exc).__name__}: {exc}") from exc

    measured = probe_video(out_path, binary=binary)
    return {
        "success": True,
        "artifact_path": str(out_path),
        "artifact_kind": "video",
        "sha256": sha256_file(out_path),
        "size_bytes": out_path.stat().st_size,
        "duration_us": measured.duration_us,
        "height": measured.height,
        "operation": canonical_id,
        "output_asset_id": str(output.get("asset_id", "out")),
        "workspace_dir": str(workspace),
        "spec_ir_hash": artifact.lane_ir_hash,
    }


async def _run_otio_branch(payload: CreativeRenderPayload, workspace: Path) -> dict[str, Any]:
    input_path = _guarded_input(payload, workspace) if payload.input_path else None
    duration_us = payload.media_duration_us or 0
    if input_path is not None:
        from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file

        sha256 = sha256_file(input_path)
    else:
        sha256 = _sha256_text("")
    project = _build_project(payload=payload, duration_us=duration_us, sha256=sha256)
    output = _dispatch(
        project,
        operation="delivery.export_otio",
        input_data=_operation_inputs(payload, duration_us),
        idempotency_key=payload.idempotency_key,
    )
    otio_text = output.get("otio_json")
    if not isinstance(otio_text, str) or not otio_text.strip():
        raise CreativeRenderError("render_failed", "export produced no OTIO document")
    out_path = workspace / "timeline.otio"
    _atomic_write_text(out_path, otio_text)
    return {
        "success": True,
        "artifact_path": str(out_path),
        "artifact_kind": "document",
        "sha256": sha256_file(out_path),
        "size_bytes": out_path.stat().st_size,
        "duration_us": duration_us,
        "operation": "delivery.export_otio",
        "output_asset_id": str(output.get("asset_id", "timeline")),
        "workspace_dir": str(workspace),
    }


async def _run_caption_branch(
    payload: CreativeRenderPayload,
    workspace: Path,
    canonical_id: str,
) -> dict[str, Any]:
    from nexus_ai_agent.creative.packs.caption.formatters import format_srt

    input_path = _guarded_input(payload, workspace)
    engine = _default_caption_engine()
    if not engine.is_available():
        raise CreativeRenderError(
            "caption_profile_unavailable", "no caption engine on this installation"
        )
    language = payload.args[0] if payload.args else None
    transcript = await engine.transcribe(input_path, language=language)

    from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file

    sha256 = sha256_file(input_path)
    duration_us = payload.media_duration_us or 0
    if transcript.segments:
        duration_us = max(duration_us, max(seg.end_us for seg in transcript.segments))
    project = _build_project(payload=payload, duration_us=duration_us, sha256=sha256)

    # canonical operation (history + outcome), transcript pinned as evidence
    _dispatch(
        project,
        operation=canonical_id,
        input_data={
            "audio_asset_id": SOURCE_ASSET_ID,
            "transcript": transcript.model_dump(mode="json"),
        },
        idempotency_key=payload.idempotency_key,
    )

    out_path = workspace / "captions.srt"
    try:
        srt_text = format_srt(transcript)
        _atomic_write_text(out_path, srt_text)
    except Exception as exc:
        raise CreativeRenderError("render_failed", f"srt materialisation failed: {exc}") from exc

    return {
        "success": True,
        "artifact_path": str(out_path),
        "artifact_kind": "document",
        "sha256": sha256_file(out_path),
        "size_bytes": out_path.stat().st_size,
        "duration_us": duration_us,
        "operation": canonical_id,
        "output_asset_id": "captions",
        "workspace_dir": str(workspace),
    }


# ---------------------------------------------------------------------------
# Queue entrypoint (worker.py registers this exact symbol)
# ---------------------------------------------------------------------------


async def creative_render_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one staged creative request through the canonical chain."""
    try:
        data = CreativeRenderPayload.model_validate(payload)
    except Exception as exc:
        return {"success": False, "error_code": "invalid_request", "error_detail": str(exc)}

    try:
        workspace = _guarded_workspace(data.workspace_dir)
    except CreativeRenderError as exc:
        return {"success": False, "error_code": exc.code, "error_detail": exc.detail}

    canonical_id = SURFACE_TO_CANONICAL.get((data.command, data.operation))
    if canonical_id is None:
        return {
            "success": False,
            "error_code": "unsupported_operation",
            "error_detail": f"/{data.command} {data.operation}",
        }
    if not workspace.exists():
        return {
            "success": False,
            "error_code": "media_missing",
            "error_detail": "staged workspace missing",
        }

    logger.info(
        "creative_render_start",
        command=data.command,
        operation=data.operation,
        idempotency_key=data.idempotency_key,
    )
    try:
        if canonical_id == "delivery.export_otio":
            result = await _run_otio_branch(data, workspace)
        elif canonical_id == "caption.transcribe":
            result = await _run_caption_branch(data, workspace, canonical_id)
        else:
            result = await _run_render_branch(data, workspace, canonical_id)
    except CreativeRenderError as exc:
        logger.warning("creative_render_typed_failure", code=exc.code, detail=exc.detail)
        return {"success": False, "error_code": exc.code, "error_detail": exc.detail}
    logger.info("creative_render_done", operation=data.operation, sha256=result["sha256"][:26])
    result.pop("_artifact_probe", None)
    return result


def cleanup_workspace(record: dict[str, Any]) -> None:
    """Best-effort tidy of the job workspace (after delivery). Never raises."""
    directory = record.get("workspace_dir")
    if not directory:
        return
    try:
        shutil.rmtree(directory, ignore_errors=True)
    except OSError:  # pragma: no cover - foreground best effort
        pass
