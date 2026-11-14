"""N=3 micro-benchmarks for the scene-imaging kernels (Wave 3, Milestone 5).

These are wall-clock samples on a synthetic workload, not production throughput
or tail-latency claims. Peak memory is measured with ``tracemalloc`` on the
kernel call only (``ru_maxrss`` is a whole-process high-water mark and would
measure the interpreter too).
"""

from __future__ import annotations

import statistics
import time
import tracemalloc

import numpy as np

from nexus_ai_agent.creative.composition import scene_imaging as k

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


def test_benchmark_homography_rectification() -> None:
    img = np.random.default_rng(0).random((256, 256, 3)).astype(np.float32)
    quad = [[10, 12], [245, 8], [250, 244], [8, 248]]
    times, peak = _timed(k.homography_perspective_rectification, img, quad)
    assert statistics.median(times) < 2000.0
    assert peak < 200 * 1024 * 1024  # < 200 MB for a 256x256 rectification


def test_benchmark_gaussian_blur_scales() -> None:
    small = np.random.default_rng(1).random((64, 64, 3)).astype(np.float32)
    large = np.random.default_rng(2).random((512, 512, 3)).astype(np.float32)
    t_small, _ = _timed(k.gaussian_blur, small, 3.0)
    t_large, _ = _timed(k.gaussian_blur, large, 3.0)
    assert statistics.median(t_large) >= statistics.median(t_small) - 1.0


def test_benchmark_inpaint_and_flow() -> None:
    img = np.random.default_rng(3).random((128, 128, 3)).astype(np.float32)
    m = np.zeros((128, 128), np.float32)
    m[40:90, 40:90] = 1.0
    t_inpaint, _ = _timed(k.remove_object_inpaint, img, m, iters=30)
    assert statistics.median(t_inpaint) < 3000.0
    small = np.random.default_rng(4).random((32, 32, 3)).astype(np.float32)
    t_flow, _ = _timed(k.motion_vector_flow, small, small, window=5, max_shift=4)
    assert statistics.median(t_flow) < 3000.0
