"""Part 2 self-audit — FFmpeg ``loudnorm`` rate guard and STRICT_LINEAR crest.

Two properties are pinned here, both of which the audit found *unproven* rather
than proven-wrong, and one of which was proven-wrong:

1. ``loudnorm`` upsamples to 192 kHz internally and, with no guard after it,
   writes the file at 192 kHz.  Measured: a 48 kHz mono source rendered with a
   bare ``loudnorm`` filter comes out at 192000 Hz.  The canonical profile must
   re-assert its rate after the stage.
2. A passport claiming ``STRICT_LINEAR`` must be refused if the published file
   actually lost crest factor — i.e. if it was limited or compressed while
   claiming to be a linear gain master.
"""

from __future__ import annotations

import subprocess

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder
from nexus_ai_agent.creative.audio_engine.models import (
    CANONICAL_PROFILE_ID,
    CanonicalStreamProfile,
)
from nexus_ai_agent.creative.audio_engine.verify import (
    CODE_CREST_NOT_INVARIANT,
    STRICT_LINEAR_CREST_TOLERANCE_DB,
    _enforce_strict_linear_crest,
)


def test_canonical_profile_reasserts_its_rate_after_loudnorm() -> None:
    """The guard exists and names the canonical rate, not the 192 kHz one."""
    profile = CanonicalStreamProfile()
    assert profile.sample_rate == 48_000
    assert profile.LOUDNORM_INTERNAL_RATE == 192_000
    assert profile.loudnorm_rate_guard() == ("aresample=48000",)


def test_rate_guard_is_a_genuine_no_op_at_192k() -> None:
    """At 192 kHz there is nothing to restore, so no redundant resampler."""
    assert CanonicalStreamProfile(sample_rate=192_000).loudnorm_rate_guard() == ()


def test_canonical_profile_id_is_unchanged() -> None:
    """The guard must not have altered the profile identity the lock hashes."""
    assert CanonicalStreamProfile().profile_id == CANONICAL_PROFILE_ID


class _Passport:
    """Minimal stand-in: only the loudness target's crest claim is consulted."""

    class _Target:
        crest_reduction_db: float | None = None
        processing_mode = "STRICT_LINEAR"

    loudness_target = _Target()


@pytest.fixture(scope="module")
def programme(tmp_path_factory: pytest.TempPathFactory) -> tuple[object, object, object]:
    """A 25 dB-crest source, a linear master of it, and a crushed master."""
    import wave

    root = tmp_path_factory.mktemp("crest")
    rate = 48_000
    count = rate * 6
    rng = np.random.default_rng(5)
    body = 0.06 * rng.standard_normal(count)
    for when in np.arange(0.5, 6.0, 0.5):
        start = int(when * rate)
        length = int(0.01 * rate)
        body[start : start + length] += (
            0.9 * np.exp(-np.linspace(0.0, 8.0, length)) * rng.standard_normal(length)
        )
    body = np.clip(body, -1.0, 1.0)
    source = root / "source.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((body * 32767).astype("<i2").tobytes())
    transcoder = FfmpegTranscoder()
    linear = root / "linear.wav"
    crushed = root / "crushed.wav"
    transcoder.render(
        source=source,
        destination=linear,
        audio_filters=["volume=-6dB"],
        profile_args=("-ar", str(rate), "-ac", "1"),
        container_hint=".wav",
    )
    transcoder.render(
        source=source,
        destination=crushed,
        audio_filters=[
            "acompressor=threshold=0.03:ratio=20:attack=1:release=50",
            "volume=4dB",
        ],
        profile_args=("-ar", str(rate), "-ac", "1"),
        container_hint=".wav",
    )
    return transcoder, source, (linear, crushed)


def test_a_linear_master_passes_the_crest_check(programme: tuple) -> None:
    """A pure gain change genuinely is crest-invariant, so it must verify."""
    transcoder, source, (linear, _crushed) = programme
    fresh = transcoder.measure_loudness(linear)
    code, detail = _enforce_strict_linear_crest(
        passport=_Passport(),  # type: ignore[arg-type]
        artifact_path=linear,
        fresh=fresh,
        source=source,
        engine=transcoder,
        cancel=None,
    )
    assert code is None, detail


def test_a_compressed_master_claiming_strict_linear_is_refused(programme: tuple) -> None:
    """The measured failure: ~10 dB of crest removed while claiming linear."""
    transcoder, source, (_linear, crushed) = programme
    fresh = transcoder.measure_loudness(crushed)
    code, detail = _enforce_strict_linear_crest(
        passport=_Passport(),  # type: ignore[arg-type]
        artifact_path=crushed,
        fresh=fresh,
        source=source,
        engine=transcoder,
        cancel=None,
    )
    assert code == CODE_CREST_NOT_INVARIANT
    assert "crest factor" in detail
    # The amount removed is reported, not just the verdict.
    assert "dB" in detail


def test_a_missing_source_refuses_rather_than_passing(programme: tuple) -> None:
    """No source means the claim is unverifiable — which is not a pass."""
    transcoder, _source, (linear, _crushed) = programme
    fresh = transcoder.measure_loudness(linear)
    code, detail = _enforce_strict_linear_crest(
        passport=_Passport(),  # type: ignore[arg-type]
        artifact_path=linear,
        fresh=fresh,
        source=None,
        engine=transcoder,
        cancel=None,
    )
    assert code == CODE_CREST_NOT_INVARIANT
    assert "no source" in detail


def test_crest_budget_is_quarter_of_a_db() -> None:
    """The tolerance is the stated 0.25 dB, tight enough to catch any limiter."""
    assert STRICT_LINEAR_CREST_TOLERANCE_DB == 0.25


def test_the_startup_fallback_guard_is_not_the_only_line_of_defence() -> None:
    """``filter_arguments`` itself carries the canonical rate.

    The renderer appends ``stream.filter_arguments()`` after ``loudnorm``, so the
    rate is restored even on a code path that forgets ``loudnorm_rate_guard``.
    Both are asserted so neither can be deleted silently.
    """
    filters = CanonicalStreamProfile().filter_arguments()
    assert filters[0] == "aresample=48000"


def test_ffmpeg_really_does_upsample_loudnorm_to_192k(programme: tuple) -> None:
    """The premise of the whole guard, measured rather than assumed.

    A 48 kHz source rendered through a *bare* ``loudnorm`` (no guard after it)
    comes out at 192000 Hz.  That is why every loudness render appends the
    canonical profile's filters: without them the published file would violate
    the locked stream shape, and the verifier would (correctly) refuse it.

    The guard itself is then measured against the same source: with
    ``filter_arguments()`` appended the output is 48 kHz again.
    """
    import imageio_ffmpeg

    binary = imageio_ffmpeg.get_ffmpeg_exe()
    transcoder, source, _masters = programme
    bare = source.parent / "bare_loudnorm.wav"
    guarded = source.parent / "guarded_loudnorm.wav"
    filter_text = "loudnorm=I=-14:TP=-2:LRA=11:linear=true"

    def render(destination: object, filters: str) -> int:
        completed = subprocess.run(
            [
                binary,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-af",
                filters,
                "-ac",
                "1",
                str(destination),
            ],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr[:400]
        probe = transcoder.probe(destination)
        return probe.sample_rate

    unguarded_rate = render(bare, filter_text)
    assert unguarded_rate == CanonicalStreamProfile.LOUDNORM_INTERNAL_RATE, (
        f"expected the unguarded loudnorm stage to leave the stream at "
        f"{CanonicalStreamProfile.LOUDNORM_INTERNAL_RATE} Hz, measured {unguarded_rate} Hz — "
        "if FFmpeg changed this, the guard reasoning must be re-derived"
    )
    guarded_rate = render(
        guarded, ",".join([filter_text, *CanonicalStreamProfile().filter_arguments()])
    )
    assert guarded_rate == CanonicalStreamProfile().sample_rate
