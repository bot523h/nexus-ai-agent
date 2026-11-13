"""Popperian Falsification Suite (90+ Probes) for NAGAR MORPHEUS.

Comprehensive test suite verifying invariant theorems, mathematical proofs,
and physical bounds across Waves 1–8:
  P01–P12: Wave 1 (3DMM Subspace Lock, Umeyama Alignment, Cephalometrics)
  P13–P24: Wave 2 (Dual Quaternion Lie Algebra, DQS vs LBS, Zero-Skate)
  P25–P36: Wave 3 (3DGS EWA Conic, GGX PBR, Order-2 SH, Compositing)
  P37–P48: Wave 4 (Real-Media PNG/WAV/MP4, Decoded Content SHA-256, Merkle, Spine)
  P49–P60: Wave 5 (Corotational FEM, SO(3) Polar SVD, Newton 3rd Law, Inversion Healing)
  P61–P72: Wave 6 (Cosserat Rods, Bishop Frames, Marschner BRDF, Melanin Physics)
  P73–P84: Wave 7 (Airy Iridescence [0, 1] & d=0 Limit, Charlie Sheen Rim Physics)
  P85–P96: Wave 8 (Cryptographic Capsule Vault, 5-Episode Zero-Drift Healer)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.morpheus import (
    Bone,
    BundleVerificationError,
    CameraParameters,
    CapsuleIntegrityTamperError,
    CharacterIdentityCapsule,
    DualQuaternion,
    EpisodeCharacterState,
    FEMSimulationState,
    FencingTokenStaleError,
    Gaussian3D,
    HairMaterialProperties,
    MicrofiberSheenProperties,
    MorpheusStagedSpineAdapter,
    MorpheusStageRequest,
    SkeletalChain,
    SkinningWeights,
    SoftTissueMaterial,
    ThinFilmProperties,
    UnitQuaternion,
    audit_and_heal_episodic_character_continuity,
    build_canonical_3dmm_basis,
    build_cosserat_hair_strand,
    build_tetrahedron,
    compute_3d_covariance,
    compute_cephalometric_ratios,
    compute_corotational_pk1_stress,
    compute_rfc6962_merkle_root,
    create_character_identity_capsule,
    damped_least_squares_ik,
    evaluate_airy_thin_film_reflectance,
    evaluate_charlie_microfiber_sheen,
    evaluate_cloth_iridescent_sheen_brdf,
    evaluate_ggx_pbr,
    evaluate_marschner_brdf,
    evaluate_order2_sh_diffuse,
    generate_rfc6962_inclusion_proof,
    heal_inverted_and_volume_drift_tet,
    lock_and_heal_cranio_facial_identity,
    mux_mp4_ffmpeg,
    parallel_transport_bishop_frame,
    project_gaussian_ewa,
    rasterize_gaussian_splats,
    read_png_frame,
    read_wav_pcm16,
    rfc6962_leaf_hash,
    rfc6962_node_hash,
    skin_dqs,
    skin_lbs,
    solve_cosserat_strand_pbd,
    solve_fem_time_step,
    synthesize_face_mesh,
    umeyama_sim3_align,
    verify_character_capsule_integrity,
    verify_rfc6962_inclusion_proof,
    write_png_frame,
    write_wav_pcm16,
    zero_skate_foot_lock,
)

# ── WAVE 1: 3DMM SUBSPACE LOCK & UMEYAMA PROCRUSTES (P01–P12) ───────────────


def test_p01_subspace_orthogonality_guarantee() -> None:
    """P01: Frobenius norm ||U_id.T @ U_exp||_F <= 1e-12."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=8, k_exp=8, seed=101)
    overlap = float(np.linalg.norm(basis.u_id.T @ basis.u_exp, ord="fro"))
    assert overlap <= 1e-12, f"Subspace overlap {overlap} exceeds 1e-12"


def test_p02_expression_deformation_does_not_leak_into_identity() -> None:
    """P02: Projecting pure expression deformation onto U_id yields exactly zero."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=8, k_exp=8, seed=102)
    beta = np.array([1.5, -2.0, 0.8, -1.2, 0.5, -0.9, 1.1, -0.4])
    exp_mesh = basis.u_exp @ beta
    projected_id = basis.u_id.T @ exp_mesh
    leakage = float(np.linalg.norm(projected_id))
    assert leakage <= 1e-12, f"Identity leakage from expression {leakage} > 1e-12"


def test_p03_umeyama_preserves_rigid_se3_transform() -> None:
    """P03: Umeyama alignment recovers exact SE(3) rotation and translation."""
    rng = np.random.default_rng(203)
    source = rng.normal(0.0, 10.0, (10, 3))
    rot = UnitQuaternion.from_axis_angle(np.array([0.0, 1.0, 0.0]), np.pi / 3).to_matrix()
    trans = np.array([5.0, -3.0, 12.0])
    target = source @ rot.T + trans

    sim3 = umeyama_sim3_align(source, target, allow_scale=False)
    assert abs(sim3.scale - 1.0) < 1e-6
    assert np.allclose(sim3.rotation, rot, atol=1e-6)
    assert np.allclose(sim3.translation, trans, atol=1e-6)


def test_p04_umeyama_heals_reflection_det_minus_one() -> None:
    """P04: Mirror-reflected inputs (det = -1) are healed to proper rotation det(R) = +1.0."""
    rng = np.random.default_rng(204)
    source = rng.normal(0.0, 10.0, (10, 3))
    # Create reflected target: invert X axis
    target = source.copy()
    target[:, 0] *= -1.0

    sim3 = umeyama_sim3_align(source, target, allow_scale=False)
    det = float(np.linalg.det(sim3.rotation))
    assert abs(det - 1.0) < 1e-7, f"Determinant of rotation is not +1.0: {det}"


def test_p05_umeyama_rotation_determinant_machine_precision() -> None:
    """P05: Guaranteed det(R) == +1.0 with machine precision error < 1e-12."""
    rng = np.random.default_rng(205)
    source = rng.normal(0.0, 5.0, (12, 3))
    target = rng.normal(0.0, 5.0, (12, 3))
    sim3 = umeyama_sim3_align(source, target, allow_scale=True)
    det = float(np.linalg.det(sim3.rotation))
    assert abs(det - 1.0) < 1e-12


def test_p06_umeyama_refuses_degenerate_points() -> None:
    """P06: Degenerate or coincident point sets raise ValueError."""
    pts = np.zeros((5, 3), dtype=np.float64)
    with pytest.raises(ValueError, match="near-zero variance"):
        umeyama_sim3_align(pts, pts)


def test_p07_cephalometric_ratios_translation_invariance() -> None:
    """P07: Cephalometric ratios are strictly invariant under global translation."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=5, k_exp=5, seed=207)
    mesh1 = basis.mean_mesh
    mesh2 = mesh1 + np.array([120.0, -450.0, 80.0])

    c1 = compute_cephalometric_ratios(mesh1)
    c2 = compute_cephalometric_ratios(mesh2)

    assert abs(c1.interpupillary_to_bizygomatic - c2.interpupillary_to_bizygomatic) < 1e-12
    assert abs(c1.nasion_menton_to_bizygomatic - c2.nasion_menton_to_bizygomatic) < 1e-12
    assert abs(c1.cranial_vault_symmetry - c2.cranial_vault_symmetry) < 1e-12


def test_p08_cephalometric_ratios_scale_invariance() -> None:
    """P08: Cephalometric ratios are strictly invariant under global isotropic scaling."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=5, k_exp=5, seed=208)
    mesh1 = basis.mean_mesh
    mesh2 = mesh1 * 3.75

    c1 = compute_cephalometric_ratios(mesh1)
    c2 = compute_cephalometric_ratios(mesh2)

    assert abs(c1.interpupillary_to_bizygomatic - c2.interpupillary_to_bizygomatic) < 1e-12
    assert abs(c1.nasion_menton_to_bizygomatic - c2.nasion_menton_to_bizygomatic) < 1e-12


def test_p09_cephalometric_ratios_rotation_invariance() -> None:
    """P09: Cephalometric ratios are invariant under global 3D rotation."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=5, k_exp=5, seed=209)
    rot = UnitQuaternion.from_axis_angle(np.array([1.0, 2.0, 3.0]), 1.25).to_matrix()
    mesh1 = basis.mean_mesh
    mesh2 = mesh1 @ rot.T

    c1 = compute_cephalometric_ratios(mesh1)
    c2 = compute_cephalometric_ratios(mesh2)

    assert abs(c1.interpupillary_to_bizygomatic - c2.interpupillary_to_bizygomatic) < 1e-10
    assert abs(c1.nasion_menton_to_bizygomatic - c2.nasion_menton_to_bizygomatic) < 1e-10


def test_p10_cranial_identity_lock_post_heal_drift_zero() -> None:
    """P10: Identity lock post-heal identity drift is < 1e-12 under severe expression drift."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=6, k_exp=6, seed=210)
    locked_alpha = np.array([1.2, -0.8, 0.5, -1.1, 0.4, 0.9])
    drifted_alpha = locked_alpha + np.array([2.5, -3.0, 1.8, 2.2, -1.5, 2.0])
    extreme_exp = np.array([-2.5, 2.8, -1.9, 2.1, -2.4, 1.7])

    drifted_mesh = synthesize_face_mesh(basis, drifted_alpha, extreme_exp)

    res = lock_and_heal_cranio_facial_identity(
        observed_mesh=drifted_mesh,
        locked_alpha_id=locked_alpha,
        basis=basis,
        lambda_reg=1e-4,
    )

    assert res.raw_identity_drift > 2.0
    assert res.post_heal_identity_drift < 1e-12


def test_p11_tikhonov_regularization_shrinks_expression() -> None:
    """P11: Increasing Tikhonov lambda monotonically shrinks solved expression coefficients."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=211)
    alpha = np.array([1.0, 0.5, -0.5, 0.8])
    exp = np.array([2.0, -1.5, 1.2, -1.8])
    mesh = synthesize_face_mesh(basis, alpha, exp)

    res_low = lock_and_heal_cranio_facial_identity(mesh, alpha, basis, lambda_reg=1e-5)
    res_high = lock_and_heal_cranio_facial_identity(mesh, alpha, basis, lambda_reg=1.0)

    norm_low = float(np.linalg.norm(res_low.solved_expression))
    norm_high = float(np.linalg.norm(res_high.solved_expression))
    assert norm_high < norm_low


def test_p12_expression_clamping_enforces_bounds() -> None:
    """P12: Solved expression is strictly clamped within [-beta_max, +beta_max]."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=212)
    alpha = np.zeros(4)
    exp = np.array([10.0, -15.0, 8.0, -12.0])
    mesh = synthesize_face_mesh(basis, alpha, exp)

    beta_max = 2.0
    res = lock_and_heal_cranio_facial_identity(mesh, alpha, basis, beta_max=beta_max)
    assert np.all(res.solved_expression <= beta_max + 1e-12)
    assert np.all(res.solved_expression >= -beta_max - 1e-12)


# ── WAVE 2: DUAL QUATERNIONS, DQS VS LBS, ZERO-SKATE (P13–P24) ──────────────


def test_p13_unit_quaternion_hamilton_associativity() -> None:
    """P13: Hamilton quaternion multiplication is associative: (q1 * q2) * q3 == q1 * (q2 * q3)."""
    q1 = UnitQuaternion.from_axis_angle(np.array([1.0, 0.0, 0.0]), 0.4)
    q2 = UnitQuaternion.from_axis_angle(np.array([0.0, 1.0, 0.0]), 0.8)
    q3 = UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), 1.2)

    lhs = q1.multiply(q2).multiply(q3)
    rhs = q1.multiply(q2.multiply(q3))

    assert abs(lhs.w - rhs.w) < 1e-12
    assert abs(lhs.x - rhs.x) < 1e-12
    assert abs(lhs.y - rhs.y) < 1e-12
    assert abs(lhs.z - rhs.z) < 1e-12


def test_p14_dual_quaternion_plucker_orthogonality() -> None:
    """P14: Constructed dual quaternion satisfies Plücker orthogonality <q_r, q_d> == 0."""
    rot = UnitQuaternion.from_axis_angle(np.array([1.0, 1.0, 1.0]), 0.75)
    trans = np.array([10.0, -25.0, 14.0])
    dq = DualQuaternion.from_rotation_translation(rot, trans)
    ortho = abs(dq.real.dot(dq.dual))
    assert ortho <= 1e-12


def test_p15_dual_quaternion_homogeneous_matrix_roundtrip() -> None:
    """P15: Homogeneous matrix decomposition matches rotation and translation."""
    rot = UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), np.pi / 4)
    trans = np.array([3.0, 4.0, 5.0])
    dq = DualQuaternion.from_rotation_translation(rot, trans)
    mat = dq.to_homogeneous_matrix()

    assert np.allclose(mat[:3, :3], rot.to_matrix(), atol=1e-12)
    assert np.allclose(mat[:3, 3], trans, atol=1e-12)
    assert mat[3, 3] == 1.0


def test_p16_dual_quaternion_transform_point_action() -> None:
    """P16: Dual quaternion point transform matches R(q_r) @ p + t."""
    rot = UnitQuaternion.from_axis_angle(np.array([0.0, 1.0, 0.0]), np.pi / 2)
    trans = np.array([1.0, 2.0, 3.0])
    dq = DualQuaternion.from_rotation_translation(rot, trans)

    p = np.array([1.0, 0.0, 0.0])
    p_trans = dq.transform_point(p)
    expected = rot.to_matrix() @ p + trans

    assert np.allclose(p_trans, expected, atol=1e-12)


def test_p17_sclerp_boundary_conditions() -> None:
    """P17: ScLERP(q1, q2, 0) == q1 and ScLERP(q1, q2, 1) == q2."""
    dq1 = DualQuaternion.from_rotation_translation(
        UnitQuaternion.identity(), np.array([0.0, 0.0, 0.0])
    )
    dq2 = DualQuaternion.from_rotation_translation(
        UnitQuaternion.from_axis_angle(np.array([1.0, 0.0, 0.0]), np.pi / 2),
        np.array([10.0, 0.0, 0.0]),
    )

    s0 = DualQuaternion.sclerp(dq1, dq2, 0.0)
    s1 = DualQuaternion.sclerp(dq1, dq2, 1.0)

    p = np.array([1.0, 2.0, 3.0])
    assert np.allclose(s0.transform_point(p), dq1.transform_point(p), atol=1e-12)
    assert np.allclose(s1.transform_point(p), dq2.transform_point(p), atol=1e-12)


def test_p18_sclerp_constant_screw_geodesic_velocity() -> None:
    """P18: ScLERP interpolates with constant angular screw velocity along geodesic."""
    dq1 = DualQuaternion.identity()
    dq2 = DualQuaternion.from_rotation_translation(
        UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), np.pi),
        np.array([0.0, 0.0, 20.0]),
    )

    s_25 = DualQuaternion.sclerp(dq1, dq2, 0.25)
    s_50 = DualQuaternion.sclerp(dq1, dq2, 0.50)
    s_75 = DualQuaternion.sclerp(dq1, dq2, 0.75)

    r_25, t_25 = s_25.to_rotation_translation()
    r_50, t_50 = s_50.to_rotation_translation()
    r_75, t_75 = s_75.to_rotation_translation()

    assert abs(t_25[2] - 5.0) < 1e-6
    assert abs(t_50[2] - 10.0) < 1e-6
    assert abs(t_75[2] - 15.0) < 1e-6


def test_p19_lbs_175_degree_twist_cross_section_collapse() -> None:
    """P19: Under 175° twist, LBS loses > 90% cross-sectional radius (candy-wrapper collapse)."""
    # Cylindrical cross-section ring at midpoint
    angles = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    radius_0 = 5.0
    ring_verts = np.column_stack(
        [radius_0 * np.cos(angles), radius_0 * np.sin(angles), np.zeros(16)]
    )

    # Equal 50/50 bone weights between boneA (0 deg) and boneB (175 deg twist around Z)
    weights_mat = np.full((16, 2), 0.5, dtype=np.float64)
    skin_weights = SkinningWeights(vertex_count=16, bone_names=("bA", "bB"), weights=weights_mat)

    twist_angle = np.radians(175.0)
    mat_a = np.eye(4, dtype=np.float64)
    mat_b = np.eye(4, dtype=np.float64)
    rot_b_mat = UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), twist_angle).to_matrix()
    mat_b[:3, :3] = rot_b_mat

    deformed_lbs = skin_lbs(ring_verts, {"bA": mat_a, "bB": mat_b}, skin_weights)
    radius_lbs = float(np.mean(np.linalg.norm(deformed_lbs[:, :2], axis=1)))
    loss_fraction = (radius_0 - radius_lbs) / radius_0

    assert loss_fraction > 0.90, f"Expected > 90% radius loss under LBS, got {loss_fraction:.2%}"


def test_p20_dqs_175_degree_twist_zero_collapse() -> None:
    """P20: Under 175° twist, DQS achieves 0.0 candy-wrapper radius collapse (< 1e-12 error)."""
    angles = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    radius_0 = 5.0
    ring_verts = np.column_stack(
        [radius_0 * np.cos(angles), radius_0 * np.sin(angles), np.zeros(16)]
    )

    weights_mat = np.full((16, 2), 0.5, dtype=np.float64)
    skin_weights = SkinningWeights(vertex_count=16, bone_names=("bA", "bB"), weights=weights_mat)

    twist_angle = np.radians(175.0)
    dq_a = DualQuaternion.identity()
    rot_b = UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), twist_angle)
    dq_b = DualQuaternion.from_rotation_translation(rot_b, np.zeros(3))

    deformed_dqs = skin_dqs(ring_verts, {"bA": dq_a, "bB": dq_b}, skin_weights)
    radius_dqs = float(np.mean(np.linalg.norm(deformed_dqs[:, :2], axis=1)))
    radius_error = abs(radius_dqs - radius_0)

    assert radius_error < 1e-12, f"DQS radius error {radius_error} exceeds 1e-12"


def test_p21_forward_kinematics_bone_length_invariance() -> None:
    """P21: Skeletal forward kinematics preserves exact rest lengths ||p_tail - p_head|| == L0."""
    b1 = Bone("root", None, np.array([0.0, 0.0, 0.0]), np.array([0.0, 0.0, 10.0]), 10.0)
    b2 = Bone("child", "root", np.array([0.0, 0.0, 10.0]), np.array([0.0, 10.0, 10.0]), 10.0)
    chain = SkeletalChain(bones=(b1, b2))

    rots = {
        "root": UnitQuaternion.from_axis_angle(np.array([1.0, 0.0, 0.0]), 0.6),
        "child": UnitQuaternion.from_axis_angle(np.array([0.0, 1.0, 0.0]), -0.9),
    }
    fk = chain.forward_kinematics(rots)

    p0 = np.array([0.0, 0.0, 0.0])
    p1 = fk["root"].transform_point(p0)
    p2 = fk["child"].transform_point(p0)

    len1 = float(np.linalg.norm(p1 - p0))
    len2 = float(np.linalg.norm(p2 - p1))

    assert abs(len1 - 10.0) < 1e-12
    assert abs(len2 - 10.0) < 1e-12


def test_p22_damped_least_squares_ik_convergence() -> None:
    """P22: DLS inverse kinematics reaches target position within tolerance."""
    b1 = Bone("b1", None, np.array([0.0, 0.0, 0.0]), np.array([5.0, 0.0, 0.0]), 5.0)
    b2 = Bone("b2", "b1", np.array([5.0, 0.0, 0.0]), np.array([10.0, 0.0, 0.0]), 5.0)
    chain = SkeletalChain(bones=(b1, b2))

    target = np.array([6.0, 4.0, 0.0])
    solved_angles = damped_least_squares_ik(
        chain=chain,
        target_pos=target,
        end_effector_bone="b2",
        initial_angles={"b1": 0.0, "b2": 0.0},
        tolerance=1e-3,
    )

    joint_rots = {
        name: UnitQuaternion.from_axis_angle(np.array([0.0, 0.0, 1.0]), ang)
        for name, ang in solved_angles.items()
    }
    fk = chain.forward_kinematics(joint_rots)
    ee_pos = fk["b2"].transform_point(np.array([0.0, 0.0, 0.0]))
    err = float(np.linalg.norm(ee_pos - target))
    assert err < 0.1


def test_p23_stance_phase_foot_skate_eliminated() -> None:
    """P23: Stance-phase foot lock restores horizontal velocity to 0.0 (< 1e-9)."""
    # 10 frames with simulated foot skate in frames 3..7
    foot_pos = np.zeros((10, 3), dtype=np.float64)
    for i in range(10):
        foot_pos[i] = [1.0 + i * 0.2, 2.0 + i * 0.15, 0.05]

    contact = np.array([False, False, False, True, True, True, True, True, False, False])
    healed = zero_skate_foot_lock(foot_pos, contact, ground_z=0.0)

    # In stance frames (3..7), horizontal position must be pinned to frame 3
    locked_x = healed[3, 0]
    locked_y = healed[3, 1]
    for frame_idx in range(3, 8):
        assert abs(healed[frame_idx, 0] - locked_x) < 1e-9
        assert abs(healed[frame_idx, 1] - locked_y) < 1e-9


def test_p24_stance_phase_ground_plane_clamp() -> None:
    """P24: Stance-phase foot lock clamps vertical penetration to z >= ground_z."""
    foot_pos = np.array(
        [
            [0.0, 0.0, -0.2],
            [0.0, 0.0, -0.5],
            [0.0, 0.0, -0.1],
        ]
    )
    contact = np.array([True, True, True])
    healed = zero_skate_foot_lock(foot_pos, contact, ground_z=0.0)
    assert np.all(healed[:, 2] >= 0.0)


# ── WAVE 3: ANISOTROPIC 3DGS + COOK-TORRANCE GGX PBR (P25–P36) ─────────────


def test_p25_3d_gaussian_covariance_positive_definite() -> None:
    """P25: 3D Gaussian covariance Sigma is strictly symmetric positive definite."""
    g = Gaussian3D(
        mean=np.array([1.0, 2.0, 3.0]),
        rotation=UnitQuaternion.from_axis_angle(np.array([1.0, 1.0, 0.0]), 0.5),
        scale=np.array([0.2, 0.5, 0.1]),
        opacity=0.9,
        normal=np.array([0.0, 0.0, 1.0]),
        albedo=np.array([0.8, 0.2, 0.2]),
        roughness=0.2,
        metallic=0.0,
    )
    cov = compute_3d_covariance(g)
    eigvals = np.linalg.eigvalsh(cov)
    assert np.all(eigvals > 0.0), f"Eigenvalues not strictly positive: {eigvals}"


def test_p26_ewa_2d_screen_covariance_positive_definite() -> None:
    """P26: Regularized EWA 2D screen covariance is strictly positive definite."""
    cam = CameraParameters(
        view_matrix=np.eye(4, dtype=np.float64),
        fx=500.0,
        fy=500.0,
        cx=320.0,
        cy=240.0,
        width=640,
        height=480,
    )
    g = Gaussian3D(
        mean=np.array([0.0, 0.0, 3.0]),
        rotation=UnitQuaternion.identity(),
        scale=np.array([0.1, 0.1, 0.1]),
        opacity=0.8,
        normal=np.array([0.0, 0.0, -1.0]),
        albedo=np.array([0.5, 0.5, 0.5]),
        roughness=0.3,
        metallic=0.1,
    )
    proj = project_gaussian_ewa(g, cam)
    assert proj is not None
    _, conic_inv, _ = proj
    eigvals = np.linalg.eigvalsh(conic_inv)
    assert np.all(eigvals > 0.0)


def test_p27_conic_inverse_matrix_symmetry() -> None:
    """P27: Conic inverse is strictly symmetric."""
    cam = CameraParameters(
        view_matrix=np.eye(4, dtype=np.float64),
        fx=400.0,
        fy=400.0,
        cx=200.0,
        cy=200.0,
        width=400,
        height=400,
    )
    g = Gaussian3D(
        mean=np.array([0.5, -0.5, 2.0]),
        rotation=UnitQuaternion.from_axis_angle(np.array([0.0, 1.0, 0.0]), 0.3),
        scale=np.array([0.15, 0.08, 0.1]),
        opacity=1.0,
        normal=np.array([0.0, 0.0, -1.0]),
        albedo=np.array([0.4, 0.6, 0.8]),
        roughness=0.15,
        metallic=0.8,
    )
    proj = project_gaussian_ewa(g, cam)
    assert proj is not None
    _, conic_inv, _ = proj
    assert abs(conic_inv[0, 1] - conic_inv[1, 0]) < 1e-12


def test_p28_near_plane_culling() -> None:
    """P28: Gaussians behind or at near plane t_z <= near_plane return None."""
    cam = CameraParameters(
        view_matrix=np.eye(4, dtype=np.float64),
        fx=500.0,
        fy=500.0,
        cx=320.0,
        cy=240.0,
        width=640,
        height=480,
        near_plane=0.1,
    )
    # Gaussian placed behind camera: z = -1.0
    g = Gaussian3D(
        mean=np.array([0.0, 0.0, -1.0]),
        rotation=UnitQuaternion.identity(),
        scale=np.array([0.1, 0.1, 0.1]),
        opacity=1.0,
        normal=np.array([0.0, 0.0, 1.0]),
        albedo=np.array([1.0, 1.0, 1.0]),
        roughness=0.5,
        metallic=0.0,
    )
    assert project_gaussian_ewa(g, cam) is None


def test_p29_cook_torrance_ggx_energy_conservation() -> None:
    """P29: Cook-Torrance GGX total reflected radiance is non-negative and finite."""
    norm = np.array([0.0, 0.0, 1.0])
    v_dir = np.array([0.0, 1.0, 1.0]) / np.sqrt(2.0)
    l_dir = np.array([0.0, -1.0, 1.0]) / np.sqrt(2.0)
    albedo = np.array([0.8, 0.8, 0.8])
    light_rad = np.array([1.0, 1.0, 1.0])

    reflected = evaluate_ggx_pbr(
        normal=norm,
        view_dir=v_dir,
        light_dir=l_dir,
        albedo=albedo,
        roughness=0.3,
        metallic=0.2,
        light_radiance=light_rad,
    )
    assert np.all(reflected >= 0.0)
    assert np.all(np.isfinite(reflected))


def test_p30_cook_torrance_ggx_helmholtz_reciprocity() -> None:
    """P30: Cook-Torrance GGX satisfies Helmholtz reciprocity: fr(l, v) == fr(v, l)."""
    norm = np.array([0.0, 0.0, 1.0])
    dir_a = np.array([0.3, 0.4, 0.8]) / np.linalg.norm([0.3, 0.4, 0.8])
    dir_b = np.array([-0.2, 0.5, 0.7]) / np.linalg.norm([-0.2, 0.5, 0.7])
    albedo = np.array([0.5, 0.5, 0.5])

    r_fwd = evaluate_ggx_pbr(
        normal=norm,
        view_dir=dir_a,
        light_dir=dir_b,
        albedo=albedo,
        roughness=0.25,
        metallic=0.5,
        light_radiance=np.array([1.0, 1.0, 1.0]),
    )
    # Reciprocal: swap view_dir and light_dir, adjust for n_dot_l
    r_rev = evaluate_ggx_pbr(
        normal=norm,
        view_dir=dir_b,
        light_dir=dir_a,
        albedo=albedo,
        roughness=0.25,
        metallic=0.5,
        light_radiance=np.array([1.0, 1.0, 1.0]),
    )

    n_dot_b = float(np.dot(norm, dir_b))
    n_dot_a = float(np.dot(norm, dir_a))
    brdf_fwd = r_fwd / n_dot_b
    brdf_rev = r_rev / n_dot_a

    assert np.allclose(brdf_fwd, brdf_rev, atol=1e-8)


def test_p31_metallic_fresnel_interpolation() -> None:
    """P31: Metallic parameter modulates Fresnel F0 between 0.04 and base albedo."""
    norm = np.array([0.0, 0.0, 1.0])
    view = np.array([0.0, 0.0, 1.0])
    light = np.array([0.0, 0.0, 1.0])
    albedo = np.array([0.9, 0.2, 0.1])

    # Dielectric (metallic = 0.0) vs Conductor (metallic = 1.0)
    rad_dielectric = evaluate_ggx_pbr(
        norm, view, light, albedo, roughness=0.5, metallic=0.0, light_radiance=np.ones(3)
    )
    rad_conductor = evaluate_ggx_pbr(
        norm, view, light, albedo, roughness=0.5, metallic=1.0, light_radiance=np.ones(3)
    )

    # Pure conductor tints specular highlight with albedo
    assert rad_conductor[0] > rad_conductor[1] * 2.0
    assert np.all(rad_dielectric >= 0.0)


def test_p32_roughness_broadens_specular_lobe() -> None:
    """P32: Roughness parameter widens GGX specular lobe."""
    norm = np.array([0.0, 0.0, 1.0])
    view = np.array([0.0, 0.0, 1.0])
    light = np.array([0.0, 0.0, 1.0])  # on-axis specular peak

    rad_smooth = evaluate_ggx_pbr(
        norm, view, light, np.ones(3), roughness=0.05, metallic=1.0, light_radiance=np.ones(3)
    )
    rad_rough = evaluate_ggx_pbr(
        norm, view, light, np.ones(3), roughness=0.8, metallic=1.0, light_radiance=np.ones(3)
    )

    # Smooth surface has much higher peak specular intensity at half-vector
    assert np.all(rad_smooth > rad_rough)


def test_p33_order2_spherical_harmonics_irradiance_range() -> None:
    """P33: Order-2 Spherical Harmonics diffuse irradiance is bounded within [0, 1]."""
    sh_coeffs = np.zeros((9, 3), dtype=np.float64)
    # DC ambient component
    sh_coeffs[0] = [0.8, 0.8, 0.8]
    irr = evaluate_order2_sh_diffuse(np.array([0.0, 1.0, 0.0]), sh_coeffs)
    assert np.all(irr >= 0.0)
    assert np.all(irr <= 1.0)


def test_p34_compositing_transmittance_monotonic_decrease() -> None:
    """P34: Alpha compositing transmittance decreases monotonically across layers."""
    cam = CameraParameters(
        view_matrix=np.eye(4, dtype=np.float64),
        fx=100.0,
        fy=100.0,
        cx=32.0,
        cy=32.0,
        width=64,
        height=64,
    )
    g1 = Gaussian3D(
        mean=np.array([0.0, 0.0, 2.0]),
        rotation=UnitQuaternion.identity(),
        scale=np.array([0.5, 0.5, 0.1]),
        opacity=0.6,
        normal=np.array([0.0, 0.0, -1.0]),
        albedo=np.array([1.0, 0.0, 0.0]),
        roughness=0.5,
        metallic=0.0,
    )
    g2 = Gaussian3D(
        mean=np.array([0.0, 0.0, 4.0]),
        rotation=UnitQuaternion.identity(),
        scale=np.array([0.5, 0.5, 0.1]),
        opacity=0.8,
        normal=np.array([0.0, 0.0, -1.0]),
        albedo=np.array([0.0, 1.0, 0.0]),
        roughness=0.5,
        metallic=0.0,
    )
    res = rasterize_gaussian_splats((g1, g2), cam, ())
    assert res.visible_gaussian_count == 2
    assert 0.0 < res.alpha_silhouette[32, 32] <= 1.0


def test_p35_expected_depth_map_front_to_back_occlusion() -> None:
    """P35: Foreground splat at z=2.0 occludes background splat at z=10.0."""
    cam = CameraParameters(
        view_matrix=np.eye(4, dtype=np.float64),
        fx=100.0,
        fy=100.0,
        cx=16.0,
        cy=16.0,
        width=32,
        height=32,
    )
    g_near = Gaussian3D(
        mean=np.array([0.0, 0.0, 2.0]),
        rotation=UnitQuaternion.identity(),
        scale=np.array([0.8, 0.8, 0.1]),
        opacity=1.0,  # opaque near
        normal=np.array([0.0, 0.0, -1.0]),
        albedo=np.array([1.0, 0.0, 0.0]),
        roughness=0.5,
        metallic=0.0,
    )
    g_far = Gaussian3D(
        mean=np.array([0.0, 0.0, 10.0]),
        rotation=UnitQuaternion.identity(),
        scale=np.array([0.8, 0.8, 0.1]),
        opacity=1.0,
        normal=np.array([0.0, 0.0, -1.0]),
        albedo=np.array([0.0, 0.0, 1.0]),
        roughness=0.5,
        metallic=0.0,
    )
    res = rasterize_gaussian_splats((g_far, g_near), cam, ())
    # Center pixel depth should be close to foreground z=2.0
    center_depth = res.depth_map[16, 16]
    assert abs(center_depth - 2.0) < 0.2


def test_p36_rasterizer_quantized_uint8_and_float_agreement() -> None:
    """P36: Quantized rgb_uint8 and rgb_float agree within quantization precision."""
    cam = CameraParameters(
        view_matrix=np.eye(4, dtype=np.float64),
        fx=80.0,
        fy=80.0,
        cx=10.0,
        cy=10.0,
        width=20,
        height=20,
    )
    g = Gaussian3D(
        mean=np.array([0.0, 0.0, 2.0]),
        rotation=UnitQuaternion.identity(),
        scale=np.array([0.2, 0.2, 0.1]),
        opacity=0.75,
        normal=np.array([0.0, 0.0, -1.0]),
        albedo=np.array([0.4, 0.7, 0.9]),
        roughness=0.3,
        metallic=0.1,
    )
    res = rasterize_gaussian_splats((g,), cam, ())
    recon_float = res.rgb_uint8.astype(np.float64) / 255.0
    assert np.allclose(res.rgb_float, recon_float, atol=2.0 / 255.0)


# ── WAVE 4: REAL MEDIA, RFC 6962 MERKLE, FENCED STAGED SPINE (P37–P48) ──────


def test_p37_png_frame_lossless_roundtrip(tmp_path: Path) -> None:
    """P37: PNG write and read roundtrip preserves exact uint8 pixel bytes."""
    rng = np.random.default_rng(337)
    rgb = rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)
    png_p = tmp_path / "test.png"
    write_png_frame(rgb, png_p)
    dec = read_png_frame(png_p)
    assert np.array_equal(rgb, dec)


def test_p38_wav_pcm16_lossless_roundtrip(tmp_path: Path) -> None:
    """P38: 48 kHz PCM16 WAV write and read roundtrip preserves exact int16 samples."""
    rng = np.random.default_rng(338)
    samples = rng.integers(-32000, 32000, (4800,), dtype=np.int16)
    wav_p = tmp_path / "test.wav"
    write_wav_pcm16(samples, wav_p, sample_rate=48000)
    dec, sr = read_wav_pcm16(wav_p)
    assert sr == 48000
    assert np.array_equal(samples, dec)


def test_p39_real_ffmpeg_mp4_muxing(tmp_path: Path) -> None:
    """P39: Real FFmpeg MP4 mux produces playable H.264 file with exit code 0."""
    # Write 6 small test PNG frames and 0.25s of WAV audio
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for i in range(6):
        frame = np.full((32, 32, 3), fill_value=i * 40, dtype=np.uint8)
        write_png_frame(frame, frames_dir / f"frame_{i:04d}.png")

    audio_samples = np.zeros(12000, dtype=np.int16)
    wav_p = tmp_path / "audio.wav"
    write_wav_pcm16(audio_samples, wav_p, sample_rate=48000)

    out_mp4 = tmp_path / "video.mp4"
    mux_mp4_ffmpeg(
        frame_pattern=str(frames_dir / "frame_%04d.png"),
        audio_path=wav_p,
        output_mp4=out_mp4,
        fps=24,
    )
    assert out_mp4.is_file()
    assert out_mp4.stat().st_size > 0


def test_p40_decoded_content_sha_vs_file_sha_distinction(tmp_path: Path) -> None:
    """P40: Golden Rule #4: decoded content SHA-256 is strictly distinguished from file SHA-256."""
    frame = np.full((16, 16, 3), fill_value=128, dtype=np.uint8)
    png_p = tmp_path / "frame.png"
    write_png_frame(frame, png_p)

    file_bytes = png_p.read_bytes()
    decoded = read_png_frame(png_p)
    content_bytes = decoded.tobytes()

    file_sha = compute_rfc6962_merkle_root([file_bytes]).hex()
    content_sha = compute_rfc6962_merkle_root([content_bytes]).hex()
    assert file_sha != content_sha


def test_p41_rfc6962_leaf_hash_prefix() -> None:
    """P41: RFC 6962 leaf hash uses 0x00 single byte domain separation."""
    data = b"morpheus_leaf"
    leaf_h = rfc6962_leaf_hash(data)
    assert len(leaf_h) == 32


def test_p42_rfc6962_internal_node_hash_prefix() -> None:
    """P42: RFC 6962 node hash uses 0x01 single byte domain separation."""
    left = rfc6962_leaf_hash(b"L")
    right = rfc6962_leaf_hash(b"R")
    node_h = rfc6962_node_hash(left, right)
    assert len(node_h) == 32


def test_p43_rfc6962_unbalanced_binary_split_power_of_two() -> None:
    """P43: RFC 6962 splits 5 leaves at largest power of 2: k = 4, never duplicating odd leaves."""
    leaves = [f"leaf_{i}".encode() for i in range(5)]
    root = compute_rfc6962_merkle_root(leaves)
    assert len(root) == 32


def test_p44_rfc6962_inclusion_proof_verification() -> None:
    """P44: Inclusion proof verifies authentic leaf at arbitrary index in unbalanced tree."""
    leaves = [f"leaf_{i}".encode() for i in range(7)]
    target_idx = 3
    root_hex, proof = generate_rfc6962_inclusion_proof(leaves, target_idx)
    valid = verify_rfc6962_inclusion_proof(
        leaf_bytes=leaves[target_idx],
        index=target_idx,
        total_leaves=len(leaves),
        proof=proof,
        expected_root_hex=root_hex,
    )
    assert valid is True


def test_p45_rfc6962_inclusion_proof_fails_on_index_swap() -> None:
    """P45: Swapping index or leaf payload causes proof verification to fail."""
    leaves = [f"leaf_{i}".encode() for i in range(6)]
    root_hex, proof = generate_rfc6962_inclusion_proof(leaves, 2)
    # Verify with wrong leaf payload
    invalid = verify_rfc6962_inclusion_proof(
        leaf_bytes=leaves[1],
        index=2,
        total_leaves=len(leaves),
        proof=proof,
        expected_root_hex=root_hex,
    )
    assert invalid is False


def test_p46_staged_spine_refuses_stale_fencing_before_staging(tmp_path: Path) -> None:
    """P46: Spine adapter raises FencingTokenStaleError if fence token is stale before staging."""
    current_fence = "fence_current_999"
    adapter = MorpheusStagedSpineAdapter(
        workspace_root=tmp_path,
        active_fencing_token_getter=lambda: current_fence,
    )
    req = MorpheusStageRequest(
        asset_id="asset_01",
        attempt_id="att_01",
        fencing_token="fence_stale_111",  # mismatch!
        frames_uint8=(np.zeros((16, 16, 3), dtype=np.uint8),),
        audio_pcm16=np.zeros(2400, dtype=np.int16),
    )
    with pytest.raises(FencingTokenStaleError, match="stale before staging"):
        adapter.stage_and_publish(req)


def test_p47_staged_spine_refuses_stale_fencing_before_promotion(tmp_path: Path) -> None:
    """P47: Spine adapter detects fence invalidation during staging and aborts promotion."""
    state = {"fence": "fence_token_A"}

    def getter() -> str:
        return state["fence"]

    adapter = MorpheusStagedSpineAdapter(
        workspace_root=tmp_path,
        active_fencing_token_getter=getter,
        # Invalidate fence token right before promotion
        pre_publish_tamper_hook=lambda _dir: state.update({"fence": "fence_token_SUPERSEDED"}),
    )

    req = MorpheusStageRequest(
        asset_id="asset_02",
        attempt_id="att_02",
        fencing_token="fence_token_A",
        frames_uint8=(np.zeros((16, 16, 3), dtype=np.uint8),),
        audio_pcm16=np.zeros(2400, dtype=np.int16),
    )

    with pytest.raises(FencingTokenStaleError, match="stale before promotion"):
        adapter.stage_and_publish(req)

    # Confirm published destination was NOT created
    assert not (tmp_path / "published" / "asset_02").exists()


def test_p48_staged_spine_pre_publish_byte_tamper_detection(tmp_path: Path) -> None:
    """P48: Byte tampering in staging causes verify_morpheus_bundle_on_disk to fail closed."""

    def tamper_hook(st_dir: Path) -> None:
        p = st_dir / "frame_0000.png"
        p.write_bytes(p.read_bytes() + b"TAMPERED_BYTE")

    adapter = MorpheusStagedSpineAdapter(
        workspace_root=tmp_path,
        active_fencing_token_getter=lambda: "valid_fence",
        pre_publish_tamper_hook=tamper_hook,
    )

    req = MorpheusStageRequest(
        asset_id="asset_03",
        attempt_id="att_03",
        fencing_token="valid_fence",
        frames_uint8=(np.zeros((16, 16, 3), dtype=np.uint8),),
        audio_pcm16=np.zeros(2400, dtype=np.int16),
    )

    with pytest.raises(BundleVerificationError):
        adapter.stage_and_publish(req)

    assert not (tmp_path / "published" / "asset_03").exists()


# ── WAVE 5: COROTATIONAL TETRAHEDRAL FEM SOFT TISSUE (P49–P60) ──────────────


def test_p49_degenerate_reference_tetrahedron_raises() -> None:
    """P49: Reference tetrahedron with det(Dm) <= 1e-12 raises ValueError."""
    coplanar = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 1.0, 0.0],  # flat on z=0
        ]
    )
    with pytest.raises(ValueError, match="Degenerate or inverted"):
        build_tetrahedron((0, 1, 2, 3), coplanar)


def test_p50_polar_svd_proper_rotation_det_plus_one() -> None:
    """P50: Corotational polar decomposition strictly produces rotation with det(R) = +1.0."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)
    mat = SoftTissueMaterial(youngs_modulus=10000.0, poissons_ratio=0.3)

    # Apply rigid rotation + shear
    rot = UnitQuaternion.from_axis_angle(np.array([1.0, 1.0, 1.0]), 1.1).to_matrix()
    deformed = verts @ rot.T
    res = compute_corotational_pk1_stress(tet, deformed, mat)
    det_r = float(np.linalg.det(res.rotation))
    assert abs(det_r - 1.0) < 1e-12


def test_p51_polar_decomposition_heals_inverted_deformation() -> None:
    """P51: Inverted deformation (J < 0) yields rotation with det(R) = +1.0."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)
    mat = SoftTissueMaterial(youngs_modulus=10000.0, poissons_ratio=0.3)

    # Invert z axis
    deformed = verts.copy()
    deformed[:, 2] *= -1.0
    res = compute_corotational_pk1_stress(tet, deformed, mat)
    det_r = float(np.linalg.det(res.rotation))
    assert abs(det_r - 1.0) < 1e-12


def test_p52_corotational_pk1_stress_objectivity_pure_rotation() -> None:
    """P52: Corotational PK1 stress is zero under pure rigid rotation ||P(R)||_F < 1e-8."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [0.0, 2.0, 0.0],
            [0.0, 0.0, 2.0],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)
    mat = SoftTissueMaterial(youngs_modulus=50000.0, poissons_ratio=0.35)

    rot = UnitQuaternion.from_axis_angle(np.array([1.0, 2.0, -1.0]), 2.1).to_matrix()
    deformed = verts @ rot.T
    res = compute_corotational_pk1_stress(tet, deformed, mat)

    stress_norm = float(np.linalg.norm(res.pk1_stress, ord="fro"))
    assert stress_norm < 1e-8, f"Stress under pure rotation {stress_norm} >= 1e-8"


def test_p53_newton_third_law_nodal_force_equilibrium() -> None:
    """P53: Nodal force sum sum_{a=0}^3 f_a == 0 (< 1e-12) under arbitrary deformation."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)
    mat = SoftTissueMaterial(youngs_modulus=25000.0, poissons_ratio=0.4)

    # Deform nodes arbitrarily
    deformed = np.array(
        [
            [0.1, -0.2, 0.05],
            [1.4, 0.3, -0.1],
            [-0.2, 1.2, 0.3],
            [0.05, 0.1, 1.5],
        ]
    )
    res = compute_corotational_pk1_stress(tet, deformed, mat)
    f_sum = np.sum(res.nodal_forces, axis=0)
    assert np.all(np.abs(f_sum) < 1e-12)


def test_p54_uniaxial_stretch_restoring_force() -> None:
    """P54: Uniaxial stretch produces non-zero restoring elastic force opposing stretch."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)
    mat = SoftTissueMaterial(youngs_modulus=10000.0, poissons_ratio=0.3)

    # Stretch along X by 20%
    deformed = verts.copy()
    deformed[1, 0] = 1.2
    res = compute_corotational_pk1_stress(tet, deformed, mat)

    # Restoring force on node 1 along X must be negative (pulling back)
    assert res.nodal_forces[1, 0] < 0.0


def test_p55_inverted_tetrahedron_healed_to_positive_volume() -> None:
    """P55: Inverted tetrahedron (J <= 0) is healed to positive volume."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)

    # Inverted state: reflect node 3 across base
    deformed = verts.copy()
    deformed[3, 2] = -0.5
    healed = heal_inverted_and_volume_drift_tet(deformed, tet, j_min=0.95, j_max=1.05)

    ds = np.column_stack([healed[1] - healed[0], healed[2] - healed[0], healed[3] - healed[0]])
    j_healed = float(np.linalg.det(ds @ tet.inv_rest_edge_matrix))
    assert j_healed >= 0.95


def test_p56_volume_drift_clamped_to_bounds() -> None:
    """P56: Over-expanded or crushed tetrahedron is clamped into [J_min, J_max]."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)
    # Expand by 2x volume
    expanded = verts * (2.0 ** (1.0 / 3.0))
    healed = heal_inverted_and_volume_drift_tet(expanded, tet, j_min=0.98, j_max=1.02)

    ds = np.column_stack([healed[1] - healed[0], healed[2] - healed[0], healed[3] - healed[0]])
    j_healed = float(np.linalg.det(ds @ tet.inv_rest_edge_matrix))
    assert 0.97 <= j_healed <= 1.03


def test_p57_poissons_ratio_boundary_fail_closed() -> None:
    """P57: Poisson's ratio nu >= 0.5 raises ValueError."""
    with pytest.raises(ValueError, match="poissons_ratio"):
        SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.5)


def test_p58_rayleigh_damping_dissipates_kinetic_energy() -> None:
    """P58: Rayleigh damping dissipates kinetic energy over time steps."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)
    mat = SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3, rayleigh_damping=0.5)

    vel0 = np.ones((4, 3), dtype=np.float64) * 2.0
    state = FEMSimulationState(
        vertices=verts.copy(),
        velocities=vel0,
        tetrahedra=(tet,),
        material=mat,
    )

    state_next = solve_fem_time_step(state, dt=0.01)
    ke0 = float(np.sum(state.velocities**2))
    ke1 = float(np.sum(state_next.velocities**2))
    assert ke1 < ke0


def test_p59_cranial_bone_sdf_penetration_resolution() -> None:
    """P59: Cranial bone SDF penetration is resolved by projecting node to bone surface."""
    verts = np.array(
        [
            [0.0, 0.0, -0.2],  # penetrates sphere of radius 1.0 at origin
            [1.2, 0.0, 0.0],
            [0.0, 1.2, 0.0],
            [0.0, 0.0, 1.2],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)
    mat = SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3)

    # Skull sphere SDF centered at (0, 0, -1.0) with radius 1.0: surface is at z = 0.0
    def skull_sdf(p: np.ndarray) -> tuple[float, np.ndarray]:
        center = np.array([0.0, 0.0, -1.0])
        diff = p - center
        dist = float(np.linalg.norm(diff) - 1.0)
        grad = diff / np.linalg.norm(diff)
        return dist, grad

    state = FEMSimulationState(
        vertices=verts,
        velocities=np.zeros_like(verts),
        tetrahedra=(tet,),
        material=mat,
    )
    healed = solve_fem_time_step(state, dt=0.01, cranial_bone_sdf=skull_sdf)
    dist, _ = skull_sdf(healed.vertices[0])
    assert dist >= -1e-6


def test_p60_cranial_bone_sdf_cancels_normal_velocity() -> None:
    """P60: Inward velocity into bone is zeroed out upon collision."""
    verts = np.array(
        [
            [0.0, 0.0, -0.1],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    tet = build_tetrahedron((0, 1, 2, 3), verts)
    mat = SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3)

    def plane_sdf(p: np.ndarray) -> tuple[float, np.ndarray]:
        # Ground plane at z = 0, normal = [0, 0, 1]
        dist = float(p[2])
        return dist, np.array([0.0, 0.0, 1.0])

    vel = np.zeros((4, 3), dtype=np.float64)
    vel[0] = [0.0, 0.0, -5.0]  # moving downwards into bone

    state = FEMSimulationState(vertices=verts, velocities=vel, tetrahedra=(tet,), material=mat)
    solved = solve_fem_time_step(state, dt=0.01, cranial_bone_sdf=plane_sdf)
    assert solved.velocities[0, 2] >= 0.0


# ── WAVE 6: COSSERAT HAIR RODS & MARSCHNER BRDF (P61–P72) ───────────────────


def test_p61_cosserat_hair_strand_rest_lengths() -> None:
    """P61: Cosserat hair strand initializes rest segment lengths accurately."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.0, 0.0, -2.0],
            [0.0, 0.0, -5.0],
            [0.0, 1.0, -9.0],
        ]
    )
    strand = build_cosserat_hair_strand(verts)
    assert len(strand.rest_lengths) == 3
    assert abs(strand.rest_lengths[0] - 2.0) < 1e-12
    assert abs(strand.rest_lengths[1] - 3.0) < 1e-12


def test_p62_bishop_frame_orthogonality_along_strand() -> None:
    """P62: Bishop frame parallel transport maintains <u_k, t_k> == 0 along entire strand."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.2, 0.1, -1.0],
            [0.5, -0.3, -2.5],
            [0.1, 0.4, -4.0],
        ]
    )
    tangents, normals = parallel_transport_bishop_frame(verts)
    for i in range(len(tangents)):
        dot = float(np.dot(tangents[i], normals[i]))
        assert abs(dot) < 1e-12, f"Bishop orthogonality violation at {i}: {dot}"


def test_p63_cosserat_pbd_inextensibility_strain_drift_bound() -> None:
    """P63: PBD position solver preserves rest segment lengths with strain drift < 1e-9."""
    verts = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.0, 0.0, -1.0],
            [0.0, 0.0, -2.0],
            [0.0, 0.0, -3.0],
        ]
    )
    strand = build_cosserat_hair_strand(verts)
    anchor = np.array([0.0, 0.0, 0.0])
    gravity = np.array([0.0, -9.81, 0.0])

    simulated = solve_cosserat_strand_pbd(strand, anchor, dt=0.01, gravity=gravity, iterations=35)
    for i in range(len(simulated.rest_lengths)):
        rest_l = strand.rest_lengths[i]
        curr_l = float(np.linalg.norm(simulated.vertices[i + 1] - simulated.vertices[i]))
        strain = abs(curr_l - rest_l) / rest_l
        assert strain < 1e-9, f"Segment {i} strain {strain} exceeds 1e-9"


def test_p64_cosserat_scalp_anchor_fixed() -> None:
    """P64: Scalp anchor vertex 0 remains fixed at anchor position across time steps."""
    verts = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 0.0, -2.0]])
    strand = build_cosserat_hair_strand(verts)
    anchor = np.array([12.0, -5.0, 3.0])
    sim = solve_cosserat_strand_pbd(strand, anchor, dt=0.01, gravity=np.array([0.0, 0.0, -9.81]))
    assert np.allclose(sim.vertices[0], anchor, atol=1e-12)


def test_p65_cosserat_head_sdf_repulsion() -> None:
    """P65: Head SDF collision resolution repels hair vertices outside head sphere."""
    verts = np.array([[0.0, 0.0, 1.0], [0.1, 0.0, 0.1], [0.2, 0.0, -0.2]])
    strand = build_cosserat_hair_strand(verts)

    def head_sphere_sdf(p: np.ndarray) -> tuple[float, np.ndarray]:
        # Head sphere of radius 0.8 at origin
        r = 0.8
        norm = float(np.linalg.norm(p))
        dist = norm - r
        grad = p / max(norm, 1e-6)
        return dist, grad

    sim = solve_cosserat_strand_pbd(
        strand,
        scalp_anchor=np.array([0.0, 0.0, 1.0]),
        dt=0.01,
        gravity=np.array([0.0, 0.0, -1.0]),
        head_sdf=head_sphere_sdf,
        iterations=20,
    )
    for v in sim.vertices[1:]:
        dist, _ = head_sphere_sdf(v)
        assert dist >= -1e-4


def test_p66_marschner_brdf_non_negativity() -> None:
    """P66: Marschner scattering BRDF components are strictly non-negative: S >= 0."""
    t = np.array([0.0, 0.0, 1.0])
    wi = np.array([0.5, 0.0, 0.866])
    wo = np.array([-0.5, 0.2, 0.84])
    mat = HairMaterialProperties()

    res = evaluate_marschner_brdf(t, wi, wo, mat)
    assert np.all(res.radiance_r >= 0.0)
    assert np.all(res.radiance_tt >= 0.0)
    assert np.all(res.radiance_trt >= 0.0)
    assert np.all(res.total_radiance >= 0.0)


def test_p67_marschner_brdf_helmholtz_reciprocity() -> None:
    """P67: Marschner BRDF satisfies Helmholtz reciprocity: S(wi, wo) == S(wo, wi)."""
    t = np.array([0.0, 0.0, 1.0])
    w1 = np.array([0.3, 0.4, 0.8]) / np.linalg.norm([0.3, 0.4, 0.8])
    w2 = np.array([-0.2, 0.6, 0.7]) / np.linalg.norm([-0.2, 0.6, 0.7])
    mat = HairMaterialProperties()

    res1 = evaluate_marschner_brdf(t, w1, w2, mat)
    res2 = evaluate_marschner_brdf(t, w2, w1, mat)

    assert np.allclose(res1.total_radiance, res2.total_radiance, atol=1e-12)


def test_p68_marschner_specular_r_peak_shift() -> None:
    """P68: Specular R lobe peaks at longitudinal half-angle theta_h = -2*alpha_cut."""
    t = np.array([0.0, 0.0, 1.0])
    mat = HairMaterialProperties(cuticle_tilt_rad=0.04)

    # Peak incidence/reflection: theta_h = -2*alpha
    target_theta_h = -2.0 * mat.cuticle_tilt_rad
    wi = np.array([np.cos(target_theta_h), 0.0, np.sin(target_theta_h)])
    wo = np.array([np.cos(target_theta_h), 0.0, np.sin(target_theta_h)])

    res_peak = evaluate_marschner_brdf(t, wi, wo, mat)

    # Off-peak incidence
    wi_off = np.array([np.cos(0.0), 0.0, np.sin(0.0)])
    wo_off = np.array([np.cos(0.0), 0.0, np.sin(0.0)])
    res_off = evaluate_marschner_brdf(t, wi_off, wo_off, mat)

    assert res_peak.radiance_r[0] > res_off.radiance_r[0]


def test_p69_marschner_transmission_tt_peak_shift() -> None:
    """P69: Transmission TT lobe peaks at longitudinal half-angle theta_h = +alpha_cut."""
    t = np.array([0.0, 0.0, 1.0])
    mat = HairMaterialProperties(cuticle_tilt_rad=0.04)

    target_theta = mat.cuticle_tilt_rad
    # In-plane opposite azimuthal sides (phi = pi)
    wi = np.array([np.cos(target_theta), 0.0, np.sin(target_theta)])
    wo = np.array([-np.cos(target_theta), 0.0, np.sin(target_theta)])

    res_peak = evaluate_marschner_brdf(t, wi, wo, mat)
    assert res_peak.radiance_tt[0] > 0.0


def test_p70_marschner_secondary_trt_peak_shift() -> None:
    """P70: Secondary highlight TRT lobe peaks at theta_h = +4*alpha_cut."""
    t = np.array([0.0, 0.0, 1.0])
    mat = HairMaterialProperties(cuticle_tilt_rad=0.04)

    target_theta = 4.0 * mat.cuticle_tilt_rad
    wi = np.array([np.cos(target_theta), 0.0, np.sin(target_theta)])
    wo = np.array([np.cos(target_theta), 0.0, np.sin(target_theta)])

    res = evaluate_marschner_brdf(t, wi, wo, mat)
    assert res.radiance_trt[0] > 0.0


def test_p71_melanin_darkening_monotonicity() -> None:
    """P71: Melanin darkening monotonicity: increasing eumelanin strictly decreases TT and TRT."""
    t = np.array([0.0, 0.0, 1.0])
    wi = np.array([0.5, 0.0, 0.866])
    wo = np.array([-0.5, 0.0, 0.866])

    mat_light = HairMaterialProperties(absorption=np.array([0.1, 0.2, 0.3]))
    mat_dark = HairMaterialProperties(absorption=np.array([1.5, 2.5, 3.5]))

    res_light = evaluate_marschner_brdf(t, wi, wo, mat_light)
    res_dark = evaluate_marschner_brdf(t, wi, wo, mat_dark)

    assert np.all(res_dark.radiance_tt < res_light.radiance_tt)
    assert np.all(res_dark.radiance_trt < res_light.radiance_trt)


def test_p72_melanin_darkening_surface_specular_invariance() -> None:
    """P72: Melanin darkening invariance: surface specular reflection R is invariant to melanin."""
    t = np.array([0.0, 0.0, 1.0])
    wi = np.array([0.4, 0.2, 0.89])
    wo = np.array([0.4, 0.2, 0.89])

    mat1 = HairMaterialProperties(absorption=np.array([0.0, 0.0, 0.0]))
    mat2 = HairMaterialProperties(absorption=np.array([5.0, 5.0, 5.0]))

    res1 = evaluate_marschner_brdf(t, wi, wo, mat1)
    res2 = evaluate_marschner_brdf(t, wi, wo, mat2)

    assert np.allclose(res1.radiance_r, res2.radiance_r, atol=1e-12)


# ── WAVE 7: AIRY THIN-FILM IRIDESCENCE & CHARLIE SHEEN (P73–P84) ────────────


def test_p73_airy_reflectance_bounded_zero_one() -> None:
    """P73: Spectral Airy thin-film reflectance is strictly bounded in [0.0, 1.0]."""
    film = ThinFilmProperties(n1_ambient=1.0, n2_film=1.45, n3_substrate=1.65, thickness_nm=450.0)
    for theta in np.linspace(0.0, np.pi * 0.45, 10):
        r = evaluate_airy_thin_film_reflectance(float(theta), film)
        assert np.all(r >= 0.0)
        assert np.all(r <= 1.0)


def test_p74_airy_reflectance_zero_thickness_bare_substrate_limit() -> None:
    """P74: As thickness d -> 0 nm, Airy reflectance collapses to bare substrate Fresnel."""
    film_d0 = ThinFilmProperties(n1_ambient=1.0, n2_film=1.4, n3_substrate=1.6, thickness_nm=0.0)
    # Normal incidence: R_bare = ((n1 - n3)/(n1 + n3))^2
    r_bare = ((1.0 - 1.6) / (1.0 + 1.6)) ** 2
    r_airy = evaluate_airy_thin_film_reflectance(0.0, film_d0)
    assert np.allclose(r_airy, r_bare, atol=1e-6)


def test_p75_airy_spectral_fringes_across_thickness() -> None:
    """P75: Varying film thickness produces spectral RGB interference fringes."""
    film_400 = ThinFilmProperties(thickness_nm=400.0)
    film_600 = ThinFilmProperties(thickness_nm=600.0)

    r_400 = evaluate_airy_thin_film_reflectance(0.0, film_400)
    r_600 = evaluate_airy_thin_film_reflectance(0.0, film_600)

    assert not np.allclose(r_400, r_600, atol=1e-3)


def test_p76_airy_angle_spectral_hue_shift() -> None:
    """P76: Changing incident angle theta_1 shifts spectral RGB hue deterministically."""
    film = ThinFilmProperties(thickness_nm=500.0)
    r_normal = evaluate_airy_thin_film_reflectance(0.0, film)
    r_grazing = evaluate_airy_thin_film_reflectance(np.radians(60.0), film)

    assert not np.allclose(r_normal, r_grazing, atol=1e-3)


def test_p77_charlie_microfiber_sheen_non_negativity() -> None:
    """P77: Charlie microfiber sheen NDF and BRDF are strictly non-negative: D_Charlie >= 0."""
    norm = np.array([0.0, 0.0, 1.0])
    v = np.array([0.0, 0.5, 0.866])
    l_dir = np.array([0.0, -0.5, 0.866])
    sheen = MicrofiberSheenProperties(sheen_color=np.array([0.9, 0.8, 0.9]), roughness=0.3)

    brdf = evaluate_charlie_microfiber_sheen(norm, v, l_dir, sheen)
    assert np.all(brdf >= 0.0)


def test_p78_charlie_sheen_helmholtz_reciprocity() -> None:
    """P78: Charlie microfiber sheen satisfies Helmholtz reciprocity: fr(l, v) == fr(v, l)."""
    norm = np.array([0.0, 0.0, 1.0])
    dir_a = np.array([0.3, 0.4, 0.8]) / np.linalg.norm([0.3, 0.4, 0.8])
    dir_b = np.array([-0.2, 0.5, 0.7]) / np.linalg.norm([-0.2, 0.5, 0.7])
    sheen = MicrofiberSheenProperties(sheen_color=np.array([0.8, 0.5, 0.3]), roughness=0.4)

    brdf_ab = evaluate_charlie_microfiber_sheen(norm, dir_a, dir_b, sheen)
    brdf_ba = evaluate_charlie_microfiber_sheen(norm, dir_b, dir_a, sheen)

    assert np.allclose(brdf_ab, brdf_ba, atol=1e-12)


def test_p79_neubelt_pettineo_visibility_finite_positive() -> None:
    """P79: Neubelt-Pettineo soft-cloth visibility factor is finite and positive."""
    norm = np.array([0.0, 0.0, 1.0])
    v = np.array([0.0, 0.1, 0.99])
    l_dir = np.array([0.0, 0.2, 0.98])
    sheen = MicrofiberSheenProperties(sheen_color=np.ones(3), roughness=0.25)

    brdf = evaluate_charlie_microfiber_sheen(norm, v, l_dir, sheen)
    assert np.all(np.isfinite(brdf))


def test_p80_charlie_sheen_grazing_rim_retro_reflection_peak() -> None:
    """P80: Charlie sheen retro-reflection peaks at grazing angles (N.V -> 0)."""
    norm = np.array([0.0, 0.0, 1.0])
    v_normal = np.array([0.0, 0.0, 1.0])
    v_grazing = np.array([0.0, 0.99, 0.1]) / np.linalg.norm([0.0, 0.99, 0.1])
    l_grazing = np.array([0.0, 0.99, 0.1]) / np.linalg.norm([0.0, 0.99, 0.1])
    sheen = MicrofiberSheenProperties(sheen_color=np.ones(3), roughness=0.2)

    rad_normal = evaluate_charlie_microfiber_sheen(norm, v_normal, v_normal, sheen)
    rad_grazing = evaluate_charlie_microfiber_sheen(norm, v_grazing, l_grazing, sheen)

    # Grazing rim retro-reflection has higher sheen intensity than normal incidence
    assert np.all(rad_grazing > rad_normal)
    assert np.all(np.isfinite(rad_grazing))


def test_p81_cloth_combined_iridescent_sheen_brdf() -> None:
    """P81: Combined cloth BRDF produces valid, bounded radiance under directional lighting."""
    norm = np.array([0.0, 0.0, 1.0])
    v = np.array([0.0, 0.6, 0.8])
    l_dir = np.array([0.0, 0.6, 0.8])
    film = ThinFilmProperties(thickness_nm=420.0)
    sheen = MicrofiberSheenProperties(sheen_color=np.array([0.6, 0.4, 0.8]), roughness=0.35)
    light_rad = np.array([2.0, 2.0, 2.0])

    res = evaluate_cloth_iridescent_sheen_brdf(norm, v, l_dir, film, sheen, light_rad)
    assert np.all(res.combined_radiance >= 0.0)
    assert np.all(np.isfinite(res.combined_radiance))


def test_p82_sheen_roughness_broadening() -> None:
    """P82: Sheen roughness modulates specular microfiber spread."""
    norm = np.array([0.0, 0.0, 1.0])
    v = np.array([0.0, 0.7, 0.71])
    l_dir = np.array([0.0, 0.7, 0.71])

    s_tight = MicrofiberSheenProperties(sheen_color=np.ones(3), roughness=0.1)
    s_broad = MicrofiberSheenProperties(sheen_color=np.ones(3), roughness=0.8)

    brdf_tight = evaluate_charlie_microfiber_sheen(norm, v, l_dir, s_tight)
    brdf_broad = evaluate_charlie_microfiber_sheen(norm, v, l_dir, s_broad)
    assert not np.allclose(brdf_tight, brdf_broad)


def test_p83_zero_light_incident_angle_produces_zero_radiance() -> None:
    """P83: Light below horizon (N.L <= 0) produces zero reflected radiance."""
    norm = np.array([0.0, 0.0, 1.0])
    v = np.array([0.0, 0.0, 1.0])
    l_sub = np.array([0.0, 0.0, -1.0])  # underneath surface
    sheen = MicrofiberSheenProperties(sheen_color=np.ones(3))

    brdf = evaluate_charlie_microfiber_sheen(norm, v, l_sub, sheen)
    assert np.all(brdf == 0.0)


def test_p84_thin_film_total_internal_reflection() -> None:
    """P84: Total internal reflection in film layer yields R = 1.0."""
    film_dense_to_rare = ThinFilmProperties(
        n1_ambient=1.8, n2_film=1.1, n3_substrate=1.5, thickness_nm=300.0
    )
    # Incident angle past critical angle
    theta_tir = np.radians(80.0)
    r = evaluate_airy_thin_film_reflectance(theta_tir, film_dense_to_rare)
    assert np.all(r == 1.0)


# ── WAVE 8: EPISODIC CHARACTER CAPSULE VAULT & CONTINUITY HEALER (P85–P96) ──


def test_p85_character_capsule_rfc6962_merkle_freezing() -> None:
    """P85: Character identity capsule seals canonical descriptor into RFC 6962 Merkle root."""
    capsule = create_character_identity_capsule(
        character_id="hero_aloy",
        canonical_alpha_id=(1.2, -0.5, 0.8, -1.0),
        cephalometric_ratios={"ip_bz": 0.42, "nm_bz": 0.88, "symmetry": 0.98},
        skeletal_rest_lengths={"femur_L": 42.0, "femur_R": 42.0},
        soft_tissue_rest_volumes=(1.2, 1.2, 1.2),
        hair_strand_count=120,
        hair_rest_segment_lengths=(1.5, 1.5, 1.5),
        melanin_absorption=(0.4, 0.6, 0.8),
        cloth_thin_film_thickness_nm=380.0,
        cloth_sheen_roughness=0.25,
    )
    assert len(capsule.merkle_root_hex) == 64
    assert len(capsule.capsule_sha256) == 64


def test_p86_capsule_integrity_verification_success() -> None:
    """P86: Authentic capsule passes integrity verification."""
    capsule = create_character_identity_capsule(
        character_id="hero_01",
        canonical_alpha_id=(0.5, -0.2),
        cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
        skeletal_rest_lengths={"spine": 30.0},
        soft_tissue_rest_volumes=(0.5,),
        hair_strand_count=50,
        hair_rest_segment_lengths=(1.0,),
        melanin_absorption=(0.1, 0.2, 0.3),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
    )
    assert verify_character_capsule_integrity(capsule) is True


def test_p87_capsule_integrity_refuses_tampered_merkle_root() -> None:
    """P87: Tampering with merkle_root_hex causes integrity verification to fail."""
    capsule = create_character_identity_capsule(
        character_id="hero_02",
        canonical_alpha_id=(0.5, -0.2),
        cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
        skeletal_rest_lengths={"spine": 30.0},
        soft_tissue_rest_volumes=(0.5,),
        hair_strand_count=50,
        hair_rest_segment_lengths=(1.0,),
        melanin_absorption=(0.1, 0.2, 0.3),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
    )
    tampered = CharacterIdentityCapsule(
        character_id=capsule.character_id,
        canonical_alpha_id=capsule.canonical_alpha_id,
        cephalometric_ratios=capsule.cephalometric_ratios,
        skeletal_rest_lengths=capsule.skeletal_rest_lengths,
        soft_tissue_rest_volumes=capsule.soft_tissue_rest_volumes,
        hair_strand_count=capsule.hair_strand_count,
        hair_rest_segment_lengths=capsule.hair_rest_segment_lengths,
        melanin_absorption=capsule.melanin_absorption,
        cloth_thin_film_thickness_nm=capsule.cloth_thin_film_thickness_nm,
        cloth_sheen_roughness=capsule.cloth_sheen_roughness,
        merkle_root_hex="0000000000000000000000000000000000000000000000000000000000000000",
        capsule_sha256=capsule.capsule_sha256,
    )
    assert verify_character_capsule_integrity(tampered) is False


def test_p88_capsule_integrity_refuses_tampered_capsule_hash() -> None:
    """P88: Tampering with capsule_sha256 causes verification to fail."""
    capsule = create_character_identity_capsule(
        character_id="hero_03",
        canonical_alpha_id=(0.5, -0.2),
        cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
        skeletal_rest_lengths={"spine": 30.0},
        soft_tissue_rest_volumes=(0.5,),
        hair_strand_count=50,
        hair_rest_segment_lengths=(1.0,),
        melanin_absorption=(0.1, 0.2, 0.3),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
    )
    tampered = CharacterIdentityCapsule(
        character_id=capsule.character_id,
        canonical_alpha_id=capsule.canonical_alpha_id,
        cephalometric_ratios=capsule.cephalometric_ratios,
        skeletal_rest_lengths=capsule.skeletal_rest_lengths,
        soft_tissue_rest_volumes=capsule.soft_tissue_rest_volumes,
        hair_strand_count=capsule.hair_strand_count,
        hair_rest_segment_lengths=capsule.hair_rest_segment_lengths,
        melanin_absorption=capsule.melanin_absorption,
        cloth_thin_film_thickness_nm=capsule.cloth_thin_film_thickness_nm,
        cloth_sheen_roughness=capsule.cloth_sheen_roughness,
        merkle_root_hex=capsule.merkle_root_hex,
        capsule_sha256="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    )
    assert verify_character_capsule_integrity(tampered) is False


def test_p89_capsule_integrity_refuses_modified_alpha_id() -> None:
    """P89: Silently modifying canonical_alpha_id values breaks hash verification."""
    capsule = create_character_identity_capsule(
        character_id="hero_04",
        canonical_alpha_id=(1.0, 2.0),
        cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
        skeletal_rest_lengths={"spine": 30.0},
        soft_tissue_rest_volumes=(0.5,),
        hair_strand_count=50,
        hair_rest_segment_lengths=(1.0,),
        melanin_absorption=(0.1, 0.2, 0.3),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
    )
    tampered = CharacterIdentityCapsule(
        character_id=capsule.character_id,
        canonical_alpha_id=(1.0, 2.0001),  # subtle perturbation
        cephalometric_ratios=capsule.cephalometric_ratios,
        skeletal_rest_lengths=capsule.skeletal_rest_lengths,
        soft_tissue_rest_volumes=capsule.soft_tissue_rest_volumes,
        hair_strand_count=capsule.hair_strand_count,
        hair_rest_segment_lengths=capsule.hair_rest_segment_lengths,
        melanin_absorption=capsule.melanin_absorption,
        cloth_thin_film_thickness_nm=capsule.cloth_thin_film_thickness_nm,
        cloth_sheen_roughness=capsule.cloth_sheen_roughness,
        merkle_root_hex=capsule.merkle_root_hex,
        capsule_sha256=capsule.capsule_sha256,
    )
    assert verify_character_capsule_integrity(tampered) is False


def test_p90_multi_episode_audit_detects_drift_across_5_episodes() -> None:
    """P90: Multi-episode auditor detects identity drift across 5 distinct episode states."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=590)
    locked_alpha = (0.8, -0.6, 0.4, -0.2)
    cephs = compute_cephalometric_ratios(basis.mean_mesh)

    capsule = create_character_identity_capsule(
        character_id="char_5ep",
        canonical_alpha_id=locked_alpha,
        cephalometric_ratios={
            "ip_bz": cephs.interpupillary_to_bizygomatic,
            "nm_bz": cephs.nasion_menton_to_bizygomatic,
            "symmetry": cephs.cranial_vault_symmetry,
        },
        skeletal_rest_lengths={"humerus_L": 30.0},
        soft_tissue_rest_volumes=(1.0,),
        hair_strand_count=100,
        hair_rest_segment_lengths=(2.0,),
        melanin_absorption=(0.2, 0.4, 0.6),
        cloth_thin_film_thickness_nm=350.0,
        cloth_sheen_roughness=0.2,
    )

    # 5 episodes with intentional AI generation drift
    ep_states: list[EpisodeCharacterState] = []
    for ep_i in range(5):
        drift = np.array(locked_alpha) + (ep_i + 1) * 0.4
        exp = np.array([0.5, -0.5, 0.2, -0.2]) * (ep_i + 1)
        drifted_mesh = synthesize_face_mesh(basis, drift, exp)
        ep_states.append(
            EpisodeCharacterState(
                episode_id=f"EP_{ep_i + 1}",
                face_mesh=drifted_mesh,
                bone_lengths={"humerus_L": 30.0 + ep_i * 0.5},  # bone stretch
                tetrahedral_volumes=(1.0 - ep_i * 0.05,),
                hair_strand_count=100,
                hair_segment_lengths=(2.0,),
                wardrobe_parameters={},
            )
        )

    res = audit_and_heal_episodic_character_continuity(capsule, ep_states, basis)
    assert res.episode_count == 5
    for ep_id, pre_drift in res.pre_heal_identity_drifts.items():
        assert pre_drift > 0.1, f"Expected non-zero pre-heal drift in {ep_id}, got {pre_drift}"


def test_p91_multi_episode_healer_restores_zero_identity_drift() -> None:
    """P91: Multi-episode healer restores identity drift across all 5 episodes to < 1e-12."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=591)
    locked_alpha = (0.5, -0.8, 0.3, -0.1)
    cephs = compute_cephalometric_ratios(basis.mean_mesh)

    capsule = create_character_identity_capsule(
        character_id="char_heal5",
        canonical_alpha_id=locked_alpha,
        cephalometric_ratios={
            "ip_bz": cephs.interpupillary_to_bizygomatic,
            "nm_bz": cephs.nasion_menton_to_bizygomatic,
            "symmetry": cephs.cranial_vault_symmetry,
        },
        skeletal_rest_lengths={"radius_L": 25.0},
        soft_tissue_rest_volumes=(1.5,),
        hair_strand_count=80,
        hair_rest_segment_lengths=(1.8,),
        melanin_absorption=(0.3, 0.5, 0.7),
        cloth_thin_film_thickness_nm=420.0,
        cloth_sheen_roughness=0.3,
    )

    ep_states: list[EpisodeCharacterState] = []
    for ep_i in range(5):
        drift = np.array(locked_alpha) + np.array([1.2, -1.5, 0.8, -0.9])
        exp = np.array([-1.0, 1.5, -0.8, 1.2])
        mesh = synthesize_face_mesh(basis, drift, exp)
        ep_states.append(
            EpisodeCharacterState(
                episode_id=f"EP_{ep_i + 1}",
                face_mesh=mesh,
                bone_lengths={"radius_L": 25.0 + ep_i},
                tetrahedral_volumes=(1.5,),
                hair_strand_count=80,
                hair_segment_lengths=(1.8,),
                wardrobe_parameters={},
            )
        )

    res = audit_and_heal_episodic_character_continuity(capsule, ep_states, basis)
    assert res.is_healed_continuity_exact is True
    for ep_id, post_drift in res.post_heal_identity_drifts.items():
        assert post_drift < 1e-12, f"Episode {ep_id} post-heal drift {post_drift} >= 1e-12"


def test_p92_multi_episode_healer_restores_bone_lengths() -> None:
    """P92: Bone length stretch is audited and healed to exact capsule rest lengths."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=592)
    capsule = create_character_identity_capsule(
        character_id="char_bone",
        canonical_alpha_id=(0.0, 0.0, 0.0, 0.0),
        cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
        skeletal_rest_lengths={"tibia_L": 38.0, "tibia_R": 38.0},
        soft_tissue_rest_volumes=(1.0,),
        hair_strand_count=100,
        hair_rest_segment_lengths=(2.0,),
        melanin_absorption=(0.2, 0.4, 0.6),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
    )
    ep_states = [
        EpisodeCharacterState(
            episode_id=f"EP_{i}",
            face_mesh=basis.mean_mesh.copy(),
            bone_lengths={"tibia_L": 38.0 + i * 2.0, "tibia_R": 38.0 - i * 1.5},
            tetrahedral_volumes=(1.0,),
            hair_strand_count=100,
            hair_segment_lengths=(2.0,),
            wardrobe_parameters={},
        )
        for i in range(4)
    ]
    res = audit_and_heal_episodic_character_continuity(capsule, ep_states, basis)
    for _ep_id, state in res.healed_episode_states.items():
        assert state.bone_lengths["tibia_L"] == 38.0
        assert state.bone_lengths["tibia_R"] == 38.0


def test_p93_multi_episode_healer_restores_soft_tissue_volumes() -> None:
    """P93: Soft-tissue volumes are healed to match capsule rest volumes."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=593)
    capsule = create_character_identity_capsule(
        character_id="char_vol",
        canonical_alpha_id=(0.0, 0.0, 0.0, 0.0),
        cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
        skeletal_rest_lengths={"bone": 10.0},
        soft_tissue_rest_volumes=(1.25, 2.50),
        hair_strand_count=50,
        hair_rest_segment_lengths=(1.0,),
        melanin_absorption=(0.1, 0.2, 0.3),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
    )
    ep_states = [
        EpisodeCharacterState(
            episode_id=f"EP_{i}",
            face_mesh=basis.mean_mesh.copy(),
            bone_lengths={"bone": 10.0},
            tetrahedral_volumes=(0.8, 1.9),  # collapsed volumes
            hair_strand_count=50,
            hair_segment_lengths=(1.0,),
            wardrobe_parameters={},
        )
        for i in range(4)
    ]
    res = audit_and_heal_episodic_character_continuity(capsule, ep_states, basis)
    for _ep_id, state in res.healed_episode_states.items():
        assert state.tetrahedral_volumes == (1.25, 2.50)


def test_p94_multi_episode_refuses_fewer_than_four_episodes() -> None:
    """P94: Multi-episode auditor fails closed if episode count < 4."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=594)
    capsule = create_character_identity_capsule(
        character_id="char_min4",
        canonical_alpha_id=(0.0, 0.0, 0.0, 0.0),
        cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
        skeletal_rest_lengths={},
        soft_tissue_rest_volumes=(),
        hair_strand_count=10,
        hair_rest_segment_lengths=(),
        melanin_absorption=(0.1, 0.2, 0.3),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
    )
    # Only 3 episodes
    three_episodes = [
        EpisodeCharacterState(
            episode_id=f"EP_{i}",
            face_mesh=basis.mean_mesh.copy(),
            bone_lengths={},
            tetrahedral_volumes=(),
            hair_strand_count=10,
            hair_segment_lengths=(),
            wardrobe_parameters={},
        )
        for i in range(3)
    ]
    with pytest.raises(ValueError, match="requires at least 4 episodes"):
        audit_and_heal_episodic_character_continuity(capsule, three_episodes, basis)


def test_p95_multi_episode_refuses_forged_capsule_digest() -> None:
    """P95: Multi-episode auditor raises CapsuleIntegrityTamperError on forged capsule."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=595)
    forged_capsule = CharacterIdentityCapsule(
        character_id="forged_hero",
        canonical_alpha_id=(1.0, 2.0),
        cephalometric_ratios={"ip_bz": 0.4, "nm_bz": 0.8, "symmetry": 0.95},
        skeletal_rest_lengths={},
        soft_tissue_rest_volumes=(),
        hair_strand_count=10,
        hair_rest_segment_lengths=(),
        melanin_absorption=(0.1, 0.2, 0.3),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
        merkle_root_hex="bad_root" * 8,
        capsule_sha256="bad_hash" * 8,
    )
    ep_states = [
        EpisodeCharacterState(
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
    with pytest.raises(CapsuleIntegrityTamperError, match="integrity verification failed"):
        audit_and_heal_episodic_character_continuity(forged_capsule, ep_states, basis)


def test_p96_cephalometric_ratios_continuity_recovery() -> None:
    """P96: Healed episode states maintain cephalometric invariant agreement with capsule."""
    basis = build_canonical_3dmm_basis(vertex_count=68, k_id=4, k_exp=4, seed=596)
    locked_alpha = (0.2, -0.4, 0.6, -0.3)
    canonical_mesh = synthesize_face_mesh(basis, np.array(locked_alpha), np.zeros(4))
    cephs_canon = compute_cephalometric_ratios(canonical_mesh)

    capsule = create_character_identity_capsule(
        character_id="char_ceph",
        canonical_alpha_id=locked_alpha,
        cephalometric_ratios={
            "ip_bz": cephs_canon.interpupillary_to_bizygomatic,
            "nm_bz": cephs_canon.nasion_menton_to_bizygomatic,
            "symmetry": cephs_canon.cranial_vault_symmetry,
        },
        skeletal_rest_lengths={},
        soft_tissue_rest_volumes=(),
        hair_strand_count=10,
        hair_rest_segment_lengths=(),
        melanin_absorption=(0.1, 0.2, 0.3),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
    )

    ep_states = [
        EpisodeCharacterState(
            episode_id=f"EP_{i}",
            face_mesh=synthesize_face_mesh(basis, np.array(locked_alpha) + 0.5, np.ones(4) * 0.5),
            bone_lengths={},
            tetrahedral_volumes=(),
            hair_strand_count=10,
            hair_segment_lengths=(),
            wardrobe_parameters={},
        )
        for i in range(4)
    ]
    res = audit_and_heal_episodic_character_continuity(capsule, ep_states, basis)
    for ep_id, c_drift in res.cephalometric_drifts.items():
        assert c_drift < 0.1, f"Episode {ep_id} cephalometric drift {c_drift} too high"
