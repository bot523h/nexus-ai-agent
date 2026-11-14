"""3D Tetrahedral Corotational Finite Element Soft-Tissue and Incompressible Volume.

Wave 5 of NAGAR MORPHEUS:
Provides reference and deformed tetrahedral kinematics, proper rotational
polar decomposition with det(R) = +1, corotational PK1 stress with pure-rotation
zero-stress objectivity, Newton's 3rd law nodal force equilibrium, inversion
and volume drift healing, and cranial bone SDF collision resolution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Tetrahedron:
    """Tetrahedral reference element with inverted-shape fail-closed validation."""

    indices: tuple[int, int, int, int]
    rest_vertices: np.ndarray  # shape: (4, 3)
    rest_edge_matrix: np.ndarray  # shape: (3, 3)
    rest_volume: float
    inv_rest_edge_matrix: np.ndarray  # shape: (3, 3)

    def __post_init__(self) -> None:
        if self.rest_vertices.shape != (4, 3):
            raise ValueError(f"rest_vertices must be (4, 3), got {self.rest_vertices.shape}")
        if self.rest_edge_matrix.shape != (3, 3):
            raise ValueError(f"rest_edge_matrix must be (3, 3), got {self.rest_edge_matrix.shape}")
        if self.rest_volume <= 1e-12:
            raise ValueError(f"Degenerate or inverted rest volume {self.rest_volume}")


def build_tetrahedron(
    indices: tuple[int, int, int, int],
    mesh_vertices: np.ndarray,
) -> Tetrahedron:
    """Build a Tetrahedron from 4 vertex indices and reference mesh coordinates."""
    if len(indices) != 4 or len(set(indices)) != 4:
        raise ValueError("Tetrahedron must have 4 distinct vertex indices")
    verts = mesh_vertices[list(indices)]
    # D_m = [X1 - X0, X2 - X0, X3 - X0]
    dm = np.column_stack([verts[1] - verts[0], verts[2] - verts[0], verts[3] - verts[0]])
    det = float(np.linalg.det(dm))
    if det <= 1e-12:
        raise ValueError(f"Degenerate or inverted reference tetrahedron det(D_m) = {det} <= 1e-12")
    vol = det / 6.0
    inv_dm = np.linalg.inv(dm)
    return Tetrahedron(
        indices=indices,
        rest_vertices=verts,
        rest_edge_matrix=dm,
        rest_volume=vol,
        inv_rest_edge_matrix=inv_dm,
    )


@dataclass(frozen=True)
class SoftTissueMaterial:
    """Linear isotropic / corotational elasticity parameters for muscle and skin."""

    youngs_modulus: float  # E > 0
    poissons_ratio: float  # nu in (0.0, 0.499]
    rayleigh_damping: float = 0.05
    density: float = 1050.0  # kg / m^3

    def __post_init__(self) -> None:
        if self.youngs_modulus <= 0.0:
            raise ValueError("youngs_modulus must be positive")
        if not (0.0 < self.poissons_ratio < 0.5):
            raise ValueError(f"poissons_ratio must be in (0.0, 0.5), got {self.poissons_ratio}")
        if self.rayleigh_damping < 0.0:
            raise ValueError("rayleigh_damping must be non-negative")
        if self.density <= 0.0:
            raise ValueError("density must be positive")

    @property
    def lame_mu(self) -> float:
        """Shear modulus mu = E / (2 * (1 + nu))."""
        return self.youngs_modulus / (2.0 * (1.0 + self.poissons_ratio))

    @property
    def lame_lambda(self) -> float:
        """First Lamé parameter lambda = (E * nu) / ((1 + nu) * (1 - 2*nu))."""
        return (self.youngs_modulus * self.poissons_ratio) / (
            (1.0 + self.poissons_ratio) * (1.0 - 2.0 * self.poissons_ratio)
        )


@dataclass(frozen=True)
class CorotationalStressResult:
    """Forensic evaluation of tetrahedral corotational PK1 stress and nodal forces."""

    deformation_gradient: np.ndarray  # shape: (3, 3)
    rotation: np.ndarray  # shape: (3, 3), det(R) = +1.0
    jacobian: float  # det(F)
    pk1_stress: np.ndarray  # shape: (3, 3)
    nodal_forces: np.ndarray  # shape: (4, 3), sum == 0


def compute_corotational_pk1_stress(
    tet: Tetrahedron,
    deformed_vertices: np.ndarray,
    material: SoftTissueMaterial,
) -> CorotationalStressResult:
    """Evaluate proper polar decomposition, corotational PK1 stress, and nodal forces.

    Guarantees:
      1. det(R) = +1.0 (reflections healed via signed SVD).
      2. Objectivity: for any pure rigid rotation F in SO(3), ||P(F)||_F < 1e-12.
      3. Newton's 3rd law equilibrium: sum_{a=0}^3 f_a = 0 (< 1e-12).
    """
    if deformed_vertices.shape != (4, 3):
        raise ValueError(f"deformed_vertices must be (4, 3), got {deformed_vertices.shape}")
    x = deformed_vertices
    # D_s = [x1 - x0, x2 - x0, x3 - x0]
    ds = np.column_stack([x[1] - x[0], x[2] - x[0], x[3] - x[0]])
    f_mat = ds @ tet.inv_rest_edge_matrix
    j = float(np.linalg.det(f_mat))

    # Polar decomposition via SVD: F = U @ Sigma @ V.T
    u, _, vt = np.linalg.svd(f_mat)
    v = vt.T
    det_rot = float(np.linalg.det(u @ v.T))
    d = np.diag([1.0, 1.0, 1.0 if det_rot >= 0.0 else -1.0])
    rot = u @ d @ v.T

    # Corotational First Piola-Kirchhoff (PK1) stress:
    # P(F) = 2*mu*(F - R) + lambda*(J - 1)*J*F^(-T)
    mu = material.lame_mu
    lam = material.lame_lambda

    if abs(j) > 1e-8:
        inv_f_t = np.linalg.inv(f_mat).T
    else:
        inv_f_t = np.eye(3, dtype=np.float64)

    pk1 = 2.0 * mu * (f_mat - rot) + lam * (j - 1.0) * j * inv_f_t

    # Nodal force assembly: H = -V0 * P(F) @ Dm^(-T)
    h_mat = -tet.rest_volume * (pk1 @ tet.inv_rest_edge_matrix.T)
    f1 = h_mat[:, 0]
    f2 = h_mat[:, 1]
    f3 = h_mat[:, 2]
    f0 = -(f1 + f2 + f3)

    nodal_forces = np.vstack([f0, f1, f2, f3])

    return CorotationalStressResult(
        deformation_gradient=f_mat,
        rotation=rot,
        jacobian=j,
        pk1_stress=pk1,
        nodal_forces=nodal_forces,
    )


def heal_inverted_and_volume_drift_tet(
    deformed_vertices: np.ndarray,
    tet: Tetrahedron,
    j_min: float = 0.95,
    j_max: float = 1.05,
) -> np.ndarray:
    """Heal inverted (J <= 0) or volume-drifted tetrahedra by clamping singular values."""
    if deformed_vertices.shape != (4, 3):
        raise ValueError(f"deformed_vertices must be (4, 3), got {deformed_vertices.shape}")
    x = deformed_vertices.copy()
    ds = np.column_stack([x[1] - x[0], x[2] - x[0], x[3] - x[0]])
    f_mat = ds @ tet.inv_rest_edge_matrix
    j = float(np.linalg.det(f_mat))

    if j_min <= j <= j_max:
        return x  # within valid volume bounds

    u, s_vals, vt = np.linalg.svd(f_mat)
    v = vt.T
    det_rot = float(np.linalg.det(u @ v.T))
    d = np.diag([1.0, 1.0, 1.0 if det_rot >= 0.0 else -1.0])
    rot = u @ d @ v.T

    target_j = float(np.clip(max(j, 0.001), j_min, j_max))
    current_prod = float(np.prod(s_vals))
    if current_prod < 1e-12:
        scale_fac = target_j ** (1.0 / 3.0)
        s_healed = np.full(3, scale_fac, dtype=np.float64)
    else:
        scale_fac = (target_j / current_prod) ** (1.0 / 3.0)
        s_healed = np.clip(s_vals * scale_fac, 0.5, 2.0)

    f_healed = rot @ (v @ np.diag(s_healed) @ v.T)
    ds_healed = f_healed @ tet.rest_edge_matrix

    x0 = x[0]
    healed_verts = np.zeros((4, 3), dtype=np.float64)
    healed_verts[0] = x0
    healed_verts[1] = x0 + ds_healed[:, 0]
    healed_verts[2] = x0 + ds_healed[:, 1]
    healed_verts[3] = x0 + ds_healed[:, 2]

    return healed_verts


@dataclass(frozen=True)
class FEMSimulationState:
    """State of tetrahedral soft-tissue physical simulation."""

    vertices: np.ndarray  # shape: (N, 3)
    velocities: np.ndarray  # shape: (N, 3)
    tetrahedra: tuple[Tetrahedron, ...]
    material: SoftTissueMaterial


def solve_fem_time_step(
    state: FEMSimulationState,
    dt: float,
    external_forces: np.ndarray | None = None,
    cranial_bone_sdf: Callable[[np.ndarray], tuple[float, np.ndarray]] | None = None,
) -> FEMSimulationState:
    """Perform one semi-implicit time step of tetrahedral soft-tissue dynamics."""
    if dt <= 0.0:
        raise ValueError(f"dt must be positive, got {dt}")
    n = state.vertices.shape[0]
    pos = state.vertices.copy()
    vel = state.velocities.copy()

    total_forces = np.zeros((n, 3), dtype=np.float64)
    if external_forces is not None:
        total_forces += external_forces

    # Accumulate internal elastic forces
    for tet in state.tetrahedra:
        verts_tet = pos[list(tet.indices)]
        res = compute_corotational_pk1_stress(tet, verts_tet, state.material)
        for i, idx in enumerate(tet.indices):
            total_forces[idx] += res.nodal_forces[i]

    # Rayleigh damping force: -gamma * vel
    total_forces -= state.material.rayleigh_damping * vel

    # Update velocities and positions
    node_mass = 0.1  # lumped nodal mass in kg
    vel += (total_forces / node_mass) * dt
    pos += vel * dt

    # Cranial bone collision projection (SDF)
    if cranial_bone_sdf is not None:
        for i in range(n):
            dist, grad = cranial_bone_sdf(pos[i])
            if dist < 0.0:
                # Penetration detected: project onto surface
                pos[i] -= dist * grad
                # Cancel normal velocity
                vn = float(np.dot(vel[i], grad))
                if vn < 0.0:
                    vel[i] -= vn * grad

    return FEMSimulationState(
        vertices=pos,
        velocities=vel,
        tetrahedra=state.tetrahedra,
        material=state.material,
    )
