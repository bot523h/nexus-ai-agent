"""Small repeatable CPU benchmark smoke test; values are environment-specific."""

from __future__ import annotations

import resource
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

from nexus_ai_agent.creative.chronos.acoustics_lipsync import shoebox_rir
from nexus_ai_agent.creative.chronos.color_science import srgb_decode
from nexus_ai_agent.creative.chronos.contracts import RationalTimestamp
from nexus_ai_agent.creative.chronos.geometry import intrinsics, project
from nexus_ai_agent.creative.studio.chronos_port import ChronosCommand


def test_chronos_benchmarks_n3(tmp_path: Path) -> None:
    def timeline_case() -> Fraction:
        return RationalTimestamp(30000, Fraction(1, 30000)).seconds

    def geometry_case() -> np.ndarray:
        matrix = intrinsics(50, 640, 480, 36, 24)
        return project(np.array([[0.1, 0.2, 2.0]]), np.eye(3), np.zeros(3), matrix)

    def color_case() -> np.ndarray:
        return srgb_decode(np.full((24, 24, 3), 0.4))

    def acoustics_case() -> np.ndarray:
        return shoebox_rir(
            np.array([1.0, 1.0, 1.0]),
            np.array([2.0, 1.0, 1.0]),
            np.array([5.0, 4.0, 3.0]),
            0.3,
            4000,
            0,
        )

    cases = {
        "timeline": timeline_case,
        "geometry": geometry_case,
        "color": color_case,
        "acoustics": acoustics_case,
    }
    report: dict[str, tuple[float, float]] = {}
    staging_bytes = 0
    for name, operation in cases.items():
        durations: list[float] = []
        for index in range(3):
            start = time.perf_counter()
            result = operation()
            elapsed = (time.perf_counter() - start) * 1000
            assert np.isfinite(np.asarray(result, dtype=np.float64)).all()
            durations.append(elapsed)
            staging_bytes += len(str(result)) if index == 0 else 0
        report[name] = (float(np.median(durations)), max(durations))
    command = ChronosCommand("bench", ("synthetic",), {})
    assert command.deterministic and not command.external_api_allowed
    peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert peak_rss_kib > 0 and staging_bytes > 0
    assert all(np.isfinite(metrics).all() for metrics in report.values())
    print(
        {
            "median_max_ms": report,
            "peak_rss_kib": peak_rss_kib,
            "staging_bytes": staging_bytes,
        }
    )
