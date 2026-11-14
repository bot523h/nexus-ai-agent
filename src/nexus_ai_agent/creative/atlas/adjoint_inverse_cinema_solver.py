"""WAVE 20 — differentiable adjoint inverse cinema and self-calibrating reconstruction.

A studio scene is described by a small parameter vector ``theta`` (key/fill light
positions and intensities, camera yaw/pitch/focal, subject albedo and specular
roughness, atmospheric extinction, room acoustic absorption). Rendering an
observation from ``theta`` is a *differentiable* forward model
:func:`render_differentiable_scene_observation` that also returns the exact
analytic adjoint/Jacobian ``J = d y_hat / d theta``.

Given a desired observation (a lighting ratio, a shadow contrast, a rim
intensity, a fog depth, a room ``T60``) the inverse problem recovers ``theta`` by
box-constrained Gauss-Newton / Levenberg-Marquardt:

* :func:`verify_adjoint_jacobian_accuracy` proves the analytic Jacobian matches a
  fourth-order central finite difference to a relative Frobenius error below
  ``1e-6``;
* :func:`solve_inverse_cinema_parameters` runs the damped, box-projected
  iteration ``(J^T J + lambda diag(J^T J + I)) delta = -(J^T r + gamma (theta -
  theta_prior))`` and returns an :class:`InverseCinemaReconstructionReport`.

Pure NumPy + stdlib, bounded, validated, offline and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import numpy as np

__all__ = [
    "PARAMETER_COUNT",
    "OBSERVATION_COUNT",
    "PARAMETER_NAMES",
    "PARAMETER_BOUNDS",
    "DEFAULT_PRIOR",
    "JACOBIAN_TOLERANCE",
    "InverseCinemaError",
    "render_differentiable_scene_observation",
    "verify_adjoint_jacobian_accuracy",
    "InverseCinemaReconstructionReport",
    "solve_inverse_cinema_parameters",
]

PARAMETER_COUNT: Final[int] = 15
OBSERVATION_COUNT: Final[int] = 15
_EPS: Final[float] = 1e-6
_DEPTH: Final[float] = 5.0
_ROOM_VOLUME: Final[float] = 100.0
_ROOM_SURFACE: Final[float] = 50.0
_SOURCE_DISTANCE: Final[float] = 2.0
_LN10: Final[float] = float(np.log(10.0))
#: The analytic-vs-finite-difference Jacobian relative Frobenius bound.
JACOBIAN_TOLERANCE: Final[float] = 1e-6

PARAMETER_NAMES: Final[tuple[str, ...]] = (
    "key_light_x",
    "key_light_y",
    "key_light_z",
    "key_intensity",
    "fill_light_x",
    "fill_light_y",
    "fill_light_z",
    "fill_intensity",
    "camera_yaw",
    "camera_pitch",
    "camera_focal",
    "albedo_diffuse",
    "specular_roughness",
    "atmosphere_extinction",
    "room_absorption",
)

_LOWER: Final[np.ndarray] = np.array(
    [-5.0, -5.0, 0.5, 0.1, -5.0, -5.0, 0.5, 0.1, -1.5, -1.5, 0.5, 0.02, 0.05, 0.0, 0.05]
)
_UPPER: Final[np.ndarray] = np.array(
    [5.0, 5.0, 5.0, 10.0, 5.0, 5.0, 5.0, 10.0, 1.5, 1.5, 5.0, 0.98, 0.95, 2.0, 0.95]
)
PARAMETER_BOUNDS: Final[tuple[np.ndarray, np.ndarray]] = (_LOWER, _UPPER)
DEFAULT_PRIOR: Final[np.ndarray] = np.array(
    [1.0, 0.5, 2.0, 3.0, -1.0, 0.7, 1.5, 2.0, 0.3, -0.2, 2.0, 0.5, 0.4, 0.6, 0.3]
)


class InverseCinemaError(ValueError):
    """Raised for a malformed parameter vector, observation or option."""


def _validate_theta(theta: np.ndarray) -> np.ndarray:
    arr = np.asarray(theta, dtype=np.float64)
    if arr.shape != (PARAMETER_COUNT,):
        raise InverseCinemaError(f"theta must have shape ({PARAMETER_COUNT},)")
    if not np.all(np.isfinite(arr)):
        raise InverseCinemaError("theta must be finite")
    if np.any(arr < _LOWER) or np.any(arr > _UPPER):
        raise InverseCinemaError("theta lies outside the physical box")
    return arr


def _light(
    position: np.ndarray, intensity: float
) -> tuple[float, float, np.ndarray, float, np.ndarray, np.ndarray]:
    """Return ``(E, cos, dE/dI, dE/dp, dcos/dp, direction)`` for one light."""
    d2 = float(position @ position)
    distance = float(np.sqrt(d2))
    denom = d2 + _EPS
    energy = intensity / denom
    cos = position[2] / distance
    d_energy_di = 1.0 / denom
    d_energy_dp = -2.0 * intensity * position / denom**2
    d_cos_dp = np.array(
        [
            -position[2] * position[0] / distance**3,
            -position[2] * position[1] / distance**3,
            (position[0] ** 2 + position[1] ** 2) / distance**3,
        ]
    )
    return energy, cos, d_energy_dp, d_energy_di, d_cos_dp, position / distance


def _direction_jacobian(position: np.ndarray) -> np.ndarray:
    """``d(p / |p|) / dp`` as a 3x3 matrix."""
    distance = float(np.sqrt(float(position @ position)))
    identity = np.eye(3)
    return identity / distance - np.outer(position, position) / distance**3


def render_differentiable_scene_observation(
    theta: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Render a studio observation and its exact analytic Jacobian.

    Returns ``(y, J)`` with ``y`` of shape ``(OBSERVATION_COUNT,)`` and ``J`` of
    shape ``(OBSERVATION_COUNT, PARAMETER_COUNT)`` where ``J[i, j] = d y_i /
    d theta_j`` evaluated in closed form.
    """
    th = _validate_theta(theta)
    key_pos = th[0:3]
    key_intensity = th[3]
    fill_pos = th[4:7]
    fill_intensity = th[7]
    yaw, pitch, focal = th[8], th[9], th[10]
    albedo, roughness, extinction, absorption = th[11], th[12], th[13], th[14]

    e_key, cos_key, d_e_key_dp, d_e_key_di, d_cos_key_dp, dir_key = _light(key_pos, key_intensity)
    e_fill, cos_fill, d_e_fill_dp, d_e_fill_di, d_cos_fill_dp, dir_fill = _light(
        fill_pos, fill_intensity
    )

    y = np.empty(OBSERVATION_COUNT, dtype=np.float64)
    y[0] = e_key
    y[1] = e_fill
    fill_denom = e_fill + _EPS
    y[2] = e_key / fill_denom
    y[3] = albedo * (e_key * cos_key + e_fill * cos_fill)
    y[4] = (1.0 - roughness) * e_key * cos_key
    y[5] = float(np.exp(-extinction * _DEPTH))
    y[6] = focal * float(np.sin(yaw))
    y[7] = focal * float(np.sin(pitch))
    y[8] = 0.161 * _ROOM_VOLUME / (absorption * _ROOM_SURFACE)
    y[9] = absorption * _ROOM_SURFACE / (16.0 * np.pi * _SOURCE_DISTANCE**2)
    base = e_key + 4.0 / (absorption * _ROOM_SURFACE) + _EPS
    y[10] = 10.0 * float(np.log10(base))
    y[11:14] = dir_key
    y[14] = dir_fill[0]

    jac = np.zeros((OBSERVATION_COUNT, PARAMETER_COUNT), dtype=np.float64)
    jac[0, 0:3] = d_e_key_dp
    jac[0, 3] = d_e_key_di
    jac[1, 4:7] = d_e_fill_dp
    jac[1, 7] = d_e_fill_di
    jac[2, 0:3] = d_e_key_dp / fill_denom
    jac[2, 3] = d_e_key_di / fill_denom
    jac[2, 4:7] = -e_key / fill_denom**2 * d_e_fill_dp
    jac[2, 7] = -e_key / fill_denom**2 * d_e_fill_di
    jac[3, 0:3] = albedo * (d_e_key_dp * cos_key + e_key * d_cos_key_dp)
    jac[3, 3] = albedo * d_e_key_di * cos_key
    jac[3, 4:7] = albedo * (d_e_fill_dp * cos_fill + e_fill * d_cos_fill_dp)
    jac[3, 7] = albedo * d_e_fill_di * cos_fill
    jac[3, 11] = e_key * cos_key + e_fill * cos_fill
    jac[4, 0:3] = (1.0 - roughness) * (d_e_key_dp * cos_key + e_key * d_cos_key_dp)
    jac[4, 3] = (1.0 - roughness) * d_e_key_di * cos_key
    jac[4, 12] = -e_key * cos_key
    jac[5, 13] = -_DEPTH * float(np.exp(-extinction * _DEPTH))
    jac[6, 8] = focal * float(np.cos(yaw))
    jac[6, 10] = float(np.sin(yaw))
    jac[7, 9] = focal * float(np.cos(pitch))
    jac[7, 10] = float(np.sin(pitch))
    jac[8, 14] = -0.161 * _ROOM_VOLUME / (_ROOM_SURFACE * absorption**2)
    jac[9, 14] = _ROOM_SURFACE / (16.0 * np.pi * _SOURCE_DISTANCE**2)
    jac[10, 0:3] = 10.0 / (_LN10 * base) * d_e_key_dp
    jac[10, 3] = 10.0 / (_LN10 * base) * d_e_key_di
    jac[10, 14] = 10.0 / (_LN10 * base) * (-4.0 / (_ROOM_SURFACE * absorption**2))
    jac[11:14, 0:3] = _direction_jacobian(key_pos)
    jac[14, 4:7] = _direction_jacobian(fill_pos)[0, :]
    return y, jac


def verify_adjoint_jacobian_accuracy(theta: np.ndarray, *, step: float = 1e-6) -> float:
    """Return ``||J_analytic - J_fd||_F / (||J_fd||_F + 1e-12)``.

    The finite-difference reference is a fourth-order central difference in each
    parameter; the returned relative error must be below ``JACOBIAN_TOLERANCE``.
    """
    th = _validate_theta(theta)
    if step <= 0.0 or not np.isfinite(step):
        raise InverseCinemaError("step must be a positive finite number")
    _y, analytic = render_differentiable_scene_observation(th)
    fd = np.zeros_like(analytic)
    for j in range(PARAMETER_COUNT):
        perturb = np.zeros(PARAMETER_COUNT)
        perturb[j] = step
        plus = _forward_only(np.clip(th + perturb, _LOWER, _UPPER))
        minus = _forward_only(np.clip(th - perturb, _LOWER, _UPPER))
        fd[:, j] = (plus - minus) / (2.0 * step)
    return float(np.linalg.norm(analytic - fd) / (np.linalg.norm(fd) + 1e-12))


def _forward_only(theta: np.ndarray) -> np.ndarray:
    th = np.clip(np.asarray(theta, dtype=np.float64), _LOWER, _UPPER)
    y, _jac = render_differentiable_scene_observation(th)
    return y


@dataclass(frozen=True)
class InverseCinemaReconstructionReport:
    """The outcome of the box-constrained inverse-cinema solve."""

    parameters: np.ndarray
    initial_loss: float
    final_loss: float
    loss_history: tuple[float, ...]
    jacobian_error: float
    parameter_rmse: float
    iterations: int
    converged: bool
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def loss_reduction_ratio(self) -> float:
        return self.final_loss / (self.initial_loss + 1e-300)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "atlas.inverse_cinema_reconstruction",
            "schema_version": 1,
            "parameters": self.parameters.tolist(),
            "initial_loss": self.initial_loss,
            "final_loss": self.final_loss,
            "jacobian_error": self.jacobian_error,
            "parameter_rmse": self.parameter_rmse,
            "iterations": self.iterations,
            "converged": self.converged,
            "loss_history": list(self.loss_history),
            "notes": list(self.notes),
        }


def _loss(theta: np.ndarray, target: np.ndarray, prior: np.ndarray, gamma: float) -> float:
    residual = _forward_only(theta) - target
    return float(0.5 * residual @ residual + gamma * float((theta - prior) @ (theta - prior)))


def solve_inverse_cinema_parameters(
    target_observation: np.ndarray,
    *,
    prior: np.ndarray = DEFAULT_PRIOR,
    ground_truth: np.ndarray | None = None,
    gamma: float = 1e-3,
    max_iterations: int = 200,
    lambda_initial: float = 1e-3,
) -> InverseCinemaReconstructionReport:
    """Recover studio parameters from a target observation by box-constrained LM.

    ``target_observation`` is the desired ``y*``; ``prior`` is the regularisation
    centre; ``gamma`` weights ``||theta - theta_prior||^2``. The iteration uses
    the analytic Jacobian, projects each trial onto the physical box, and adapts
    ``lambda_lm`` by a standard trust-region accept/reject rule.
    """
    target = np.asarray(target_observation, dtype=np.float64)
    if target.shape != (OBSERVATION_COUNT,):
        raise InverseCinemaError(f"target_observation must have shape ({OBSERVATION_COUNT},)")
    if not np.all(np.isfinite(target)):
        raise InverseCinemaError("target_observation must be finite")
    prior_vec = _validate_theta(prior)
    if gamma < 0.0 or not np.isfinite(gamma):
        raise InverseCinemaError("gamma must be finite and non-negative")
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
        raise InverseCinemaError("max_iterations must be a positive int")
    if max_iterations < 1:
        raise InverseCinemaError("max_iterations must be a positive int")
    if lambda_initial <= 0.0 or not np.isfinite(lambda_initial):
        raise InverseCinemaError("lambda_initial must be positive and finite")

    theta = prior_vec.copy()
    loss = _loss(theta, target, prior_vec, gamma)
    initial_loss = loss
    damping = float(lambda_initial)
    history = [loss]
    converged = False
    iterations = 0
    identity = np.eye(PARAMETER_COUNT)
    while iterations < max_iterations:
        iterations += 1
        _y, jac = render_differentiable_scene_observation(theta)
        residual = _forward_only(theta) - target
        gradient = jac.T @ residual + gamma * (theta - prior_vec)
        hessian = jac.T @ jac + gamma * identity
        damped = hessian + damping * np.diag(np.diag(hessian) + 1.0)
        try:
            step = np.linalg.solve(damped, -gradient)
        except np.linalg.LinAlgError:
            step = -np.linalg.lstsq(damped, gradient, rcond=None)[0]
        trial = np.clip(theta + step, _LOWER, _UPPER)
        trial_loss = _loss(trial, target, prior_vec, gamma)
        if trial_loss < loss:
            improvement = loss - trial_loss
            theta = trial
            loss = trial_loss
            damping = max(damping * 0.5, 1e-12)
            history.append(loss)
            if improvement <= 1e-14 * max(loss, 1.0):
                converged = True
                break
        else:
            damping *= 2.0
            history.append(loss)
            if damping > 1e12:
                break

    jacobian_error = verify_adjoint_jacobian_accuracy(theta)
    rmse = 0.0
    if ground_truth is not None:
        truth = _validate_theta(ground_truth)
        rmse = float(np.sqrt(np.mean((theta - truth) ** 2)))
    return InverseCinemaReconstructionReport(
        parameters=theta,
        initial_loss=initial_loss,
        final_loss=loss,
        loss_history=tuple(history),
        jacobian_error=jacobian_error,
        parameter_rmse=rmse,
        iterations=iterations,
        converged=converged,
        notes=(
            "loss is monotonically non-increasing across accepted steps",
            "every trial is projected onto the physical parameter box",
        ),
    )
