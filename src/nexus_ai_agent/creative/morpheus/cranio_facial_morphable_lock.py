"""Cranio-Facial Morphable Model with Subspace Lock and Umeyama Alignment.

Wave 1 of NAGAR MORPHEUS:
Provides orthogonalized 3DMM identity and expression decomposition,
reflection-free Sim(3)/SE(3) Umeyama alignment, cephalometric anthropometric
invariant extraction, and Tikhonov-regularized expression recovery under a
cryptographically locked cranial identity basis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np

CANONICAL_VERTEX_COUNT: int = 68
RIGID_CRANIAL_ANCHOR_INDICES: tuple[int, ...] = (0, 16, 27, 28, 29, 30, 36, 39, 42, 45)


@dataclass(frozen=True)
class CranioFacialBasis:
    """Immutable orthonormalized 3DMM cranio-facial subspace basis."""

    mean_mesh: np.ndarray  # shape: (V, 3)
    u_id: np.ndarray  # shape: (3V, K_id)
    u_exp: np.ndarray  # shape: (3V, K_exp)
    vertex_count: int
    k_id: int
    k_exp: int

    def __post_init__(self) -> None:
        if self.mean_mesh.ndim != 2 or self.mean_mesh.shape[1] != 3:
            raise ValueError(f"mean_mesh must have shape (V, 3), got {self.mean_mesh.shape}")
        v = self.mean_mesh.shape[0]
        if v < CANONICAL_VERTEX_COUNT:
            raise ValueError(f"Vertex count {v} is less than minimum {CANONICAL_VERTEX_COUNT}")
        if self.u_id.shape != (3 * v, self.k_id):
            raise ValueError(
                f"u_id shape mismatch: expected ({3 * v}, {self.k_id}), got {self.u_id.shape}"
            )
        if self.u_exp.shape != (3 * v, self.k_exp):
            raise ValueError(
                f"u_exp shape mismatch: expected ({3 * v}, {self.k_exp}), got {self.u_exp.shape}"
            )
        if np.isnan(self.mean_mesh).any() or np.isinf(self.mean_mesh).any():
            raise ValueError("mean_mesh contains NaN or Inf values")
        if np.isnan(self.u_id).any() or np.isinf(self.u_id).any():
            raise ValueError("u_id contains NaN or Inf values")
        if np.isnan(self.u_exp).any() or np.isinf(self.u_exp).any():
            raise ValueError("u_exp contains NaN or Inf values")


@dataclass(frozen=True)
class Sim3Transform:
    """Reflection-free similarity transformation (scale, rotation, translation)."""

    rotation: np.ndarray  # shape: (3, 3), det(R) = +1.0
    translation: np.ndarray  # shape: (3,)
    scale: float

    def __post_init__(self) -> None:
        if self.rotation.shape != (3, 3):
            raise ValueError(f"rotation must be (3, 3), got {self.rotation.shape}")
        if self.translation.shape != (3,):
            raise ValueError(f"translation must be (3,), got {self.translation.shape}")
        det = float(np.linalg.det(self.rotation))
        if abs(det - 1.0) > 1e-7:
            raise ValueError(f"Rotation matrix must have det(R) = +1.0, got {det}")
        if self.scale <= 0.0 or np.isnan(self.scale) or np.isinf(self.scale):
            raise ValueError(f"scale must be strictly positive, got {self.scale}")

    def transform(self, points: np.ndarray) -> np.ndarray:
        """Apply transform: Y = s * (X @ R.T) + t."""
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError(f"points must have shape (N, 3), got {points.shape}")
        res = self.scale * (points @ self.rotation.T) + self.translation
        return cast(np.ndarray, res)

    def inverse_transform(self, points: np.ndarray) -> np.ndarray:
        """Apply inverse transform: X = (1/s) * (Y - t) @ R."""
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError(f"points must have shape (N, 3), got {points.shape}")
        res = (points - self.translation) @ self.rotation / self.scale
        return cast(np.ndarray, res)


@dataclass(frozen=True)
class CephalometricRatios:
    """Invariant anthropometric cephalometric cranio-facial ratios."""

    interpupillary_to_bizygomatic: float
    nasion_menton_to_bizygomatic: float
    cranial_vault_symmetry: float

    def __post_init__(self) -> None:
        for name, val in [
            ("interpupillary_to_bizygomatic", self.interpupillary_to_bizygomatic),
            ("nasion_menton_to_bizygomatic", self.nasion_menton_to_bizygomatic),
            ("cranial_vault_symmetry", self.cranial_vault_symmetry),
        ]:
            if val <= 0.0 or np.isnan(val) or np.isinf(val):
                raise ValueError(f"{name} must be finite positive, got {val}")


@dataclass(frozen=True)
class CranialLockResult:
    """Full forensic audit result from identity locking and expression healing."""

    aligned_observed_mesh: np.ndarray  # shape: (V, 3)
    raw_identity_drift: float
    solved_expression: np.ndarray  # shape: (K_exp,)
    healed_canonical_mesh: np.ndarray  # shape: (V, 3)
    healed_world_mesh: np.ndarray  # shape: (V, 3)
    post_heal_identity_drift: float
    cephalometric_drift: float
    alignment: Sim3Transform


def build_canonical_3dmm_basis(
    vertex_count: int = CANONICAL_VERTEX_COUNT,
    k_id: int = 10,
    k_exp: int = 10,
    seed: int = 42,
) -> CranioFacialBasis:
    """Construct a canonical 3DMM basis with identity-expression subspace orthogonality.

    Guarantees ||U_id.T @ U_exp||_F <= 1e-12 by projecting the raw expression
    basis out of the identity subspace and orthonormalizing via QR decomposition.
    """
    if vertex_count < CANONICAL_VERTEX_COUNT:
        raise ValueError(
            f"vertex_count {vertex_count} is less than minimum {CANONICAL_VERTEX_COUNT}"
        )
    if k_id <= 0 or k_exp <= 0:
        raise ValueError(f"Subspace dimensions must be positive, got k_id={k_id}, k_exp={k_exp}")
    rng = np.random.default_rng(seed)

    # 1. Synthesize an anatomical 68-vertex cranio-facial mean mesh
    mean_mesh = np.zeros((vertex_count, 3), dtype=np.float64)
    # Mandible / jaw outline (0..16)
    theta_jaw = np.linspace(-np.pi * 0.75, np.pi * 0.75, 17)
    mean_mesh[:17, 0] = 70.0 * np.sin(theta_jaw)
    mean_mesh[:17, 1] = -40.0 * np.cos(theta_jaw) - 20.0
    mean_mesh[:17, 2] = -20.0 * np.cos(theta_jaw)

    # Eyebrows (17..21 left, 22..26 right)
    mean_mesh[17:22, 0] = np.linspace(-55.0, -15.0, 5)
    mean_mesh[17:22, 1] = 35.0 + 5.0 * np.sin(np.linspace(0, np.pi, 5))
    mean_mesh[17:22, 2] = 20.0
    mean_mesh[22:27, 0] = np.linspace(15.0, 55.0, 5)
    mean_mesh[22:27, 1] = 35.0 + 5.0 * np.sin(np.linspace(0, np.pi, 5))
    mean_mesh[22:27, 2] = 20.0

    # Nose bridge and tip (27..35)
    mean_mesh[27:31, 0] = 0.0
    mean_mesh[27:31, 1] = np.linspace(25.0, -5.0, 4)
    mean_mesh[27:31, 2] = np.linspace(25.0, 45.0, 4)
    mean_mesh[31:36, 0] = np.linspace(-15.0, 15.0, 5)
    mean_mesh[31:36, 1] = -10.0
    mean_mesh[31:36, 2] = 40.0

    # Eyes (36..41 left, 42..47 right)
    for i in range(6):
        ang = 2.0 * np.pi * i / 6.0
        mean_mesh[36 + i, 0] = -35.0 + 10.0 * np.cos(ang)
        mean_mesh[36 + i, 1] = 20.0 + 5.0 * np.sin(ang)
        mean_mesh[36 + i, 2] = 18.0
        mean_mesh[42 + i, 0] = 35.0 + 10.0 * np.cos(ang)
        mean_mesh[42 + i, 1] = 20.0 + 5.0 * np.sin(ang)
        mean_mesh[42 + i, 2] = 18.0

    # Lips (48..67)
    for i in range(12):
        ang = 2.0 * np.pi * i / 12.0
        mean_mesh[48 + i, 0] = 25.0 * np.cos(ang)
        mean_mesh[48 + i, 1] = -35.0 + 12.0 * np.sin(ang)
        mean_mesh[48 + i, 2] = 25.0
    for i in range(8):
        ang = 2.0 * np.pi * i / 8.0
        mean_mesh[60 + i, 0] = 18.0 * np.cos(ang)
        mean_mesh[60 + i, 1] = -35.0 + 8.0 * np.sin(ang)
        mean_mesh[60 + i, 2] = 24.0

    # Extra cranial vertices if vertex_count > 68
    if vertex_count > 68:
        for idx in range(68, vertex_count):
            phi = rng.uniform(0.0, 2.0 * np.pi)
            costheta = rng.uniform(0.0, 1.0)
            theta = np.arccos(costheta)
            r = 75.0
            mean_mesh[idx] = [
                r * np.sin(theta) * np.cos(phi),
                r * np.sin(theta) * np.sin(phi) + 20.0,
                r * np.cos(theta),
            ]

    # 2. Generate raw basis matrices
    d = 3 * vertex_count
    raw_id = rng.normal(0.0, 1.0, (d, k_id))
    q_id, _ = np.linalg.qr(raw_id)
    u_id = q_id[:, :k_id]

    raw_exp = rng.normal(0.0, 1.0, (d, k_exp))
    # Explicitly project out identity subspace
    exp_projected = raw_exp - u_id @ (u_id.T @ raw_exp)
    q_exp, _ = np.linalg.qr(exp_projected)
    u_exp = q_exp[:, :k_exp]

    # Verify orthogonality: ||U_id.T @ U_exp||_F <= 1e-12
    cross_overlap = np.linalg.norm(u_id.T @ u_exp, ord="fro")
    if cross_overlap > 1e-12:
        # Re-project to enforce machine-precision zero
        u_exp = u_exp - u_id @ (u_id.T @ u_exp)
        q_exp_clean, _ = np.linalg.qr(u_exp)
        u_exp = q_exp_clean[:, :k_exp]

    return CranioFacialBasis(
        mean_mesh=mean_mesh,
        u_id=u_id,
        u_exp=u_exp,
        vertex_count=vertex_count,
        k_id=k_id,
        k_exp=k_exp,
    )


def synthesize_face_mesh(
    basis: CranioFacialBasis,
    alpha_id: np.ndarray,
    beta_exp: np.ndarray,
) -> np.ndarray:
    """Synthesize surface mesh: S = S_bar + U_id @ alpha_id + U_exp @ beta_exp."""
    if alpha_id.shape != (basis.k_id,):
        raise ValueError(f"alpha_id shape mismatch: expected ({basis.k_id},), got {alpha_id.shape}")
    if beta_exp.shape != (basis.k_exp,):
        raise ValueError(
            f"beta_exp shape mismatch: expected ({basis.k_exp},), got {beta_exp.shape}"
        )
    if np.isnan(alpha_id).any() or np.isnan(beta_exp).any():
        raise ValueError("alpha_id or beta_exp contains NaN")

    flat_mean = basis.mean_mesh.reshape(-1)
    flat_mesh = flat_mean + basis.u_id @ alpha_id + basis.u_exp @ beta_exp
    return cast(np.ndarray, flat_mesh.reshape(basis.vertex_count, 3))


def umeyama_sim3_align(
    source: np.ndarray,
    target: np.ndarray,
    allow_scale: bool = True,
) -> Sim3Transform:
    """Reflection-free Umeyama Sim(3) or SE(3) alignment guaranteeing det(R) = +1.0.

    Aligns source points to target points:
        target ≈ scale * (source @ R.T) + translation
    Reflections (det(V @ U.T) = -1) are healed via reflection matrix D = diag(1, 1, det).
    """
    if source.ndim != 2 or source.shape[1] != 3:
        raise ValueError(f"source must be (N, 3), got {source.shape}")
    if target.ndim != 2 or target.shape[1] != 3:
        raise ValueError(f"target must be (N, 3), got {target.shape}")
    if source.shape[0] != target.shape[0]:
        raise ValueError(
            f"Point count mismatch: source {source.shape[0]} vs target {target.shape[0]}"
        )
    n = source.shape[0]
    if n < 3:
        raise ValueError(f"At least 3 anchor points required for 3D alignment, got {n}")
    if np.isnan(source).any() or np.isnan(target).any():
        raise ValueError("source or target contains NaN")

    mu_x = np.mean(source, axis=0)
    mu_y = np.mean(target, axis=0)

    x_c = source - mu_x
    y_c = target - mu_y

    var_x = float(np.sum(x_c**2) / n)
    if var_x < 1e-12:
        raise ValueError(f"Degenerate source points with near-zero variance {var_x}")

    h = (x_c.T @ y_c) / n
    u, s_vals, vt = np.linalg.svd(h)
    v = vt.T

    # Compute candidate rotation and enforce det(R) = +1
    det_rot = float(np.linalg.det(v @ u.T))
    d = np.diag([1.0, 1.0, 1.0 if det_rot >= 0 else -1.0])

    rotation = v @ d @ u.T
    det_final = float(np.linalg.det(rotation))
    if abs(det_final - 1.0) > 1e-7:
        raise ValueError(f"Umeyama failed to heal reflection: det(R) = {det_final}")

    if allow_scale:
        scale = float(np.sum(s_vals * np.diag(d)) / var_x)
        if scale <= 1e-9:
            scale = 1.0
    else:
        scale = 1.0

    translation = mu_y - scale * (rotation @ mu_x)

    return Sim3Transform(
        rotation=rotation,
        translation=translation,
        scale=scale,
    )


def compute_cephalometric_ratios(mesh: np.ndarray) -> CephalometricRatios:
    """Extract invariant anthropometric cranio-facial cephalometric ratios.

    Extracts:
      1. Interpupillary distance to bizygomatic width ratio (IP / BZ).
      2. Nasion-Menton distance to bizygomatic width index (NM / BZ).
      3. Cranial vault sagittal symmetry balance.
    """
    if mesh.ndim != 2 or mesh.shape[1] != 3 or mesh.shape[0] < CANONICAL_VERTEX_COUNT:
        raise ValueError(f"mesh must have shape (V >= 68, 3), got {mesh.shape}")

    # Pupil centers: Left eye (36..41), Right eye (42..47)
    left_pupil = np.mean(mesh[36:42], axis=0)
    right_pupil = np.mean(mesh[42:48], axis=0)
    d_ip = float(np.linalg.norm(left_pupil - right_pupil))

    # Bizygomatic width: left cheek landmark 0 to right cheek landmark 16
    d_bz = float(np.linalg.norm(mesh[0] - mesh[16]))
    if d_bz < 1e-6:
        raise ValueError("Degenerate bizygomatic distance")

    # Nasion (27) to Menton (8)
    d_nm = float(np.linalg.norm(mesh[27] - mesh[8]))

    # Mid-sagittal line from nasion to menton
    sagittal_axis = mesh[8] - mesh[27]
    sag_norm = float(np.linalg.norm(sagittal_axis))
    if sag_norm < 1e-6:
        raise ValueError("Degenerate sagittal axis")
    sag_unit = sagittal_axis / sag_norm

    # Bilateral cheek distance to sagittal axis
    v_left = mesh[0] - mesh[27]
    proj_l = np.dot(v_left, sag_unit) * sag_unit
    perp_l = float(np.linalg.norm(v_left - proj_l))

    v_right = mesh[16] - mesh[27]
    proj_r = np.dot(v_right, sag_unit) * sag_unit
    perp_r = float(np.linalg.norm(v_right - proj_r))

    symmetry = min(perp_l, perp_r) / max(perp_l, perp_r, 1e-9)

    return CephalometricRatios(
        interpupillary_to_bizygomatic=d_ip / d_bz,
        nasion_menton_to_bizygomatic=d_nm / d_bz,
        cranial_vault_symmetry=symmetry,
    )


def lock_and_heal_cranio_facial_identity(
    observed_mesh: np.ndarray,
    locked_alpha_id: np.ndarray,
    basis: CranioFacialBasis,
    lambda_reg: float = 1e-4,
    beta_max: float = 3.0,
    rigid_anchor_indices: tuple[int, ...] = RIGID_CRANIAL_ANCHOR_INDICES,
) -> CranialLockResult:
    """Align observed mesh, quantify identity drift, and solve expression with locked identity.

    Steps:
      (a) Align observed mesh to canonical frame via rigid cranial anchors (Umeyama Sim(3)).
      (b) Estimate raw drifted identity alpha_hat and compute drift against locked_alpha_id.
      (c) Solve ridge-regularized expression coefficients with locked identity:
            beta_hat = argmin ||r - U_exp @ beta||^2 + lambda * ||beta||^2
      (d) Reconstruct healed mesh in world space, proving post-heal identity drift < 1e-12.
    """
    if observed_mesh.shape != (basis.vertex_count, 3):
        exp_shape = (basis.vertex_count, 3)
        raise ValueError(
            f"observed_mesh shape mismatch: expected {exp_shape}, got {observed_mesh.shape}"
        )
    if locked_alpha_id.shape != (basis.k_id,):
        raise ValueError(
            f"locked_alpha_id shape mismatch: expected ({basis.k_id},), got {locked_alpha_id.shape}"
        )
    if lambda_reg < 0.0 or beta_max <= 0.0:
        raise ValueError("Invalid solver hyperparameters: lambda_reg >= 0, beta_max > 0")

    # (a) Align observed mesh onto canonical mean mesh using rigid anchors
    anchor_idx = list(rigid_anchor_indices)
    source_anchors = observed_mesh[anchor_idx]
    target_anchors = basis.mean_mesh[anchor_idx]

    # Compute Sim(3) transform that maps observed -> canonical
    sim3 = umeyama_sim3_align(source_anchors, target_anchors, allow_scale=True)
    aligned_obs = sim3.transform(observed_mesh)

    # (b) Estimate raw drifted identity
    s_diff = (aligned_obs - basis.mean_mesh).reshape(-1)
    raw_alpha_id = basis.u_id.T @ s_diff
    raw_identity_drift = float(np.linalg.norm(raw_alpha_id - locked_alpha_id))

    cephs_observed = compute_cephalometric_ratios(aligned_obs)
    canonical_locked_mesh = synthesize_face_mesh(basis, locked_alpha_id, np.zeros(basis.k_exp))
    cephs_locked = compute_cephalometric_ratios(canonical_locked_mesh)
    ceph_drift = float(
        abs(
            cephs_observed.interpupillary_to_bizygomatic
            - cephs_locked.interpupillary_to_bizygomatic
        )
        + abs(
            cephs_observed.nasion_menton_to_bizygomatic - cephs_locked.nasion_menton_to_bizygomatic
        )
        + abs(cephs_observed.cranial_vault_symmetry - cephs_locked.cranial_vault_symmetry)
    )

    # (c) Solve ridge-regularized expression coefficients with locked identity
    residual = s_diff - basis.u_id @ locked_alpha_id
    # Since U_exp is orthonormal and orthogonal to U_id:
    # (U_exp.T @ U_exp + lambda * I)^(-1) @ U_exp.T @ residual
    beta_hat = (basis.u_exp.T @ residual) / (1.0 + lambda_reg)
    beta_clamped = np.clip(beta_hat, -beta_max, beta_max)

    # (d) Reconstruct healed mesh in canonical frame
    flat_healed = (
        basis.mean_mesh.reshape(-1) + basis.u_id @ locked_alpha_id + basis.u_exp @ beta_clamped
    )
    healed_canonical_mesh = flat_healed.reshape(basis.vertex_count, 3)

    # Prove post-heal identity drift in canonical space is machine-zero
    post_diff = (healed_canonical_mesh - basis.mean_mesh).reshape(-1)
    recovered_alpha_id = basis.u_id.T @ post_diff
    post_heal_drift = float(np.linalg.norm(recovered_alpha_id - locked_alpha_id))

    # Transform healed mesh back into observed world space
    healed_world_mesh = sim3.inverse_transform(healed_canonical_mesh)

    return CranialLockResult(
        aligned_observed_mesh=aligned_obs,
        raw_identity_drift=raw_identity_drift,
        solved_expression=beta_clamped,
        healed_canonical_mesh=healed_canonical_mesh,
        healed_world_mesh=healed_world_mesh,
        post_heal_identity_drift=post_heal_drift,
        cephalometric_drift=ceph_drift,
        alignment=sim3,
    )
