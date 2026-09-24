"""Worker-side adapter for ``creative_render`` jobs — the canonical chain.

Job lifecycle (task-166, CREATIVE_STUDIO plan §4–§5; Gate-5 closure):

    validated payload
      → workspace containment check (payloads are a queue trust boundary)
      → packs runtime registry (CapabilityRegistry, composed packs)
      → CommandBus.dispatch (typed pack operation, idempotency-keyed)
      → render lane (FFmpeg via the codebase's binary allow-list) into STAGING
      → canonical artifact verification (``creative.artifacts.verify_artifact``)
      → atomic publication to the destination (never over a valid artifact)
      → verified evidence + traceability reported back to the queue

Gate-5 rule: **execution success is not job success.** This worker may only
report ``success: True`` after the canonical verifier accepted the bytes it is
about to publish, and it may only publish through
:func:`nexus_ai_agent.application.artifact_publication.publish_artifact`, which
refuses to overwrite a valid destination artifact that does not carry this
job's own identity sidecar. A typed failure carries its ``failure_class`` so the
queue can persist ``FAILED_RETRYABLE`` / ``TERMINAL_FAILED`` instead of a
``COMPLETED`` row that no reader could tell apart from a real success.

Translation and user-facing copy live in the bot completion notifier; this
module returns typed results instead:

* ``{"success": True, ...}`` — measured artifact facts (sha256-only truth),
* ``{"success": False, "error_code": <typed>}`` — expected, user-typed
  failure (bad args, unavailable caption engine, ...) — the job COMPLETES,
  the notifier translates the code;
* **raises** — unexpected environment failure → job FAILED, operator-visible.

Nothing here imports Telegram; the worker runs outside the bot process.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.application.artifact_publication import (
    DestinationOccupied,
    PublicationError,
    publish_artifact,
    verify_document_artifact,
)
from nexus_ai_agent.application.job_lifecycle import (
    RESULT_FAILURE_CLASS,
    RESULT_PHASE_TRAIL,
    RESULT_TRACEABILITY,
    ArtifactTrace,
    FailureClass,
    JobPhase,
    PhaseTrack,
)
from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

CREATIVE_RENDER_JOB_TYPE = "creative_render"
WORKSPACE_PREFIX = "creative_"
SOURCE_ASSET_ID = "src"

#: Surface (command, operation) → canonical packs operation id (closed set —
#: this map *is* the surface's promise).  Session 3 adds ``lut``/``burnin``:
#: shipped ``.cube`` assets (``creative.luts``) and the ``lut``/``subtitle``
#: lane instruments now give both an honest execution path (CREATIVE_STUDIO
#: §7).  Anything hand-queued outside this map dies as ``unsupported_operation``.
SURFACE_TO_CANONICAL: dict[tuple[str, str], str] = {
    ("edit", "trim"): "timeline.trim",
    ("edit", "speed"): "timeline.speed_ramp",
    ("edit", "reverse"): "timeline.reverse_segment",
    ("caption", "transcribe"): "caption.transcribe",
    ("caption", "burnin"): "caption.burn_in",
    ("grade", "exposure"): "color.adjust_exposure",
    ("grade", "lut"): "color.apply_lut",
    ("grade", "proxy"): "delivery.make_proxy_480p",
    ("grade", "otio"): "delivery.export_otio",
}

#: Asset id of the worker-staged caption file inside a burnin job project.
JOB_CAPTION_ASSET_ID = "job-caption"

#: Max caption text a burnin job stages (single SRT line; longer text is a
#: different job shape, not a silent truncation).
BURNIN_MAX_TEXT_CHARS = 500

#: Typed failure codes surfaced to users via ``creative.failed.<code>``.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "unsupported_operation",
        "media_missing",
        "caption_profile_unavailable",
        "render_failed",
        "ffmpeg_unavailable",
        # Gate 5: the verification gate and the destination policy are failures
        # in their own right — they are never mapped to a success.
        "artifact_verification_failed",
        "artifact_destination_conflict",
    }
)

#: Failure classification per code (the *evidence* for the retryable/terminal
#: split, one rationale per entry). Retryability is a classification, not a
#: scheduler: the queue has no automatic retry — it tells an operator whether a
#: retry is meaningful, and the publication policy guarantees that a retry can
#: never destroy an already-valid artifact.
FAILURE_CLASS_BY_CODE: dict[str, FailureClass] = {
    # payload/permanent: retrying the identical request cannot change the answer
    "invalid_request": FailureClass.TERMINAL,
    "unsupported_operation": FailureClass.TERMINAL,
    "media_missing": FailureClass.TERMINAL,
    "artifact_destination_conflict": FailureClass.TERMINAL,
    # environment/transient: the same request can succeed once the world changes
    "ffmpeg_unavailable": FailureClass.RETRYABLE,
    "caption_profile_unavailable": FailureClass.RETRYABLE,
    "render_failed": FailureClass.RETRYABLE,
    "artifact_verification_failed": FailureClass.RETRYABLE,
}

#: Why each class was chosen (recorded so the table is auditable, not vibes).
FAILURE_CLASS_RATIONALE: dict[str, str] = {
    "invalid_request": "the payload is wrong; an identical retry fails identically",
    "unsupported_operation": "the surface map has no such operation; retrying cannot add one",
    "media_missing": "the staged input is gone; the operator/user must re-issue the command",
    "artifact_destination_conflict": "a valid artifact of another identity owns the destination; "
    "an operator must decide, a retry must not guess",
    "ffmpeg_unavailable": "no FFmpeg binary resolved; installing it makes the same retry succeed",
    "caption_profile_unavailable": "the optional speech engine is absent; installing it makes "
    "the same retry succeed",
    "render_failed": "the encode crashed (resource/codec/timeout); a later attempt can succeed, "
    "and the publication policy never lets it damage a valid artifact",
    "artifact_verification_failed": "the produced bytes did not verify; a re-render can produce "
    "valid bytes, and an invalid destination is quarantined rather than trusted",
}


def failure_class_for(code: str) -> FailureClass:
    """Classify a typed failure code; unknown codes fail closed to TERMINAL."""
    return FAILURE_CLASS_BY_CODE.get(code, FailureClass.TERMINAL)


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
    allow_experimental: bool = False
    """Per-job opt-in for EXPERIMENTAL packs (delivery/audio/motion/vision).

    The bus lifecycle gate (step 3.5) refuses experimental packs without it;
    queueing a job with this flag is the explicit operator act."""


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


def _build_project(  # noqa: ANN401
    *,
    payload: CreativeRenderPayload,
    duration_us: int,
    sha256: str,
    caption_sha256: str | None = None,
) -> Any:
    """One asset ("src") + an empty main timeline — the minimal central state
    a pack operation can lawfully mutate.  Burnin jobs additionally register
    the staged SRT (its real file hash) as the caption asset."""
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
    assets = [src]
    if caption_sha256 is not None:
        caption = AssetRecord(
            asset_id=JOB_CAPTION_ASSET_ID,
            media_kind="caption",
            content_sha256=caption_sha256,
            duration_us=duration_us,
            parent_asset_ids=(),
            provenance={
                "origin": "worker.staged_srt",
                "idempotency_key": payload.idempotency_key,
            },
        )
        assets.append(caption)
    return project.model_copy(update={"assets": assets})


@dataclass(frozen=True)
class _DispatchOutcome:
    """The bus facts the job result must be able to trace back to.

    ``output`` is the operation's own payload; the identity fields are what
    makes ``command_id → project_id → operation_id → revision`` reconstructible
    from the persisted result (the queue stamps the ``job_id``).
    """

    command_id: str
    transaction_id: str
    state_revision: int | None
    state_hash: str | None
    output: dict[str, Any]


def _dispatch(
    project: Any,  # noqa: ANN401
    *,
    operation: str,
    input_data: dict[str, Any],
    idempotency_key: str,
    allow_experimental: bool = False,
    confirmed: bool = False,
) -> _DispatchOutcome:
    """Registry lookup → bus dispatch. Bad args become typed
    ``invalid_request`` (a retry with the same payload fails identically)."""
    from nexus_ai_agent.creative.packs.runtime import build_runtime_registry
    from nexus_ai_agent.creative.studio.authorization import ProjectAccess
    from nexus_ai_agent.creative.studio.bus import CommandBus
    from nexus_ai_agent.creative.studio.models import (
        ActorIdentity,
        CommandProvenance,
        InputRef,
        InputRefMetadata,
        RequestContext,
        TargetRef,
        TypedCommand,
    )

    # A queued payload's user_id/project_id is *not* authentication. This
    # worker owns the ephemeral per-job project; only its fixed service actor
    # receives a grant. The Telegram edge authenticates the human separately.
    worker = ActorIdentity(kind="service", actor_id="nagar.creative-render-worker")
    bus = CommandBus(
        state=project,
        registry=build_runtime_registry(),
        authorizer=ProjectAccess(
            actor=worker,
            project_id=project.project_id,
            permissions=frozenset({"project:read", "project:write"}),
        ),
        allow_experimental=allow_experimental,
    )
    command = TypedCommand(
        command_id=f"cmd-{idempotency_key}-{operation}",
        actor=worker,
        provenance=CommandProvenance(source="service", source_id="creative_render"),
        request_context=RequestContext(channel="telegram", request_id=idempotency_key),
        operation=operation,
        input=input_data,
        input_refs=(
            InputRef(
                ref_type="asset",
                project_id=project.project_id,
                ref_id=SOURCE_ASSET_ID,
                metadata=InputRefMetadata(media_kind="video"),
            ),
        ),
        target=TargetRef(project_id=project.project_id, track_id="main"),
        idempotency_key=idempotency_key,
        confirmed=confirmed,
    )
    try:
        result = bus.dispatch(command)
    except Exception as exc:
        raise CreativeRenderError("invalid_request", f"{type(exc).__name__}: {exc}") from exc
    return _DispatchOutcome(
        command_id=command.command_id,
        transaction_id=result.transaction_id,
        state_revision=result.state_revision,
        state_hash=result.state_hash,
        output=dict(result.output or {}),
    )


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
    if payload.operation == "lut":
        lut_name = payload.args[0] if payload.args else "warm"
        intensity = _seconds_args(payload, 1, 1.0)
        return {
            "clip_asset_id": SOURCE_ASSET_ID,
            "lut_name": lut_name,
            "intensity": intensity,
        }
    if payload.operation == "burnin":
        _burnin_text(payload)  # validates now; the branch stages the SRT
        return {
            "video_asset_id": SOURCE_ASSET_ID,
            "caption_asset_id": JOB_CAPTION_ASSET_ID,
            "confirmed": True,
        }
    if payload.operation == "proxy":
        return {"video_asset_id": SOURCE_ASSET_ID, "output_asset_id": "proxy-480p"}
    if payload.operation == "otio":
        return {"frame_rate": 24.0}
    raise CreativeRenderError("unsupported_operation", payload.operation)


def _burnin_text(payload: CreativeRenderPayload) -> str:
    """The single caption line a burnin job burns (validated, never truncated)."""
    raw = payload.args[0] if payload.args else ""
    text = raw.strip()
    if not text:
        raise CreativeRenderError("invalid_request", "burnin needs caption text in args[0]")
    if len(text) > BURNIN_MAX_TEXT_CHARS:
        raise CreativeRenderError(
            "invalid_request",
            f"burnin text is {len(text)} chars (max {BURNIN_MAX_TEXT_CHARS})",
        )
    if "\n" in text:
        raise CreativeRenderError("invalid_request", "burnin stages one line (no newlines)")
    return text


def _srt_timestamp(micros: int) -> str:
    total_ms = max(micros, 0) // 1000
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _stage_burnin_srt(*, text: str, duration_us: int, workspace: Path) -> Path:
    """Stage the burnin caption as a real SRT file covering the full media."""
    if duration_us <= 0:
        raise CreativeRenderError("invalid_request", "burnin needs positive media duration")
    staged = workspace / "caption.srt"
    staged.write_text(
        f"1\n{_srt_timestamp(0)} --> {_srt_timestamp(duration_us)}\n{text}\n",
        encoding="utf-8",
    )
    return staged


def _stage_verify_publish_document(
    *,
    payload: CreativeRenderPayload,
    workspace: Path,
    track: PhaseTrack,
    staging_name: str,
    destination_name: str,
    kind: str,
    text: str,
    canonical_id: str,
    logical_identity: str,
    identity_extra: dict[str, Any],
    command_id: str,
    revision: int | None,
    state_hash: str | None,
    operation_id: str,
    duration_us: int,
    output_asset_id: str,
) -> dict[str, Any]:
    """Stage a document, verify it canonically, publish it atomically.

    Documents have no media streams to probe, so their canonical verifier is
    the dependency-free :func:`verify_document_artifact` (exists ∧ size>0 ∧
    sha256(bytes) ∧ structural marker). Everything else is identical to the
    media path: staging → verify → atomic publication → traceable evidence.
    """
    staging_dir = workspace / f".staging-{uuid4().hex[:12]}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / staging_name
    destination = workspace / destination_name
    identity = {
        "idempotency_key": payload.idempotency_key,
        "operation": canonical_id,
        **identity_extra,
    }
    try:
        staged.write_text(text, encoding="utf-8")
        try:
            verified = verify_document_artifact(staged, kind=kind)
        except Exception as exc:
            raise CreativeRenderError("artifact_verification_failed", str(exc)) from exc
        identity["logical_content_identity"] = logical_identity
        identity["physical_artifact_sha256"] = verified.sha256
        try:
            published = publish_artifact(
                staging_path=staged,
                destination=destination,
                identity=identity,
                verify=lambda candidate: verify_document_artifact(candidate, kind=kind),
            )
        except DestinationOccupied as exc:
            raise CreativeRenderError("artifact_destination_conflict", str(exc)) from exc
        except PublicationError as exc:
            raise CreativeRenderError(
                "artifact_verification_failed", f"publication refused: {exc}"
            ) from exc
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    track.advance(JobPhase.SUCCEEDED)
    trace = ArtifactTrace(
        command_id=command_id,
        project_id=str(identity_extra.get("project_id", "")),
        operation_id=operation_id,
        revision=revision,
        state_hash=state_hash,
        logical_content_identity=logical_identity,
        render_spec_hash=None,
        physical_sha256=published.sha256,
        size_bytes=published.size_bytes,
        artifact_path=published.destination,
        verification={
            "prover": verified.prover,
            "artifact_kind": kind,
            "reused_existing_artifact": published.reused,
        },
    )
    return {
        "success": True,
        "artifact_path": published.destination,
        "artifact_kind": "document",
        "sha256": published.sha256,
        "size_bytes": published.size_bytes,
        "duration_us": duration_us,
        "operation": canonical_id,
        "output_asset_id": output_asset_id,
        "workspace_dir": str(workspace),
        RESULT_PHASE_TRAIL: list(track.trail()),
        RESULT_TRACEABILITY: trace.as_dict(),
    }


def _lane_ops(
    payload: CreativeRenderPayload,
    canonical_id: str,
    duration_us: int,
    *,
    staged_subtitle: Path | None = None,
) -> tuple[list[Any], Any | None]:  # noqa: ANN401
    """Lane-IR instrumentation matching the canonical operation one-to-one.
    Trim is clamped to the measured duration — the media is the truth."""
    from nexus_ai_agent.creative.rendering.ir import (
        ExposureOp,
        LaneProfile,
        LutOp,
        ReverseOp,
        SpeedOp,
        SubtitleOp,
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
    if canonical_id == "color.apply_lut":
        from nexus_ai_agent.creative.luts import LutError, resolve_lut

        lut_name = payload.args[0] if payload.args else "warm"
        intensity = _seconds_args(payload, 1, 1.0)
        try:
            lut_path = resolve_lut(lut_name)
        except LutError as exc:
            raise CreativeRenderError("invalid_request", f"unknown LUT: {exc}") from exc
        try:
            op = LutOp(lut_name=lut_name, lut_path=str(lut_path), intensity=intensity)
        except Exception as exc:
            raise CreativeRenderError("invalid_request", f"bad LUT intensity: {exc}") from exc
        return [op], None
    if canonical_id == "caption.burn_in":
        if staged_subtitle is None or not staged_subtitle.is_file():
            raise CreativeRenderError("invalid_request", "burnin has no staged subtitle file")
        return [SubtitleOp(subtitle_path=str(staged_subtitle))], None
    if canonical_id == "delivery.make_proxy_480p":
        return [], LaneProfile(width=854, height=480)
    raise CreativeRenderError("unsupported_operation", canonical_id)


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _logical_content_identity(
    *,
    canonical_id: str,
    input_data: dict[str, Any],
    source_sha256: str,
    caption_sha256: str | None = None,
) -> str:
    """The media's *logical* identity: ``(parents, operation, parameters)``.

    A derivation digest — it identifies what the media is, never the produced
    file's bytes (that is ``physical_artifact_sha256``, measured from disk by
    the canonical verifier).
    """
    from nexus_ai_agent.creative.artifacts import render_spec_hash_of

    return render_spec_hash_of(
        {
            "operation": canonical_id,
            "params": input_data,
            "parents": {"source": source_sha256, "caption": caption_sha256},
        }
    )


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

    # 0) burnin stages its caption file first: the project registers the SRT's
    # real hash and the lane burns the same file — one staged artifact, no drift.
    staged_subtitle: Path | None = None
    caption_sha256: str | None = None
    if canonical_id == "caption.burn_in":
        staged_subtitle = _stage_burnin_srt(
            text=_burnin_text(payload), duration_us=duration_us, workspace=workspace
        )
        caption_sha256 = sha256_file(staged_subtitle)

    sha256 = sha256_file(input_path)
    project = _build_project(
        payload=payload,
        duration_us=duration_us,
        sha256=sha256,
        caption_sha256=caption_sha256,
    )
    input_data = _operation_inputs(payload, duration_us)

    # RUNNING boundary: the durable PROCESSING mark is already on the row
    # before this handler was invoked (queue._process_job); the attempt trail
    # records it so the result proves the ordering.
    track = PhaseTrack()
    track.advance(JobPhase.RUNNING)

    # 1) canonical operation (state mutation + history + measured record)
    # Queueing a Level-C job IS the explicit user confirmation, so burnin
    # dispatches confirmed; anything else keeps the default (unconfirmed).
    outcome = _dispatch(
        project,
        operation=canonical_id,
        input_data=input_data,
        idempotency_key=payload.idempotency_key,
        allow_experimental=payload.allow_experimental,
        confirmed=canonical_id == "caption.burn_in",
    )
    render_project = project  # lane instrumentation below mirrors the SAME op

    # 2) render lane (measured artifact, allow-listed binary) — into STAGING.
    # The lane keeps its own ``.<name>.part.<ext>`` + atomic rename internally;
    # the *destination* is only ever reached through the publication gate, so
    # an unverified or partial render can never become the artifact.
    lane_ops_raw, profile = _lane_ops(
        payload, canonical_id, duration_us, staged_subtitle=staged_subtitle
    )
    media_paths = {SOURCE_ASSET_ID: str(input_path)}
    try:
        ir = lane_ir_from_project(
            render_project, media_paths, SOURCE_ASSET_ID, list(lane_ops_raw), profile=profile
        )
    except Exception as exc:
        raise CreativeRenderError("invalid_request", f"lane materialisation failed: {exc}") from exc

    staging_dir = workspace / f".staging-{uuid4().hex[:12]}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_output = staging_dir / "output.mp4"
    # Subtitle fonts resolve like the ffprobe binary: explicit env override,
    # else libass system fonts (documented in CREATIVE_RUNTIME §fonts).
    fontsdir = os.environ.get("NEXUS_FONTS_DIR") or None

    # VERIFYING boundary: bytes exist; nothing may be verified or published
    # before this point, and SUCCEEDED is unreachable until they verify.
    # One cleanup scope covers encode → verify → publish: a refused verify or
    # a crashed encode must not leave staging bytes behind (partial ≠ final).
    try:
        track.advance(JobPhase.VERIFYING)
        try:
            artifact = render_lane(
                ir, staged_output, binary=binary, overwrite=True, fontsdir=fontsdir
            )
        except Exception as exc:
            raise CreativeRenderError("render_failed", f"{type(exc).__name__}: {exc}") from exc

        from nexus_ai_agent.creative.artifacts import ArtifactVerificationError, verify_artifact

        logical_identity = _logical_content_identity(
            canonical_id=canonical_id,
            input_data=input_data,
            source_sha256=sha256,
            caption_sha256=caption_sha256,
        )
        try:
            verified = verify_artifact(
                staged_output,
                logical_content_identity=logical_identity,
                render_spec_hash=artifact.lane_ir_hash,
                expected_sha256=artifact.sha256,
                expected_duration_us=artifact.duration_us,
                provenance={
                    "job_operation": canonical_id,
                    "idempotency_key": payload.idempotency_key,
                    "lane_ir_hash": artifact.lane_ir_hash,
                    "lane_ops": list(artifact.ops),
                    "lane_binary": artifact.binary,
                },
                ffmpeg_bin=binary,
            )
        except ArtifactVerificationError as exc:
            raise CreativeRenderError("artifact_verification_failed", str(exc)) from exc

        # 3) atomic publication (never over a valid artifact of another identity)
        identity = {
            "idempotency_key": payload.idempotency_key,
            "operation": canonical_id,
            "project_id": project.project_id,
            "state_revision": outcome.state_revision,
            "state_hash": outcome.state_hash,
            "logical_content_identity": verified.logical_content_identity,
            "physical_artifact_sha256": verified.physical_sha256,
        }
        out_path = workspace / "output.mp4"
        try:
            published = publish_artifact(
                staging_path=staged_output,
                destination=out_path,
                identity=identity,
                verify=lambda candidate: verify_artifact(
                    candidate,
                    logical_content_identity=logical_identity,
                    render_spec_hash=artifact.lane_ir_hash,
                    ffmpeg_bin=binary,
                ),
            )
        except DestinationOccupied as exc:
            raise CreativeRenderError("artifact_destination_conflict", str(exc)) from exc
        except PublicationError as exc:
            raise CreativeRenderError(
                "artifact_verification_failed", f"publication refused: {exc}"
            ) from exc
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    measured = probe_video(Path(published.destination), binary=binary)
    track.advance(JobPhase.SUCCEEDED)
    trace = ArtifactTrace(
        command_id=outcome.command_id,
        project_id=project.project_id,
        operation_id=canonical_id,
        revision=outcome.state_revision,
        state_hash=outcome.state_hash,
        logical_content_identity=verified.logical_content_identity,
        render_spec_hash=verified.render_spec_hash,
        physical_sha256=published.sha256,
        size_bytes=published.size_bytes,
        artifact_path=published.destination,
        verification={
            "prover": verified.probe.prover,
            "probe": verified.probe.as_dict(),
            "expected_sha256_cross_checked": artifact.sha256,
            "reused_existing_artifact": published.reused,
        },
    )
    return {
        "success": True,
        "artifact_path": published.destination,
        "artifact_kind": "video",
        "sha256": published.sha256,
        "size_bytes": published.size_bytes,
        "duration_us": measured.duration_us,
        "height": measured.height,
        "operation": canonical_id,
        "output_asset_id": str(outcome.output.get("asset_id", "out")),
        "workspace_dir": str(workspace),
        RESULT_PHASE_TRAIL: list(track.trail()),
        RESULT_TRACEABILITY: trace.as_dict(),
        "_artifact_probe": repr(artifact)[:120],  # debugging only, never user-facing
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
    input_data = _operation_inputs(payload, duration_us)
    track = PhaseTrack()
    track.advance(JobPhase.RUNNING)
    outcome = _dispatch(
        project,
        operation="delivery.export_otio",
        input_data=input_data,
        idempotency_key=payload.idempotency_key,
        allow_experimental=payload.allow_experimental,
    )
    otio_text = outcome.output.get("otio_json")
    if not isinstance(otio_text, str) or not otio_text.strip():
        raise CreativeRenderError("render_failed", "export produced no OTIO document")

    track.advance(JobPhase.VERIFYING)
    return _stage_verify_publish_document(
        payload=payload,
        workspace=workspace,
        track=track,
        staging_name="timeline.otio",
        destination_name="timeline.otio",
        kind="otio",
        text=otio_text,
        canonical_id="delivery.export_otio",
        identity_extra={
            "project_id": project.project_id,
            "state_revision": outcome.state_revision,
            "state_hash": outcome.state_hash,
        },
        command_id=outcome.command_id,
        revision=outcome.state_revision,
        state_hash=outcome.state_hash,
        operation_id="delivery.export_otio",
        duration_us=duration_us,
        output_asset_id=str(outcome.output.get("asset_id", "timeline")),
        logical_identity=_logical_content_identity(
            canonical_id="delivery.export_otio",
            input_data=input_data,
            source_sha256=sha256,
        ),
    )


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
    outcome = _dispatch(
        project,
        operation=canonical_id,
        input_data={
            "audio_asset_id": SOURCE_ASSET_ID,
            "transcript": transcript.model_dump(mode="json"),
        },
        idempotency_key=payload.idempotency_key,
    )

    track = PhaseTrack()
    track.advance(JobPhase.RUNNING)
    try:
        srt_text = format_srt(transcript)
    except Exception as exc:
        raise CreativeRenderError("render_failed", f"srt materialisation failed: {exc}") from exc
    track.advance(JobPhase.VERIFYING)
    return _stage_verify_publish_document(
        payload=payload,
        workspace=workspace,
        track=track,
        staging_name="captions.srt",
        destination_name="captions.srt",
        kind="srt",
        text=srt_text,
        canonical_id=canonical_id,
        identity_extra={
            "project_id": project.project_id,
            "state_revision": outcome.state_revision,
            "state_hash": outcome.state_hash,
        },
        command_id=outcome.command_id,
        revision=outcome.state_revision,
        state_hash=outcome.state_hash,
        operation_id=canonical_id,
        duration_us=duration_us,
        output_asset_id="captions",
        logical_identity=_logical_content_identity(
            canonical_id=canonical_id,
            input_data={"audio_asset_id": SOURCE_ASSET_ID},
            source_sha256=sha256,
        ),
    )


# ---------------------------------------------------------------------------
# Queue entrypoint (worker.py registers this exact symbol)
# ---------------------------------------------------------------------------


def _failure(code: str, detail: str = "") -> dict[str, Any]:
    """One typed failure envelope, class included (the queue maps it to a status)."""
    return {
        "success": False,
        "error_code": code,
        "error_detail": detail,
        RESULT_FAILURE_CLASS: failure_class_for(code).value,
    }


async def creative_render_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one staged creative request through the canonical chain.

    Every return path here declares the outcome contract: ``success: True``
    only after the canonical verifier accepted the published bytes, otherwise a
    typed ``success: False`` envelope with its failure class. Nothing in this
    function can report success for an unverified artifact — the success
    envelopes are constructed after verification and nowhere else.
    """
    try:
        data = CreativeRenderPayload.model_validate(payload)
    except Exception as exc:
        return _failure("invalid_request", str(exc))

    try:
        workspace = _guarded_workspace(data.workspace_dir)
    except CreativeRenderError as exc:
        return _failure(exc.code, exc.detail)

    canonical_id = SURFACE_TO_CANONICAL.get((data.command, data.operation))
    if canonical_id is None:
        return _failure("unsupported_operation", f"/{data.command} {data.operation}")
    if not workspace.exists():
        return _failure("media_missing", "staged workspace missing")

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
        return _failure(exc.code, exc.detail)
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
