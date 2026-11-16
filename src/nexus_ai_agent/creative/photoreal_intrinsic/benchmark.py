"""Per-trial subprocess benchmarks for the image-space intrinsic rendering model."""

from __future__ import annotations

import importlib
import json
import os
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path
from types import ModuleType
from typing import Any, TypedDict, cast

try:
    resource: ModuleType | None = importlib.import_module("resource")
except ImportError:  # Windows does not provide the resource module.
    resource = None


class BenchmarkTrial(TypedDict):
    pid: int
    elapsed_ms: float
    baseline_rss_mib: float
    peak_rss_mib: float
    incremental_rss_mib: float
    tracemalloc_peak_mib: float


class BenchmarkResult(TypedDict):
    median_ms: float
    mad_ms: float
    raw_trials: list[BenchmarkTrial]


def _rss_mib() -> float:
    """Return process peak working-set RSS in MiB using standard-library APIs."""
    if resource is not None:
        peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return peak / (1024.0**2 if sys.platform == "darwin" else 1024.0)
    if sys.platform == "win32":
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


def _benchmark_trial(size: int) -> BenchmarkTrial:
    """Measure one fresh process: baseline, one warm-up, and one timed pass."""
    from .log_chromophore_tomography_baker import LogChromophoreTomographyBaker
    from .ocular_caustics_and_film_chemistry import OcularCausticsAndFilmChemistry

    baseline = _rss_mib()
    baker = LogChromophoreTomographyBaker()
    image = baker.canonical_archetype(size, size)

    def run() -> None:
        vault = baker.bake(image)
        OcularCausticsAndFilmChemistry.compile(vault, t=0.25)

    run()  # exactly one warm-up in each isolated trial process
    tracemalloc.start()
    started = time.perf_counter()
    run()
    elapsed = (time.perf_counter() - started) * 1000.0
    traced_peak = tracemalloc.get_traced_memory()[1] / (1024.0**2)
    tracemalloc.stop()
    peak_rss = _rss_mib()
    return {
        "pid": os.getpid(),
        "elapsed_ms": elapsed,
        "baseline_rss_mib": baseline,
        "peak_rss_mib": peak_rss,
        "incremental_rss_mib": max(0.0, peak_rss - baseline),
        "tracemalloc_peak_mib": traced_peak,
    }


def benchmark_pipeline(
    sizes: tuple[int, ...] = (128, 256, 512), iterations: int = 3
) -> dict[str, BenchmarkResult]:
    """Run each trial in a new subprocess and report its raw record, median, and MAD."""
    if iterations < 1 or any(size < 2 for size in sizes):
        raise ValueError("sizes must be >=2 and iterations >=1")
    if not sizes:
        return {}
    worker_code = (
        "import json,sys; from nexus_ai_agent.creative.photoreal_intrinsic.benchmark "
        "import _benchmark_trial; "
        "print(json.dumps(_benchmark_trial(int(sys.argv[1]))))"
    )
    report: dict[str, BenchmarkResult] = {}
    for size in sizes:
        trials: list[BenchmarkTrial] = []
        for _ in range(iterations):
            environment = os.environ.copy()
            source_root = str(Path(__file__).resolve().parents[3])
            environment["PYTHONPATH"] = os.pathsep.join(
                part for part in (source_root, environment.get("PYTHONPATH", "")) if part
            )
            completed = subprocess.run(
                [sys.executable, "-c", worker_code, str(size)],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            trials.append(cast(BenchmarkTrial, json.loads(completed.stdout)))
        timings = [trial["elapsed_ms"] for trial in trials]
        median = statistics.median(timings)
        report[f"{size}x{size}"] = {
            "median_ms": median,
            "mad_ms": statistics.median(abs(value - median) for value in timings),
            "raw_trials": trials,
        }
    return report
