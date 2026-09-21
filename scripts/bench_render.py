#!/usr/bin/env python3
"""Render-lane bench — wave-4 step6 (pure compile, no FFmpeg).

Measures ``compile_lane`` (IR → filtergraph → argv) so the bench is
deterministic, GPU-free and offline.  The result is stored as a JSON
baseline in ``tests/bench/baseline_render.json``; CI fails when the
current p50 is >15 % above the committed baseline.
"""

from __future__ import annotations

import argparse
import json
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--iterations", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.add_argument("--baseline", type=Path, default=None)
    p.add_argument("--check", action="store_true", help="fail if >15% slower than baseline")
    args = p.parse_args()

    stats = bench_render_ir_compile(args.iterations)
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print(
            f"render IR compile: p50={stats['p50_ms']:.3f}ms "
            f"p95={stats['p95_ms']:.3f}ms mean={stats['mean_ms']:.3f}ms"
        )

    if args.baseline and args.check:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        threshold = baseline["p50_ms"] * 1.15
        if stats["p50_ms"] > threshold:
            print(
                f"REGRESSION: p50 {stats['p50_ms']:.3f} > baseline {baseline['p50_ms']:.3f} *1.15"
            )
            return 1
        print("bench ok: within 15% of baseline")
    elif args.baseline:
        args.baseline.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {args.baseline}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
