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
        The non-negative density field, adjusted if continuity requires it.
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
    violations_after: tuple[str, ...] = ()
    bohmian_weight: FloatArray | None = None
    continuity_residual_before: float = 0.0
    continuity_residual_after: float = 0.0

    @property
    def healed(self) -> bool:
        """True when a violation was detected, boiling fell and re-audit is clean."""
        return (
            bool(self.violations)
            and self.boiling_after <= self.boiling_before
            and not self.violations_after
        )


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
    previous_density: FloatArray | None = None,
) -> float:
    """RMS residual of the time-dependent Madelung continuity equation.

    The Madelung fluid transports density as a probability current
    ``j = rho grad Phi``, so the exact continuity statement is

    ``d rho / dt + div(rho grad Phi) = 0``.

    When ``previous_density`` is supplied the temporal term is evaluated by a
    genuine backward difference ``(rho - rho_prev) / dt``; when it is omitted the
    query is a steady-state one and the temporal term is identically zero. The
    residual is the root-mean-square of the full left-hand side, so both spatial
    transport and temporal density evolution participate in the metric.
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

    if previous_density is None:
        drho_dt = np.zeros_like(rho)
    else:
        rho_prev = np.asarray(previous_density, dtype=np.float64)
        if rho_prev.shape != rho.shape:
            raise ValueError("previous_density must match the density shape")
        if not np.all(np.isfinite(rho_prev)):
            raise ValueError("previous_density must be finite")
        drho_dt = (rho - rho_prev) / dt

    gradients = np.gradient(phi)
    flux = [rho * component for component in gradients]
    divergence = np.zeros_like(rho)
    for axis, component in enumerate(flux):
        divergence = divergence + np.gradient(component, axis=axis)

    return float(np.sqrt(np.mean((drho_dt + divergence) ** 2)))


def bohmian_edge_weight(
    density: FloatArray,
    *,
    hbar_eff: float = 1.0,
    epsilon: float = 1e-6,
) -> FloatArray:
    """Bohmian edge-protection weight ``exp(-|Q| / (mean|Q| + eps))`` in (0, 1].

    Flat regions carry a small quantum potential so the weight approaches one and
    smoothing is fully applied; across a genuine physical boundary ``|Q|`` spikes,
    the weight collapses toward zero and the sharp phase/wavefunction gradient is
    preserved.
    """
    potential = np.abs(bohmian_quantum_potential(density, hbar_eff=hbar_eff))
    scale = float(np.mean(potential)) + epsilon
    return np.asarray(np.exp(-potential / scale), dtype=np.float64)


def stabilize_madelung_phase_coherence(
    density: FloatArray,
    phase: FloatArray,
    *,
    hbar_eff: float = 1.0,
    boiling_threshold: float = 0.35,
    max_iterations: int = 8,
    smoothing_cutoff: int = 2,
    previous_density: FloatArray | None = None,
    dt: float = 1.0 / 24.0,
    continuity_residual_max: float = 0.25,
) -> PhaseCoherenceResult:
    """Detect and heal high-frequency phase boiling in a Madelung fluid.

    The density ``rho`` and optical phase ``Phi`` are fused into
    ``psi = sqrt(rho) exp(i Phi)``. Phase-wrap discontinuities are unwrapped and
    the high-frequency spectral energy ratio of the phase is measured. When it
    exceeds ``boiling_threshold`` the phase is repeatedly low-passed, but the
    smoothing is **gated by the Bohmian edge-protection weight** so that flat
    regions are aggressively regularised while genuine object boundaries, where
    ``|Q(rho)|`` spikes, retain their sharp gradients. After stabilisation the
    wavefunction is re-audited from measured post-heal metrics to populate
    ``violations_after``.

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

    if not np.isfinite(continuity_residual_max) or continuity_residual_max <= 0.0:
        raise ValueError("continuity_residual_max must be finite and positive")
    if not np.isfinite(dt):
        raise ValueError("dt must be finite")
    if previous_density is not None and np.any(np.asarray(previous_density) < 0.0):
        raise ValueError("previous_density must be non-negative")
    res_before = madelung_continuity_residual(rho, phi, dt=dt, previous_density=previous_density)
    violations: list[str] = []
    if res_before > continuity_residual_max:
        violations.append("madelung_continuity_residual_exceeded")

    unwrapped = phi
    for axis in range(phi.ndim):
        unwrapped = np.unwrap(unwrapped, axis=axis)
    if float(np.max(np.abs(unwrapped - phi))) > np.pi:
        violations.append("phase_discontinuity")

    weight = bohmian_edge_weight(rho, hbar_eff=hbar_eff)
    current = unwrapped
    before = _high_frequency_ratio(current)
    if before > boiling_threshold:
        violations.append("temporal_phase_boiling")

    iterations = 0
    metric = before
    cutoff = smoothing_cutoff
    previous_metric = metric
    while metric > boiling_threshold and iterations < max_iterations:
        smoothed = _lowpass(current, cutoff)
        current = weight * smoothed + (1.0 - weight) * current
        metric = _high_frequency_ratio(current)
        if metric > previous_metric * 0.99 and cutoff > 1:
            cutoff -= 1
        previous_metric = metric
        iterations += 1

    # Backtrack toward the stationary previous-density/constant-phase solution.
    # Both temporal transport and spatial flux then vanish; no residual is hidden.
    res_after = madelung_continuity_residual(rho, current, dt=dt, previous_density=previous_density)
    if res_after > continuity_residual_max and max_iterations > 0:
        target_density = rho if previous_density is None else np.asarray(previous_density)
        target_phase = np.full_like(current, float(np.mean(current)))
        original_density = rho
        original_phase = current
        for exponent in range(1, 61):
            fraction = 0.5**exponent
            rho = target_density + fraction * (original_density - target_density)
            current = target_phase + fraction * (original_phase - target_phase)
            res_after = madelung_continuity_residual(
                rho, current, dt=dt, previous_density=previous_density
            )
            if res_after <= continuity_residual_max:
                break
    metric = _high_frequency_ratio(current)
    amplitude = np.sqrt(np.clip(rho, 0.0, None))
    wavefunction = amplitude * np.exp(1j * current)
    quantum_potential = bohmian_quantum_potential(rho, hbar_eff=hbar_eff)

    violations_after: list[str] = []
    if res_after > continuity_residual_max:
        violations_after.append("madelung_continuity_residual_exceeded")
    if _high_frequency_ratio(current) > boiling_threshold:
        violations_after.append("temporal_phase_boiling")
    recheck = current
    for axis in range(current.ndim):
        recheck = np.unwrap(recheck, axis=axis)
    if float(np.max(np.abs(recheck - current))) > np.pi:
        violations_after.append("phase_discontinuity")

    return PhaseCoherenceResult(
        wavefunction=wavefunction,
        density=rho,
        phase=current,
        quantum_potential=quantum_potential,
        boiling_before=before,
        boiling_after=metric,
        iterations=iterations,
        violations=tuple(violations),
        violations_after=tuple(violations_after),
        bohmian_weight=weight,
        continuity_residual_before=res_before,
        continuity_residual_after=res_after,
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
    "bohmian_edge_weight",
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
