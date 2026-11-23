"""Symplectic Störmer–Verlet dynamics and the unified cinema science pipeline.

Formulation
-----------
Hamiltonian in phase space ``(q, p)`` with ``q, p in R^d``, mass ``m`` ≫ 0,
spring matrix ``K`` ≻ 0 and quartic coupling ``alpha >= 0``::

    H(q, p) = ||p||_2^2 / (2 m) + 1/2 q^T K q + 1/4 alpha ||q||_2^4

Equations of motion: ``dq/dt = p/m``, ``dp/dt = -gradV``.  The flow is
symplectic, hence volume-preserving on the phase space (Liouville's theorem):
for the one-step flow ``Phi_h``, ``|det J_Phi_h| = 1``.

Störmer–Verlet (leapfrog) step::

    p_half = p_n - (h/2) gradV(q_n)
    q_n+1  = q_n + h p_half / m
    p_n+1  = p_half - (h/2) gradV(q_n+1)

with ``gradV(q) = K q + alpha ||q||^2 q``.

Rayleigh dissipation: with ``c > 0``, the force gains the velocity-proportional
Rayleigh term ``-(c/m) p``; along the continuous flow, ``dH/dt = -c ||p||^2 / m^2``
(a Lyapunov rate — the orbit converges to the global minimum ``(0, 0)`` of the
Hamiltonian in the absence of external drive).  The discrete step uses a
symmetric Strang splitting — exact half-step damping
``p <- p * exp(-c dt / (2 m))`` on both sides of a conservative Störmer–Verlet
step — so the damping factor is always in ``(0, 1)`` and is unconditionally
stable.  The conservative part still obeys the leapfrog/Verlet stability limit
``omega_max * dt < 2`` with ``omega_max = sqrt(lambda_max(K) / m)``; this is the
**harmonic (K-only) frequency** — it ignores the quartic coupling, so it is a
necessary but NOT sufficient stability condition for the nonlinear flow.  A
stricter **local effective frequency** ``omega_eff(q) = sqrt(lambda_max(H_V(q))/m)``
with ``H_V(q) = K + alpha (||q||^2 I + 2 q q^T)`` is ALSO enforced (fail-closed)
at every step start and at the half-step midpoint ``q_{n+1/2}`` (both
``omega_max * dt < 2`` and ``omega_eff(q) * dt < 2``, or the step is refused).

The :class:`UnifiedCinemaSciencePipeline` then combines the four science
modules (photochemical emulsion, physical optics radiometry, optimal
transport SPD, symplectic Hamiltonian) over a shared reference scenario and
produces an authenticated :class:`ScientificPipelineReceipt` (SHA-256).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import numpy as np
from numpy.typing import NDArray

from nexus_ai_agent.creative.science.optimal_transport_spd import bures_wasserstein_2
from nexus_ai_agent.creative.science.photochemical_emulsion import (
    CANONICAL_LAYERS,
    hurter_driffield_density,
    schwarzschild_reciprocity,
)
from nexus_ai_agent.creative.science.physical_optics_radiometry import (
    henyey_greenstein_phase_function,
    planck_blackbody_spectral_radiance,
    thin_film_reflectance,
)


@dataclass(frozen=True, slots=True)
class HamiltonianSpec:
    """Störmer–Verlet particle spec: ``m > 0``, ``K`` is d-by-d SPD, ``alpha >= 0``."""

    mass: float = 1.0
    stiffness: np.ndarray | list[list[float]] = field(default_factory=lambda: np.eye(2))
    quartic_coupling: float = 0.1

    def __post_init__(self) -> None:
        if not math.isfinite(self.mass) or self.mass <= 0.0:
            raise ValueError("mass must be positive and finite")
        k = np.asarray(self.stiffness, dtype=float)
        if k.ndim != 2 or k.shape[0] != k.shape[1] or k.shape[0] == 0:
            raise ValueError("stiffness must be a non-empty d-by-d square matrix")
        if not np.all(np.isfinite(k)):
            raise ValueError("stiffness must contain only finite entries")
        # Scale-aware symmetry check BEFORE any symmetrization: asymmetric
        # matrices may pass naive eigvalsh checks on the lower triangle, so
        # never silently average a large asymmetry away.
        asymmetry = float(np.max(np.abs(k - k.T)))
        scale = float(np.max(np.abs(k)))
        if asymmetry > 1e-10 + 1e-8 * scale:
            raise ValueError("stiffness matrix must be symmetric")
        k_s = 0.5 * (k + k.T)
        eigs = np.linalg.eigvalsh(k_s)
        if float(np.min(eigs)) <= 0.0:
            raise ValueError("stiffness must be positive definite")
        if not math.isfinite(self.quartic_coupling) or self.quartic_coupling < 0.0:
            raise ValueError("quartic_coupling must be non-negative and finite")
        # Defensive, read-only copy: the caller cannot mutate the stored tensor.
        k_copy = np.array(k_s, dtype=np.float64, copy=True)
        k_copy.setflags(write=False)
        object.__setattr__(self, "stiffness", k_copy)

    @property
    def dimension(self) -> int:
        return int(np.asarray(self.stiffness).shape[0])

    @property
    def lambda_max(self) -> float:
        """Largest eigenvalue of the stiffness matrix."""
        return float(np.max(np.linalg.eigvalsh(np.asarray(self.stiffness))))

    @property
    def omega_max(self) -> float:
        """Highest angular frequency of the conservative flow, sqrt(lambda_max / m)."""
        return math.sqrt(self.lambda_max / self.mass)


def hamiltonian(
    q: np.ndarray | list[float],
    p: np.ndarray | list[float],
    spec: HamiltonianSpec,
) -> float:
    """Scalar Hamiltonian value."""
    arr_q = np.asarray(q, dtype=float).ravel()
    arr_p = np.asarray(p, dtype=float).ravel()
    if arr_q.size != spec.dimension or arr_p.size != spec.dimension:
        raise ValueError("q and p must match the stiffness dimension")
    k = np.asarray(spec.stiffness)
    kinetic = float(arr_p @ arr_p) / (2.0 * spec.mass)
    harmonic = 0.5 * float(arr_q @ (k @ arr_q))
    quartic = 0.25 * spec.quartic_coupling * float(arr_q @ arr_q) ** 2
    return kinetic + harmonic + quartic


def gradient_potential(
    q: np.ndarray | list[float],
    spec: HamiltonianSpec,
) -> NDArray[np.float64]:
    """Gradient of the potential ``V(q) = 1/2 q^T K q + 1/4 alpha ||q||^4``."""
    arr_q = np.asarray(q, dtype=float).ravel()
    k = np.asarray(spec.stiffness)
    grad = k @ arr_q + spec.quartic_coupling * float(arr_q @ arr_q) * arr_q
    return np.asarray(grad, dtype=np.float64)


def local_effective_omega(spec: HamiltonianSpec, q: np.ndarray | list[float]) -> float:
    """Local (nonlinear) effective frequency at ``q``: ``sqrt(lambda_max(H_V(q))/m)``.

    ``H_V(q) = K + alpha * (||q||^2 * I + 2 q q^T)`` is the Hessian of the
    potential ``V(q) = 1/2 q^T K q + 1/4 alpha ||q||^4`` at ``q``.  Unlike the
    harmonic ``omega_max`` this is **q-dependent**: the nonlinear flow
    accelerates as ``||q||`` grows, so the step must respect this tighter bound.
    """
    arr_q = np.asarray(q, dtype=float).ravel()
    if arr_q.size != spec.dimension:
        raise ValueError("q must match the stiffness dimension")
    k = np.asarray(spec.stiffness, dtype=float)
    alpha = spec.quartic_coupling
    n_sq = float(arr_q @ arr_q)
    eye = np.eye(arr_q.size)
    hessian = k + alpha * (n_sq * eye + 2.0 * np.outer(arr_q, arr_q))
    return math.sqrt(float(np.max(np.linalg.eigvalsh(hessian))) / spec.mass)


def _require_local_stability(
    spec: HamiltonianSpec, q: NDArray[np.float64], dt: float, label: str
) -> None:
    """Fail-closed local stability gate: ``omega_eff(q) * dt < 2``."""
    omega_eff = local_effective_omega(spec, q)
    if omega_eff * abs(dt) >= 2.0:
        raise ValueError(
            f"dt violates the local effective stability limit at {label}: omega_eff * dt = "
            f"{omega_eff * abs(dt):.6f} >= 2.0 (omega_eff = {omega_eff:.6f}, "
            f"||q||^2 = {float(q @ q):.6f})"
        )


def stormer_verlet_step(
    q: np.ndarray | list[float],
    p: np.ndarray | list[float],
    dt: float,
    spec: HamiltonianSpec,
    *,
    rayleigh_damping: float = 0.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """One Störmer–Verlet step with optional Rayleigh damping.

    The damping is a symmetric Strang splitting: exact half-step damping
    ``p <- p * exp(-c dt / (2m))`` on both sides of a conservative
    Störmer–Verlet step.  The damping factor is always in ``(0, 1]`` — it is
    unconditionally stable — and the conservative part is gated by the
    leapfrog/Verlet stability limit ``omega_max * dt < 2`` (fail-closed).

    For ``c = 0`` the step is the exact symplectic Störmer–Verlet flow
    (time-reversible and volume-preserving).  Returns ``(q_next, p_next)``.
    """
    if not math.isfinite(dt) or dt == 0.0:
        raise ValueError("dt must be finite and non-zero")
    if not math.isfinite(rayleigh_damping) or rayleigh_damping < 0.0:
        raise ValueError("rayleigh_damping must be non-negative and finite")
    arr_q = np.asarray(q, dtype=float).ravel()
    arr_p = np.asarray(p, dtype=float).ravel()
    if arr_q.shape != arr_p.shape:
        raise ValueError("q and p must have equal length")
    omega_max = spec.omega_max
    if omega_max * abs(dt) >= 2.0:
        raise ValueError(
            f"dt violates the symplectic stability limit: omega_max * dt = "
            f"{omega_max * dt:.6f} >= 2.0 (omega_max = {omega_max:.6f})"
        )
    _require_local_stability(spec, arr_q, dt, "step start")
    damp_half = math.exp(-rayleigh_damping * dt / (2.0 * spec.mass))
    # Half-step damping (exact).
    p_half = arr_p * damp_half
    # Conservative Störmer–Verlet step.
    grad_q = gradient_potential(arr_q, spec)
    p_half2 = p_half - 0.5 * dt * grad_q
    # Gate the mid-step position q_{n+1/2} (half-drift) before committing to it.
    q_half = arr_q + 0.5 * dt * p_half2 / spec.mass
    _require_local_stability(spec, q_half, dt, "mid-step q_{n+1/2}")
    q_next = arr_q + dt * p_half2 / spec.mass
    grad_q_next = gradient_potential(q_next, spec)
    p_next = p_half2 - 0.5 * dt * grad_q_next
    # Half-step damping (exact).
    p_next = p_next * damp_half
    return q_next, p_next


def jacobian_of_step(
    q: np.ndarray | list[float],
    p: np.ndarray | list[float],
    dt: float,
    spec: HamiltonianSpec,
    eps: float = 1e-6,
    *,
    rayleigh_damping: float = 0.0,
) -> NDArray[np.float64]:
    """Numerical Jacobian of one Störmer–Verlet step (centrally differenced)."""
    if not math.isfinite(eps) or eps <= 0.0:
        raise ValueError("eps must be positive and finite")
    arr_q = np.asarray(q, dtype=float).ravel()
    arr_p = np.asarray(p, dtype=float).ravel()
    d = arr_q.size
    n = 2 * d
    jacobian = np.zeros((n, n), dtype=float)
    for i in range(d):
        # q direction
        delta = np.zeros(d)
        delta[i] = eps
        qp, pp = stormer_verlet_step(
            arr_q + delta, arr_p, dt, spec, rayleigh_damping=rayleigh_damping
        )
        qm, pm = stormer_verlet_step(
            arr_q - delta, arr_p, dt, spec, rayleigh_damping=rayleigh_damping
        )
        jacobian[:d, i] = (qp - qm) / (2.0 * eps)
        jacobian[d:, i] = (pp - pm) / (2.0 * eps)
        # p direction
        delta = np.zeros(d)
        delta[i] = eps
        qp, pp = stormer_verlet_step(
            arr_q, arr_p + delta, dt, spec, rayleigh_damping=rayleigh_damping
        )
        qm, pm = stormer_verlet_step(
            arr_q, arr_p - delta, dt, spec, rayleigh_damping=rayleigh_damping
        )
        jacobian[:d, d + i] = (qp - qm) / (2.0 * eps)
        jacobian[d:, d + i] = (pp - pm) / (2.0 * eps)
    return jacobian


def symplectic_volume_preservation_error(
    q: np.ndarray | list[float],
    p: np.ndarray | list[float],
    dt: float,
    spec: HamiltonianSpec,
    eps: float = 1e-6,
) -> float:
    """``|det J - 1|`` for one Störmer–Verlet step (volume preservation check)."""
    jac = jacobian_of_step(q, p, dt, spec, eps=eps)
    det = float(abs(np.linalg.det(jac)))
    return float(abs(det - 1.0))


def integrate_verlet(
    q0: np.ndarray | list[float],
    p0: np.ndarray | list[float],
    dt: float,
    n_steps: int,
    spec: HamiltonianSpec,
    *,
    rayleigh_damping: float = 0.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Integrate Störmer–Verlet, returning ``(q_traj, p_traj, H_traj)``.

    Every step is fail-closed on the symplectic leapfrog stability limits,
    enforced inside :func:`stormer_verlet_step` (harmonic ``omega_max * dt`` and
    the stricter local ``omega_eff(q) * dt`` at the start and midpoint of every
    step).  The initial position is additionally gated here at step start.
    """
    if n_steps < 0:
        raise ValueError("n_steps must be non-negative")
    arr_q = np.asarray(q0, dtype=float).ravel()
    arr_p = np.asarray(p0, dtype=float).ravel()
    _require_local_stability(spec, arr_q, dt, "step start")
    traj_q = [arr_q.copy()]
    traj_p = [arr_p.copy()]
    traj_h = [hamiltonian(arr_q, arr_p, spec)]
    for _ in range(int(n_steps)):
        arr_q, arr_p = stormer_verlet_step(
            arr_q, arr_p, dt, spec, rayleigh_damping=rayleigh_damping
        )
        traj_q.append(arr_q.copy())
        traj_p.append(arr_p.copy())
        traj_h.append(hamiltonian(arr_q, arr_p, spec))
    return np.asarray(traj_q), np.asarray(traj_p), np.asarray(traj_h)


# ---------------------------------------------------------------------------
# Unified cinema science pipeline
# ---------------------------------------------------------------------------


def _freeze_receipt_value(value: object) -> object:
    """Deep-copy and freeze one receipt output value (fail-closed for the rest)."""
    if isinstance(value, bool):
        raise TypeError("receipt output values must not be booleans")
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_receipt_value(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_receipt_value(v) for v in value)
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("receipt output floats must be finite")
        return value
    raise TypeError(
        f"receipt output values must be JSON-scalars or JSON-containers; got {type(value).__name__}"
    )


def _unfreeze_receipt_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _unfreeze_receipt_value(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_unfreeze_receipt_value(v) for v in value]
    return value


def _validate_executed_at(text: object) -> str:
    """Validate the ISO-8601 timestamp, returning the normalized string."""
    if not isinstance(text, str):
        raise TypeError("executed_at must be a string")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"executed_at must be an ISO-8601 timestamp, got {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError("executed_at must be timezone-aware (e.g. '2026-11-22T00:00:00Z')")
    return parsed.isoformat()


@dataclass(frozen=True, slots=True)
class ScientificPipelineReceipt:
    """SHA-256-bound scientific receipt of the unified pipeline.

    The receipt is **deep-frozen**: ``outputs`` is defensively copied at
    construction time and stored as a :class:`MappingProxyType`; mutating it
    raises :class:`TypeError`, never silently reverting to a live dict.

    ``executed_at`` defaults to the real wall-clock time at pipeline
    execution, UTC ISO-8601.  An explicit ``executed_at`` may be supplied as an
    optional override; it is validated to be a timezone-aware ISO-8601 string
    and raises :class:`ValueError` otherwise.
    """

    schema_version: str
    pipeline: str
    inputs_digest: str
    outputs: Mapping[str, float]
    modules_digest: str
    receipt_digest: str
    executed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        executed_at = _validate_executed_at(self.executed_at)
        frozen: dict[str, object] = {}
        for key, value in self.outputs.items():
            if isinstance(value, bool):
                raise TypeError(f"receipt output value {key!r} must not be a boolean")
            frozen[str(key)] = _freeze_receipt_value(value)
        object.__setattr__(self, "executed_at", executed_at)
        object.__setattr__(self, "outputs", MappingProxyType(frozen))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "pipeline": self.pipeline,
            "inputs_digest": self.inputs_digest,
            "outputs": {str(k): _unfreeze_receipt_value(v) for k, v in self.outputs.items()},
            "modules_digest": self.modules_digest,
            "receipt_digest": self.receipt_digest,
            "executed_at": self.executed_at,
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class UnifiedCinemaScienceInputs:
    """Shared inputs across the four science modules."""

    temperature_k: float = 293.15
    exposure_intensity: float = 11.0
    exposure_time_s: float = 100.0
    reciprocity_p: float = 1.0
    lambda_peak_m: float = 550e-9
    blackbody_temperature_k: float = 5778.0
    film_index: float = 1.5
    film_thickness_m: float = 200e-9
    hg_asymmetry: float = 0.4
    transport_mean: tuple[float, float] = (1.0, -2.0)
    transport_target_mean: tuple[float, float] = (3.0, 0.5)
    rayleigh_damping: float = 0.0
    n_steps: int = 32
    dt: float = 0.05

    def __post_init__(self) -> None:
        """Fail-closed validation of every field (finiteness + physical domain)."""
        for name in (
            "temperature_k",
            "exposure_intensity",
            "exposure_time_s",
            "reciprocity_p",
            "lambda_peak_m",
            "blackbody_temperature_k",
            "film_index",
            "film_thickness_m",
            "hg_asymmetry",
            "rayleigh_damping",
            "dt",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a real number")
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        self._validate_means("transport_mean", self.transport_mean)
        self._validate_means("transport_target_mean", self.transport_target_mean)
        if isinstance(self.n_steps, bool) or not isinstance(self.n_steps, int):
            raise TypeError("n_steps must be an integer")
        if self.temperature_k <= 0.0:
            raise ValueError("temperature_k must be positive")
        if self.exposure_intensity <= 0.0:
            raise ValueError("exposure_intensity must be positive")
        if self.exposure_time_s <= 0.0:
            raise ValueError("exposure_time_s must be positive")
        if not (0.5 <= self.reciprocity_p <= 1.0):
            raise ValueError("reciprocity_p must be in [0.5, 1.0]")
        if not (0.0 < self.lambda_peak_m < 1e-3):
            raise ValueError("lambda_peak_m must be in (0, 1e-3)")
        if self.blackbody_temperature_k <= 0.0:
            raise ValueError("blackbody_temperature_k must be positive")
        if self.film_index <= 0.0:
            raise ValueError("film_index must be positive")
        if self.film_thickness_m < 0.0:
            raise ValueError("film_thickness_m must be non-negative")
        if not (-1.0 < self.hg_asymmetry < 1.0):
            raise ValueError("hg_asymmetry must lie in (-1, 1)")
        if self.rayleigh_damping < 0.0:
            raise ValueError("rayleigh_damping must be non-negative")
        if self.n_steps < 0:
            raise ValueError("n_steps must be non-negative")
        if self.dt <= 0.0:
            raise ValueError("dt must be positive")

    def _validate_means(self, name: str, mean: object) -> None:
        if not isinstance(mean, (list, tuple)):
            raise TypeError(f"{name} must be a 2-element sequence (list or tuple)")
        if len(mean) != 2:
            raise ValueError(f"{name} must have 2 entries")
        for i, entry in enumerate(mean):
            if isinstance(entry, bool) or not isinstance(entry, (int, float)):
                raise TypeError(f"{name}[{i}] must be a real number")
            if not math.isfinite(entry):
                raise ValueError(f"{name}[{i}] must be finite")


def _digest_json(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()


def _reference_inputs_payload(inputs: UnifiedCinemaScienceInputs) -> dict[str, object]:
    return {
        "temperature_k": inputs.temperature_k,
        "exposure_intensity": inputs.exposure_intensity,
        "exposure_time_s": inputs.exposure_time_s,
        "reciprocity_p": inputs.reciprocity_p,
        "lambda_peak_m": inputs.lambda_peak_m,
        "blackbody_temperature_k": inputs.blackbody_temperature_k,
        "film_index": inputs.film_index,
        "film_thickness_m": inputs.film_thickness_m,
        "hg_asymmetry": inputs.hg_asymmetry,
        "transport_mean": list(inputs.transport_mean),
        "transport_target_mean": list(inputs.transport_target_mean),
        "rayleigh_damping": inputs.rayleigh_damping,
        "n_steps": inputs.n_steps,
        "dt": inputs.dt,
    }


class UnifiedCinemaSciencePipeline:
    """Combines the four science modules over a shared reference scenario.

    The pipeline is stateless (all computation is functional); it is driven by
    :class:`UnifiedCinemaScienceInputs` and emits a
    :class:`ScientificPipelineReceipt` whose ``receipt_digest`` binds every
    module's outputs plus the inputs via SHA-256.
    """

    MODULE_CODES = {
        "photochemical_emulsion": "W6-EMULSION-1",
        "physical_optics_radiometry": "W6-OPTICS-1",
        "optimal_transport_spd": "W6-OT-1",
        "symplectic_hamiltonian_pipeline": "W6-SYMPLECTIC-1",
    }

    def __init__(self, spec: HamiltonianSpec | None = None) -> None:
        self.spec = spec or HamiltonianSpec()

    def module_digests(self) -> dict[str, str]:
        """Digests binding each science module's on-disk source bytes to this pipeline.

        The digest is the SHA-256 of the literal source bytes of the
        ``<module>.py`` file in this package, folded with the conventional
        version id from :attr:`MODULE_CODES`.  A silent edit to any science
        module therefore changes its digest (and the receipt), while the
        shipped version id still binds the contract revision.
        """
        digests: dict[str, str] = {}
        base = Path(__file__).resolve().parent
        for name, code in self.MODULE_CODES.items():
            source_path = base / f"{name}.py"
            source_bytes = source_path.read_bytes()
            digests[name] = hashlib.sha256(f"{name}:{code}\n".encode() + source_bytes).hexdigest()
        return digests

    def execute(
        self, inputs: UnifiedCinemaScienceInputs | None = None
    ) -> ScientificPipelineReceipt:
        """Run the unified pipeline. Every science module computes its real output."""
        inputs = inputs or UnifiedCinemaScienceInputs()

        # Photochemistry: reciprocity law -> Hurter-Driffield density at cyan layer.
        h_eff = schwarzschild_reciprocity(
            inputs.exposure_intensity, inputs.exposure_time_s, inputs.reciprocity_p
        )
        layer = CANONICAL_LAYERS[0]
        photochemical_density = hurter_driffield_density(
            layer, math.log10(h_eff), inputs.temperature_k
        )
        # Physical optics: Planck radiance + HG phase + thin-film reflectance.
        planck_intensity = planck_blackbody_spectral_radiance(
            inputs.lambda_peak_m, inputs.blackbody_temperature_k
        )
        hg_phase = henyey_greenstein_phase_function(0.0, inputs.hg_asymmetry)
        film_reflectance = thin_film_reflectance(
            0.2, 0.1, inputs.lambda_peak_m, inputs.film_index, inputs.film_thickness_m
        )
        # Optimal transport: Bures-Wasserstein between the two given means.
        cov_1 = np.eye(2)
        cov_2 = 4.0 * np.eye(2)
        bures_distance_sq = bures_wasserstein_2(
            np.asarray(inputs.transport_mean),
            cov_1,
            np.asarray(inputs.transport_target_mean),
            cov_2,
        )
        # Symplectic Hamiltonian: integrate the Störmer-Verlet flow.
        traj_q, traj_p, traj_h = integrate_verlet(
            np.array(inputs.transport_mean),
            np.zeros(2),
            inputs.dt,
            inputs.n_steps,
            self.spec,
            rayleigh_damping=inputs.rayleigh_damping,
        )
        hamiltonian_initial = float(traj_h[0])
        hamiltonian_final = float(traj_h[-1])

        outputs = {
            "photochemical_density": float(photochemical_density),
            "hg_phase_function": float(hg_phase),
            "planck_intensity": float(planck_intensity),
            "thin_film_reflectance": float(film_reflectance),
            "bures_wasserstein_2": float(bures_distance_sq),
            "hamiltonian_initial": hamiltonian_initial,
            "hamiltonian_final": hamiltonian_final,
        }
        inputs_digest = _digest_json(_reference_inputs_payload(inputs))
        modules_digest = _digest_json(self.module_digests())
        outputs_digest = _digest_json(outputs)
        receipt_digest = _digest_json(
            {
                "schema_version": "1",
                "pipeline": "unified-cinema-science",
                "inputs_digest": inputs_digest,
                "outputs_digest": outputs_digest,
                "modules_digest": modules_digest,
            }
        )
        return ScientificPipelineReceipt(
            schema_version="1",
            pipeline="unified-cinema-science",
            inputs_digest=inputs_digest,
            outputs=outputs,
            modules_digest=modules_digest,
            receipt_digest=receipt_digest,
        )


def analytic_jacobian_of_step(
    q: np.ndarray | list[float],
    p: np.ndarray | list[float],
    dt: float,
    spec: HamiltonianSpec,
    rayleigh_damping: float = 0.0,
) -> NDArray[np.float64]:
    """Exact (analytic) Jacobian of one Störmer-Verlet step.

    Uses the chain rule through the half-kick / drift / half-kick decomposition,
    so for the conservative flow (``rayleigh_damping == 0``) the result
    satisfies ``M^T Ω M = Ω`` to machine precision — no finite-difference ``eps``.

    For ``c > 0`` (Rayleigh damping) the accumulated exact linearization is the
    conservative blocks congruenced by the scalar half-step damping factor
    ``d = exp(-c dt / (2 m))``: ``M = [[A, d B], [d C, d^2 D]]``.
    """
    if not math.isfinite(dt) or dt == 0.0:
        raise ValueError("dt must be finite and non-zero")
    if not math.isfinite(rayleigh_damping) or rayleigh_damping < 0.0:
        raise ValueError("rayleigh_damping must be non-negative and finite")
    arr_q = np.asarray(q, dtype=float).ravel()
    arr_p = np.asarray(p, dtype=float).ravel()
    if arr_q.shape != arr_p.shape:
        raise ValueError("q and p must have equal length")
    mass = spec.mass
    stiff = np.asarray(spec.stiffness, dtype=float)
    alpha = spec.quartic_coupling
    d = arr_q.size
    eye = np.eye(d)

    def hessian(x: NDArray[np.float64]) -> NDArray[np.float64]:
        return stiff + alpha * (1.0 * float(x @ x) * eye + 2.0 * np.outer(x, x))

    damp_half = math.exp(-rayleigh_damping * dt / (2.0 * mass))

    # Conservative forward step (c = 0) on the same state, used for the exact
    # second half-step position.
    grad_q = stiff @ arr_q + alpha * float(arr_q @ arr_q) * arr_q
    p_half = arr_p * damp_half
    p_half2 = p_half - 0.5 * dt * grad_q
    q_next = arr_q + dt * p_half2 / mass

    hess_q = hessian(arr_q)
    hess_next = hessian(q_next)

    # Exact conservative blocks.
    a_q = eye - (dt * dt / (2.0 * mass)) * hess_q
    b_q = (dt / mass) * eye
    c_q = -0.5 * dt * hess_q - 0.5 * dt * (hess_next @ a_q)
    d_q = eye - (dt * dt / (2.0 * mass)) * hess_next

    # Congruence with the scalar half-step damping factor ``damp_half``.
    matrix = np.block(
        [
            [a_q, damp_half * b_q],
            [damp_half * c_q, damp_half * damp_half * d_q],
        ]
    )
    return np.ascontiguousarray(matrix)


def default_reference_inputs() -> UnifiedCinemaScienceInputs:
    """The canonical reference scenario for the unified pipeline."""
    return UnifiedCinemaScienceInputs()


__all__ = [
    "HamiltonianSpec",
    "hamiltonian",
    "gradient_potential",
    "local_effective_omega",
    "stormer_verlet_step",
    "jacobian_of_step",
    "analytic_jacobian_of_step",
    "symplectic_volume_preservation_error",
    "integrate_verlet",
    "ScientificPipelineReceipt",
    "UnifiedCinemaScienceInputs",
    "UnifiedCinemaSciencePipeline",
    "default_reference_inputs",
]
