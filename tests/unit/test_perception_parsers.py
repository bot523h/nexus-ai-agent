"""Pure parsers for FFmpeg diagnostics: captured real output, plus hostile text."""

from __future__ import annotations

import math

import pytest

from nexus_ai_agent.creative.perception import parsers
from nexus_ai_agent.creative.perception.parsers import ParseError

SEC = 1_000_000

# Captured from FFmpeg 6.1.1 (apt) and 7.0.2 (imageio-ffmpeg wheel).
SILENCE_REAL = """\
[silencedetect @ 0x55a7ff5fca00] silence_start: 1.99998
[silencedetect @ 0x55a7ff5fca00] silence_end: 3.50002 | silence_duration: 1.50005
size=N/A time=00:00:04.96 bitrate=N/A speed= 398x
"""

SCENE_REAL = """\
[Parsed_showinfo_1 @ 0x1] n:   0 pts:  10240 pts_time:1       duration:   1024 fmt:yuv420p
[Parsed_showinfo_1 @ 0x1] n:   1 pts:  20480 pts_time:2       duration:   1024 fmt:yuv420p
"""

LOUDNESS_REAL = """\
[Parsed_ebur128_0 @ 0x557f966cfb00] Summary:

  Integrated loudness:
    I:         -22.0 LUFS
    Threshold: -32.1 LUFS

  Loudness range:
    LRA:         1.0 LU
    Threshold: -44.4 LUFS
    LRA low:   -24.7 LUFS
    LRA high:  -23.7 LUFS

  True peak:
    Peak:      -18.1 dBFS
"""

PROBE_VIDEO = (
    "  Duration: 00:00:03.00, start: 0.000000, bitrate: 6 kb/s\n"
    "  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), "
    "160x90 [SAR 1:1 DAR 16:9], 3 kb/s, 10 fps, 10 tbr, 10240 tbn (default)\n"
)

PROBE_AUDIO = """\
  Duration: 00:00:05.00, bitrate: 705 kb/s
  Stream #0:0: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 44100 Hz, 1 channels, s16, 705 kb/s
"""


# -- silences -----------------------------------------------------------


def test_silence_pair_becomes_one_span() -> None:
    assert parsers.parse_silences(SILENCE_REAL.splitlines(), 5 * SEC) == [(1_999_980, 3_500_020)]


def test_trailing_silence_is_closed_at_the_duration() -> None:
    lines = ["[silencedetect @ 0x1] silence_start: 8.5"]
    assert parsers.parse_silences(lines, 10 * SEC) == [(8_500_000, 10 * SEC)]


def test_negative_start_and_overshooting_end_are_clamped() -> None:
    lines = [
        "[silencedetect @ 0x1] silence_start: -0.0213",
        "[silencedetect @ 0x1] silence_end: 1.0 | silence_duration: 1.02",
        "[silencedetect @ 0x1] silence_start: 9.0",
        "[silencedetect @ 0x1] silence_end: 10.04 | silence_duration: 1.04",
    ]
    assert parsers.parse_silences(lines, 10 * SEC) == [(0, SEC), (9 * SEC, 10 * SEC)]


def test_overlapping_and_empty_spans_never_escape() -> None:
    lines = [
        "silence_start: 1.0",
        "silence_end: 3.0",
        "silence_start: 2.0",  # overlaps the previous span
        "silence_end: 4.0",
        "silence_start: 5.0",
        "silence_end: 5.0",  # empty
    ]
    assert parsers.parse_silences(lines, 10 * SEC) == [(SEC, 3 * SEC), (3 * SEC, 4 * SEC)]


def test_orphan_silence_end_is_ignored_and_noise_is_skipped() -> None:
    lines = ["silence_end: 2.0 | silence_duration: 1.0", "random banner", "", "silence_start:"]
    assert parsers.parse_silences(lines, 10 * SEC) == []


def test_no_silence_lines_means_no_spans() -> None:
    assert parsers.parse_silences([], 10 * SEC) == []


# -- scene cuts ---------------------------------------------------------


def test_scene_cuts_are_extracted_sorted_and_unique() -> None:
    assert parsers.parse_scene_cuts(SCENE_REAL.splitlines(), 3 * SEC) == [SEC, 2 * SEC]
    doubled = SCENE_REAL.splitlines() * 2
    assert parsers.parse_scene_cuts(doubled, 3 * SEC) == [SEC, 2 * SEC]


def test_scene_cuts_outside_the_media_or_without_showinfo_are_dropped() -> None:
    lines = [
        "[Parsed_showinfo_1 @ 0x1] n: 0 pts: 1 pts_time:-0.5 duration: 1",
        "[Parsed_showinfo_1 @ 0x1] n: 1 pts: 1 pts_time:99 duration: 1",
        "[Parsed_showinfo_1 @ 0x1] n: 2 pts: 1 duration: 1",
        "frame pts_time:1.5 but not from showinfo",
        "[Parsed_showinfo_1 @ 0x1] n: 3 pts: 1 pts_time:1.5e0 duration: 1",
    ]
    assert parsers.parse_scene_cuts(lines, 3 * SEC) == [1_500_000]


# -- loudness -----------------------------------------------------------


def test_real_summary_is_parsed() -> None:
    result = parsers.parse_loudness(LOUDNESS_REAL)
    assert result == parsers.Loudness(-22.0, 1.0, -18.1)


def test_only_the_final_summary_counts() -> None:
    text = "I:  -50.0 LUFS\nSummary:\n    I:  -40.0 LUFS\n" + LOUDNESS_REAL
    assert parsers.parse_loudness(text).integrated_lufs == -22.0


def test_minus_infinity_means_not_measurable() -> None:
    text = """\
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -inf LUFS

  Loudness range:
    LRA:         0.0 LU

  True peak:
    Peak:      -inf dBFS
"""
    assert parsers.parse_loudness(text) == parsers.Loudness(None, 0.0, None)


def test_the_absolute_gate_floor_is_also_not_measurable() -> None:
    """FFmpeg 7 prints the gate (-70.0) where FFmpeg 6 prints -inf."""
    text = LOUDNESS_REAL.replace("-22.0 LUFS", "-70.0 LUFS", 1)
    assert parsers.parse_loudness(text).integrated_lufs is None
    assert (
        parsers.parse_loudness(LOUDNESS_REAL.replace("-22.0", "-69.9", 1)).integrated_lufs == -69.9
    )


@pytest.mark.parametrize(
    "text",
    ["", "no summary at all", "Summary:\n  nothing useful here\n"],
)
def test_incoherent_loudness_text_is_an_error(text: str) -> None:
    with pytest.raises(ParseError):
        parsers.parse_loudness(text)


# -- probe --------------------------------------------------------------


def test_video_probe() -> None:
    probe = parsers.parse_probe(PROBE_VIDEO.splitlines())
    assert (probe.duration_us, probe.has_video, probe.has_audio) == (3 * SEC, True, False)
    # the hex codec tag "0x31637661" must never be mistaken for a resolution
    assert (probe.width, probe.height) == (160, 90)


def test_audio_probe() -> None:
    probe = parsers.parse_probe(PROBE_AUDIO.splitlines())
    assert (probe.duration_us, probe.has_video, probe.has_audio) == (5 * SEC, False, True)
    assert probe.sample_rate == 44100 and probe.width is None


def test_combined_probe_and_hms_arithmetic() -> None:
    lines = [
        "  Duration: 01:02:03.50, start: 0.0, bitrate: 1 kb/s",
        PROBE_VIDEO.splitlines()[1],
        PROBE_AUDIO.splitlines()[1],
    ]
    probe = parsers.parse_probe(lines)
    assert probe.duration_us == (3600 + 120 + 3) * SEC + 500_000
    assert probe.has_video and probe.has_audio


def test_probe_without_duration_is_an_error() -> None:
    with pytest.raises(ParseError, match="Duration"):
        parsers.parse_probe(["Stream #0:0: Audio: pcm_s16le, 44100 Hz, mono, s16"])


def test_stream_lines_that_do_not_parse_do_not_claim_presence() -> None:
    lines = ["  Duration: 00:00:01.00, start: 0", "  Stream #0:0: Video: weird", "Audio: nope"]
    probe = parsers.parse_probe(lines)
    assert not probe.has_video and not probe.has_audio


# -- helpers ------------------------------------------------------------


def test_to_us_rounds_and_rejects_non_finite() -> None:
    assert parsers.to_us(1.9999996) == 2 * SEC
    assert parsers.to_us(0.0000004) == 0
    for bad in (math.inf, -math.inf, math.nan):
        with pytest.raises(ParseError):
            parsers.to_us(bad)


def test_version_parsing() -> None:
    assert parsers.parse_version(["ffmpeg version 6.1.1-3ubuntu5 Copyright"]) == "6.1.1-3ubuntu5"
    assert parsers.parse_version(["ffmpeg version 7.0.2-static https://x"]) == "7.0.2-static"
    assert parsers.parse_version(["something else"]) == "unknown"


def test_line_filters_select_only_what_parsers_need() -> None:
    assert parsers.SILENCE_LINE.search("[silencedetect @ 0x1] silence_start: 1")
    assert not parsers.SILENCE_LINE.search("frame=  1 fps=0")
    assert parsers.SCENE_LINE.search(SCENE_REAL.splitlines()[0])
    assert not parsers.SCENE_LINE.search("pts_time:3 but no showinfo")
    assert parsers.PROBE_LINE.match("  Duration: 00:00:01.00")
    assert parsers.PROBE_LINE.match("  Stream #0:0: Audio: x")
    assert not parsers.PROBE_LINE.match("Metadata:")
    kept = [ln for ln in LOUDNESS_REAL.splitlines() if parsers.LOUDNESS_SUMMARY_LINE.search(ln)]
    assert parsers.parse_loudness("\n".join(kept)).integrated_lufs == -22.0
    assert parsers.VERSION_LINE.match("ffmpeg version 6")
