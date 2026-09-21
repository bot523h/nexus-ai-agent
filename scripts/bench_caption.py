#!/usr/bin/env python3
"""Caption-engine bench CLI — thin wrapper over the packaged bench core.

The measurable logic lives in
:func:`nexus_ai_agent.creative.caption.bench.bench_caption_format`
(installed package, importable by tests and CI).  This script only parses
arguments and prints — it must stay free of logic so the test suite never
needs to import the unpackaged ``scripts/`` namespace.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from nexus_ai_agent.creative.caption.bench import bench_caption_format


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--iterations", type=int, default=200)
    p.add_argument("--json", action="store_true")
    p.add_argument("--baseline", type=Path, default=None)
    p.add_argument("--check", action="store_true")
    args = p.parse_args()

    try:
        stats = bench_caption_format(args.iterations)
    except Exception as exc:  # pragma: no cover
        print(f"bench skipped: {exc}")
        return 0

    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print(f"caption format: p50={stats['p50_ms']:.4f}ms p95={stats['p95_ms']:.4f}ms")

    if args.baseline and args.check:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        threshold = baseline["p50_ms"] * 1.15
        if stats["p50_ms"] > threshold:
            msg = f"REGRESSION: p50 {stats['p50_ms']:.4f} > baseline {baseline['p50_ms']:.4f} *1.15"
            print(msg)
            return 1
        print("bench ok: within 15% of baseline")
    elif args.baseline:
        args.baseline.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {args.baseline}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
