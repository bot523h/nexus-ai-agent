"""Queue-side adapter for ``creative_render`` — the canonical one-shot chain.

Chain (owner directive 2026-09-24, §3 — exactly one production path):

    Telegram /edit|/caption|/grade
      → typed surface request            (bot/creative_surface.py)
      → JobQueuePort.enqueue             (durable row)
      → this handler                     (trust boundary: a queue row is data,
                                          never a permission)
      → canonical lane IR                (creative/rendering/ir.py)
      → Agent B's single executor        (``render_lane`` — one FFmpeg process)
      → artifact verification            (exists ∧ non-empty ∧ sha256 ∧ re-probe)
      → measured evidence to the queue   (COMPLETED only for a verified artifact)
      → localized delivery               (bot/creative_notify.py)

Rules this module obeys:

* **No second engine.**  It owns no rendering, probing, hashing or downloading
  code.  ``render_lane`` / :class:`LaneArtifact` (creative runtime) and the
  canonical ``probe_video`` / ``sha256_file`` helpers (slideshow ffmpeg module,
  itself the one allow-listed binary resolver) do all the work.
* **No fake success.**  The job is reported successful only after the artifact
  passed :func:`verify_lane_artifact` *in this process*.  Every other outcome
  raises :class:`CreativeRenderError`, so the queue persists a durable
  ``failed`` row (owner directive §8: a failure is a status, not a boolean
  hidden in a result dict that still says COMPLETED).
* **Trust boundary.**  Workspace / input paths arrive from a queue row, so they
  are re-validated here: absolute, inside ``settings.creative_temp_dir``, name
  prefixed ``creative_``, input inside the workspace.  A hand-crafted payload
  cannot make the worker render from an arbitrary path.
* **Single seam for Agent B.**  :func:`build_lane_ir` is the *only* function
  that turns a validated request into lane ops.  When the render-plan bridge
  (``creative/rendering/plan.py`` — PR#64) lands, that function is replaced by
  ``compile_execution_plan``; nothing else in this module changes.
* **Honest operation set.**  Only surface operations with a real lane primitive
  are accepted.  ``caption.burn_in`` (needs a libass stage), ``color.apply_lut``
  (needs a lut3d file stage) and ``delivery.make_proxy_480p`` (produces a *proxy
  record*, not a 480p file — see PR#64 ``MISSING_RENDER_PRIMITIVES``) are
  refused typed instead of being reported as renders.  They are refused twice:
  in the surface (user-facing copy) and here (trust boundary).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

#: Registered in ``nexus_ai_agent.worker.default_job_handlers``.
CREATIVE_RENDER_JOB_TYPE = "creative_render"

#: Workspaces created by the surface carry this prefix; the worker refuses
#: anything else so a stray path can never be rendered from (or cleaned).
WORKSPACE_PREFIX = "creative_"

#: The one output name inside the job workspace.
MASTER_FILENAME = "master.mp4"

#: Surface (command, operation) → canonical pack operation id.  This table *is*
#: the product promise of the surface: every entry has a real lane primitive.
EXECUTABLE_OPERATIONS: dict[tuple[str, str], str] = {
    ("edit", "trim"): "timeline.trim",
    ("edit", "speed"): "timeline.speed_ramp",
    ("edit", "reverse"): "timeline.reverse_segment",
    ("grade", "exposure"): "color.adjust_exposure",
}

#: Closed vocabulary of typed failures.  ``bot/creative_notify.py`` maps each
#: code to ``creative.failed.<code>`` in every locale.
ERROR_CODES: frozenset[str] = frozenset(
    {
        "invalid_request",
        "unsupported_operation",
        "media_missing",
        "media_unusable",
        "render_failed",
        "ffmpeg_unavailable",
        "artifact_verification_failed",
        "timeout",
    }
)

#: Re-probing tolerance between the lane's evidence and our independent probe
#: of the same bytes (both read ``ffmpeg -i`` output; rounding differs only in
#: the last microsecond digits).
PROBE_TOLERANCE_US = 50_000


class CreativeRenderError(RuntimeError):
    """A typed ``creative_render`` failure the queue must persist as FAILED."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown creative render error code: {code!r}")
        super().__init__(f"[{code}] {detail}" if detail else f"[{code}]")
        self.code = code
        self.detail = detail


class CreativeRenderPayload(BaseModel):
    """Validated envelope of a ``creative_render`` queue row."""

    model_config = ConfigDict(extra="forbid")

    command: Literal["edit", "caption", "grade"]
    operation: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    workspace_dir: str = Field(min_length=1)
    input_path: str = Field(min_length=1)
    media_duration_us: int | None = Field(default=None, ge=0)
    user_id: int
    chat_id: int
    lang: str = "en"
    idempotency_key: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# trust boundary — a queue row is data, never a permission
# ---------------------------------------------------------------------------


def _creative_temp_root() -> Path:
    return Path(get_settings().creative_temp_dir).resolve(strict=False)


def _contained_workspace(raw: str) -> Path:
    """Return the job workspace iff it is a reserved directory under the temp root."""
    try:
        workspace = Path(raw).resolve(strict=False)
        root = _creative_temp_root()
    except (OSError, RuntimeError) as exc:  # pragma: no cover - hostile filesystem
        raise CreativeRenderError("invalid_request", f"workspace unusable: {exc}") from exc
    if not workspace.is_relative_to(root):
        raise CreativeRenderError("invalid_request", "workspace escapes the creative root")
    if not workspace.name.startswith(WORKSPACE_PREFIX):
        raise CreativeRenderError("invalid_request", "workspace missing the reserved prefix")
    return workspace


def _contained_input(payload: CreativeRenderPayload, workspace: Path) -> Path:
    """Return the staged source file iff it lives inside the job workspace."""
    try:
        candidate = Path(payload.input_path).resolve(strict=False)
    except OSError as exc:  # pragma: no cover - hostile filesystem
        raise CreativeRenderError("invalid_request", f"input path unusable: {exc}") from exc
    if not candidate.is_relative_to(workspace):
        raise CreativeRenderError("invalid_request", "input path escapes its workspace")
    if not candidate.is_file():
        raise CreativeRenderError("media_missing", "staged source file is not there")
    return candidate


# ---------------------------------------------------------------------------
# the single seam: validated request → canonical lane IR
# ---------------------------------------------------------------------------


#: Numeric operands each operation expects: ``{slot: default}`` (``None`` means
#: required).  Parsed *before* any media work so a malformed request never
#: reaches the encoder.
NUMERIC_SLOTS: dict[str, tuple[float | None, ...]] = {
    "timeline.trim": (0.0, None),
    "timeline.speed_ramp": (None,),
    "color.adjust_exposure": (0.0,),
    "timeline.reverse_segment": (),
}


def canonical_operation(payload: CreativeRenderPayload) -> str:
    """Map a surface request onto the canonical pack operation id (or refuse)."""
    canonical = EXECUTABLE_OPERATIONS.get((payload.command, payload.operation))
    if canonical is None:
        raise CreativeRenderError("unsupported_operation", payload.operation)
    return canonical


def numeric_args(payload: CreativeRenderPayload) -> dict[str, float]:
    """Parse the request's numeric operands, or refuse with a typed error."""
    canonical = canonical_operation(payload)
    slots = NUMERIC_SLOTS[canonical]
    if len(payload.args) > len(slots):
        raise CreativeRenderError(
            "invalid_request", f"{payload.operation} takes at most {len(slots)} arguments"
        )
    parsed: dict[str, float] = {}
    for index, default in enumerate(slots):
        if len(payload.args) <= index:
            if default is None:
                raise CreativeRenderError(
                    "invalid_request", f"argument {index + 1} is required for {payload.operation}"
                )
            parsed[f"arg{index}"] = default
            continue
        raw = payload.args[index]
        try:
            parsed[f"arg{index}"] = float(raw)
        except ValueError as exc:
            raise CreativeRenderError(
                "invalid_request", f"argument {index + 1} must be a number"
            ) from exc
    if canonical == "timeline.trim":
        if parsed["arg0"] < 0:
            raise CreativeRenderError("invalid_request", "trim start must not be negative")
        if "arg1" in parsed and parsed["arg1"] <= parsed["arg0"]:
            raise CreativeRenderError("invalid_request", "trim needs in < out")
    return parsed


def build_lane_ir(
    payload: CreativeRenderPayload,
    *,
    workspace: Path,
    input_path: Path,
    duration_us: int,
) -> Any:  # noqa: ANN401 - creative runtime type; imported lazily on purpose
    """Build the canonical :class:`LaneIR` for one validated surface request.

    This is the integration seam handed to Agent B: the ops below are the lane's
    own primitives (``TrimOp``/``SpeedOp``/``ReverseOp``/``ExposureOp``), and
    replacing this body with ``compile_execution_plan(project, track_id)`` is a
    one-function change (see ``docs/ops/CREATIVE_PRODUCTION_PATH.md`` §5).
    """
    from nexus_ai_agent.creative.rendering.ir import (
        ExposureOp,
        LaneError,
        LaneIR,
        LaneSource,
        ReverseOp,
        SpeedOp,
        TrimOp,
    )

    canonical = canonical_operation(payload)
    numbers = numeric_args(payload)

    ops: list[Any] = []
    if canonical == "timeline.trim":
        in_us = int(numbers["arg0"] * 1_000_000)
        out_us = int(numbers["arg1"] * 1_000_000) if "arg1" in numbers else duration_us
        # The probed media is the truth: clamp rather than trust the client.
        out_us = min(out_us, duration_us)
        if in_us < 0:
            raise CreativeRenderError("invalid_request", "trim start must not be negative")
        if out_us <= in_us:
            raise CreativeRenderError("invalid_request", "trim needs in < out inside the clip")
        ops.append(TrimOp(in_us=in_us, out_us=out_us))
    elif canonical == "timeline.speed_ramp":
        ops.append(SpeedOp(factor=numbers["arg0"]))
    elif canonical == "timeline.reverse_segment":
        ops.append(ReverseOp())
    elif canonical == "color.adjust_exposure":
        ops.append(ExposureOp(exposure_ev=numbers["arg0"]))
    else:  # pragma: no cover - the table above is exhaustive
        raise CreativeRenderError("unsupported_operation", payload.operation)

    try:
        return LaneIR(
            main=LaneSource(
                asset_id="src",
                path=str(input_path),
                media_kind="video",
                duration_us=duration_us,
            ),
            ops=tuple(ops),
        )
    except (LaneError, ValueError) as exc:
        raise CreativeRenderError("invalid_request", str(exc)) from exc


# ---------------------------------------------------------------------------
# artifact verification — COMPLETED means "verified bytes exist", nothing else
# ---------------------------------------------------------------------------


def verify_lane_artifact(
    *,
    artifact: Any,  # noqa: ANN401 - LaneArtifact from the creative runtime
    output_path: Path,
    workspace: Path,
    binary: str,
) -> None:
    """Fail closed unless the produced file is real, complete and where we asked.

    Checks (all independent of the renderer's own bookkeeping):

    1. the lane reported the path we requested, inside the job workspace;
    2. the file exists and is non-empty;
    3. a fresh sha256 of the bytes equals the lane's digest;
    4. a fresh ``ffmpeg -i`` probe of the same bytes reproduces the duration.
    """
    from nexus_ai_agent.creative.slideshow.ffmpeg import probe_video, sha256_file

    reported = Path(str(getattr(artifact, "path", "")))
    if reported != output_path:
        raise CreativeRenderError(
            "artifact_verification_failed", "the lane reported an unexpected output path"
        )
    if not reported.is_relative_to(workspace):
        raise CreativeRenderError(
            "artifact_verification_failed", "artifact escaped the job workspace"
        )
    if not reported.is_file():
        raise CreativeRenderError("artifact_verification_failed", "no artifact was written")
    if reported.stat().st_size <= 0:
        raise CreativeRenderError("artifact_verification_failed", "artifact is empty")

    expected = str(getattr(artifact, "sha256", ""))
    actual = sha256_file(reported)
    if not expected or actual != expected:
        raise CreativeRenderError(
            "artifact_verification_failed", "artifact digest does not match its evidence"
        )

    lane_duration = int(getattr(artifact, "duration_us", 0) or 0)
    if lane_duration <= 0:
        raise CreativeRenderError("artifact_verification_failed", "artifact has no duration")
    try:
        probe = probe_video(reported, binary=binary)
    except Exception as exc:  # noqa: BLE001 - any probe failure is a failed verification
        raise CreativeRenderError(
            "artifact_verification_failed", "artifact could not be probed back"
        ) from exc
    if abs(probe.duration_us - lane_duration) > PROBE_TOLERANCE_US:
        raise CreativeRenderError(
            "artifact_verification_failed", "artifact duration does not match its evidence"
        )


# ---------------------------------------------------------------------------
# the handler
# ---------------------------------------------------------------------------


def _code_for(exc: Exception) -> str:
    from nexus_ai_agent.creative.rendering.executor import LaneExecutionError
    from nexus_ai_agent.creative.slideshow.ffmpeg import FfmpegUnavailableError, RenderError

    if isinstance(exc, CreativeRenderError):
        return exc.code
    if isinstance(exc, FfmpegUnavailableError):
        return "ffmpeg_unavailable"
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
        return "timeout"
    if isinstance(exc, LaneExecutionError):
        return "render_failed"
    if isinstance(exc, RenderError):
        return "render_failed"
    return "render_failed"


def _redact(message: str) -> str:
    """Never persist absolute paths or URLs of the host into the queue row."""
    words = []
    for token in str(message).split():
        words.append(Path(token).name if token.startswith("/") and "/" in token[1:] else token)
    return " ".join(words)[:400]


def _run_render(payload: CreativeRenderPayload) -> dict[str, object]:
    """Blocking half: probe → lane IR → one FFmpeg process → verify → evidence."""
    from nexus_ai_agent.creative.rendering import render_lane
    from nexus_ai_agent.creative.slideshow.ffmpeg import (
        FfmpegUnavailableError,
        RenderError,
        probe_video,
        resolve_ffmpeg_bin,
    )

    settings = get_settings()
    workspace = _contained_workspace(payload.workspace_dir)
    source = _contained_input(payload, workspace)
    # A request that can never render is refused before the media engine is
    # even resolved: no operation without a lane twin, no malformed operands.
    canonical_operation(payload)
    numeric_args(payload)

    try:
        binary = resolve_ffmpeg_bin(settings.ffmpeg_bin)
    except FfmpegUnavailableError as exc:
        raise CreativeRenderError("ffmpeg_unavailable", _redact(str(exc))) from exc

    try:
        source_probe = probe_video(source, binary=binary)
    except RenderError as exc:
        raise CreativeRenderError("media_unusable", _redact(str(exc))) from exc
    duration_us = int(source_probe.duration_us)
    if duration_us <= 0:
        raise CreativeRenderError("media_unusable", "source has no readable duration")

    lane = build_lane_ir(payload, workspace=workspace, input_path=source, duration_us=duration_us)

    output_path = workspace / MASTER_FILENAME
    if output_path.exists():
        # A previous attempt in the same workspace must not be silently reused.
        output_path.unlink()

    try:
        artifact = render_lane(
            lane,
            output_path,
            binary=binary,
            timeout=int(settings.slideshow_render_timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        raise CreativeRenderError("timeout", "the render exceeded its time budget") from exc
    except CreativeRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - every lane failure is typed here
        raise CreativeRenderError(_code_for(exc), _redact(str(exc))) from exc

    verify_lane_artifact(
        artifact=artifact, output_path=output_path, workspace=workspace, binary=binary
    )

    evidence = artifact.evidence()
    return {
        "success": True,
        "verified": True,
        "command": payload.command,
        "operation": payload.operation,
        "canonical_operation": EXECUTABLE_OPERATIONS[(payload.command, payload.operation)],
        "output_path": str(output_path),
        "workspace_dir": str(workspace),
        "output_sha256": evidence["output_sha256"],
        "size_bytes": evidence["size_bytes"],
        "duration_us": evidence["duration_us"],
        "width": evidence["width"],
        "height": evidence["height"],
        "has_audio": evidence["has_audio"],
        "lane_ir_hash": evidence["lane_ir_hash"],
        "ops": evidence["ops"],
    }


async def creative_render_job(payload: dict[str, object]) -> dict[str, object]:
    """``creative_render`` handler: validated payload in, verified artifact out.

    Unexpected environment failures raise :class:`CreativeRenderError` so the
    queue persists ``failed``; there is no code path that returns success
    without :func:`verify_lane_artifact` having passed.
    """
    try:
        validated = CreativeRenderPayload.model_validate(dict(payload))
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError is a bad payload
        raise CreativeRenderError("invalid_request", "payload rejected") from exc

    logger.info(
        "creative_render_started",
        command=validated.command,
        operation=validated.operation,
        user_id=validated.user_id,
    )
    try:
        result = await asyncio.to_thread(_run_render, validated)
    except asyncio.CancelledError:
        raise
    except CreativeRenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - unknown failure must still be durable
        raise CreativeRenderError(_code_for(exc), _redact(str(exc))) from exc
    logger.info(
        "creative_render_finished",
        command=validated.command,
        operation=validated.operation,
        sha256=result["output_sha256"],
        size_bytes=result["size_bytes"],
    )
    return result


__all__ = [
    "CREATIVE_RENDER_JOB_TYPE",
    "ERROR_CODES",
    "EXECUTABLE_OPERATIONS",
    "MASTER_FILENAME",
    "PROBE_TOLERANCE_US",
    "WORKSPACE_PREFIX",
    "CreativeRenderError",
    "CreativeRenderPayload",
    "build_lane_ir",
    "creative_render_job",
    "verify_lane_artifact",
]
