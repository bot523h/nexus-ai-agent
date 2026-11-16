"""Repeatable end-to-end CPU benchmark for the intrinsic shot compiler."""

from __future__ import annotations

import resource
import statistics
import sys
import time
import tracemalloc
from typing import TypedDict

import numpy as np

from .log_chromophore_tomography_baker import LogChromophoreTomographyBaker
from .ocular_caustics_and_film_chemistry import OcularCausticsAndFilmChemistry


class BenchmarkResult(TypedDict):
    median_ms: float
    mad_ms: float
    peak_tracemalloc_mib: float
    peak_rss_mib: float
    samples_ms: list[float]


def _rss_mib() -> float:
    peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak / (1024.0**2 if sys.platform == "darwin" else 1024.0)


def benchmark_pipeline(
    sizes: tuple[int, ...] = (128, 256, 512), iterations: int = 3, warmups: int = 1
) -> dict[str, BenchmarkResult]:
    """Return medians, MAD, tracemalloc peaks, and process peak RSS per square size."""
    if iterations < 1 or warmups < 0 or any(size < 2 for size in sizes):
        raise ValueError("sizes must be >=2, iterations >=1, and warmups >=0")
    baker = LogChromophoreTomographyBaker()
    report: dict[str, BenchmarkResult] = {}
    for size in sizes:
        image = baker.canonical_archetype(size, size)

        def run(current_image: np.ndarray = image) -> None:
            vault = baker.bake(current_image)
            OcularCausticsAndFilmChemistry.compile(vault, t=0.25)

        for _ in range(warmups):
            run()
        samples: list[float] = []
        peaks: list[int] = []
        rss_peaks: list[float] = []
        for _ in range(iterations):
            tracemalloc.start()
            started = time.perf_counter()
            run()
            samples.append((time.perf_counter() - started) * 1000.0)
            peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
            rss_peaks.append(_rss_mib())
        median = statistics.median(samples)
        report[f"{size}x{size}"] = {
            "median_ms": median,
            "mad_ms": statistics.median(abs(value - median) for value in samples),
            "peak_tracemalloc_mib": max(peaks) / (1024.0**2),
            "peak_rss_mib": max(rss_peaks),
            "samples_ms": samples,
        }
    return report
