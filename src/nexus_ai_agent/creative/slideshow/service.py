"""End-to-end slideshow planning: real files in, one atomic edit out.

This is the orchestration layer the CLI (and, later, the bot/API) calls:

1. probe the input files and hash them (:mod:`probe`);
2. estimate a beat grid from the soundtrack (:mod:`audio`);
3. optionally score the images — locally or, with an explicit opt-in, with a
   hosted model (:mod:`analysis`);
4. dispatch the pack's typed commands through the *real* command bus
   (``slideshow.scan_assets`` -> ``slideshow.score_images`` ->
   ``slideshow.suggest_tone`` -> ``slideshow.compose``), so the whole path is
   the production path: registry gating, permission ladder, idempotency,
   preconditions, one atomic transaction, undo.

Nothing here writes project files: the bus state is in memory, exactly like
Wave 1.  Persistence is a separate concern and a separate decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from nexus_ai_agent.creative.packs.slideshow.models import (
    AssetEvidence,
    BeatGrid,
    ComposeInput,
    ImageScore,
    ShotSelection,
    SlideshowAnalysis,
)
from nexus_ai_agent.creative.packs.slideshow.operations import (
    OPERATION_COMPOSE,
    OPERATION_SCAN,
    OPERATION_SCORE,
    OPERATION_SUGGEST_TONE,
    build_slideshow_registry,
    tone_library,
)
from nexus_ai_agent.creative.packs.slideshow.planning import MICROSECONDS_PER_SECOND
from nexus_ai_agent.creative.slideshow.analysis import analyze
from nexus_ai_agent.creative.slideshow.audio import AudioError, beat_grid_for_file
from nexus_ai_agent.creative.slideshow.probe import probe_audio, probe_image
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import Playhead, Timeline, new_project

DEFAULT_FALLBACK_BPM = 100.0


@dataclass(frozen=True)
class PlanningRequest:
    """Everything one planning run needs (no globals, no hidden state)."""

    images: tuple[Path, ...]
    target_duration_us: int
    audio: Path | None = None
    mode: Literal["auto", "manual"] = "auto"
    template_id: str | None = None
    shot_seconds: tuple[float, ...] = ()
    provider: Literal["local", "gemini"] = "local"
    allow_image_upload: bool = False
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.0-flash"
    resolution: str = "1920x1080"
    aspect_ratio: str = "16:9"
    fps: int | None = None
    project_name: str = "slideshow"
    transport: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PlanningOutcome:
    """The plan plus the audit trail of the commands that produced it."""

    plan: dict[str, Any]
    template_id: str
    ordered_evidence_ids: tuple[str, ...]
    warnings: tuple[str, ...]
    alignment_quality: str
    tempo_bpm: float | None
    beat_confidence: float
    state_revision: int
    state_hash: str
    commands: tuple[str, ...]
    asset_count: int


def _command(operation: str, payload: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
    return {
        "protocol_version": "nagar.command.v1",
        "command_id": f"cmd_{uuid4().hex[:16]}",
        "session_id": "slideshow-cli",
        "operation": operation,
        "input": payload,
        "confirmed": confirmed,
        "idempotency_key": f"{operation}:{uuid4().hex[:16]}",
    }


def _manual_shot_durations(
    shot_seconds: tuple[float, ...], *, target_duration_us: int, fps: int
) -> list[int]:
    """Convert seconds to microseconds, absorbing at most one frame of rounding.

    The pack requires the shots to tile the target duration exactly; a human
    typing ``4,4,4`` for a 12 s master is one rounding step away from that, so
    the last shot absorbs the remainder when it is within one frame.
    """
    durations_us = [int(round(value * MICROSECONDS_PER_SECOND)) for value in shot_seconds]
    total = sum(durations_us)
    frame_us = int(round(MICROSECONDS_PER_SECOND / max(1, fps)))
    delta = target_duration_us - total
    if delta and abs(delta) > frame_us:
        raise ValueError(
            f"manual shots sum to {total}us, which is more than one frame "
            f"({frame_us}us) away from the target {target_duration_us}us"
        )
    durations_us[-1] += delta
    return durations_us


def plan_from_files(request: PlanningRequest) -> PlanningOutcome:
    """Run the whole planning pipeline for a request and return the outcome."""
    if not request.images:
        raise ValueError("at least one image is required")
    library = tone_library()

    images: list[AssetEvidence] = [probe_image(path) for path in request.images]
    audio_path = request.audio
    audio_evidence = probe_audio(audio_path) if audio_path is not None else None

    beat_grid: BeatGrid | None = None
    if audio_path is not None:
        try:
            beat_grid = beat_grid_for_file(audio_path, fallback_bpm=DEFAULT_FALLBACK_BPM)
        except AudioError:
            beat_grid = None

    tempo_bpm = beat_grid.tempo_bpm if beat_grid is not None else None
    fps = request.fps or 30

    project = new_project(
        project_id=f"proj_{uuid4().hex[:12]}",
        name=request.project_name,
        timeline=Timeline(
            timeline_id=f"tl_{uuid4().hex[:12]}",
            duration_us=0,
            playhead=Playhead(timecode_us=0),
        ),
    )
    bus = CommandBus(project, registry=build_slideshow_registry(library=library))
    commands: list[str] = []

    scan_payload: dict[str, Any] = {
        "assets": [evidence.model_dump(mode="json") for evidence in images]
    }
    if audio_evidence is not None:
        scan_payload["assets"].append(audio_evidence.model_dump(mode="json"))
    bus.dispatch(_command(OPERATION_SCAN, scan_payload))
    commands.append(OPERATION_SCAN)

    analysis: SlideshowAnalysis | None = None
    if request.mode == "auto":
        analysis = analyze(
            tuple(images),
            provider=request.provider,
            api_key=request.gemini_api_key,
            model=request.gemini_model,
            allow_upload=request.allow_image_upload,
            transport=request.transport,
        )
        score_payload: dict[str, Any] = {
            "scores": [score.model_dump(mode="json") for score in analysis.scores],
            "source": analysis.source,
            "reasoning": analysis.reasoning,
        }
        if analysis.source == "gemini":
            score_payload["preferred_order"] = list(analysis.ordered_evidence_ids)
        scored = bus.dispatch(_command(OPERATION_SCORE, score_payload))
        commands.append(OPERATION_SCORE)
        analysis = analysis.model_copy(
            update={
                "ordered_evidence_ids": tuple(scored.output["ordered_evidence_ids"]),
                "scores": tuple(
                    ImageScore.model_validate(item) for item in scored.output["scores"]
                ),
            }
        )

    tone_payload: dict[str, Any] = {
        "image_count": len(images),
        "tempo_bpm": tempo_bpm,
        "requested_template_id": request.template_id,
        "recommended_template_id": analysis.recommended_template_id if analysis else None,
    }
    tone = bus.dispatch(_command(OPERATION_SUGGEST_TONE, tone_payload))
    commands.append(OPERATION_SUGGEST_TONE)
    template_id = tone.output["template_id"]

    template = library.get(template_id)
    if beat_grid is not None and beat_grid.tempo_source != "detected" and audio_path is not None:
        refined = beat_grid_for_file(audio_path, fallback_bpm=template.audio.bpm_fallback)
        if refined.beats_us:
            beat_grid = refined

    shots: list[ShotSelection] | None = None
    if request.mode == "manual":
        if not request.shot_seconds:
            raise ValueError("manual mode needs --shot-seconds (one duration per image)")
        if len(request.shot_seconds) != len(images):
            raise ValueError(
                f"manual mode needs one duration per image "
                f"({len(request.shot_seconds)} given for {len(images)} images)"
            )
        durations_us = _manual_shot_durations(
            request.shot_seconds, target_duration_us=request.target_duration_us, fps=fps
        )
        ordered = tuple(evidence.evidence_id for evidence in images)
        index_of = {evidence_id: position for position, evidence_id in enumerate(ordered)}
        shots = [
            ShotSelection(evidence_id=evidence_id, duration_us=durations_us[index_of[evidence_id]])
            for evidence_id in ordered
        ]

    compose_payload: dict[str, Any] = {
        "assets": [evidence.model_dump(mode="json") for evidence in images],
        "target_duration_us": request.target_duration_us,
        "mode": request.mode,
        "tone_template_id": template_id,
        "resolution": request.resolution,
        "aspect_ratio": request.aspect_ratio,
        "fps": request.fps,
    }
    if audio_evidence is not None:
        compose_payload["audio"] = audio_evidence.model_dump(mode="json")
    if beat_grid is not None:
        compose_payload["beat_grid"] = beat_grid.model_dump(mode="json")
    if analysis is not None:
        compose_payload["analysis"] = analysis.model_dump(mode="json")
    if shots is not None:
        compose_payload["shots"] = [shot.model_dump(mode="json") for shot in shots]

    ComposeInput.model_validate(compose_payload)  # fail with a typed error before dispatch
    result = bus.dispatch(_command(OPERATION_COMPOSE, compose_payload))
    commands.append(OPERATION_COMPOSE)

    plan = result.output["plan"]
    return PlanningOutcome(
        plan=plan,
        template_id=plan["template_id"],
        ordered_evidence_ids=tuple(shot["evidence_id"] for shot in plan["shots"]),
        warnings=tuple(plan["warnings"]),
        alignment_quality=plan["alignment_quality"],
        tempo_bpm=beat_grid.tempo_bpm if beat_grid is not None else None,
        beat_confidence=plan["beat_confidence"],
        state_revision=result.state_revision,
        state_hash=result.state_hash,
        commands=tuple(commands),
        asset_count=len(bus.project.assets),
    )
