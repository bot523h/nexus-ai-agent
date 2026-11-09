#!/usr/bin/env python3
"""Composition runtime benchmark CLI.

Run the canonical 3-op composition (resize -> grayscale -> image-to-video)
and report real timing, peak memory, output size, and verification status.

Usage:
    python scripts/bench_composition.py [--iterations N] [--json]
"""

from __future__ import annotations

import argparse
import json

from nexus_ai_agent.creative.composition.bench import bench_composition


def main() -> int:
    p = argparse.ArgumentParser(description="NAGAR composition runtime benchmark")
    p.add_argument("--iterations", type=int, default=5)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    stats = bench_composition(args.iterations)
    if args.json:
        print(json.dumps(stats.to_dict(), indent=2))
        return 0

    print("NAGAR Composition Runtime Benchmark")
    print("=" * 50)
    print(f"iterations:          {stats.iterations}")
    print(f"compile time:        {stats.compile_ms:.3f} ms")
    print(f"execute mean:        {stats.execute_ms_mean:.3f} ms")
    print(f"execute min:         {stats.execute_ms_min:.3f} ms")
    print(f"execute max:         {stats.execute_ms_max:.3f} ms")
    print("per-node (mean):")
    for node, ms in stats.per_node_ms.items():
        print(f"  {node:<12} {ms:>8.3f} ms")
    print(f"peak RSS:            {stats.peak_rss_mb:.2f} MB")
    print(f"output size:         {stats.output_bytes} bytes")
    print(f"output hash:         {stats.output_hash}")
    print(f"independently verified: {stats.verified}")
    print("tool versions:")
    for tool, ver in stats.tool_versions.items():
        print(f"  {tool}: {ver[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
