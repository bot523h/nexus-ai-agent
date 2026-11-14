"""N=5 micro-benchmarks for the portrait-imaging kernels (Wave 4, Milestone 5).

Same measurement discipline as ``tests/creative/packs/scene`` (shared through
``tests/creative/packs/conftest.py``): two discarded warm-up rounds, a
``gc.collect()`` before every measured round, five measured rounds, a **median**
acceptance statistic, and a failure message that carries the raw samples, the
MAD, the minimum and the peak allocation.  The median is kept — not the minimum —
because a minimum can hide a systematic regression behind one lucky round.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import numpy as np

from nexus_ai_agent.creative.composition import portrait_imaging as p


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


def test_benchmark_skintone_harmonization(measure: Measure) -> None:
    img = np.random.default_rng(0).random((256, 256, 3)).astype(np.float32)
    mask = np.zeros((256, 256), np.float32)
    mask[60:200, 60:200] = 1.0
    m = measure(p.skintone_harmonization, img, mask, label="skintone", strength=0.5)
    assert m.median < 3000.0, m.describe()
    assert m.peak_bytes < 300 * 1024 * 1024, m.describe()


def test_benchmark_lab_round_trip(measure: Measure) -> None:
    img = np.random.default_rng(1).random((256, 256, 3)).astype(np.float32)
    m = measure(lambda a: p.lab_to_srgb(p.srgb_to_lab(a)), img, label="lab-round-trip")
    assert m.median < 3000.0, m.describe()


def test_benchmark_bokeh_and_relight(measure: Measure) -> None:
    img = np.random.default_rng(2).random((128, 128, 3)).astype(np.float32)
    depth = np.random.default_rng(3).random((128, 128)).astype(np.float32)
    t_bokeh = measure(p.depth_of_field_bokeh, img, depth, label="bokeh", focus=0.5, blades=6)
    assert t_bokeh.median < 3000.0, t_bokeh.describe()
    t_relight = measure(p.facial_relighting_gradient, img, label="relight")
    assert t_relight.median < 1000.0, t_relight.describe()
