"""M72--M137 source/operator mutants, isolated without unloading C extensions.

Each oracle is first run against the real implementation, then the mutated
source. A mutant is killed only when the SAME independent oracle rejects it.
Existing fault-injection probes and handler-disable tests remain unchanged.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import numpy.typing as npt
import pytest

from nexus_ai_agent.creative.veritas import (
    catoptric_reflection_physics as mirror,
)
from nexus_ai_agent.creative.veritas import (
    continuum_and_quantum_mechanics as quantum,
)
from nexus_ai_agent.creative.veritas import (
    hand_kinematics as hand,
)
from nexus_ai_agent.creative.veritas import (
    homography_diegetic_typography as typography,
)
from nexus_ai_agent.creative.veritas import (
    neuro_facial_microdynamics as face,
)
from nexus_ai_agent.creative.veritas import (
    ocular_biophysics as ocular,
)
from nexus_ai_agent.creative.veritas import (
    omnia_cinema_physics as omnia,
)
from nexus_ai_agent.creative.veritas import (
    projective_shadow_transport as shadow,
)
from nexus_ai_agent.creative.veritas import (
    real_media_veritas_compiler as compiler,
)
from nexus_ai_agent.creative.veritas import (
    reality_auditor as auditor,
)
from nexus_ai_agent.creative.veritas import (
    se3_object_permanence as se3,
)
from nexus_ai_agent.creative.veritas import (
    symplectic_contact_mechanics as mechanics,
)
from nexus_ai_agent.creative.veritas import (
    thermodynamic_arrow_of_time as thermo,
)
from nexus_ai_agent.creative.veritas import (
    veritas_spine_adapter as spine,
)
from nexus_ai_agent.creative.veritas import (
    xpbd_cloth_drape as cloth,
)

MUTATIONS = [
    ("M72", quantum, "(drho_dt + divergence) ** 2", "divergence ** 2"),
    ("M73", quantum, "np.exp(-potential / scale)", "np.exp(potential / scale)"),
    ("M74", omnia, "cauchy_b_coefficient / lambda_r**2", "-cauchy_b_coefficient / lambda_r**2"),
    (
        "M75",
        omnia,
        "sound_speed_m_s - float(radial_velocity_m_s)",
        "sound_speed_m_s + float(radial_velocity_m_s)",
    ),
    ("M76", omnia, "if n_cols % 2 == 0:", "if True:"),
    ("M77", omnia, "-0.5 * blur_arc, 0.5 * blur_arc", "0.0 * blur_arc, blur_arc"),
    ("M78", auditor, "after, _ = self._check_eyes(healed)", "after = ()"),
    (
        "M79",
        compiler,
        "output_frames_content_sha256 = _digest_array(decoded_frames)",
        "output_frames_content_sha256 = _digest_files(png_paths)",
    ),
    (
        "M80",
        spine,
        (
            "if self.active_fencing_token_getter() != request.fencing_token:\n"
            '                raise VeritasStaleFencingTokenError("stale before promotion")'
        ),
        'if False:\n                raise VeritasStaleFencingTokenError("stale before promotion")',
    ),
    ("M81", spine, "if not verify_compiled_veritas_bundle_on_disk(bundle):", "if False:"),
]


def oracle(code: str, module: ModuleType, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if code == "M72":
        value = module.madelung_continuity_residual(
            np.ones((5, 5)) * 2, np.zeros((5, 5)), previous_density=np.ones((5, 5)), dt=0.25
        )
        assert value == pytest.approx(4.0)
    elif code == "M73":
        density = np.ones((5, 5))
        density[2, 2] = 5
        weight = module.bohmian_edge_weight(density)
        assert np.all(weight > 0) and np.all(weight <= 1) and np.any(weight < 1)
    elif code == "M74":
        result = module.rectify_anamorphic_dioptric_optics(np.zeros((8, 8, 3)))
        expected = ((1.5168 + 0.0042 / 0.650**2) - (1.5168 + 0.0042 / 0.532**2)) / (
            1.5168 + 0.0042 / 0.532**2
        )
        assert result.delta_red == pytest.approx(expected)
    elif code == "M75":
        result = module.apply_acoustic_doppler_shift(np.ones(100), 48000, 10)
        assert result.doppler_factor == pytest.approx(343 / 333)
    elif code == "M76":
        kx, _ = module._wavenumbers(7, 7)
        assert kx[-1] == pytest.approx(2 * np.pi * 3 / 7)
    elif code == "M77":
        angles: list[float] = []

        def capture(patch: npt.NDArray[np.float64], angle: float) -> npt.NDArray[np.float64]:
            angles.append(angle)
            return patch

        with monkeypatch.context() as patcher:
            patcher.setattr(module, "_rotate", capture)
            module.rectify_stroboscopic_wagon_wheel(
                np.ones((8, 8)), angular_velocity_rad_s=200, fps=24
            )
        assert np.mean(angles) == pytest.approx(0, abs=1e-12)
    elif code == "M78":
        with monkeypatch.context() as patcher:
            patcher.setattr(
                module,
                "solve_binocular_vergence",
                lambda *a: SimpleNamespace(
                    violations_before=("operator_fault",), is_consistent=False
                ),
            )
            shot = module.CinemaShot(
                eyes=(
                    np.array([-31.5, 0.0, 0.0]),
                    np.array([31.5, 0.0, 0.0]),
                    np.array([0.0, 0.0, 500.0]),
                )
            )
            report = module.VeritasRealityAuditor(laws=(2,)).audit_and_heal_cinema_shot(shot)
        assert "operator_fault" in report.details_after[2]
    elif code == "M79":
        result = module.compile_and_heal_real_media_shot(
            root, input_frames=np.zeros((2, 8, 8, 3)), input_audio_pcm=np.zeros(100), mux_mp4=False
        )
        decoded = np.stack([module._read_png_rgb(Path(p)) for p in result.png_paths])
        assert result.output_frames_content_sha256 == hashlib.sha256(decoded.tobytes()).hexdigest()
    else:
        token = [7]

        def fault(stage: Path) -> None:
            if code == "M80":
                token[0] = 8
            else:
                path = stage / "dialogue.wav"
                path.write_bytes(path.read_bytes()[:-1])

        req = module.VeritasSpinePublishRequest(
            "p",
            "r",
            "s",
            "a",
            7,
            root,
            root / "published",
            input_frames_rgb=np.zeros((2, 8, 8, 3)),
            input_audio_pcm=np.zeros(100),
        )
        refused = False
        try:
            module.VeritasStagedSpineAdapter(lambda: token[0], fault).execute(req)
        except module.VeritasSpineVerificationError:
            refused = True
        assert refused and not (root / "published").exists()


@pytest.mark.parametrize("code,original,old,new", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_internal_source_mutant_killed(
    code: str,
    original: ModuleType,
    old: str,
    new: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle(code, original, tmp_path / "baseline", monkeypatch)
    assert original.__file__ is not None
    source = Path(original.__file__).read_text()
    assert source.count(old) == 1, "mutation site must be unique"
    name = f"nexus_ai_agent.creative.veritas._isolated_{code}"
    spec = importlib.util.spec_from_file_location(name, original.__file__)
    assert spec is not None
    mutated = importlib.util.module_from_spec(spec)
    # Register only this new Python module. Never purge numpy/PIL/scipy or
    # reload their C extensions during a coverage run.
    monkeypatch.setitem(sys.modules, name, mutated)
    exec(compile(source.replace(old, new), original.__file__, "exec"), mutated.__dict__)
    with pytest.raises(AssertionError):
        oracle(code, mutated, tmp_path / "mutant", monkeypatch)


# Twenty-five additional equation mutants: three each for Laws 1--5 and two
# each for Laws 6--10. Every site is unique and has an independent numeric oracle.
CLASSICAL_MUTATIONS = [
    (
        "M82",
        hand,
        "2.0 - 2.0 * np.cos(np.pi * k / ray_length)",
        "2.0 + 2.0 * np.cos(np.pi * k / ray_length)",
    ),
    ("M83", hand, "TENDON_RATIO = 2.0 / 3.0", "TENDON_RATIO = 1.0 / 3.0"),
    ("M84", hand, "-np.sin(theta),", "np.sin(theta),"),
    ("M85", ocular, "return vector / norm", "return vector * norm"),
    (
        "M86",
        ocular,
        "10.0 * tau**3 - 15.0 * tau**4 + 6.0 * tau**5",
        "10.0 * tau**3 + 15.0 * tau**4 + 6.0 * tau**5",
    ),
    ("M87", ocular, "np.exp(-magnitude / theta_0_deg)", "np.exp(magnitude / theta_0_deg)"),
    (
        "M88",
        mirror,
        "np.eye(3, dtype=np.float64) - 2.0 * np.outer(n, n)",
        "np.eye(3, dtype=np.float64) + 2.0 * np.outer(n, n)",
    ),
    ("M89", mirror, "matrix[:3, 3] = -2.0 * offset * n", "matrix[:3, 3] = 2.0 * offset * n"),
    ("M90", mirror, "np.arctan2(n2, n1)", "np.arctan2(n1, n2)"),
    (
        "M91",
        shadow,
        "scale * np.eye(4, dtype=np.float64) - np.outer(source, coefficients)",
        "scale * np.eye(4, dtype=np.float64) + np.outer(source, coefficients)",
    ),
    ("M92", shadow, "projected[:, :3] / w[:, None]", "projected[:, :3] * w[:, None]"),
    ("M93", shadow, "(light_distance - occluder_height)", "(light_distance + occluder_height)"),
    ("M94", se3, "a = np.sin(theta) / theta", "a = -np.sin(theta) / theta"),
    ("M95", se3, "b = (1.0 - np.cos(theta)) / theta**2", "b = (1.0 + np.cos(theta)) / theta**2"),
    (
        "M96",
        se3,
        "factor = theta / (2.0 * np.sin(theta))",
        "factor = -theta / (2.0 * np.sin(theta))",
    ),
    ("M97", typography, "shifted = points - centroid", "shifted = points + centroid"),
    ("M98", typography, "abs(determinant) ** (-1.0 / 3.0)", "abs(determinant) ** (1.0 / 3.0)"),
    (
        "M99",
        mechanics,
        "0.5 * np.sum(mass * np.sum(vel**2, axis=1))",
        "1.5 * np.sum(mass * np.sum(vel**2, axis=1))",
    ),
    (
        "M100",
        mechanics,
        "acceleration = force / mass[:, None]",
        "acceleration = force * mass[:, None]",
    ),
    (
        "M101",
        cloth,
        "np.linalg.norm(pts - closest, axis=1) - radius",
        "np.linalg.norm(pts - closest, axis=1) + radius",
    ),
    ("M102", cloth, "((pts - a) @ segment) / length_sq", "((pts - a) @ segment) * length_sq"),
    (
        "M103",
        thermo,
        "entropy = -np.sum(terms, axis=(-2, -1))",
        "entropy = np.sum(terms, axis=(-2, -1))",
    ),
    ("M104", thermo, "np.exp(-diffusivity * 4.0", "np.exp(diffusivity * 4.0"),
    (
        "M105",
        face,
        "np.maximum(corrected[:, AU6], required)",
        "np.minimum(corrected[:, AU6], required)",
    ),
    (
        "M106",
        face,
        "np.minimum(corrected[:, AU26], corrected[:, AU25])",
        "np.maximum(corrected[:, AU26], corrected[:, AU25])",
    ),
]


def classical_oracle(code: str, module: ModuleType) -> None:
    if code == "M82":
        np.testing.assert_allclose(module.spectral_laplacian_signature(3), [0, 1, 3], atol=1e-12)
    elif code == "M83":
        skeleton = module.canonical_hand_skeleton()
        angles = skeleton.joint_angles.copy()
        ray = module.CANONICAL_RAYS[1]
        angles[ray[2], 0] = 60
        projected = module._project_angles(angles)
        assert projected[ray[3], 0] == pytest.approx(40)
    elif code == "M84":
        np.testing.assert_allclose(module._segment_direction(30, 0), [0, np.sqrt(3) / 2, -0.5])
    elif code == "M85":
        np.testing.assert_allclose(module._unit(np.array([3.0, 4.0, 0.0])), [0.6, 0.8, 0])
    elif code == "M86":
        result = module.synthesize_saccade_vor_trajectory(10, 0.1)
        assert result.eye_angle_deg[-1] == pytest.approx(10)
    elif code == "M87":
        result = module.synthesize_saccade_vor_trajectory(10, 0.1)
        assert result.peak_velocity_deg_s == pytest.approx(
            module.V_MAX_DEG_S * (1 - np.exp(-10 / module.THETA_0_DEG))
        )
    elif code in ("M88", "M89"):
        matrix = module.compute_householder_reflection_pose(np.array([0.0, 0.0, 1.0]), -2.0)
        # z=3 reflects to z=1 across z=2; unit x/y and orientation reversal.
        np.testing.assert_allclose(matrix @ np.array([1.0, 2.0, 3.0, 1.0]), [1, 2, 1, 1])
    elif code == "M90":
        result = module.compute_fresnel_reflectance_rgb(0, 1, 1.5)
        assert result.brewster_deg == pytest.approx(np.degrees(np.arctan(1.5)))
    elif code in ("M91", "M92"):
        matrix = module.compute_shadow_projection_matrix(
            np.array([0.0, 0.0, 5.0, 1.0]), np.array([0.0, 0.0, 1.0, 0.0])
        )
        points = module.project_vertices_to_shadow(matrix, np.array([[1.0, 0.0, 1.0]]))
        np.testing.assert_allclose(points, [[1.25, 0, 0]])
    elif code == "M93":
        assert module.penumbra_radius(1, 0.1, 3) == pytest.approx(0.05)
    elif code in ("M94", "M95"):
        matrix = module._so3_exp(np.array([0.0, 0.0, np.pi / 3]))
        expected = np.array([[0.5, -np.sqrt(3) / 2, 0], [np.sqrt(3) / 2, 0.5, 0], [0, 0, 1]])
        np.testing.assert_allclose(matrix, expected, atol=1e-12)
    elif code == "M96":
        rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
        np.testing.assert_allclose(module._so3_log(rotation), [0, 0, np.pi / 2])
    elif code == "M97":
        normalized, _ = module._normalise_points(
            np.array([[1.0, 1.0], [3.0, 1.0], [3.0, 3.0], [1.0, 3.0]])
        )
        np.testing.assert_allclose(normalized.mean(axis=0), [0, 0], atol=1e-12)
    elif code == "M98":
        result = module._to_sl3(np.diag([2.0, 2.0, 2.0]))
        assert np.linalg.det(result) == pytest.approx(1.0)
    elif code == "M99":
        assert module.kinetic_energy(np.array([[3.0, 4.0, 0.0]]), np.array([2.0])) == pytest.approx(
            25
        )
    elif code == "M100":
        p, v = module.stormer_verlet_step(
            np.zeros((1, 3)), np.zeros((1, 3)), np.array([2.0]), np.array([[4.0, 0.0, 0.0]]), 0.5
        )
        np.testing.assert_allclose(p, [[0.25, 0, 0]])
        np.testing.assert_allclose(v, [[1.0, 0, 0]])
    elif code in ("M101", "M102"):
        distance = module.capsule_sdf(
            np.array([[1.0, 1.0, 0.0]]), np.zeros(3), np.array([2.0, 0.0, 0.0]), 0.5
        )
        np.testing.assert_allclose(distance, [0.5])
    elif code == "M103":
        assert module.spatial_entropy(np.ones((2, 2))) == pytest.approx(np.log(4))
    elif code == "M104":
        y, _ = np.indices((8, 8))
        field = 1 + 0.2 * np.cos(2 * np.pi * y / 8)
        result = module.fourier_advection_diffusion(field, (0.0, 0.0), 0.1, 1.0, 1.0)
        expected = 1 + 0.2 * np.exp(-0.1 * (2 * np.pi / 8) ** 2) * np.cos(2 * np.pi * y / 8)
        np.testing.assert_allclose(result, expected, atol=1e-12)
    elif code == "M105":
        au = np.zeros((1, 26))
        au[0, 11] = 0.9
        result = module.enforce_facs_synergy_manifold(au)
        assert result.action_units[0, 5] == pytest.approx(0.4)
    else:
        au = np.zeros((1, 26))
        au[0, 25] = 0.8
        au[0, 24] = 0.2
        result = module.enforce_facs_synergy_manifold(au)
        assert result.action_units[0, 25] == pytest.approx(0.2)


@pytest.mark.parametrize(
    "code,original,old,new", CLASSICAL_MUTATIONS, ids=[m[0] for m in CLASSICAL_MUTATIONS]
)
def test_classical_source_mutant_killed(
    code: str,
    original: ModuleType,
    old: str,
    new: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classical_oracle(code, original)
    assert original.__file__ is not None
    source = Path(original.__file__).read_text()
    assert source.count(old) == 1, "mutation site must be unique"
    name = f"nexus_ai_agent.creative.veritas._isolated_{code}"
    spec = importlib.util.spec_from_file_location(name, original.__file__)
    assert spec is not None
    mutated = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mutated)
    exec(compile(source.replace(old, new), original.__file__, "exec"), mutated.__dict__)
    with pytest.raises(AssertionError):
        classical_oracle(code, mutated)


CLOSURE_MUTATIONS = [
    (
        "M107",
        omnia,
        "means[1] / np.maximum(means[[0, 2]], 1e-12)",
        "np.maximum(means[[0, 2]], 1e-12) / means[1]",
    ),
    (
        "M108",
        omnia,
        "pixels[..., 0][edge] = pixels[..., 1][edge]",
        "pixels[..., 0][edge] = pixels[..., 0][edge]",
    ),
    (
        "M109",
        omnia,
        "pixels[..., 2][edge] = pixels[..., 1][edge]",
        "pixels[..., 2][edge] = pixels[..., 2][edge]",
    ),
    (
        "M110",
        omnia,
        "np.ceil(abs(angular_velocity_rad_s) / (0.85 * np.pi * fps))",
        "np.floor(abs(angular_velocity_rad_s) / (0.85 * np.pi * fps))",
    ),
    ("M111", omnia, "output_fps = fps * factor", "output_fps = fps + factor"),
    (
        "M112",
        omnia,
        "timestamps = np.arange(count, dtype=np.float64) / output_fps",
        "timestamps = np.arange(count, dtype=np.float64) / fps",
    ),
    (
        "M113",
        omnia,
        "phases = angular_velocity_rad_s * timestamps",
        "phases = -angular_velocity_rad_s * timestamps",
    ),
    (
        "M114",
        omnia,
        "np.linspace(-0.5 * exposure, 0.5 * exposure, samples)",
        "np.linspace(0.0, exposure, samples)",
    ),
    (
        "M115",
        omnia,
        "_rotate(patch, float(phase + angular_velocity_rad_s * offset))",
        "patch.copy()",
    ),
    ("M116", omnia, "count = source_frame_count * factor", "count = source_frame_count"),
    (
        "M117",
        compiler,
        (
            "if len(leaves) == 1:\n        return leaves[0]\n"
            "    split = 1 << ((len(leaves) - 1).bit_length() - 1)"
        ),
        "if len(leaves) == 1:\n        return leaves[0]\n    split = 1",
    ),
    (
        "M118",
        compiler,
        (
            'return hashlib.sha256(b"\\x00" + '
            "json.dumps(payload, sort_keys=True).encode()).hexdigest()"
        ),
        (
            'return hashlib.sha256(b"\\x01" + '
            "json.dumps(payload, sort_keys=True).encode()).hexdigest()"
        ),
    ),
    (
        "M119",
        spine,
        'return hashlib.sha256(b"\\x01" + child + bytes.fromhex(next(cursor))).digest()',
        'return hashlib.sha256(b"\\x00" + child + bytes.fromhex(next(cursor))).digest()',
    ),
    (
        "M120",
        auditor,
        "if recheck.pre_audit_violations or recheck.post_heal_violations:",
        "if False:",
    ),
    ("M121", quantum, "drho_dt = (rho - rho_prev) / dt", "drho_dt = (rho - rho_prev) * dt"),
    (
        "M122",
        quantum,
        "-(hbar_eff**2 / 2.0) * _laplacian(amplitude) / amplitude",
        "(hbar_eff**2 / 2.0) * _laplacian(amplitude) / amplitude",
    ),
    (
        "M123",
        quantum,
        "flux = [rho * component for component in gradients]",
        "flux = [component / rho for component in gradients]",
    ),
    (
        "M124",
        omnia,
        "ux_sol = ux_hat - correction * kxx[None, :, :]",
        "ux_sol = ux_hat + correction * kxx[None, :, :]",
    ),
    (
        "M125",
        omnia,
        "return float(np.sqrt(np.mean(field**2)))",
        "return float(np.sqrt(np.mean(field)))",
    ),
    ("M126", omnia, "source = freqs / factor", "source = freqs * factor"),
    (
        "M127",
        omnia,
        "healed_pucker[rounded] = np.maximum(healed_pucker[rounded], required_pucker[rounded])",
        "healed_lips[rounded] = np.maximum(healed_pucker[rounded], required_pucker[rounded])",
    ),
    (
        "M128",
        face,
        "np.maximum(corrected[:, AU1], 0.5 * prosody)",
        "np.maximum(corrected[:, AU1], 0.2 * prosody)",
    ),
    ("M129", hand, "= ratios * scale", "= ratios / scale"),
    (
        "M130",
        mechanics,
        "normal_impulse = -(1.0 + restitution)",
        "normal_impulse = -(1.0 - restitution)",
    ),
    (
        "M131",
        cloth,
        "np.add.at(predicted, edge[:, 0], -0.5 * correction)",
        "np.add.at(predicted, edge[:, 0], 0.5 * correction)",
    ),
    ("M132", typography, "top * (1.0 - wy) + bottom * wy", "top * (1.0 - wy) - bottom * wy"),
    (
        "M133",
        se3,
        "twist[3:] = twist[3:] + acceleration * dt",
        "twist[3:] = twist[3:] - acceleration * dt",
    ),
    ("M134", thermo, "phase = -2.0j * np.pi", "phase = 2.0j * np.pi"),
    (
        "M135",
        face,
        "np.exp(-(local[opening] - tau_down_s) / tau_up_s)",
        "np.exp((local[opening] - tau_down_s) / tau_up_s)",
    ),
    (
        "M136",
        mirror,
        "unpolarised = 0.5 * (reflectance_s + reflectance_p)",
        "unpolarised = (reflectance_s + reflectance_p)",
    ),
    (
        "M137",
        shadow,
        "ao = 1.0 - np.exp(-heights / ao_height)",
        "ao = 1.0 - np.exp(heights / ao_height)",
    ),
]


def closure_oracle(code: str, module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    if code == "M107":
        np.testing.assert_allclose(
            module._reference_green_gains(np.array([1.02, 1.0, 0.98])), [1 / 1.02, 1 / 0.98]
        )
    elif code in ("M108", "M109"):
        image = np.random.default_rng(64).random((8, 8, 3))
        result = module.calibrate_catadioptric_optics(image)
        decoded = np.rint(result.healed_image * 255) / 255
        assert omnia._fringe_metric(decoded) <= 1e-6
    elif code in {f"M{i}" for i in range(110, 117)}:
        patch = np.zeros((8, 8))
        patch[1:7, 4:6] = 1
        sequence = module.synthesize_rotary_subframes(patch, angular_velocity_rad_s=200, fps=24)
        assert sequence.frames.shape == (8, 8, 8)
        assert sequence.output_fps == 96
        np.testing.assert_allclose(sequence.timestamps_s, np.arange(8) / 96)
        np.testing.assert_allclose(sequence.phases_rad, 200 * np.arange(8) / 96)
        assert abs(np.mean(sequence.exposure_offsets_s)) < 1e-12
        expected = np.mean(
            np.stack(
                [
                    omnia._rotate(patch, float(200 / 96 + 200 * offset))
                    for offset in np.linspace(-0.5 / 192, 0.5 / 192, 16)
                ]
            ),
            axis=0,
        )
        np.testing.assert_allclose(sequence.frames[1], expected, atol=1e-12)
    elif code == "M117":
        leaves = [hashlib.sha256(bytes([i])).hexdigest() for i in range(3)]
        left = hashlib.sha256(
            b"\x01" + bytes.fromhex(leaves[0]) + bytes.fromhex(leaves[1])
        ).digest()
        expected = hashlib.sha256(b"\x01" + left + bytes.fromhex(leaves[2])).hexdigest()
        assert module._merkle_root(leaves) == expected
    elif code == "M118":
        assert module._leaf_digest({}) == hashlib.sha256(b"\x00{}").hexdigest()
    elif code == "M119":
        left, right = hashlib.sha256(b"left").digest(), hashlib.sha256(b"right").digest()
        root = hashlib.sha256(b"\x01" + left + right).hexdigest()
        assert module._verify_inclusion(left, 0, 2, [right.hex()], root)
    elif code == "M120":
        # A permanently bad physics kernel must not produce a certified state.
        with monkeypatch.context() as patcher:
            patcher.setattr(
                module, "calibrate_catadioptric_optics", omnia.rectify_anamorphic_dioptric_optics
            )
            shot = module.CinemaShot(anamorphic=np.random.default_rng(3).random((8, 8, 3)))
            refused = False
            try:
                module.VeritasRealityAuditor().heal_cinema_shot(shot)
            except module.VeritasHealingRefusalError:
                refused = True
        assert refused
    elif code == "M121":
        oracle("M72", module, Path("unused"), monkeypatch)
    elif code == "M122":
        density = np.ones((5, 5))
        density[2, 2] = 4
        potential = module.bohmian_quantum_potential(density)
        assert potential[2, 2] == pytest.approx(1.0, abs=1e-11)
    elif code == "M123":
        y, x = np.indices((5, 5), dtype=float)
        phase = x * x + y * y
        gradients = np.gradient(phase)
        divergence = sum(
            np.gradient(2 * component, axis=axis) for axis, component in enumerate(gradients)
        )
        expected = np.sqrt(np.mean(divergence**2))
        assert module.madelung_continuity_residual(np.ones((5, 5)) * 2, phase) == pytest.approx(
            expected
        )
    elif code == "M124":
        result = module.enforce_helmholtz_fluid_conservation(
            np.random.default_rng(8).normal(size=(2, 7, 7, 2)), np.ones((2, 7, 7))
        )
        assert result.divergence_after < 1e-12
    elif code == "M125":
        assert module._l2(np.array([1.0, 3.0])) == pytest.approx(np.sqrt(5))
    elif code == "M126":
        signal = np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)
        shifted = module.apply_acoustic_doppler_shift(signal, 48000, 10).shifted_signal
        assert np.argmax(np.abs(np.fft.rfft(shifted))) == 453
    elif code == "M127":
        result = module.audit_and_heal_viseme_formant_sync(
            np.ones(4),
            np.full(4, 0.2),
            f1_hz=np.full(4, 700.0),
            au18_lip_pucker=np.zeros(4),
            f2_hz=np.full(4, 800.0),
        )
        np.testing.assert_allclose(result.au20_healed, 0.2)
        assert result.au18_lip_pucker_healed is not None
        np.testing.assert_allclose(result.au18_lip_pucker_healed, 0.48)
    elif code == "M128":
        result = module.enforce_facs_synergy_manifold(
            np.zeros((1, 26)), f0_envelope=np.array([0.8])
        )
        assert result.action_units[0, 0] == pytest.approx(0.4)
    elif code == "M129":
        original = hand.canonical_hand_skeleton().bone_lengths
        result = module._project_lengths(original)
        for ray in hand.CANONICAL_RAYS:
            assert sum(result[list(ray[2:])]) == pytest.approx(sum(original[list(ray[2:])]))
    elif code == "M130":
        result = module.resolve_rigid_body_collision(
            np.array([[0.0, 0.0, 0.0], [0.5, 0.0, 0.0]]),
            np.array([[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]]),
            np.ones(2),
            np.full(2, 0.6),
            restitution=0.5,
        )
        np.testing.assert_allclose(result.velocities[:, 0], [-0.5, 0.5])
    elif code == "M131":
        result = module.step_xpbd_cloth_simulation(
            np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]),
            np.array([[-1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]),
            np.array([[0.0, 1.0]]),
            dt=0.1,
            gravity=(0.0, 0.0, 0.0),
            compliance=0.0,
            iterations=1,
        )
        assert np.linalg.norm(result.positions[1] - result.positions[0]) == pytest.approx(1.1)
    elif code == "M132":
        result = module._sample_bilinear(
            np.array([[0.0, 1.0], [2.0, 3.0]]), np.array([0.5]), np.array([0.5])
        )
        np.testing.assert_allclose(result, [1.5])
    elif code == "M133":
        state = module.ObjectState(np.eye(4), np.zeros(6), np.ones(3), 1.0)
        result = module.propagate_occluded_object(state, 0.5)
        assert result[-1].twist[5] == pytest.approx(-9.80665 * 0.5)
    elif code == "M134":
        _, x = np.indices((8, 8))
        field = 1 + 0.2 * np.cos(2 * np.pi * x / 8)
        result = module.fourier_advection_diffusion(field, (0.0, 1.0), 0.0, 1.0, 1.0)
        np.testing.assert_allclose(result, np.roll(field, 1, axis=1), atol=1e-12)
    elif code == "M135":
        result = module.synthesize_asymmetric_blink_curve(0.4)
        assert result.closure[50] == pytest.approx(np.exp(-1))
    elif code == "M136":
        result = module.compute_fresnel_reflectance_rgb(0, 1, 1.5)
        np.testing.assert_allclose(result.reflectance_unpolarised, [0.04])
    else:
        field = module.synthesize_coherent_cast_shadows(
            np.array([[0.0, 0.0, 0.1]]),
            np.array([0.0, 0.0, 5.0, 1.0]),
            np.array([0.0, 0.0, 1.0, 0.0]),
        )
        np.testing.assert_allclose(field.contact_ao, [1 - np.exp(-0.1 / 0.15)])


@pytest.mark.parametrize(
    "code,original,old,new", CLOSURE_MUTATIONS, ids=[m[0] for m in CLOSURE_MUTATIONS]
)
def test_closure_source_mutant_killed(
    code: str, original: ModuleType, old: str, new: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    closure_oracle(code, original, monkeypatch)
    assert original.__file__ is not None
    source = Path(original.__file__).read_text()
    assert source.count(old) == 1, "mutation site must be unique"
    name = f"nexus_ai_agent.creative.veritas._isolated_{code}"
    spec = importlib.util.spec_from_file_location(name, original.__file__)
    assert spec is not None
    mutated = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mutated)
    exec(compile(source.replace(old, new), original.__file__, "exec"), mutated.__dict__)
    with pytest.raises(AssertionError):
        closure_oracle(code, mutated, monkeypatch)
