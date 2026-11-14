"""Coverage completion probes targeting defensive validations and edge branches.

Ensures strict >= 95% line coverage across all 8 Morpheus architectural waves.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.morpheus.corotational_fem_soft_tissue import (
    FEMSimulationState,
    SoftTissueMaterial,
    Tetrahedron,
    build_tetrahedron,
    compute_corotational_pk1_stress,
    heal_inverted_and_volume_drift_tet,
    solve_fem_time_step,
)
from nexus_ai_agent.creative.morpheus.cosserat_hair_marschner_brdf import (
    HairMaterialProperties,
    HairStrand,
    build_cosserat_hair_strand,
    evaluate_marschner_brdf,
    parallel_transport_bishop_frame,
    solve_cosserat_strand_pbd,
)
from nexus_ai_agent.creative.morpheus.cranio_facial_morphable_lock import (
    CephalometricRatios,
    CranioFacialBasis,
    Sim3Transform,
    build_canonical_3dmm_basis,
    compute_cephalometric_ratios,
    lock_and_heal_cranio_facial_identity,
    synthesize_face_mesh,
    umeyama_sim3_align,
)
from nexus_ai_agent.creative.morpheus.dual_quaternion_skeletal_rig import (
    Bone,
    DualQuaternion,
    SkeletalChain,
    SkinningWeights,
    UnitQuaternion,
    damped_least_squares_ik,
    skin_dqs,
    skin_lbs,
    zero_skate_foot_lock,
)
from nexus_ai_agent.creative.morpheus.gaussian_splatting_pbr_rasterizer import (
    CameraParameters,
    DirectionalLight,
    Gaussian3D,
    PointLight,
    evaluate_ggx_pbr,
    rasterize_gaussian_splats,
)
from nexus_ai_agent.creative.morpheus.iridescence_microfiber_cloth_brdf import (
    MicrofiberSheenProperties,
    ThinFilmProperties,
)
from nexus_ai_agent.creative.morpheus.morpheus_real_media_spine import (
    BundleVerificationError,
    FencingTokenStaleError,
    MorpheusStagedSpineAdapter,
    MorpheusStageRequest,
    compute_rfc6962_merkle_root,
    generate_rfc6962_inclusion_proof,
    largest_power_of_two_less_than,
    read_png_frame,
    read_wav_pcm16,
    verify_morpheus_bundle_on_disk,
    verify_rfc6962_inclusion_proof,
    write_png_frame,
    write_wav_pcm16,
)

# ── WAVE 1 COVERAGE GUARDS ───────────────────────────────────────────────────


def test_wave1_umeyama_and_cephalometric_guards() -> None:
    """Exercise umeyama_sim3_align, lock_and_heal, and compute_cephalometric guards."""
    with pytest.raises(ValueError, match="source must be"):
        umeyama_sim3_align(np.zeros((5, 3, 1)), np.zeros((5, 3)))

    with pytest.raises(ValueError, match="target must be"):
        umeyama_sim3_align(np.zeros((5, 3)), np.zeros((5, 2)))

    with pytest.raises(ValueError, match="Point count mismatch"):
        umeyama_sim3_align(np.zeros((5, 3)), np.zeros((6, 3)))

    with pytest.raises(ValueError, match="At least 3 anchor points"):
        umeyama_sim3_align(np.zeros((2, 3)), np.zeros((2, 3)))

    with pytest.raises(ValueError, match="source or target contains NaN"):
        umeyama_sim3_align(np.full((4, 3), np.nan), np.zeros((4, 3)))

    with pytest.raises(ValueError, match="source or target contains NaN"):
        umeyama_sim3_align(np.zeros((4, 3)), np.full((4, 3), np.nan))

    # Near-zero variance in source points
    with pytest.raises(ValueError, match="near-zero variance"):
        umeyama_sim3_align(np.ones((4, 3)), np.zeros((4, 3)))

    # Zero target variance with allow_scale=True hits scale <= 1e-9 -> scale = 1.0
    src_pts = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    tgt_zeros = np.zeros((4, 3), dtype=np.float64)
    sim3_zero = umeyama_sim3_align(src_pts, tgt_zeros, allow_scale=True)
    assert sim3_zero.scale == 1.0

    b = build_canonical_3dmm_basis(vertex_count=68, k_id=2, k_exp=2)

    with pytest.raises(ValueError, match="Invalid solver hyperparameters"):
        lock_and_heal_cranio_facial_identity(b.mean_mesh, np.zeros(2), b, lambda_reg=-0.1)

    with pytest.raises(ValueError, match="Invalid solver hyperparameters"):
        lock_and_heal_cranio_facial_identity(b.mean_mesh, np.zeros(2), b, beta_max=-1.0)

    with pytest.raises(ValueError, match="observed_mesh shape mismatch"):
        lock_and_heal_cranio_facial_identity(np.zeros((60, 3)), np.zeros(2), b)

    with pytest.raises(ValueError, match="locked_alpha_id shape mismatch"):
        lock_and_heal_cranio_facial_identity(b.mean_mesh, np.zeros(3), b)

    # CranioFacialBasis guards
    with pytest.raises(ValueError, match="mean_mesh must have shape"):
        CranioFacialBasis(np.zeros((68, 2)), b.u_id, b.u_exp, 68, 2, 2)

    with pytest.raises(ValueError, match="is less than minimum"):
        CranioFacialBasis(np.zeros((50, 3)), b.u_id, b.u_exp, 50, 2, 2)

    with pytest.raises(ValueError, match="u_id shape mismatch"):
        CranioFacialBasis(b.mean_mesh, np.zeros((68 * 3, 3)), b.u_exp, 68, 2, 2)

    with pytest.raises(ValueError, match="u_exp shape mismatch"):
        CranioFacialBasis(b.mean_mesh, b.u_id, np.zeros((68 * 3, 3)), 68, 2, 2)

    with pytest.raises(ValueError, match="mean_mesh contains NaN or Inf"):
        CranioFacialBasis(np.full((68, 3), np.nan), b.u_id, b.u_exp, 68, 2, 2)

    with pytest.raises(ValueError, match="u_id contains NaN or Inf"):
        CranioFacialBasis(b.mean_mesh, np.full_like(b.u_id, np.nan), b.u_exp, 68, 2, 2)

    with pytest.raises(ValueError, match="u_exp contains NaN or Inf"):
        CranioFacialBasis(b.mean_mesh, b.u_id, np.full_like(b.u_exp, np.nan), 68, 2, 2)

    # Sim3Transform guards
    with pytest.raises(ValueError, match="rotation must be \\(3, 3\\)"):
        Sim3Transform(np.eye(2), np.zeros(3), 1.0)

    with pytest.raises(ValueError, match="translation must be \\(3,\\)"):
        Sim3Transform(np.eye(3), np.zeros(2), 1.0)

    refl_rot = np.diag([1.0, 1.0, -1.0])
    with pytest.raises(ValueError, match="Rotation matrix must have det"):
        Sim3Transform(refl_rot, np.zeros(3), 1.0)

    with pytest.raises(ValueError, match="scale must be strictly positive"):
        Sim3Transform(np.eye(3), np.zeros(3), -1.0)

    valid_sim3 = Sim3Transform(np.eye(3), np.zeros(3), 1.0)
    with pytest.raises(ValueError, match="points must have shape"):
        valid_sim3.transform(np.zeros((5, 2)))

    with pytest.raises(ValueError, match="points must have shape"):
        valid_sim3.inverse_transform(np.zeros((5, 2)))

    # build_canonical_3dmm_basis guards & expansion
    with pytest.raises(ValueError, match="less than minimum"):
        build_canonical_3dmm_basis(vertex_count=50)

    with pytest.raises(ValueError, match="Subspace dimensions must be positive"):
        build_canonical_3dmm_basis(k_id=0)

    b_expanded = build_canonical_3dmm_basis(vertex_count=70, k_id=2, k_exp=2)
    assert b_expanded.vertex_count == 70

    # synthesize_face_mesh guards
    with pytest.raises(ValueError, match="alpha_id shape mismatch"):
        synthesize_face_mesh(b, np.zeros(3), np.zeros(2))

    with pytest.raises(ValueError, match="beta_exp shape mismatch"):
        synthesize_face_mesh(b, np.zeros(2), np.zeros(3))

    with pytest.raises(ValueError, match="alpha_id or beta_exp contains NaN"):
        synthesize_face_mesh(b, np.array([np.nan, 0.0]), np.zeros(2))

    # CephalometricRatios guards
    with pytest.raises(ValueError, match="must be finite positive"):
        CephalometricRatios(-1.0, 1.0, 1.0)

    with pytest.raises(ValueError, match="must be finite positive"):
        CephalometricRatios(1.0, float("nan"), 1.0)

    # Cephalometric ratios degeneracies
    with pytest.raises(ValueError, match="mesh must have shape"):
        compute_cephalometric_ratios(np.zeros((68, 2)))

    with pytest.raises(ValueError, match="mesh must have shape"):
        compute_cephalometric_ratios(np.zeros((50, 3)))

    deg_mesh = b.mean_mesh.copy()
    deg_mesh[16] = deg_mesh[0]  # zero bizygomatic distance
    with pytest.raises(ValueError, match="Degenerate bizygomatic distance"):
        compute_cephalometric_ratios(deg_mesh)

    deg_mesh2 = b.mean_mesh.copy()
    deg_mesh2[8] = deg_mesh2[27]  # zero sagittal axis
    with pytest.raises(ValueError, match="Degenerate sagittal axis"):
        compute_cephalometric_ratios(deg_mesh2)


# ── WAVE 2 COVERAGE GUARDS ───────────────────────────────────────────────────


def test_wave2_quaternion_and_skeletal_guards() -> None:
    """Exercise UnitQuaternion, DualQuaternion, Bone, and SkeletalChain guards."""
    with pytest.raises(ValueError, match="Invalid quaternion"):
        UnitQuaternion(float("nan"), 0.0, 0.0, 0.0)

    # UnitQuaternion zero axis returns identity
    q_zero_axis = UnitQuaternion.from_axis_angle(np.zeros(3), 1.0)
    assert q_zero_axis.w == 1.0

    # UnitQuaternion zero norm fallback
    q_zero = UnitQuaternion(0.0, 0.0, 0.0, 0.0)
    assert q_zero.normalize().w == 1.0
    assert np.allclose(q_zero.to_matrix(), np.eye(3))

    ident = UnitQuaternion.identity()
    with pytest.raises(ValueError, match="Plücker orthogonality"):
        DualQuaternion(ident, ident)

    # Translation shape mismatch
    with pytest.raises(ValueError, match="translation must be \\(3,\\)"):
        DualQuaternion.from_rotation_translation(ident, np.zeros(2))

    # Degenerate dual quaternion normalization
    deg_dq = DualQuaternion(q_zero, q_zero)
    with pytest.raises(ValueError, match="Cannot normalize degenerate dual quaternion"):
        deg_dq.normalize()

    dq1 = DualQuaternion.identity()
    dq2 = DualQuaternion.identity()
    with pytest.raises(ValueError, match="Interpolation parameter t"):
        DualQuaternion.sclerp(dq1, dq2, -0.1)

    with pytest.raises(ValueError, match="Interpolation parameter t"):
        DualQuaternion.sclerp(dq1, dq2, 1.1)

    # ScLERP near identity (sin_half < 1e-8)
    dq_trans = DualQuaternion.from_rotation_translation(ident, np.array([2.0, 0.0, 0.0]))
    s_mid = DualQuaternion.sclerp(dq1, dq_trans, 0.5)
    assert np.allclose(s_mid.transform_point(np.zeros(3)), np.array([1.0, 0.0, 0.0]))

    with pytest.raises(ValueError, match="Bone bad rest_length"):
        Bone("bad", None, np.zeros(3), np.array([1.0, 0.0, 0.0]), 2.0)

    b = Bone("b1", None, np.zeros(3), np.array([1.0, 0.0, 0.0]), 1.0)
    chain = SkeletalChain(bones=(b,))
    with pytest.raises(KeyError, match="not found in chain"):
        chain.get_bone("nonexistent")

    assert chain.get_bone("b1").name == "b1"

    with pytest.raises(ValueError, match="Weights shape mismatch"):
        SkinningWeights(2, ("b1",), np.ones((3, 1)))

    with pytest.raises(ValueError, match="Weights shape mismatch"):
        SkinningWeights(2, ("b1",), np.ones((2, 2)))

    with pytest.raises(ValueError, match="Skinning weights contain negative values"):
        SkinningWeights(1, ("b1",), np.array([[-0.5]]))

    with pytest.raises(ValueError, match="Skinning weight rows must sum to 1.0"):
        SkinningWeights(1, ("b1",), np.array([[0.5]]))

    w = SkinningWeights(1, ("b1",), np.array([[1.0]]))
    with pytest.raises(ValueError, match="vertices shape mismatch with weights"):
        skin_dqs(np.zeros((2, 3)), {"b1": dq1}, w)

    with pytest.raises(KeyError):
        skin_dqs(np.zeros((1, 3)), {}, w)

    # DQS with zero weight for a bone exercises w_ij <= 0.0 branch
    w_multi = SkinningWeights(1, ("b1", "b2"), np.array([[1.0, 0.0]]))
    dq_map = {"b1": dq1, "b2": dq2}
    v_out = skin_dqs(np.zeros((1, 3)), dq_map, w_multi)
    assert v_out.shape == (1, 3)

    with pytest.raises(ValueError, match="vertices shape mismatch with weights"):
        skin_lbs(np.zeros((2, 3)), {"b1": np.eye(4)}, w)

    with pytest.raises(KeyError):
        skin_lbs(np.zeros((1, 3)), {}, w)

    with pytest.raises(ValueError, match="foot_positions must be \\(T, 3\\)"):
        zero_skate_foot_lock(np.zeros((3, 2)), np.array([True, True, True]))

    with pytest.raises(ValueError, match="contact_flags shape"):
        zero_skate_foot_lock(np.zeros((3, 3)), np.array([True]))

    # Damped Least Squares IK target already reached
    angles_init = {"b1": 0.0}
    current_tip = chain.forward_kinematics({"b1": ident})["b1"].transform_point(np.zeros(3))
    solved_angles = damped_least_squares_ik(
        chain=chain,
        end_effector_bone="b1",
        target_pos=current_tip,
        initial_angles=angles_init,
        tolerance=1e-3,
    )
    assert abs(solved_angles["b1"] - 0.0) < 1e-3


# ── WAVE 3 COVERAGE GUARDS ───────────────────────────────────────────────────


def test_wave3_gaussian_splatting_guards() -> None:
    """Exercise Gaussian3D, CameraParameters, and PointLight validation guards."""
    ident = UnitQuaternion.identity()

    with pytest.raises(ValueError, match="mean must be \\(3,\\)"):
        Gaussian3D(
            np.zeros(2), ident, np.ones(3), 0.8, np.array([0.0, 0.0, 1.0]), np.ones(3), 0.5, 0.0
        )

    with pytest.raises(ValueError, match="opacity must be in"):
        Gaussian3D(
            np.zeros(3), ident, np.ones(3), 1.5, np.array([0.0, 0.0, 1.0]), np.ones(3), 0.5, 0.0
        )

    with pytest.raises(ValueError, match="scale must be \\(3,\\) with positive entries"):
        Gaussian3D(
            np.zeros(3),
            ident,
            np.array([-1.0, 1.0, 1.0]),
            0.8,
            np.array([0.0, 0.0, 1.0]),
            np.ones(3),
            0.5,
            0.0,
        )

    with pytest.raises(ValueError, match="roughness must be in"):
        Gaussian3D(
            np.zeros(3), ident, np.ones(3), 0.8, np.array([0.0, 0.0, 1.0]), np.ones(3), -0.1, 0.0
        )

    with pytest.raises(ValueError, match="metallic must be in"):
        Gaussian3D(
            np.zeros(3), ident, np.ones(3), 0.8, np.array([0.0, 0.0, 1.0]), np.ones(3), 0.5, 1.5
        )

    with pytest.raises(ValueError, match="normal must be"):
        Gaussian3D(np.zeros(3), ident, np.ones(3), 0.8, np.zeros(2), np.ones(3), 0.5, 0.0)

    with pytest.raises(ValueError, match="normal must be unit length"):
        Gaussian3D(
            np.zeros(3),
            ident,
            np.ones(3),
            0.8,
            np.array([2.0, 0.0, 0.0]),
            np.ones(3),
            0.5,
            0.0,
        )

    with pytest.raises(ValueError, match="albedo"):
        Gaussian3D(
            np.zeros(3), ident, np.ones(3), 0.8, np.array([0.0, 0.0, 1.0]), np.zeros(2), 0.5, 0.0
        )

    with pytest.raises(ValueError, match="sh_coefficients must be"):
        Gaussian3D(
            np.zeros(3),
            ident,
            np.ones(3),
            0.8,
            np.array([0.0, 0.0, 1.0]),
            np.ones(3),
            0.5,
            0.0,
            sh_coefficients=np.zeros((8, 3)),
        )

    with pytest.raises(ValueError, match="Invalid depth planes"):
        CameraParameters(np.eye(4), 100.0, 100.0, 50.0, 50.0, 100, 100, near_plane=-0.1)

    with pytest.raises(ValueError, match="Invalid depth planes"):
        CameraParameters(
            np.eye(4), 100.0, 100.0, 50.0, 50.0, 100, 100, near_plane=1.0, far_plane=0.5
        )

    with pytest.raises(ValueError, match="width and height must be positive"):
        CameraParameters(np.eye(4), 100.0, 100.0, 50.0, 50.0, 0, 100)

    with pytest.raises(ValueError, match="Focal lengths fx and fy must be positive"):
        CameraParameters(np.eye(4), -100.0, 100.0, 50.0, 50.0, 100, 100)

    with pytest.raises(ValueError, match="view_matrix must be"):
        CameraParameters(np.eye(3), 100.0, 100.0, 50.0, 50.0, 100, 100)

    # DirectionalLight and PointLight guards
    with pytest.raises(ValueError, match="direction must be \\(3,\\)"):
        DirectionalLight(np.zeros(2), np.ones(3))

    with pytest.raises(ValueError, match="direction must be unit length"):
        DirectionalLight(np.array([2.0, 0.0, 0.0]), np.ones(3))

    with pytest.raises(ValueError, match="radiance must be non-negative"):
        DirectionalLight(np.array([0.0, 0.0, 1.0]), np.array([-1.0, 0.0, 0.0]))

    with pytest.raises(ValueError, match="position must be"):
        PointLight(np.zeros(2), np.ones(3))

    with pytest.raises(ValueError, match="intensity must be non-negative"):
        PointLight(np.zeros(3), np.array([-1.0, 1.0, 1.0]))

    # evaluate_ggx_pbr backfacing guard
    pbr_zero = evaluate_ggx_pbr(
        normal=np.array([0.0, 0.0, 1.0]),
        view_dir=np.array([0.0, 0.0, 1.0]),
        light_dir=np.array([0.0, 0.0, -1.0]),  # light from behind
        albedo=np.ones(3),
        roughness=0.5,
        metallic=0.0,
        light_radiance=np.ones(3),
    )
    assert np.allclose(pbr_zero, 0.0)

    # Rasterize with DirectionalLight, SH coefficients, ambient_radiance, offscreen Gaussian
    cam = CameraParameters(np.eye(4), 100.0, 100.0, 50.0, 50.0, 100, 100)
    sh_coeffs = np.ones((9, 3), dtype=np.float64) * 0.1
    g_sh = Gaussian3D(
        mean=np.array([0.0, 0.0, 2.0]),
        rotation=ident,
        scale=np.array([0.1, 0.1, 0.1]),
        opacity=0.9,
        normal=np.array([0.0, 0.0, -1.0]),
        albedo=np.array([0.8, 0.8, 0.8]),
        roughness=0.3,
        metallic=0.1,
        sh_coefficients=sh_coeffs,
    )
    # Offscreen Gaussian (cx far off screen)
    g_offscreen = Gaussian3D(
        mean=np.array([-500.0, 0.0, 2.0]),
        rotation=ident,
        scale=np.array([0.01, 0.01, 0.01]),
        opacity=0.9,
        normal=np.array([0.0, 0.0, -1.0]),
        albedo=np.array([0.8, 0.8, 0.8]),
        roughness=0.3,
        metallic=0.1,
    )
    d_light = DirectionalLight(np.array([0.0, 0.0, 1.0]), np.array([1.0, 1.0, 1.0]))
    p_light_coincident = PointLight(np.array([0.0, 0.0, 2.0]), np.array([1.0, 1.0, 1.0]))

    res = rasterize_gaussian_splats(
        (g_sh, g_offscreen),
        cam,
        lights=(d_light, p_light_coincident),
        ambient_radiance=np.array([0.1, 0.1, 0.1]),
    )
    assert res.rgb_float.shape == (100, 100, 3)
    assert res.visible_gaussian_count >= 1


# ── WAVE 4 COVERAGE GUARDS ───────────────────────────────────────────────────


def test_wave4_media_spine_and_merkle_guards() -> None:
    """Exercise I/O, Merkle tree, and staging adapter error guards."""
    with pytest.raises(ValueError, match="rgb must be \\(H, W, 3\\)"):
        write_png_frame(np.zeros((10, 10), dtype=np.uint8), Path("/tmp/dummy.png"))

    with pytest.raises(ValueError, match="rgb must be \\(H, W, 3\\)"):
        write_png_frame(np.zeros((10, 10, 3), dtype=np.float32), Path("/tmp/dummy.png"))

    with pytest.raises(FileNotFoundError):
        read_png_frame(Path("/tmp/nonexistent_frame_12345.png"))

    with pytest.raises(ValueError, match="samples must be int16"):
        write_wav_pcm16(np.zeros(100, dtype=np.float32), Path("/tmp/dummy.wav"))

    with pytest.raises(FileNotFoundError):
        read_wav_pcm16(Path("/tmp/nonexistent_wav_12345.wav"))

    with pytest.raises(ValueError, match="n must be > 1"):
        largest_power_of_two_less_than(1)

    # Empty leaves returns empty sha256
    empty_root = compute_rfc6962_merkle_root([])
    assert empty_root == hashlib.sha256(b"").digest()

    with pytest.raises(ValueError, match="out of bounds"):
        generate_rfc6962_inclusion_proof([b"a", b"b"], -1)

    # Inclusion proof index out of range check
    assert not verify_rfc6962_inclusion_proof(b"a", 2, 2, [], "root")
    # Invalid proof direction
    assert not verify_rfc6962_inclusion_proof(b"a", 0, 2, [("X", "abcd")], "root")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # Read multi-channel stereo WAV
        stereo_wav_p = tdp / "stereo.wav"
        stereo_data = np.zeros((100, 2), dtype=np.int16)
        with wave.open(str(stereo_wav_p), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(stereo_data.tobytes())

        loaded_stereo, sr = read_wav_pcm16(stereo_wav_p)
        assert loaded_stereo.shape == (100, 2)
        assert sr == 48000

        # Read 8-bit WAV (sampwidth != 2)
        bad_wav_p = tdp / "bad8bit.wav"
        with wave.open(str(bad_wav_p), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(1)
            wf.setframerate(48000)
            wf.writeframes(b"\x80" * 50)

        with pytest.raises(ValueError, match="Expected 16-bit WAV"):
            read_wav_pcm16(bad_wav_p)

        # Missing manifest.json
        with pytest.raises(BundleVerificationError, match="Missing manifest.json"):
            verify_morpheus_bundle_on_disk(tdp)

        # Missing PNG frames
        manifest_p = tdp / "manifest.json"
        manifest_p.write_text(json.dumps({"digests": {}}))
        with pytest.raises(BundleVerificationError, match="No PNG frames found"):
            verify_morpheus_bundle_on_disk(tdp)

        # Missing audio.wav
        f0 = tdp / "frame_0000.png"
        write_png_frame(np.zeros((8, 8, 3), dtype=np.uint8), f0)
        h_f = hashlib.sha256(f0.read_bytes()).hexdigest()
        h_c = hashlib.sha256(np.zeros((8, 8, 3), dtype=np.uint8).tobytes()).hexdigest()
        manifest_p.write_text(
            json.dumps(
                {
                    "digests": {
                        "output_frames_file_sha256": h_f,
                        "output_frames_content_sha256": h_c,
                    }
                }
            )
        )
        with pytest.raises(BundleVerificationError, match="Missing audio.wav"):
            verify_morpheus_bundle_on_disk(tdp)

        # Frame content mismatch
        manifest_p.write_text(
            json.dumps(
                {
                    "digests": {
                        "output_frames_file_sha256": h_f,
                        "output_frames_content_sha256": "tampered",
                    }
                }
            )
        )
        with pytest.raises(BundleVerificationError, match="output_frames_content_sha256 mismatch"):
            verify_morpheus_bundle_on_disk(tdp)

        # Missing video.mp4
        wav_p = tdp / "audio.wav"
        write_wav_pcm16(np.zeros(100, dtype=np.int16), wav_p)
        h_wav_f = hashlib.sha256(wav_p.read_bytes()).hexdigest()
        h_wav_c = hashlib.sha256(np.zeros(100, dtype=np.int16).tobytes()).hexdigest()
        manifest_p.write_text(
            json.dumps(
                {
                    "digests": {
                        "output_frames_file_sha256": h_f,
                        "output_frames_content_sha256": h_c,
                        "output_audio_file_sha256": h_wav_f,
                        "output_audio_content_sha256": h_wav_c,
                    }
                }
            )
        )
        with pytest.raises(BundleVerificationError, match="Missing video.mp4"):
            verify_morpheus_bundle_on_disk(tdp)

        # Stale fencing token error in staging adapter
        adapter = MorpheusStagedSpineAdapter(
            workspace_root=tdp,
            active_fencing_token_getter=lambda: "token_current",
        )
        req = MorpheusStageRequest(
            asset_id="asset_test",
            attempt_id="att_1",
            fencing_token="token_stale",
            frames_uint8=(np.zeros((8, 8, 3), dtype=np.uint8),),
            audio_pcm16=np.zeros(100, dtype=np.int16),
            audit_metrics={"cephalometric_error": 0.002},
        )
        with pytest.raises(FencingTokenStaleError, match="Fencing token stale before staging"):
            adapter.stage_and_publish(req)


# ── WAVE 5 COVERAGE GUARDS ───────────────────────────────────────────────────


def test_wave5_fem_soft_tissue_guards() -> None:
    """Exercise SoftTissueMaterial, Tetrahedron, and FEMState guards."""
    with pytest.raises(ValueError, match="youngs_modulus must be positive"):
        SoftTissueMaterial(youngs_modulus=-100.0, poissons_ratio=0.3)

    with pytest.raises(ValueError, match="poissons_ratio must be in"):
        SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.6)

    with pytest.raises(ValueError, match="density must be positive"):
        SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3, density=0.0)

    with pytest.raises(ValueError, match="rayleigh_damping must be non-negative"):
        SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3, rayleigh_damping=-0.1)

    mat = SoftTissueMaterial(youngs_modulus=1000.0, poissons_ratio=0.3)
    verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    tet = build_tetrahedron((0, 1, 2, 3), verts)

    with pytest.raises(ValueError, match="rest_vertices must be"):
        Tetrahedron(
            (0, 1, 2, 3),
            np.zeros((3, 3)),
            tet.rest_edge_matrix,
            0.1,
            tet.inv_rest_edge_matrix,
        )

    with pytest.raises(ValueError, match="rest_edge_matrix must be"):
        Tetrahedron(
            (0, 1, 2, 3),
            tet.rest_vertices,
            np.zeros((2, 2)),
            0.1,
            tet.inv_rest_edge_matrix,
        )

    with pytest.raises(ValueError, match="Degenerate or inverted rest volume"):
        Tetrahedron(
            (0, 1, 2, 3),
            tet.rest_vertices,
            tet.rest_edge_matrix,
            -0.1,
            tet.inv_rest_edge_matrix,
        )

    with pytest.raises(ValueError, match="Tetrahedron must have 4 distinct vertex indices"):
        build_tetrahedron((0, 0, 1, 2), verts)

    with pytest.raises(ValueError, match="deformed_vertices must be \\(4, 3\\)"):
        compute_corotational_pk1_stress(tet, np.zeros((3, 3)), mat)

    with pytest.raises(ValueError, match="deformed_vertices must be \\(4, 3\\)"):
        heal_inverted_and_volume_drift_tet(np.zeros((3, 3)), tet)

    # Healing already valid tet returns identity (exercises j_min <= j <= j_max early return)
    healed_id = heal_inverted_and_volume_drift_tet(verts, tet)
    assert np.allclose(healed_id, verts)

    # PK1 stress with collapsed tet (abs(J) <= 1e-8 exercises inv_f_t = np.eye(3))
    flat_verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]])
    res_flat = compute_corotational_pk1_stress(tet, flat_verts, mat)
    assert abs(res_flat.jacobian) <= 1e-8
    assert res_flat.nodal_forces.shape == (4, 3)

    # Inverted tet healing (det_rot < 0 in SVD)
    inverted_verts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]])
    healed_inv = heal_inverted_and_volume_drift_tet(inverted_verts, tet)
    assert healed_inv.shape == (4, 3)

    # Flattened tet healing (current_prod < 1e-12)
    healed_flat = heal_inverted_and_volume_drift_tet(flat_verts, tet)
    assert healed_flat.shape == (4, 3)

    # FEM simulation step with external forces and bone SDF collision
    st = FEMSimulationState(verts.copy(), np.array([[0.0, 0.0, -2.0]] * 4), (tet,), mat)
    with pytest.raises(ValueError, match="dt must be positive"):
        solve_fem_time_step(st, dt=0.0)

    def bone_sdf(pt: np.ndarray) -> tuple[float, np.ndarray]:
        # Plane at z = 0.5; pt has z < 0.5 means penetration
        return float(pt[2] - 0.5), np.array([0.0, 0.0, 1.0])

    ext_forces = np.ones((4, 3)) * 0.1
    next_st = solve_fem_time_step(
        st, dt=0.01, external_forces=ext_forces, cranial_bone_sdf=bone_sdf
    )
    assert next_st.vertices.shape == (4, 3)


# ── WAVE 6 COVERAGE GUARDS ───────────────────────────────────────────────────


def test_wave6_cosserat_hair_guards() -> None:
    """Exercise HairMaterialProperties, HairStrand, and Cosserat solver guards."""
    with pytest.raises(ValueError, match="ior must be > 1.0"):
        HairMaterialProperties(ior=0.9)

    with pytest.raises(ValueError, match="absorption must be non-negative"):
        HairMaterialProperties(absorption=np.zeros(2))

    with pytest.raises(ValueError, match="vertices must be \\(N, 3\\)"):
        HairStrand(0, np.zeros((3, 3, 1)), (1.0, 1.0), np.zeros((2, 3)), np.zeros((2, 3)))

    with pytest.raises(ValueError, match="Hair strand must have at least 2 vertices"):
        HairStrand(0, np.zeros((1, 3)), (), np.zeros((0, 3)), np.zeros((0, 3)))

    with pytest.raises(ValueError, match="rest_lengths length"):
        HairStrand(0, np.zeros((3, 3)), (1.0,), np.zeros((2, 3)), np.zeros((2, 3)))

    with pytest.raises(ValueError, match="tangents shape mismatch"):
        HairStrand(0, np.zeros((3, 3)), (1.0, 1.0), np.zeros((3, 3)), np.zeros((2, 3)))

    with pytest.raises(ValueError, match="bishop_normals shape mismatch"):
        HairStrand(0, np.zeros((3, 3)), (1.0, 1.0), np.zeros((2, 3)), np.zeros((3, 3)))

    with pytest.raises(ValueError, match="Strand vertices count"):
        build_cosserat_hair_strand(np.zeros((1, 3)))

    with pytest.raises(ValueError, match="Degenerate hair segment"):
        parallel_transport_bishop_frame(np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]))

    # Parallel transport Bishop frame where tangent turns into previous normal (n_ui < 1e-8)
    curved_hair = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 1.0]], dtype=np.float64)
    tangents, normals = parallel_transport_bishop_frame(curved_hair)
    assert tangents.shape == (2, 3)
    assert normals.shape == (2, 3)

    strand = build_cosserat_hair_strand(
        np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 2.0]])
    )
    with pytest.raises(ValueError, match="dt must be positive"):
        solve_cosserat_strand_pbd(strand, np.zeros(3), dt=0.0, gravity=np.zeros(3))

    # PBD with collapsed vertex distance (exercises curr_l < 1e-12 continue)
    with pytest.raises(ValueError, match="Degenerate hair segment"):
        solve_cosserat_strand_pbd(
            strand,
            scalp_anchor=np.zeros(3),
            dt=0.1,
            gravity=np.array([0.0, 0.0, -100.0]),
        )

    # PBD with head SDF collision
    def head_sdf(pt: np.ndarray) -> tuple[float, np.ndarray]:
        return float(pt[2] - 0.5), np.array([0.0, 0.0, 1.0])

    pbd_strand = solve_cosserat_strand_pbd(
        strand,
        scalp_anchor=np.zeros(3),
        dt=0.01,
        gravity=np.array([0.0, 0.0, -9.8]),
        head_sdf=head_sdf,
    )
    assert pbd_strand.vertices.shape == (3, 3)

    # Evaluate Marschner BRDF with wi parallel to tangent (exercises norm_pi <= 1e-6 branch)
    hair_mat = HairMaterialProperties()
    brdf_parallel = evaluate_marschner_brdf(
        tangent=np.array([0.0, 0.0, 1.0]),
        omega_in=np.array([0.0, 0.0, 1.0]),
        omega_out=np.array([0.0, 1.0, 0.0]),
        material=hair_mat,
    )
    assert (brdf_parallel.total_radiance >= 0.0).all()


# ── WAVE 7 COVERAGE GUARDS ───────────────────────────────────────────────────


def test_wave7_cloth_guards() -> None:
    """Exercise Wave 7 ThinFilmProperties and MicrofiberSheenProperties guards."""
    with pytest.raises(ValueError, match="Refractive indices must be strictly positive"):
        ThinFilmProperties(n1_ambient=-1.0)

    with pytest.raises(ValueError, match="thickness_nm must be in"):
        ThinFilmProperties(thickness_nm=2500.0)

    with pytest.raises(ValueError, match="sheen_color must be \\(3,\\)"):
        MicrofiberSheenProperties(sheen_color=np.zeros(2))

    with pytest.raises(ValueError, match="roughness must be in"):
        MicrofiberSheenProperties(sheen_color=np.ones(3), roughness=0.0)


# ── TAMPER HOOK AND PRE-PUBLISH STALE FENCING ────────────────────────────────


def test_morpheus_spine_pre_publish_stale_fencing() -> None:
    """Exercise pre-publish tamper hook causing fencing token change right before promotion."""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        current_token = ["tok_initial"]

        def get_tok() -> str:
            return current_token[0]

        def tamper_change_tok(staging_p: Path) -> None:
            # Change active token during staging before promotion step
            current_token[0] = "tok_superseded"

        adapter = MorpheusStagedSpineAdapter(
            workspace_root=tdp,
            active_fencing_token_getter=get_tok,
            pre_publish_tamper_hook=tamper_change_tok,
        )

        req = MorpheusStageRequest(
            asset_id="asset_stale_test",
            attempt_id="att_1",
            fencing_token="tok_initial",
            frames_uint8=(np.zeros((8, 8, 3), dtype=np.uint8),),
            audio_pcm16=np.zeros(100, dtype=np.int16),
            audit_metrics={"drift_error": 0.0005},
        )

        with pytest.raises(FencingTokenStaleError, match="before promotion"):
            adapter.stage_and_publish(req)


# ── WAVE 9 COVERAGE GUARDS ───────────────────────────────────────────────────


def test_wave9_spectral_skin_chromophore_sss_coverage_guards() -> None:
    from nexus_ai_agent.creative.morpheus.spectral_skin_chromophore_sss import (
        ChromophoreProperties,
        compute_spectral_mu_a,
        compute_spectral_mu_s_prime,
        convolve_separable_sss_screen_buffer,
        couple_fem_strain_to_perfusion,
    )

    with pytest.raises(ValueError, match="melanin_fraction"):
        ChromophoreProperties(melanin_fraction=-0.1)
    with pytest.raises(ValueError, match="melanin_fraction"):
        ChromophoreProperties(melanin_fraction=1.5)
    with pytest.raises(ValueError, match="blood_fraction"):
        ChromophoreProperties(blood_fraction=-0.1)
    with pytest.raises(ValueError, match="blood_fraction"):
        ChromophoreProperties(blood_fraction=1.5)
    with pytest.raises(ValueError, match="oxygenation_ratio"):
        ChromophoreProperties(oxygenation_ratio=-0.1)
    with pytest.raises(ValueError, match="oxygenation_ratio"):
        ChromophoreProperties(oxygenation_ratio=1.5)
    with pytest.raises(ValueError, match="Scattering coefficients"):
        ChromophoreProperties(scattering_a=-1.0)

    with pytest.raises(ValueError, match="wavelength_nm must be positive"):
        compute_spectral_mu_a(-500.0, 0.05, 0.8, 0.02, 0.7)
    with pytest.raises(ValueError, match="wavelength_nm must be positive"):
        compute_spectral_mu_s_prime(-500.0)

    with pytest.raises(ValueError, match="autonomic_flush_arousal must be in"):
        couple_fem_strain_to_perfusion(0.05, np.zeros(1), autonomic_flush_arousal=-0.1)
    with pytest.raises(ValueError, match="autonomic_flush_arousal must be in"):
        couple_fem_strain_to_perfusion(0.05, np.zeros(1), autonomic_flush_arousal=1.5)

    h, w = 8, 8
    irr = np.ones((h, w, 3), dtype=np.float64)
    depth = np.full((h, w), 10.0, dtype=np.float64)
    normal = np.zeros((h, w, 3), dtype=np.float64)
    normal[:, :, 2] = 1.0
    l_d = np.array([2.0, 1.0, 0.5])

    with pytest.raises(ValueError, match=r"irradiance_rgb must be \(H, W, 3\)"):
        convolve_separable_sss_screen_buffer(np.ones((h, w)), l_d, depth, normal)
    with pytest.raises(ValueError, match="depth_z shape"):
        convolve_separable_sss_screen_buffer(irr, l_d, np.ones((h, w + 1)), normal)
    with pytest.raises(ValueError, match="world_normal shape"):
        convolve_separable_sss_screen_buffer(irr, l_d, depth, np.ones((h, w, 2)))
    with pytest.raises(ValueError, match="pixel_size_mm must be positive"):
        convolve_separable_sss_screen_buffer(irr, l_d, depth, normal, pixel_size_mm=-1.0)

    depth_with_zeros = depth.copy()
    depth_with_zeros[0, :] = -1.0
    depth_with_zeros[2:4, :] = 50.0
    res = convolve_separable_sss_screen_buffer(
        irr, l_d, depth_with_zeros, normal, max_radius_pixels=2, bilateral_depth_threshold_mm=1.0
    )
    assert res.shape == (h, w, 3)


# ── WAVE 10 COVERAGE GUARDS ──────────────────────────────────────────────────


def test_wave10_neuromuscular_facs_articulatory_coverage_guards() -> None:
    from nexus_ai_agent.creative.morpheus.neuromuscular_facs_articulatory_solver import (
        HillMuscleParameters,
        apply_antagonistic_inhibition,
        compute_hill_muscle_force,
        evaluate_tmj_coupled_se3,
        extract_audio_acoustic_features,
        solve_bilabial_contact_and_lip_seal,
        step_first_order_activation,
    )

    with pytest.raises(ValueError, match="f_max_n and optimal_length_mm"):
        HillMuscleParameters(f_max_n=-10.0)
    with pytest.raises(ValueError, match="Time constants tau_act and tau_deact"):
        HillMuscleParameters(tau_act_s=-0.01)

    params = HillMuscleParameters()
    with pytest.raises(ValueError, match="dt_s must be positive"):
        step_first_order_activation(0.5, 1.0, dt_s=-0.01, params=params)

    a_deact = step_first_order_activation(0.8, 0.2, dt_s=0.01, params=params)
    assert a_deact < 0.8

    f_ecc = compute_hill_muscle_force(1.0, 1.0, 0.5, params=params)
    assert f_ecc > 80.0

    f_zero_pas = compute_hill_muscle_force(0.0, 0.9, 0.0, params=params)
    assert f_zero_pas == 0.0

    inhibited_noop = apply_antagonistic_inhibition(
        {"AU12": 0.3, "AU15": 0.3, "AU22": 0.2, "AU20": 0.2}
    )
    assert inhibited_noop["AU12"] == 0.3

    with pytest.raises(ValueError, match="jaw_opening_rad must be in"):
        evaluate_tmj_coupled_se3(jaw_opening_rad=-0.1)

    chin_low = np.array([0.0, 0.0, -100.0])
    tmj_clamped = evaluate_tmj_coupled_se3(
        jaw_opening_rad=0.3, mandible_chin_rest=chin_low, neck_boundary_z=-50.0
    )
    assert tmj_clamped.chin_clearance_mm == 0.0

    with pytest.raises(ValueError, match="samples must be int16"):
        extract_audio_acoustic_features(np.zeros(10, dtype=np.float32))
    empty_feat = extract_audio_acoustic_features(np.zeros(0, dtype=np.int16))
    assert empty_feat.rms_energy == 0.0

    with pytest.raises(ValueError, match="upper_lip_vertices must be"):
        solve_bilabial_contact_and_lip_seal(np.zeros((4, 2)), np.zeros((4, 3)), False)
    with pytest.raises(ValueError, match="lower_lip_vertices must be"):
        solve_bilabial_contact_and_lip_seal(np.zeros((4, 3)), np.zeros((4, 2)), False)
    with pytest.raises(ValueError, match="Upper and lower lip vertex counts must match"):
        solve_bilabial_contact_and_lip_seal(np.zeros((4, 3)), np.zeros((5, 3)), False)

    u_open = np.array([[0.0, 5.0, 0.0]])
    l_open = np.array([[0.0, 0.0, 0.0]])
    _, _, d_open = solve_bilabial_contact_and_lip_seal(u_open, l_open, is_closure_frame=False)
    assert d_open == 5.0


# ── WAVE 11 COVERAGE GUARDS ──────────────────────────────────────────────────


def test_wave11_ocular_corneal_refraction_coverage_guards() -> None:
    from nexus_ai_agent.creative.morpheus.ocular_corneal_refraction_meniscus import (
        EyeOpticalParameters,
        deform_eyelid_corneal_bulge,
        evaluate_pupillary_light_reflex,
        trace_3d_snell_corneal_refraction,
    )

    with pytest.raises(ValueError, match="cornea_radius must be smaller than sclera_radius"):
        EyeOpticalParameters(cornea_radius_mm=20.0, sclera_radius_mm=12.0)
    with pytest.raises(ValueError, match="Refractive indices must be > 1.0"):
        EyeOpticalParameters(cornea_ior=0.8)

    params = EyeOpticalParameters()
    c_s = np.zeros(3)
    rot = np.eye(3)

    with pytest.raises(ValueError, match="eyelid_vertices must be"):
        deform_eyelid_corneal_bulge(np.zeros((4, 2)), c_s, rot, params)

    eyelid_center = np.zeros((1, 3))
    def_center = deform_eyelid_corneal_bulge(eyelid_center, c_s, rot, params)
    assert def_center.shape == (1, 3)

    with pytest.raises(ValueError, match="scene_illuminance_lux cannot be negative"):
        evaluate_pupillary_light_reflex(-10.0)

    ray_miss = trace_3d_snell_corneal_refraction(
        view_ray_origin=np.array([100.0, 100.0, 100.0]),
        view_ray_direction=np.array([1.0, 0.0, 0.0]),
        sclera_center=c_s,
        gaze_rotation=rot,
    )
    assert ray_miss is None

    ray_away = trace_3d_snell_corneal_refraction(
        view_ray_origin=np.array([0.0, 0.0, 25.0]),
        view_ray_direction=np.array([0.0, 0.0, 1.0]),
        sclera_center=c_s,
        gaze_rotation=rot,
    )
    assert ray_away is None


# ── WAVE 12 COVERAGE GUARDS ──────────────────────────────────────────────────


def test_wave12_coupled_multiphysics_ipc_coverage_guards() -> None:
    from nexus_ai_agent.creative.morpheus import (
        Bone,
        FEMSimulationState,
        SkeletalChain,
        SkinningWeights,
        SoftTissueMaterial,
        UnitQuaternion,
        build_tetrahedron,
    )
    from nexus_ai_agent.creative.morpheus.coupled_multiphysics_ipc_contact import (
        ccd_conservative_line_search,
        compute_smoothed_coulomb_friction,
        evaluate_ipc_log_barrier,
        step_coupled_morpheus_multiphysics,
    )

    with pytest.raises(ValueError, match="hat_d and kappa must be strictly positive"):
        evaluate_ipc_log_barrier(0.1, hat_d=-1.0)
    with pytest.raises(ValueError, match="non-positive distance"):
        evaluate_ipc_log_barrier(-0.1, hat_d=1.0)

    f_zero, p_zero = compute_smoothed_coulomb_friction(0.0, np.zeros(3), np.array([0.0, 0.0, 1.0]))
    assert np.allclose(f_zero, 0.0)
    assert p_zero == 0.0

    alpha_max = ccd_conservative_line_search(
        start_point=np.array([0.0, 0.0, 0.0]),
        displacement_step=np.array([0.0, 0.0, 1.0]),
        obstacle_sdf_fn=lambda p: -10.0,
        max_bisections=3,
    )
    assert alpha_max < 0.2

    b = Bone("b1", None, np.zeros(3), np.array([0.0, 0.0, 10.0]), 10.0)
    chain = SkeletalChain((b,))
    weights = SkinningWeights(1, ("b1",), np.array([[1.0]]))
    mat = SoftTissueMaterial(5000.0, 0.3)
    v = np.array([[0.0, 0.0, 0.0], [5.0, 0.0, 0.0], [0.0, 5.0, 0.0], [0.0, 0.0, 5.0]])
    tet = build_tetrahedron((0, 1, 2, 3), v)
    fem = FEMSimulationState(v, np.zeros((4, 3)), (tet,), mat)

    with pytest.raises(ValueError, match="dt_s must be positive"):
        step_coupled_morpheus_multiphysics(
            chain,
            {"b1": UnitQuaternion.identity()},
            weights,
            fem,
            (),
            np.zeros((0, 3)),
            dt_s=0.0,
        )

    bad_weights = SkinningWeights(2, ("b1", "b2"), np.eye(2))
    # Weights mismatch falls back gracefully
    bone_fb, _, _, _, _ = step_coupled_morpheus_multiphysics(
        chain, {"b1": UnitQuaternion.identity()}, bad_weights, fem, (), np.zeros((0, 3))
    )
    assert bone_fb.shape == (1, 3)

    cloth_near = np.array([[0.0, 0.0, 26.5]])
    _, _, _, cloth_out, receipt = step_coupled_morpheus_multiphysics(
        chain,
        {"b1": UnitQuaternion.identity()},
        weights,
        fem,
        (),
        cloth_near,
        dt_s=0.01,
        ipc_hat_d_mm=2.0,
    )
    assert cloth_out.shape == (1, 3)


# ── WAVE 13 COVERAGE GUARDS ──────────────────────────────────────────────────


def test_wave13_gaussian_deferred_relighting_coverage_guards() -> None:
    from nexus_ai_agent.creative.morpheus import CameraParameters, Gaussian3D, UnitQuaternion
    from nexus_ai_agent.creative.morpheus.gaussian_deferred_relighting_taa import (
        SphericalGaussianLobe,
        apply_variance_clipped_taa,
        evaluate_sg_inner_product,
        relight_deferred_gbuffer_sg,
        splat_deferred_gbuffer,
    )

    with pytest.raises(ValueError, match="Focal lengths fx and fy must be positive"):
        CameraParameters(np.eye(4), -10.0, 10.0, 10.0, 10.0, 16, 16)
    with pytest.raises(ValueError, match="Focal lengths fx and fy must be positive"):
        CameraParameters(np.eye(4), 10.0, -10.0, 10.0, 10.0, 16, 16)
    with pytest.raises(ValueError, match="Image dimensions width and height must be positive"):
        CameraParameters(np.eye(4), 10.0, 10.0, 10.0, 10.0, 0, 16)

    cam = CameraParameters(np.eye(4), 50.0, 50.0, 8.0, 8.0, 16, 16)

    g_behind = Gaussian3D(
        mean=np.array([0.0, 0.0, -5.0]),
        rotation=UnitQuaternion.identity(),
        scale=np.array([0.1, 0.1, 0.1]),
        opacity=0.9,
        normal=np.array([0.0, 0.0, 1.0]),
        albedo=np.ones(3),
        roughness=0.5,
        metallic=0.0,
    )
    g_out_of_bounds = Gaussian3D(
        mean=np.array([100.0, 100.0, 2.0]),
        rotation=UnitQuaternion.identity(),
        scale=np.array([0.1, 0.1, 0.1]),
        opacity=0.9,
        normal=np.array([0.0, 0.0, 1.0]),
        albedo=np.ones(3),
        roughness=0.5,
        metallic=0.0,
    )
    gbuf_empty = splat_deferred_gbuffer((g_behind, g_out_of_bounds), cam)
    assert np.all(gbuf_empty.alpha_matte == 0.0)

    sg_1 = SphericalGaussianLobe(np.array([0.0, 0.0, 1.0]), 1.0, np.ones(3))
    sg_antipodal = SphericalGaussianLobe(np.array([0.0, 0.0, -1.0]), 1.0, np.ones(3))
    prod_zero = evaluate_sg_inner_product(sg_1, sg_antipodal)
    assert prod_zero.shape == (3,)

    lit_empty = relight_deferred_gbuffer_sg(gbuf_empty, cam, (sg_1,))
    assert lit_empty.shape == (16, 16, 3)

    with pytest.raises(ValueError, match="current and history frame shapes mismatch"):
        apply_variance_clipped_taa(
            np.zeros((8, 8, 3)),
            np.zeros((9, 8, 3)),
            np.zeros((8, 8, 2)),
            np.zeros((8, 8)),
            np.zeros((8, 8)),
        )
    with pytest.raises(ValueError, match="motion_vectors_2d shape mismatch"):
        apply_variance_clipped_taa(
            np.zeros((8, 8, 3)),
            np.zeros((8, 8, 3)),
            np.zeros((8, 8, 3)),
            np.zeros((8, 8)),
            np.zeros((8, 8)),
        )

    big_motion = np.full((8, 8, 2), 100.0)
    taa_oob = apply_variance_clipped_taa(
        np.ones((8, 8, 3)), np.zeros((8, 8, 3)), big_motion, np.zeros((8, 8)), np.zeros((8, 8))
    )
    assert np.allclose(taa_oob, 1.0)


# ── WAVE 14 COVERAGE GUARDS ──────────────────────────────────────────────────


def test_wave14_real_media_spine_and_port_coverage_guards(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.morpheus.morpheus_port import MorpheusPort
    from nexus_ai_agent.creative.morpheus.morpheus_real_media_spine import (
        MorpheusAssetVersionRecord,
        MorpheusBundleDigests,
        MorpheusDurableLineageStore,
        largest_power_of_two_less_than,
        probe_mp4_container,
        read_png_frame,
        write_png_frame,
    )

    with pytest.raises(ValueError, match="n must be > 1"):
        largest_power_of_two_less_than(1)

    with pytest.raises(ValueError, match=r"rgb must be \(H, W, 3\) uint8 array"):
        write_png_frame(np.zeros((8, 8), dtype=np.uint8), tmp_path / "bad.png")
    with pytest.raises(ValueError, match=r"rgb must be \(H, W, 3\) uint8 array"):
        write_png_frame(np.zeros((8, 8, 3), dtype=np.float32), tmp_path / "bad.png")

    with pytest.raises(FileNotFoundError, match="PNG frame not found"):
        read_png_frame(tmp_path / "nonexistent.png")

    with pytest.raises(FileNotFoundError, match="MP4 file not found"):
        probe_mp4_container(tmp_path / "nonexistent.mp4", 24, 10)

    corrupt_mp4 = tmp_path / "corrupt.mp4"
    corrupt_mp4.write_bytes(b"not an mp4")
    with pytest.raises(BundleVerificationError, match="Failed to open MP4 container"):
        probe_mp4_container(corrupt_mp4, 24, 10)

    store = MorpheusDurableLineageStore(tmp_path)
    assert store.get_asset_version("nonexistent") is None
    assert store.get_artifact_passport("nonexistent") is None

    store.ledger_file.write_text("corrupted json content")
    assert store.get_asset_version("nonexistent") is None

    digests = MorpheusBundleDigests("a", "b", "c", "d", "e", "f", "g", "h")
    rec = MorpheusAssetVersionRecord(
        "asset_100", "att_1", "tok_01", digests, "pub_dir", "pub_mp4", "manifest_sha"
    )
    passport = store.record_asset_version(rec)
    assert passport.asset_id == "asset_100"

    passport_replay = store.record_asset_version(rec)
    assert passport_replay.passport_id == passport.passport_id

    retrieved_rec = store.get_asset_version("asset_100")
    assert retrieved_rec is not None
    assert retrieved_rec.asset_id == "asset_100"

    retrieved_pass = store.get_artifact_passport("asset_100")
    assert retrieved_pass is not None
    assert retrieved_pass.passport_id == passport.passport_id

    rec_stale = MorpheusAssetVersionRecord(
        "asset_100", "att_2", "tok_00", digests, "pub_dir", "pub_mp4", "manifest_sha"
    )
    with pytest.raises(FencingTokenStaleError, match="Fencing token regression"):
        store.record_asset_version(rec_stale)

    from nexus_ai_agent.creative.morpheus.morpheus_real_media_spine import (
        MorpheusStagedSpineAdapter,
        MorpheusStageRequest,
        mux_mp4_ffmpeg,
        verify_morpheus_bundle_on_disk,
    )

    with pytest.raises(FileNotFoundError, match="Audio WAV missing"):
        mux_mp4_ffmpeg(
            str(tmp_path / "frame_%04d.png"),
            tmp_path / "missing.wav",
            tmp_path / "out.mp4",
        )

    stale_p1_req = MorpheusStageRequest(
        asset_id="asset_stale_p1",
        attempt_id="att_p1",
        fencing_token="tok_bad",
        frames_uint8=(np.zeros((8, 8, 3), dtype=np.uint8),),
        audio_pcm16=np.zeros(2400, dtype=np.int16),
    )
    adapter_stale1 = MorpheusStagedSpineAdapter(tmp_path / "sp_stale", lambda: "tok_good")
    with pytest.raises(FencingTokenStaleError, match="Fencing token stale before staging"):
        adapter_stale1.stage_and_publish(stale_p1_req)

    def authorizer(op: str, actor: str) -> bool:
        if actor == "error_actor":
            raise RuntimeError("authorizer crashed")
        return actor == "auth_user"

    port = MorpheusPort(
        workspace_root=tmp_path,
        active_fencing_token_getter=lambda: "tok_1",
        command_authorizer=authorizer,
        lineage_repository=store,
    )

    with pytest.raises(PermissionError, match="CommandBus authorization error"):
        port.execute_operation("morpheus.character_capsule_verify", {}, actor_id="error_actor")

    from nexus_ai_agent.creative.morpheus import (
        Bone,
        FEMSimulationState,
        MorpheusStageRequest,
        SkeletalChain,
        SkinningWeights,
        SoftTissueMaterial,
        UnitQuaternion,
        build_tetrahedron,
        create_character_identity_capsule,
    )

    with pytest.raises(PermissionError, match="CommandBus denied"):
        port.execute_operation("morpheus.character_capsule_verify", {}, actor_id="unauth_user")

    capsule = create_character_identity_capsule(
        character_id="hero",
        canonical_alpha_id=(0.0, 0.0),
        cephalometric_ratios={"ip_bz": 0.4},
        skeletal_rest_lengths={},
        soft_tissue_rest_volumes=(),
        hair_strand_count=0,
        hair_rest_segment_lengths=(),
        melanin_absorption=(0.1, 0.2, 0.3),
        cloth_thin_film_thickness_nm=400.0,
        cloth_sheen_roughness=0.3,
    )
    res_capsule = port.execute_operation(
        "morpheus.character_capsule_verify",
        {"capsule": capsule},
        actor_id="auth_user",
        idempotency_key="idem_1",
    )
    assert res_capsule.status == "SUCCESS"

    res_bad_cap = port.execute_operation(
        "morpheus.character_capsule_verify",
        {"capsule": "bad"},
        actor_id="auth_user",
    )
    assert res_bad_cap.status == "REFUSED"

    res_replayed = port.execute_operation(
        "morpheus.character_capsule_verify",
        {},
        actor_id="auth_user",
        idempotency_key="idem_1",
    )
    assert res_replayed.idempotent_replayed is True

    stage_req = MorpheusStageRequest(
        asset_id="ast_port",
        attempt_id="att_port",
        fencing_token="tok_1",
        frames_uint8=(np.zeros((8, 8, 3), dtype=np.uint8),),
        audio_pcm16=np.zeros(2400, dtype=np.int16),
    )
    res_spine = port.execute_operation(
        "morpheus.staged_spine_master",
        {"request": stage_req},
        actor_id="auth_user",
    )
    assert res_spine.status == "SUCCESS"
    assert res_spine.data is not None
    pub_mp4 = str(res_spine.data["published_mp4_path"])
    with pytest.raises(BundleVerificationError, match="Decoded MP4 frame count"):
        probe_mp4_container(pub_mp4, expected_fps=24, expected_frame_count=999)

    with pytest.raises(BundleVerificationError, match="MP4 stream fps"):
        probe_mp4_container(pub_mp4, expected_fps=120, expected_frame_count=1)

    # Test verify_morpheus_bundle_on_disk with epistemic violations
    dummy_staging_viol = tmp_path / "dummy_viol"
    dummy_staging_viol.mkdir(parents=True, exist_ok=True)
    (dummy_staging_viol / "manifest.json").write_text(
        json.dumps({"wave_epistemic_statuses": {"WAVE_01": "VIOLATED"}})
    )
    with pytest.raises(BundleVerificationError, match="VIOLATED physical wave invariant"):
        verify_morpheus_bundle_on_disk(dummy_staging_viol)

    res_bad_req = port.execute_operation(
        "morpheus.staged_spine_master",
        {"request": "bad"},
        actor_id="auth_user",
    )
    assert res_bad_req.status == "REFUSED"

    b = Bone("b1", None, np.zeros(3), np.array([0.0, 0.0, 10.0]), 10.0)
    chain = SkeletalChain((b,))
    weights = SkinningWeights(1, ("b1",), np.array([[1.0]]))
    mat = SoftTissueMaterial(5000.0, 0.3)
    v_tet = np.array([[0.0, 0.0, 0.0], [5.0, 0.0, 0.0], [0.0, 5.0, 0.0], [0.0, 0.0, 5.0]])
    tet = build_tetrahedron((0, 1, 2, 3), v_tet)
    fem = FEMSimulationState(v_tet, np.zeros((4, 3)), (tet,), mat)

    res_render = port.execute_operation(
        "morpheus.multiphysics_relight_render",
        {
            "skeletal_chain": chain,
            "joint_rotations": {"b1": UnitQuaternion.identity()},
            "skinning_weights": weights,
            "fem_state": fem,
            "hair_strands": (),
            "cloth_vertices": np.zeros((0, 3)),
        },
        actor_id="auth_user",
    )
    assert res_render.status == "SUCCESS"


def test_wave_deep_branch_coverage_completion() -> None:
    """Exercise remaining defensive branch completions across Morpheus modules."""
    from nexus_ai_agent.creative.morpheus.coupled_multiphysics_ipc_contact import (
        compute_smoothed_coulomb_friction,
    )
    from nexus_ai_agent.creative.morpheus.gaussian_deferred_relighting_taa import (
        SphericalGaussianLobe,
    )
    from nexus_ai_agent.creative.morpheus.ocular_corneal_refraction_meniscus import (
        EyeOpticalParameters,
        trace_3d_snell_corneal_refraction,
    )
    from nexus_ai_agent.creative.morpheus.spectral_skin_chromophore_sss import (
        ChromophoreProperties,
    )

    # 1. SphericalGaussianLobe validation guards
    with pytest.raises(ValueError, match="axis_direction must be"):
        SphericalGaussianLobe(np.zeros(2), 1.0, np.ones(3))

    with pytest.raises(ValueError, match="must be unit length"):
        SphericalGaussianLobe(np.array([2.0, 0.0, 0.0]), 1.0, np.ones(3))

    with pytest.raises(ValueError, match="sharpness must be positive"):
        SphericalGaussianLobe(np.array([0.0, 0.0, 1.0]), -1.0, np.ones(3))

    with pytest.raises(ValueError, match="amplitude_rgb must be non-negative"):
        SphericalGaussianLobe(np.array([0.0, 0.0, 1.0]), 1.0, np.array([-1.0, 0.0, 0.0]))

    with pytest.raises(ValueError, match="amplitude_rgb must be non-negative"):
        SphericalGaussianLobe(np.array([0.0, 0.0, 1.0]), 1.0, np.ones(2))

    # 2. compute_smoothed_coulomb_friction with speed_t >= eps_velocity (phi = 1.0)
    f_t, p_diss = compute_smoothed_coulomb_friction(
        normal_force_n=10.0,
        relative_tangential_velocity=np.array([10.0, 0.0, 0.0]),
        contact_normal=np.array([0.0, 1.0, 0.0]),
        mu_friction=0.3,
        eps_velocity=1e-3,
    )
    assert f_t.shape == (3,)
    assert p_diss > 0.0

    # 3. trace_3d_snell_corneal_refraction missing ray returning None
    opt = EyeOpticalParameters()
    res_miss = trace_3d_snell_corneal_refraction(
        view_ray_origin=np.array([0.0, 0.0, -100.0]),
        view_ray_direction=np.array([0.0, 1.0, 0.0]),  # perpendicular to cornea vertex at [0,0,0]
        sclera_center=np.zeros(3),
        gaze_rotation=np.eye(3),
        params=opt,
    )
    assert res_miss is None

    # 4. ChromophoreProperties invalid oxygenation ratio
    with pytest.raises(ValueError, match="oxygenation_ratio must be in"):
        ChromophoreProperties(oxygenation_ratio=0.2)

    with pytest.raises(ValueError, match="oxygenation_ratio must be in"):
        ChromophoreProperties(oxygenation_ratio=1.2)
