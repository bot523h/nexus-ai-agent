"""Wave 10: Neuromuscular Hill FACS Actuator, TMJ SE(3) Jaw Kinematics & Bilabial Coarticulation.

Implements first-order neuromuscular activation ODEs, Hill-type force-length-velocity laws
with antagonistic inhibition, anatomical Temporomandibular Joint (TMJ) coupled hinge-slide
SE(3) kinematics, and audio-driven articulatory coarticulation with exact bilabial contact locks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

# Canonical FACS Action Unit IDs
CANONICAL_FACS_AUS: Final[tuple[str, ...]] = (
    "AU1",  # Inner Brow Raiser
    "AU2",  # Outer Brow Raiser
    "AU4",  # Brow Lowerer
    "AU6",  # Cheek Raiser
    "AU9",  # Nose Wrinkler
    "AU12",  # Lip Corner Puller (Zygomaticus major)
    "AU15",  # Lip Corner Depressor
    "AU17",  # Chin Raiser (Mentalis)
    "AU20",  # Lip Stretcher (Risorius)
    "AU22",  # Lip Funneler / Pucker (Orbicularis oris)
    "AU25",  # Lips Part
    "AU26",  # Jaw Drop
    "AU27",  # Mouth Stretch
)


@dataclass(frozen=True)
class HillMuscleParameters:
    """Parameters for active-passive Hill-type neuromuscular muscle model."""

    f_max_n: float = 80.0  # Maximum isometric force (Newtons)
    optimal_length_mm: float = 35.0  # Optimal fiber length (mm)
    gamma_l: float = 0.45  # Width of force-length Gaussian bell
    k_v: float = 1.33  # Force-velocity hyperbolic curvature
    k_p: float = 4.0  # Passive exponential stiffness
    tau_act_s: float = 0.020  # Activation time constant (seconds)
    tau_deact_s: float = 0.040  # Deactivation time constant (seconds)

    def __post_init__(self) -> None:
        if self.f_max_n <= 0.0 or self.optimal_length_mm <= 0.0:
            raise ValueError("f_max_n and optimal_length_mm must be positive")
        if self.tau_act_s <= 0.0 or self.tau_deact_s <= 0.0:
            raise ValueError("Time constants tau_act and tau_deact must be positive")


def step_first_order_activation(
    current_activation: float,
    target_excitation: float,
    dt_s: float,
    params: HillMuscleParameters,
) -> float:
    """Integrate neuromuscular activation ODE: da/dt = (u - a) / tau(u, a)."""
    if dt_s <= 0.0:
        raise ValueError(f"dt_s must be positive, got {dt_s}")
    u = float(np.clip(target_excitation, 0.0, 1.0))
    a = float(np.clip(current_activation, 0.0, 1.0))

    if u > a:
        tau = params.tau_act_s * (0.5 + 1.5 * a)
    else:
        tau = params.tau_deact_s / (0.5 + 1.5 * a)

    da_dt = (u - a) / max(tau, 1e-4)
    a_next = a + da_dt * dt_s
    return float(np.clip(a_next, 0.0, 1.0))


def compute_hill_muscle_force(
    activation: float,
    normalized_length: float,
    normalized_velocity: float,
    params: HillMuscleParameters,
) -> float:
    """Evaluate active-passive Hill force: F = F_max * (F_act + F_pas)."""
    a = float(np.clip(activation, 0.0, 1.0))
    l_tilde = float(normalized_length)
    v_tilde = float(np.clip(normalized_velocity, -1.0, 1.0))

    # Active force-length: Gaussian bell around optimal length l_tilde = 1.0
    f_l = np.exp(-((l_tilde - 1.0) ** 2) / max(params.gamma_l, 1e-4))

    # Active force-velocity: Hill hyperbolic curve
    if v_tilde <= 0.0:  # Concentric shortening (v < 0)
        f_v = (1.0 + v_tilde) / (1.0 - params.k_v * v_tilde)
    else:  # Eccentric lengthening (v > 0)
        f_v = (1.0 + 1.5 * v_tilde) / (1.0 + params.k_v * v_tilde)

    f_act = a * f_l * max(f_v, 0.0)

    # Passive force: exponential resistance to overstretching beyond l_tilde = 1.0
    if l_tilde > 1.0:
        f_pas = np.exp(params.k_p * (l_tilde - 1.0)) - 1.0
    else:
        f_pas = 0.0

    total_normalized = f_act + f_pas
    return float(params.f_max_n * total_normalized)


def apply_antagonistic_inhibition(
    raw_activations: dict[str, float],
    max_co_contraction_energy: float = 1.2,
) -> dict[str, float]:
    """Inhibit antagonistic FACS muscle pairs exceeding physiological co-contraction bounds.

    Antagonistic pairs:
      1. AU12 (Zygomaticus major pull) vs AU15 (Depressor anguli oris pull)
      2. AU22 (Orbicularis oris pucker) vs AU20 (Risorius stretcher)
    """
    out = dict(raw_activations)

    # Pair 1: AU12 vs AU15
    a12 = out.get("AU12", 0.0)
    a15 = out.get("AU15", 0.0)
    sum_1 = a12 + a15
    if sum_1 > max_co_contraction_energy:
        scale = max_co_contraction_energy / sum_1
        out["AU12"] = a12 * scale
        out["AU15"] = a15 * scale

    # Pair 2: AU22 vs AU20
    a22 = out.get("AU22", 0.0)
    a20 = out.get("AU20", 0.0)
    sum_2 = a22 + a20
    if sum_2 > max_co_contraction_energy:
        scale = max_co_contraction_energy / sum_2
        out["AU22"] = a22 * scale
        out["AU20"] = a20 * scale

    return out


# ── Anatomical Temporomandibular Joint (TMJ) SE(3) Coupled Kinematics ─────────


@dataclass(frozen=True)
class TMJKinematicState:
    """Rigid SE(3) transformation of the mandible with hinge rotation and condylar slide."""

    jaw_opening_rad: float  # theta in [0, theta_max]
    rotation_matrix: np.ndarray  # shape: (3, 3) in SO(3)
    translation_vector: np.ndarray  # shape: (3,) in mm
    condyle_slide_displacement_mm: np.ndarray  # shape: (3,)
    chin_clearance_mm: float  # vertical clearance above cervical neck threshold


def evaluate_tmj_coupled_se3(
    jaw_opening_rad: float,
    theta_max_rad: float = 0.45,  # ~25.8 degrees maximum natural opening
    slide_max_mm: float = 12.0,  # Condyle translation along articular eminence
    eminence_angle_rad: float = 0.52,  # ~30 degrees downward anterior slide
    mandible_chin_rest: np.ndarray | None = None,
    neck_boundary_z: float = -60.0,
) -> TMJKinematicState:
    """Compute exact SE(3) TMJ hinge-slide transform guaranteeing det(R) = +1.0.

    Translation combines pitch rotation around condylar hinge axis (x-axis)
    and anterior-inferior condylar translation along temporal eminence curve.
    """
    if not (0.0 <= jaw_opening_rad <= theta_max_rad * 1.2):
        raise ValueError(
            f"jaw_opening_rad must be in [0, {theta_max_rad * 1.2}], got {jaw_opening_rad}"
        )
    theta = float(np.clip(jaw_opening_rad, 0.0, theta_max_rad))

    # Condylar pitch rotation R_x(theta)
    c, s = np.cos(theta), np.sin(theta)
    rot = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, c, -s],
            [0.0, s, c],
        ],
        dtype=np.float64,
    )

    # Condylar translation along articular eminence using cubic smoothstep
    u = theta / max(theta_max_rad, 1e-4)
    phi = 3.0 * (u**2) - 2.0 * (u**3)  # smoothstep in [0, 1]

    # Slide direction: anterior (+y) and inferior (-z)
    slide_y = slide_max_mm * np.sin(eminence_angle_rad) * phi
    slide_z = -slide_max_mm * np.cos(eminence_angle_rad) * phi
    t_slide = np.array([0.0, slide_y, slide_z], dtype=np.float64)

    # Evaluate chin tip position to verify cervical neck clearance
    chin_ref = (
        np.array([0.0, 45.0, -80.0], dtype=np.float64)
        if mandible_chin_rest is None
        else mandible_chin_rest
    )
    chin_transformed = rot @ chin_ref + t_slide
    clearance = float(chin_transformed[2] - neck_boundary_z)

    # Enforce non-collision with neck
    if clearance < 0.0:
        # Prevent unphysical neck penetration by clamping translation
        t_slide[2] -= clearance
        clearance = 0.0

    return TMJKinematicState(
        jaw_opening_rad=theta,
        rotation_matrix=rot,
        translation_vector=t_slide,
        condyle_slide_displacement_mm=t_slide,
        chin_clearance_mm=clearance,
    )


# ── Audio-Driven Articulatory Coarticulation & Exact Bilabial Contact Lock ───


@dataclass(frozen=True)
class AudioAcousticFeatures:
    """Extracted acoustic cues from 16-bit 48 kHz PCM audio window."""

    rms_energy: float
    zero_crossing_rate: float
    spectral_centroid_hz: float
    is_bilabial_closure: bool


def extract_audio_acoustic_features(
    samples_pcm16: np.ndarray,
    sample_rate: int = 48000,
    bilabial_energy_threshold: float = 0.005,
    bilabial_zcr_threshold: float = 0.08,
) -> AudioAcousticFeatures:
    """Analyze audio frame to detect speech energy and bilabial consonant closures."""
    if samples_pcm16.dtype != np.int16:
        raise ValueError(f"samples must be int16, got {samples_pcm16.dtype}")
    if samples_pcm16.size == 0:
        return AudioAcousticFeatures(0.0, 0.0, 0.0, False)

    float_samples = samples_pcm16.astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(float_samples**2)))

    # Zero-crossing rate
    signs = np.sign(float_samples)
    zcr = float(np.mean(np.abs(signs[1:] - signs[:-1])) / 2.0)

    # Spectral centroid via real FFT
    fft_mag = np.abs(np.fft.rfft(float_samples))
    freqs = np.fft.rfftfreq(len(float_samples), d=1.0 / sample_rate)
    sum_mag = float(np.sum(fft_mag))
    centroid = float(np.sum(freqs * fft_mag) / max(sum_mag, 1e-8))

    # Bilabial plosive/nasal detection: low energy and low zero-crossing rate
    is_bilabial = bool(rms < bilabial_energy_threshold and zcr < bilabial_zcr_threshold)

    return AudioAcousticFeatures(
        rms_energy=rms,
        zero_crossing_rate=zcr,
        spectral_centroid_hz=centroid,
        is_bilabial_closure=is_bilabial,
    )


def solve_bilabial_contact_and_lip_seal(
    upper_lip_vertices: np.ndarray,
    lower_lip_vertices: np.ndarray,
    is_closure_frame: bool,
    poisson_expansion_factor: float = 0.35,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Enforce exact bilabial contact lock: d_lip >= 0, zero lip interpenetration.

    During bilabial closure (/p/, /b/, /m/), project upper and lower lip margins onto
    the contact mid-plane and expand lateral lip width by Poisson volume conservation.
    Returns: (projected_upper, projected_lower, lip_distance_mm).
    """
    if upper_lip_vertices.ndim != 2 or upper_lip_vertices.shape[1] != 3:
        raise ValueError("upper_lip_vertices must be (N, 3)")
    if lower_lip_vertices.ndim != 2 or lower_lip_vertices.shape[1] != 3:
        raise ValueError("lower_lip_vertices must be (N, 3)")
    if upper_lip_vertices.shape[0] != lower_lip_vertices.shape[0]:
        raise ValueError("Upper and lower lip vertex counts must match")

    u_pos = upper_lip_vertices.copy()
    l_pos = lower_lip_vertices.copy()

    # Vertical distance: y_upper - y_lower
    # In canonical head frame, +y is superior/up, -y is inferior/down
    d_lips = u_pos[:, 1] - l_pos[:, 1]
    min_dist = float(np.min(d_lips))

    if is_closure_frame or min_dist < 0.0:
        # Enforce sealed mid-plane contact: y_contact = 0.5 * (y_upper + y_lower)
        y_mid = 0.5 * (u_pos[:, 1] + l_pos[:, 1])
        compression = np.maximum(0.0, -d_lips if min_dist < 0.0 else 2.0)

        # Pin vertical coordinates to contact plane
        u_pos[:, 1] = y_mid
        l_pos[:, 1] = y_mid

        # Lateral Poisson bulging along x-axis (lip widening upon compression)
        lateral_sign = np.sign(u_pos[:, 0])
        bulge = lateral_sign * compression * poisson_expansion_factor
        u_pos[:, 0] += bulge
        l_pos[:, 0] += bulge

        final_dist = 0.0
    else:
        final_dist = max(0.0, min_dist)

    return (
        u_pos,
        l_pos,
        final_dist,
    )
