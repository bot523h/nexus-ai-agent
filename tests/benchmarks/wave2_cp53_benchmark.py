"""Reproduce CP5.3: python tests/benchmarks/wave2_cp53_benchmark.py.

Three fresh workers; one untimed warm-up per path. Timings are total wall ms
for the documented batch, not per-operation estimates or performance gates.
"""

from __future__ import annotations

import json
import platform
import runpy
import statistics
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

from nexus_ai_agent.creative.atlas.aces_hald_film_optic import aces_tone_map
from nexus_ai_agent.creative.audio_engine.master_cinema_pipeline import MasterCinemaAudioPipeline
from nexus_ai_agent.creative.photoreal_intrinsic.screened_poisson_skin_transport import (
    solve_screened_poisson_neumann,
)
from nexus_ai_agent.creative.storyboard_compiler.cinematography_solver import (
    minimum_jerk_profile,
    se3_exp,
    se3_log,
)
from nexus_ai_agent.creative.studio.bus import CommandBus, PlanStep, PlanTransaction

ROOT = Path(__file__).resolve().parents[2]
BATCHES = {"transaction": 100, "se3_jerk": 1000, "poisson_aces": 20, "master_cinema": 1}


def worker() -> dict[str, Any]:
    helpers = runpy.run_path(str(ROOT / "tests/unit/test_plan_transaction.py"))
    twist = np.array([0.1, -0.2, 0.3, 1.0, 2.0, -1.0])
    taus = np.linspace(0.0, 1.0, 257)
    rng = np.random.default_rng(53)
    source = rng.standard_normal((64, 64))
    rgb = rng.random((64, 64, 3)) * 4
    sr = 24000
    t = np.arange(sr) / sr
    dialogue = 0.1 * np.sin(2 * np.pi * 220 * t)
    foley = 0.01 * rng.standard_normal(sr)
    visual = np.abs(np.sin(np.arange(24)))
    pipeline = MasterCinemaAudioPipeline(sample_rate=sr)

    def transaction() -> None:
        bus = CommandBus(helpers["make_project"](), registry=helpers["_plan_test_registry"]([]))
        step = PlanStep(
            "test.plan_step", command=helpers["make_command"]("test.plan_step", "bench")
        )
        plan = PlanTransaction(plan_id="benchmark", steps=(step,))
        plan.commit(bus)
        assert PlanTransaction(plan_id="benchmark", steps=(step,)).commit(bus) is plan
        plan.rollback_committed_plan()
        assert bus.state_revision == 2

    def geometry() -> None:
        np.testing.assert_allclose(se3_log(se3_exp(twist)), twist, atol=1e-10)
        assert minimum_jerk_profile(taus).shape[0] == 257

    def images() -> None:
        assert np.isfinite(solve_screened_poisson_neumann(source, 0.2)).all()
        assert np.isfinite(aces_tone_map(rgb)).all()

    def audio() -> None:
        result = pipeline.run(dialogue, dialogue * 0.5, foley, visual)
        assert result.status == "PASS"

    paths: dict[str, Callable[[], None]] = {
        "transaction": transaction,
        "se3_jerk": geometry,
        "poisson_aces": images,
        "master_cinema": audio,
    }
    result: dict[str, Any] = {}
    for name, operation in paths.items():
        operation()
        started = perf_counter()
        for _ in range(BATCHES[name]):
            operation()
        result[name] = (perf_counter() - started) * 1000
    return result


def main() -> None:
    if "--worker" in sys.argv:
        print(json.dumps(worker()))
        return
    runs = [
        json.loads(
            subprocess.check_output(
                [sys.executable, str(Path(__file__).resolve()), "--worker"],
                cwd=ROOT,
                text=True,
            )
        )
        for _ in range(3)
    ]
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "batches": BATCHES,
                "unit": "ms/batch",
                "warmup_per_path": 1,
                "results": {
                    name: {
                        "runs": [run[name] for run in runs],
                        "median": statistics.median(run[name] for run in runs),
                    }
                    for name in BATCHES
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
