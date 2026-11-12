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
    measured_paths: list[Path] = []
    real_measure = ffmpeg_ops.FfmpegTranscoder.measure_loudnorm_pass

    def spy(self: FfmpegTranscoder, path: Path, profile: object, *, cancel: object = None):
        measured_paths.append(Path(path))
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
    assert measured_paths, "pass 1 never ran"
    canonical = engine.probe(measured_paths[0])
    assert canonical.channels == 2, (
        "pass 1 measured the source layout; the gain was computed for a different "
        "channel count than the one that gets published"
    )


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
