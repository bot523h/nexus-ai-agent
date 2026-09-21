#!/usr/bin/env python3
"""Render-lane bench CLI — thin wrapper over the packaged bench core.

The measurable logic lives in
:func:`nexus_ai_agent.creative.rendering.bench.bench_render_ir_compile`
(installed package, importable by tests and CI).  This script only parses
arguments and prints — it must stay free of logic so the test suite never
needs to import the unpackaged ``scripts/`` namespace.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nexus_ai_agent.creative.rendering.bench import bench_render_ir_compile


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
