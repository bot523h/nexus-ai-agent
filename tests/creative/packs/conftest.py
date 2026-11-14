"""Measurement discipline shared by the pack micro-benchmarks (scene, portrait).

Both packs answered the same question — "how long does this kernel take?" — with
the same measurement, and both got it slightly wrong: three samples, no warm-up,
and a median taken over cold-cache first runs.  A wall-clock gate that measures
the machine's warm-up as well as the kernel is a flake generator: it reddens on a
busy runner and can hide a genuine regression behind noise.

The rules encoded in :class:`Measurement` / :func:`measure_kernel`:

* **warm-up rounds are not measured.**  Two rounds run first (each preceded by
  ``gc.collect()``) so caches, allocator arenas and lazily-initialised state are
  warm before the first sample is taken;
* **every measured round is preceded by ``gc.collect()``**, so a collection
  triggered by the previous round lands in neither sample;
* the **median** is the acceptance statistic — robust to one slow round, and it
  cannot hide a systematic regression the way a minimum can;
* the **MAD** (median absolute deviation) is reported next to it, so a reader
  sees whether the median means anything on this machine: a MAD that is a large
  fraction of the median says the box was noisy, which the failure message
  states instead of hiding;
* **raw samples and the minimum are recorded too**, so a reviewer can recompute
  the statistic without re-running the benchmark;
* peak allocation is measured with ``tracemalloc`` around the call only
  (``ru_maxrss`` is a whole-process high-water mark and would measure the
  interpreter as well).

It lives in a ``conftest.py`` on purpose: ``tests/`` is not an importable
package (see ``tests/architecture/test_test_suite_hygiene.py``), so a shared
helper is provided as a fixture, never as ``from tests.… import …``.
"""

from __future__ import annotations

import gc
import statistics
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

#: Rounds whose timings are discarded (they warm caches and allocators).
WARMUP_ROUNDS = 2

#: Measured rounds per kernel: five gives the median a real middle and the MAD a
#: meaningful spread, at a cost of a handful of milliseconds per kernel.
MEASURED_ROUNDS = 5


@dataclass(frozen=True)
class Measurement:
    """One kernel's measured cost, with everything a reviewer needs to judge it."""

    #: Timings of the measured rounds, milliseconds, in execution order.
    samples: tuple[float, ...]
    #: Largest traced allocation across the measured rounds, bytes.
    peak_bytes: int
    #: Timings of the discarded warm-up rounds, milliseconds.
    warmup_samples: tuple[float, ...] = ()
    #: The label used in :meth:`describe`.
    label: str = "kernel"

    @property
    def median(self) -> float:
        """The acceptance statistic: robust to one slow round, not to a regression."""
        return statistics.median(self.samples)

    @property
    def minimum(self) -> float:
        """The fastest measured round."""
        return min(self.samples)

    @property
    def mad(self) -> float:
        """Median absolute deviation of the measured rounds."""
        centre = statistics.median(self.samples)
        return statistics.median([abs(sample - centre) for sample in self.samples])

    def relative_mad(self) -> float:
        """MAD as a fraction of the median (0.0 when the median is zero)."""
        return self.mad / self.median if self.median else 0.0

    def describe(self) -> str:
        """Every measured fact, one line — used verbatim in failure messages."""
        samples = ", ".join(f"{sample:.3f}" for sample in self.samples)
        warmup = ", ".join(f"{sample:.3f}" for sample in self.warmup_samples) or "none"
        return (
            f"{self.label}: median {self.median:.3f} ms "
            f"(MAD {self.mad:.3f} ms, {self.relative_mad():.1%} of the median; "
            f"min {self.minimum:.3f} ms; peak {self.peak_bytes / 1024 / 1024:.1f} MB) | "
            f"measured samples [{samples}] ms | warm-up samples [{warmup}] ms"
        )


def measure_kernel(
    fn: Callable[..., Any],
    *args: Any,
    label: str = "kernel",
    warmup: int = WARMUP_ROUNDS,
    rounds: int = MEASURED_ROUNDS,
    **kwargs: Any,
) -> Measurement:
    """Time one kernel call the disciplined way (see the module docstring)."""
    if rounds < 3:
        raise ValueError("at least three measured rounds are needed for a median and a MAD")
    warmup_samples: list[float] = []
    for _ in range(warmup):
        gc.collect()
        started = time.perf_counter()
        fn(*args, **kwargs)
        warmup_samples.append((time.perf_counter() - started) * 1000.0)

    samples: list[float] = []
    peak = 0
    for _ in range(rounds):
        gc.collect()
        tracemalloc.start()
        try:
            started = time.perf_counter()
            fn(*args, **kwargs)
            samples.append((time.perf_counter() - started) * 1000.0)
            _, measured_peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        peak = max(peak, measured_peak)
    return Measurement(
        samples=tuple(samples),
        peak_bytes=peak,
        warmup_samples=tuple(warmup_samples),
        label=label,
    )


@pytest.fixture()
def measure() -> Callable[..., Measurement]:
    """The measurement helper as a fixture (``measure(fn, *args, label=...)``)."""
    return measure_kernel


__all__ = ["MEASURED_ROUNDS", "WARMUP_ROUNDS", "Measurement", "measure", "measure_kernel"]
