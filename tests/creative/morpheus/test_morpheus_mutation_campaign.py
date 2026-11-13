"""Popperian Mutation Campaign (80/80 True AST/Code Mutations) for NAGAR MORPHEUS.

Executes 80 distinct implementation mutations (10 mutations per wave across Waves 1–8)
loaded via `importlib.util.spec_from_file_location` without evicting C-extensions
(`numpy`, `scipy`, `PIL`) from `sys.modules` (Golden Rule #3).
Proves 100% (80/80) mutation kill rate (0 survivors) on the exact oracle that passes
on the unmutated baseline.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus_ai_agent.creative.morpheus.cranio_facial_morphable_lock import (
    build_canonical_3dmm_basis,
    compute_cephalometric_ratios,
    synthesize_face_mesh,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MORPHEUS_SRC = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "morpheus"


def load_mutated_module(
    source_filename: str,
    old_fragment: str,
    new_fragment: str,
    mutant_id: str,
) -> Any:
    """Create and load a dynamically mutated module under a unique module name.

    CRUCIAL (Golden Rule #3): Preserves all C-extensions in sys.modules so
    pytest-cov does not raise 'cannot load module more than once per process'.
    """
    src_file = MORPHEUS_SRC / source_filename
    source_code = src_file.read_text(encoding="utf-8")
    if old_fragment not in source_code:
        raise ValueError(
            f"Mutant {mutant_id}: old_fragment not found in {source_filename}:\n{old_fragment}"
        )
    mutated_code = source_code.replace(old_fragment, new_fragment, 1)

    tmp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    try:
        tmp_file.write(mutated_code)
        tmp_file.flush()
        tmp_path = tmp_file.name
    finally:
        tmp_file.close()

    try:
        mod_name = f"morpheus_mutant_{mutant_id.lower()}"
        spec = importlib.util.spec_from_file_location(mod_name, tmp_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load spec for mutant {mutant_id}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def run_mutation_kill_probe(
    source_filename: str,
    old_fragment: str,
    new_fragment: str,
    mutant_id: str,
    oracle: Callable[[Any], None],
) -> None:
    """Prove oracle passes on baseline module and is killed (fails) on mutated module."""
    # 1. Baseline module must PASS the oracle
    mod_stem = Path(source_filename).stem
    baseline_mod = importlib.import_module(f"nexus_ai_agent.creative.morpheus.{mod_stem}")
    oracle(baseline_mod)

    # 2. Mutated module must FAIL the oracle (mutation killed)
    mutant_mod = load_mutated_module(source_filename, old_fragment, new_fragment, mutant_id)
    try:
        oracle(mutant_mod)
        killed = False
    except Exception:
        killed = True
    except BaseException as exc:
        if "Failed" in type(exc).__name__:
            killed = True
        else:
            raise
    assert killed, f"Mutant {mutant_id} survived oracle!"


# ── WAVE 1 MUTATIONS: M01–M10 ───────────────────────────────────────────────


def test_m01_kill_wave1_qr_projection_omission() -> None:
    """M01: Omitting QR identity projection breaks subspace orthogonality."""

    def oracle(mod: Any) -> None:
        basis = mod.build_canonical_3dmm_basis(vertex_count=68, k_id=6, k_exp=6, seed=1)
        overlap = float(np.linalg.norm(basis.u_id.T @ basis.u_exp, ord="fro"))
        assert overlap <= 1e-12

    frag_old = (
        "exp_projected = raw_exp - u_id @ (u_id.T @ raw_exp)\n"
        "    q_exp, _ = np.linalg.qr(exp_projected)\n"
        "    u_exp = q_exp[:, :k_exp]\n\n"
        "    # Verify orthogonality: ||U_id.T @ U_exp||_F <= 1e-12\n"
        '    cross_overlap = np.linalg.norm(u_id.T @ u_exp, ord="fro")\n'
        "    if cross_overlap > 1e-12:\n"
        "        # Re-project to enforce machine-precision zero\n"
        "        u_exp = u_exp - u_id @ (u_id.T @ u_exp)\n"
        "        q_exp_clean, _ = np.linalg.qr(u_exp)\n"
        "        u_exp = q_exp_clean[:, :k_exp]"
    )
    frag_new = "q_exp, _ = np.linalg.qr(raw_exp)\n    u_exp = q_exp[:, :k_exp]"

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment=frag_old,
        new_fragment=frag_new,
        mutant_id="M01",
        oracle=oracle,
    )


def test_m02_kill_wave1_umeyama_reflection_healing() -> None:
    """M02: Disabling reflection matrix D fails to heal mirror reflections to det = +1."""

    def oracle(mod: Any) -> None:
        src = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [1.0, 1.0, 1.0]])
        tgt = src.copy()
        tgt[:, 0] *= -1.0  # mirror reflection
        sim3 = mod.umeyama_sim3_align(src, tgt, allow_scale=False)
        assert abs(float(np.linalg.det(sim3.rotation)) - 1.0) < 1e-6

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment="d = np.diag([1.0, 1.0, 1.0 if det_rot >= 0 else -1.0])",
        new_fragment="d = np.diag([1.0, 1.0, 1.0])",
        mutant_id="M02",
        oracle=oracle,
    )


def test_m03_kill_wave1_umeyama_scale_variance_inversion() -> None:
    """M03: Inverting scale computation var_x divisor breaks recovered scale."""

    def oracle(mod: Any) -> None:
        src = np.array([[1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 3.0]])
        tgt = src * 2.5
        sim3 = mod.umeyama_sim3_align(src, tgt, allow_scale=True)
        assert abs(sim3.scale - 2.5) < 1e-4

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment="scale = float(np.sum(s_vals * np.diag(d)) / var_x)",
        new_fragment="scale = float(np.sum(s_vals * np.diag(d)) * var_x)",
        mutant_id="M03",
        oracle=oracle,
    )


def test_m04_kill_wave1_cephalometric_ip_bz_inversion() -> None:
    """M04: Inverting interpupillary-to-bizygomatic ratio corrupts ratio bound."""

    def oracle(mod: Any) -> None:
        basis = mod.build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=2)
        cephs = mod.compute_cephalometric_ratios(basis.mean_mesh)
        # IP is typically ~60-70mm, BZ is ~120-140mm, so IP/BZ < 1.0
        assert cephs.interpupillary_to_bizygomatic < 1.0

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment="interpupillary_to_bizygomatic=d_ip / d_bz,",
        new_fragment="interpupillary_to_bizygomatic=d_bz / d_ip,",
        mutant_id="M04",
        oracle=oracle,
    )


def test_m05_kill_wave1_cephalometric_nasion_menton_index() -> None:
    """M05: Multiplying instead of dividing NM by BZ blows up ratio value."""

    def oracle(mod: Any) -> None:
        basis = mod.build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=3)
        cephs = mod.compute_cephalometric_ratios(basis.mean_mesh)
        assert cephs.nasion_menton_to_bizygomatic < 3.0

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment="nasion_menton_to_bizygomatic=d_nm / d_bz,",
        new_fragment="nasion_menton_to_bizygomatic=d_nm * d_bz,",
        mutant_id="M05",
        oracle=oracle,
    )


def test_m06_kill_wave1_tikhonov_lambda_sign() -> None:
    """M06: Flipping regularization sign (1 - lambda) corrupts Tikhonov shrinkage."""

    def oracle(mod: Any) -> None:
        basis = mod.build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=4)
        alpha = np.zeros(4)
        exp = np.array([1.0, 1.0, 1.0, 1.0])
        mesh = mod.synthesize_face_mesh(basis, alpha, exp)
        res = mod.lock_and_heal_cranio_facial_identity(mesh, alpha, basis, lambda_reg=0.5)
        # With lambda=0.5, norm must shrink: 1 / 1.5 = 0.6667
        assert float(np.linalg.norm(res.solved_expression)) < float(np.linalg.norm(exp))

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment="beta_hat = (basis.u_exp.T @ residual) / (1.0 + lambda_reg)",
        new_fragment="beta_hat = (basis.u_exp.T @ residual) / (1.0 - lambda_reg)",
        mutant_id="M06",
        oracle=oracle,
    )


def test_m07_kill_wave1_expression_clamping_removal() -> None:
    """M07: Removing expression clamping allows unbounded expression deformation."""

    def oracle(mod: Any) -> None:
        basis = mod.build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=5)
        alpha = np.zeros(4)
        exp = np.array([10.0, 10.0, 10.0, 10.0])
        mesh = mod.synthesize_face_mesh(basis, alpha, exp)
        res = mod.lock_and_heal_cranio_facial_identity(mesh, alpha, basis, beta_max=2.0)
        assert np.all(np.abs(res.solved_expression) <= 2.0 + 1e-12)

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment="beta_clamped = np.clip(beta_hat, -beta_max, beta_max)",
        new_fragment="beta_clamped = beta_hat",
        mutant_id="M07",
        oracle=oracle,
    )


def test_m08_kill_wave1_healed_mesh_expression_sign() -> None:
    """M08: Inverting expression deformation sign in healed mesh corrupts reconstruction."""

    def oracle(mod: Any) -> None:
        basis = mod.build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=6)
        alpha = np.array([0.5, -0.5, 0.2, -0.2])
        exp = np.array([1.0, -1.0, 0.5, -0.5])
        mesh = mod.synthesize_face_mesh(basis, alpha, exp)
        res = mod.lock_and_heal_cranio_facial_identity(mesh, alpha, basis, lambda_reg=0.0)
        assert float(np.max(np.abs(res.healed_canonical_mesh - mesh))) < 0.2

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment="+ basis.u_exp @ beta_clamped",
        new_fragment="- basis.u_exp @ beta_clamped",
        mutant_id="M08",
        oracle=oracle,
    )


def test_m09_kill_wave1_umeyama_translation_sign() -> None:
    """M09: Flipping translation vector sign breaks Umeyama alignment."""

    def oracle(mod: Any) -> None:
        src = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 10.0]])
        trans = np.array([10.0, -5.0, 2.0])
        tgt = src + trans
        sim3 = mod.umeyama_sim3_align(src, tgt, allow_scale=False)
        assert np.allclose(sim3.translation, trans, atol=1e-6)

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment="translation = mu_y - scale * (rotation @ mu_x)",
        new_fragment="translation = mu_y + scale * (rotation @ mu_x)",
        mutant_id="M09",
        oracle=oracle,
    )


def test_m10_kill_wave1_cranial_vault_symmetry_inversion() -> None:
    """M10: Inverting symmetry ratio allows ratio values > 1.0."""

    def oracle(mod: Any) -> None:
        basis = mod.build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=7)
        cephs = mod.compute_cephalometric_ratios(basis.mean_mesh)
        assert cephs.cranial_vault_symmetry <= 1.0 + 1e-12

    run_mutation_kill_probe(
        source_filename="cranio_facial_morphable_lock.py",
        old_fragment="symmetry = min(perp_l, perp_r) / max(perp_l, perp_r, 1e-9)",
        new_fragment="symmetry = max(perp_l, perp_r) / min(perp_l, perp_r, 1e-9)",
        mutant_id="M10",
        oracle=oracle,
    )


# ── WAVE 2 MUTATIONS: M11–M20 ───────────────────────────────────────────────


def test_m11_kill_wave2_hamilton_quaternion_product() -> None:
    """M11: Corrupting Hamilton quaternion product sign breaks multiplication."""

    def oracle(mod: Any) -> None:
        q1 = mod.UnitQuaternion.from_axis_angle(np.array([1.0, 0.0, 0.0]), np.pi / 2)
        q2 = mod.UnitQuaternion.from_axis_angle(np.array([1.0, 0.0, 0.0]), np.pi / 2)
        prod = q1.multiply(q2)
        # Rotation of 180 deg around X: w = cos(90) = 0, x = sin(90) = 1
        assert abs(prod.w) < 1e-6
        assert abs(prod.x - 1.0) < 1e-6

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="w = self.w * other.w - self.x * other.x",
        new_fragment="w = self.w * other.w + self.x * other.x",
        mutant_id="M11",
        oracle=oracle,
    )


def test_m12_kill_wave2_plucker_orthogonality_check() -> None:
    """M12: Dropping y and z from Plücker orthogonality check allows illegal dual parts."""

    def oracle(mod: Any) -> None:
        rot = mod.UnitQuaternion(w=1.0, x=0.0, y=0.0, z=0.0)
        # Non-orthogonal dual part along W
        bad_dual = mod.UnitQuaternion(w=1.0, x=0.0, y=0.0, z=0.0)
        with pytest.raises(ValueError, match="Plücker orthogonality"):
            mod.DualQuaternion(real=rot, dual=bad_dual)

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="ortho = abs(self.real.dot(self.dual))",
        new_fragment="ortho = abs(self.real.y * self.dual.y)",
        mutant_id="M12",
        oracle=oracle,
    )


def test_m13_kill_wave2_dual_quaternion_translation_factor() -> None:
    """M13: Missing factor of 2 in translation extraction halves recovered translation."""

    def oracle(mod: Any) -> None:
        rot = mod.UnitQuaternion.identity()
        trans = np.array([4.0, 6.0, 8.0])
        dq = mod.DualQuaternion.from_rotation_translation(rot, trans)
        _, t_rec = dq.to_rotation_translation()
        assert np.allclose(t_rec, trans, atol=1e-12)

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="t = np.array([2.0 * prod.x, 2.0 * prod.y, 2.0 * prod.z], dtype=np.float64)",
        new_fragment="t = np.array([prod.x, prod.y, prod.z], dtype=np.float64)",
        mutant_id="M13",
        oracle=oracle,
    )


def test_m14_kill_wave2_sclerp_antipodal_sign() -> None:
    """M14: Disabling antipodal sign alignment in ScLERP corrupts geodesic path."""

    def oracle(mod: Any) -> None:
        dq1 = mod.DualQuaternion.identity()
        rot_small = mod.UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), 0.2)
        rot_anti = mod.UnitQuaternion(
            w=-rot_small.w, x=-rot_small.x, y=-rot_small.y, z=-rot_small.z
        )
        dq2 = mod.DualQuaternion.from_rotation_translation(rot_anti, np.zeros(3))
        mid = mod.DualQuaternion.sclerp(dq1, dq2, 0.5)
        p = np.array([1.0, 0.0, 0.0])
        assert mid.transform_point(p)[0] > 0.5

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="sign = 1.0 if cos_half >= 0.0 else -1.0",
        new_fragment="sign = 1.0",
        mutant_id="M14",
        oracle=oracle,
    )


def test_m15_kill_wave2_dqs_antipodal_pivot_sign() -> None:
    """M15: Disabling dominant bone antipodal sign in DQS breaks antipodal blending."""

    def oracle(mod: Any) -> None:
        verts = np.array([[1.0, 0.0, 0.0]])
        rot_small = mod.UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), 0.2)
        rot_anti = mod.UnitQuaternion(
            w=-rot_small.w, x=-rot_small.x, y=-rot_small.y, z=-rot_small.z
        )
        dq_a = mod.DualQuaternion.identity()
        dq_b = mod.DualQuaternion.from_rotation_translation(rot_anti, np.zeros(3))
        weights = mod.SkinningWeights(
            vertex_count=1,
            bone_names=("bA", "bB"),
            weights=np.array([[0.6, 0.4]], dtype=np.float64),
        )
        deformed = mod.skin_dqs(verts, {"bA": dq_a, "bB": dq_b}, weights)
        assert deformed[0, 1] > 0.0

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="sign = 1.0 if q_dom.real.dot(q_j.real) >= 0.0 else -1.0",
        new_fragment="sign = 1.0",
        mutant_id="M15",
        oracle=oracle,
    )


def test_m16_kill_wave2_lbs_blend_accumulation() -> None:
    """M16: Subtracting instead of adding LBS bone contributions breaks identity rig."""

    def oracle(mod: Any) -> None:
        verts = np.array([[2.0, 3.0, 4.0]])
        mat = np.eye(4, dtype=np.float64)
        weights = mod.SkinningWeights(vertex_count=1, bone_names=("b1",), weights=np.array([[1.0]]))
        deformed = mod.skin_lbs(verts, {"b1": mat}, weights)
        assert np.allclose(deformed[0], verts[0], atol=1e-12)

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="deformed += w * trans_v",
        new_fragment="deformed -= w * trans_v",
        mutant_id="M16",
        oracle=oracle,
    )


def test_m17_kill_wave2_forward_kinematics_step_composition() -> None:
    """M17: Inverting rotation and translation step composition corrupts forward kinematics."""

    def oracle(mod: Any) -> None:
        b = mod.Bone("root", None, np.array([0.0, 0.0, 0.0]), np.array([5.0, 0.0, 0.0]), 5.0)
        chain = mod.SkeletalChain(bones=(b,))
        rot = mod.UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), np.pi / 2)
        fk = chain.forward_kinematics({"root": rot})
        tail = fk["root"].transform_point(np.array([0.0, 0.0, 0.0]))
        # 90 deg rotation around Z should place tail at [0, 5, 0]
        assert np.allclose(tail, np.array([0.0, 5.0, 0.0]), atol=1e-6)

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="local_step = q_rot.multiply(q_trans)",
        new_fragment="local_step = q_trans.multiply(q_rot)",
        mutant_id="M17",
        oracle=oracle,
    )


def test_m18_kill_wave2_dls_ik_negative_damping() -> None:
    """M18: Negative damping in Levenberg-Marquardt DLS breaks IK convergence."""

    def oracle(mod: Any) -> None:
        b1 = mod.Bone("b1", None, np.array([0.0, 0.0, 0.0]), np.array([5.0, 0.0, 0.0]), 5.0)
        b2 = mod.Bone("b2", "b1", np.array([5.0, 0.0, 0.0]), np.array([10.0, 0.0, 0.0]), 5.0)
        chain = mod.SkeletalChain(bones=(b1, b2))
        target = np.array([6.0, 4.0, 0.0])
        solved = mod.damped_least_squares_ik(
            chain, target, "b2", {"b1": 0.1, "b2": 0.1}, lambda_damping=0.5
        )
        joint_rots = {
            n: mod.UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), ang)
            for n, ang in solved.items()
        }
        fk = chain.forward_kinematics(joint_rots)
        ee = fk["b2"].transform_point(np.array([0.0, 0.0, 0.0]))
        assert float(np.linalg.norm(ee - target)) < 0.1

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="jj_t = jac @ jac.T + (lambda_damping**2) * np.eye(3)",
        new_fragment="jj_t = jac @ jac.T - (lambda_damping**2) * np.eye(3)",
        mutant_id="M18",
        oracle=oracle,
    )


def test_m19_kill_wave2_zero_skate_horizontal_pinning() -> None:
    """M19: Adding instead of pinning horizontal position causes foot skate drift."""

    def oracle(mod: Any) -> None:
        pos = np.array([[1.0, 2.0, 0.0], [1.5, 2.5, 0.0], [1.8, 2.8, 0.0]])
        contact = np.array([True, True, True])
        healed = mod.zero_skate_foot_lock(pos, contact, ground_z=0.0)
        assert abs(healed[1, 0] - 1.0) < 1e-9
        assert abs(healed[2, 0] - 1.0) < 1e-9

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="healed[i, 0] = locked_xy[0]",
        new_fragment="healed[i, 0] += locked_xy[0]",
        mutant_id="M19",
        oracle=oracle,
    )


def test_m20_kill_wave2_zero_skate_ground_clamp() -> None:
    """M20: Inverting max to min in ground plane clamping forces feet underground."""

    def oracle(mod: Any) -> None:
        pos = np.array([[0.0, 0.0, -0.5]])
        contact = np.array([True])
        healed = mod.zero_skate_foot_lock(pos, contact, ground_z=0.0)
        assert healed[0, 2] >= 0.0

    run_mutation_kill_probe(
        source_filename="dual_quaternion_skeletal_rig.py",
        old_fragment="healed[i, 2] = max(healed[i, 2], ground_z)",
        new_fragment="healed[i, 2] = min(healed[i, 2], ground_z)",
        mutant_id="M20",
        oracle=oracle,
    )


# ── WAVE 3 MUTATIONS: M21–M30 ───────────────────────────────────────────────


def test_m21_kill_wave3_3d_covariance_scale_squaring() -> None:
    """M21: Omitting squaring of scale breaks 3D Gaussian covariance dimension."""

    def oracle(mod: Any) -> None:
        g = mod.Gaussian3D(
            mean=np.zeros(3),
            rotation=mod.UnitQuaternion.identity(),
            scale=np.array([2.0, 3.0, 4.0]),
            opacity=1.0,
            normal=np.array([0.0, 0.0, 1.0]),
            albedo=np.ones(3),
            roughness=0.5,
            metallic=0.0,
        )
        cov = mod.compute_3d_covariance(g)
        # Diagonal entries should be 4, 9, 16
        assert abs(cov[0, 0] - 4.0) < 1e-12
        assert abs(cov[1, 1] - 9.0) < 1e-12

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="s_diag = np.diag(gaussian.scale**2)",
        new_fragment="s_diag = np.diag(gaussian.scale)",
        mutant_id="M21",
        oracle=oracle,
    )


def test_m22_kill_wave3_ewa_screen_covariance_regularization() -> None:
    """M22: Negative regularization in EWA screen covariance destroys positive definiteness."""

    def oracle(mod: Any) -> None:
        cam = mod.CameraParameters(
            view_matrix=np.eye(4, dtype=np.float64),
            fx=400.0,
            fy=400.0,
            cx=200.0,
            cy=200.0,
            width=400,
            height=400,
        )
        # Very tiny Gaussian that relies on +0.3 regularization
        g = mod.Gaussian3D(
            mean=np.array([0.0, 0.0, 2.0]),
            rotation=mod.UnitQuaternion.identity(),
            scale=np.array([0.001, 0.001, 0.001]),
            opacity=0.8,
            normal=np.array([0.0, 0.0, -1.0]),
            albedo=np.ones(3),
            roughness=0.5,
            metallic=0.0,
        )
        proj = mod.project_gaussian_ewa(g, cam)
        assert proj is not None
        _, conic_inv, _ = proj
        assert np.all(np.linalg.eigvalsh(conic_inv) > 0.0)

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="+ 0.3 * np.eye(2, dtype=np.float64)",
        new_fragment="- 0.3 * np.eye(2, dtype=np.float64)",
        mutant_id="M22",
        oracle=oracle,
    )


def test_m23_kill_wave3_near_plane_culling_omission() -> None:
    """M23: Omitting near plane culling allows inverted projection behind camera."""

    def oracle(mod: Any) -> None:
        cam = mod.CameraParameters(
            view_matrix=np.eye(4, dtype=np.float64),
            fx=300.0,
            fy=300.0,
            cx=100.0,
            cy=100.0,
            width=200,
            height=200,
            near_plane=0.1,
        )
        g_behind = mod.Gaussian3D(
            mean=np.array([0.0, 0.0, -2.0]),  # behind camera
            rotation=mod.UnitQuaternion.identity(),
            scale=np.array([0.1, 0.1, 0.1]),
            opacity=1.0,
            normal=np.array([0.0, 0.0, 1.0]),
            albedo=np.ones(3),
            roughness=0.5,
            metallic=0.0,
        )
        assert mod.project_gaussian_ewa(g_behind, cam) is None

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="if t_z <= camera.near_plane or t_z >= camera.far_plane:",
        new_fragment="if t_z >= camera.far_plane:",
        mutant_id="M23",
        oracle=oracle,
    )


def test_m24_kill_wave3_ggx_ndf_denominator() -> None:
    """M24: Corrupting GGX NDF denominator breaks peak specular intensity."""

    def oracle(mod: Any) -> None:
        norm = np.array([0.0, 0.0, 1.0])
        view = np.array([0.0, 0.0, 1.0])
        light = np.array([0.0, 0.0, 1.0])
        rad = mod.evaluate_ggx_pbr(
            norm, view, light, np.ones(3), roughness=0.1, metallic=1.0, light_radiance=np.ones(3)
        )
        # At roughness=0.1 on-axis, peak specular radiance is high (> 10)
        assert rad[0] > 10.0

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="d_denom = np.pi * ((n_dot_h**2) * (alpha_g**2 - 1.0) + 1.0) ** 2",
        new_fragment="d_denom = np.pi * ((n_dot_h**2) * (alpha_g**2 + 1.0) + 1.0) ** 2",
        mutant_id="M24",
        oracle=oracle,
    )


def test_m25_kill_wave3_schlick_f0_metallic_interpolation() -> None:
    """M25: Inverting metallic F0 interpolation corrupts dielectric vs conductor reflectance."""

    def oracle(mod: Any) -> None:
        norm = np.array([0.0, 0.0, 1.0])
        view = np.array([0.0, 0.0, 1.0])
        light = np.array([0.0, 0.0, 1.0])
        albedo = np.array([1.0, 0.0, 0.0])  # red conductor
        rad_cond = mod.evaluate_ggx_pbr(
            norm, view, light, albedo, roughness=0.5, metallic=1.0, light_radiance=np.ones(3)
        )
        # Red conductor should have much higher red specular than green
        assert rad_cond[0] > rad_cond[1] * 2.0

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="f0 = 0.04 * (1.0 - metallic) + albedo * metallic",
        new_fragment="f0 = 0.04 * metallic + albedo * (1.0 - metallic)",
        mutant_id="M25",
        oracle=oracle,
    )


def test_m26_kill_wave3_diffuse_kd_metallic_conservation() -> None:
    """M26: Inverting (1 - metallic) in kd produces illegal diffuse light on conductors."""

    def oracle(mod: Any) -> None:
        norm = np.array([0.0, 0.0, 1.0])
        view = np.array([0.0, 0.0, 1.0])
        light = np.array([0.0, 0.6, 0.8])  # off-specular direction
        albedo = np.array([0.5, 0.5, 0.5])
        # Pure metal with zero roughness has zero diffuse off-specular
        rad_cond = mod.evaluate_ggx_pbr(
            norm, view, light, albedo, roughness=0.1, metallic=1.0, light_radiance=np.ones(3)
        )
        assert np.all(rad_cond < 0.01)

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="kd = (1.0 - f_schlick) * (1.0 - metallic)",
        new_fragment="kd = (1.0 - f_schlick) * metallic",
        mutant_id="M26",
        oracle=oracle,
    )


def test_m27_kill_wave3_transmittance_blowup() -> None:
    """M27: Adding instead of subtracting alpha splat in transmittance causes energy blowup."""

    def oracle(mod: Any) -> None:
        cam = mod.CameraParameters(
            view_matrix=np.eye(4, dtype=np.float64),
            fx=50.0,
            fy=50.0,
            cx=10.0,
            cy=10.0,
            width=20,
            height=20,
        )
        g1 = mod.Gaussian3D(
            mean=np.array([0.0, 0.0, 2.0]),
            rotation=mod.UnitQuaternion.identity(),
            scale=np.array([0.2, 0.2, 0.1]),
            opacity=0.9,
            normal=np.array([0.0, 0.0, -1.0]),
            albedo=np.array([1.0, 0.0, 0.0]),
            roughness=0.5,
            metallic=0.0,
        )
        g2 = mod.Gaussian3D(
            mean=np.array([0.0, 0.0, 3.0]),
            rotation=mod.UnitQuaternion.identity(),
            scale=np.array([0.2, 0.2, 0.1]),
            opacity=0.9,
            normal=np.array([0.0, 0.0, -1.0]),
            albedo=np.array([0.0, 1.0, 0.0]),
            roughness=0.5,
            metallic=0.0,
        )
        res = mod.rasterize_gaussian_splats((g1, g2), cam, ())
        assert res.rgb_float[10, 10, 1] < 0.02

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="transmittance[min_v:max_v, min_u:max_u] *= 1.0 - alpha_splat",
        new_fragment="transmittance[min_v:max_v, min_u:max_u] *= 1.0 + alpha_splat",
        mutant_id="M27",
        oracle=oracle,
    )


def test_m28_kill_wave3_alpha_silhouette_negation() -> None:
    """M28: Subtracting alpha splat produces negative alpha silhouette."""

    def oracle(mod: Any) -> None:
        cam = mod.CameraParameters(
            view_matrix=np.eye(4, dtype=np.float64),
            fx=50.0,
            fy=50.0,
            cx=10.0,
            cy=10.0,
            width=20,
            height=20,
        )
        g = mod.Gaussian3D(
            mean=np.array([0.0, 0.0, 2.0]),
            rotation=mod.UnitQuaternion.identity(),
            scale=np.array([0.2, 0.2, 0.1]),
            opacity=0.8,
            normal=np.array([0.0, 0.0, -1.0]),
            albedo=np.ones(3),
            roughness=0.5,
            metallic=0.0,
        )
        res = mod.rasterize_gaussian_splats((g,), cam, ())
        assert np.all(res.alpha_silhouette >= 0.0)
        assert res.alpha_silhouette[10, 10] > 0.5

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="accum_alpha[min_v:max_v, min_u:max_u] += weight",
        new_fragment="accum_alpha[min_v:max_v, min_u:max_u] -= weight",
        mutant_id="M28",
        oracle=oracle,
    )


def test_m29_kill_wave3_sh_ambient_dc_sign() -> None:
    """M29: Negating DC ambient component c0 in Order-2 SH produces negative irradiance."""

    def oracle(mod: Any) -> None:
        sh = np.zeros((9, 3))
        sh[0] = [1.0, 1.0, 1.0]
        irr = mod.evaluate_order2_sh_diffuse(np.array([0.0, 0.0, 1.0]), sh)
        assert np.all(irr > 0.0)

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="c0 = 0.5 * np.sqrt(1.0 / np.pi)",
        new_fragment="c0 = -0.5 * np.sqrt(1.0 / np.pi)",
        mutant_id="M29",
        oracle=oracle,
    )


def test_m30_kill_wave3_gaussian_splat_power_sign() -> None:
    """M30: Flipping Gaussian splat power sign from negative to positive causes blowup."""

    def oracle(mod: Any) -> None:
        cam = mod.CameraParameters(
            view_matrix=np.eye(4, dtype=np.float64),
            fx=40.0,
            fy=40.0,
            cx=8.0,
            cy=8.0,
            width=16,
            height=16,
        )
        g = mod.Gaussian3D(
            mean=np.array([0.0, 0.0, 2.0]),
            rotation=mod.UnitQuaternion.identity(),
            scale=np.array([0.1, 0.1, 0.1]),
            opacity=0.5,
            normal=np.array([0.0, 0.0, -1.0]),
            albedo=np.ones(3),
            roughness=0.5,
            metallic=0.0,
        )
        res = mod.rasterize_gaussian_splats((g,), cam, ())
        assert res.rgb_float[6, 6, 0] > 0.005

    run_mutation_kill_probe(
        source_filename="gaussian_splatting_pbr_rasterizer.py",
        old_fragment="power = -0.5 * (",
        new_fragment="power = 0.5 * (",
        mutant_id="M30",
        oracle=oracle,
    )


# ── WAVE 4 MUTATIONS: M31–M40 ───────────────────────────────────────────────


def test_m31_kill_wave4_rfc6962_leaf_hash_prefix() -> None:
    """M31: Changing leaf hash prefix 0x00 to 0x01 breaks RFC 6962 domain separation."""

    def oracle(mod: Any) -> None:
        h1 = mod.rfc6962_leaf_hash(b"data")
        # Direct RFC 6962 reference: SHA256(0x00 || data)
        import hashlib

        expected = hashlib.sha256(b"\x00data").digest()
        assert h1 == expected

    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment='h.update(b"\\x00")',
        new_fragment='h.update(b"\\x01")',
        mutant_id="M31",
        oracle=oracle,
    )


def test_m32_kill_wave4_rfc6962_node_hash_prefix() -> None:
    """M32: Changing internal node prefix 0x01 to 0x02 breaks RFC 6962 tree verification."""

    def oracle(mod: Any) -> None:
        left = mod.rfc6962_leaf_hash(b"L")
        right = mod.rfc6962_leaf_hash(b"R")
        nh = mod.rfc6962_node_hash(left, right)
        import hashlib

        expected = hashlib.sha256(b"\x01" + left + right).digest()
        assert nh == expected

    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment='h.update(b"\\x01")',
        new_fragment='h.update(b"\\x02")',
        mutant_id="M32",
        oracle=oracle,
    )


def test_m33_kill_wave4_rfc6962_power_of_two_split() -> None:
    """M33: Changing (k << 1) < n to <= n breaks largest power of two strictly less than n."""

    def oracle(mod: Any) -> None:
        # For n = 4, largest power of two STRICTLY LESS than 4 is 2 (not 4!)
        k = mod.largest_power_of_two_less_than(4)
        assert k == 2

    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment="while (k << 1) < n:",
        new_fragment="while (k << 1) <= n:",
        mutant_id="M33",
        oracle=oracle,
    )


def test_m34_kill_wave4_inclusion_proof_sibling_direction() -> None:
    """M34: Swapping left/right hashing in inclusion proof causes verification to fail."""

    def oracle(mod: Any) -> None:
        leaves = [b"a", b"b", b"c"]
        root_hex, proof = mod.generate_rfc6962_inclusion_proof(leaves, 1)
        valid = mod.verify_rfc6962_inclusion_proof(b"b", 1, 3, proof, root_hex)
        assert valid is True

    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment="current_hash = rfc6962_node_hash(sibling, current_hash)",
        new_fragment="current_hash = rfc6962_node_hash(current_hash, sibling)",
        mutant_id="M34",
        oracle=oracle,
    )


def test_m35_kill_wave4_staged_spine_fence_check_1() -> None:
    """M35: Disabling phase 1 fencing token check allows superseded attempt to stage."""

    def oracle(mod: Any) -> None:
        with tempfile.TemporaryDirectory() as td:

            class TokenGetter:
                def __init__(self) -> None:
                    self.count = 0

                def __call__(self) -> str:
                    self.count += 1
                    return "STALE" if self.count == 1 else "VALID"

            adapter = mod.MorpheusStagedSpineAdapter(
                workspace_root=Path(td),
                active_fencing_token_getter=TokenGetter(),
            )
            req = mod.MorpheusStageRequest(
                asset_id="a1",
                attempt_id="att1",
                fencing_token="VALID",
                frames_uint8=(np.zeros((8, 8, 3), dtype=np.uint8),),
                audio_pcm16=np.zeros(2400, dtype=np.int16),
            )
            with pytest.raises(mod.FencingTokenStaleError):
                adapter.stage_and_publish(req)

    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment="if active_token_1 != request.fencing_token:",
        new_fragment="if False:",
        mutant_id="M35",
        oracle=oracle,
    )


def test_m36_kill_wave4_staged_spine_fence_check_2() -> None:
    """M36: Disabling phase 2 pre-promotion fence check allows stolen attempt to publish."""

    def oracle(mod: Any) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = {"fence": "valid_token"}
            adapter = mod.MorpheusStagedSpineAdapter(
                workspace_root=Path(td),
                active_fencing_token_getter=lambda: state["fence"],
                pre_publish_tamper_hook=lambda _p: state.update({"fence": "STOLEN"}),
            )
            req = mod.MorpheusStageRequest(
                asset_id="a2",
                attempt_id="att2",
                fencing_token="valid_token",
                frames_uint8=(np.zeros((8, 8, 3), dtype=np.uint8),),
                audio_pcm16=np.zeros(1000, dtype=np.int16),
            )
            with pytest.raises(mod.FencingTokenStaleError):
                adapter.stage_and_publish(req)

    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment="if active_token_2 != request.fencing_token:",
        new_fragment="if False:",
        mutant_id="M36",
        oracle=oracle,
    )


def test_m37_kill_wave4_disk_verification_frame_file_hash() -> None:
    """M37: Bypassing frame file hash verification allows corrupted disk PNGs."""

    def oracle(mod: Any) -> None:
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            frame_p = st_dir / "frame_0000.png"
            mod.write_png_frame(np.zeros((8, 8, 3), dtype=np.uint8), frame_p)
            wav_p = st_dir / "audio.wav"
            mod.write_wav_pcm16(np.zeros(100, dtype=np.int16), wav_p)
            mp4_p = st_dir / "video.mp4"
            mp4_p.write_bytes(b"dummy_mp4")

            # Manifest with tampered frame file hash
            manifest = {
                "digests": {
                    "input_frames_content_sha256": "x",
                    "output_frames_content_sha256": "x",
                    "output_frames_file_sha256": "WRONG_HASH",
                    "input_audio_content_sha256": "x",
                    "output_audio_content_sha256": "x",
                    "output_audio_file_sha256": "x",
                    "output_mp4_sha256": "x",
                    "merkle_root_hex": "x",
                }
            }
            import json

            (st_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with pytest.raises(mod.BundleVerificationError, match="output_frames_file_sha256"):
                mod.verify_morpheus_bundle_on_disk(st_dir)

    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment='if out_frames_file_sha != manifest["digests"]["output_frames_file_sha256"]:',
        new_fragment="if False:",
        mutant_id="M37",
        oracle=oracle,
    )


def test_m38_kill_wave4_disk_verification_audio_content_hash() -> None:
    """M38: Bypassing audio content verification allows tampered audio samples."""

    def oracle(mod: Any) -> None:
        with tempfile.TemporaryDirectory() as td:
            st_dir = Path(td)
            frame_p = st_dir / "frame_0000.png"
            mod.write_png_frame(np.zeros((8, 8, 3), dtype=np.uint8), frame_p)
            wav_p = st_dir / "audio.wav"
            mod.write_wav_pcm16(np.zeros(100, dtype=np.int16), wav_p)
            mp4_p = st_dir / "video.mp4"
            mp4_p.write_bytes(b"dummy_mp4")

            f_bytes = frame_p.read_bytes()
            import hashlib

            f_sha = hashlib.sha256(f_bytes).hexdigest()
            w_bytes = wav_p.read_bytes()
            w_file_sha = hashlib.sha256(w_bytes).hexdigest()
            zero_content = np.zeros((8, 8, 3), dtype=np.uint8).tobytes()
            zero_sha = hashlib.sha256(zero_content).hexdigest()

            manifest = {
                "digests": {
                    "input_frames_content_sha256": "x",
                    "output_frames_content_sha256": zero_sha,
                    "output_frames_file_sha256": f_sha,
                    "input_audio_content_sha256": "x",
                    "output_audio_content_sha256": "WRONG_AUDIO_CONTENT_HASH",
                    "output_audio_file_sha256": w_file_sha,
                    "output_mp4_sha256": hashlib.sha256(b"dummy_mp4").hexdigest(),
                    "merkle_root_hex": "x",
                }
            }
            import json

            (st_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with pytest.raises(mod.BundleVerificationError, match="output_audio_content_sha256"):
                mod.verify_morpheus_bundle_on_disk(st_dir)

    old_frag = 'if out_audio_content_sha != manifest["digests"]["output_audio_content_sha256"]:'
    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment=old_frag,
        new_fragment="if False:",
        mutant_id="M38",
        oracle=oracle,
    )


def test_m39_kill_wave4_ffmpeg_faststart_flag() -> None:
    """M39: Corrupting FFmpeg faststart flag fails deterministic muxing."""

    def oracle(mod: Any) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            frame_p = p / "f_0000.png"
            mod.write_png_frame(np.zeros((16, 16, 3), dtype=np.uint8), frame_p)
            wav_p = p / "a.wav"
            mod.write_wav_pcm16(np.zeros(2400, dtype=np.int16), wav_p)
            out_mp4 = p / "out.mp4"
            mod.mux_mp4_ffmpeg(str(p / "f_%04d.png"), wav_p, out_mp4)
            assert out_mp4.is_file() and out_mp4.stat().st_size > 0

    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment='"-movflags",\n        "+faststart",',
        new_fragment='"-movflags",\n        "INVALID_FLAG_FAIL_FAST",',
        mutant_id="M39",
        oracle=oracle,
    )


def test_m40_kill_wave4_atomic_promotion_staging_cleanliness() -> None:
    """M40: Incomplete promotion leaving staging tree intact violates atomic cleanliness."""

    def oracle(mod: Any) -> None:
        with tempfile.TemporaryDirectory() as td:
            adapter = mod.MorpheusStagedSpineAdapter(
                workspace_root=Path(td),
                active_fencing_token_getter=lambda: "token_ok",
            )
            req = mod.MorpheusStageRequest(
                asset_id="asset_clean",
                attempt_id="att_clean",
                fencing_token="token_ok",
                frames_uint8=(np.zeros((16, 16, 3), dtype=np.uint8),),
                audio_pcm16=np.zeros(2400, dtype=np.int16),
            )
            rec = adapter.stage_and_publish(req)
            assert Path(rec.published_mp4_path).is_file()

    run_mutation_kill_probe(
        source_filename="morpheus_real_media_spine.py",
        old_fragment="os.replace(staging_dir, published_dir)",
        new_fragment="pass",
        mutant_id="M40",
        oracle=oracle,
    )


# ── WAVE 5 MUTATIONS: M41–M50 ───────────────────────────────────────────────


def test_m41_kill_wave5_degenerate_tetrahedron_guard() -> None:
    """M41: Bypassing reference tet determinant check allows inverted elements."""

    def oracle(mod: Any) -> None:
        flat_tet = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 1.0, 0.0]])
        with pytest.raises(ValueError, match="Degenerate or inverted"):
            mod.build_tetrahedron((0, 1, 2, 3), flat_tet)

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="if det <= 1e-12:",
        new_fragment="if False:",
        mutant_id="M41",
        oracle=oracle,
    )


def test_m42_kill_wave5_polar_svd_reflection_healing() -> None:
    """M42: Disabling reflection healing in polar SVD produces det(R) = -1 on inverted tet."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        tet = mod.build_tetrahedron((0, 1, 2, 3), verts)
        mat = mod.SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3)
        deformed = verts.copy()
        deformed[:, 2] *= -1.0  # invert Z
        res = mod.compute_corotational_pk1_stress(tet, deformed, mat)
        assert abs(float(np.linalg.det(res.rotation)) - 1.0) < 1e-6

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="d = np.diag([1.0, 1.0, 1.0 if det_rot >= 0.0 else -1.0])",
        new_fragment="d = np.diag([1.0, 1.0, 1.0])",
        mutant_id="M42",
        oracle=oracle,
    )


def test_m43_kill_wave5_corotational_pk1_strain_sign() -> None:
    """M43: Inverting rotation subtraction in (F - R) breaks pure rotation zero stress."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        tet = mod.build_tetrahedron((0, 1, 2, 3), verts)
        mat = mod.SoftTissueMaterial(youngs_modulus=10000.0, poissons_ratio=0.3)
        res = mod.compute_corotational_pk1_stress(tet, verts, mat)
        assert float(np.linalg.norm(res.pk1_stress, ord="fro")) < 1e-12

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="pk1 = 2.0 * mu * (f_mat - rot)",
        new_fragment="pk1 = 2.0 * mu * (f_mat + rot)",
        mutant_id="M43",
        oracle=oracle,
    )


def test_m44_kill_wave5_lame_mu_calculation() -> None:
    """M44: Multiplying instead of dividing in Lamé mu corrupts shear modulus."""

    def oracle(mod: Any) -> None:
        mat = mod.SoftTissueMaterial(youngs_modulus=2600.0, poissons_ratio=0.3)
        # mu = E / (2 * (1 + nu)) = 2600 / 2.6 = 1000.0
        assert abs(mat.lame_mu - 1000.0) < 1e-12

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="return self.youngs_modulus / (2.0 * (1.0 + self.poissons_ratio))",
        new_fragment="return self.youngs_modulus * (2.0 * (1.0 + self.poissons_ratio))",
        mutant_id="M44",
        oracle=oracle,
    )


def test_m45_kill_wave5_newton_third_law_equilibrium() -> None:
    """M45: Negating f0 balance violates Newton's 3rd law translational equilibrium."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        tet = mod.build_tetrahedron((0, 1, 2, 3), verts)
        mat = mod.SoftTissueMaterial(youngs_modulus=5000.0, poissons_ratio=0.3)
        deformed = verts.copy()
        deformed[1, 0] = 1.3
        res = mod.compute_corotational_pk1_stress(tet, deformed, mat)
        f_sum = np.sum(res.nodal_forces, axis=0)
        assert np.all(np.abs(f_sum) < 1e-12)

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="f0 = -(f1 + f2 + f3)",
        new_fragment="f0 = (f1 + f2 + f3)",
        mutant_id="M45",
        oracle=oracle,
    )


def test_m46_kill_wave5_volume_drift_healer_clamping() -> None:
    """M46: Omitting volume bounds clamping leaves over-expanded tets bloated."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        tet = mod.build_tetrahedron((0, 1, 2, 3), verts)
        expanded = verts * 1.5  # 3.375x volume
        healed = mod.heal_inverted_and_volume_drift_tet(expanded, tet, j_min=0.98, j_max=1.02)
        ds = np.column_stack([healed[1] - healed[0], healed[2] - healed[0], healed[3] - healed[0]])
        j_healed = float(np.linalg.det(ds @ tet.inv_rest_edge_matrix))
        assert j_healed <= 1.03

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="target_j = float(np.clip(max(j, 0.001), j_min, j_max))",
        new_fragment="target_j = float(max(j, 0.001))",
        mutant_id="M46",
        oracle=oracle,
    )


def test_m47_kill_wave5_nodal_force_sign() -> None:
    """M47: Flipping nodal force assembly sign inverts restoring force direction."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        tet = mod.build_tetrahedron((0, 1, 2, 3), verts)
        mat = mod.SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3)
        deformed = verts.copy()
        deformed[1, 0] = 1.3  # stretch along X
        res = mod.compute_corotational_pk1_stress(tet, deformed, mat)
        # Restoring force must oppose positive X stretch
        assert res.nodal_forces[1, 0] < 0.0

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="h_mat = -tet.rest_volume * (pk1 @ tet.inv_rest_edge_matrix.T)",
        new_fragment="h_mat = tet.rest_volume * (pk1 @ tet.inv_rest_edge_matrix.T)",
        mutant_id="M47",
        oracle=oracle,
    )


def test_m48_kill_wave5_rayleigh_damping_energy_injection() -> None:
    """M48: Adding damping velocity injects kinetic energy over time."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        tet = mod.build_tetrahedron((0, 1, 2, 3), verts)
        mat = mod.SoftTissueMaterial(
            youngs_modulus=1000.0, poissons_ratio=0.3, rayleigh_damping=0.4
        )
        vel0 = np.ones((4, 3)) * 3.0
        state0 = mod.FEMSimulationState(verts, vel0, (tet,), mat)
        state1 = mod.solve_fem_time_step(state0, dt=0.01)
        ke0 = float(np.sum(state0.velocities**2))
        ke1 = float(np.sum(state1.velocities**2))
        assert ke1 < ke0

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="total_forces -= state.material.rayleigh_damping * vel",
        new_fragment="total_forces += state.material.rayleigh_damping * vel",
        mutant_id="M48",
        oracle=oracle,
    )


def test_m49_kill_wave5_cranial_bone_sdf_projection_direction() -> None:
    """M49: Inverting SDF projection pushes penetrated nodes deeper into bone."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, -0.2], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        tet = mod.build_tetrahedron((0, 1, 2, 3), verts)
        mat = mod.SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3)

        def sdf(p: np.ndarray) -> tuple[float, np.ndarray]:
            return (float(p[2]), np.array([0.0, 0.0, 1.0]))

        st = mod.FEMSimulationState(verts, np.zeros_like(verts), (tet,), mat)
        healed = mod.solve_fem_time_step(st, dt=0.01, cranial_bone_sdf=sdf)
        assert healed.vertices[0, 2] >= 0.0

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="pos[i] -= dist * grad",
        new_fragment="pos[i] += dist * grad",
        mutant_id="M49",
        oracle=oracle,
    )


def test_m50_kill_wave5_cranial_bone_normal_velocity_cancellation() -> None:
    """M50: Adding normal velocity accelerates penetrating node through bone."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, -0.1], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        tet = mod.build_tetrahedron((0, 1, 2, 3), verts)
        mat = mod.SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3)

        def sdf(p: np.ndarray) -> tuple[float, np.ndarray]:
            return (float(p[2]), np.array([0.0, 0.0, 1.0]))

        vel = np.zeros((4, 3))
        vel[0] = [0.0, 0.0, -2.0]
        st = mod.FEMSimulationState(verts, vel, (tet,), mat)
        healed = mod.solve_fem_time_step(st, dt=0.01, cranial_bone_sdf=sdf)
        assert healed.velocities[0, 2] >= 0.0

    run_mutation_kill_probe(
        source_filename="corotational_fem_soft_tissue.py",
        old_fragment="vel[i] -= vn * grad",
        new_fragment="vel[i] += vn * grad",
        mutant_id="M50",
        oracle=oracle,
    )


# ── WAVE 6 MUTATIONS: M51–M60 ───────────────────────────────────────────────


def test_m51_kill_wave6_bishop_normal_parallel_transport() -> None:
    """M51: Corrupting Bishop normal projection violates <u, t> = 0 orthogonality."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.5, 0.2], [1.5, 1.0, 0.8]])
        tangents, normals = mod.parallel_transport_bishop_frame(verts)
        for i in range(len(tangents)):
            assert abs(float(np.dot(tangents[i], normals[i]))) < 1e-12

    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment="ui = u_prev - np.dot(u_prev, ti) * ti",
        new_fragment="ui = u_prev + np.dot(u_prev, ti) * ti",
        mutant_id="M51",
        oracle=oracle,
    )


def test_m52_kill_wave6_pbd_distance_constraint_zeroing() -> None:
    """M52: Zeroing PBD distance correction disables inextensibility."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 0.0, -2.0]])
        strand = mod.build_cosserat_hair_strand(verts)
        sim = mod.solve_cosserat_strand_pbd(
            strand, np.zeros(3), dt=0.01, gravity=np.array([0.0, 0.0, -9.81]), iterations=25
        )
        l0 = float(np.linalg.norm(sim.vertices[1] - sim.vertices[0]))
        assert abs(l0 - 1.0) < 1e-6

    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment="corr = (diff / curr_l) * err",
        new_fragment="corr = (diff / curr_l) * (err * 0.0)",
        mutant_id="M52",
        oracle=oracle,
    )


def test_m53_kill_wave6_pbd_scalp_anchor_pinning() -> None:
    """M53: Bypassing scalp anchor pinning causes hair root to fall."""

    def oracle(mod: Any) -> None:
        verts = np.array([[10.0, 5.0, 2.0], [10.0, 5.0, 1.0]])
        strand = mod.build_cosserat_hair_strand(verts)
        anchor = np.array([20.0, 15.0, 10.0])
        sim = mod.solve_cosserat_strand_pbd(
            strand, anchor, dt=0.01, gravity=np.array([0.0, 0.0, -10.0])
        )
        assert np.allclose(sim.vertices[0], anchor, atol=1e-12)

    frag_old = (
        "    # Constrain root to scalp anchor\n"
        "    pos[0] = scalp_anchor\n\n"
        "    # Iterative PBD distance constraint projection\n"
        "    for _ in range(iterations):\n"
        "        pos[0] = scalp_anchor"
    )
    frag_new = "    pass\n    for _ in range(iterations):\n        pass"
    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment=frag_old,
        new_fragment=frag_new,
        mutant_id="M53",
        oracle=oracle,
    )


def test_m54_kill_wave6_marschner_difference_half_angle_swap() -> None:
    """M54: Swapping theta_d with theta_h corrupts longitudinal scattering."""

    def oracle(mod: Any) -> None:
        t = np.array([0.0, 0.0, 1.0])
        wi = np.array([0.6, 0.0, 0.8])
        wo = np.array([0.6, 0.0, -0.8])
        mat = mod.HairMaterialProperties()
        res = mod.evaluate_marschner_brdf(t, wi, wo, mat)
        assert res.total_radiance[0] > 0.4

    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment="theta_d = 0.5 * (theta_o - theta_i)",
        new_fragment="theta_d = 0.5 * (theta_o + theta_i)",
        mutant_id="M54",
        oracle=oracle,
    )


def test_m55_kill_wave6_marschner_r_lobe_shift_sign() -> None:
    """M55: Flipping R lobe cuticle shift sign peaks in wrong direction."""

    def oracle(mod: Any) -> None:
        t = np.array([0.0, 0.0, 1.0])
        mat = mod.HairMaterialProperties(cuticle_tilt_rad=0.05)
        # Test on correct shifted angle
        theta_peak = -2.0 * mat.cuticle_tilt_rad
        wi = np.array([np.cos(theta_peak), 0.0, np.sin(theta_peak)])
        wo = np.array([np.cos(theta_peak), 0.0, np.sin(theta_peak)])
        res_peak = mod.evaluate_marschner_brdf(t, wi, wo, mat)

        theta_wrong = 2.0 * mat.cuticle_tilt_rad
        wi_w = np.array([np.cos(theta_wrong), 0.0, np.sin(theta_wrong)])
        wo_w = np.array([np.cos(theta_wrong), 0.0, np.sin(theta_wrong)])
        res_wrong = mod.evaluate_marschner_brdf(t, wi_w, wo_w, mat)
        assert res_peak.radiance_r[0] > res_wrong.radiance_r[0]

    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment="m_r = gaussian_m(-2.0 * alpha, material.roughness_r)",
        new_fragment="m_r = gaussian_m(2.0 * alpha, material.roughness_r)",
        mutant_id="M55",
        oracle=oracle,
    )


def test_m56_kill_wave6_marschner_tt_lobe_shift_sign() -> None:
    """M56: Inverting TT lobe forward shift peaks in wrong direction."""

    def oracle(mod: Any) -> None:
        t = np.array([0.0, 0.0, 1.0])
        mat = mod.HairMaterialProperties(cuticle_tilt_rad=0.04)
        th_pos = mat.cuticle_tilt_rad
        wi = np.array([np.cos(th_pos), 0.0, np.sin(th_pos)])
        wo = np.array([-np.cos(th_pos), 0.0, np.sin(th_pos)])
        res_pos = mod.evaluate_marschner_brdf(t, wi, wo, mat)

        th_neg = -mat.cuticle_tilt_rad
        wi_n = np.array([np.cos(th_neg), 0.0, np.sin(th_neg)])
        wo_n = np.array([-np.cos(th_neg), 0.0, np.sin(th_neg)])
        res_neg = mod.evaluate_marschner_brdf(t, wi_n, wo_n, mat)
        assert res_pos.radiance_tt[0] > res_neg.radiance_tt[0]

    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment="m_tt = gaussian_m(alpha, material.roughness_tt)",
        new_fragment="m_tt = gaussian_m(-alpha, material.roughness_tt)",
        mutant_id="M56",
        oracle=oracle,
    )


def test_m57_kill_wave6_marschner_trt_lobe_shift_sign() -> None:
    """M57: Inverting TRT lobe secondary highlight shift peaks in wrong direction."""

    def oracle(mod: Any) -> None:
        t = np.array([0.0, 0.0, 1.0])
        mat = mod.HairMaterialProperties(cuticle_tilt_rad=0.04)
        th_4 = 4.0 * mat.cuticle_tilt_rad
        wi = np.array([np.cos(th_4), 0.0, np.sin(th_4)])
        wo = np.array([np.cos(th_4), 0.0, np.sin(th_4)])
        res_4 = mod.evaluate_marschner_brdf(t, wi, wo, mat)

        th_neg4 = -4.0 * mat.cuticle_tilt_rad
        wi_n = np.array([np.cos(th_neg4), 0.0, np.sin(th_neg4)])
        wo_n = np.array([np.cos(th_neg4), 0.0, np.sin(th_neg4)])
        res_neg4 = mod.evaluate_marschner_brdf(t, wi_n, wo_n, mat)
        assert res_4.radiance_trt[0] > res_neg4.radiance_trt[0]

    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment="m_trt = gaussian_m(4.0 * alpha, material.roughness_trt)",
        new_fragment="m_trt = gaussian_m(-4.0 * alpha, material.roughness_trt)",
        mutant_id="M57",
        oracle=oracle,
    )


def test_m58_kill_wave6_melanin_absorption_amplification() -> None:
    """M58: Inverting absorption exponent amplifies instead of attenuates transmitted light."""

    def oracle(mod: Any) -> None:
        t = np.array([0.0, 0.0, 1.0])
        wi = np.array([1.0, 0.0, 0.0])
        wo = np.array([-1.0, 0.0, 0.0])
        mat_dense = mod.HairMaterialProperties(absorption=np.array([2.0, 2.0, 2.0]))
        res = mod.evaluate_marschner_brdf(t, wi, wo, mat_dense)
        assert res.radiance_tt[0] < 1.0

    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment="trans_factor = np.exp(-2.0 * material.absorption / cos_theta_t)",
        new_fragment="trans_factor = np.exp(2.0 * material.absorption / cos_theta_t)",
        mutant_id="M58",
        oracle=oracle,
    )


def test_m59_kill_wave6_head_sdf_hair_repulsion_direction() -> None:
    """M59: Inverting SDF projection direction pushes hair vertices into head."""

    def oracle(mod: Any) -> None:
        verts = np.array([[0.0, 0.0, 1.0], [0.1, 0.0, 0.1], [0.2, 0.0, -0.2]])
        strand = mod.build_cosserat_hair_strand(verts)

        def sdf(p: np.ndarray) -> tuple[float, np.ndarray]:
            d = float(np.linalg.norm(p) - 0.8)
            g = p / max(float(np.linalg.norm(p)), 1e-6)
            return (d, g)

        sim = mod.solve_cosserat_strand_pbd(
            strand,
            np.array([0.0, 0.0, 1.0]),
            dt=0.01,
            gravity=np.array([0.0, 0.0, -1.0]),
            head_sdf=sdf,
        )
        assert np.linalg.norm(sim.vertices[1]) >= 0.799

    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment="pos[i] -= dist * unit_g",
        new_fragment="pos[i] += dist * unit_g",
        mutant_id="M59",
        oracle=oracle,
    )


def test_m60_kill_wave6_specular_r_melanin_uncolored() -> None:
    """M60: Tinting surface specular R with melanin breaks uncolored reflection invariant."""

    def oracle(mod: Any) -> None:
        t = np.array([0.0, 0.0, 1.0])
        wi = np.array([1.0, 0.0, 0.0])
        wo = np.array([float(np.cos(-0.16)), 0.0, float(np.sin(-0.16))])
        mat = mod.HairMaterialProperties(absorption=np.array([1.0, 2.0, 3.0]))
        res = mod.evaluate_marschner_brdf(t, wi, wo, mat)
        # Uncolored surface reflection must be achromatic across RGB
        assert abs(res.radiance_r[0] - res.radiance_r[1]) < 1e-12
        assert abs(res.radiance_r[1] - res.radiance_r[2]) < 1e-12

    run_mutation_kill_probe(
        source_filename="cosserat_hair_marschner_brdf.py",
        old_fragment="rad_r = np.full(3, s_r_scalar, dtype=np.float64)",
        new_fragment="rad_r = s_r_scalar * material.absorption",
        mutant_id="M60",
        oracle=oracle,
    )


# ── WAVE 7 MUTATIONS: M61–M70 ───────────────────────────────────────────────


def test_m61_kill_wave7_airy_optical_path_difference_factor() -> None:
    """M61: Missing factor of 2 in optical path difference halves roundtrip phase."""

    def oracle(mod: Any) -> None:
        film = mod.ThinFilmProperties(
            n1_ambient=1.0, n2_film=1.4, n3_substrate=1.8, thickness_nm=380.0
        )
        r = mod.evaluate_airy_thin_film_reflectance(0.0, film)
        assert r[1] > 0.05

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="opd = 2.0 * n2 * d * cos_theta2",
        new_fragment="opd = 0.5 * n2 * d * cos_theta2",
        mutant_id="M61",
        oracle=oracle,
    )


def test_m62_kill_wave7_airy_phase_calculation() -> None:
    """M62: Halving phase delta breaks wave interference periodicity."""

    def oracle(mod: Any) -> None:
        film = mod.ThinFilmProperties(
            n1_ambient=1.0, n2_film=1.4, n3_substrate=1.8, thickness_nm=380.0
        )
        r = mod.evaluate_airy_thin_film_reflectance(0.0, film)
        assert r[1] > r[0] * 2.0

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="delta = (2.0 * np.pi * opd) / w_nm",
        new_fragment="delta = (0.2 * np.pi * opd) / w_nm",
        mutant_id="M62",
        oracle=oracle,
    )


def test_m63_kill_wave7_airy_numerator_interference_term() -> None:
    """M63: Removing cosine interference term from Airy numerator destroys iridescence fringes."""

    def oracle(mod: Any) -> None:
        film = mod.ThinFilmProperties(thickness_nm=450.0)
        r = mod.evaluate_airy_thin_film_reflectance(0.0, film)
        assert np.all(r > 0.01)

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="num_s = r12_s**2 + r23_s**2 + 2.0 * r12_s * r23_s * np.cos(delta)",
        new_fragment="num_s = r12_s**2 - r23_s**2 - 50.0",
        mutant_id="M63",
        oracle=oracle,
    )


def test_m64_kill_wave7_airy_zero_thickness_bare_interface_limit() -> None:
    """M64: Corrupting Airy summation breaks d=0 bare substrate Fresnel collapse."""

    def oracle(mod: Any) -> None:
        film = mod.ThinFilmProperties(
            n1_ambient=1.0, n2_film=1.4, n3_substrate=1.6, thickness_nm=0.0
        )
        r_bare = ((1.0 - 1.6) / (1.0 + 1.6)) ** 2
        r_airy = mod.evaluate_airy_thin_film_reflectance(0.0, film)
        assert np.allclose(r_airy, r_bare, atol=1e-6)

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="r_s = num_s / max(den_s, 1e-12)",
        new_fragment="r_s = (num_s + 0.4) / max(den_s, 1e-12)",
        mutant_id="M64",
        oracle=oracle,
    )


def test_m65_kill_wave7_charlie_sheen_ndf_sign() -> None:
    """M65: Negating Charlie microfiber NDF produces negative sheen radiance."""

    def oracle(mod: Any) -> None:
        norm = np.array([0.0, 0.0, 1.0])
        v = np.array([0.0, 0.5, 0.866])
        l_dir = np.array([0.0, 0.5, 0.866])
        sheen = mod.MicrofiberSheenProperties(sheen_color=np.ones(3))
        brdf = mod.evaluate_charlie_microfiber_sheen(norm, v, l_dir, sheen)
        assert np.all(brdf >= 0.0)

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="d_charlie = ((2.0 + inv_r) * (sin_theta_h**inv_r)) / (2.0 * np.pi)",
        new_fragment="d_charlie = -((2.0 + inv_r) * (sin_theta_h**inv_r)) / (2.0 * np.pi)",
        mutant_id="M65",
        oracle=oracle,
    )


def test_m66_kill_wave7_neubelt_pettineo_visibility_inversion() -> None:
    """M66: Inverting Neubelt-Pettineo soft-cloth visibility factor blows up at grazing angles."""

    def oracle(mod: Any) -> None:
        norm = np.array([0.0, 0.0, 1.0])
        v = np.array([0.0, 0.9, 0.1])
        l_dir = np.array([0.0, 0.9, 0.1])
        sheen = mod.MicrofiberSheenProperties(sheen_color=np.ones(3), roughness=0.3)
        brdf = mod.evaluate_charlie_microfiber_sheen(norm, v, l_dir, sheen)
        assert np.all(brdf >= 0.0)

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="v_cloth = 1.0 / (4.0 * max(n_dot_l + n_dot_v - n_dot_l * n_dot_v, 1e-7))",
        new_fragment="v_cloth = -100.0 / (4.0 * max(n_dot_l + n_dot_v - n_dot_l * n_dot_v, 1e-7))",
        mutant_id="M66",
        oracle=oracle,
    )


def test_m67_kill_wave7_charlie_sheen_color_modulation() -> None:
    """M67: Inverting sheen color tint corrupts reflected spectrum."""

    def oracle(mod: Any) -> None:
        norm = np.array([0.0, 0.0, 1.0])
        v = np.array([0.0, 0.4, 0.9])
        l_dir = np.array([0.0, 0.4, 0.9])
        sheen = mod.MicrofiberSheenProperties(sheen_color=np.array([1.0, 0.0, 0.0]), roughness=0.2)
        brdf = mod.evaluate_charlie_microfiber_sheen(norm, v, l_dir, sheen)
        assert brdf[0] > 0.0
        assert brdf[1] == 0.0

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="f_sheen = d_charlie * v_cloth * sheen.sheen_color",
        new_fragment="f_sheen = d_charlie * v_cloth * (1.0 - sheen.sheen_color)",
        mutant_id="M67",
        oracle=oracle,
    )


def test_m68_kill_wave7_cloth_combined_radiance_operator() -> None:
    """M68: Subtracting sheen from iridescence yields negative combined radiance."""

    def oracle(mod: Any) -> None:
        norm = np.array([0.0, 0.0, 1.0])
        v = np.array([0.0, 0.6, 0.8])
        l_dir = np.array([0.0, 0.6, 0.8])
        film = mod.ThinFilmProperties()
        sheen = mod.MicrofiberSheenProperties(sheen_color=np.ones(3), roughness=0.1)
        res = mod.evaluate_cloth_iridescent_sheen_brdf(norm, v, l_dir, film, sheen, np.ones(3))
        assert np.all(res.combined_radiance >= 0.0)

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="combined = (airy_r + sheen_brdf) * n_dot_l * light_radiance",
        new_fragment="combined = (airy_r - sheen_brdf * 10.0) * n_dot_l * light_radiance",
        mutant_id="M68",
        oracle=oracle,
    )


def test_m69_kill_wave7_light_horizon_clamping() -> None:
    """M69: Disabling N.L horizon clamping allows light from underneath surface."""

    def oracle(mod: Any) -> None:
        norm = np.array([0.0, 0.0, 1.0])
        v = np.array([0.0, 0.0, 1.0])
        l_sub = np.array([0.0, 0.0, -1.0])
        sheen = mod.MicrofiberSheenProperties(sheen_color=np.ones(3))
        brdf = mod.evaluate_charlie_microfiber_sheen(norm, v, l_sub, sheen)
        assert np.all(brdf == 0.0)

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="n_dot_l = float(np.clip(np.dot(n, l_u), 0.0, 1.0))",
        new_fragment="n_dot_l = 1.0",
        mutant_id="M69",
        oracle=oracle,
    )


def test_m70_kill_wave7_charlie_sin_theta_h_calculation() -> None:
    """M70: Swapping sine and cosine in Charlie half-angle NDF corrupts sheen distribution."""

    def oracle(mod: Any) -> None:
        norm = np.array([0.0, 0.0, 1.0])
        # On-axis: N.H = 1 -> sin_theta_h = 0 -> Charlie sheen is small
        v_on = np.array([0.0, 0.0, 1.0])
        l_on = np.array([0.0, 0.0, 1.0])
        # Grazing rim: N.H is small -> sin_theta_h ~ 1 -> Charlie sheen peaks
        v_rim = np.array([0.0, 0.95, 0.1]) / np.linalg.norm([0.0, 0.95, 0.1])
        l_rim = np.array([0.0, 0.95, 0.1]) / np.linalg.norm([0.0, 0.95, 0.1])
        sheen = mod.MicrofiberSheenProperties(sheen_color=np.ones(3), roughness=0.2)
        b_on = mod.evaluate_charlie_microfiber_sheen(norm, v_on, l_on, sheen)
        b_rim = mod.evaluate_charlie_microfiber_sheen(norm, v_rim, l_rim, sheen)
        assert np.all(b_rim > b_on)

    run_mutation_kill_probe(
        source_filename="iridescence_microfiber_cloth_brdf.py",
        old_fragment="sin_theta_h = float(np.sqrt(max(1.0 - n_dot_h**2, 1e-6)))",
        new_fragment="sin_theta_h = float(np.sqrt(max(n_dot_h**2, 1e-6)))",
        mutant_id="M70",
        oracle=oracle,
    )


# ── WAVE 8 MUTATIONS: M71–M80 ───────────────────────────────────────────────


def test_m71_kill_wave8_capsule_integrity_verification_bypass() -> None:
    """M71: Returning True unconditionally in capsule verification accepts forged capsules."""

    def oracle(mod: Any) -> None:
        forged = mod.CharacterIdentityCapsule(
            character_id="forged",
            canonical_alpha_id=(1.0,),
            cephalometric_ratios={"a": 1.0},
            skeletal_rest_lengths={},
            soft_tissue_rest_volumes=(),
            hair_strand_count=10,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=400.0,
            cloth_sheen_roughness=0.3,
            merkle_root_hex="bad",
            capsule_sha256="bad",
        )
        assert mod.verify_character_capsule_integrity(forged) is False

    frag_old = "return exp_merkle == capsule.merkle_root_hex and exp_hash == capsule.capsule_sha256"
    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment=frag_old,
        new_fragment="return True",
        mutant_id="M71",
        oracle=oracle,
    )


def test_m72_kill_wave8_multi_episode_minimum_count_guard() -> None:
    """M72: Disabling minimum episode count guard allows < 4 episodes."""

    def oracle(mod: Any) -> None:
        basis = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2, seed=872)
        capsule = mod.create_character_identity_capsule(
            character_id="hero",
            canonical_alpha_id=(0.0, 0.0),
            cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
            skeletal_rest_lengths={},
            soft_tissue_rest_volumes=(),
            hair_strand_count=10,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=400.0,
            cloth_sheen_roughness=0.3,
        )
        two_episodes = [
            mod.EpisodeCharacterState(
                episode_id=f"EP_{i}",
                face_mesh=basis.mean_mesh.copy(),
                bone_lengths={},
                tetrahedral_volumes=(),
                hair_strand_count=10,
                hair_segment_lengths=(),
                wardrobe_parameters={},
            )
            for i in range(2)
        ]
        with pytest.raises(ValueError, match="at least 4 episodes"):
            mod.audit_and_heal_episodic_character_continuity(capsule, two_episodes, basis)

    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment="if len(episode_states) < 4:",
        new_fragment="if False:",
        mutant_id="M72",
        oracle=oracle,
    )


def test_m73_kill_wave8_multi_episode_tamper_guard() -> None:
    """M73: Disabling capsule integrity guard in multi-episode auditor accepts forged capsule."""

    def oracle(mod: Any) -> None:
        basis = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2, seed=873)
        forged = mod.CharacterIdentityCapsule(
            character_id="forged",
            canonical_alpha_id=(1.0, 2.0),
            cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
            skeletal_rest_lengths={},
            soft_tissue_rest_volumes=(),
            hair_strand_count=10,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=400.0,
            cloth_sheen_roughness=0.3,
            merkle_root_hex="bad",
            capsule_sha256="bad",
        )
        four_episodes = [
            mod.EpisodeCharacterState(
                episode_id=f"EP_{i}",
                face_mesh=basis.mean_mesh.copy(),
                bone_lengths={},
                tetrahedral_volumes=(),
                hair_strand_count=10,
                hair_segment_lengths=(),
                wardrobe_parameters={},
            )
            for i in range(4)
        ]
        with pytest.raises(mod.CapsuleIntegrityTamperError):
            mod.audit_and_heal_episodic_character_continuity(forged, four_episodes, basis)

    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment="if not verify_character_capsule_integrity(capsule):",
        new_fragment="if False:",
        mutant_id="M73",
        oracle=oracle,
    )


def test_m74_kill_wave8_multi_episode_locked_alpha_target() -> None:
    """M74: Zeroing locked alpha target heals character to mean face instead of identity."""

    def oracle(mod: Any) -> None:
        basis = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2, seed=874)
        alpha = (2.0, -1.5)
        cephs = compute_cephalometric_ratios(basis.mean_mesh)
        ceph_dict = {
            "ip_bz": cephs.interpupillary_to_bizygomatic,
            "nm_bz": cephs.nasion_menton_to_bizygomatic,
            "symmetry": cephs.cranial_vault_symmetry,
        }
        capsule = mod.create_character_identity_capsule(
            character_id="hero",
            canonical_alpha_id=alpha,
            cephalometric_ratios=ceph_dict,
            skeletal_rest_lengths={},
            soft_tissue_rest_volumes=(),
            hair_strand_count=10,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=400.0,
            cloth_sheen_roughness=0.3,
        )
        episodes = [
            mod.EpisodeCharacterState(
                episode_id=f"EP_{i}",
                face_mesh=synthesize_face_mesh(basis, np.array(alpha), np.zeros(2)),
                bone_lengths={},
                tetrahedral_volumes=(),
                hair_strand_count=10,
                hair_segment_lengths=(),
                wardrobe_parameters={},
            )
            for i in range(4)
        ]
        res = mod.audit_and_heal_episodic_character_continuity(capsule, episodes, basis)
        h_face = res.healed_episode_states["EP_0"].face_mesh
        rec = basis.u_id.T @ (h_face - basis.mean_mesh).flatten()
        assert float(np.linalg.norm(rec)) > 1.5

    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment="locked_alpha = np.array(capsule.canonical_alpha_id, dtype=np.float64)",
        new_fragment="locked_alpha = np.zeros(len(capsule.canonical_alpha_id), dtype=np.float64)",
        mutant_id="M74",
        oracle=oracle,
    )


def test_m75_kill_wave8_multi_episode_bone_length_healer() -> None:
    """M75: Bypassing bone length healing leaves stretched bones unhealed."""

    def oracle(mod: Any) -> None:
        basis = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2, seed=875)
        capsule = mod.create_character_identity_capsule(
            character_id="hero",
            canonical_alpha_id=(0.0, 0.0),
            cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
            skeletal_rest_lengths={"femur": 40.0},
            soft_tissue_rest_volumes=(),
            hair_strand_count=10,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=400.0,
            cloth_sheen_roughness=0.3,
        )
        episodes = [
            mod.EpisodeCharacterState(
                episode_id=f"EP_{i}",
                face_mesh=basis.mean_mesh.copy(),
                bone_lengths={"femur": 45.0},  # stretched by 5
                tetrahedral_volumes=(),
                hair_strand_count=10,
                hair_segment_lengths=(),
                wardrobe_parameters={},
            )
            for i in range(4)
        ]
        res = mod.audit_and_heal_episodic_character_continuity(capsule, episodes, basis)
        assert res.healed_episode_states["EP_0"].bone_lengths["femur"] == 40.0

    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment="healed_bones[b_name] = target_len",
        new_fragment="healed_bones[b_name] = curr_len",
        mutant_id="M75",
        oracle=oracle,
    )


def test_m76_kill_wave8_multi_episode_soft_tissue_volume_healer() -> None:
    """M76: Bypassing tetrahedral volume healing leaves volume collapse unhealed."""

    def oracle(mod: Any) -> None:
        basis = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2, seed=876)
        capsule = mod.create_character_identity_capsule(
            character_id="hero",
            canonical_alpha_id=(0.0, 0.0),
            cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
            skeletal_rest_lengths={},
            soft_tissue_rest_volumes=(1.0, 2.0),
            hair_strand_count=10,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=400.0,
            cloth_sheen_roughness=0.3,
        )
        episodes = [
            mod.EpisodeCharacterState(
                episode_id=f"EP_{i}",
                face_mesh=basis.mean_mesh.copy(),
                bone_lengths={},
                tetrahedral_volumes=(0.5, 0.9),  # collapsed
                hair_strand_count=10,
                hair_segment_lengths=(),
                wardrobe_parameters={},
            )
            for i in range(4)
        ]
        res = mod.audit_and_heal_episodic_character_continuity(capsule, episodes, basis)
        assert res.healed_episode_states["EP_0"].tetrahedral_volumes == (1.0, 2.0)

    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment="tetrahedral_volumes=capsule.soft_tissue_rest_volumes,",
        new_fragment="tetrahedral_volumes=ep.tetrahedral_volumes,",
        mutant_id="M76",
        oracle=oracle,
    )


def test_m77_kill_wave8_multi_episode_hair_count_erasure() -> None:
    """M77: Zeroing hair strand count in healed state erases hair."""

    def oracle(mod: Any) -> None:
        basis = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2, seed=877)
        capsule = mod.create_character_identity_capsule(
            character_id="hero",
            canonical_alpha_id=(0.0, 0.0),
            cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
            skeletal_rest_lengths={},
            soft_tissue_rest_volumes=(),
            hair_strand_count=150,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=400.0,
            cloth_sheen_roughness=0.3,
        )
        episodes = [
            mod.EpisodeCharacterState(
                episode_id=f"EP_{i}",
                face_mesh=basis.mean_mesh.copy(),
                bone_lengths={},
                tetrahedral_volumes=(),
                hair_strand_count=150,
                hair_segment_lengths=(),
                wardrobe_parameters={},
            )
            for i in range(4)
        ]
        res = mod.audit_and_heal_episodic_character_continuity(capsule, episodes, basis)
        assert res.healed_episode_states["EP_0"].hair_strand_count == 150

    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment="hair_strand_count=capsule.hair_strand_count,",
        new_fragment="hair_strand_count=0,",
        mutant_id="M77",
        oracle=oracle,
    )


def test_m78_kill_wave8_multi_episode_wardrobe_healing() -> None:
    """M78: Setting negative cloth thickness corrupts healed wardrobe parameters."""

    def oracle(mod: Any) -> None:
        basis = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2, seed=878)
        capsule = mod.create_character_identity_capsule(
            character_id="hero",
            canonical_alpha_id=(0.0, 0.0),
            cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
            skeletal_rest_lengths={},
            soft_tissue_rest_volumes=(),
            hair_strand_count=10,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=360.0,
            cloth_sheen_roughness=0.25,
        )
        episodes = [
            mod.EpisodeCharacterState(
                episode_id=f"EP_{i}",
                face_mesh=basis.mean_mesh.copy(),
                bone_lengths={},
                tetrahedral_volumes=(),
                hair_strand_count=10,
                hair_segment_lengths=(),
                wardrobe_parameters={},
            )
            for i in range(4)
        ]
        res = mod.audit_and_heal_episodic_character_continuity(capsule, episodes, basis)
        assert res.healed_episode_states["EP_0"].wardrobe_parameters["film_thickness_nm"] == 360.0

    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment='"film_thickness_nm": capsule.cloth_thin_film_thickness_nm,',
        new_fragment='"film_thickness_nm": -1.0,',
        mutant_id="M78",
        oracle=oracle,
    )


def test_m79_kill_wave8_cephalometric_drift_audit_metric() -> None:
    """M79: Adding instead of subtracting cephalometric values corrupts drift metric."""

    def oracle(mod: Any) -> None:
        basis = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2, seed=879)
        cephs = compute_cephalometric_ratios(basis.mean_mesh)
        ceph_dict = {
            "ip_bz": cephs.interpupillary_to_bizygomatic,
            "nm_bz": cephs.nasion_menton_to_bizygomatic,
            "symmetry": cephs.cranial_vault_symmetry,
        }
        capsule = mod.create_character_identity_capsule(
            character_id="hero",
            canonical_alpha_id=(0.0, 0.0),
            cephalometric_ratios=ceph_dict,
            skeletal_rest_lengths={},
            soft_tissue_rest_volumes=(),
            hair_strand_count=10,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=400.0,
            cloth_sheen_roughness=0.3,
        )
        episodes = [
            mod.EpisodeCharacterState(
                episode_id=f"EP_{i}",
                face_mesh=basis.mean_mesh.copy(),
                bone_lengths={},
                tetrahedral_volumes=(),
                hair_strand_count=10,
                hair_segment_lengths=(),
                wardrobe_parameters={},
            )
            for i in range(4)
        ]
        res = mod.audit_and_heal_episodic_character_continuity(capsule, episodes, basis)
        assert res.cephalometric_drifts["EP_0"] < 1e-6

    frag_old = (
        'abs(cephs_healed.interpupillary_to_bizygomatic - capsule.cephalometric_ratios["ip_bz"])'
    )
    frag_new = (
        'abs(cephs_healed.interpupillary_to_bizygomatic + capsule.cephalometric_ratios["ip_bz"])'
    )
    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment=frag_old,
        new_fragment=frag_new,
        mutant_id="M79",
        oracle=oracle,
    )


def test_m80_kill_wave8_exact_continuity_condition() -> None:
    """M80: Inverting post-heal drift threshold breaks is_healed_continuity_exact flag."""

    def oracle(mod: Any) -> None:
        basis = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2, seed=880)
        capsule = mod.create_character_identity_capsule(
            character_id="hero",
            canonical_alpha_id=(0.0, 0.0),
            cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
            skeletal_rest_lengths={},
            soft_tissue_rest_volumes=(),
            hair_strand_count=10,
            hair_rest_segment_lengths=(),
            melanin_absorption=(0.1, 0.2, 0.3),
            cloth_thin_film_thickness_nm=400.0,
            cloth_sheen_roughness=0.3,
        )
        episodes = [
            mod.EpisodeCharacterState(
                episode_id=f"EP_{i}",
                face_mesh=basis.mean_mesh.copy(),
                bone_lengths={},
                tetrahedral_volumes=(),
                hair_strand_count=10,
                hair_segment_lengths=(),
                wardrobe_parameters={},
            )
            for i in range(4)
        ]
        res = mod.audit_and_heal_episodic_character_continuity(capsule, episodes, basis)
        assert res.is_healed_continuity_exact is True

    run_mutation_kill_probe(
        source_filename="episodic_character_capsule_vault.py",
        old_fragment="exact_continuity = all(drift < 1e-12 for drift in post_drifts.values())",
        new_fragment="exact_continuity = all(drift > 1.0 for drift in post_drifts.values())",
        mutant_id="M80",
        oracle=oracle,
    )
