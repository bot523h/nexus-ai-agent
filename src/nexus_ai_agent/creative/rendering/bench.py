"""Render-lane benchmark core — pure ``compile_lane`` timing (no FFmpeg).

Wave-4 step6 harness.  Lives inside the installed package so tests, CI and
operators all import the same code; ``scripts/bench_render.py`` is only a
thin CLI over :func:`bench_render_ir_compile`.

Why inside the package: tests must never import the unpackaged ``scripts/``
namespace — the console-script ``pytest`` used by CI does not put the
repository root on ``sys.path`` (src-layout installs only the
``nexus_ai_agent`` package), so such imports pass under ``python -m pytest``
locally and fail in CI.  This module is the task-104 lane's measurement
harness: deterministic, GPU-free, offline.
"""

from __future__ import annotations

import time
from pathlib import Path

from nexus_ai_agent.creative.rendering.compiler import compile_lane
from nexus_ai_agent.creative.rendering.ir import (
    LaneIR,
    LaneProfile,
    LaneSource,
    SpeedOp,
    TrimOp,
)


def _fixture_ir() -> LaneIR:
    main = LaneSource(
        asset_id="main", path="/tmp/fake_main.mp4", media_kind="video", duration_us=5_000_000
    )
    return LaneIR(
        main=main,
        ops=(TrimOp(in_us=0, out_us=2_500_000), SpeedOp(factor=1.5)),
        profile=LaneProfile(width=1280, height=720, fps=30),
    )


def bench_render_ir_compile(iterations: int = 50) -> dict[str, float]:
    """Compile ``LaneIR → argv`` ``iterations`` times, return timing stats (ms)."""
    ir = _fixture_ir()
    # Warm up once so any lazy import is not timed
    compile_lane(ir).argv(Path("/tmp/out.mp4"))

    times: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        compiled = compile_lane(ir)
        cmd = compiled.argv(Path("/tmp/out.mp4"))
        _ = " ".join(cmd)
        times.append((time.perf_counter() - t0) * 1000.0)

    times.sort()
    return {
        "iterations": float(iterations),
        "p50_ms": times[len(times) // 2],
        "p95_ms": times[int(len(times) * 0.95)],
        "mean_ms": sum(times) / len(times),
        "min_ms": times[0],
        "max_ms": times[-1],
    }
