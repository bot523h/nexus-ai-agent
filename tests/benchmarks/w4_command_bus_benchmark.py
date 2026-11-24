"""N=5 real-disk W4 benchmark; run directly or explicitly with pytest.

Each sample uses an independent TemporaryDirectory and real SQLite WAL. The
recovery path reuses the ingress subprocess-death regression, including actual
os._exit(73), lease expiry wait, nonce replay rejection and PRESENT compensation.
Times are end-to-end scenario times (including assertions/setup where stated),
not isolated primitive latency. tracemalloc covers only the parent Python heap,
not SQLite native allocations or the killed child's heap. No timing thresholds.
"""

from __future__ import annotations

import json
import math
import runpy
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from nexus_ai_agent.creative.studio.bus import PlanTransactionState
from nexus_ai_agent.creative.studio.models import TypedCommand
from nexus_ai_agent.creative.studio.plan_execution import compile_plan
from nexus_ai_agent.creative.studio.plan_ir import PlanIR
from nexus_ai_agent.creative.studio.runtime import DurableStudioExecutionRuntime

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
Factory = Callable[
    [Path], tuple[DurableStudioExecutionRuntime, PlanIR, dict[str, TypedCommand], str]
]


def summarize(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "min_ms": min(samples),
        "median_ms": statistics.median(samples),
        "mean_ms": statistics.mean(samples),
        "p95_ms": ordered[math.ceil(0.95 * len(samples)) - 1],
        "max_ms": max(samples),
        "ops_per_second": 1000 / statistics.mean(samples),
    }


def benchmark() -> dict[str, Any]:
    helpers = runpy.run_path(str(ROOT / "tests/integration/test_w4_ingress_e2e.py"))
    factory = cast(Factory, helpers["external_runtime"])
    recovery = cast(
        Callable[[Path, bool], None],
        helpers["test_subprocess_death_and_authenticated_external_recovery"],
    )
    samples: dict[str, list[float]] = {
        name: [] for name in ("compile_sha_ibpc", "publication", "crash_recovery")
    }
    peaks: dict[str, list[int]] = {name: [] for name in samples}
    for _ in range(5):
        with tempfile.TemporaryDirectory(prefix="w4-benchmark-") as temporary:
            root = Path(temporary)
            runtime, ir, commands, credential = factory(root)
            with sqlite3.connect(root / "external.db") as connection:
                assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            tracemalloc.start()
            started = time.perf_counter()
            plan = compile_plan(ir, commands)
            assert plan.admitted_ir_hash == ir.plan_hash
            token = runtime.approve_plan_ir(ir, commands=commands, credential=credential)
            samples["compile_sha_ibpc"].append((time.perf_counter() - started) * 1000)
            peaks["compile_sha_ibpc"].append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
            tracemalloc.start()
            started = time.perf_counter()
            result = runtime.execute_approved_plan_ir(
                ir, token, commands=commands, credential=credential
            )
            assert result.state == PlanTransactionState.COMMITTED
            artifacts = list(root.rglob("*.effect"))
            assert len(artifacts) == 1 and artifacts[0].read_bytes() == b"durable artifact"
            samples["publication"].append((time.perf_counter() - started) * 1000)
            peaks["publication"].append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
            crash_root = root / "crash"
            crash_root.mkdir()
            tracemalloc.start()
            started = time.perf_counter()
            try:
                recovery(crash_root, True)
                samples["crash_recovery"].append((time.perf_counter() - started) * 1000)
                peaks["crash_recovery"].append(tracemalloc.get_traced_memory()[1])
            finally:
                tracemalloc.stop()
            with sqlite3.connect(crash_root / "external.db") as connection:
                assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    return {
        "runs": 5,
        "p95_method": "nearest rank",
        "memory_scope": "parent Python heap only",
        "recovery_scope": "real subprocess death, setup, lease wait, recovery and assertions",
        "paths": {
            name: {**summarize(values), "samples_ms": values, "peak_bytes": max(peaks[name])}
            for name, values in samples.items()
        },
    }


def test_benchmark_real_wal_and_crash_scenarios() -> None:
    report = benchmark()
    assert report["runs"] == 5
    for row in report["paths"].values():
        assert len(row["samples_ms"]) == 5
        assert row["min_ms"] <= row["median_ms"] <= row["max_ms"]
        assert row["p95_ms"] == row["max_ms"]
        assert row["peak_bytes"] > 0


def test_summary_uses_nearest_rank_and_scenario_throughput() -> None:
    assert summarize([1, 2, 3, 4, 5]) == {
        "min_ms": 1,
        "median_ms": 3,
        "mean_ms": 3,
        "p95_ms": 5,
        "max_ms": 5,
        "ops_per_second": 1000 / 3,
    }


if __name__ == "__main__":
    report = benchmark()
    destination = ROOT / "ci-artifacts/w4/benchmark.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
