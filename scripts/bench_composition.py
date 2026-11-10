#!/usr/bin/env python3
"""Benchmark the NAGAR composition runtime — compile, queue, worker, execute, verify.

The benchmark measures the **whole** canonical chain, not a micro-benchmark of
the compiler: a composition that compiles fast but spends its life in a queue
is not a fast composition.

Stages measured per run:

===============  ============================================================
``compile_ms``   plan → compiled plan (the compiler's own cost)
``queue_ms``     enqueue → the row is durable and the job is scheduled
``worker_ms``    the worker picked the row up and produced a result
``execute_ms``   the media production (real FFmpeg encodes)
``verify_ms``    the independent semantic verification
``total_ms``     the sum of the above
===============  ============================================================

Plus: peak RSS, CPU time, disk delta, output size, node count, failure rate
and reproducibility (the plan fingerprint must be identical every run).

Usage::

    python scripts/bench_composition.py --runs 10 --json out.json
    python scripts/bench_composition.py --runs 10 --queue     # through the queue

The script is *not* part of the installed package (see
``tests/architecture/test_scripts_import_boundary.py``); the measurement core
lives in ``nexus_ai_agent.continuum.composition_bench``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from nexus_ai_agent.continuum.composition_bench import (  # noqa: E402
    bench_composition,
    bench_composition_through_queue,
)


def _environment() -> dict[str, object]:
    from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin

    try:
        binary = resolve_ffmpeg_bin()
    except Exception:  # pragma: no cover - environment dependent
        binary = ""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "ffmpeg_binary": binary,
    }


def _peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes; macOS reports bytes.
    return usage / 1024.0 if sys.platform != "darwin" else usage / (1024.0 * 1024.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10, help="comparable runs")
    parser.add_argument("--json", type=Path, default=None, help="write the report here")
    parser.add_argument(
        "--queue",
        action="store_true",
        help="measure through the canonical queue instead of the executor directly",
    )
    parser.add_argument("--width", type=int, default=1280, help="the resize target width")
    parser.add_argument("--height", type=int, default=720, help="the resize target height")
    parser.add_argument("--duration-us", type=int, default=1_000_000, help="the clip duration")
    parser.add_argument("--fps", type=int, default=25, help="the clip frame rate")
    args = parser.parse_args()

    runner = bench_composition_through_queue if args.queue else bench_composition
    started = time.monotonic()
    report = runner(
        runs=args.runs,
        width=args.width,
        height=args.height,
        duration_us=args.duration_us,
        fps=args.fps,
    )
    report["environment"] = _environment()
    report["peak_rss_mb"] = round(_peak_rss_mb(), 2)
    report["wall_clock_seconds"] = round(time.monotonic() - started, 3)
    report["mode"] = "queue" if args.queue else "executor"

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return 0 if report["failure_rate"] == 0.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
