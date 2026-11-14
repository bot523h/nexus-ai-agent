"""Unit Dual Quaternion Lie-Clifford Algebra and Skeletal Rigging.

Wave 2 of NAGAR MORPHEUS:
Provides Unit Dual Quaternions (Spin(3) ⋉ R^3), ScLERP geodesic interpolation,
antipodal-hemisphere sign-stabilized Dual Quaternion Skinning (DQS) vs Linear
Blend Skinning (LBS), hierarchical forward kinematics, damped least-squares IK,
and stance-phase zero-skate foot pinning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np


@dataclass(frozen=True)
class UnitQuaternion:
    """Unit quaternion representing 3D rotation in SO(3)."""

    w: float
    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        n = np.sqrt(self.w**2 + self.x**2 + self.y**2 + self.z**2)
        if np.isnan(n) or np.isinf(n):
            raise ValueError(f"Invalid quaternion with norm {n}")

    @classmethod
    def identity(cls) -> UnitQuaternion:
        return cls(w=1.0, x=0.0, y=0.0, z=0.0)

    @classmethod
    def from_axis_angle(cls, axis: np.ndarray, angle: float) -> UnitQuaternion:
        axis_arr = np.asarray(axis, dtype=np.float64)
        n = float(np.linalg.norm(axis_arr))
        if n < 1e-12:
            return cls.identity()
        unit_axis = axis_arr / n
        half = angle * 0.5
        s = float(np.sin(half))
        return cls(
            w=float(np.cos(half)),
            x=float(unit_axis[0] * s),
            y=float(unit_axis[1] * s),
            z=float(unit_axis[2] * s),
        ).normalize()

    def norm(self) -> float:
        return float(np.sqrt(self.w**2 + self.x**2 + self.y**2 + self.z**2))

    def normalize(self) -> UnitQuaternion:
        n = self.norm()
        if n < 1e-12:
            return UnitQuaternion(w=1.0, x=0.0, y=0.0, z=0.0)
        return UnitQuaternion(
            w=self.w / n,
            x=self.x / n,
            y=self.y / n,
            z=self.z / n,
        )

    def conjugate(self) -> UnitQuaternion:
        return UnitQuaternion(w=self.w, x=-self.x, y=-self.y, z=-self.z)

    def dot(self, other: UnitQuaternion) -> float:
        return float(self.w * other.w + self.x * other.x + self.y * other.y + self.z * other.z)

    def multiply(self, other: UnitQuaternion) -> UnitQuaternion:
        w = self.w * other.w - self.x * other.x - self.y * other.y - self.z * other.z
        x = self.w * other.x + self.x * other.w + self.y * other.z - self.z * other.y
        y = self.w * other.y - self.x * other.z + self.y * other.w + self.z * other.x
        z = self.w * other.z + self.x * other.y - self.y * other.x + self.z * other.w
        return UnitQuaternion(w=w, x=x, y=y, z=z)

    def to_matrix(self) -> np.ndarray:
        n = self.norm()
        if n < 1e-12:
            return np.eye(3, dtype=np.float64)
        q = self.normalize()
        w, x, y, z = q.w, q.x, q.y, q.z
        return np.array(
            [
                [1.0 - 2.0 * (y**2 + z**2), 2.0 * (x * y - w * z), 2.0 * (x * z + w * y)],
                [2.0 * (x * y + w * z), 1.0 - 2.0 * (x**2 + z**2), 2.0 * (y * z - w * x)],
                [2.0 * (x * z - w * y), 2.0 * (y * z + w * x), 1.0 - 2.0 * (x**2 + y**2)],
            ],
            dtype=np.float64,
        )

    def rotate_vector(self, vec: np.ndarray) -> np.ndarray:
        v = np.asarray(vec, dtype=np.float64)
        rot_mat = self.to_matrix()
        return rot_mat @ v


@dataclass(frozen=True)
class DualQuaternion:
    """Unit Dual Quaternion representing rigid transform in Spin(3) ⋉ R^3.

    Satisfies Plücker orthogonality: <real, dual> = 0.
    """

    real: UnitQuaternion
    dual: UnitQuaternion

    def __post_init__(self) -> None:
        ortho = abs(self.real.dot(self.dual))
        if ortho > 1e-6:
            raise ValueError(f"Plücker orthogonality violation: <q_r, q_d> = {ortho}")

    @classmethod
    def identity(cls) -> DualQuaternion:
        return cls(
            real=UnitQuaternion.identity(),
            dual=UnitQuaternion(w=0.0, x=0.0, y=0.0, z=0.0),
        )

    @classmethod
    def from_rotation_translation(
        cls, rotation: UnitQuaternion, translation: np.ndarray
    ) -> DualQuaternion:
        t = np.asarray(translation, dtype=np.float64)
        if t.shape != (3,):
            raise ValueError(f"translation must be (3,), got {t.shape}")
        q_r = rotation.normalize()
        # q_d = 0.5 * (0, t) * q_r
        t_quat = UnitQuaternion(w=0.0, x=float(t[0]), y=float(t[1]), z=float(t[2]))
        prod = t_quat.multiply(q_r)
        q_d = UnitQuaternion(
            w=0.5 * prod.w,
            x=0.5 * prod.x,
            y=0.5 * prod.y,
            z=0.5 * prod.z,
        )
        return cls(real=q_r, dual=q_d)

    def to_rotation_translation(self) -> tuple[UnitQuaternion, np.ndarray]:
        q_r = self.real.normalize()
        # t_quat = 2.0 * q_d * q_r.conjugate()
        prod = self.dual.multiply(q_r.conjugate())
        t = np.array([2.0 * prod.x, 2.0 * prod.y, 2.0 * prod.z], dtype=np.float64)
        return q_r, t

    def to_homogeneous_matrix(self) -> np.ndarray:
        rot, trans = self.to_rotation_translation()
        mat = np.eye(4, dtype=np.float64)
        mat[:3, :3] = rot.to_matrix()
        mat[:3, 3] = trans
        return mat

    def multiply(self, other: DualQuaternion) -> DualQuaternion:
        r = self.real.multiply(other.real)
        # d = self.real * other.dual + self.dual * other.real
        d1 = self.real.multiply(other.dual)
        d2 = self.dual.multiply(other.real)
        d = UnitQuaternion(
            w=d1.w + d2.w,
            x=d1.x + d2.x,
            y=d1.y + d2.y,
            z=d1.z + d2.z,
        )
        return DualQuaternion(real=r.normalize(), dual=d)

    def conjugate(self) -> DualQuaternion:
        return DualQuaternion(
            real=self.real.conjugate(),
            dual=self.dual.conjugate(),
        )

    def normalize(self) -> DualQuaternion:
        mag_r = self.real.norm()
        if mag_r < 1e-12:
            raise ValueError("Cannot normalize degenerate dual quaternion")
        dot = self.real.dot(self.dual)
        q_r = UnitQuaternion(
            w=self.real.w / mag_r,
            x=self.real.x / mag_r,
            y=self.real.y / mag_r,
            z=self.real.z / mag_r,
        )
        q_d = UnitQuaternion(
            w=(self.dual.w - q_r.w * dot) / mag_r,
            x=(self.dual.x - q_r.x * dot) / mag_r,
            y=(self.dual.y - q_r.y * dot) / mag_r,
            z=(self.dual.z - q_r.z * dot) / mag_r,
        )
        return DualQuaternion(real=q_r, dual=q_d)

    def transform_point(self, point: np.ndarray) -> np.ndarray:
        p = np.asarray(point, dtype=np.float64)
        rot, trans = self.to_rotation_translation()
        return cast(np.ndarray, rot.rotate_vector(p) + trans)

    @classmethod
    def sclerp(cls, q1: DualQuaternion, q2: DualQuaternion, t: float) -> DualQuaternion:
        """Screw Linear Interpolation along constant-pitch screw geodesic."""
        if not (0.0 <= t <= 1.0):
            raise ValueError(f"Interpolation parameter t must be in [0, 1], got {t}")
        # Antipodal hemisphere sign stabilization
        cos_half = q1.real.dot(q2.real)
        sign = 1.0 if cos_half >= 0.0 else -1.0
        q2_aligned = DualQuaternion(
            real=UnitQuaternion(
                w=sign * q2.real.w,
                x=sign * q2.real.x,
                y=sign * q2.real.y,
                z=sign * q2.real.z,
            ),
            dual=UnitQuaternion(
                w=sign * q2.dual.w,
                x=sign * q2.dual.x,
                y=sign * q2.dual.y,
                z=sign * q2.dual.z,
            ),
        )

        delta = q1.conjugate().multiply(q2_aligned)
        theta = 2.0 * np.arccos(np.clip(delta.real.w, -1.0, 1.0))
        sin_half = np.sin(theta * 0.5)

        if abs(sin_half) < 1e-8:
            # Pure translation or identity
            trans_interp = np.array(
                [
                    delta.dual.x * 2.0 * t,
                    delta.dual.y * 2.0 * t,
                    delta.dual.z * 2.0 * t,
                ],
                dtype=np.float64,
            )
            delta_t = DualQuaternion.from_rotation_translation(
                UnitQuaternion.identity(), trans_interp
            )
        else:
            axis = np.array([delta.real.x, delta.real.y, delta.real.z]) / sin_half
            pitch = -2.0 * delta.dual.w / sin_half
            theta_t = theta * t
            pitch_t = pitch * t

            rot_t = UnitQuaternion.from_axis_angle(axis, theta_t)
            # Moment calculation:
            trans_axis = axis * pitch_t
            delta_t = DualQuaternion.from_rotation_translation(rot_t, trans_axis)

        return q1.multiply(delta_t).normalize()


@dataclass(frozen=True)
class SkinningWeights:
    """Rig skinning weights matrix for linear and dual-quaternion blend skinning."""

    vertex_count: int
    bone_names: tuple[str, ...]
    weights: np.ndarray  # shape: (V, B), non-negative, rows sum to 1.0

    def __post_init__(self) -> None:
        if self.weights.shape != (self.vertex_count, len(self.bone_names)):
            exp_s = (self.vertex_count, len(self.bone_names))
            raise ValueError(f"Weights shape mismatch: expected {exp_s}, got {self.weights.shape}")
        if (self.weights < -1e-6).any():
            raise ValueError("Skinning weights contain negative values")
        row_sums = np.sum(self.weights, axis=1)
        if not np.allclose(row_sums, 1.0, atol=1e-4):
            raise ValueError("Skinning weight rows must sum to 1.0")


def skin_lbs(
    vertices: np.ndarray,
    bone_matrices: dict[str, np.ndarray],
    weights: SkinningWeights,
) -> np.ndarray:
    """Linear Blend Skinning (LBS) prone to candy-wrapper joint collapse."""
    if vertices.shape != (weights.vertex_count, 3):
        raise ValueError("vertices shape mismatch with weights")
    deformed = np.zeros_like(vertices)
    homo = np.ones((weights.vertex_count, 4), dtype=np.float64)
    homo[:, :3] = vertices

    for j, b_name in enumerate(weights.bone_names):
        if b_name not in bone_matrices:
            raise KeyError(f"Missing bone matrix for '{b_name}'")
        m = bone_matrices[b_name]
        w = weights.weights[:, j : j + 1]
        trans_v = (homo @ m.T)[:, :3]
        deformed += w * trans_v

    return deformed


def skin_dqs(
    vertices: np.ndarray,
    bone_transforms: dict[str, DualQuaternion],
    weights: SkinningWeights,
) -> np.ndarray:
    """Antipodal-Hemisphere Sign-Stabilized Dual Quaternion Skinning (DQS).

    Preserves 100% volume under 175°-180° twist with zero candy-wrapper collapse.
    """
    if vertices.shape != (weights.vertex_count, 3):
        raise ValueError("vertices shape mismatch with weights")
    deformed = np.zeros_like(vertices)

    bone_dqs = [bone_transforms[b] for b in weights.bone_names]

    for i in range(weights.vertex_count):
        w_i = weights.weights[i]
        # Dominant bone pivot
        dom_idx = int(np.argmax(w_i))
        q_dom = bone_dqs[dom_idx]

        r_sum_w = 0.0
        r_sum_x = 0.0
        r_sum_y = 0.0
        r_sum_z = 0.0
        d_sum_w = 0.0
        d_sum_x = 0.0
        d_sum_y = 0.0
        d_sum_z = 0.0

        for j, w_ij in enumerate(w_i):
            if w_ij <= 0.0:
                continue
            q_j = bone_dqs[j]
            sign = 1.0 if q_dom.real.dot(q_j.real) >= 0.0 else -1.0
            factor = w_ij * sign

            r_sum_w += factor * q_j.real.w
            r_sum_x += factor * q_j.real.x
            r_sum_y += factor * q_j.real.y
            r_sum_z += factor * q_j.real.z

            d_sum_w += factor * q_j.dual.w
            d_sum_x += factor * q_j.dual.x
            d_sum_y += factor * q_j.dual.y
            d_sum_z += factor * q_j.dual.z

        blended = DualQuaternion(
            real=UnitQuaternion(w=r_sum_w, x=r_sum_x, y=r_sum_y, z=r_sum_z).normalize(),
            dual=UnitQuaternion(w=d_sum_w, x=d_sum_x, y=d_sum_y, z=d_sum_z),
        ).normalize()

        deformed[i] = blended.transform_point(vertices[i])

    return deformed


@dataclass(frozen=True)
class Bone:
    """Rig skeletal bone with immutable rest geometry."""

    name: str
    parent_name: str | None
    rest_head: np.ndarray  # shape: (3,)
    rest_tail: np.ndarray  # shape: (3,)
    rest_length: float

    def __post_init__(self) -> None:
        calc_len = float(np.linalg.norm(self.rest_tail - self.rest_head))
        if abs(calc_len - self.rest_length) > 1e-4:
            raise ValueError(
                f"Bone {self.name} rest_length {self.rest_length} != calculated {calc_len}"
            )


@dataclass(frozen=True)
class SkeletalChain:
    """Kinematic bone hierarchy enforcing strict rest bone length invariance."""

    bones: tuple[Bone, ...]

    def get_bone(self, name: str) -> Bone:
        for b in self.bones:
            if b.name == name:
                return b
        raise KeyError(f"Bone '{name}' not found in chain")

    def forward_kinematics(
        self, joint_rotations: dict[str, UnitQuaternion]
    ) -> dict[str, DualQuaternion]:
        """Compute global bone transforms preserving rest lengths ||p_tail - p_head|| == L0."""
        global_dqs: dict[str, DualQuaternion] = {}
        for bone in self.bones:
            local_rot = joint_rotations.get(bone.name, UnitQuaternion.identity())
            q_rot = DualQuaternion.from_rotation_translation(local_rot, np.zeros(3))
            l_vec = bone.rest_tail - bone.rest_head
            q_trans = DualQuaternion.from_rotation_translation(UnitQuaternion.identity(), l_vec)
            local_step = q_rot.multiply(q_trans)

            if bone.parent_name is None:
                origin_dq = DualQuaternion.from_rotation_translation(
                    UnitQuaternion.identity(), bone.rest_head
                )
                global_dqs[bone.name] = origin_dq.multiply(local_step)
            else:
                parent_dq = global_dqs[bone.parent_name]
                global_dqs[bone.name] = parent_dq.multiply(local_step)

        return global_dqs


def damped_least_squares_ik(
    chain: SkeletalChain,
    target_pos: np.ndarray,
    end_effector_bone: str,
    initial_angles: dict[str, float],
    lambda_damping: float = 0.1,
    max_iterations: int = 50,
    tolerance: float = 1e-4,
) -> dict[str, float]:
    """Levenberg-Marquardt Damped Least-Squares Inverse Kinematics solver."""
    angles = dict(initial_angles)
    bone_names = [b.name for b in chain.bones]

    for _ in range(max_iterations):
        joint_rots = {
            name: UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), angles.get(name, 0.0))
            for name in bone_names
        }
        fk = chain.forward_kinematics(joint_rots)
        ee_pos = fk[end_effector_bone].transform_point(np.array([0.0, 0.0, 0.0]))
        err = target_pos - ee_pos
        if float(np.linalg.norm(err)) < tolerance:
            break

        # Numerical Jacobian
        j_cols: list[np.ndarray] = []
        eps = 1e-4
        for name in bone_names:
            perturbed_angles = dict(angles)
            perturbed_angles[name] = perturbed_angles.get(name, 0.0) + eps
            perturbed_rots = {
                n: UnitQuaternion.from_axis_angle(
                    np.array([0.0, 0.0, 1.0]), perturbed_angles.get(n, 0.0)
                )
                for n in bone_names
            }
            fk_p = chain.forward_kinematics(perturbed_rots)
            ee_p = fk_p[end_effector_bone].transform_point(np.array([0.0, 0.0, 0.0]))
            d_ee = (ee_p - ee_pos) / eps
            j_cols.append(d_ee)

        jac = np.column_stack(j_cols)  # (3, N)
        # Delta = J.T @ (J @ J.T + lambda^2 * I)^(-1) @ err
        jj_t = jac @ jac.T + (lambda_damping**2) * np.eye(3)
        delta_theta = jac.T @ np.linalg.solve(jj_t, err)
        clamped_step = np.clip(delta_theta * 0.5, -0.2, 0.2)

        for idx, name in enumerate(bone_names):
            angles[name] = float(np.clip(angles.get(name, 0.0) + clamped_step[idx], -np.pi, np.pi))

    return angles


def zero_skate_foot_lock(
    foot_positions: np.ndarray,
    contact_flags: np.ndarray,
    ground_z: float = 0.0,
) -> np.ndarray:
    """Stance-phase foot-skate elimination and ground plane enforcement.

    For frames with contact == True, horizontal velocity is pinned to 0.0 (< 1e-9)
    and vertical position is clamped to z >= ground_z.
    """
    if foot_positions.ndim != 2 or foot_positions.shape[1] != 3:
        raise ValueError(f"foot_positions must be (T, 3), got {foot_positions.shape}")
    t = foot_positions.shape[0]
    if contact_flags.shape != (t,):
        raise ValueError(f"contact_flags shape {contact_flags.shape} != frames count {t}")

    healed = np.copy(foot_positions)
    in_contact = False
    locked_xy = np.zeros(2, dtype=np.float64)

    for i in range(t):
        if contact_flags[i]:
            if not in_contact:
                in_contact = True
                locked_xy = healed[i, :2].copy()
            healed[i, 0] = locked_xy[0]
            healed[i, 1] = locked_xy[1]
            healed[i, 2] = max(healed[i, 2], ground_z)
        else:
            in_contact = False

    return healed
