"""Label-dependent FFmpeg parsing, hardened against version drift.

The `probe()` channel-count bug (FFmpeg 6.1.1's ``N channels``) was one instance
of a class: **the engine read FFmpeg's human-readable stderr as if its format
were a contract.** It is not. This file pins every remaining label-dependent
parse against the shapes that have actually shipped, and asserts that an
unrecognisable shape *refuses* rather than becoming a plausible number.

The failure mode this prevents is silent and specific: a regex that misses
returns a wrong loudness, and a wrong loudness is what lets an artifact report
that it hit a target it missed.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
    _DURATION_RE,
    _SUMMARY_I_RE,
    _SUMMARY_LRA_RE,
    _SUMMARY_PEAK_RE,
    _SUMMARY_THRESHOLD_RE,
    AudioMeasurementError,
    _summary_value,
)

# --------------------------------------------------------------------------- #
# The integrated-loudness line
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # FFmpeg 4.x / 6.x / 9.x: column-padded after the label.
        ("    I:         -14.2 LUFS", -14.2),
        # Minimum spacing, which a strict `": "` pattern breaks on.
        ("I: -14.2 LUFS", -14.2),
        # No space before the unit.
        ("I: -14.2LUFS", -14.2),
        # Case variance.
        ("i: -14.2 lufs", -14.2),
        ("I: -14.2 Lufs", -14.2),
        # Tabs instead of spaces.
        ("\tI:\t-14.2\tLUFS", -14.2),
        # Explicit positive sign.
        ("I: +3.5 LUFS", 3.5),
        # Scientific notation (some builds print tiny values this way).
        ("I: -1.5e1 LUFS", -15.0),
        # -inf: gating never opened. This is silence, not a measurement.
        ("I: -inf LUFS", None),
        ("I: -INF lufs", None),
    ],
)
def test_the_integrated_loudness_line_parses_across_formats(
    line: str, expected: float | None
) -> None:
    assert _summary_value(line, "I", _SUMMARY_I_RE) == expected


def test_a_missing_integrated_line_is_none_not_zero() -> None:
    """Zero would be a valid loudness. Absence must not become a number."""
    assert _summary_value("Loudness range:\n    LRA: 0.0 LU", "I", _SUMMARY_I_RE) is None
    assert _summary_value("", "I", _SUMMARY_I_RE) is None


# --------------------------------------------------------------------------- #
# The loudness-range line
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("    LRA:         0.0 LU", 0.0),
        ("LRA: 19.99999 LU", 19.99999),
        ("LRA:     -inf LU", None),
        ("lra: 7.5 lu", 7.5),
    ],
)
def test_the_loudness_range_line_parses(line: str, expected: float | None) -> None:
    assert _summary_value(line, "LRA", _SUMMARY_LRA_RE) == expected


# --------------------------------------------------------------------------- #
# The true-peak line — note the label is not stable either
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # The summary's label is "Peak".
        ("    Peak:      -10.5 dBFS", -10.5),
        # The live progress line uses "TPK"; some builds reuse it in the summary.
        ("TPK: -10.5 dBFS", -10.5),
        # True-peak-dBTP spelling appears in newer builds.
        ("Peak: -1.0 dBTP", -1.0),
        ("Peak: -inf dBFS", None),
    ],
)
def test_the_true_peak_line_accepts_both_labels(line: str, expected: float | None) -> None:
    assert _summary_value(line, "Peak", _SUMMARY_PEAK_RE) == expected


# --------------------------------------------------------------------------- #
# Unit confusion must refuse
# --------------------------------------------------------------------------- #


def test_a_wrong_unit_for_the_label_refuses_rather_than_guessing() -> None:
    """``LRA: 0.0 LUFS`` is not a loudness range in LU.

    The dangerous case is ``Threshold``: it appears under *both* the integrated
    and the LRA section. A unit-blind regex reading the wrong block yields a
    plausible number under the wrong name, which looks like a measurement.
    """
    with pytest.raises(AudioMeasurementError, match="expected one of"):
        _summary_value("LRA: 0.0 LUFS", "LRA", _SUMMARY_LRA_RE)


def test_every_label_has_a_declared_unit_set() -> None:
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import _SUMMARY_UNITS

    assert set(_SUMMARY_UNITS) == {"I", "LRA", "Peak", "Threshold"}
    assert "lufs" in _SUMMARY_UNITS["I"]
    assert "lu" in _SUMMARY_UNITS["LRA"]
    # True peak has been printed as both dBFS and dBTP.
    assert {"dbfs", "dbtp"} <= _SUMMARY_UNITS["Peak"]


# --------------------------------------------------------------------------- #
# Duration
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("line", "seconds"),
    [
        ("  Duration: 00:00:02.00, start: 0.000000, bitrate: 768 kb/s", 2.0),
        ("Duration: 00:01:30.50, bitrate: 128 kb/s", 90.5),
        ("Duration: 1:02:03.25", 3723.25),
        ("duration: 00:00:07.5", 7.5),
        ("Duration:00:00:01.00", 1.0),
    ],
)
def test_the_duration_line_parses_across_formats(line: str, seconds: float) -> None:
    match = _DURATION_RE.search(line)
    assert match is not None, f"did not parse: {line!r}"
    total = int(match.group("h")) * 3600 + int(match.group("m")) * 60 + float(match.group("s"))
    assert total == pytest.approx(seconds)


def test_a_stream_with_no_duration_is_absent_not_zero() -> None:
    """A raw stream legitimately has no duration; the probe records ``None``."""
    assert _DURATION_RE.search("Duration: N/A, bitrate: N/A") is None
    assert _DURATION_RE.search("Input #0, s16le, from 'pipe:':") is None


# --------------------------------------------------------------------------- #
# The threshold line appears twice — the section split is what disambiguates
# --------------------------------------------------------------------------- #


def test_the_threshold_is_read_from_its_own_section() -> None:
    """Both sections print a ``Threshold``; only one is the integrated gate."""
    summary = (
        "\n  Integrated loudness:\n"
        "    I:         -14.2 LUFS\n"
        "    Threshold: -24.2 LUFS\n"
        "\n  Loudness range:\n"
        "    LRA:         3.0 LU\n"
        "    Threshold:   0.0 LUFS\n"
    )
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import _section

    integrated_block = _section(summary, "Integrated loudness:", "Loudness range:")
    lra_block = _section(summary, "Loudness range:", None)
    assert _summary_value(integrated_block, "Threshold", _SUMMARY_THRESHOLD_RE) == -24.2
    assert _summary_value(lra_block, "Threshold", _SUMMARY_THRESHOLD_RE) == 0.0
