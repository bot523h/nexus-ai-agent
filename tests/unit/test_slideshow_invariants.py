"""Slideshow pack invariants that were shipped but never exercised.

The Continuum coverage contract (95% per pack, DECISION_LOG D-0023) located
these lines as unexecuted.  Each test below pins a real, user-visible contract
of the slideshow pack — never a line for its own sake:

* evidence models reject malformed beat grids and inconsistent manual shots;
* a plan must tile its timeline exactly (no gap, overlap or short ending);
* an upscale records measured dimensions that must match the requested target;
* ``slideshow.compose`` with audio lays an audio track that never outlasts the
  video, and refuses audio evidence that is not a registered audio asset.
"""

from __future__ import annotations

import pytest
from command_authority import TEST_SERVICE_ACTOR, make_test_authorizer
from pydantic import ValidationError

from nexus_ai_agent.creative.packs.slideshow import (
    AssetEvidence,
    BeatGrid,
    ComposeInput,
    ShotPlan,
    SlideshowPlan,
    UpscaleInput,
    build_slideshow_registry,
)
from nexus_ai_agent.creative.packs.slideshow.operations import OPERATION_COMPOSE, OPERATION_SCAN
from nexus_ai_agent.creative.studio.bus import CommandBus
from nexus_ai_agent.creative.studio.models import (
    CommandValidationError,
    Playhead,
    Timeline,
    TimeRangeUS,
    new_project,
)

TARGET_US = 30_000_000


def _image(index: int) -> AssetEvidence:
    return AssetEvidence(
        evidence_id=f"img{index:02d}",
        path=f"/tmp/invariant_{index}.jpg",
        content_sha256="sha256:" + f"{index:02x}" * 32,
        media_kind="image",
        width=1920,
        height=1080,
    )


def _audio(duration_us: int, *, evidence_id: str = "aud00") -> AssetEvidence:
    return AssetEvidence(
        evidence_id=evidence_id,
        path=f"/tmp/{evidence_id}.wav",
        content_sha256="sha256:" + "ab" * 32,
        media_kind="audio",
        duration_us=duration_us,
    )


def _images(count: int) -> list[dict[str, object]]:
    return [_image(index).model_dump(mode="json") for index in range(count)]


def _bus() -> CommandBus:
    project = new_project(
        "proj",
        "slideshow",
        Timeline(timeline_id="tl", duration_us=0, playhead=Playhead(timecode_us=0)),
    )
    return CommandBus(
        project, registry=build_slideshow_registry(), authorizer=make_test_authorizer(project)
    )


def _command(operation: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol_version": "nagar.command.v1",
        "schema_version": 2,
        "command_id": f"cmd_{operation}",
        "actor": TEST_SERVICE_ACTOR.model_dump(mode="json"),
        "target": {"project_id": "proj"},
        "provenance": {"source": "service", "source_id": TEST_SERVICE_ACTOR.actor_id},
        "session_id": "invariants",
        "operation": operation,
        "input": payload,
        "confirmed": False,
    }


# ---------------------------------------------------------------------------
# evidence models
# ---------------------------------------------------------------------------


def test_beat_grid_rejects_negative_and_unordered_beats() -> None:
    with pytest.raises(ValidationError, match="non-negative"):
        BeatGrid(duration_us=10_000_000, beats_us=(-1, 500_000))
    with pytest.raises(ValidationError, match="ordered"):
        BeatGrid(duration_us=10_000_000, beats_us=(1_000_000, 500_000))
    assert BeatGrid(duration_us=10_000_000, beats_us=(0, 500_000)).beats_us == (0, 500_000)


def _manual(shots: list[dict[str, object]] | None, count: int = 2) -> dict[str, object]:
    payload: dict[str, object] = {
        "assets": _images(count),
        "target_duration_us": TARGET_US,
        "mode": "manual",
    }
    if shots is not None:
        payload["shots"] = shots
    return payload


@pytest.mark.parametrize(
    ("shots", "message"),
    [
        (None, "requires an explicit 'shots' list"),
        ([], "requires an explicit 'shots' list"),
        (
            [
                {"evidence_id": "img00", "duration_us": 15_000_000},
                {"evidence_id": "ghost", "duration_us": 15_000_000},
            ],
            "unknown evidence ids",
        ),
        ([{"evidence_id": "img00", "duration_us": 30_000_000}], "cover every image exactly once"),
        (
            [
                {"evidence_id": "img00", "duration_us": 15_000_000},
                {"evidence_id": "img00", "duration_us": 15_000_000},
            ],
            "must not reference an image twice",
        ),
    ],
)
def test_manual_mode_rejects_inconsistent_shot_lists(
    shots: list[dict[str, object]] | None, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ComposeInput.model_validate(_manual(shots))


def test_manual_mode_accepts_a_complete_unique_shot_list() -> None:
    payload = ComposeInput.model_validate(
        _manual(
            [
                {"evidence_id": "img01", "duration_us": 10_000_000},
                {"evidence_id": "img00", "duration_us": 20_000_000},
            ]
        )
    )
    assert [shot.evidence_id for shot in payload.shots or ()] == ["img01", "img00"]


def test_compose_input_rejects_image_evidence_in_the_audio_slot() -> None:
    payload = {
        "assets": _images(2),
        "target_duration_us": TARGET_US,
        "audio": _image(9).model_dump(mode="json"),
    }
    with pytest.raises(ValidationError, match="must be an audio asset"):
        ComposeInput.model_validate(payload)


# ---------------------------------------------------------------------------
# the plan tiles its timeline exactly
# ---------------------------------------------------------------------------


def _shot(evidence_id: str, start_us: int, end_us: int) -> ShotPlan:
    return ShotPlan(evidence_id=evidence_id, slot=TimeRangeUS(start_us=start_us, end_us=end_us))


def _plan(*shots: ShotPlan, target_us: int = 10_000_000) -> SlideshowPlan:
    return SlideshowPlan(
        template_id="travel_documentary",
        mode="auto",
        target_duration_us=target_us,
        shots=shots,
    )


def test_plan_accepts_contiguous_exact_tiling() -> None:
    plan = _plan(_shot("a", 0, 4_000_000), _shot("b", 4_000_000, 10_000_000))
    assert plan.shots[-1].slot.end_us == plan.target_duration_us


@pytest.mark.parametrize(
    ("shots", "message"),
    [
        ((("a", 1, 4_000_000), ("b", 4_000_000, 10_000_000)), "first shot must start at 0"),
        ((("a", 0, 4_000_000), ("b", 5_000_000, 10_000_000)), "without gaps or overlaps"),
        ((("a", 0, 6_000_000), ("b", 5_000_000, 10_000_000)), "without gaps or overlaps"),
        ((("a", 0, 4_000_000), ("b", 4_000_000, 9_000_000)), "must end at target_duration_us"),
    ],
)
def test_plan_rejects_gaps_overlaps_and_short_endings(
    shots: tuple[tuple[str, int, int], ...], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _plan(*(_shot(*shot) for shot in shots))


# ---------------------------------------------------------------------------
# upscale evidence must match the requested target
# ---------------------------------------------------------------------------


def _upscale(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source_asset_id": "img00",
        "source_sha256": "sha256:" + "00" * 32,
        "output_path": "/tmp/up.png",
        "output_sha256": "sha256:" + "11" * 32,
        "source_width": 960,
        "source_height": 540,
        "width": 1920,
        "height": 1080,
    }
    base.update(overrides)
    return base


def test_upscale_accepts_a_target_resolution_that_matches_the_measured_output() -> None:
    value = UpscaleInput.model_validate(_upscale(target_resolution="1920x1080"))
    assert (value.width, value.height) == (1920, 1080)


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_resolution": "3840x2160"},
        {"scale_factor": 1.5},
    ],
)
def test_upscale_rejects_measured_dimensions_that_miss_the_target(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="do not match"):
        UpscaleInput.model_validate(_upscale(**overrides))


def test_upscale_requires_exactly_one_target_kind() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        UpscaleInput.model_validate(_upscale())
    with pytest.raises(ValidationError, match="exactly one"):
        UpscaleInput.model_validate(_upscale(scale_factor=2.0, target_resolution="1920x1080"))


# ---------------------------------------------------------------------------
# compose with audio
# ---------------------------------------------------------------------------


def _scan(bus: CommandBus, assets: list[dict[str, object]]) -> None:
    bus.dispatch(_command(OPERATION_SCAN, {"assets": assets}))


@pytest.mark.parametrize(
    ("audio_us", "expected_span_us"),
    [(12_000_000, 12_000_000), (90_000_000, TARGET_US)],
)
def test_compose_with_audio_lays_a_track_that_never_outlasts_the_video(
    audio_us: int, expected_span_us: int
) -> None:
    bus = _bus()
    images = _images(3)
    audio = _audio(audio_us).model_dump(mode="json")
    _scan(bus, [*images, audio])
    result = bus.dispatch(
        _command(
            OPERATION_COMPOSE,
            {"assets": images, "target_duration_us": TARGET_US, "audio": audio},
        )
    )

    tracks = bus.project.timeline.tracks
    assert [track.kind for track in tracks] == ["video", "audio"]
    (audio_clip,) = tracks[1].clips
    assert audio_clip.media_ref.media_kind == "audio"
    (audio_record,) = [a for a in bus.project.assets if a.media_kind == "audio"]
    assert audio_clip.media_ref.asset_id == audio_record.asset_id
    assert audio_clip.media_ref.content_sha256 == audio_record.content_sha256
    assert audio_clip.timeline_range == TimeRangeUS(start_us=0, end_us=expected_span_us)
    assert audio_clip.source_range == audio_clip.timeline_range
    assert result.output["track_id"] == tracks[0].track_id


def test_compose_refuses_audio_evidence_registered_as_an_image() -> None:
    bus = _bus()
    images = _images(3)
    _scan(bus, images)
    # the audio slot references an id that the project knows as an image
    impostor = _audio(10_000_000, evidence_id="img00").model_dump(mode="json")
    with pytest.raises(CommandValidationError, match="expected 'audio'"):
        bus.dispatch(
            _command(
                OPERATION_COMPOSE,
                {"assets": images, "target_duration_us": TARGET_US, "audio": impostor},
            )
        )
