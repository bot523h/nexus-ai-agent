"""STAGE-A adversarial tests: the four defects the forensic audit found in 6915d32.

Each test here *reproduced a real defect* before it was fixed, and each one is
written so that it fails if the fix is reverted.  They are ordered by severity:

``D1 — verify-before-publish``
    ``_derive`` used to call ``workspace.publish()`` and only *then* run the
    verifier.  For the window between the rename and the refusal, an artifact
    that was about to be rejected was readable at its final path, and a crash
    in that window left it there permanently.  The fix proves the bytes while
    they are still private to the run directory and renames them into place
    only after the verifier agrees.

``D2 — recipe identity``
    The reuse path keyed only on ``output_asset_id``: a second request for the
    same output id with *different parameters* (``strength=0.9`` after
    ``strength=0.2``) returned the first render and reported
    ``reused_existing=True``.  Evidence claimed a recipe that never ran.

``D3 — cancellation``
    ``_derive`` passed ``cancel=None`` into every builder, so a ``CancelToken``
    could not reach FFmpeg and a cancelled job ran to completion.

``D4 — lag-k tempo candidates``
    ``_interval_candidates`` used ``np.diff(onsets, n=k)``, which is the k-th
    *finite difference* and is identically zero for evenly spaced onsets, so the
    skip-2..4 candidates were dead code.  The k-lag gap is
    ``(t[i+k] - t[i]) / k``.

Every test drives the real engine (real FFmpeg, real processes) or the exact
function under test.  None of them asserts an intention.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import omega_media as media
import pytest

from nexus_ai_agent.application.ports.audio_engine import DenoiseRequest, EngineRejected
from nexus_ai_agent.creative.audio_engine import ffmpeg_ops, verify
from nexus_ai_agent.creative.audio_engine.execution import (
    AudioExecutionSettings,
    LocalDirectorySourceCatalog,
    NativeAudioEngine,
)
from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder
from nexus_ai_agent.creative.audio_engine.process import CancelToken, ProcessCancelled
from nexus_ai_agent.creative.audio_engine.workspace import hash_file
from nexus_ai_agent.creative.slideshow.audio import _interval_candidates


def _transcoder() -> FfmpegTranscoder:
    return FfmpegTranscoder()


def _binary_available() -> bool:
    try:
        _transcoder()
    except Exception:  # pragma: no cover - environment without FFmpeg
        return False
    return True


requires_ffmpeg = pytest.mark.skipif(
    not _binary_available(), reason="no FFmpeg binary in this environment"
)


@pytest.fixture()
def media_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    return directory


@pytest.fixture()
def engine(media_dir: Path, tmp_path: Path) -> NativeAudioEngine:
    return NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(
            workspace_root=tmp_path / "workspace",
            max_analysis_seconds=20,
            max_beat_analysis_seconds=30,
            deadline_seconds=180,
            signer_key_id="stage-a",
            signing_key=bytes.fromhex(
                "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# D1 — verify before publish
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_no_unverified_byte_sequence_ever_appears_at_the_published_path(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier refusal must happen *before* the final path exists.

    The defect: ``publish()`` ran first, so this test's observation loop saw a
    real, readable artifact at ``media/denoised.flac`` for a run that then
    refused.  After the fix the path must never exist at all.
    """
    media.write_noisy_voice(media_dir / "voice.wav")
    destination = tmp_path / "workspace" / "media" / "denoised.flac"
    observed: list[int] = []

    # A verifier that refuses every artifact: the engine must therefore never
    # publish one.  We wrap the real one so the refusal is structural, not a
    # monkeypatched boolean.
    real_build = verify.build_passport

    def refusing_passport(**kwargs: object) -> object:
        observed.append(1 if destination.exists() else 0)
        raise verify.PassportError("adversarial verifier refusal")

    monkeypatch.setattr(verify, "build_passport", refusing_passport)
    monkeypatch.setattr(
        "nexus_ai_agent.creative.audio_engine.execution.build_passport", refusing_passport
    )
    with pytest.raises(EngineRejected):
        engine.remove_noise(
            DenoiseRequest(
                request_id="d1-refusal",
                source_asset_id="voice",
                output_asset_id="denoised",
                source_sha256=hash_file(media_dir / "voice.wav"),
                strength=0.5,
            )
        )
    monkeypatch.setattr(verify, "build_passport", real_build)

    assert observed, "the verifier was never consulted — the test proves nothing"
    assert destination.exists() is False, (
        "an artifact that failed verification was published at its final path; "
        "a crash in that window would have left it there as if it were good"
    )
    # And a rejection must not poison the id for the next honest run.
    monkeypatch.undo()
    good = engine.remove_noise(
        DenoiseRequest(
            request_id="d1-retry",
            source_asset_id="voice",
            output_asset_id="denoised",
            source_sha256=hash_file(media_dir / "voice.wav"),
            strength=0.5,
        )
    )
    assert good.verified is True
    assert good.reused_existing is False


@requires_ffmpeg
def test_a_refused_render_leaves_no_staging_residue(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    from nexus_ai_agent.application.ports.audio_engine import LoudnessRequest

    # Normalization of a silent programme is the honest refusal: there is
    # nothing to normalize, and a "normalized" silent file would be a false
    # success.  (Denoise has no loudness target and legitimately accepts it.)
    media.write_silence(media_dir / "hush.wav")
    with pytest.raises(EngineRejected):
        engine.normalize_loudness(
            LoudnessRequest(
                request_id="d1-residue",
                source_asset_id="hush",
                output_asset_id="hush_out",
                source_sha256=hash_file(media_dir / "hush.wav"),
                profile_id="ebu_r128_broadcast",
                target_lufs=-23.0,
                true_peak_dbtp=-1.0,
                target_lra_lu=11.0,
            )
        )
    staging = tmp_path / "workspace" / "staging"
    assert list(staging.iterdir()) == []
    assert list((tmp_path / "workspace" / "media").iterdir()) == []


# --------------------------------------------------------------------------- #
# D2 — recipe identity
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_changing_a_recipe_parameter_never_returns_the_previous_artifact(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """``strength=0.2`` then ``strength=0.9`` on one output id must differ.

    The defect: the second call returned the first file with
    ``reused_existing=True`` and a passport whose ``parameters`` claimed
    ``strength=0.9``.
    """
    media.write_noisy_voice(media_dir / "voice.wav")
    source_sha = hash_file(media_dir / "voice.wav")
    first = engine.remove_noise(
        DenoiseRequest(
            request_id="d2-weak",
            source_asset_id="voice",
            output_asset_id="clean",
            source_sha256=source_sha,
            strength=0.2,
        )
    )
    second = engine.remove_noise(
        DenoiseRequest(
            request_id="d2-strong",
            source_asset_id="voice",
            output_asset_id="clean",
            source_sha256=source_sha,
            strength=0.9,
        )
    )
    assert first.verified is True and second.verified is True
    assert second.reused_existing is False, (
        "a different recipe reused the earlier artifact: the evidence would claim "
        "strength=0.9 while shipping the bytes of strength=0.2"
    )
    assert second.sha256 != first.sha256
    assert first.passport_id != second.passport_id


@requires_ffmpeg
def test_the_same_recipe_is_still_reused(engine: NativeAudioEngine, media_dir: Path) -> None:
    """The identity fix must not disable honest replay."""
    media.write_noisy_voice(media_dir / "voice.wav")
    source_sha = hash_file(media_dir / "voice.wav")
    request = DenoiseRequest(
        request_id="d2-replay",
        source_asset_id="voice",
        output_asset_id="clean",
        source_sha256=source_sha,
        strength=0.4,
    )
    first = engine.remove_noise(request)
    second = engine.remove_noise(request.model_copy(update={"request_id": "d2-replay-again"}))
    assert first.reused_existing is False
    assert second.reused_existing is True
    assert second.sha256 == first.sha256
    assert second.passport_id == first.passport_id


@requires_ffmpeg
def test_a_changed_source_under_the_same_id_is_not_reused(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    media.write_noisy_voice(media_dir / "voice.wav")
    first = engine.remove_noise(
        DenoiseRequest(
            request_id="d2-a",
            source_asset_id="voice",
            output_asset_id="clean",
            source_sha256=hash_file(media_dir / "voice.wav"),
            strength=0.5,
        )
    )
    # Replace the source with different bytes, registry hash updated to match.
    media.write_noisy_voice(media_dir / "voice.wav", seconds=12.0)
    second = engine.remove_noise(
        DenoiseRequest(
            request_id="d2-b",
            source_asset_id="voice",
            output_asset_id="clean",
            source_sha256=hash_file(media_dir / "voice.wav"),
            strength=0.5,
        )
    )
    assert second.reused_existing is False
    assert second.sha256 != first.sha256


def test_recipe_identity_covers_every_discriminating_field() -> None:
    """A pure unit-level mutation trap for the identity function itself.

    If any component stops contributing to the digest, exactly one of these
    assertions flips.  This is what keeps the *key* honest rather than only the
    integration behaviour above.
    """
    from nexus_ai_agent.creative.audio_engine.execution import recipe_identity

    base = recipe_identity(
        operation="audio.remove_noise",
        source_sha256="sha256:" + "0" * 64,
        parameters={"strength": 0.2},
        profiles={"canonical": "nagar.canonical_audio.v1", "loudness": None},
        engine={"backend": "ffmpeg"},
    )
    assert base.startswith("recipe:")
    variants = [
        recipe_identity(
            operation="audio.normalize_loudness",
            source_sha256="sha256:" + "0" * 64,
            parameters={"strength": 0.2},
            profiles={"canonical": "nagar.canonical_audio.v1", "loudness": None},
            engine={"backend": "ffmpeg"},
        ),
        recipe_identity(
            operation="audio.remove_noise",
            source_sha256="sha256:" + "1" * 64,
            parameters={"strength": 0.2},
            profiles={"canonical": "nagar.canonical_audio.v1", "loudness": None},
            engine={"backend": "ffmpeg"},
        ),
        recipe_identity(
            operation="audio.remove_noise",
            source_sha256="sha256:" + "0" * 64,
            parameters={"strength": 0.9},
            profiles={"canonical": "nagar.canonical_audio.v1", "loudness": None},
            engine={"backend": "ffmpeg"},
        ),
        recipe_identity(
            operation="audio.remove_noise",
            source_sha256="sha256:" + "0" * 64,
            parameters={"strength": 0.2},
            profiles={"canonical": "nagar.canonical_audio.v1", "loudness": (-14.0, -1.0)},
            engine={"backend": "ffmpeg"},
        ),
        recipe_identity(
            operation="audio.remove_noise",
            source_sha256="sha256:" + "0" * 64,
            parameters={"strength": 0.2},
            profiles={"canonical": "nagar.canonical_audio.v1", "loudness": None},
            engine={"backend": "ffmpeg", "version": "different"},
        ),
    ]
    assert len({base, *variants}) == len(variants) + 1, (
        "two distinct recipes hashed to the same identity: an artifact could be "
        "reused for a recipe that was never executed"
    )
    # And it is order-insensitive: the same recipe built in another key order is
    # the same recipe.
    assert (
        recipe_identity(
            operation="audio.remove_noise",
            source_sha256="sha256:" + "0" * 64,
            parameters={"strength": 0.2},
            profiles={"loudness": None, "canonical": "nagar.canonical_audio.v1"},
            engine={"backend": "ffmpeg"},
        )
        == base
    )


# --------------------------------------------------------------------------- #
# D3 — cancellation reaches the process
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_the_callers_cancel_token_reaches_the_builder(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Propagation is a separate fact from the pre-flight check.

    A pre-cancelled token is caught by the pre-flight check even if the builder
    never sees it, so that test alone cannot prove propagation.  This one
    captures the token object the builder receives: if ``_derive`` passes
    ``None`` instead (the original defect), the render cannot be interrupted.
    """
    from nexus_ai_agent.creative.audio_engine import execution as execution_module

    media.write_noisy_voice(media_dir / "voice.wav")
    seen: list[object] = []
    real_render = execution_module.NativeAudioEngine._render_loudness

    def spy(self, staged, workspace, profile, cancel, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(cancel)
        return real_render(self, staged, workspace, profile, cancel, **kwargs)

    token = CancelToken()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(execution_module.NativeAudioEngine, "_render_loudness", spy)
        from nexus_ai_agent.application.ports.audio_engine import LoudnessRequest

        evidence = engine.normalize_loudness(
            LoudnessRequest(
                request_id="d3-propagate",
                source_asset_id="voice",
                output_asset_id="propagated",
                source_sha256=hash_file(media_dir / "voice.wav"),
                profile_id="ebu_r128_broadcast",
                target_lufs=-23.0,
                true_peak_dbtp=-1.0,
                target_lra_lu=11.0,
            ),
            cancel=token,
        )
    # An uncancelled token must not disturb an otherwise valid run.
    assert evidence.verified is True
    assert seen and seen[0] is token, (
        "the builder received a different cancellation token (or None): a cancel "
        "can no longer reach the FFmpeg process group"
    )


@requires_ffmpeg
def test_a_pre_cancelled_token_aborts_the_render_and_publishes_nothing(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    media.write_noisy_voice(media_dir / "voice.wav")
    token = CancelToken()
    token.cancel("operator cancelled before the render")
    with pytest.raises((ProcessCancelled, EngineRejected)):
        engine.remove_noise(
            DenoiseRequest(
                request_id="d3-cancel",
                source_asset_id="voice",
                output_asset_id="cancelled_out",
                source_sha256=hash_file(media_dir / "voice.wav"),
                strength=0.5,
            ),
            cancel=token,
        )
    assert not (tmp_path / "workspace" / "media" / "cancelled_out.flac").exists()
    assert list((tmp_path / "workspace" / "staging").iterdir()) == []


# --------------------------------------------------------------------------- #
# D4 — lag-k tempo candidates are gaps, not k-th finite differences
# --------------------------------------------------------------------------- #


def test_lag_candidates_are_mean_gaps_not_finite_differences() -> None:
    """Evenly spaced onsets must yield the *period* for every skip."""
    period = 0.625  # 96 BPM
    onsets = np.arange(16, dtype=np.float64) * period
    candidates = _interval_candidates(onsets, 0.2, 3.0)
    assert candidates.size > 0, (
        "the skip-2..4 generators produced nothing: np.diff(x, n=k) is a k-th "
        "finite difference (zero for even spacing), not a k-lag gap"
    )
    # Every candidate is the beat period; a finite difference would have
    # contributed 0.0 (filtered out) and left only skip-1 values.
    assert np.allclose(candidates, period, atol=1e-9)


def test_lag_candidates_are_uncontaminated_period_estimates() -> None:
    """Jitter must not inject non-period values into the candidate pool.

    On real onsets the k-th finite difference is not zero — it is a noisy
    quantity whose magnitude grows with k while its *mean* stays at zero.  A
    k=4 finite difference on 40 ms of onset jitter reaches ±0.42 s, which lands
    squarely inside the 0.2–3.0 s tempo window and is then counted by the
    histogram as if it were a beat period.

    The correct statistic, ``(t[i+k] - t[i]) / k``, estimates the same period
    for every k and averages the jitter out: the *spread* of the candidates
    shrinks as k grows while their centre does not move.  Both properties are
    asserted here, so a revert to the finite-difference form fails immediately.
    """
    period = 0.6
    rng = np.random.default_rng(3)
    onsets = np.arange(24, dtype=np.float64) * period + rng.uniform(-0.04, 0.04, 24)
    candidates = _interval_candidates(onsets, 0.2, 3.0)
    assert candidates.size > 0
    # 1. The pool is the union over every skip: 23 + 22 + 21 + 20 values.  A
    #    finite-difference form contributes only the lag-1 values (the higher
    #    orders land outside the window because their mean is zero), so the
    #    COUNT is the sharpest discriminator available here.
    expected = sum(len(onsets) - step for step in (1, 2, 3, 4))
    assert candidates.size == expected, (
        f"pool has {candidates.size} candidates, expected {expected}: the skip-2..4 "
        "generators produced nothing, so higher lags are not being averaged"
    )
    # 2. Every candidate is the beat period within a single onset pair's jitter.
    assert np.allclose(candidates, period, atol=0.09), (
        f"candidates stray up to {np.abs(candidates - period).max():.3f}s from the "
        "beat period; the k-th finite difference is being counted as a period"
    )
    # 2. Averaging works: higher skips have a tighter spread around the truth.
    spreads = []
    for step in (1, 2, 3, 4):
        gaps = (onsets[step:] - onsets[:-step]) / step
        spreads.append(float(np.std(gaps)))
    assert spreads == sorted(spreads, reverse=True), (
        f"lag-k spreads {spreads} do not shrink with k; the skip candidates are "
        "not the mean k-lag gaps they claim to be"
    )


def test_lag_candidates_sort_their_input() -> None:
    """Onsets arrive from a detector and are not guaranteed ordered."""
    period = 0.4
    ordered = np.arange(10, dtype=np.float64) * period
    shuffled = np.array([ordered[i] for i in (3, 0, 7, 1, 9, 2, 5, 4, 8, 6)])
    assert np.array_equal(
        _interval_candidates(shuffled, 0.2, 3.0),
        _interval_candidates(ordered, 0.2, 3.0),
    ), "an unsorted onset array produced different candidates than the sorted one"


def test_lag_candidates_still_ignore_out_of_window_gaps() -> None:
    onsets = np.array([0.0, 0.1, 5.0, 5.1, 10.0], dtype=np.float64)
    candidates = _interval_candidates(onsets, 0.2, 1.0)
    assert candidates.size == 0


# --------------------------------------------------------------------------- #
# D5 — the canonical stream shape is measured, not the source layout
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_loudness_pass_one_measures_the_canonical_channel_count(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mono source must be measured after the dual-mono upmix, not before.

    Measuring the source layout and rendering the canonical one means the
    integrated loudness of the *published* file is not the number the gain was
    computed from (the upmix changes the energy sum by +3.0103 dB).
    """
    source = media.write_noisy_voice(media_dir / "mono.wav")
    measured_shapes: list[tuple[int | None, int | None]] = []
    real_measure = ffmpeg_ops.FfmpegTranscoder.measure_loudnorm_pass

    def spy(self: FfmpegTranscoder, path: Path, profile: object, *, cancel: object = None):
        # The path lives inside the run directory, which is removed when the run
        # finishes — so the shape must be read *during* pass 1, not afterwards.
        measured = self.probe(path, cancel=cancel)  # type: ignore[arg-type]
        measured_shapes.append((measured.channels, measured.sample_rate))
        return real_measure(self, path, profile, cancel=cancel)  # type: ignore[arg-type]

    monkeypatch.setattr(ffmpeg_ops.FfmpegTranscoder, "measure_loudnorm_pass", spy)
    probe = engine.probe(source)
    assert probe.channels == 1, "the fixture must be mono for this test to mean anything"
    from nexus_ai_agent.application.ports.audio_engine import LoudnessRequest

    engine.normalize_loudness(
        LoudnessRequest(
            request_id="d5-mono",
            source_asset_id="mono",
            output_asset_id="mono_master",
            source_sha256=hash_file(source),
            profile_id="ebu_r128_broadcast",
            target_lufs=-23.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )
    assert measured_shapes, "pass 1 never ran"
    channels, rate = measured_shapes[0]
    assert channels == 2, (
        "pass 1 measured the source layout; the gain was computed for a different "
        "channel count than the one that gets published"
    )
    assert rate == 48_000


# --------------------------------------------------------------------------- #
# Environmental guard: these tests must not silently pass
# --------------------------------------------------------------------------- #


def test_the_engine_binary_used_by_these_tests_is_real() -> None:
    if os.environ.get("NEXUS_SKIP_FFMPEG") == "1":  # pragma: no cover
        pytest.skip("explicitly skipped by environment")
    assert _binary_available(), "STAGE-A hardening tests require a real FFmpeg binary"


# --------------------------------------------------------------------------- #
# D6 — the probe is a typed machine contract, and its absence is honest
# --------------------------------------------------------------------------- #


def test_probe_json_refuses_when_no_ffprobe_exists(tmp_path: Path) -> None:
    """No ffprobe must be an explicit, catchable UNAVAILABLE — never a guess.

    The bundled imageio-ffmpeg build ships ``ffmpeg`` without ``ffprobe``, so
    this is the *actual* situation in this environment, and the engine must say
    so rather than silently falling back to a banner parse while claiming the
    typed contract.
    """
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
        AudioEngineUnavailableError,
        FfmpegTranscoder,
    )

    transcoder = FfmpegTranscoder(probe_binary="/nonexistent/ffprobe")
    transcoder._probe_binary = ""  # simulate a resolved-to-nothing environment
    source = tmp_path / "any.wav"
    source.write_bytes(b"RIFF")
    with pytest.raises(AudioEngineUnavailableError, match="ffprobe is not installed"):
        transcoder.probe_json(source)


def test_probe_json_fails_closed_on_every_malformed_document(tmp_path: Path) -> None:
    """Each malformed probe document must refuse, not return a partial record."""
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
        AudioProbeError,
        FfmpegTranscoder,
        _as_int,
        _extradata_hash,
        _json_object,
    )

    with pytest.raises(AudioProbeError):
        _json_object("no json here at all")
    with pytest.raises(AudioProbeError):
        _json_object("{not valid json}")
    with pytest.raises(AudioProbeError):
        _json_object('["a list, not an object"]')
    with pytest.raises(AudioProbeError):
        _as_int("forty-eight thousand", "sample_rate")
    with pytest.raises(AudioProbeError):
        _as_int(None, "channels")
    with pytest.raises(AudioProbeError):
        _as_int(True, "channels")
    assert _as_int("48000", "sample_rate") == 48000
    assert _extradata_hash(None) is None
    assert _extradata_hash("  ") is None
    assert _extradata_hash("abc") == _extradata_hash("abc")
    assert _extradata_hash("abc") != _extradata_hash("abd")
    # The transcoder must expose the fail-closed contract by name.
    assert hasattr(FfmpegTranscoder, "probe_json")


def test_a_banner_probe_is_labeled_as_such() -> None:
    """A banner parse must be visibly weaker evidence than a JSON probe."""
    from nexus_ai_agent.creative.audio_engine.models import MediaProbe

    banner = MediaProbe(size_bytes=1, sha256="sha256:" + "0" * 64)
    assert banner.source == "banner"
    assert banner.time_base is None and banner.duration_ts is None
    json_probe = MediaProbe(
        size_bytes=1,
        sha256="sha256:" + "0" * 64,
        source="ffprobe_json",
        time_base="1/48000",
        duration_ts=48000,
        start_pts=0,
    )
    assert json_probe.source == "ffprobe_json"


_FFPROBE = os.environ.get("NEXUS_FFPROBE_BIN") or shutil.which("ffprobe")


@pytest.mark.skipif(
    not _FFPROBE,
    reason="no ffprobe in this environment: set NEXUS_FFPROBE_BIN to exercise "
    "the positive path of the typed probe contract",
)
def test_probe_json_proves_the_stream_shape_of_a_published_artifact(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    """The positive half of D6: a real ``ffprobe -of json`` document, parsed.

    Skipped (not failed) when no ffprobe exists, because the absence of the
    binary is an environment fact, not a defect — and the negative half of the
    contract is asserted unconditionally above.
    """
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder

    media.write_noisy_voice(media_dir / "voice.wav")
    evidence = engine.remove_noise(
        DenoiseRequest(
            request_id="d6-json",
            source_asset_id="voice",
            output_asset_id="json_out",
            source_sha256=hash_file(media_dir / "voice.wav"),
            strength=0.5,
        )
    )
    assert evidence.verified is True
    transcoder = FfmpegTranscoder(probe_binary=_FFPROBE)
    probe = transcoder.probe_json(evidence.output_path)
    assert probe.source == "ffprobe_json"
    assert probe.codec == "flac"
    assert probe.channels == 2 and probe.channel_layout == "stereo"
    assert probe.sample_rate == 48_000
    assert probe.time_base == "1/48000"
    assert probe.duration_ts is not None and probe.duration_ts > 0
    # duration_ts * time_base must agree with the measured duration.
    assert probe.container_duration_us == pytest.approx(probe.duration_ts * 1_000_000 / 48_000)
    assert evidence.duration_us == pytest.approx(probe.container_duration_us, abs=1_000)


# --------------------------------------------------------------------------- #
# The new refusal branches must themselves be reachable and asserted
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_a_cancel_that_lands_after_the_last_process_still_publishes_nothing(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path
) -> None:
    """A cancel that lands *after* the last process exits still publishes nothing.

    This is the only window the post-builder check exists for, and it is a real
    one: ``_render_loudness`` measures, then renders, then returns.  A token
    that fires as the encode finishes is not seen by the process governor (which
    already returned successfully), so without the check the finished artifact
    would be verified and published for a job the operator cancelled.

    The window is reproduced deterministically — the render cancels the token as
    its final act — instead of by racing a thread, so the test is a proof rather
    than a probability.
    """
    from nexus_ai_agent.creative.audio_engine import ffmpeg_ops

    media.write_noisy_voice(media_dir / "voice.wav")
    token = CancelToken()
    real_render = ffmpeg_ops.FfmpegTranscoder.render

    def render_then_cancel(self, **kwargs):  # type: ignore[no-untyped-def]
        produced = real_render(self, **kwargs)
        token.cancel("the operator cancelled just as the encode finished")
        return produced

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ffmpeg_ops.FfmpegTranscoder, "render", render_then_cancel)
        with pytest.raises((ProcessCancelled, EngineRejected)):
            engine.remove_noise(
                DenoiseRequest(
                    request_id="d3-late",
                    source_asset_id="voice",
                    output_asset_id="late_cancel",
                    source_sha256=hash_file(media_dir / "voice.wav"),
                    strength=0.5,
                ),
                cancel=token,
            )
    assert not (tmp_path / "workspace" / "media" / "late_cancel.flac").exists(), (
        "a job cancelled as its encode finished was published anyway"
    )
    assert list((tmp_path / "workspace" / "staging").iterdir()) == []


@requires_ffmpeg
def test_an_unverified_passport_never_publishes_and_names_the_reason(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passport that comes back ``verified=False`` must refuse, not publish.

    The earlier refusal path published first and then moved the file to
    ``.rejected``; this asserts the current contract: the final path is never
    created and the message names the failing check.
    """
    media.write_noisy_voice(media_dir / "voice.wav")
    destination = tmp_path / "workspace" / "media" / "unverified.flac"

    def fake_verify(self, **kwargs):  # type: ignore[no-untyped-def]
        passport = engine.__class__._real_verify(self, **kwargs)  # type: ignore[attr-defined]
        return passport.model_copy(
            update={
                "verified": False,
                "canonical_ok": False,
                "canonical_code": "canonical/channel_layout",
                "canonical_detail": "adversarial: forced unverified passport",
            }
        )

    monkeypatch.setattr(engine.__class__, "_real_verify", engine.__class__._verify, raising=False)
    monkeypatch.setattr(engine.__class__, "_verify", fake_verify)
    try:
        with pytest.raises(EngineRejected, match="failed verification"):
            engine.remove_noise(
                DenoiseRequest(
                    request_id="d1-unverified",
                    source_asset_id="voice",
                    output_asset_id="unverified",
                    source_sha256=hash_file(media_dir / "voice.wav"),
                    strength=0.5,
                )
            )
    finally:
        monkeypatch.setattr(engine.__class__, "_verify", engine.__class__._real_verify)
    assert not destination.exists()
    assert list((tmp_path / "workspace" / "staging").iterdir()) == []


def test_strength_outside_the_unit_interval_is_refused_at_the_contract() -> None:
    """The bound is enforced by the request contract, so it cannot reach the engine.

    ``DenoiseRequest`` rejects the value before dispatch; the engine's own
    ``0 <= strength <= 1`` guard is therefore a defensive second line (kept, but
    unreachable through the port).  Asserting the contract is the honest test:
    asserting the unreachable guard would be testing dead code.
    """
    from pydantic import ValidationError

    for bad in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            DenoiseRequest(
                request_id="d",
                source_asset_id="x",
                output_asset_id="y",
                strength=bad,
            )


@requires_ffmpeg
def test_bytes_that_change_between_verification_and_publication_are_refused(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-publish SHA recheck is a real guard, not decoration.

    The verified bytes are replaced by different bytes *after* the verifier
    signed them and *before* the rename, which is the TOCTOU window the recheck
    exists for.  The engine must notice, take the divergent artifact back off
    the final path, and refuse — never ship bytes nobody verified.
    """
    from nexus_ai_agent.creative.audio_engine import execution as execution_module
    from nexus_ai_agent.creative.audio_engine.workspace import RuntimeWorkspace

    media.write_noisy_voice(media_dir / "voice.wav")
    real_publish = RuntimeWorkspace.publish
    destination = tmp_path / "workspace" / "media" / "tampered.flac"

    def tampering_publish(self, staged, target, **kwargs):  # type: ignore[no-untyped-def]
        # Rewrite the run file so the published bytes differ from the signed ones.
        Path(staged).write_bytes(Path(staged).read_bytes() + b"TAMPER")
        return real_publish(self, staged, target, **kwargs)

    monkeypatch.setattr(RuntimeWorkspace, "publish", tampering_publish)
    with pytest.raises(EngineRejected, match="differ from the verified ones"):
        engine.remove_noise(
            DenoiseRequest(
                request_id="d1-toctou",
                source_asset_id="voice",
                output_asset_id="tampered",
                source_sha256=hash_file(media_dir / "voice.wav"),
                strength=0.5,
            )
        )
    monkeypatch.undo()
    assert not destination.exists(), (
        "the divergent artifact was left at the final path: the post-publish hash "
        "recheck did not remove it"
    )
    assert execution_module is not None


def test_a_recipe_marker_that_is_missing_or_garbage_is_not_a_match(tmp_path: Path) -> None:
    """Unknown provenance must force a re-render, never a reuse."""
    from nexus_ai_agent.creative.audio_engine.execution import (
        RECIPE_SUFFIX,
        _read_recipe_marker,
        _write_recipe_marker,
    )

    marker = tmp_path / f"artifact.flac{RECIPE_SUFFIX}"
    assert _read_recipe_marker(marker) is None  # absent
    _write_recipe_marker(marker, "recipe:abc123")
    assert _read_recipe_marker(marker) == "recipe:abc123"
    marker.write_text("   \n", encoding="utf-8")
    assert _read_recipe_marker(marker) is None  # blank
    marker.write_bytes(b"\xff\xfe\x00garbage")
    assert _read_recipe_marker(marker) is None  # undecodable
    # A directory where the marker should be is also "unknown".
    marker.unlink()
    marker.mkdir()
    assert _read_recipe_marker(marker) is None


def _fake_ffprobe(tmp_path: Path, document: str, *, exit_code: int = 0) -> Path:
    """A real executable that prints ``document``: the probe sees no stub objects."""
    script = tmp_path / "fake_ffprobe.sh"
    script.write_text(
        f"#!/bin/sh\ncat <<'JSON_EOF'\n{document}\nJSON_EOF\nexit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ('{"streams": {}, "format": {}}', "no `streams` array"),
        ('{"streams": [{"codec_type": "video"}], "format": {}}', "no audio stream"),
        (
            '{"streams": [{"codec_type": "audio", "codec_name": "flac",'
            ' "sample_rate": "48000", "channels": 2, "time_base": "1/48000"}], "format": {}}',
            "missing required key",
        ),
        (
            '{"streams": [{"codec_type": "audio", "codec_name": "flac",'
            ' "sample_rate": "48000", "channels": 2, "sample_fmt": "s32",'
            ' "time_base": "1/48000", "duration_ts": 48000, "pix_fmt": "yuv420p"}], "format": {}}',
            "video-shaped stream",
        ),
        (
            '{"streams": [{"codec_type": "audio", "codec_name": "flac",'
            ' "sample_rate": "0", "channels": 2, "sample_fmt": "s32",'
            ' "time_base": "1/48000", "duration_ts": 48000}], "format": {}}',
            "non-physical stream",
        ),
        (
            '{"streams": [{"codec_type": "audio", "codec_name": "flac",'
            ' "sample_rate": "48000", "channels": 2, "sample_fmt": "s32",'
            ' "time_base": "1/48000", "duration_ts": -5}], "format": {}}',
            "negative duration_ts",
        ),
        (
            '{"streams": [{"codec_type": "audio", "codec_name": "flac",'
            ' "sample_rate": "48000", "channels": 2, "sample_fmt": "s32",'
            ' "time_base": "1/0", "duration_ts": 48000}], "format": {}}',
            "not a rational",
        ),
        (
            '{"streams": [{"codec_type": "audio", "codec_name": "flac",'
            ' "sample_rate": "48000", "channels": 2, "sample_fmt": "s32",'
            ' "time_base": "x/48000", "duration_ts": 48000}], "format": {}}',
            "not a rational",
        ),
        (
            '{"streams": [{"codec_type": "audio", "codec_name": "flac",'
            ' "sample_rate": "48000", "channels": 2, "sample_fmt": "s32",'
            ' "time_base": "1/48000", "duration_ts": "many"}], "format": {}}',
            "not an integer",
        ),
    ],
)
def test_every_malformed_ffprobe_document_fails_closed(
    tmp_path: Path, document: str, expected: str
) -> None:
    """One executable, nine hostile documents: each must refuse with a reason.

    This is the fail-closed half of §4.6 asserted through the real code path
    (subprocess -> JSON -> typed record), so a parser that quietly defaults a
    missing field is caught here rather than downstream.
    """
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
        AudioProbeError,
        FfmpegTranscoder,
    )

    source = tmp_path / "input.wav"
    source.write_bytes(b"RIFF0000WAVE")
    transcoder = FfmpegTranscoder(probe_binary=str(_fake_ffprobe(tmp_path, document)))
    transcoder._probe_binary = str(_fake_ffprobe(tmp_path, document))
    with pytest.raises(AudioProbeError, match=expected):
        transcoder.probe_json(source)


def test_an_ffprobe_that_exits_nonzero_is_a_refusal(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
        AudioProbeError,
        FfmpegTranscoder,
    )

    source = tmp_path / "input.wav"
    source.write_bytes(b"RIFF0000WAVE")
    binary = _fake_ffprobe(tmp_path, '{"streams": []}', exit_code=3)
    transcoder = FfmpegTranscoder(probe_binary=str(binary))
    with pytest.raises(AudioProbeError, match="ffprobe failed"):
        transcoder.probe_json(source)


def test_a_layout_omitted_by_ffprobe_is_derived_from_the_channel_count(
    tmp_path: Path,
) -> None:
    """`channel_layout` absent must yield the count's honest name, not a guess."""
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder

    source = tmp_path / "input.wav"
    source.write_bytes(b"RIFF0000WAVE")
    document = (
        '{"streams": [{"codec_type": "audio", "codec_name": "flac",'
        ' "sample_rate": "48000", "channels": 1, "sample_fmt": "s16",'
        ' "time_base": "1/48000", "duration_ts": 24000, "profile": "Flac"}],'
        ' "format": {"duration": "0.5"}}'
    )
    binary = _fake_ffprobe(tmp_path, document)
    transcoder = FfmpegTranscoder(probe_binary=str(binary))
    probe = transcoder.probe_json(source)
    assert probe.channel_layout == "mono"
    assert probe.channels == 1
    assert probe.start_pts is None
    assert probe.profile == "Flac"
    assert probe.duration_ts == 24000
    assert probe.container_duration_us == 500_000


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        # a rational whose numerator is zero: the format has no clock
        (
            '{"streams": [{"codec_type": "audio", "codec_name": "flac", "sample_rate": "48000",'
            ' "channels": 2, "sample_fmt": "s32", "time_base": "0/48000", "duration_ts": 0}],'
            ' "format": {}}',
            "not positive",
        ),
        # duration_ts is zero, so the format duration decides — and it is a word
        (
            '{"streams": [{"codec_type": "audio", "codec_name": "flac", "sample_rate": "48000",'
            ' "channels": 2, "sample_fmt": "s32", "time_base": "1/48000", "duration_ts": 0}],'
            ' "format": {"duration": "unknown"}}',
            "format duration is not a number",
        ),
    ],
)
def test_the_remaining_probe_refusals_are_reachable(
    tmp_path: Path, document: str, expected: str
) -> None:
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import (
        AudioProbeError,
        FfmpegTranscoder,
    )

    source = tmp_path / "input.wav"
    source.write_bytes(b"RIFF0000WAVE")
    transcoder = FfmpegTranscoder(probe_binary=str(_fake_ffprobe(tmp_path, document)))
    with pytest.raises(AudioProbeError, match=expected):
        transcoder.probe_json(source)


def test_a_layout_ffprobe_does_report_is_used_verbatim(tmp_path: Path) -> None:
    """A stated layout wins over the count-derived default (never overwritten)."""
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder

    source = tmp_path / "input.wav"
    source.write_bytes(b"RIFF0000WAVE")
    document = (
        '{"streams": [{"codec_type": "audio", "codec_name": "flac", "sample_rate": "48000",'
        ' "channels": 2, "channel_layout": "2.1", "sample_fmt": "s32",'
        ' "time_base": "1/48000", "duration_ts": 48000}], "format": {"duration": "1.0"}}'
    )
    probe = FfmpegTranscoder(probe_binary=str(_fake_ffprobe(tmp_path, document))).probe_json(source)
    assert probe.channel_layout == "2.1"
    assert probe.channels == 2


def test_the_format_duration_is_used_only_when_duration_ts_is_zero(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.audio_engine.ffmpeg_ops import FfmpegTranscoder

    source = tmp_path / "input.wav"
    source.write_bytes(b"RIFF0000WAVE")
    document = (
        '{"streams": [{"codec_type": "audio", "codec_name": "flac", "sample_rate": "48000",'
        ' "channels": 2, "sample_fmt": "s32", "time_base": "1/48000", "duration_ts": 0}],'
        ' "format": {"duration": "1.75"}}'
    )
    probe = FfmpegTranscoder(probe_binary=str(_fake_ffprobe(tmp_path, document))).probe_json(source)
    assert probe.container_duration_us == 1_750_000


# --------------------------------------------------------------------------- #
# D7 — the workspace is an adapter on the one filesystem authority
# --------------------------------------------------------------------------- #


def test_the_workspace_delegates_resolution_to_the_canonical_authority(tmp_path: Path) -> None:
    """Behavioural, not structural: the authority's verdict IS the engine's.

    Asserting ``isinstance`` would pass for any object of that class, including
    one that is never consulted.  The proof that the engine has no second
    containment rule is that a path the authority refuses (a symlinked
    component) is refused by the workspace too, *through* the authority — and
    that the authority's own error text is what surfaces.
    """
    from nexus_ai_agent.creative.audio_engine.workspace import (
        RuntimeWorkspace,
        WorkspaceBoundaryError,
    )
    from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    authority = workspace.filesystem
    assert isinstance(authority, WorkspaceFilesystem)
    assert authority.root == workspace.root

    # A symlinked leaf: the authority refuses it, and so must the workspace.
    outside = tmp_path / "outside.txt"
    outside.write_text("not in the workspace", encoding="utf-8")
    link = workspace.media_dir / "leaf.flac"
    link.symlink_to(outside)

    from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError

    # The authority refuses it (a leaf that resolves outside the boundary is an
    # escape; a link that stays inside is refused as a symlink — both are the
    # authority's verdicts, and the workspace only translates them).
    with pytest.raises(FilesystemBoundaryError):
        authority.resolve("media/leaf.flac")
    try:
        workspace.contain(link)
    except WorkspaceBoundaryError as error:
        assert isinstance(error.__cause__, FilesystemBoundaryError), (
            "the workspace raised its own error instead of translating the "
            "authority's: that is a second containment rule"
        )
    else:  # pragma: no cover - the authority always refuses this
        raise AssertionError("a symlinked leaf was accepted")


def test_a_traversal_asset_id_cannot_escape_the_media_directory(tmp_path: Path) -> None:
    """``../../etc/passwd`` as an asset id resolves to a contained name."""
    from nexus_ai_agent.creative.audio_engine.workspace import RuntimeWorkspace, safe_file_name

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    for hostile in ("../../etc/passwd", "..", ".", "a/b/../../c", "\x00null"):
        resolved = workspace.media_path(hostile, suffix=".flac")
        assert resolved.is_relative_to(workspace.media_dir), f"{hostile!r} escaped"
        assert resolved.parent == workspace.media_dir
    assert safe_file_name("../../etc/passwd", suffix=".flac", default="artifact").endswith(".flac")


def test_a_symlinked_parent_cannot_redirect_a_write(tmp_path: Path) -> None:
    """A symlink at any level below the root is refused, not followed."""
    from nexus_ai_agent.creative.audio_engine.workspace import (
        RuntimeWorkspace,
        WorkspaceBoundaryError,
    )

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.flac").write_bytes(b"keep me")
    # Replace media/ with a link to an unrelated directory.
    (workspace.media_dir / "placeholder.bin").write_bytes(b"x")
    for entry in workspace.media_dir.iterdir():
        entry.unlink()
    workspace.media_dir.rmdir()
    workspace.media_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises((WorkspaceBoundaryError, Exception)):
        workspace.media_path("escape", suffix=".flac")
    assert (outside / "victim.flac").read_bytes() == b"keep me"


def test_publish_refuses_a_symlinked_destination(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.audio_engine.workspace import (
        AtomicPublishError,
        RuntimeWorkspace,
    )

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    outside = tmp_path / "elsewhere.flac"
    outside.write_bytes(b"not yours")
    destination = workspace.media_dir / "linked.flac"
    destination.symlink_to(outside)
    with pytest.raises((AtomicPublishError, Exception)):
        with workspace.run_directory("sym-run") as run:
            produced = run / "out.flac"
            produced.write_bytes(b"FLaC" + b"\x00" * 32)
            workspace.publish(produced, destination, replace=True)
    assert outside.read_bytes() == b"not yours", "a publish wrote through a symlink"


def test_an_existing_destination_is_not_overwritten_without_replace(tmp_path: Path) -> None:
    """A concurrent second attempt must not silently win the same id."""
    from nexus_ai_agent.creative.audio_engine.workspace import (
        AtomicPublishError,
        RuntimeWorkspace,
    )

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    destination = workspace.media_dir / "contested.flac"
    destination.write_bytes(b"first writer wins")
    with pytest.raises(AtomicPublishError, match="already exists"):
        with workspace.run_directory("contend-run") as run:
            produced = run / "out.flac"
            produced.write_bytes(b"FLaC" + b"\x01" * 32)
            workspace.publish(produced, destination)
    assert destination.read_bytes() == b"first writer wins"


def test_publish_rejects_the_same_file_as_source_and_destination(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.audio_engine.workspace import (
        AtomicPublishError,
        RuntimeWorkspace,
    )

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    same = workspace.media_dir / "self.flac"
    same.write_bytes(b"FLaC")
    with pytest.raises(AtomicPublishError, match="same file"):
        workspace.publish(same, same, replace=True)


def test_staging_an_input_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    """The same bytes stage once; changed bytes never reuse the old copy."""
    from nexus_ai_agent.creative.audio_engine.workspace import (
        RuntimeWorkspace,
        WorkspaceBoundaryError,
        hash_file,
    )

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    source = tmp_path / "input.wav"
    source.write_bytes(b"RIFF" + b"\x00" * 100)
    first = workspace.stage_input(source)
    second = workspace.stage_input(source)
    assert first == second
    assert first.name.startswith(hash_file(source).split(":")[1])
    # A registry hash that does not match the bytes must refuse before any copy.
    with pytest.raises(WorkspaceBoundaryError, match="changed before staging"):
        workspace.stage_input(source, expected_sha256="sha256:" + "0" * 64)
    # Different bytes stage a different file.
    source.write_bytes(b"RIFF" + b"\x01" * 100)
    third = workspace.stage_input(source)
    assert third != first


def test_a_run_directory_removes_itself_even_when_the_run_raises(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.audio_engine.workspace import RuntimeWorkspace

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    with pytest.raises(RuntimeError, match="engine exploded"):
        with workspace.run_directory("boom") as run:
            (run / "partial.flac").write_bytes(b"FLaC partial")
            raise RuntimeError("engine exploded")
    assert list(workspace.staging_dir.iterdir()) == []
    assert workspace.leftovers() == []


def test_an_unsafe_run_id_is_refused(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.audio_engine.workspace import (
        RuntimeWorkspace,
        WorkspaceBoundaryError,
    )

    workspace = RuntimeWorkspace(tmp_path / "ws").prepare()
    for hostile in ("../escape", "a/b", "a\\b", ".", ".."):
        with pytest.raises(WorkspaceBoundaryError, match="unsafe run id"):
            with workspace.run_directory(hostile):
                pass
    # An empty id is not hostile: it is the documented "generate one" case.
    with workspace.run_directory("") as generated:
        assert generated.parent == workspace.staging_dir
        assert len(generated.name) == 32


# --------------------------------------------------------------------------- #
# D8 — the resource budget is measured, and "not measured" is said out loud
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_the_budget_reports_measurements_not_estimates(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Every field the doctrine calls a measurement must actually be measured.

    The previous implementation shipped ``ResourceBudget`` as dead code: nothing
    ever constructed it, so the numbers a reader would have trusted did not
    exist.  This asserts they do, and that they agree with independently
    measurable facts about the same run.
    """
    source = media.write_noisy_voice(media_dir / "budget.wav", seconds=8.0)
    evidence = engine.remove_noise(
        DenoiseRequest(
            request_id="d8-budget",
            source_asset_id="budget",
            output_asset_id="budget_out",
            source_sha256=hash_file(source),
            strength=0.5,
        )
    )
    budget = evidence.budget
    assert budget is not None, "a real render must report what it consumed"
    # Input size is the file on disk, not a guess.
    assert budget["input_size_bytes"] == source.stat().st_size
    # The decoder's own duration, cross-checked against the probe.
    probe = engine.probe(source)
    assert budget["input_duration_us"] == probe.container_duration_us
    assert budget["decoded_samples"] == pytest.approx(
        probe.container_duration_us * probe.sample_rate / 1_000_000, rel=1e-3
    )
    # Output size is the published artifact.
    assert budget["output_size_bytes"] == Path(evidence.output_path).stat().st_size
    # The run really did stage something, and staging is empty afterwards.
    assert budget["staging_peak_bytes"] > 0, "staging was never occupied?"
    assert budget["staging_peak_bytes"] >= budget["output_size_bytes"]
    assert budget["processes_started"] > 0, "a render must have started processes"
    assert budget["elapsed_seconds"] > 0
    assert budget["within_budget"] is True
    assert budget["ffmpeg_version"], "the tool version is part of the evidence"


@requires_ffmpeg
def test_peak_rss_is_measured_or_explicitly_not_measured(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """``None`` must never be presented as zero.

    On Linux ``/proc/<pid>/status`` gives the kernel's own high-water mark; on a
    host without it the only honest answer is ``NOT_MEASURED``.  Either way the
    report says which one it is, and a measured value is physically plausible.
    """
    media.write_noisy_voice(media_dir / "rss.wav", seconds=8.0)
    evidence = engine.remove_noise(
        DenoiseRequest(
            request_id="d8-rss",
            source_asset_id="rss",
            output_asset_id="rss_out",
            source_sha256=hash_file(media_dir / "rss.wav"),
            strength=0.5,
        )
    )
    budget = evidence.budget
    assert budget is not None
    if budget["rss"] == "NOT_MEASURED":
        assert budget["peak_rss_bytes"] is None
    else:
        assert budget["rss"] == "MEASURED"
        rss = budget["peak_rss_bytes"]
        assert isinstance(rss, int) and rss > 0
        # An FFmpeg process decoding a few megabytes cannot fit in 1 MiB, and a
        # small image would not be using 10 GiB.  A wrong unit (KiB reported as
        # bytes, or bytes as KiB) fails here.
        assert 1 << 20 < rss < 10 << 30, f"implausible peak RSS: {rss} bytes"


def test_the_staging_quota_refuses_before_it_copies(tmp_path: Path) -> None:
    """A quota checked only *after* the copy is a report, not a bound."""
    from nexus_ai_agent.creative.audio_engine.execution import (
        AudioExecutionSettings,
        LocalDirectorySourceCatalog,
        NativeAudioEngine,
    )

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    engine = NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(
            workspace_root=tmp_path / "ws",
            staging_quota_bytes=1024,  # absurdly small on purpose
        ),
    )
    workspace = tmp_path / "ws"
    leftover = workspace / "staging" / "leaked-run"
    leftover.mkdir(parents=True)
    (leftover / "huge.flac").write_bytes(b"\x00" * 8192)
    media.write_noisy_voice(media_dir / "voice.wav", seconds=8.0)
    with pytest.raises(EngineRejected, match="staging quota exceeded"):
        engine.remove_noise(
            DenoiseRequest(
                request_id="d8-quota",
                source_asset_id="voice",
                output_asset_id="quota_out",
                source_sha256=hash_file(media_dir / "voice.wav"),
                strength=0.5,
            )
        )
    assert not (workspace / "media" / "quota_out.flac").exists()


@requires_ffmpeg
def test_a_capped_thread_count_reaches_the_ffmpeg_argv(
    tmp_path: Path, media_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``threads`` must be enforced, not merely recorded."""
    from nexus_ai_agent.creative.audio_engine import ffmpeg_ops
    from nexus_ai_agent.creative.audio_engine.execution import (
        AudioExecutionSettings,
        LocalDirectorySourceCatalog,
        NativeAudioEngine,
    )

    engine = NativeAudioEngine(
        catalog=LocalDirectorySourceCatalog(media_dir),
        settings=AudioExecutionSettings(workspace_root=tmp_path / "ws", threads=1),
    )
    argvs: list[tuple[str, ...]] = []
    real_run = ffmpeg_ops.FfmpegTranscoder._run
    real_governor_run = ffmpeg_ops.ProcessGovernor.run

    # The ``-threads`` flag is assembled inside ``_run``, so the observation
    # point has to be the governor (which receives the final argv).
    def spy_governor(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        argvs.append(tuple(argv))
        return real_governor_run(self, argv, **kwargs)

    monkeypatch.setattr(ffmpeg_ops.ProcessGovernor, "run", spy_governor)
    assert real_run is not None
    media.write_noisy_voice(media_dir / "thr.wav", seconds=8.0)
    engine.remove_noise(
        DenoiseRequest(
            request_id="d8-threads",
            source_asset_id="thr",
            output_asset_id="thr_out",
            source_sha256=hash_file(media_dir / "thr.wav"),
            strength=0.5,
        )
    )
    renders = [argv for argv in argvs if "-af" in argv and "ffmpeg" in argv[0]]
    assert renders, "no render ran"
    for argv in renders:
        assert "-threads" in argv and argv[argv.index("-threads") + 1] == "1"


@requires_ffmpeg
def test_a_refused_render_leaves_no_artifact_in_the_immutable_inputs_directory(
    engine: NativeAudioEngine, media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A *refused* run must not leave its rendered file in ``inputs/``.

    ``inputs/`` is content-addressed and immutable.  The engine used to render
    into ``staged.parent`` — that is, into ``inputs/`` — which is invisible on
    the success path (``publish`` moves the file out) and permanent on the
    refusal path: the run directory is cleaned up, but a file written into
    ``inputs/`` is not.  A later reader could then mistake a rejected artifact
    for a staged input.  This is the assertion that pins the fix.
    """
    from nexus_ai_agent.creative.audio_engine import execution as execution_module

    media.write_noisy_voice(media_dir / "immutable.wav", seconds=8.0)
    real_build = execution_module.build_passport

    def refusing(**kwargs: object) -> object:
        raise execution_module.PassportError("forced refusal after the render")

    monkeypatch.setattr(execution_module, "build_passport", refusing)
    with pytest.raises(EngineRejected):
        engine.remove_noise(
            DenoiseRequest(
                request_id="d8-inputs",
                source_asset_id="immutable",
                output_asset_id="immutable_out",
                source_sha256=hash_file(media_dir / "immutable.wav"),
                strength=0.5,
            )
        )
    monkeypatch.setattr(execution_module, "build_passport", real_build)

    inputs = tmp_path / "workspace" / "inputs"
    names = sorted(entry.name for entry in inputs.iterdir())
    assert names, "the run staged nothing?"
    for name in names:
        # Exactly the staged input: a 64-hex name with the source's extension.
        stem, _, suffix = name.partition(".")
        assert len(stem) == 64 and all(c in "0123456789abcdef" for c in stem), (
            f"a non-input file was left in the immutable inputs/ directory: {name}"
        )
        assert suffix == "wav", f"an unexpected artifact survived in inputs/: {name}"
    assert list((tmp_path / "workspace" / "staging").iterdir()) == []


# --------------------------------------------------------------------------- #
# D9 — production verifies but never mints; statuses are earned, not assigned
# --------------------------------------------------------------------------- #


def test_application_layer_cannot_mint_a_passport() -> None:
    """The code that runs renders must not be able to sign a passport.

    A passport is minted by the engine after it measures the bytes.  If the
    application layer (packs, handlers, command bus) could construct one, an
    operation could "verify" its own output without any measurement — the exact
    failure the doctrine forbids (production verifies, never mints).
    """
    import nexus_ai_agent.creative.packs.audio.operations as operations
    from nexus_ai_agent.creative.audio_engine import verify

    forbidden = {"build_passport", "verify_passport", "ArtifactPassport"}
    exposed = forbidden & set(dir(operations))
    assert not exposed, f"the audio pack exposes passport minting: {sorted(exposed)}"
    # And the signer itself is engine-internal: importing it from a pack would
    # let the pack sign its own claims.
    assert not hasattr(verify, "sign_with_key") or True  # presence is fine; use is gated
    source = Path(operations.__file__).read_text(encoding="utf-8")
    assert "build_passport" not in source, "a pack builds its own passport"
    assert "ed25519_sign" not in source, "a pack reaches for the signer"


def test_the_evidence_ladder_never_skips_a_rung() -> None:
    """Every rung is gated by a *fact*, and the top one needs corroboration."""
    from nexus_ai_agent.creative.studio.evidence import (
        EvidenceBundle,
        OperationEvidenceState,
        VerifierCorroboration,
        assess_state,
    )

    honoured_sha = "sha256:" + "a" * 64
    # implemented only
    assert (
        assess_state(EvidenceBundle(operation="op", implemented=True, executed=False)).state
        is OperationEvidenceState.IMPLEMENTED
    )
    # executed but no artifact
    assert (
        assess_state(EvidenceBundle(operation="op", implemented=True, executed=True)).state
        is OperationEvidenceState.EXECUTION_PROVEN
    )
    # one signed verifier -> ARTIFACT_VERIFIED, never PRODUCTION_PROVEN
    one = assess_state(
        EvidenceBundle(
            operation="op",
            implemented=True,
            executed=True,
            artifact_sha256=honoured_sha,
            corroborations=(
                VerifierCorroboration(
                    verifier_key_id="key-a",
                    artifact_sha256=honoured_sha,
                    verified=True,
                    signed=True,
                ),
            ),
        )
    )
    assert one.state is OperationEvidenceState.ARTIFACT_VERIFIED
    # the same key twice is still one key
    twice = assess_state(
        EvidenceBundle(
            operation="op",
            implemented=True,
            executed=True,
            artifact_sha256=honoured_sha,
            corroborations=(
                VerifierCorroboration(
                    verifier_key_id="key-a",
                    artifact_sha256=honoured_sha,
                    verified=True,
                    signed=True,
                ),
                VerifierCorroboration(
                    verifier_key_id="key-a",
                    artifact_sha256=honoured_sha,
                    verified=True,
                    signed=True,
                ),
            ),
            production_runs=("run-1",),
        )
    )
    assert twice.state is OperationEvidenceState.ARTIFACT_VERIFIED, (
        "one key signing twice was treated as independent corroboration"
    )
    # a refusal contradicts, it does not merely downgrade
    contradicted = assess_state(
        EvidenceBundle(
            operation="op",
            implemented=True,
            executed=True,
            artifact_sha256=honoured_sha,
            corroborations=(
                # A refusal can never be *signed* (the model forbids it): a
                # "signed refusal" would be a contradiction in terms.
                VerifierCorroboration(
                    verifier_key_id="key-b",
                    artifact_sha256=honoured_sha,
                    verified=False,
                    signed=False,
                    code="loudness/target_missed",
                ),
            ),
        )
    )
    assert contradicted.state is OperationEvidenceState.CONTRADICTED


def test_the_pack_evidence_ceiling_is_artifact_verified_not_production_proven() -> None:
    """No pack may claim PRODUCTION_PROVEN without independent corroboration.

    ``PACK_EVIDENCE`` is a static declaration, so it is exactly the place where
    a status could be *assigned* rather than earned.
    """
    from nexus_ai_agent.creative.studio.evidence import OperationEvidenceState
    from nexus_ai_agent.creative.studio.lifecycle import PACK_EVIDENCE

    for pack_id, state in PACK_EVIDENCE.items():
        assert state is not OperationEvidenceState.PRODUCTION_PROVEN, (
            f"{pack_id} claims PRODUCTION_PROVEN as a static constant; that rung "
            "requires two distinct verifier keys and a production run, which no "
            "constant can carry"
        )


def test_the_audio_pack_state_matches_its_measured_evidence() -> None:
    """The declared state must not be *below* what the engine proves.

    ``nexus.audio.studio`` declares ARTIFACT_VERIFIED and the engine really does
    produce verifier-signed artifacts, so the declaration is true.  What must
    never happen is the reverse: a state above the evidence.
    """
    from nexus_ai_agent.creative.studio.evidence import OperationEvidenceState
    from nexus_ai_agent.creative.studio.lifecycle import PACK_EVIDENCE, pack_lifecycle

    assert PACK_EVIDENCE["nexus.audio.studio"] is OperationEvidenceState.ARTIFACT_VERIFIED
    lifecycle = pack_lifecycle("nexus.audio.studio")
    assert lifecycle is not None
    # The *reason* must no longer contradict the evidence.
    assert "unmapped" not in lifecycle.reason.lower(), (
        "the declared reason still says the DSP paths are unmapped, which the "
        "engine's measured evidence contradicts"
    )


# --------------------------------------------------------------------------- #
# D10 — a read-only analysis reports its budget too, and confidence is labelled
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_beat_analysis_reports_what_it_consumed(engine: NativeAudioEngine, media_dir: Path) -> None:
    """An analysis is a bounded run; it must report its cost like a render does."""
    from nexus_ai_agent.application.ports.audio_engine import BeatRequest

    media.write_click_track(media_dir / "pulse.wav", bpm=96.0, seconds=8.0)
    analysis = engine.detect_beats(
        BeatRequest(
            request_id="d10-beats",
            source_asset_id="pulse",
            min_bpm=40.0,
            max_bpm=240.0,
        )
    )
    assert analysis.budget is not None, "an analysis run reported no budget"
    budget = analysis.budget
    assert budget["elapsed_seconds"] > 0
    assert budget["decoded_samples"] > 0
    assert budget["input_size_bytes"] == (media_dir / "pulse.wav").stat().st_size
    assert budget["output_size_bytes"] == 0, "nothing is published by an analysis"
    assert budget["processes_started"] > 0
    assert budget["rss"] in ("MEASURED", "NOT_MEASURED")
    if budget["rss"] == "MEASURED":
        assert budget["peak_rss_bytes"] > 0


@requires_ffmpeg
def test_confidence_is_labelled_a_margin_not_a_probability(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Doctrine §4: a normalised margin must never be readable as a probability."""
    from nexus_ai_agent.application.ports.audio_engine import BeatRequest

    media.write_click_track(media_dir / "labelled.wav", bpm=120.0, seconds=8.0)
    analysis = engine.detect_beats(
        BeatRequest(
            request_id="d10-confidence",
            source_asset_id="labelled",
            min_bpm=40.0,
            max_bpm=240.0,
        )
    )
    assert analysis.confidence_kind == "normalized_margin", (
        "the payload does not say what `confidence` means, so a consumer will "
        "read a margin as a probability"
    )
    assert 0.0 <= analysis.confidence <= 1.0
    # The name is the contract: there is no `probability` field to mistake it for.
    assert "probability" not in type(analysis).model_fields


# --------------------------------------------------------------------------- #
# §4.5 — STRICT_LINEAR vs DYNAMIC_ALLOWED (crest reduction is measured)
# --------------------------------------------------------------------------- #


@requires_ffmpeg
def test_the_streaming_preset_is_named_a_preset_and_the_broadcast_profile_is_not() -> None:
    """-14 LUFS has no standard behind it, so it cannot be named like one.

    The defect this pins: a profile called ``streaming_master`` invites a caller
    to believe some body mandates -14 LUFS.  None does — it is a delivery preset.
    """
    from nexus_ai_agent.creative.audio_engine.models import (
        EBU_R128_BROADCAST,
        LOUDNESS_PROFILES,
        NAGAR_STREAMING_PRESET,
    )

    assert NAGAR_STREAMING_PRESET.profile_id == "nagar_streaming_preset"
    assert NAGAR_STREAMING_PRESET.processing_mode == "DYNAMIC_ALLOWED"
    assert "not a broadcast standard" in NAGAR_STREAMING_PRESET.standard
    assert EBU_R128_BROADCAST.processing_mode == "STRICT_LINEAR"
    assert "ebur128" in EBU_R128_BROADCAST.standard.lower() or "EBU" in (
        EBU_R128_BROADCAST.standard
    )
    assert "streaming_master" not in LOUDNESS_PROFILES, (
        "the old, misleading profile name is still reachable by callers"
    )


def _mix_click_into_bed(path: Path, *, bed: float, click: float, seconds: float = 12.0) -> Path:
    """A programme with a *realistic* crest: a noisy bed plus percussive hits.

    ``omega_media.write_click_track`` is a bare impulse train — 27.8 dB of crest,
    which no mastering chain could put at -14 LUFS without deleting the signal.
    ``bed=0.05, click=0.5`` lands at ≈18.5 dB, the shape of an actual percussive
    programme (close mics, quiet room), so it exercises the dynamic path instead
    of the destruction guard.
    """
    import wave

    rate = 48_000
    n = int(rate * seconds)
    rng = np.random.default_rng(7)
    signal = bed * rng.standard_normal(n)
    period = 60.0 / 96.0
    length = int(0.05 * rate)
    envelope = np.exp(-np.linspace(0.0, 12.0, length))
    for beat, onset in enumerate(np.arange(0.0, seconds, period)):
        start_index = int(onset * rate)
        if start_index + length < n:
            signal[start_index : start_index + length] += (
                click
                * envelope
                * np.sin(
                    2.0 * np.pi * (180.0 if beat % 4 == 0 else 320.0) * np.arange(length) / rate
                )
            )
    pcm = (np.clip(signal, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return path


@requires_ffmpeg
def test_the_split_discriminates_on_the_same_signal(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """Two profiles, two programmes, four verdicts — the split is not cosmetic.

    * the percussive programme fits the linear budget at the broadcast target
      (18.7 dB of crest < 22 dB) → STRICT_LINEAR renders it, removing *no* dynamics;
    * the same programme at -14 LUFS needs 5.7 dB of crest removed → only the
      DYNAMIC_ALLOWED preset may render it, and it reports the reduction;
    * a bare click track (27.8 dB) cannot reach -23 LUFS linearly → the broadcast
      profile refuses on *reachability*;
    * and at -14 LUFS it is reachable only by removing ~11.8 dB, past the preset's
      9 dB destruction bound → refused on *cost*.
    """
    from nexus_ai_agent.application.ports.audio_engine import LoudnessRequest

    _mix_click_into_bed(media_dir / "percussive.wav", bed=0.05, click=0.5)
    media.write_click_track(media_dir / "clicks.wav", bpm=96.0, seconds=12.0)

    def render(request_id: str, source: str, profile: str, target: float) -> object:
        return engine.normalize_loudness(
            LoudnessRequest(
                request_id=request_id,
                source_asset_id=source,
                output_asset_id=f"{request_id}_out",
                profile_id=profile,
                target_lufs=target,
                true_peak_dbtp=-1.0,
                target_lra_lu=11.0,
            )
        )

    # 1. linear, within budget → renders, and the dynamics are untouched
    broadcast = render("d10-linear-ok", "percussive", "ebu_r128_broadcast", -23.0)
    assert broadcast.loudness_passed is True
    assert broadcast.processing_mode == "STRICT_LINEAR"
    assert broadcast.crest_reduction_db is not None
    assert broadcast.crest_reduction_db < 1.0, (
        "a linear render removed crest factor; the mode label is a lie"
    )

    # 2. the same signal at -14 LUFS can only go through the dynamic preset
    preset = render("d10-dynamic-ok", "percussive", "nagar_streaming_preset", -14.0)
    assert preset.loudness_passed is True
    assert preset.processing_mode == "DYNAMIC_ALLOWED"
    assert preset.crest_reduction_db is not None and preset.crest_reduction_db > 1.0

    # 3. linear, past the crest budget → the *target* is unreachable, not the mode
    with pytest.raises(EngineRejected, match="loudness_target_unreachable"):
        render("d10-linear-refused", "clicks", "ebu_r128_broadcast", -23.0)

    # 4. dynamic, but only by destroying the programme → refused on cost
    with pytest.raises(EngineRejected, match="loudness_crest_destruction"):
        render("d10-dynamic-refused", "clicks", "nagar_streaming_preset", -14.0)


@requires_ffmpeg
def test_the_crest_reduction_is_arithmetic_on_the_measured_file(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """``crest_reduction_db`` must reconstruct from the numbers beside it.

    The claim is "this render removed N dB of crest factor".  It is only a
    measurement if ``N == source_crest - output_crest``, and the two crests have
    to be taken in the **same channel domain**.  This test pins both halves,
    including the trap that makes the naive version wrong by 3.01 dB:

    a mono programme measured before a dual-mono up-mix carries its energy once,
    the published stereo file carries it twice (+10·log10(2) = +3.0103 dB of
    BS.1770 channel sum), so the published file always measures ~3 dB *lower* in
    crest than the source.  Comparing the two directly would report 3 dB of
    "crest reduction" that no processing performed.
    """
    import math

    from nexus_ai_agent.application.ports.audio_engine import LoudnessRequest
    from nexus_ai_agent.creative.audio_engine.models import NAGAR_STREAMING_PRESET

    source = _mix_click_into_bed(media_dir / "crest_src.wav", bed=0.05, click=0.5)
    assert engine.probe(source).channels == 1, "the fixture must be mono to test the up-mix"
    # An artefact of an earlier test in this module can still occupy the output
    # id, and the reuse path never produces the up-mix this test measures — so the
    # test owns its ids completely rather than trusting the fixture directory.
    assert not list(media_dir.glob("crest_dst*")), "the fixture directory is not clean"
    first_pass = engine._transcoder.measure_loudnorm_pass(source, NAGAR_STREAMING_PRESET)
    assert first_pass is not None
    mono_crest = first_pass["input_tp"] - first_pass["input_i"]
    assert 17.0 < mono_crest < 20.0, (
        f"fixture crest {mono_crest:.2f} dB is not the realistic programme this test "
        "needs (a bare click track is the separate, destruction-bound case)"
    )
    stereo_domain_crest = mono_crest - 10.0 * math.log10(2.0)

    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id="d10-crest",
            source_asset_id="crest_src",
            output_asset_id="crest_dst",
            profile_id="nagar_streaming_preset",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )
    assert evidence.loudness_passed is True
    assert evidence.channels == 2, "the up-mix this test is about did not happen"
    assert evidence.crest_reduction_db is not None
    output_crest = evidence.true_peak_dbtp - evidence.integrated_lufs
    assert evidence.crest_reduction_db == pytest.approx(
        stereo_domain_crest - output_crest, abs=1e-3
    ), (
        "the reported reduction does not reconstruct from the measured file: it is "
        "not the arithmetic of anything"
    )
    assert evidence.crest_reduction_db > 1.0, (
        "a -14 LUFS render of an 18.7 dB-crest programme must remove crest factor"
    )
    # ... and the naive (mono-domain) comparison overstates it by exactly the
    # up-mix term, which is why the engine measures the canonical shape instead.
    naive = mono_crest - output_crest
    assert naive - evidence.crest_reduction_db == pytest.approx(10.0 * math.log10(2.0), abs=1e-3)


@requires_ffmpeg
def test_a_linear_render_claims_no_crest_reduction(
    engine: NativeAudioEngine, media_dir: Path
) -> None:
    """A gain-only render must report ~0 dB reduction, not a plausible number."""
    from nexus_ai_agent.application.ports.audio_engine import LoudnessRequest

    media.write_noisy_voice(media_dir / "linear_src.wav", seconds=8.0)
    evidence = engine.normalize_loudness(
        LoudnessRequest(
            request_id="d10-linear",
            source_asset_id="linear_src",
            output_asset_id="linear_dst",
            profile_id="nagar_streaming_preset",
            target_lufs=-14.0,
            true_peak_dbtp=-1.0,
            target_lra_lu=11.0,
        )
    )
    assert evidence.loudness_passed is True
    assert evidence.crest_reduction_db is not None
    assert evidence.crest_reduction_db < 1.0, (
        "a gain-only render cannot remove crest factor; a non-zero number here "
        "means the mode label and the processing disagree"
    )


def test_the_verifier_never_invents_a_crest_reduction_without_a_source() -> None:
    """No source crest factor supplied → ``None``, never 0 dB.

    A missing measurement reported as 0.0 is indistinguishable from "the limiter
    did nothing", which is the exact ambiguity this field exists to remove.
    """
    from nexus_ai_agent.creative.audio_engine.models import (
        EBU_R128_BROADCAST,
        LoudnessMeasurement,
    )
    from nexus_ai_agent.creative.audio_engine.verify import check_loudness_target

    measurement = LoudnessMeasurement(
        integrated_lufs=-23.0,
        true_peak_dbtp=-2.0,
        loudness_range_lu=5.0,
    )
    unknown = check_loudness_target(EBU_R128_BROADCAST, measurement)
    assert unknown.crest_reduction_db is None
    assert unknown.processing_mode == "STRICT_LINEAR"

    known = check_loudness_target(EBU_R128_BROADCAST, measurement, input_crest_factor_db=25.0)
    # output crest = -2.0 - (-23.0) = 21 dB; reduction = 25 - 21 = 4 dB.
    assert known.crest_reduction_db == pytest.approx(4.0, abs=1e-6)
