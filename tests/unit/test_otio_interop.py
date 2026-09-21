"""Real OpenTimelineIO interop tests for ``delivery.export_otio`` (task-110).

Until now the exported document was only checked against a hand-written
expectation of the schema. These tests load it with the **actual**
OpenTimelineIO library (ASWF, PyPI ``opentimelineio``) and assert that a real
NLE would see the same timeline: clip order, media references, frame rate and
durations.

The first bug this caught: the pack emitted ``media_url`` on each clip, which is
not an OTIO schema field. The real reader silently drops unknown keys, so every
clip came back as ``MissingReference`` — i.e. an export that looks correct in a
diff but opens as 100% offline media in DaVinci/Premiere/Kdenlive. Clips now
carry ``ExternalReference.1``.

Requires the dev extra (``pip install '.[otio]'`` or ``--group dev``); the
module skips in a typed way when it is absent.
"""

from __future__ import annotations

import json

import pytest

otio = pytest.importorskip("opentimelineio", reason="requires the [otio] dev extra")

from nexus_ai_agent.creative.packs.delivery.operations import (  # noqa: E402
    build_delivery_registry,
)
from nexus_ai_agent.creative.studio.bus import CommandBus  # noqa: E402
from nexus_ai_agent.creative.studio.models import (  # noqa: E402
    AssetRecord,
    Project,
    Timeline,
    TypedCommand,
    new_project,
)

VIDEO_ASSETS = {
    "clip_master_01": 5_000_000,
    "clip_master_02": 7_500_000,
}
AUDIO_ASSETS = {"audio_voice_01": 10_000_000}


def _project_with_media() -> Project:
    """Project with two video clips and one audio clip (deterministic order)."""
    assets = [
        AssetRecord(
            asset_id=asset_id,
            media_kind="video",
            content_sha256=f"sha256:{asset_id}",
            duration_us=duration_us,
        )
        for asset_id, duration_us in VIDEO_ASSETS.items()
    ] + [
        AssetRecord(
            asset_id=asset_id,
            media_kind="audio",
            content_sha256=f"sha256:{asset_id}",
            duration_us=duration_us,
        )
        for asset_id, duration_us in AUDIO_ASSETS.items()
    ]
    timeline = Timeline(timeline_id="tl_interop", duration_us=10_000_000)
    project = new_project("p_otio_interop", "OTIO Interop Project", timeline)
    return project.model_copy(update={"assets": assets})


def _export(frame_rate: float) -> str:
    """Run ``delivery.export_otio`` and return the raw OTIO JSON string."""
    bus = CommandBus(_project_with_media(), registry=build_delivery_registry())
    result = bus.dispatch(
        TypedCommand(
            command_id="cmd_otio_interop",
            operation="delivery.export_otio",
            input={"timeline_id": "tl_interop", "frame_rate": frame_rate},
        )
    )
    assert result.status == "applied"
    return str(result.output["otio_json"])


def _read(document: str) -> object:
    """Parse *document* with the real OTIO JSON adapter."""
    return otio.adapters.read_from_string(document, "otio_json")


def test_export_opens_with_the_real_otio_reader() -> None:
    timeline = _read(_export(24.0))

    assert timeline.schema_name() == "Timeline"
    assert timeline.name == "OTIO Interop Project"
    kinds = [track.kind for track in timeline.tracks]
    assert list(kinds) == ["Video", "Audio"]


def test_clip_order_and_track_membership_survive_the_round_trip() -> None:
    timeline = _read(_export(24.0))
    video, audio = timeline.tracks[0], timeline.tracks[1]

    assert [clip.name for clip in video.find_clips()] == list(VIDEO_ASSETS)
    assert [clip.name for clip in audio.find_clips()] == list(AUDIO_ASSETS)


def test_every_clip_carries_a_real_media_reference() -> None:
    """Regression: ``media_url`` was dropped by the reader as an unknown key."""
    timeline = _read(_export(24.0))

    references = {
        clip.name: clip.media_reference for track in timeline.tracks for clip in track.find_clips()
    }
    assert len(references) == len(VIDEO_ASSETS) + len(AUDIO_ASSETS)
    for asset_id, reference in references.items():
        assert isinstance(reference, otio.schema.ExternalReference), (
            f"{asset_id} came back as {type(reference).__name__}: the export lost "
            "its media reference and would open as offline media in every NLE"
        )
        assert reference.target_url == f"asset:{asset_id}"


def test_media_url_is_not_emitted_as_an_unknown_schema_key() -> None:
    document = json.loads(_export(24.0))
    clips = [clip for track in document["tracks"]["children"] for clip in track["children"]]
    assert clips
    for clip in clips:
        assert "media_url" not in clip
        assert clip["media_reference"]["OTIO_SCHEMA"] == "ExternalReference.1"


@pytest.mark.parametrize("frame_rate", [24.0, 25.0, 30.0, 23.976])
def test_frame_rate_and_durations_are_preserved(frame_rate: float) -> None:
    timeline = _read(_export(frame_rate))
    video = timeline.tracks[0]

    clips = list(video.find_clips())
    for clip in clips:
        duration = clip.source_range.duration
        assert duration.rate == pytest.approx(frame_rate), "frame rate was not preserved"
        expected_frames = int((VIDEO_ASSETS[clip.name] / 1_000_000.0) * frame_rate)
        assert float(duration.value) == float(expected_frames)
        assert float(clip.source_range.start_time.value) == 0.0
        assert clip.source_range.start_time.rate == pytest.approx(frame_rate)

    assert timeline.global_start_time.rate == pytest.approx(frame_rate)
    assert float(timeline.global_start_time.value) == 0.0


def test_timeline_duration_equals_the_sum_of_video_clips() -> None:
    frame_rate = 24.0
    timeline = _read(_export(frame_rate))
    video = timeline.tracks[0]

    expected = sum(int((us / 1_000_000.0) * frame_rate) for us in VIDEO_ASSETS.values())
    assert float(video.duration().value) == float(expected)


def test_round_trip_through_the_real_writer_is_idempotent() -> None:
    """Write the parsed timeline back out and read it again: nothing drifts."""
    first = _read(_export(23.976))
    rewritten = otio.adapters.write_to_string(first, "otio_json")
    second = _read(rewritten)

    assert second.name == first.name
    assert [clip.name for clip in second.tracks[0].find_clips()] == [
        clip.name for clip in first.tracks[0].find_clips()
    ]
    assert second.tracks[0].find_clips()[0].source_range.duration.rate == pytest.approx(23.976)
    assert second.metadata["nagar_project_id"] == "p_otio_interop"


def test_legacy_media_url_shape_is_detected_as_missing_media() -> None:
    """Negative control: proves the reader silently drops unknown clip keys.

    Without this the round-trip assertions above could pass vacuously — this
    shows what the pre-fix export actually looked like to a real NLE.
    """
    legacy = json.loads(_export(24.0))
    clip = legacy["tracks"]["children"][0]["children"][0]
    del clip["media_reference"]
    clip["media_url"] = "asset:clip_master_01"

    timeline = _read(json.dumps(legacy))
    parsed = timeline.tracks[0].find_clips()[0]
    assert isinstance(parsed.media_reference, otio.schema.MissingReference)
