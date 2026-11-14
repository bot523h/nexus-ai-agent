"""WAVE 20 — the NAGAR TITAN staged execution spine.

The spine is the last wave: it takes a compiled film (Wave 19) and *actually
executes* each PROPOSED render pass on a small, bounded, synthetic fixture,
in-memory, using the real Wave 13-18 operators — then writes every per-pass
artifact through the repository's authorized staging boundary, re-opens the
bytes and verifies them. It produces a deterministic audit ledger that records,
for each pass, exactly what was computed and whether the persisted bytes match.

The spine is deliberately *bounded and honest*:

* every stage is a pure function of a declared, bounded fixture;
* a pass whose operator cannot be exercised on the available fixture is
  recorded ``SKIPPED`` with a reason — never faked;
* the spine never dispatches a CommandBus command, never mints a passport and
  never claims to have rendered real film pixels; the "renders" are small
  synthetic arrays whose digests are real.

This is the "executable, auditable, reproducible" end of the pipeline: the
whole chain (story -> analysis -> plan -> passes -> operator execution ->
staged bytes -> verified ledger) is deterministic and re-runnable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np

from nexus_ai_agent.creative.atlas import (
    anamorphic_seidel_optics as optics,
)
from nexus_ai_agent.creative.atlas import (
    eikonal_crowd_social_force as crowd,
)
from nexus_ai_agent.creative.atlas import (
    non_euclidean_portal_metrics as portal,
)
from nexus_ai_agent.creative.atlas import (
    pontryagin_editing_pacing_solver as pacing,
)
from nexus_ai_agent.creative.atlas import (
    titan_feature_film_compiler as compiler,
)
from nexus_ai_agent.creative.atlas import (
    volumetric_atmospheric_scattering as atmosphere,
)
from nexus_ai_agent.creative.atlas import (
    voronoi_fracture_destruction_ledger as fracture,
)
from nexus_ai_agent.execution.staging import AttemptStaging
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

__all__ = [
    "MAX_FIXTURE_SIDE",
    "SpineError",
    "StageResult",
    "SpineLedger",
    "run_staged_spine",
]

MAX_FIXTURE_SIDE: Final[int] = 64


class SpineError(ValueError):
    """Raised for malformed spine inputs."""


@dataclass(frozen=True)
class StageResult:
    """The outcome of executing one render pass on a fixture."""

    pass_id: str
    step_index: int
    status: str  # "COMPUTED" | "SKIPPED"
    detail: str
    artifact_sha256: str
    relative_path: str


@dataclass
class SpineLedger:
    """The deterministic audit ledger of one staged spine run."""

    story_fingerprint: str
    stages: tuple[StageResult, ...]
    ledger_sha256: str
    total_bytes: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def computed(self) -> int:
        return sum(1 for s in self.stages if s.status == "COMPUTED")

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.stages if s.status == "SKIPPED")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "titan.staged_spine_ledger",
            "schema_version": 1,
            "story_fingerprint": self.story_fingerprint,
            "ledger_sha256": self.ledger_sha256,
            "total_bytes": self.total_bytes,
            "computed": self.computed,
            "skipped": self.skipped,
            "stages": [
                {
                    "pass_id": s.pass_id,
                    "step_index": s.step_index,
                    "status": s.status,
                    "detail": s.detail,
                    "artifact_sha256": s.artifact_sha256,
                    "relative_path": s.relative_path,
                }
                for s in self.stages
            ],
            "notes": list(self.notes),
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


def _synthetic_scene(side: int, *, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.clip(rng.random((side, side, 3)), 0.0, 1.0)


def _digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _execute_pass(
    pass_id: str,
    side: int,
    *,
    seed: int,
) -> tuple[str, str, bytes]:
    """Execute one pass on a synthetic fixture; return (status, detail, bytes).

    Only the passes that have a real, deterministic operator are computed; every
    other catalogue entry is reported SKIPPED with a reason.
    """
    if pass_id == "titan.wave13.portal_metrics":
        rot = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
        km = portal.transported_kinematics(
            position=np.array([0.1, 0.2, 0.3]),
            linear_velocity=np.array([1.0, 0.0, 0.0]),
            angular_velocity=np.array([0.0, 0.1, 0.0]),
            rotation=rot,
            translation=np.array([1.0, 0.0, 0.0]),
        )
        payload = json.dumps(
            {"continuity_gap": km.continuity_gap},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "COMPUTED", "SE(3) kinematics transported", payload
    if pass_id == "titan.wave14.atmosphere":
        beta_r = atmosphere.rayleigh_beta(np.array([650.0, 550.0, 460.0]))
        beta_m = atmosphere.mie_beta(np.array([650.0, 550.0, 460.0]), scale=0.02)
        hg = atmosphere.henyey_greenstein_phase(np.array([0.2]), 0.6)
        payload = json.dumps(
            {"rayleigh": beta_r.tolist(), "mie": beta_m.tolist(), "hg": hg.tolist()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "COMPUTED", "rayleigh+mie+HG phase", payload
    if pass_id == "titan.wave17.anamorphic_optics":
        img = _synthetic_scene(side, seed=seed)
        res = optics.apply_anamorphic_optics(
            img,
            optics.AnamorphicLens(),
            size=(min(side, 33) | 1),
            defocus_mm=0.05,
            pixel_pitch_mm=0.01,
        )
        payload = json.dumps(
            {"energy_residual": res.energy_residual, "psf_energy": res.psf_energy},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "COMPUTED", "anamorphic PSF applied", payload
    if pass_id == "titan.wave18.pacing":
        prob = pacing.LQProblem(
            A=np.array([[1.0]]),
            B=np.array([[1.0]]),
            Q=np.array([[1.0]]),
            R=np.array([[1.0]]),
            Qf=np.array([[1.0]]),
            x0=np.array([1.0]),
            horizon=4,
        )
        sol = pacing.solve_lq_optimal_control(prob)
        plan = pacing.pacing_plan_from_control(sol.controls.ravel())
        payload = json.dumps(
            {"total_cost": sol.total_cost, "total_duration": plan.total_duration},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "COMPUTED", "Riccati pacing solved", payload
    if pass_id == "titan.wave15.crowd_heal":
        sim = crowd.simulate_and_heal_crowd_trajectories(
            np.array([[0.0, 0.0], [0.05, 0.0]]),
            np.array([[1.0, 0.0], [-1.0, 0.0]]),
            np.array([0.6, 0.6]),
            np.array([[5.0, 0.0], [-5.0, 0.0]]),
            steps=5,
        )
        payload = json.dumps(
            {"max_overlap": sim.max_overlap, "min_separation": sim.min_separation},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "COMPUTED", "social-force overlap healed", payload
    if pass_id == "titan.wave16.fracture_ledger":
        seeds = [
            fracture.FractureSeed(np.array([1.0, 1.0, 1.0])),
            fracture.FractureSeed(np.array([3.0, 3.0, 3.0])),
        ]
        frac = fracture.fracture_voronoi_cells((4, 4, 4), seeds)
        payload = json.dumps(
            {"mass_drift": frac.mass_drift, "total_voxels": frac.total_voxels},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "COMPUTED", "voronoi fracture ledger", payload
    return "SKIPPED", "no bounded operator fixture for this pass", b""


def run_staged_spine(
    film: compiler.CompiledFilm,
    filesystem: WorkspaceFilesystem,
    *,
    out_dir: str = "titan_spine",
    side: int = 16,
    staging: AttemptStaging | None = None,
) -> SpineLedger:
    """Execute every pass of ``film`` on a bounded fixture and persist a ledger.

    Each computed pass writes a small JSON artifact under ``out_dir`` through
    ``filesystem`` (or ``staging`` when supplied); the bytes are re-opened and
    verified. The ledger digest is a SHA-256 over the canonical ledger with its
    own digest field zeroed, so the digest is a real function of the recorded
    stages.
    """
    if side < 2 or side > MAX_FIXTURE_SIDE:
        raise SpineError(f"side must be in [2, {MAX_FIXTURE_SIDE}]")
    if not out_dir or out_dir.startswith("/") or ".." in out_dir.split("/"):
        raise SpineError("out_dir must be a safe relative path")

    stages: list[StageResult] = []
    total_bytes = 0
    for i, p in enumerate(film.passes):
        status, detail, payload = _execute_pass(p.pass_id, side, seed=p.step_index + 1)
        rel = f"{out_dir}/step{p.step_index}_{p.pass_id.replace('.', '_')}.json"
        digest = ""
        if status == "COMPUTED":
            if staging is not None:
                staging.prepare()
                staged_name = f"s{i}.json"
                staging.write(staged_name, payload)
                published = staging.publish(staged_name, rel)
                rel = str(published.relative_to(filesystem.root))
            else:
                filesystem.write_bytes(rel, payload, create_parents=True)
            reopened = filesystem.read_bytes(rel, max_bytes=1 << 20)
            if reopened != payload:
                raise SpineError(f"persisted bytes for {p.pass_id} do not match")
            digest = _digest_bytes(reopened)
            total_bytes += len(reopened)
        stages.append(
            StageResult(
                pass_id=p.pass_id,
                step_index=p.step_index,
                status=status,
                detail=detail,
                artifact_sha256=digest,
                relative_path=rel,
            )
        )

    provisional = SpineLedger(
        story_fingerprint=film.fingerprint,
        stages=tuple(stages),
        ledger_sha256="0" * 64,
        total_bytes=total_bytes,
        notes=("every COMPUTED artifact was re-opened and byte-verified",),
    )
    ledger_digest = _digest_bytes(provisional.canonical_bytes())
    provisional.ledger_sha256 = ledger_digest
    return provisional
