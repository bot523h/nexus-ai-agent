"""N=3 micro-benchmarks for the portrait-imaging kernels (Wave 4, Milestone 5)."""

from __future__ import annotations

import statistics
import time
import tracemalloc

import numpy as np

from nexus_ai_agent.creative.composition import portrait_imaging as p

N = 3


def _timed(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    times = []
    peak = 0
    for _ in range(N):
        tracemalloc.start()
        start = time.perf_counter()
        fn(*args, **kwargs)
        times.append((time.perf_counter() - start) * 1000.0)
        _, pk = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak = max(peak, pk)
    return times, peak


def test_benchmark_skintone_harmonization() -> None:
    img = np.random.default_rng(0).random((256, 256, 3)).astype(np.float32)
    m = np.zeros((256, 256), np.float32)
    m[60:200, 60:200] = 1.0
    times, peak = _timed(p.skintone_harmonization, img, m, strength=0.5)
    assert statistics.median(times) < 3000.0
    assert peak < 300 * 1024 * 1024


def test_benchmark_lab_round_trip() -> None:
    img = np.random.default_rng(1).random((256, 256, 3)).astype(np.float32)
    times, _ = _timed(lambda a: p.lab_to_srgb(p.srgb_to_lab(a)), img)
    assert statistics.median(times) < 3000.0


def test_benchmark_bokeh_and_relight() -> None:
    img = np.random.default_rng(2).random((128, 128, 3)).astype(np.float32)
    d = np.random.default_rng(3).random((128, 128)).astype(np.float32)
    t_bokeh, _ = _timed(p.depth_of_field_bokeh, img, d, focus=0.5, blades=6)
    assert statistics.median(t_bokeh) < 3000.0
    t_relight, _ = _timed(p.facial_relighting_gradient, img)
    assert statistics.median(t_relight) < 1000.0
