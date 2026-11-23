"""Wave 6 CP5: N=5 benchmark of the four science paths.

Reproduce CP5: ``python tests/benchmarks/w6_cp5_benchmark.py``.

Five fresh workers, one untimed warm-up per path. Timings are total wall ms
for the documented batch, not per-operation estimates or performance gates.
"""

from __future__ import annotations

import json
import platform
import statistics
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

from nexus_ai_agent.creative.character import (
    CharacterCapsule,
    ContinuityThreshold,
    verify_dimensions,
)
from nexus_ai_agent.creative.science.optimal_transport_spd import bures_wasserstein_2
from nexus_ai_agent.creative.science.photochemical_emulsion import (
    CANONICAL_LAYERS,
    hurter_driffield_density,
)
from nexus_ai_agent.creative.science.physical_optics_radiometry import (
    henyey_greenstein_phase_function,
    thin_film_reflectance,
    volumetric_radiative_transfer,
)
from nexus_ai_agent.creative.science.symplectic_hamiltonian_pipeline import (
    HamiltonianSpec,
    UnifiedCinemaSciencePipeline,
    integrate_verlet,
)

ROOT = Path(__file__).resolve().parents[2]
BATCHES = {
    "unified_pipeline": 20,
    "verlet_integration": 10,
    "bures_wasserstein_2": 100,
    "optics_batch": 100,
    "character_capsule": 30,
}


def worker() -> dict[str, Any]:
    spec = HamiltonianSpec()

    def unified() -> None:
        receipt = UnifiedCinemaSciencePipeline().execute()
        assert len(receipt.receipt_digest) == 64

    def verlet() -> None:
        q0 = np.array([1.0, 0.5])
        p0 = np.array([0.2, -0.1])
        _, _, traj_h = integrate_verlet(q0, p0, 0.02, 300, spec)
        assert np.all(np.isfinite(traj_h))

    def bures() -> None:
        m1 = np.zeros(2)
        c1 = np.eye(2)
        m2 = np.array([0.5, -0.3])
        c2 = np.array([[1.2, 0.1], [0.1, 1.3]])
        assert bures_wasserstein_2(m1, c1, m2, c2) > 0.0

    def optics() -> None:
        for layer in CANONICAL_LAYERS:
            for stimulus in np.linspace(-3.0, 3.0, 25):
                _ = hurter_driffield_density(layer, float(stimulus), 293.15)
        _ = henyey_greenstein_phase_function(0.7, 0.4)
        _ = thin_film_reflectance(0.2, 0.1, 550e-9, 1.5, 200e-9, 20.0)
        _ = volumetric_radiative_transfer(1.0, 0.5, 0.4)

    def capsule() -> None:
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=1
        )
        capsule = capsule.apply_revision_changes("face", {"skin_texture": "weathered"})
        assert capsule.digest().startswith("sha256:")
        report = verify_dimensions(
            {"face": 0.1, "voice": 0.7},
            {
                "face": ContinuityThreshold("face", 0.2, 0.6),
                "voice": ContinuityThreshold("voice", 0.2, 0.6),
            },
        )
        assert report.verdict is not None

    paths: dict[str, Callable[[], None]] = {
        "unified_pipeline": unified,
        "verlet_integration": verlet,
        "bures_wasserstein_2": bures,
        "optics_batch": optics,
        "character_capsule": capsule,
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
        for _ in range(5)
    ]
    from statistics import mean, pstdev

    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "platform": platform.platform(),
                "batches": BATCHES,
                "unit": "ms/batch",
                "warmup_per_path": 1,
                "results": {
                    name: {
                        "runs": [run[name] for run in runs],
                        "mean": mean(run[name] for run in runs),
                        "median": statistics.median(run[name] for run in runs),
                        "std": pstdev(run[name] for run in runs),
                        "min": min(run[name] for run in runs),
                        "max": max(run[name] for run in runs),
                    }
                    for name in BATCHES
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
