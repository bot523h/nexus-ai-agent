"""Project <-> OpenTimelineIO: the real library parses and emits (session 3).

No hand-rolled ``.otio`` JSON writer — every test below goes through
``opentimelineio`` itself (parse via ``otio_json`` adapter, emission
re-parsed by the same adapter).  The contract pinned here:

* clips → source/timeline windows in exact integer microseconds;
* gaps survive as placement spacing; transitions import as honestly-unmapped
  ``motion.add_transition`` layers (no silent dissolve);
* external references → asset ids with *unresolved* content identity
  (staging + hashing resolve it — the adapter never invents a ``sha256:``);
* track + clip markers survive with labels, colors, and rebased timecodes;
* Project → OTIO → Project round-trips exactly at integer frame rates;
* garbage in → typed :class:`OtioError`, never a guessed timeline.
"""

from __future__ import annotations

import pytest

otio = pytest.importorskip("opentimelineio")

from nexus_ai_agent.creative.interop.otio import (  # noqa: E402
    OtioError,
    otio_to_project,
    parse_otio_string,
    project_to_otio,
    rational_to_us,
    us_to_rational,
)


def _clip(name: str, src_start: int, src_dur: int, target: str, rate: float = 30.0) -> object:
    reference = otio.schema.ExternalReference(
        target_url=target,
        available_range=otio.opentime.TimeRange(
            otio.opentime.RationalTime(0, rate), otio.opentime.RationalTime(300, rate)
        ),
    )
    return otio.schema.Clip(
        name=name,
        source_range=otio.opentime.TimeRange(
            otio.opentime.RationalTime(src_start, rate),
            otio.opentime.RationalTime(src_dur, rate),
        ),
        media_reference=reference,
    )


def _two_clip_timeline() -> object:
    timeline = otio.schema.Timeline(name="demo")
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    track.append(_clip("a", 30, 60, "/media/a.mp4"))
    track.append(_clip("b", 0, 90, "/media/b.mp4"))
    track.markers.append(
        otio.schema.Marker(
            name="beat",
            marked_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(75, 30), otio.opentime.RationalTime(1, 30)
            ),
            color=otio.schema.MarkerColor.RED,
        )
    )
    timeline.tracks.append(track)
    return timeline


# ---------------------------------------------------------------------------
# time math: exact rationals, integer microseconds
# ---------------------------------------------------------------------------


def test_rational_to_us_is_exact_at_integer_rates() -> None:
    assert rational_to_us(30, 30.0) == 1_000_000
    assert rational_to_us(90, 30.0) == 3_000_000
    assert rational_to_us(1, 24.0) == 41667  # round-half-even of 41666.66...


def test_us_round_trip_is_exact_at_integer_rates() -> None:
    for micros in (0, 1_000_000, 2_500_000, 123_456_789):
        assert rational_to_us(us_to_rational(micros, 30.0), 30.0) == micros


def test_nonpositive_rate_is_refused() -> None:
    with pytest.raises(OtioError, match="positive"):
        rational_to_us(10, 0.0)


# ---------------------------------------------------------------------------
# import: clips, gaps, transitions, markers
# ---------------------------------------------------------------------------


def test_two_clips_import_with_exact_windows() -> None:
    project = otio_to_project(_two_clip_timeline(), project_id="p1")
    (track,) = project.timeline.tracks
    assert track.track_id == "V1"
    assert track.kind == "video"
    first, second = track.clips
    assert (first.source_range.start_us, first.source_range.end_us) == (1_000_000, 3_000_000)
    assert (first.timeline_range.start_us, first.timeline_range.end_us) == (0, 2_000_000)
    assert (second.source_range.start_us, second.source_range.end_us) == (0, 3_000_000)
    assert (second.timeline_range.start_us, second.timeline_range.end_us) == (2_000_000, 5_000_000)
    assert project.timeline.duration_us == 5_000_000


def test_external_references_yield_asset_ids_with_unresolved_identity() -> None:
    project = otio_to_project(_two_clip_timeline(), project_id="p1")
    clips = project.timeline.tracks[0].clips
    assert clips[0].media_ref.asset_id == "a"
    assert clips[0].media_ref.content_sha256 == "unresolved:external:/media/a.mp4"
    assert clips[1].media_ref.asset_id == "b"
    # Unresolved is honest: no invented "sha256:" digest anywhere.
    for clip in clips:
        assert not clip.media_ref.content_sha256.startswith("sha256:")


def test_track_markers_import_with_label_color_and_timecode() -> None:
    project = otio_to_project(_two_clip_timeline(), project_id="p1")
    (marker,) = project.timeline.markers
    assert (marker.timecode_us, marker.label, marker.color) == (2_500_000, "beat", "RED")


def test_clip_markers_are_rebased_onto_the_placement() -> None:
    timeline = otio.schema.Timeline(name="m")
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    clip = _clip("a", 30, 60, "/media/a.mp4")  # src 1s..3s, placed at 0s..2s
    clip.markers.append(
        otio.schema.Marker(
            name="srcmark",
            marked_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(45, 30), otio.opentime.RationalTime(1, 30)
            ),
            color=otio.schema.MarkerColor.GREEN,
        )
    )
    track.append(clip)
    timeline.tracks.append(track)
    project = otio_to_project(timeline, project_id="p1")
    (marker,) = project.timeline.markers
    # Source frame 1.5s, clip source starts at 1s, placed at 0s → 0.5s.
    assert (marker.timecode_us, marker.label) == (500_000, "srcmark")


def test_gaps_survive_as_placement_spacing() -> None:
    timeline = otio.schema.Timeline(name="g")
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    track.append(_clip("a", 0, 30, "/media/a.mp4"))
    track.append(
        otio.schema.Gap(
            name="hole",
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, 30), otio.opentime.RationalTime(30, 30)
            ),
        )
    )
    track.append(_clip("b", 0, 30, "/media/b.mp4"))
    timeline.tracks.append(track)
    project = otio_to_project(timeline, project_id="p1")
    first, second = project.timeline.tracks[0].clips
    assert (first.timeline_range.start_us, first.timeline_range.end_us) == (0, 1_000_000)
    assert (second.timeline_range.start_us, second.timeline_range.end_us) == (2_000_000, 3_000_000)


def test_transitions_import_as_honestly_unmapped_layers() -> None:
    timeline = otio.schema.Timeline(name="t")
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    track.append(_clip("a", 0, 60, "/media/a.mp4"))
    track.append(
        otio.schema.Transition(
            name="dissolve",
            transition_type=otio.schema.TransitionTypes.SMPTE_Dissolve,
            in_offset=otio.opentime.RationalTime(15, 30),
            out_offset=otio.opentime.RationalTime(15, 30),
        )
    )
    track.append(_clip("b", 0, 60, "/media/b.mp4"))
    timeline.tracks.append(track)
    project = otio_to_project(timeline, project_id="p1")
    first, second = project.timeline.tracks[0].clips
    assert [effect.operation for effect in first.effects] == ["motion.add_transition"]
    assert first.effects[0].parameters["duration_us"] == 1_000_000
    assert second.effects == []
    # ... and the plan compiler reports it unmapped (no silent dissolve).
    from nexus_ai_agent.creative.rendering.plan import compile_execution_plan

    plan = compile_execution_plan(project, track_id="V1")
    assert "a:motion.add_transition" in plan.unmapped_effects


def test_missing_reference_imports_with_missing_asset_id() -> None:
    timeline = otio.schema.Timeline(name="m")
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    track.append(
        otio.schema.Clip(
            name="ghost",
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, 30), otio.opentime.RationalTime(30, 30)
            ),
            media_reference=otio.schema.MissingReference(),
        )
    )
    timeline.tracks.append(track)
    project = otio_to_project(timeline, project_id="p1")
    (clip,) = project.timeline.tracks[0].clips
    assert clip.media_ref.asset_id == "missing:ghost"
    assert clip.media_ref.content_sha256 == "unresolved:missing-reference"


def test_audio_tracks_import_as_audio() -> None:
    timeline = otio.schema.Timeline(name="au")
    track = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)
    track.append(_clip("s", 0, 30, "/media/s.wav"))
    timeline.tracks.append(track)
    project = otio_to_project(timeline, project_id="p1")
    (track_out,) = project.timeline.tracks
    assert track_out.kind == "audio"
    assert track_out.clips[0].media_ref.media_kind == "audio"


# ---------------------------------------------------------------------------
# refusal paths
# ---------------------------------------------------------------------------


def test_garbage_string_is_refused() -> None:
    with pytest.raises(OtioError, match="not a parseable OTIO"):
        parse_otio_string("{ this is not otio")


def test_non_timeline_document_is_refused() -> None:
    clip_json = otio.adapters.write_to_string(_clip("a", 0, 30, "/x.mp4"), adapter_name="otio_json")
    with pytest.raises(OtioError, match="not a Timeline"):
        parse_otio_string(clip_json)


def test_missing_file_is_refused() -> None:
    from nexus_ai_agent.creative.interop.otio import parse_otio_file

    with pytest.raises(OtioError, match="no such OTIO file"):
        parse_otio_file("/tmp/definitely-not-here-xyz.otio")


# ---------------------------------------------------------------------------
# export + round-trip through the real library
# ---------------------------------------------------------------------------


def test_export_emits_real_otio_that_reparses() -> None:
    project = otio_to_project(_two_clip_timeline(), project_id="p1")
    emitted = project_to_otio(project)
    payload = otio.adapters.write_to_string(emitted, adapter_name="otio_json")
    assert "OTIO_SCHEMA" in payload
    reparsed = parse_otio_string(payload)
    assert reparsed.name == "demo"


def test_round_trip_preserves_windows_and_markers_exactly() -> None:
    project = otio_to_project(_two_clip_timeline(), project_id="p1")
    back = otio_to_project(project_to_otio(project), project_id="p2")
    first_clips = project.timeline.tracks[0].clips
    second_clips = back.timeline.tracks[0].clips
    assert len(first_clips) == len(second_clips) == 2
    for original, returned in zip(first_clips, second_clips, strict=True):
        assert returned.clip_id == original.clip_id
        assert returned.source_range == original.source_range
        assert returned.timeline_range == original.timeline_range
    assert [(m.timecode_us, m.label) for m in back.timeline.markers] == [
        (m.timecode_us, m.label) for m in project.timeline.markers
    ]
