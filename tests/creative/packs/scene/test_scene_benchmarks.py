"""N=5 micro-benchmarks for the scene-imaging kernels (Wave 3, Milestone 5).

These are wall-clock samples on a synthetic workload, not production throughput
or tail-latency claims.  The measurement discipline — two discarded warm-up
rounds, a ``gc.collect()`` before every measured round, five measured rounds, and
a **median** acceptance statistic reported together with the MAD, the minimum and
the peak allocation — lives in ``tests/creative/packs/conftest.py`` and is shared
with the portrait benchmarks.

The median (not the minimum) is the acceptance statistic on purpose: a minimum
can hide a systematic regression behind one lucky round, while a median cannot.
The MAD is printed in the failure message, so a noisy machine is visible instead
of being silently averaged away.

Peak memory is measured with ``tracemalloc`` on the kernel call only
(``ru_maxrss`` is a whole-process high-water mark and would measure the
interpreter too).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import numpy as np

from nexus_ai_agent.creative.composition import scene_imaging as k


class Measurement(Protocol):
    """The slice of the shared measurement the assertions read (see conftest)."""

    @property
    def median(self) -> float: ...

    @property
    def minimum(self) -> float: ...

    @property
    def peak_bytes(self) -> int: ...

    def describe(self) -> str: ...


Measure = Callable[..., Measurement]


def test_benchmark_homography_rectification(measure: Measure) -> None:
    img = np.random.default_rng(0).random((256, 256, 3)).astype(np.float32)
    quad = [[10, 12], [245, 8], [250, 244], [8, 248]]
    m = measure(k.homography_perspective_rectification, img, quad, label="homography")
    assert m.median < 2000.0, m.describe()
    assert m.peak_bytes < 200 * 1024 * 1024, m.describe()  # < 200 MB for a 256x256 rectification


def test_benchmark_gaussian_blur_scales(measure: Measure) -> None:
    small = np.random.default_rng(1).random((64, 64, 3)).astype(np.float32)
    large = np.random.default_rng(2).random((512, 512, 3)).astype(np.float32)
    t_small = measure(k.gaussian_blur, small, 3.0, label="blur-64")
    t_large = measure(k.gaussian_blur, large, 3.0, label="blur-512")
    # A scaling relation, not a budget: the larger image may not be faster.
    assert t_large.median >= t_small.median - 1.0, f"{t_small.describe()} | {t_large.describe()}"


def test_benchmark_inpaint_and_flow(measure: Measure) -> None:
    img = np.random.default_rng(3).random((128, 128, 3)).astype(np.float32)
    mask = np.zeros((128, 128), np.float32)
    mask[40:90, 40:90] = 1.0
    t_inpaint = measure(k.remove_object_inpaint, img, mask, label="inpaint", iters=30)
    assert t_inpaint.median < 3000.0, t_inpaint.describe()
    small = np.random.default_rng(4).random((32, 32, 3)).astype(np.float32)
    t_flow = measure(k.motion_vector_flow, small, small, label="flow", window=5, max_shift=4)
    assert t_flow.median < 3000.0, t_flow.describe()
