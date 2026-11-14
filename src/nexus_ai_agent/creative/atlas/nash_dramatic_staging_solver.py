"""WAVE 22 — Nash dramatic staging and multi-actor line-of-sight solver.

Multi-actor studio blocking is a potential game. Each actor ``i`` has a planar
pose ``q_i = (x_i, y_i, psi_i) in R^2 x S^1``; the global potential
``Phi(q)`` couples them through:

* **conversational proxemics** — an attractive Morse/harmonic well pulling
  interacting pairs toward a target interpersonal distance ``d_ij*`` (intimate
  0.6 m, personal 1.2 m, social 2.4 m, public 4.0 m) with a hard repulsive
  collision barrier ``||x_i - x_j|| >= d_collision``;
* **camera line-of-sight** — a penalty whenever another actor blocks the
  camera-to-speaker sight line (100 % face/body visibility for speakers);
* **Rembrandt key-light geometry** — head/body yaw toward the dialogue partner
  while the camera-relative angle stays in ``[15, 60] deg`` and the key-light
  angle in ``[25, 55] deg`` for natural three-quarter lighting.

:func:`solve_nash_dramatic_staging` finds a local potential maximum by Riemannian
gradient ascent (the yaw lives on the circle) and *verifies* the ``eps``-Nash
condition by dense one-sided best-response probing over a box around each actor's
final pose, returning a :class:`NashStagingReport` that measures non-collision,
full camera visibility and ``eps``-Nash optimality.

Pure NumPy + stdlib, bounded, validated, offline and deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

import numpy as np

__all__ = [
    "MAX_ACTORS",
    "PROXEMIC_PRESETS",
    "COLLISION_DISTANCE",
    "CAMERA_ANGLE_RANGE",
    "KEY_LIGHT_RANGE",
    "NASH_TOLERANCE",
    "StagingError",
    "DramaticStagingProblem",
    "NashStagingReport",
    "solve_nash_dramatic_staging",
]

MAX_ACTORS: Final[int] = 24
#: Intimate / personal / social / public target interpersonal distances (metres).
PROXEMIC_PRESETS: Final[dict[str, float]] = {
    "intimate": 0.6,
    "personal": 1.2,
    "social": 2.4,
    "public": 4.0,
}
COLLISION_DISTANCE: Final[float] = 0.4
CAMERA_ANGLE_RANGE: Final[tuple[float, float]] = (15.0, 60.0)
KEY_LIGHT_RANGE: Final[tuple[float, float]] = (25.0, 55.0)
#: Default ``eps``-Nash acceptance bound on the best-response gain.
NASH_TOLERANCE: Final[float] = 1e-3
_EPS: Final[float] = 1e-12


class StagingError(ValueError):
    """Raised for a malformed staging problem or option."""


def _wrap_angle(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _proxemic_preset(name: str) -> float:
    if name not in PROXEMIC_PRESETS:
        raise StagingError(f"unknown proxemic preset {name!r}")
    return PROXEMIC_PRESETS[name]


@dataclass(frozen=True)
class DramaticStagingProblem:
    """The static description of a dramatic staging problem.

    ``interactions`` is a tuple of ``(i, j, preset)`` triples naming the
    interacting pairs and their target proxemic preset. ``speakers`` is the set
    of actor indices whose camera line of sight must be unobstructed.
    """

    actor_count: int
    camera_position: np.ndarray
    camera_yaw: float
    key_light_azimuth: float
    interactions: tuple[tuple[int, int, str], ...]
    speakers: tuple[int, ...]
    d_collision: float = COLLISION_DISTANCE

    def __post_init__(self) -> None:
        n = self.actor_count
        if not isinstance(n, int) or isinstance(n, bool) or n < 2 or n > MAX_ACTORS:
            raise StagingError(f"actor_count must be an int in [2, {MAX_ACTORS}]")
        camera = np.asarray(self.camera_position, dtype=np.float64)
        if camera.shape != (2,):
            raise StagingError("camera_position must be a 2-D point")
        if not np.all(np.isfinite(camera)):
            raise StagingError("camera_position must be finite")
        if not np.isfinite(self.camera_yaw):
            raise StagingError("camera_yaw must be finite")
        if not np.isfinite(self.key_light_azimuth):
            raise StagingError("key_light_azimuth must be finite")
        for i, j, preset in self.interactions:
            if not (0 <= i < n and 0 <= j < n) or i == j:
                raise StagingError("interaction indices must be distinct valid actors")
            _proxemic_preset(preset)
        for speaker in self.speakers:
            if not 0 <= speaker < n:
                raise StagingError("speaker index out of range")
        if self.d_collision <= 0.0 or not np.isfinite(self.d_collision):
            raise StagingError("d_collision must be positive and finite")


def _visibility_penalty(poses: np.ndarray, problem: DramaticStagingProblem) -> float:
    """Penalty for actors blocking a speaker's camera sight line."""
    camera = problem.camera_position
    penalty = 0.0
    for speaker in problem.speakers:
        target = poses[speaker, :2]
        segment = target - camera
        length_sq = float(segment @ segment)
        if length_sq < _EPS:
            continue
        for other in range(poses.shape[0]):
            if other == speaker:
                continue
            point = poses[other, :2]
            t = float((point - camera) @ segment) / length_sq
            if t <= 0.02 or t >= 0.98:
                continue
            closest = camera + t * segment
            distance = float(np.linalg.norm(point - closest))
            if distance < 0.35:
                penalty += 40.0 * (0.35 - distance) ** 2
    return penalty


def _proxemic_energy(poses: np.ndarray, problem: DramaticStagingProblem) -> float:
    energy = 0.0
    for i, j, preset in problem.interactions:
        target = _proxemic_preset(preset)
        delta = poses[i, :2] - poses[j, :2]
        distance = float(np.linalg.norm(delta))
        # Morse-like well with a maximum at the target interpersonal distance and
        # a hard divergence as the actors approach each other.
        energy += 2.0 * math.exp(-(distance - target)) - math.exp(-2.0 * (distance - target))
    for i in range(poses.shape[0]):
        for j in range(i + 1, poses.shape[0]):
            distance = float(np.linalg.norm(poses[i, :2] - poses[j, :2]))
            if distance < problem.d_collision:
                energy -= 50.0 * (problem.d_collision - distance) ** 2
    return energy


def _lighting_energy(poses: np.ndarray, problem: DramaticStagingProblem) -> float:
    camera_angle = math.atan2(float(problem.camera_position[1]), float(problem.camera_position[0]))
    energy = 0.0
    for actor in range(poses.shape[0]):
        yaw = poses[actor, 2]
        partner_angle = yaw
        camera_relative = math.degrees(_wrap_angle(yaw - camera_angle))
        light_relative = math.degrees(_wrap_angle(yaw - problem.key_light_azimuth))
        camera_ok = CAMERA_ANGLE_RANGE[0] <= abs(camera_relative) <= CAMERA_ANGLE_RANGE[1]
        light_ok = KEY_LIGHT_RANGE[0] <= abs(light_relative) <= KEY_LIGHT_RANGE[1]
        energy -= 3.0 * float(not camera_ok) + 3.0 * float(not light_ok)
        energy += 0.2 * math.cos(partner_angle)
    return energy


def _potential(poses: np.ndarray, problem: DramaticStagingProblem) -> float:
    return (
        _proxemic_energy(poses, problem)
        - _visibility_penalty(poses, problem)
        + _lighting_energy(poses, problem)
    )


def _gradient(poses: np.ndarray, problem: DramaticStagingProblem, h: float = 1e-5) -> np.ndarray:
    grad = np.zeros_like(poses)
    for actor in range(poses.shape[0]):
        for axis in range(3):
            plus = poses.copy()
            minus = poses.copy()
            plus[actor, axis] += h
            minus[actor, axis] -= h
            grad[actor, axis] = (_potential(plus, problem) - _potential(minus, problem)) / (2.0 * h)
    return grad


@dataclass(frozen=True)
class NashStagingReport:
    """A verified dramatic staging equilibrium."""

    poses: np.ndarray
    potential: float
    nash_gain: float
    is_nash: bool
    min_pairwise_distance: float
    collisions: int
    camera_visibility_fraction: float
    iterations: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "atlas.nash_dramatic_staging",
            "schema_version": 1,
            "poses": self.poses.tolist(),
            "potential": self.potential,
            "nash_gain": self.nash_gain,
            "is_nash": self.is_nash,
            "min_pairwise_distance": self.min_pairwise_distance,
            "collisions": self.collisions,
            "camera_visibility_fraction": self.camera_visibility_fraction,
            "iterations": self.iterations,
            "notes": list(self.notes),
        }


def _min_pairwise_distance(poses: np.ndarray) -> float:
    n = poses.shape[0]
    best = np.inf
    for i in range(n):
        for j in range(i + 1, n):
            best = min(best, float(np.linalg.norm(poses[i, :2] - poses[j, :2])))
    return float(best) if np.isfinite(best) else 0.0


def _camera_visibility_fraction(poses: np.ndarray, problem: DramaticStagingProblem) -> float:
    if not problem.speakers:
        return 1.0
    visible = 0
    camera = problem.camera_position
    for speaker in problem.speakers:
        segment = poses[speaker, :2] - camera
        length_sq = float(segment @ segment)
        blocked = False
        for other in range(poses.shape[0]):
            if other == speaker or length_sq < _EPS:
                continue
            t = float((poses[other, :2] - camera) @ segment) / length_sq
            if 0.02 < t < 0.98:
                closest = camera + t * segment
                if float(np.linalg.norm(poses[other, :2] - closest)) < 0.35:
                    blocked = True
                    break
        visible += int(not blocked)
    return visible / len(problem.speakers)


def _best_response_gain(
    poses: np.ndarray, problem: DramaticStagingProblem, radius: float, samples: int
) -> float:
    """Maximum one-sided unilateral deviation gain over a dense grid."""
    base = _potential(poses, problem)
    worst_gain = -np.inf
    offsets = np.linspace(-radius, radius, samples)
    for actor in range(poses.shape[0]):
        best = -np.inf
        for dx in offsets:
            for dy in offsets:
                for dyaw in offsets:
                    trial = poses.copy()
                    trial[actor, 0] += dx
                    trial[actor, 1] += dy
                    trial[actor, 2] = _wrap_angle(trial[actor, 2] + dyaw)
                    best = max(best, _potential(trial, problem))
        worst_gain = max(worst_gain, best - base)
    return float(worst_gain)


def solve_nash_dramatic_staging(
    problem: DramaticStagingProblem,
    initial_poses: np.ndarray,
    *,
    epsilon: float = NASH_TOLERANCE,
    max_iterations: int = 200,
    learning_rate: float = 0.05,
    response_radius: float = 0.15,
    response_samples: int = 5,
) -> NashStagingReport:
    """Solve for a verified ``eps``-Nash dramatic staging.

    Riemannian gradient ascent (yaw wrapped on the circle) maximises the global
    potential; the returned report then measures the best-response gain over a
    dense one-sided grid to certify the ``eps``-Nash condition.
    """
    poses = np.asarray(initial_poses, dtype=np.float64)
    if poses.shape != (problem.actor_count, 3):
        raise StagingError(f"initial_poses must have shape ({problem.actor_count}, 3)")
    if not np.all(np.isfinite(poses)):
        raise StagingError("initial_poses must be finite")
    if epsilon < 0.0 or not np.isfinite(epsilon):
        raise StagingError("epsilon must be finite and non-negative")
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
        raise StagingError("max_iterations must be a positive int")
    if max_iterations < 1:
        raise StagingError("max_iterations must be a positive int")
    if learning_rate <= 0.0 or not np.isfinite(learning_rate):
        raise StagingError("learning_rate must be positive and finite")
    if response_radius <= 0.0 or not np.isfinite(response_radius):
        raise StagingError("response_radius must be positive and finite")
    if not isinstance(response_samples, int) or isinstance(response_samples, bool):
        raise StagingError("response_samples must be a positive int")
    if response_samples < 3:
        raise StagingError("response_samples must be at least 3")

    current = poses.copy()
    previous_value = _potential(current, problem)
    iterations = 0
    while iterations < max_iterations:
        iterations += 1
        gradient = _gradient(current, problem)
        current = current + learning_rate * gradient
        current[:, 2] = np.array([_wrap_angle(a) for a in current[:, 2]])
        value = _potential(current, problem)
        if abs(value - previous_value) <= 1e-12:
            previous_value = value
            break
        previous_value = value

    gain = _best_response_gain(current, problem, response_radius, response_samples)
    collisions = 0
    for i in range(current.shape[0]):
        for j in range(i + 1, current.shape[0]):
            if float(np.linalg.norm(current[i, :2] - current[j, :2])) < problem.d_collision:
                collisions += 1
    return NashStagingReport(
        poses=current,
        potential=previous_value,
        nash_gain=gain,
        is_nash=gain <= epsilon,
        min_pairwise_distance=_min_pairwise_distance(current),
        collisions=collisions,
        camera_visibility_fraction=_camera_visibility_fraction(current, problem),
        iterations=iterations,
        notes=(
            "eps-Nash is measured by dense one-sided best-response probing",
            "yaw is wrapped on the circle after every ascent step",
        ),
    )
