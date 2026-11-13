"""Reproducible N=3 offline numerical benchmark; no media fixtures or AI services.

Run directly to emit the measured JSON (keep it under ignored ci-artifacts/):
    .venv/bin/python tests/integration/test_audio_engine_stage_b_benchmarks.py

Peak RSS is the kernel-reported high-water mark of THIS BENCHMARK PROCESS,
including Python/imports/all kernels; it is not falsely attributed to one DSP
function or an isolated worker. Timings are observations, not a real-time SLA.
"""

from __future__ import annotations

import json
import platform
import resource
import statistics
import time

import numpy as np

from nexus_ai_agent.creative.audio_engine import ambisonics_binaural_fdn as k1
from nexus_ai_agent.creative.audio_engine import av_subframe_foley_sync as k3
from nexus_ai_agent.creative.audio_engine import psychoacoustic_bark_mastering as k4
from nexus_ai_agent.creative.audio_engine import spectral_voice_restoration as k2


def _band_rt(samples, fs, hz):
    length = 2 * round(6 * fs / hz) + 1
    n = np.arange(length) - (length - 1) / 2
    low, high = hz / np.sqrt(2), min(hz * np.sqrt(2), fs * 0.49)
    fir = (
        2 * high / fs * np.sinc(2 * high / fs * n) - 2 * low / fs * np.sinc(2 * low / fs * n)
    ) * np.hanning(length)
    size = 1 << (samples.shape[1] + length).bit_length()
    filtered = np.fft.irfft(
        np.fft.rfft(samples, n=size, axis=1) * np.fft.rfft(fir, n=size), n=size, axis=1
    )[:, : samples.shape[1] + length - 1]
    return k1.schroeder_rt60(filtered, sample_rate=fs)


def _timing(values):
    return dict(
        n=len(values),
        median_seconds=statistics.median(values),
        min_seconds=min(values),
        max_seconds=max(values),
        spread_seconds=max(values) - min(values),
    )


def _k3_k4_measurement(repeat):
    fs = 16000
    fps = (24, 30000 / 1001, 60)[repeat]
    lag_ms = (-67.35, 4.65, 37.29)[repeat]

    def events(t):
        return sum(
            a * np.exp(-0.5 * ((t - c) / w) ** 2)
            for c, w, a in [(0.7, 0.09, 1), (1.45, 0.12, 0.8), (2.3, 0.09, 0.9)]
        )

    t = np.arange(fs * 3) / fs
    pcm = events(t - lag_ms / 1000) * np.cos(2 * np.pi * 1000 * t)
    visual = events(np.arange(int(3 * fps)) / fps)
    started = time.perf_counter()
    sync = k3.synchronize_audio_to_visual(pcm, visual, sample_rate=fs, frame_rate=fps)
    sync_seconds = time.perf_counter() - started
    started = time.perf_counter()
    onsets = []
    for material in k3.MATERIALS:
        foley = k3.render_collision_foley(
            (k3.FoleyCollision(123, material, contact_seconds=0),),
            length_samples=fs,
            sample_rate=fs,
        )
        onsets.append(int(np.flatnonzero(abs(foley) > 1e-12)[0]))
    foley_seconds = time.perf_counter() - started
    t = np.arange(fs) / fs
    fade = np.sin(np.pi * t) ** 2
    voice = 0.06 * fade * (np.sin(2 * np.pi * 1250 * t) + np.sin(2 * np.pi * 2500 * t))
    music = fade * (
        0.12 * (np.sin(2 * np.pi * 1250 * t) + np.sin(2 * np.pi * 2500 * t))
        + 0.03 * np.sin(2 * np.pi * 160 * t)
        + 0.03 * np.sin(2 * np.pi * 7000 * t)
    )
    sfx = 0.01 * fade * np.sin(2 * np.pi * 2500 * t)
    started = time.perf_counter()
    master = k4.master_dialogue_music_sfx(voice, music, sfx, sample_rate=fs)
    master_seconds = time.perf_counter() - started
    protected_delta = []
    for hz in (160, 7000):
        kernel = np.exp(-2j * np.pi * hz * t)
        protected_delta.append(
            float(
                20
                * np.log10(
                    abs(np.sum(master.masking.music[0] * kernel)) / abs(np.sum(music * kernel))
                )
            )
        )
    return dict(
        sync_seconds=sync_seconds,
        foley_seconds=foley_seconds,
        mastering_seconds=master_seconds,
        frame_rate=fps,
        offset_error_ms=abs(sync.input_offset_ms - lag_ms),
        residual_offset_ms=sync.residual_offset_ms,
        foley_onset_samples=onsets,
        measured_lkfs=master.measurement.lufs,
        measured_true_peak_dbtp=master.measurement.true_peak_dbtp,
        minimum_active_speech_masker_margin_db=float(
            master.masking.output_margin_db[master.masking.active_speech_bands].min()
        ),
        protected_bass_air_delta_db=protected_delta,
    )


def measure_stage_b_benchmarks():
    fdn_times, restoration_times, rotations, rt_bands, metrics = [], [], [], [], []
    sync_master_metrics = []
    for _repeat in range(3):
        b = np.random.default_rng(510).normal(size=(16, 4096))
        rotated = k1.rotate_ambisonics(b, yaw=0.72, pitch=-0.43, roll=0.29)
        rotations.append(float(abs(np.linalg.norm(rotated) - np.linalg.norm(b))))
        started = time.perf_counter()
        reverb = k1.fdn_reverb(np.array([1.0]), sample_rate=48000, tail_seconds=1.8)
        fdn_times.append(time.perf_counter() - started)
        rt_bands.append([_band_rt(reverb.samples, 48000, hz) for hz in (100, 1000, 10000)])
        fs = 16000
        t = np.arange(fs * 3) / fs
        envelope = np.sin(np.pi * np.clip((t - 0.3) / 2.4, 0, 1)) ** 2 * (
            0.65 + 0.35 * np.sin(2 * np.pi * 3 * t) ** 2
        )
        clean = envelope * (
            0.45 * np.sin(2 * np.pi * 180 * t)
            + 0.18 * np.sin(2 * np.pi * 540 * t)
            + 0.1 * np.sin(2 * np.pi * 900 * t)
        )
        noise = np.random.default_rng(42).normal(size=t.size)
        noise *= np.sqrt(np.mean(clean**2) / (10**0.5 * np.mean(noise**2)))
        threshold = float(np.quantile(np.abs(clean + noise), 0.85))
        clipped = np.clip(clean + noise, -threshold, threshold)
        started = time.perf_counter()
        declipped = k2.consistent_declip(
            clipped, sample_rate=fs, threshold=threshold, bandwidth_hz=1800
        )
        output = k2.mmse_lsa_denoise(
            declipped.samples, sample_rate=fs, noise_reference=noise[: fs // 2]
        )
        restoration_times.append(time.perf_counter() - started)
        measured = k2.restoration_metrics(clean, clipped, output.samples, sample_rate=fs)
        metrics.append(
            dict(
                snr_gain_db=measured.snr_gain_db,
                snr_after_db=measured.snr_after_db,
                input_crest_db=measured.input_crest_db,
                output_crest_db=measured.output_crest_db,
                reference_crest_db=measured.reference_crest_db,
                input_modulation_correlation=measured.input_modulation_correlation,
                output_modulation_correlation=measured.output_modulation_correlation,
                preclip_noise_snr_db=float(10 * np.log10(np.sum(clean**2) / np.sum(noise**2))),
                clipped_sample_fraction=float(np.mean(abs(clipped) >= threshold)),
            )
        )
        sync_master_metrics.append(_k3_k4_measurement(_repeat))
    return dict(
        python=platform.python_version(),
        numpy=np.__version__,
        repeats=3,
        fdn=_timing(fdn_times),
        restoration=_timing(restoration_times),
        sync=_timing([m["sync_seconds"] for m in sync_master_metrics]),
        foley_six_materials=_timing([m["foley_seconds"] for m in sync_master_metrics]),
        bark_mastering=_timing([m["mastering_seconds"] for m in sync_master_metrics]),
        sync_master_metrics=sync_master_metrics,
        rotation_norm_errors=rotations,
        rt60_low_mid_high_seconds=rt_bands,
        restoration_metrics=metrics,
        peak_rss_bytes=int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
        peak_rss_scope="benchmark-process (Linux ru_maxrss; imports and all kernels included)",
        material="deterministic AM voiced surrogate, not natural-speech/listening validation",
    )


def _assert_report(report):
    assert report["repeats"] >= 3
    assert max(report["rotation_norm_errors"]) < 1e-12
    for bands in report["rt60_low_mid_high_seconds"]:
        np.testing.assert_allclose(bands, [0.8, 0.6, 0.4], rtol=0.08)
    for metrics in report["restoration_metrics"]:
        assert metrics["snr_gain_db"] >= 9
        assert metrics["output_modulation_correlation"] > metrics["input_modulation_correlation"]
    for metrics in report["sync_master_metrics"]:
        assert metrics["offset_error_ms"] <= 0.5
        assert abs(metrics["residual_offset_ms"]) <= 0.5
        assert metrics["foley_onset_samples"] == [123] * 6
        assert abs(metrics["measured_lkfs"] + 24) <= 0.1
        assert metrics["measured_true_peak_dbtp"] <= -1
        assert metrics["minimum_active_speech_masker_margin_db"] >= 5.95
        assert max(abs(delta) for delta in metrics["protected_bass_air_delta_db"]) < 0.01
    assert report["peak_rss_bytes"] > 0


def test_stage_b_numerical_benchmarks_are_measured_not_declared():
    report = measure_stage_b_benchmarks()
    _assert_report(report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    report = measure_stage_b_benchmarks()
    _assert_report(report)
    print(json.dumps(report, indent=2, sort_keys=True))
