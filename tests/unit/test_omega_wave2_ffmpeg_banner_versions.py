"""FFmpeg banner parsing across versions — the channel-count portability defect.

`probe()` reads the channel count from ``ffmpeg -i`` stderr. The token in that
banner is **not stable across FFmpeg releases**:

* 9.0.2 prints ``48000 Hz, mono, s16``
* 6.1.1 prints ``48000 Hz, 1 channels, s16``

The original parser was ``1 if layout.startswith("mono") else 2 if
layout.startswith("stereo") else None``, which returns ``None`` on 6.1.1. A
``None`` channel count makes `probe()` fail closed, so every artifact is
rejected as "carries no decodable audio stream" — nine live failures on a 6.1.1
host, invisible on a 9.x sandbox.

These tests feed **verbatim banner lines** from each version. They never invoke
``ffprobe``: decision r5 forbids a second required binary, so the fix has to live
in the single-binary path.

This is evidence for ``creative.packs.audio``: the pack's handlers reach the
stream through this engine, and a ``None`` channel count here is what turned
into "carries no decodable audio stream" at ``execution.py:424`` for every
request on a 6.1.1 host. The parser is the pack's ground truth about a stream,
so it is measured as pack evidence rather than exempted as host-layer.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
    _AUDIO_STREAM_RE,
    parse_audio_stream_channels,
    probe_banner_channels,
)

# --------------------------------------------------------------------------- #
# The pure token parser
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        # -- FFmpeg 9.x / 8.x style: the layout has a name ------------------
        ("mono", 1),
        ("stereo", 2),
        ("downmix", 2),
        ("quad", 4),
        ("quadraphonic", 4),
        ("hexagonal", 6),
        ("octagonal", 8),
        # -- FFmpeg 5.x / 6.x style: the layout is "N channels" -------------
        ("1 channels", 1),
        ("2 channels", 2),
        ("6 channels", 6),
        ("1 channel", 1),
        ("2 channel", 2),
        ("1 ch", 1),
        ("2 ch", 2),
        ("8 ch", 8),
        # -- Surround notation: the components sum --------------------------
        ("2.1", 3),
        ("4.0", 4),
        ("5.1", 6),
        ("5.1(side)", 6),
        ("6.0", 6),
        ("7.1", 8),
        ("7.1.4", 12),
        ("3.0", 3),
        ("3.0(back)", 3),
        # -- Case and whitespace variance -----------------------------------
        ("MONO", 1),
        ("Stereo", 2),
        ("  5.1  ", 6),
        ("  2 channels  ", 2),
        # -- Unrecognised fails closed, never guesses -----------------------
        ("", None),
        ("   ", None),
        ("unknown(6ch)", None),
        ("weird", None),
        ("0 channels", None),
        ("channels", None),
    ],
)
def test_the_channel_count_is_resolved_from_every_known_token(
    token: str, expected: int | None
) -> None:
    assert parse_audio_stream_channels(token) == expected


def test_a_guessed_channel_count_is_never_returned() -> None:
    """Failing closed is the contract: a wrong count corrupts the loudness sum.

    If an unknown layout returned a default of 2 (the tempting choice, since
    most masters are stereo), a 5.1 file would be metered as stereo and its
    integrated loudness would be wrong by the surround channel weights.
    """
    for token in ("atmos", "ambisonic", "12.4.1.9", "?", "n/a", None):
        assert parse_audio_stream_channels(token) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Verbatim banner lines, end to end through the regex
# --------------------------------------------------------------------------- #

BANNERS = {
    # FFmpeg 4.4 — same "N channels" shape as 6.x.
    "4.4 mono": (
        "  Stream #0:0: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 48000 Hz, "
        "1 channels, s16, 768 kb/s",
        1,
    ),
    "4.4 stereo": (
        "  Stream #0:0: Audio: pcm_f32le ([3][0][0][0] / 0x0003), 48000 Hz, "
        "2 channels, flt, 3072 kb/s",
        2,
    ),
    # FFmpeg 6.1.1 — the exact lines the live audit reported.
    "6.1.1 mono": (
        "  Stream #0:0: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 48000 Hz, "
        "1 channels, s16, 768 kb/s",
        1,
    ),
    "6.1.1 stereo": (
        "  Stream #0:0: Audio: pcm_f32le ([3][0][0][0] / 0x0003), 48000 Hz, "
        "2 channels, flt, 3072 kb/s",
        2,
    ),
    # FFmpeg 7.1 — surround notation with a parenthetical tag.
    "7.1 surround": (
        "  Stream #0:0: Audio: pcm_s24le ([1][0][0][0] / 0x0001), 48000 Hz, "
        "5.1(side), s32 (24 bit), 6912 kb/s",
        6,
    ),
    # FFmpeg 9.0.2 — the sandbox's version, named layouts.
    "9.0.2 mono": (
        "  Stream #0:0: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 48000 Hz, mono, s16, 768 kb/s",
        1,
    ),
    "9.0.2 stereo": (
        "  Stream #0:0: Audio: pcm_f32le, 48000 Hz, stereo, fltp, 3072 kb/s",
        2,
    ),
}


@pytest.mark.parametrize(("label", "payload"), sorted(BANNERS.items()))
def test_a_verbatim_banner_resolves_to_its_channel_count(
    label: str, payload: tuple[str, int]
) -> None:
    """The whole parse path, not just the helper."""
    line, expected = payload
    resolved = probe_banner_channels(line)
    assert resolved is not None, f"{label}: the banner did not parse at all"
    channels, rate, codec, layout = resolved
    assert channels == expected, f"{label}: got {channels}, expected {expected} (layout {layout!r})"
    assert rate == 48_000
    assert codec


def test_a_6_1_1_banner_is_no_longer_none() -> None:
    """The exact regression, stated as its own assertion.

    Before the fix this returned ``None`` for both lines, which is what made
    ``probe()`` fail closed with "carries no decodable audio stream".
    """
    one = probe_banner_channels(BANNERS["6.1.1 mono"][0])
    two = probe_banner_channels(BANNERS["6.1.1 stereo"][0])
    assert one is not None and one[0] == 1
    assert two is not None and two[0] == 2


def test_the_stereo_layout_name_is_preserved_verbatim() -> None:
    """``channel_layout`` is evidence: it must report what FFmpeg printed.

    The count is derived, the layout is recorded. Rewriting ``1 channels`` to
    ``mono`` would make the evidence claim something FFmpeg never said.
    """
    resolved = probe_banner_channels(BANNERS["6.1.1 stereo"][0])
    assert resolved is not None
    assert resolved[3] == "2 channels"


def test_no_stream_line_parses_to_nothing() -> None:
    assert probe_banner_channels("no audio here") is None
    assert _AUDIO_STREAM_RE.search("Input #0, wav, from 'x.wav':") is None


# --------------------------------------------------------------------------- #
# The JSON path must agree with the banner path
# --------------------------------------------------------------------------- #


def test_json_channel_count_prefers_the_documented_field() -> None:
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import _json_channel_count

    assert _json_channel_count({"channels": 1, "channel_layout": "mono"}) == 1
    assert _json_channel_count({"channels": "2", "channel_layout": "stereo"}) == 2
    assert _json_channel_count({"channels": 6}) == 6


@pytest.mark.parametrize(
    ("layout", "expected"),
    [("mono", 1), ("stereo", 2), ("1 channels", 1), ("2 channels", 2), ("5.1(side)", 6)],
)
def test_json_channel_count_derives_from_the_layout_token(layout: str, expected: int) -> None:
    """A build that omits ``channels`` must still produce a verdict."""
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import _json_channel_count

    assert _json_channel_count({"channel_layout": layout}) == expected


@pytest.mark.parametrize(
    "stream",
    [{}, {"channels": 0}, {"channels": -2}, {"channels": "many"}, {"channel_layout": "martian"}],
)
def test_json_channel_count_refuses_an_unprovable_shape(stream: dict) -> None:
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
        AudioProbeError,
        _json_channel_count,
    )

    with pytest.raises(AudioProbeError):
        _json_channel_count(stream)


def test_banner_and_json_agree_on_every_published_layout() -> None:
    """Cross-probe parity: both parsers answer the same question identically."""
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import _json_channel_count

    for token in ("mono", "stereo", "1 channels", "2 channels", "quad", "5.1", "7.1.4"):
        banner = parse_audio_stream_channels(token)
        assert banner is not None, token
        assert _json_channel_count({"channel_layout": token}) == banner, token
