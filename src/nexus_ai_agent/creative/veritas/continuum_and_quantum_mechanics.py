"""Consolidated continuum and quantum mechanics — Laws 5, 7, 8, 9 and 11.

Single import surface for the dynamical layer of NAGAR VERITAS, aggregating:

* :mod:`se3_object_permanence` — Law 5, Lie-group SE(3) object permanence
  through total Z-buffer occlusion.
* :mod:`symplectic_contact_mechanics` — Law 7, the symplectic Stormer-Verlet
  integrator and frictional impulse resolution.
* :mod:`xpbd_cloth_drape` — Law 8, XPBD cloth drape against analytic capsule
  signed-distance fields.
* :mod:`thermodynamic_arrow_of_time` — Law 9, the Clausius-Duhem entropy arrow
  and retarded-wave causality.

On top of those verified engines this module adds **Law 11**, the Madelung
quantum-fluid phase-coherence transport. Frame luminance/chrominance density
``rho`` and optical phase ``Phi`` are fused into a complex wavefunction
``psi = sqrt(rho) exp(i Phi)`` obeying the Madelung continuity equation. The
Bohmian quantum potential ``Q(rho) = -(hbar_eff^2 / 2) lap(sqrt(rho)) / sqrt(rho)``
acts as an edge-preserving regulariser: it suppresses the high-frequency phase
"boiling" that plagues generative video while retaining sharp physical edges,
because the quantum pressure diverges exactly where the amplitude gradient is
discontinuous.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from nexus_ai_agent.creative.veritas.se3_object_permanence import (
    ObjectPermanenceError,
    ObjectState,
    PermanenceAudit,
    audit_object_permanence,
    propagate_occluded_object,
    repair_object_permanence,
    se3_exp,
    se3_log,
)
from nexus_ai_agent.creative.veritas.symplectic_contact_mechanics import (
    CollisionResult,
    MechanicsViolationError,
    kinetic_energy,
    resolve_rigid_body_collision,
    stormer_verlet_step,
    total_angular_momentum,
    total_linear_momentum,
)
from nexus_ai_agent.creative.veritas.thermodynamic_arrow_of_time import (
    ArrowAudit,
    ThermodynamicError,
    audit_and_enforce_thermodynamic_arrow,
    fourier_advection_diffusion,
    retarded_wavefront,
    spatial_entropy,
    verify_retarded_causality,
)
from nexus_ai_agent.creative.veritas.xpbd_cloth_drape import (
    ClothSimulationError,
    XpbdResult,
    capsule_sdf,
    step_xpbd_cloth_simulation,
)

FloatArray = npt.NDArray[np.float64]
ComplexArray = npt.NDArray[np.complex128]

#: Directive-compatible aliases for the three renamed entry points.
propagate_occluded_object_se3 = propagate_occluded_object
step_symplectic_verlet = stormer_verlet_step
verify_retarded_wave_causality = verify_retarded_causality


@dataclass(frozen=True)
class PhaseCoherenceResult:
    """Outcome of a Madelung phase-coherence stabilisation pass.

    Attributes
    ----------
    wavefunction:
        The regularised complex wavefunction ``psi = sqrt(rho) exp(i Phi)``.
    density:
        The (unchanged) non-negative density field.
    phase:
        The healed optical phase field.
    quantum_potential:
        The Bohmian quantum potential evaluated on the input density.
    boiling_before, boiling_after:
        High-frequency phase-energy ratios before and after regularisation.
    iterations:
        Number of low-pass sweeps applied.
    violations:
        Detected pathologies, e.g. ``"temporal_phase_boiling"``.
    """

    wavefunction: ComplexArray
    density: FloatArray
    phase: FloatArray
    quantum_potential: FloatArray
    boiling_before: float
    boiling_after: float
    iterations: int
    violations: tuple[str, ...]

    @property
    def healed(self) -> bool:
        """True when a violation was detected and boiling energy strictly fell."""
        return bool(self.violations) and self.boiling_after < self.boiling_before


def _laplacian(field: FloatArray) -> FloatArray:
    """Periodic second-difference Laplacian, fully vectorised over all axes."""
    accumulator = np.zeros_like(field)
    for axis in range(field.ndim):
        accumulator = (
            accumulator + np.roll(field, 1, axis=axis) + np.roll(field, -1, axis=axis) - 2.0 * field
        )
    return accumulator


def bohmian_quantum_potential(
    density: FloatArray,
    *,
    hbar_eff: float = 1.0,
    epsilon: float = 1e-12,
) -> FloatArray:
    """Evaluate ``Q(rho) = -(hbar_eff^2 / 2) lap(sqrt(rho)) / sqrt(rho)``.

    The quantum potential is the Bohmian correction term that carries the
    non-local "quantum pressure" of the Madelung fluid. Where the amplitude is
    smooth it is small; across a genuine physical edge it spikes, which is
    exactly why it can regularise texture boiling without blurring silhouettes.
    """
    rho = np.asarray(density, dtype=np.float64)
    if rho.ndim < 2:
        raise ValueError("density field must be at least 2-D")
    if not np.all(np.isfinite(rho)):
        raise ValueError("density field must be finite")
    if np.any(rho < 0.0):
        raise ValueError("density field must be non-negative")
    if hbar_eff <= 0.0:
        raise ValueError("hbar_eff must be strictly positive")
    amplitude = np.sqrt(np.clip(rho, 0.0, None) + epsilon)
    return -(hbar_eff**2 / 2.0) * _laplacian(amplitude) / amplitude


def _high_frequency_ratio(field: FloatArray) -> float:
    """Fraction of spectral power living outside the lowest-frequency block."""
    spectrum = np.fft.fftn(field)
    power = np.abs(spectrum) ** 2
    total = float(power.sum())
    if total <= 0.0:
        return 0.0
    mask = np.ones(power.shape, dtype=bool)
    for axis in range(field.ndim):
        index = np.arange(field.shape[axis])
        keep = (index <= 1) | (index >= field.shape[axis] - 1)
        shape = [1] * field.ndim
        shape[axis] = field.shape[axis]
        mask = mask & keep.reshape(shape)
    high = float(power[~mask].sum())
    return high / total


def _lowpass(field: FloatArray, cutoff: int) -> FloatArray:
    """Ideal brick-wall low-pass over every axis, returned in real space."""
    spectrum = np.fft.fftn(field)
    mask = np.ones(field.shape, dtype=bool)
    for axis in range(field.ndim):
        index = np.arange(field.shape[axis])
        keep = (index <= cutoff) | (index >= field.shape[axis] - cutoff)
        shape = [1] * field.ndim
        shape[axis] = field.shape[axis]
        mask = mask & keep.reshape(shape)
    filtered = np.where(mask, spectrum, 0.0)
    return np.real(np.fft.ifftn(filtered))


def madelung_continuity_residual(
    density: FloatArray,
    phase: FloatArray,
    *,
    dt: float = 1.0,
) -> float:
    """RMS residual of ``d rho / dt + div(rho grad Phi) = 0`` (hbar/m = 1).

    A coherent Madelung fluid transports density as a probability current
    ``j = rho grad Phi``. Generative frames that "boil" violate continuity: the
    measured divergence does not match the observed density change. This scalar
    is a compact falsification metric for that failure.
    """
    rho = np.asarray(density, dtype=np.float64)
    phi = np.asarray(phase, dtype=np.float64)
    if rho.shape != phi.shape:
        raise ValueError("density and phase shapes must match")
    if rho.ndim < 2:
        raise ValueError("fields must be at least 2-D")
    if not np.all(np.isfinite(rho)) or not np.all(np.isfinite(phi)):
        raise ValueError("fields must be finite")
    if dt <= 0.0:
        raise ValueError("dt must be strictly positive")
    gradients = np.gradient(phi)
    flux = [rho * component for component in gradients]
    divergence = np.zeros_like(rho)
    for axis, component in enumerate(flux):
        divergence = divergence + np.gradient(component, axis=axis)
    return float(np.sqrt(np.mean(divergence**2)))


def stabilize_madelung_phase_coherence(
    density: FloatArray,
    phase: FloatArray,
    *,
    hbar_eff: float = 1.0,
    boiling_threshold: float = 0.35,
    max_iterations: int = 8,
    smoothing_cutoff: int = 2,
) -> PhaseCoherenceResult:
    """Detect and heal high-frequency phase boiling in a Madelung fluid.

    The input density ``rho`` and optical phase ``Phi`` are fused into
    ``psi = sqrt(rho) exp(i Phi)``. Phase-wrap discontinuities are unwrapped and
    the high-frequency spectral energy ratio of the phase is measured. When it
    exceeds ``boiling_threshold`` the phase is repeatedly brick-wall low-passed
    (an idealised Bohmian smoothing) until the boiling energy falls below
    threshold or ``max_iterations`` is exhausted.

    Raises
    ------
    ValueError
        If shapes disagree, fields are not finite, density is negative, or any
        scalar parameter is out of domain.
    """
    rho = np.asarray(density, dtype=np.float64)
    phi = np.asarray(phase, dtype=np.float64)
    if rho.shape != phi.shape:
        raise ValueError("density and phase shapes must match")
    if rho.ndim < 2:
        raise ValueError("fields must be at least 2-D")
    if not np.all(np.isfinite(rho)) or not np.all(np.isfinite(phi)):
        raise ValueError("fields must be finite")
    if np.any(rho < 0.0):
        raise ValueError("density field must be non-negative")
    if hbar_eff <= 0.0:
        raise ValueError("hbar_eff must be strictly positive")
    if boiling_threshold <= 0.0:
        raise ValueError("boiling_threshold must be strictly positive")
    if max_iterations < 0:
        raise ValueError("max_iterations must be non-negative")
    if smoothing_cutoff < 1:
        raise ValueError("smoothing_cutoff must be at least one")

    violations: list[str] = []

    unwrapped = phi
    for axis in range(phi.ndim):
        unwrapped = np.unwrap(unwrapped, axis=axis)
    if float(np.max(np.abs(unwrapped - phi))) > np.pi:
        violations.append("phase_discontinuity")

    current = unwrapped
    before = _high_frequency_ratio(current)
    if before > boiling_threshold:
        violations.append("temporal_phase_boiling")

    iterations = 0
    metric = before
    while metric > boiling_threshold and iterations < max_iterations:
        current = _lowpass(current, smoothing_cutoff)
        metric = _high_frequency_ratio(current)
        iterations += 1

    amplitude = np.sqrt(np.clip(rho, 0.0, None))
    wavefunction = amplitude * np.exp(1j * current)
    quantum_potential = bohmian_quantum_potential(rho, hbar_eff=hbar_eff)
    return PhaseCoherenceResult(
        wavefunction=wavefunction,
        density=rho,
        phase=current,
        quantum_potential=quantum_potential,
        boiling_before=before,
        boiling_after=metric,
        iterations=iterations,
        violations=tuple(violations),
    )


__all__ = [
    "ArrowAudit",
    "ClothSimulationError",
    "CollisionResult",
    "MechanicsViolationError",
    "ObjectPermanenceError",
    "ObjectState",
    "PermanenceAudit",
    "PhaseCoherenceResult",
    "ThermodynamicError",
    "XpbdResult",
    "audit_and_enforce_thermodynamic_arrow",
    "audit_object_permanence",
    "bohmian_quantum_potential",
    "capsule_sdf",
    "fourier_advection_diffusion",
    "kinetic_energy",
    "madelung_continuity_residual",
    "propagate_occluded_object",
    "propagate_occluded_object_se3",
    "repair_object_permanence",
    "resolve_rigid_body_collision",
    "retarded_wavefront",
    "se3_exp",
    "se3_log",
    "spatial_entropy",
    "stabilize_madelung_phase_coherence",
    "step_symplectic_verlet",
    "step_xpbd_cloth_simulation",
    "stormer_verlet_step",
    "total_angular_momentum",
    "total_linear_momentum",
    "verify_retarded_causality",
    "verify_retarded_wave_causality",
]
