"""Independent oracles added while auditing the inherited mutation evidence.

A redundant guard or a neighbouring helper can hide a missing check. These
fixtures reach the actual policy boundary, or use arithmetic inputs that
separate the claimed numerical contract from the rejected alternative.
"""

import math
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine.execution import build_native_audio_engine
from nexus_ai_agent.creative.audio_engine.forensic_qc import deess_sibilance_band
from nexus_ai_agent.creative.audio_engine.loudness_bs1770 import gated_loudness, loudness_range
from nexus_ai_agent.creative.audio_engine.process import CancelToken, ProcessCancelled
from nexus_ai_agent.creative.audio_engine.wiener_denoise import estimate_noise_psd
from nexus_ai_agent.creative.audio_engine.workspace import AtomicPublishError, RuntimeWorkspace
from nexus_ai_agent.creative.slideshow import audio as tempo


def test_media_root_is_derived_from_workspace_when_cwd_is_already_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Changing cwd *after* composition cannot distinguish a cached cwd-derived
    # root from a workspace-derived one. Compose while cwd is already elsewhere.
    monkeypatch.delenv("NEXUS_AUDIO_MEDIA_ROOT", raising=False)
    workspace = tmp_path / "declared-deployment" / "runtime"
    foreign = tmp_path / "unrelated-process-directory"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    engine = build_native_audio_engine(
        workspace_root=workspace, media_root="", signing_key=b"\x01" * 32
    )
    assert engine._catalog.directory == workspace.parent / "media"
    assert engine._catalog.directory != Path.cwd() / "media"
    assert engine._settings.workspace_root == workspace


def test_post_builder_cancellation_precedes_verification_and_any_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "inputs"
    media.mkdir()
    (media / "source.wav").write_bytes(b"opaque immutable input; no DSP is under test")
    root = tmp_path / "workspace"
    engine = build_native_audio_engine(workspace_root=root, media_root=media)
    token = CancelToken()
    monkeypatch.setattr(engine, "_engine_identity", lambda: {"backend": "boundary-control"})

    def verification_must_not_start(**kwargs):
        pytest.fail("post-builder cancellation reached verification")

    monkeypatch.setattr(engine, "_verify", verification_must_not_start)

    def builder(staged, workspace, cancel, run_dir):
        assert cancel is token
        produced = run_dir / "private.flac"
        produced.write_bytes(b"private renderer result")
        token.cancel("operator cancelled at the last builder boundary")
        return produced

    with pytest.raises(ProcessCancelled, match="cancelled"):
        engine._derive(
            request_id="cancel-boundary",
            operation="audio.remove_noise",
            source_asset_id="source",
            output_asset_id="never-public",
            expected_source_sha256=None,
            parameters={},
            loudness_profile=None,
            transaction_id=None,
            continuity_lock_hash=None,
            builder=builder,
            cancel=token,
        )
    assert not (root / "media" / "never-public.flac").exists()
    assert list((root / "staging").iterdir()) == []


def test_explicitly_protected_path_is_reserved_even_before_it_has_an_inode(tmp_path: Path) -> None:
    # An existing protected file is ALSO caught by the later inode-alias guard.
    # A declared-but-absent path isolates the explicit name protection contract.
    workspace = RuntimeWorkspace(tmp_path / "workspace").prepare()
    target = workspace.media_path("reserved-input", suffix=".flac")
    assert not target.exists()
    with workspace.run_directory("reservation") as run:
        produced = run / "candidate.flac"
        produced.write_bytes(b"private bytes")
        with pytest.raises(AtomicPublishError, match="run input"):
            workspace.publish(produced, target, replace=True, protected=(target,))
        assert produced.read_bytes() == b"private bytes"
        assert not target.exists()


def test_nearly_equal_onset_strengths_do_not_manufacture_a_slower_backbeat() -> None:
    # 5% accent variation is not a weak off-beat subdivision. Use exact onset
    # times to test the octave decision independently of the onset detector.
    hop = 0.01
    onsets = np.arange(0.25, 8.0, 0.25)
    weights = np.ones(onsets.size)
    weights[::2] = 0.95
    envelope = np.zeros(800)
    envelope[np.rint(onsets / hop).astype(int)] = weights
    period, relation, _clarity = tempo._resolve_octave(
        0.25,
        envelope=envelope,
        hop_seconds=hop,
        min_period=0.2,
        max_period=1.0,
        onsets=onsets,
        weights=weights,
    )
    assert period == 0.25 and relation == "as_is"


def test_tempo_prior_has_a_log_frequency_shape_not_a_constant_vote() -> None:
    assert tempo._tempo_prior(120.0) == pytest.approx(1.0)
    assert tempo._tempo_prior(60.0) == pytest.approx(math.exp(-0.5), abs=1e-12)
    assert tempo._tempo_prior(240.0) == pytest.approx(math.exp(-0.5), abs=1e-12)
    assert tempo._tempo_prior(30.0) < tempo._tempo_prior(60.0) < tempo._tempo_prior(120.0)
    assert tempo._tempo_prior(0.0) == 0.0


@pytest.mark.parametrize("intervals", [[], [0.5]])
def test_no_interval_population_cannot_claim_nonzero_gum_precision(intervals) -> None:
    assert tempo.tempo_uncertainty(np.asarray(intervals), mean_period=0.5, hop_seconds=0.01) == (
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def test_absolute_gate_is_applied_before_computing_the_relative_mean() -> None:
    floor = 10 ** ((-70.0 + 0.691) / 10.0)
    # Many inaudible blocks must not lower the relative threshold enough to
    # admit the 1.5x-floor block. Only the 100x-floor block survives both gates.
    values = np.r_[100 * floor, 1.5 * floor, np.full(100, floor / 1000)]
    measured, count = gated_loudness(values)
    assert count == 1
    assert measured == pytest.approx(-50.0, abs=1e-12)


def test_lra_uses_three_second_levels_not_instantaneous_gating_blocks() -> None:
    # Each three-second window contains 15 quiet and 15 loud blocks: constant
    # short-term power, hence exactly zero LRA despite a 10 dB block spread.
    alternating = np.tile([0.001, 0.01], 30)
    assert loudness_range(alternating, hop_seconds=0.1) == pytest.approx(0.0, abs=1e-10)


def test_noise_psd_is_a_time_percentile_not_the_quietest_frame() -> None:
    # All spectral neighbours agree, so only the temporal statistic matters.
    # The median of ten frames with powers 1..10 is 5.5, not the minimum 1.
    power = np.repeat(np.arange(1, 11, dtype=np.float64)[:, None], 17, axis=1)
    measured = estimate_noise_psd(power, percentile=50.0, bias_correction_db=0.0)
    np.testing.assert_allclose(measured, 5.5, rtol=0, atol=1e-12)


def test_active_deessing_preserves_the_low_tone_when_the_first_frame_is_sibilant() -> None:
    # Pure low-band material never engages a de-esser, so it cannot detect an
    # erroneous whole-signal gain. This fixture engages from the very first block.
    rate = 24_000
    time = np.arange(rate) / rate
    source = 0.2 * np.sin(2 * np.pi * 700 * time) + 0.4 * np.sin(2 * np.pi * 6500 * time)
    result = deess_sibilance_band(source, sample_rate=rate)
    before, after = np.fft.rfft(source), np.fft.rfft(result.samples)
    assert abs(after[6500]) / abs(before[6500]) < 0.3
    assert abs(after[700]) / abs(before[700]) == pytest.approx(1.0, abs=1e-10)
