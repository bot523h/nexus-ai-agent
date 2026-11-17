"""Law 2 — binocular vergence, saccade main sequence and pupillary light reflex.

"Zombie eyes" arise when the two optical axes diverge, when gaze drifts instead
of executing a ballistic saccade, and when pupils ignore scene luminance. This
module solves the exact binocular vergence geometry, synthesises Bahill main
sequence saccades with vestibulo-ocular compensation, and integrates the
Watson-Yellott (2012) unified pupillary light reflex with asymmetric iris
sphincter / dilator time constants.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

IPD_MIN_MM = 54.0
IPD_MAX_MM = 72.0
MAX_SKEW_MM = 0.5
V_MAX_DEG_S = 500.0
THETA_0_DEG = 15.0
TAU_CONSTRICT_S = 0.200
TAU_DILATE_S = 1.100
PUPIL_MIN_MM = 2.0
PUPIL_MAX_MM = 8.0


class OcularGeometryError(ValueError):
    """Raised for physically impossible binocular geometry."""


class PupilStimulusError(ValueError):
    """Raised for invalid luminance, age or field-area stimuli."""


@dataclass(frozen=True)
class VergenceSolution:
    """Exact binocular fixation solution."""

    axis_left: FloatArray
    axis_right: FloatArray
    vergence_deg: float
    skew_mm: float
    fixation: FloatArray
    violations_before: tuple[str, ...]

    @property
    def is_consistent(self) -> bool:
        return not self.violations_before


@dataclass(frozen=True)
class SaccadeTrajectory:
    """A sampled saccade trajectory with VOR compensation."""

    time_s: FloatArray
    eye_angle_deg: FloatArray
    eye_velocity_deg_s: FloatArray
    world_angle_deg: FloatArray
    peak_velocity_deg_s: float
    duration_s: float


@dataclass(frozen=True)
class PupilResponse:
    """A sampled pupillary light reflex."""

    time_s: FloatArray
    diameter_mm: FloatArray
    steady_state_mm: float
    constriction_mm: float


def _unit(vector: FloatArray) -> FloatArray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise OcularGeometryError("optical axis is degenerate")
    return vector / norm


def _closest_points(
    c_left: FloatArray, u_left: FloatArray, c_right: FloatArray, u_right: FloatArray
) -> tuple[FloatArray, FloatArray, float]:
    """Closest points on two 3D rays and their skew distance."""
    w0 = c_left - c_right
    a = float(u_left @ u_left)
    b = float(u_left @ u_right)
    c = float(u_right @ u_right)
    d = float(u_left @ w0)
    e = float(u_right @ w0)
    denom = a * c - b * b
    if abs(denom) < 1e-12:
        # Parallel axes: closest point is the perpendicular foot.
        s = 0.0
        t = e / c
    else:
        s = (b * e - c * d) / denom
        t = (a * e - b * d) / denom
    p_left = c_left + s * u_left
    p_right = c_right + t * u_right
    return p_left, p_right, float(np.linalg.norm(p_left - p_right))


def solve_binocular_vergence(
    c_left_mm: FloatArray,
    c_right_mm: FloatArray,
    fixation_mm: FloatArray,
    *,
    axis_left: FloatArray | None = None,
    axis_right: FloatArray | None = None,
    max_skew_mm: float = MAX_SKEW_MM,
) -> VergenceSolution:
    """Solve exact binocular vergence and project inconsistent gaze onto fixation."""
    left = np.asarray(c_left_mm, dtype=np.float64)
    right = np.asarray(c_right_mm, dtype=np.float64)
    target = np.asarray(fixation_mm, dtype=np.float64)
    if left.shape != (3,) or right.shape != (3,) or target.shape != (3,):
        raise OcularGeometryError("eye centres and fixation must be 3-vectors")
    if not (
        np.all(np.isfinite(left)) and np.all(np.isfinite(right)) and np.all(np.isfinite(target))
    ):
        raise OcularGeometryError("binocular inputs must be finite")
    ipd = float(np.linalg.norm(left - right))
    if not IPD_MIN_MM <= ipd <= IPD_MAX_MM:
        raise OcularGeometryError(f"interpupillary distance {ipd:.3f} mm outside [54, 72]")

    if axis_left is None:
        left_axis = _unit(target - left)
    else:
        left_axis = _unit(np.asarray(axis_left, dtype=np.float64))
    if axis_right is None:
        right_axis = _unit(target - right)
    else:
        right_axis = _unit(np.asarray(axis_right, dtype=np.float64))
    violations: list[str] = []
    vergence = float(np.degrees(np.arccos(np.clip(float(left_axis @ right_axis), -1.0, 1.0))))
    if vergence > 90.0:
        violations.append("divergent_gaze")
    _, _, skew = _closest_points(left, left_axis, right, right_axis)
    if skew > max_skew_mm:
        violations.append("non_intersecting_gaze")
    if "divergent_gaze" in violations or "non_intersecting_gaze" in violations:
        # Heal: both eyes converge on the true fixation point, which is the
        # only physically admissible gaze that satisfies binocular fusion.
        left_axis = _unit(target - left)
        right_axis = _unit(target - right)
        vergence = float(np.degrees(np.arccos(np.clip(float(left_axis @ right_axis), -1.0, 1.0))))
        _, _, skew = _closest_points(left, left_axis, right, right_axis)
    return VergenceSolution(
        axis_left=left_axis,
        axis_right=right_axis,
        vergence_deg=vergence,
        skew_mm=skew,
        fixation=target,
        violations_before=tuple(violations),
    )


def synthesize_saccade_vor_trajectory(
    delta_theta_deg: float,
    duration_s: float,
    *,
    head_angular_velocity_deg_s: float = 0.0,
    sample_rate_hz: float = 1000.0,
    v_max_deg_s: float = V_MAX_DEG_S,
    theta_0_deg: float = THETA_0_DEG,
) -> SaccadeTrajectory:
    """Synthesise a Bahill main-sequence saccade with vestibulo-ocular reflex."""
    if not np.isfinite(delta_theta_deg) or not np.isfinite(duration_s):
        raise OcularGeometryError("saccade parameters must be finite")
    if duration_s <= 0.0:
        raise OcularGeometryError("saccade duration must be positive")
    if sample_rate_hz <= 0.0:
        raise OcularGeometryError("sample rate must be positive")
    if v_max_deg_s <= 0.0 or theta_0_deg <= 0.0:
        raise OcularGeometryError("main-sequence constants must be positive")
    samples = max(2, int(round(duration_s * sample_rate_hz)) + 1)
    time_s = np.linspace(0.0, duration_s, samples, dtype=np.float64)
    magnitude = abs(delta_theta_deg)
    peak_velocity = v_max_deg_s * (1.0 - np.exp(-magnitude / theta_0_deg))
    # Minimum-jerk position profile scaled to the ballistic peak velocity.
    tau = time_s / duration_s
    profile = 10.0 * tau**3 - 15.0 * tau**4 + 6.0 * tau**5
    eye_angle = delta_theta_deg * profile
    eye_velocity = np.gradient(eye_angle, time_s, edge_order=1)
    world_angle = eye_angle + head_angular_velocity_deg_s * time_s
    return SaccadeTrajectory(
        time_s=time_s,
        eye_angle_deg=eye_angle,
        eye_velocity_deg_s=eye_velocity,
        world_angle_deg=world_angle,
        peak_velocity_deg_s=float(peak_velocity),
        duration_s=duration_s,
    )


def _watson_yellott_steady_state(
    luminance_cd_m2: float, field_area_deg2: float, age_years: float, binocular: float
) -> float:
    base = 4.9 - 3.0 * np.tanh(0.4 * np.log10(luminance_cd_m2))
    field = 0.25 * np.log10(1.0 + field_area_deg2)
    age = 0.02 * (age_years - 30.0)
    binocular_gain = 1.0 - 0.05 * binocular
    diameter = (base + field + age) * binocular_gain
    return float(np.clip(diameter, PUPIL_MIN_MM, PUPIL_MAX_MM))


def compute_watson_yellott_pupil_diameter(
    luminance_cd_m2: float,
    field_area_deg2: float = 100.0,
    age_years: float = 30.0,
    binocular: float = 1.0,
    *,
    duration_s: float = 2.0,
    sample_rate_hz: float = 200.0,
    initial_diameter_mm: float | None = None,
) -> PupilResponse:
    """Integrate the Watson-Yellott steady-state pupil through an asymmetric ODE."""
    if not np.isfinite(luminance_cd_m2) or luminance_cd_m2 <= 0.0:
        raise PupilStimulusError("luminance must be positive and finite")
    if not np.isfinite(field_area_deg2) or field_area_deg2 <= 0.0:
        raise PupilStimulusError("field area must be positive and finite")
    if not 0.0 <= age_years <= 120.0:
        raise PupilStimulusError("age must lie in [0, 120] years")
    if not 0.0 <= binocular <= 1.0:
        raise PupilStimulusError("binocular factor must lie in [0, 1]")
    if duration_s <= 0.0 or sample_rate_hz <= 0.0:
        raise PupilStimulusError("duration and sample rate must be positive")

    target = _watson_yellott_steady_state(luminance_cd_m2, field_area_deg2, age_years, binocular)
    start = PUPIL_MAX_MM if initial_diameter_mm is None else float(initial_diameter_mm)
    if not PUPIL_MIN_MM <= start <= PUPIL_MAX_MM:
        raise PupilStimulusError("initial pupil diameter must lie in [2, 8] mm")

    steps = max(1, int(round(duration_s * sample_rate_hz)))
    dt = duration_s / steps
    diameter = np.empty(steps + 1, dtype=np.float64)
    diameter[0] = start
    for index in range(steps):
        tau = TAU_CONSTRICT_S if target < diameter[index] else TAU_DILATE_S
        diameter[index + 1] = diameter[index] + (target - diameter[index]) * (
            1.0 - np.exp(-dt / tau)
        )
    time_s = np.linspace(0.0, duration_s, steps + 1, dtype=np.float64)
    return PupilResponse(
        time_s=time_s,
        diameter_mm=diameter,
        steady_state_mm=target,
        constriction_mm=float(start - target),
    )
