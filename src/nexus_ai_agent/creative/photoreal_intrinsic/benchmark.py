"""Isolated, repeatable CPU and memory benchmark for intrinsic shot compilation."""

from __future__ import annotations

import importlib
import json
import os
import statistics
import subprocess
import sys
import time
import tracemalloc
from types import ModuleType
from typing import Any, TypedDict, cast

try:
    resource: ModuleType | None = importlib.import_module("resource")
except ImportError:  # Windows does not provide the resource module.
    resource = None


class BenchmarkResult(TypedDict):
    median_ms: float
    mad_ms: float
    peak_tracemalloc_mib: float
    baseline_rss_mib: float
    peak_rss_mib: float
    incremental_rss_mib: float
    samples_ms: list[float]


def _rss_mib() -> float:
    """Current process high-water RSS in MiB, using only the standard library."""
    if resource is not None:
        peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return peak / (1024.0**2 if sys.platform == "darwin" else 1024.0)
    platform_name = sys.platform
    if platform_name == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        windows_api = cast(Any, ctypes).windll
        process = windows_api.kernel32.GetCurrentProcess()
        success = windows_api.psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        )
        if not success:
            raise OSError("GetProcessMemoryInfo failed")
        return float(counters.PeakWorkingSetSize) / (1024.0**2)
    raise RuntimeError("Peak RSS measurement is unsupported on this platform")


def _benchmark_worker(size: int, iterations: int, warmups: int) -> BenchmarkResult:
    """Run one image size in the short-lived worker process."""
    from .log_chromophore_tomography_baker import LogChromophoreTomographyBaker
    from .ocular_caustics_and_film_chemistry import OcularCausticsAndFilmChemistry

    baseline_rss = _rss_mib()
    baker = LogChromophoreTomographyBaker()
    image = baker.canonical_archetype(size, size)

    def run() -> None:
        vault = baker.bake(image)
        OcularCausticsAndFilmChemistry.compile(vault, t=0.25)

    for _ in range(warmups):
        run()
    samples: list[float] = []
    traced_peaks: list[int] = []
    for _ in range(iterations):
        tracemalloc.start()
        started = time.perf_counter()
        run()
        samples.append((time.perf_counter() - started) * 1000.0)
        traced_peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
    median = statistics.median(samples)
    peak_rss = _rss_mib()
    return {
        "median_ms": median,
        "mad_ms": statistics.median(abs(value - median) for value in samples),
        "peak_tracemalloc_mib": max(traced_peaks) / (1024.0**2),
        "baseline_rss_mib": baseline_rss,
        "peak_rss_mib": peak_rss,
        "incremental_rss_mib": max(0.0, peak_rss - baseline_rss),
        "samples_ms": samples,
    }


def benchmark_pipeline(
    sizes: tuple[int, ...] = (128, 256, 512), iterations: int = 3, warmups: int = 1
) -> dict[str, BenchmarkResult]:
    """Benchmark each size in its own process, isolating high-water RSS per size."""
    if iterations < 1 or warmups < 0 or any(size < 2 for size in sizes):
        raise ValueError("sizes must be >=2, iterations >=1, and warmups >=0")
    if not sizes:
        return {}
    worker_code = (
        "import json,sys; from nexus_ai_agent.creative.photoreal_intrinsic.benchmark "
        "import _benchmark_worker; "
        "print(json.dumps(_benchmark_worker(int(sys.argv[1]), int(sys.argv[2]), "
        "int(sys.argv[3]))))"
    )
    report: dict[str, BenchmarkResult] = {}
    for size in sizes:
        completed = subprocess.run(
            [sys.executable, "-c", worker_code, str(size), str(iterations), str(warmups)],
            check=True,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        report[f"{size}x{size}"] = cast(BenchmarkResult, json.loads(completed.stdout))
    return report
