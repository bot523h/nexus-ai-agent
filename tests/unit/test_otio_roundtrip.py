"""Nagar ↔ OTIO round-trip tests (task-165 — mission §11/§12 acceptance).

Sample timelines cover every interchange construct the mission names:

    clip · gap · multiple tracks · marker · trim · time range · metadata

Acceptance is **semantic** round-trip equality (not byte-for-byte), with every
intentional loss documented in ``LOSS_CONTRACT`` and pinned by explicit tests.
Two cross-checks run against the **real** ``opentimelineio`` library (the
task-121 acceptance style): they prove the emitted JSON is genuine OTIO and
that real-library output parses back into the normalized timeline.  When the
library is absent those two tests skip — skipped is NOT green (mission §22) and
is reported as UNVERIFIED.
"""

from __future__ import annotations

import json

import pytest

from nexus_ai_agent.creative.otio import (
    LOSS_CONTRACT,
    OtioConversionError,
    dumps_otio_document,
    frames_value_to_us,
    marker_otio_color,
    parse_otio_document,
    project_to_otio_document,
    us_to_frames_value,
)
from nexus_ai_agent.creative.studio.models import (
    AssetRecord,
    Clip,
    EffectLayerRef,
    Marker,
    MediaRef,
    Project,
    Timeline,
    TimeRangeUS,
    Track,
    new_project,
)

RATE = 24.0


def _sample_project() -> Project:
    """A deterministic fixture timeline exercising every construct."""
    hero = MediaRef(
        asset_id="hero",
        content_sha256="sha256:hero",
        media_kind="video",
        duration_us=8_000_000,
    )
    broll = MediaRef(
        asset_id="broll",
        content_sha256="sha256:broll",
        media_kind="video",
        duration_us=4_000_000,
        color_space="bt709",
    )
    music = MediaRef(
        asset_id="music",
        content_sha256="sha256:music",
        media_kind="audio",
        duration_us=12_000_000,
    )
    video_track = Track(
        track_id="video_01",
        name="Video 1",
        kind="video",
        clips=[
            # trim: source window 1s..5s of an 8s hero (non-zero source start)
            Clip(
                clip_id="clip_hero",
                media_ref=hero,
                source_range=TimeRangeUS(start_us=1_000_000, end_us=5_000_000),
                timeline_range=TimeRangeUS(start_us=2_000_000, end_us=6_000_000),
                effects=[
                    EffectLayerRef(
                        operation="color.adjust_exposure",
                        parameters={"exposure_ev": 0.5},
                        range=TimeRangeUS(start_us=0, end_us=4_000_000),
                    )
                ],
            ),
            # explicit gap 6s..8s is implicit between clips; broll starts at 8s
            Clip(
                clip_id="clip_broll",
                media_ref=broll,
                source_range=TimeRangeUS(start_us=0, end_us=2_000_000),
                timeline_range=TimeRangeUS(start_us=8_000_000, end_us=10_000_000),
            ),
        ],
    )
    audio_track = Track(
        track_id="audio_01",
        name="Audio 1",
        kind="audio",
        clips=[
            Clip(
                clip_id="clip_music",
                media_ref=music,
                source_range=TimeRangeUS(start_us=500_000, end_us=9_500_000),
                timeline_range=TimeRangeUS(start_us=0, end_us=9_000_000),
            )
        ],
    )
    image_track = Track(
        track_id="image_01",
        name="Stills",
        kind="image",
        clips=[],
    )
    timeline = Timeline(
        timeline_id="tl_main",
        duration_us=10_000_000,
        tracks=[video_track, audio_track, image_track],
        markers=[
            Marker(marker_id="mark_start", timecode_us=0, label="intro", color="RED"),
            Marker(marker_id="mark_beat", timecode_us=2_500_000, label="beat", color="blue-note"),
        ],
    )
    project = new_project("proj_rt", "Round Trip", timeline)
    return project.model_copy(
        update={
            "assets": [
                AssetRecord(
                    asset_id="hero",
                    media_kind="video",
                    content_sha256="sha256:hero",
                    duration_us=8_000_000,
                ),
                AssetRecord(
                    asset_id="broll",
                    media_kind="video",
                    content_sha256="sha256:broll",
                    duration_us=4_000_000,
                ),
                AssetRecord(
                    asset_id="music",
                    media_kind="audio",
                    content_sha256="sha256:music",
                    duration_us=12_000_000,
                ),
            ]
        }
    )


# ---------------------------------------------------------------------------
# semantic round-trip (with metadata: exact)
# ---------------------------------------------------------------------------


def test_round_trip_preserves_clips_gaps_tracks_markers_ranges_and_metadata() -> None:
    project = _sample_project()
    document = project_to_otio_document(project, frame_rate=RATE)
    state = parse_otio_document(document)

    assert state.timeline_id == "tl_main"
    assert state.playhead_us == 0
    assert [track.track_id for track in state.tracks] == ["video_01", "audio_01", "image_01"]
    assert [track.kind for track in state.tracks] == ["video", "audio", "image"]

    video = state.tracks[0]
    # leading gap(0..2s) preserves the clip's 2s placement, mid gap(6s..8s)
    assert len(video.children) == 4
    lead_gap, clip_hero, gap, clip_broll = video.children
    assert lead_gap.name == "gap"
    assert lead_gap.timeline_range.start_us == 0
    assert lead_gap.timeline_range.duration_us == 2_000_000
    assert clip_hero.clip_id == "clip_hero"
    assert clip_hero.source_range.start_us == 1_000_000  # trim survives
    assert clip_hero.source_range.duration_us == 4_000_000
    assert clip_hero.timeline_range.start_us == 2_000_000
    assert gap.name == "gap"
    assert gap.timeline_range.start_us == 6_000_000
    assert gap.timeline_range.duration_us == 2_000_000
    assert clip_broll.timeline_range.start_us == 8_000_000

    media = clip_hero.media_ref
    assert media is not None
    assert media.asset_id == "hero"
    assert media.content_sha256 == "sha256:hero"
    assert media.duration_us == 8_000_000
    assert clip_broll.media_ref is not None
    assert clip_broll.media_ref.color_space == "bt709"

    assert clip_hero.effects[0]["operation"] == "color.adjust_exposure"
    assert clip_hero.effects[0]["parameters"] == {"exposure_ev": 0.5}

    audio_clip = state.tracks[1].children[0]
    assert audio_clip.clip_id == "clip_music"
    assert audio_clip.timeline_range.duration_us == 9_000_000

    assert [marker.marker_id for marker in state.markers] == ["mark_start", "mark_beat"]
    assert state.markers[1].timecode_us == 2_500_000
    assert state.markers[1].color == "blue-note"  # original survives
    assert state.markers[1].otio_color == "WHITE"  # palette mapping is lossy-by-design
    assert state.markers[0].otio_color == "RED"


def test_round_trip_is_deterministic_json() -> None:
    project = _sample_project()
    first = dumps_otio_document(project_to_otio_document(project, frame_rate=RATE))
    second = dumps_otio_document(project_to_otio_document(project, frame_rate=RATE))
    assert first == second
    assert json.loads(first) == json.loads(second)


def test_include_markers_false_emits_no_marker_children() -> None:
    project = _sample_project()
    with_markers = project_to_otio_document(project, frame_rate=RATE, include_markers=True)
    without = project_to_otio_document(project, frame_rate=RATE, include_markers=False)
    assert with_markers["tracks"]["markers"]
    assert without["tracks"]["markers"] == []
    state = parse_otio_document(without)
    assert state.markers == ()


def test_multiple_tracks_and_time_ranges_survive_via_frame_view() -> None:
    project = _sample_project()
    document = project_to_otio_document(project, frame_rate=RATE)
    state = parse_otio_document(document)
    assert state.duration_us == 10_000_000
    # the OTIO-visible frame values are the quantised view of the same ranges
    track_children = document["tracks"]["children"][0]["children"]
    clip_node = track_children[1]  # [leading gap, clip_hero, gap, clip_broll]
    start_us, duration_us = (
        frames_value_to_us(
            clip_node["source_range"]["start_time"]["value"],
            clip_node["source_range"]["start_time"]["rate"],
        ),
        frames_value_to_us(
            clip_node["source_range"]["duration"]["value"],
            clip_node["source_range"]["duration"]["rate"],
        ),
    )
    assert abs(start_us - 1_000_000) <= 1_000_000 / RATE  # within one frame by contract
    assert abs(duration_us - 4_000_000) <= 1_000_000 / RATE


# ---------------------------------------------------------------------------
# documented losses (explicit tests — mission §12)
# ---------------------------------------------------------------------------


def test_loss_contract_is_non_empty_and_covers_markers_and_precision() -> None:
    whats = {entry.what for entry in LOSS_CONTRACT}
    assert "sub-frame microsecond precision" in whats
    assert "free-form marker colors" in whats
    assert "timeline.playhead" in whats
    assert all(entry.detail for entry in LOSS_CONTRACT)


def test_marker_color_mapping_is_deterministic_and_documented() -> None:
    assert marker_otio_color("RED") == "RED"
    assert marker_otio_color("blue-note") == "WHITE"
    assert marker_otio_color(None) == "WHITE"
    assert marker_otio_color("grey") == "WHITE"
    assert marker_otio_color("blue-note") == marker_otio_color("blue-note")


def test_stripped_metadata_degrades_to_the_quantized_frame_view() -> None:
    """A hostile NLE that drops metadata.nagar still yields valid structure."""
    project = _sample_project()
    document = project_to_otio_document(project, frame_rate=RATE)

    def strip(node: object) -> object:
        if isinstance(node, dict):
            cleaned = {
                key: strip(value)
                for key, value in node.items()
                if key != "metadata"
            }
            cleaned["metadata"] = {}
            return cleaned
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    stripped = strip(document)
    state = parse_otio_document(stripped)  # type: ignore[arg-type]
    video = state.tracks[0]
    assert len(video.children) == 4  # structure survives (leading gap included)
    clip_hero = video.children[1]
    # placement survives via gap assembly (better than feared) ...
    assert clip_hero.timeline_range.start_us == 2_000_000
    # ... but the metadata-carrying facts are the documented losses:
    assert clip_hero.clip_id == "clip_hero"  # id falls back to the name
    assert clip_hero.media_ref is not None
    assert clip_hero.media_ref.content_sha256 is None  # hash lost (documented)
    assert clip_hero.effects == ()  # effect layers lost (documented)
    assert state.markers[0].marker_id.startswith("marker_")  # id lost (documented)
    assert state.markers[0].color is None  # original color lost (documented)
    assert state.markers[0].marker_id.startswith("marker_")


def test_overlapping_track_clips_fail_closed() -> None:
    hero = MediaRef(
        asset_id="hero", content_sha256="sha256:hero", media_kind="video", duration_us=8_000_000
    )
    track = Track(
        track_id="video_01",
        name="Video 1",
        kind="video",
        clips=[
            Clip(
                clip_id="a",
                media_ref=hero,
                source_range=TimeRangeUS(start_us=0, end_us=3_000_000),
                timeline_range=TimeRangeUS(start_us=0, end_us=3_000_000),
            ),
            Clip(
                clip_id="b",
                media_ref=hero,
                source_range=TimeRangeUS(start_us=0, end_us=3_000_000),
                timeline_range=TimeRangeUS(start_us=2_000_000, end_us=5_000_000),
            ),
        ],
    )
    timeline = Timeline(timeline_id="tl", duration_us=5_000_000, tracks=[track])
    project = new_project("p", "Overlap", timeline)
    with pytest.raises(OtioConversionError):
        project_to_otio_document(project, frame_rate=RATE)


def test_microsecond_frame_algebra_round_trips_within_one_frame() -> None:
    for us in (0, 41_667, 1_000_000, 2_500_000, 9_999_999):
        value = us_to_frames_value(us, RATE)
        restored = frames_value_to_us(value, RATE)
        assert abs(restored - us) <= 1_000_000 / RATE
    with pytest.raises(OtioConversionError):
        us_to_frames_value(0, 0.0)


# ---------------------------------------------------------------------------
# real-library cross-checks (UNVERIFIED when opentimelineio is absent)
# ---------------------------------------------------------------------------


def test_real_opentimelineio_accepts_our_document() -> None:
    otio = pytest.importorskip("opentimelineio", reason="opentimelineio not installed (UNVERIFIED)")
    project = _sample_project()
    document = project_to_otio_document(project, frame_rate=RATE)
    timeline = otio.adapters.read_from_string(dumps_otio_document(document), "otio_json")

    assert timeline.name == "Round Trip"
    assert len(timeline.tracks) == 3
    video_track = timeline.tracks[0]
    kinds = [type(child).__name__ for child in video_track]
    assert kinds == ["Gap", "Clip", "Gap", "Clip"]
    clip = video_track[1]
    assert type(clip.media_reference).__name__ == "ExternalReference"
    assert clip.media_reference.target_url == "asset:hero"
    markers = list(timeline.tracks.markers)
    assert [marker.name for marker in markers] == ["intro", "beat"]
    assert markers[0].color == "RED"
    assert markers[1].metadata["nagar"]["color"] == "blue-note"


def test_real_opentimelineio_output_parses_back_into_the_normalized_timeline() -> None:
    otio = pytest.importorskip("opentimelineio", reason="opentimelineio not installed (UNVERIFIED)")
    project = _sample_project()
    document = project_to_otio_document(project, frame_rate=RATE)

    # serialize through the REAL library, then parse its output back
    real_text = otio.adapters.write_to_string(
        otio.adapters.read_from_string(dumps_otio_document(document), "otio_json")
    )
    state = parse_otio_document(json.loads(real_text))

    assert [track.track_id for track in state.tracks] == ["video_01", "audio_01", "image_01"]
    video = state.tracks[0]
    assert [getattr(child, "clip_id", child.name) for child in video.children] == [
        "gap",
        "clip_hero",
        "gap",
        "clip_broll",
    ]
    assert video.children[1].source_range.start_us == 1_000_000
    assert video.children[1].timeline_range.start_us == 2_000_000
    assert video.children[3].timeline_range.start_us == 8_000_000
    assert state.markers[1].timecode_us == 2_500_000
    assert state.markers[1].color == "blue-note"
    clip_hero = video.children[1]
    assert clip_hero.media_ref is not None
    assert clip_hero.media_ref.asset_id == "hero"
    assert clip_hero.media_ref.content_sha256 == "sha256:hero"
    assert clip_hero.effects[0]["operation"] == "color.adjust_exposure"
