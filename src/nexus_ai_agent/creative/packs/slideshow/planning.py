"""Pure planning rules: pinned evidence + a tone template -> ``SlideshowPlan``.

Everything here is deterministic arithmetic on already-pinned evidence — no
I/O, no network, no randomness.  That is what allows ``slideshow.compose`` to
be a single atomic, undoable edit transaction and what makes the master
reproducible from the state hash alone (TDD section 2.4).

Two invariants are enforced by construction and re-checked by the plan model:

* the shots tile ``[0, target_duration_us)`` exactly — no gap, no overlap;
* beat alignment is *opportunistic*: when snapping would break the minimum
  shot length the arithmetic boundaries win and the reason is recorded in
  ``warnings`` (a bad estimate must never be able to corrupt the plan).
"""

from __future__ import annotations

from typing import Literal

from nexus_ai_agent.creative.packs.slideshow.models import (
    RECOMMENDED_IMAGE_COUNT,
    BeatGrid,
    ComposeInput,
    ShotPlan,
    SlideshowAnalysis,
    SlideshowPlan,
)
from nexus_ai_agent.creative.packs.slideshow.templates import ToneTemplate, ToneTemplateLibrary
from nexus_ai_agent.creative.studio.models import TimeRangeUS

MICROSECONDS_PER_SECOND = 1_000_000

#: A still that is shorter than this is not watchable; below it the planner
#: refuses rather than silently producing a degenerate master.
MIN_SHOT_US = 250_000

#: Role -> relative weight (auto mode): openers breathe, climaxes cut faster.
ROLE_WEIGHTS: dict[str, float] = {
    "opener": 1.15,
    "body": 1.0,
    "climax": 0.85,
    "closer": 1.10,
}

#: Deterministic tone suggestion from measured tempo (upper bound, template id).
_TEMPO_TONE_TABLE: tuple[tuple[float, str], ...] = (
    (70.0, "calm_reflective"),
    (90.0, "nostalgic_film"),
    (108.0, "travel_documentary"),
    (125.0, "tech_product"),
    (140.0, "upbeat_energetic"),
    (float("inf"), "celebration_party"),
)


def resolve_template(
    library: ToneTemplateLibrary,
    *,
    requested_id: str | None,
    analysis: SlideshowAnalysis | None = None,
    tempo_bpm: float | None = None,
    image_count: int = 0,
) -> ToneTemplate:
    """Explicit choice, then the analyzer's suggestion, then the tempo table."""
    if requested_id:
        return library.get(requested_id)
    if analysis is not None and analysis.recommended_template_id:
        if analysis.recommended_template_id in library:
            return library.get(analysis.recommended_template_id)
    return library.get(suggest_template_id(library, tempo_bpm=tempo_bpm, image_count=image_count))


def suggest_template_id(
    library: ToneTemplateLibrary,
    *,
    tempo_bpm: float | None = None,
    image_count: int = 0,
) -> str:
    """Deterministic tone suggestion from measurable evidence (A-level, pure)."""
    if tempo_bpm is not None:
        for upper_bound, template_id in _TEMPO_TONE_TABLE:
            if tempo_bpm <= upper_bound and template_id in library:
                return template_id
    if image_count and image_count < RECOMMENDED_IMAGE_COUNT[0]:
        fallback = "calm_reflective"
    elif image_count > RECOMMENDED_IMAGE_COUNT[1]:
        fallback = "upbeat_energetic"
    else:
        fallback = "travel_documentary"
    if fallback in library:
        return fallback
    return library.ids()[0]


def ordered_evidence_ids(input_payload: ComposeInput) -> tuple[str, ...]:
    """Manual order is authoritative; auto order follows the analysis when valid."""
    if input_payload.mode == "manual":
        assert input_payload.shots is not None  # guaranteed by ComposeInput validation
        return tuple(shot.evidence_id for shot in input_payload.shots)
    analysis = input_payload.analysis
    if analysis is not None:
        available = {asset.evidence_id for asset in input_payload.assets}
        ordered = tuple(analysis.ordered_evidence_ids)
        if len(ordered) == len(available) and set(ordered) == available:
            return ordered
    return tuple(asset.evidence_id for asset in input_payload.assets if asset.media_kind == "image")


def shot_weights(ordered_ids: tuple[str, ...], analysis: SlideshowAnalysis | None) -> list[float]:
    """Role-weighted shot lengths in auto mode; uniform when nothing is known."""
    if analysis is None:
        return [1.0] * len(ordered_ids)
    roles = {score.evidence_id: score.suggested_role for score in analysis.scores}
    return [ROLE_WEIGHTS.get(roles.get(evidence_id, "body"), 1.0) for evidence_id in ordered_ids]


def distribute_us(total_us: int, weights: list[float]) -> list[int]:
    """Split ``total_us`` by ``weights`` with integer microseconds that sum exactly.

    Largest-remainder apportionment with a deterministic tie-break (lowest
    index first), so equal inputs always yield equal slot lengths.
    """
    if not weights:
        raise ValueError("cannot distribute a duration over zero shots")
    positive = [weight if weight > 0 else 0.0 for weight in weights]
    total_weight = sum(positive)
    if total_weight <= 0:
        positive = [1.0] * len(weights)
        total_weight = float(len(weights))
    exact = [total_us * weight / total_weight for weight in positive]
    floors = [int(value) for value in exact]
    remainder = total_us - sum(floors)
    order = sorted(range(len(weights)), key=lambda index: (-(exact[index] - floors[index]), index))
    for index in order[:remainder]:
        floors[index] += 1
    return floors


def _cumulative(durations: list[int]) -> list[int]:
    running = 0
    boundaries = [0]
    for duration in durations:
        running += duration
        boundaries.append(running)
    return boundaries


def snap_boundaries(
    boundaries: list[int],
    beats_us: tuple[int, ...],
    *,
    min_gap_us: int,
) -> list[int] | None:
    """Snap internal boundaries to the nearest beat, or ``None`` if that fails.

    The last boundary is always the target duration, so the exact-sum
    invariant survives snapping by construction.
    """
    if len(boundaries) <= 2 or not beats_us:
        return None
    target = boundaries[-1]
    snapped = [boundaries[0]]
    for boundary in boundaries[1:-1]:
        candidate = min(beats_us, key=lambda beat: (abs(beat - boundary), beat))
        if candidate - snapped[-1] < min_gap_us:
            return None
        snapped.append(candidate)
    if target - snapped[-1] < min_gap_us:
        return None
    snapped.append(target)
    return snapped


def _transition_for(template: ToneTemplate, index: int) -> dict[str, object] | None:
    if index == 0:
        return None
    return {
        "kind": template.transition.kind,
        "duration_us": int(round(template.transition.duration_seconds * MICROSECONDS_PER_SECOND)),
    }


def _motion_for(template: ToneTemplate, index: int) -> dict[str, object]:
    zoom_from, zoom_to = template.motion.zoom_from, template.motion.zoom_to
    if template.motion.alternate and index % 2 == 1:
        zoom_from, zoom_to = zoom_to, zoom_from
    return {"kind": template.motion.kind, "zoom_from": zoom_from, "zoom_to": zoom_to}


def _manual_durations(
    input_payload: ComposeInput, ordered_ids: tuple[str, ...], target_us: int
) -> list[int]:
    assert input_payload.shots is not None  # guaranteed by ComposeInput validation
    durations = [shot.duration_us for shot in input_payload.shots]
    if len(durations) != len(ordered_ids):  # pragma: no cover - ComposeInput guarantees this
        raise ValueError("the manual shot list does not match the ordered image set")
    total = sum(durations)
    if total != target_us:
        raise ValueError(
            f"manual shots sum to {total}us but target_duration_us is {target_us}us "
            "(the plan must tile the target duration exactly)"
        )
    too_short = [duration for duration in durations if duration < MIN_SHOT_US]
    if too_short:
        raise ValueError(
            f"every shot must be at least {MIN_SHOT_US}us; got a shot of {min(too_short)}us"
        )
    return durations


def plan_slideshow(
    input_payload: ComposeInput,
    *,
    library: ToneTemplateLibrary,
    template_id: str | None = None,
) -> SlideshowPlan:
    """Build the deterministic plan for one ``slideshow.compose`` command."""
    beat_grid: BeatGrid | None = input_payload.beat_grid
    tempo = beat_grid.tempo_bpm if beat_grid is not None else None
    ordered_ids = ordered_evidence_ids(input_payload)
    template = resolve_template(
        library,
        requested_id=template_id or input_payload.tone_template_id,
        analysis=input_payload.analysis,
        tempo_bpm=tempo,
        image_count=len(ordered_ids),
    )

    warnings: list[str] = []
    image_count = len(ordered_ids)
    if not (RECOMMENDED_IMAGE_COUNT[0] <= image_count <= RECOMMENDED_IMAGE_COUNT[1]):
        warnings.append(
            f"{image_count} images is outside the recommended "
            f"{RECOMMENDED_IMAGE_COUNT[0]}-{RECOMMENDED_IMAGE_COUNT[1]} range for this pack"
        )

    target_us = input_payload.target_duration_us
    if input_payload.mode == "manual":
        durations = _manual_durations(input_payload, ordered_ids, target_us)
    else:
        durations = distribute_us(target_us, shot_weights(ordered_ids, input_payload.analysis))
        if any(duration < MIN_SHOT_US for duration in durations):
            raise ValueError(
                f"{image_count} images cannot fill {target_us // MICROSECONDS_PER_SECOND}s at "
                f"the {MIN_SHOT_US}us minimum shot length; raise the target duration"
            )

    boundaries = _cumulative(durations)
    alignment: Literal["detected", "interpolated"] = "interpolated"
    tempo_source: Literal["detected", "fallback_bpm", "equal_division"] = "equal_division"
    confidence = 0.0

    if beat_grid is not None and beat_grid.beats_us:
        alignment = beat_grid.alignment_quality
        tempo_source = beat_grid.tempo_source
        confidence = beat_grid.confidence
        if input_payload.mode == "auto" and image_count > 1:
            candidates = (
                beat_grid.strong_beats_us
                if template.rhythm.align_to_strong_beats and beat_grid.strong_beats_us
                else beat_grid.beats_us
            )
            snapped = snap_boundaries(boundaries, tuple(candidates), min_gap_us=MIN_SHOT_US)
            if snapped is not None and snapped != boundaries:
                boundaries = snapped
            elif snapped is None:
                warnings.append(
                    "beat grid is too sparse for this shot count; kept arithmetic boundaries"
                )
    elif image_count > 1:
        warnings.append("no beat grid: shot boundaries are arithmetic, not beat-aligned")

    audio = input_payload.audio
    audio_payload: dict[str, object] = {
        "fade_in_seconds": template.audio.fade_in_seconds,
        "fade_out_seconds": template.audio.fade_out_seconds,
    }
    if audio is not None:
        audio_payload["evidence_id"] = audio.evidence_id
        audio_payload["path"] = audio.path
        audio_payload["source_duration_us"] = audio.duration_us
        if audio.duration_us and audio.duration_us < target_us:
            warnings.append(
                f"audio is shorter than the master ({audio.duration_us}us < {target_us}us); "
                "the tail will be silent"
            )
    else:
        warnings.append("no audio asset supplied; the master will be silent")

    shots = tuple(
        ShotPlan(
            evidence_id=evidence_id,
            slot=TimeRangeUS(start_us=boundaries[index], end_us=boundaries[index + 1]),
            transition_in=_transition_for(template, index),
            motion=_motion_for(template, index),
            color=template.color.model_dump(mode="json"),
        )
        for index, evidence_id in enumerate(ordered_ids)
    )

    return SlideshowPlan(
        template_id=template.template_id,
        mode=input_payload.mode,
        target_duration_us=target_us,
        shots=shots,
        audio=audio_payload,
        render_profile={
            "fps": input_payload.fps or template.render.fps,
            "crf": template.render.crf,
            "preset": template.render.preset,
            "audio_bitrate": template.render.audio_bitrate,
            "resolution": input_payload.resolution,
            "aspect_ratio": input_payload.aspect_ratio,
            "loudness_lufs": template.audio.loudness_lufs,
            "intro_fade_seconds": template.transition.duration_seconds,
        },
        alignment_quality=alignment,
        tempo_source=tempo_source,
        beat_confidence=confidence,
        warnings=tuple(warnings),
    )
