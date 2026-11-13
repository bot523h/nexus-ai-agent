"""M72--M106 source/operator mutants, isolated without unloading C extensions.

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
