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


def measure_stage_b_benchmarks():
    fdn_times, restoration_times, rotations, rt_bands, metrics = [], [], [], [], []
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
    return dict(
        python=platform.python_version(),
        numpy=np.__version__,
        repeats=3,
        fdn=_timing(fdn_times),
        restoration=_timing(restoration_times),
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
    assert report["peak_rss_bytes"] > 0


def test_stage_b_numerical_benchmarks_are_measured_not_declared():
    report = measure_stage_b_benchmarks()
    _assert_report(report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    report = measure_stage_b_benchmarks()
    _assert_report(report)
    print(json.dumps(report, indent=2, sort_keys=True))
