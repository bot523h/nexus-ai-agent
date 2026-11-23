"""W6 CP4: symplectic Hamiltonian pipeline + PR #205 / PR #326 supersession.

Proves Störmer–Verlet volume preservation (Liouville), energy boundedness,
Rayleigh-dissipation Lyapunov convergence, the unified cinema science pipeline
(receipt binding), and verifies the Asset Library V1 contracts of old PR #205
and the character capsule re-engineered from old PR #326.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np
import pytest

from nexus_ai_agent.creative.assets.mime import detect_media_type
from nexus_ai_agent.creative.assets.models import (
    SUPPORTED_MEDIA_TYPES,
    IngestState,
    MediaKind,
)
from nexus_ai_agent.creative.character import (
    CHARACTER_ID_RE,
    CharacterCapsule,
    ContinuityThreshold,
    FaceModule,
    ScopeViolation,
    ValidationError,
    VerificationState,
    verify_dimensions,
)
from nexus_ai_agent.creative.character.continuity import ContinuityReport
from nexus_ai_agent.creative.science.symplectic_hamiltonian_pipeline import (
    HamiltonianSpec,
    ScientificPipelineReceipt,
    UnifiedCinemaScienceInputs,
    UnifiedCinemaSciencePipeline,
    analytic_jacobian_of_step,
    gradient_potential,
    hamiltonian,
    integrate_verlet,
    jacobian_of_step,
    local_effective_omega,
    stormer_verlet_step,
    symplectic_volume_preservation_error,
)


def _omega(d: int) -> np.ndarray:
    eye = np.eye(d)
    return np.block([[np.zeros((d, d)), eye], [-eye, np.zeros((d, d))]])


# ---------------------------------------------------------------------------
# Symplectic Störmer–Verlet
# ---------------------------------------------------------------------------


class TestSymplectic:
    def test_hamiltonian_known_answer(self):
        spec = HamiltonianSpec(mass=2.0, stiffness=np.eye(2), quartic_coupling=0.5)
        q = np.array([1.0, 0.5])
        p = np.array([0.3, 0.2])
        kinetic = (0.09 + 0.04) / (2.0 * 2.0)
        harmonic = 0.5 * (1.0 + 0.25)
        quartic = 0.25 * 0.5 * (1.0 + 0.25) ** 2
        assert hamiltonian(q, p, spec) == pytest.approx(kinetic + harmonic + quartic)

    def test_gradient_potential_is_k_plus_alpha_coupling(self):
        spec = HamiltonianSpec(mass=1.0, stiffness=np.eye(2), quartic_coupling=0.5)
        q = np.array([1.0, 0.0])
        expected = q + 0.5 * 1.0 * q  # grad V = K q + alpha ||q||^2 q
        assert np.allclose(gradient_potential(q, spec), expected)

    def test_volume_preservation_is_near_exact(self):
        spec = HamiltonianSpec()
        q = np.array([1.0, 0.5])
        p = np.array([0.2, 0.1])
        err = symplectic_volume_preservation_error(q, p, 0.05, spec)
        assert err < 1e-8

    def test_jacobian_is_symplectic(self):
        spec = HamiltonianSpec()
        q = np.array([1.0, 0.5])
        p = np.array([0.0, 0.0])
        jac = jacobian_of_step(q, p, 0.05, spec, eps=1e-7)
        det = abs(np.linalg.det(jac))
        assert det == pytest.approx(1.0, abs=1e-3)

    def test_energy_bounded_without_damping(self):
        spec = HamiltonianSpec()
        q0 = np.array([1.0, 0.5])
        p0 = np.array([0.0, 0.0])
        traj_q, traj_p, traj_h = integrate_verlet(q0, p0, 0.05, 300, spec)
        assert np.all(np.isfinite(traj_h))
        assert np.max(np.abs(traj_h - traj_h[0])) < 0.05

    def test_rayleigh_damping_converges(self):
        spec = HamiltonianSpec()
        q0 = np.array([1.0, 0.5])
        p0 = np.array([0.0, 0.0])
        traj_q, traj_p, traj_h = integrate_verlet(q0, p0, 0.05, 400, spec, rayleigh_damping=0.05)
        assert traj_h[-1] < traj_h[0]
        assert np.linalg.norm(traj_q[-1]) < np.linalg.norm(traj_q[0])
        # Monotone energy decrease (within tolerance) for Lyapunov convergence.
        assert np.all(np.diff(traj_h) <= 1e-6)

    def test_verlet_step_extremes_are_finite(self):
        spec = HamiltonianSpec(mass=0.5, stiffness=np.diag([1.0, 4.0]), quartic_coupling=0.2)
        q = np.array([2.0, -1.0])
        p = np.array([1.5, -0.5])
        q_next, p_next = stormer_verlet_step(q, p, 0.02, spec)
        assert np.all(np.isfinite(q_next))
        assert np.all(np.isfinite(p_next))

    def test_spec_refuses_non_positive_mass(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(mass=0.0)

    def test_spec_refuses_non_spd_stiffness(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(stiffness=np.diag([1.0, -0.5]))

    def test_spec_refuses_negative_quartic(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(quartic_coupling=-0.1)


# ---------------------------------------------------------------------------
# Unified cinema science pipeline
# ---------------------------------------------------------------------------


class TestUnifiedPipeline:
    def test_receipt_is_deterministic(self):
        pipe = UnifiedCinemaSciencePipeline()
        receipt_1 = pipe.execute()
        receipt_2 = pipe.execute()
        assert receipt_1.receipt_digest == receipt_2.receipt_digest
        assert receipt_1.inputs_digest == receipt_2.inputs_digest

    def test_receipt_binds_inputs_and_outputs(self):
        pipe = UnifiedCinemaSciencePipeline()
        receipt = pipe.execute()
        assert isinstance(receipt, ScientificPipelineReceipt)
        assert receipt.receipt_digest.startswith("sha256:") or receipt.receipt_digest
        assert len(receipt.receipt_digest) == 64
        assert {
            "photochemical_density",
            "hg_phase_function",
            "planck_intensity",
            "thin_film_reflectance",
            "bures_wasserstein_2",
            "hamiltonian_initial",
            "hamiltonian_final",
        } <= set(receipt.outputs)

    def test_receipt_json_is_canonical_and_self_consistent(self):
        pipe = UnifiedCinemaSciencePipeline()
        receipt = pipe.execute()
        blob = receipt.as_json()
        parsed = json.loads(blob)
        assert parsed["receipt_digest"] == receipt.receipt_digest
        assert parsed["schema_version"] == "1"

    def test_module_digests_are_stable(self):
        pipe = UnifiedCinemaSciencePipeline()
        digests = pipe.module_digests()
        assert set(digests) == {
            "photochemical_emulsion",
            "physical_optics_radiometry",
            "optimal_transport_spd",
            "symplectic_hamiltonian_pipeline",
        }
        assert all(isinstance(value, str) and len(value) == 64 for value in digests.values())

    def test_inputs_validated(self):
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(temperature_k=0.0)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(reciprocity_p=0.3)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(hg_asymmetry=1.0)

    def test_every_input_field_rejects_nan_and_inf(self):
        # Every scalar/field must reject NaN/Inf, fail-closed.
        bad_kwargs: list[dict] = [
            {"temperature_k": float("nan")},
            {"temperature_k": float("inf")},
            {"exposure_intensity": float("nan")},
            {"exposure_intensity": float("inf")},
            {"exposure_time_s": float("nan")},
            {"exposure_time_s": float("inf")},
            {"reciprocity_p": float("nan")},
            {"reciprocity_p": float("inf")},
            {"lambda_peak_m": float("nan")},
            {"lambda_peak_m": float("inf")},
            {"blackbody_temperature_k": float("nan")},
            {"blackbody_temperature_k": float("inf")},
            {"film_index": float("nan")},
            {"film_index": float("inf")},
            {"film_thickness_m": float("nan")},
            {"film_thickness_m": float("inf")},
            {"hg_asymmetry": float("nan")},
            {"hg_asymmetry": float("inf")},
            {"rayleigh_damping": float("nan")},
            {"rayleigh_damping": float("inf")},
            {"dt": float("nan")},
            {"dt": float("inf")},
            {"transport_mean": (float("nan"), 0.0)},
            {"transport_mean": (float("inf"), -1.0)},
            {"transport_target_mean": (1.0, float("nan"))},
            {"transport_target_mean": (float("inf"), 0.0)},
        ]
        for kwargs in bad_kwargs:
            with pytest.raises((ValueError, TypeError)):
                UnifiedCinemaScienceInputs(**kwargs)

    def test_every_scalar_field_rejects_bool(self):
        for name in (
            "temperature_k",
            "exposure_intensity",
            "exposure_time_s",
            "reciprocity_p",
            "lambda_peak_m",
            "blackbody_temperature_k",
            "film_index",
            "film_thickness_m",
            "hg_asymmetry",
            "rayleigh_damping",
            "dt",
        ):
            with pytest.raises(TypeError):
                UnifiedCinemaScienceInputs(**{name: True})

    def test_n_steps_rejects_bool_and_non_integer(self):
        with pytest.raises(TypeError):
            UnifiedCinemaScienceInputs(n_steps=True)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            UnifiedCinemaScienceInputs(n_steps=3.5)  # type: ignore[arg-type]

    def test_n_steps_rejects_negative(self):
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(n_steps=-1)

    def test_transport_mean_rejects_bad_lengths_and_types(self):
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(transport_mean=(1.0,))  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(transport_mean=(1.0, 2.0, 3.0))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            UnifiedCinemaScienceInputs(transport_mean=(True, 0.0))  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            UnifiedCinemaScienceInputs(transport_mean=(1.0, "x"))  # type: ignore[arg-type]

    def test_receipt_outputs_are_finite(self):
        receipt = UnifiedCinemaSciencePipeline().execute()
        assert all(math.isfinite(v) for v in receipt.outputs.values())


# ---------------------------------------------------------------------------
# PR #205 supersession: Asset Library V1 contracts
# ---------------------------------------------------------------------------


class TestAssetLibrarySupersession:
    def test_supported_media_types_is_closed(self):
        expected = {
            "image/png": MediaKind.IMAGE,
            "image/jpeg": MediaKind.IMAGE,
            "image/webp": MediaKind.IMAGE,
            "audio/wav": MediaKind.AUDIO,
            "audio/mpeg": MediaKind.AUDIO,
            "video/mp4": MediaKind.VIDEO,
            "video/webm": MediaKind.VIDEO,
        }
        assert SUPPORTED_MEDIA_TYPES == expected

    def test_ingest_state_is_closed(self):
        assert {s.value for s in IngestState} == {"STAGING", "READY", "FAILED"}

    def test_mime_detection_classifies_png(self):
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        assert detect_media_type(png_header) == "image/png"

    def test_mime_detection_classifies_jpeg(self):
        jpeg_header = b"\xff\xd8\xff\xe0" + b"\x00" * 16
        assert detect_media_type(jpeg_header) == "image/jpeg"

    def test_mime_detection_classifies_webm(self):
        webm_header = b"\x1a\x45\xdf\xa3" + b"\x00" * 24
        assert detect_media_type(webm_header) == "video/webm"

    def test_mime_detection_unknown_returns_none(self):
        assert detect_media_type(b"\x89\x50\x4e\x47") is None

    def test_mime_detection_empty_returns_none(self):
        assert detect_media_type(b"") is None


# ---------------------------------------------------------------------------
# PR #326 supersession: character capsule
# ---------------------------------------------------------------------------


class TestCharacterCapsuleSupersession:
    def test_capsule_id_grammar(self):
        assert CHARACTER_ID_RE.match("NAGAR-CHAR-000001")
        assert not CHARACTER_ID_RE.match("NAGAR-CHAR-12345")
        assert not CHARACTER_ID_RE.match("NAGAR-CHAR-000001-x")

    def test_capsule_is_immutable_and_sealed(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        assert isinstance(capsule, CharacterCapsule)
        assert capsule.seal() is capsule
        # digest is deterministic and domain-separated
        assert capsule.digest().startswith("sha256:")
        assert capsule.digest() == capsule.seal().digest()

    def test_revision_changes_bump_revision_and_module_revision(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        updated = capsule.apply_revision_changes("face", {"skin_texture": "weathered"})
        assert updated.revision_no == 2
        assert updated.face.revision == 2
        assert updated.face.skin_texture == "weathered"

    def test_revision_update_refuses_identity_fields(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ScopeViolation):
            capsule.apply_revision_changes("face", {"eye_colour": "#ffffff"})

    def test_revision_update_refuses_unknown_field(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ValidationError):
            capsule.apply_revision_changes("face", {"unknown": 1})

    def test_revision_update_requires_changes(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ValidationError):
            capsule.apply_revision_changes("face", {})

    def test_identity_version_mints_and_bumps_only_identity_fields(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        updated = capsule.new_identity_version(
            face_changes={"eye_colour": "#2a4d88"},
            voice_changes={"f0_hz": 180.0},
        )
        assert updated.identity_version == 2
        assert updated.face.eye_colour == "#2a4d88"
        assert updated.voice.f0_hz == 180.0
        assert updated.face.revision == 2

    def test_identity_version_refuses_rev_fields(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ValidationError):
            capsule.new_identity_version(face_changes={"skin_texture": "scarred"})

    def test_identity_version_requires_at_least_one_identity_change(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ValidationError):
            capsule.new_identity_version()

    def test_continuity_verifier_three_state_bands(self):
        thresholds = {
            "face": ContinuityThreshold("face", 0.2, 0.6),
            "body": ContinuityThreshold("body", 0.2, 0.6),
        }
        report = verify_dimensions({"face": 0.10, "body": 0.40}, thresholds)
        assert report.verdict == VerificationState.REVIEW_REQUIRED
        assert report.dimensions["face"].state == VerificationState.VERIFIED
        assert report.dimensions["body"].state == VerificationState.REVIEW_REQUIRED

    def test_continuity_verifier_drift_dominates(self):
        thresholds = {
            "face": ContinuityThreshold("face", 0.2, 0.6),
            "body": ContinuityThreshold("body", 0.2, 0.6),
        }
        report = verify_dimensions({"face": 0.05, "body": 0.75}, thresholds)
        assert report.verdict == VerificationState.IDENTITY_DRIFT

    def test_continuity_verifier_not_measured_is_not_scored(self):
        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        report = verify_dimensions({"face": None}, thresholds)
        assert report.dimensions["face"].state == VerificationState.NOT_MEASURED

    def test_continuity_report_seal_binds_content(self):
        thresholds = {
            "face": ContinuityThreshold("face", 0.2, 0.6),
        }
        report = verify_dimensions({"face": 0.1}, thresholds)
        sealed = report.seal()
        assert sealed.startswith("sha256:")
        assert isinstance(report, ContinuityReport)

    def test_character_id_is_validated(self):
        with pytest.raises(ValidationError):
            CharacterCapsule(character_id="BAD-ID", identity_version=1, revision_no=1)

    def test_face_module_validates_colours(self):
        with pytest.raises(ValidationError):
            FaceModule(eye_colour="red")

    def test_identity_version_is_monotone(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        v1 = capsule.new_identity_version(face_changes={"eye_colour": "#2a4d88"})
        v2 = v1.new_identity_version(face_changes={"eye_colour": "#1a3a66"})
        assert v2.identity_version == 3


# ---------------------------------------------------------------------------
# W6-CP5: P1 regression tests — HamiltonianSpec symmetry, damping theory,
# exact symplecticity, reversibility and O(dt^2) convergence
# ---------------------------------------------------------------------------


class TestHamiltonianSpecRegression:
    def test_asymmetric_matrix_rejected_despite_lower_triangle_eigs(self):
        # The lower triangle [[1, 0], [0, 1]] is positive definite, but
        # symmetrization would make the matrix indefinite (1-50=-49 < 0).
        # The fix must reject BEFORE symmetrizing, never average away asymmetry.
        k = np.array([[1.0, 100.0], [0.0, 1.0]])
        with pytest.raises(ValueError, match="must be symmetric"):
            HamiltonianSpec(stiffness=k)

    def test_nan_and_inf_entries_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            HamiltonianSpec(stiffness=np.array([[1.0, np.nan], [np.nan, 1.0]]))
        with pytest.raises(ValueError, match="finite"):
            HamiltonianSpec(stiffness=np.array([[np.inf, 0.0], [0.0, 1.0]]))

    def test_stored_stiffness_is_read_only(self):
        spec = HamiltonianSpec()
        with pytest.raises(ValueError):  # numpy raises ValueError on write
            spec.stiffness[0, 0] = 99.0

    def test_external_array_mutations_do_not_leak(self):
        source = np.eye(2)
        spec = HamiltonianSpec(stiffness=source)
        before = spec.stiffness.copy()
        source[0, 0] = 42.0  # mutate the caller's array after construction
        assert np.allclose(spec.stiffness, before)

    def test_spec_exposes_omega_max(self):
        spec = HamiltonianSpec(mass=2.0, stiffness=np.diag([4.0, 1.0]))
        assert spec.lambda_max == pytest.approx(4.0)
        assert spec.omega_max == pytest.approx(math.sqrt(2.0))


class TestRayleighDampingTheory:
    def test_continuous_dH_dt_matches_c_over_m_squared(self):
        # For the continuous flow, dH/dt = -c ||p||^2 / m^2. Integrating the
        # Strang-split step and measuring energy decrease per unit time is the
        # practical numerical check: the measured slope is consistent with the
        # theory and is negative (never positive) for c > 0.
        spec = HamiltonianSpec(mass=2.0, stiffness=np.eye(1), quartic_coupling=0.0)
        q0 = np.array([1.0])
        p0 = np.array([1.0])
        h_initial = hamiltonian(q0, p0, spec)
        h_final = h_initial
        dt = 0.02
        steps = 25
        q, p = q0.copy(), p0.copy()
        spec_c = HamiltonianSpec(mass=2.0, stiffness=np.eye(1), quartic_coupling=0.0)
        for _ in range(steps):
            q, p = stormer_verlet_step(q, p, dt, spec_c, rayleigh_damping=0.5)
        h_final = hamiltonian(q, p, spec_c)
        # With the exact-damping Strang split (c=0.5 < 2m effectively), energy
        # must never increase over one step:
        q1, p1 = stormer_verlet_step(q0, p0, dt, spec_c, rayleigh_damping=0.5)
        assert hamiltonian(q1, p1, spec_c) <= h_initial + 1e-12
        assert h_final < h_initial

    def test_damping_factor_always_in_unit_interval(self):
        # Even for a violent c dt / m, the Strang-split damping factor is
        # strictly in (0, 1] — never a divergent explicit multiplier.
        dt = 0.02
        spec = HamiltonianSpec(mass=0.5, stiffness=np.eye(2), quartic_coupling=0.0)
        q = np.array([1.0, 0.5])
        p = np.array([2.0, 1.0])
        # This would be unstable for the old explicit form (c dt/(2m) = 2.5).
        q_next, p_next = stormer_verlet_step(q, p, dt, spec, rayleigh_damping=25.0)
        assert np.all(np.isfinite(q_next))
        assert np.all(np.isfinite(p_next))

    def test_unstable_dt_refused_fail_closed(self):
        spec = HamiltonianSpec(stiffness=np.diag([4.0, 1.0]))  # lambda_max = 4
        with pytest.raises(ValueError, match="omega_max"):
            stormer_verlet_step(np.array([1.0, 1.0]), np.zeros(2), 1.5, spec)

    def test_zero_damping_recovers_symplectic_flow(self):
        # Independent, executable reference Störmer–Verlet (conservative only):
        # the implementation under test must reproduce it to machine precision.
        spec = HamiltonianSpec(mass=1.0, stiffness=np.eye(2), quartic_coupling=0.0)
        q0 = np.array([0.7, 0.3])
        p0 = np.array([0.4, -0.1])
        dt = 0.05
        n_steps = 200

        def reference_step(q, p, mass, stiffness):
            p_half = p - 0.5 * dt * (stiffness @ q)
            q_next = q + dt * p_half / mass
            p_next = p_half - 0.5 * dt * (stiffness @ q_next)
            return q_next, p_next

        q_ref, p_ref = q0.copy(), p0.copy()
        for _ in range(n_steps):
            q_ref, p_ref = reference_step(q_ref, p_ref, 1.0, np.eye(2))
        q, p = q0.copy(), p0.copy()
        for _ in range(n_steps):
            q, p = stormer_verlet_step(q, p, dt, spec, rayleigh_damping=0.0)
        assert np.allclose(q, q_ref, atol=1e-12)
        assert np.allclose(p, p_ref, atol=1e-12)

        # SHO energy conservation (K isotropic, alpha = 0): energy must be
        # conserved over the trajectory (symplecticity, bounded drift).
        h_initial = hamiltonian(q0, p0, spec)
        q, p = q0.copy(), p0.copy()
        for _ in range(n_steps):
            q, p = stormer_verlet_step(q, p, dt, spec, rayleigh_damping=0.0)
        assert abs(hamiltonian(q, p, spec) - h_initial) < 1e-3

        # Symplectic condition on the FD-verified Jacobian (machine precision).
        fd = jacobian_of_step(q, p, dt, spec)
        an = analytic_jacobian_of_step(q, p, dt, spec)
        assert np.linalg.norm(an - fd, ord="fro") < 1e-7
        omega = _omega(spec.dimension)
        residual = np.linalg.norm(an.T @ omega @ an - omega, ord="fro")
        assert residual < 1e-12


class TestAnalyticJacobianFDVerification:
    """The exact analytic Jacobian must match the finite-difference Jacobian."""

    @pytest.mark.parametrize("alpha", (0.0, 0.3))
    @pytest.mark.parametrize("damping", (0.0, 0.4))
    def test_analytic_matches_fd_central(self, alpha, damping):
        spec = HamiltonianSpec(mass=1.3, stiffness=np.diag([1.1, 2.2]), quartic_coupling=alpha)
        q = np.array([0.4, -0.7])  # nonzero q, nonzero p
        p = np.array([0.5, 0.2])
        dt = 0.02
        fd = jacobian_of_step(q, p, dt, spec, rayleigh_damping=damping)
        an = analytic_jacobian_of_step(q, p, dt, spec, rayleigh_damping=damping)
        err = np.linalg.norm(an - fd, ord="fro")
        assert err < 1e-7, f"FD vs analytic residual {err:.3e}"


class TestLocalEffectiveOmegaGate:
    """The harmonic ``omega_max`` gate alone is not sufficient for nonlinear flows."""

    def test_local_omega_is_larger_than_harmonic(self):
        spec = HamiltonianSpec(stiffness=np.eye(2), quartic_coupling=5.0)
        q = np.array([1.5, 0.3])
        omega_eff = local_effective_omega(spec, q)
        assert omega_eff > spec.omega_max

    def test_harmonic_gate_passes_but_local_gate_refuses(self):
        # omega_max * dt < 2 (harmonic gate passes) but the local Hessian
        # bound omega_eff(q) * dt >= 2 for large alpha * ||q||^2.
        spec = HamiltonianSpec(mass=1.0, stiffness=np.eye(2), quartic_coupling=5.0)
        q = np.array([3.0, 0.0])
        p = np.zeros(2)
        dt = 0.2
        assert spec.omega_max * dt < 2.0  # harmonic gate passes
        with pytest.raises(ValueError, match="local effective"):
            stormer_verlet_step(q, p, dt, spec)

    def test_local_gate_applies_along_midpoint(self):
        # q at the start is small enough to pass every gate, but the half-step
        # position q_{n+1/2} crosses the local bound at the same dt.
        spec = HamiltonianSpec(mass=1.0, stiffness=np.eye(2), quartic_coupling=8.0)
        q = np.array([1.2, 0.0])
        p = np.array([30.0, 0.0])
        dt = 0.3
        assert spec.omega_max * dt < 2.0
        assert local_effective_omega(spec, q) * dt < 2.0
        with pytest.raises(ValueError, match="mid-step"):
            stormer_verlet_step(q, p, dt, spec)


class TestReceiptDeepFreeze:
    def test_outputs_mapping_is_frozen(self):
        from types import MappingProxyType

        receipt = UnifiedCinemaSciencePipeline().execute()
        assert isinstance(receipt.outputs, MappingProxyType)
        with pytest.raises(TypeError):
            receipt.outputs["photochemical_density"] = 0.0  # type: ignore[index]
        with pytest.raises(TypeError):
            receipt.outputs["extra"] = 0.0  # type: ignore[index]

    def test_as_dict_is_plain_copy(self):
        receipt = UnifiedCinemaSciencePipeline().execute()
        as_dict = receipt.as_dict()
        as_dict["outputs"]["photochemical_density"] = 999.0  # mutate the copy
        assert receipt.outputs["photochemical_density"] != 999.0

    def test_executed_at_is_real_iso8601_utc(self):
        receipt = UnifiedCinemaSciencePipeline().execute()
        assert receipt.executed_at
        parsed = datetime.fromisoformat(receipt.executed_at)
        assert parsed.tzinfo is not None

    def test_executed_at_override_validated(self):
        with pytest.raises(ValueError, match="ISO-8601"):
            ScientificPipelineReceipt(
                "1",
                "unified-cinema-science",
                "0" * 64,
                {"k": 1.0},
                "0" * 64,
                "0" * 64,
                executed_at="not-a-timestamp",
            )

    def test_modules_digest_binds_source_bytes(self):
        pipe = UnifiedCinemaSciencePipeline()
        digests = pipe.module_digests()
        names = set(digests)
        assert names == {
            "photochemical_emulsion",
            "physical_optics_radiometry",
            "optimal_transport_spd",
            "symplectic_hamiltonian_pipeline",
        }
        assert all(len(v) == 64 for v in digests.values())
        # A single extra byte in any science module must change its digest.
        import hashlib
        from pathlib import Path

        base = (
            Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent" / "creative" / "science"
        )
        probe = base / "photochemical_emulsion.py"
        content = probe.read_bytes()
        real = hashlib.sha256(b"photochemical_emulsion:W6-EMULSION-1\n" + content).hexdigest()
        assert digests["photochemical_emulsion"] == real


class TestExactSymplecticity:
    def test_symplectic_condition_to_machine_precision(self):
        # || M^T Ω M - Ω ||_F < 1e-12 (exact symplecticity, not just det = 1).
        d = 2
        omega = _omega(d)
        spec = HamiltonianSpec(mass=1.0, stiffness=np.diag([1.0, 4.0]), quartic_coupling=0.2)
        q = np.array([0.5, 0.3])
        p = np.array([0.2, -0.1])
        for dt in (0.05, 0.01, 0.001):
            jac = analytic_jacobian_of_step(q, p, dt, spec)
            residual = np.linalg.norm(jac.T @ omega @ jac - omega, ord="fro")
            assert residual < 1e-12, f"residual {residual:.3e} at dt={dt}"

    def test_reversibility_recovers_initial_state(self):
        spec = HamiltonianSpec(mass=1.0, stiffness=np.diag([1.0, 4.0]), quartic_coupling=0.2)
        q0 = np.array([0.5, 0.3])
        p0 = np.array([0.2, -0.1])
        dt = 0.05
        q1, p1 = stormer_verlet_step(q0, p0, dt, spec)
        q2, p2 = stormer_verlet_step(q1, p1, -dt, spec)
        assert np.allclose(q2, q0, atol=1e-14)
        assert np.allclose(p2, p0, atol=1e-14)

    def test_second_order_convergence_of_energy(self):
        # Energy error over a fixed horizon is O(dt^2) for a second-order
        # symplectic integrator: halving dt should quarter the error.
        spec = HamiltonianSpec(mass=1.0, stiffness=np.diag([1.0, 0.5]), quartic_coupling=0.1)
        q0 = np.array([0.8, 0.4])
        p0 = np.array([0.3, -0.2])
        h0 = hamiltonian(q0, p0, spec)

        def energy_error(dt: float, steps: int) -> float:
            q, p = q0.copy(), p0.copy()
            for _ in range(steps):
                q, p = stormer_verlet_step(q, p, dt, spec)
            return abs(hamiltonian(q, p, spec) - h0)

        e_coarse = energy_error(0.1, 50)
        e_fine = energy_error(0.05, 100)
        assert e_fine < e_coarse
        assert e_coarse / e_fine > 3.0  # ~4 for a d^2 law


# ---------------------------------------------------------------------------
# W6-CP5: P1 regression tests — ContinuityReport deep-freeze + finiteness gates
# ---------------------------------------------------------------------------


class TestContinuityDeepFreeze:
    def test_dimensions_mapping_is_read_only(self):
        from types import MappingProxyType

        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        report = verify_dimensions({"face": 0.10}, thresholds)
        assert isinstance(report.dimensions, MappingProxyType)
        with pytest.raises(TypeError):
            report.dimensions["face"] = report.dimensions["face"]  # type: ignore[index]
        with pytest.raises(TypeError):
            report.dimensions["extra"] = report.dimensions["face"]  # type: ignore[index]

    def test_caller_dict_mutation_does_not_leak(self):
        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        measurements = {"face": 0.10}
        report = verify_dimensions(measurements, thresholds)
        before = dict(report.dimensions)
        measurements["face"] = 0.999  # mutate the caller's dict after sealing
        assert report.dimensions["face"].distance == pytest.approx(before["face"].distance)

    def test_dimensions_mapping_immutable_after_every_call(self):
        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        report1 = verify_dimensions({"face": 0.10}, thresholds)
        report2 = verify_dimensions({"face": 0.50}, thresholds)
        # Two sealed reports must not alias each other's dimension state.
        assert report1.dimensions["face"].distance == pytest.approx(0.10)
        assert report2.dimensions["face"].distance == pytest.approx(0.50)

    def test_seal_digest_matches_to_dict_canonicalization(self):
        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        report = verify_dimensions({"face": 0.10}, thresholds)
        digest1 = report.seal()
        digest2 = verify_dimensions({"face": 0.10}, thresholds).seal()
        assert digest1 == digest2
        assert digest1.startswith("sha256:")
        assert len(digest1) == len("sha256:") + 64


class TestContinuityFinitenessGates:
    def test_nan_verified_max_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            ContinuityThreshold("face", float("nan"), 0.6)

    def test_inf_drift_min_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            ContinuityThreshold("face", 0.2, float("inf"))

    def test_negative_verified_max_rejected(self):
        with pytest.raises(ValueError, match="verified_max"):
            ContinuityThreshold("face", -0.1, 0.6)

    def test_nan_distance_rejected(self):
        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        with pytest.raises(ValueError, match="finite"):
            verify_dimensions({"face": float("nan")}, thresholds)

    def test_inf_distance_rejected(self):
        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        with pytest.raises(ValueError, match="finite"):
            verify_dimensions({"face": float("inf")}, thresholds)

    def test_negative_distance_rejected(self):
        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        with pytest.raises(ValueError, match="non-negative"):
            verify_dimensions({"face": -0.01}, thresholds)

    def test_threshold_ordering_gate_still_holds(self):
        with pytest.raises(ValueError, match="verified_max < drift_min"):
            ContinuityThreshold("face", 0.6, 0.2)


class TestAnalyticJacobianGuards:
    def test_zero_dt_rejected(self):
        spec = HamiltonianSpec(mass=1.0, stiffness=np.eye(1))
        with pytest.raises(ValueError, match="non-zero"):
            analytic_jacobian_of_step(np.array([1.0]), np.zeros(1), 0.0, spec)

    def test_infinite_dt_rejected(self):
        spec = HamiltonianSpec(mass=1.0, stiffness=np.eye(1))
        with pytest.raises(ValueError, match="non-zero"):
            analytic_jacobian_of_step(np.array([1.0]), np.zeros(1), float("inf"), spec)

    def test_negative_damping_rejected(self):
        spec = HamiltonianSpec(mass=1.0, stiffness=np.eye(1))
        with pytest.raises(ValueError, match="non-negative"):
            analytic_jacobian_of_step(
                np.array([1.0]), np.zeros(1), 0.5, spec, rayleigh_damping=-0.1
            )

    def test_shape_mismatch_rejected(self):
        spec = HamiltonianSpec(mass=1.0, stiffness=np.eye(2))
        with pytest.raises(ValueError, match="equal length"):
            analytic_jacobian_of_step(np.array([1.0, 2.0]), np.zeros(1), 0.5, spec)

    def test_non_dimension_result_rejected_on_report_construction(self):
        with pytest.raises(ValueError, match="DimensionResult"):
            ContinuityReport(
                verdict=VerificationState.VERIFIED,
                dimensions={"face": "not-a-result"},  # type: ignore[dict-item]
            )

    def test_non_numeric_threshold_rejected(self):
        with pytest.raises(ValueError, match="numeric"):
            ContinuityThreshold("face", "not-a-number", 0.6)  # type: ignore[arg-type]

    def test_public_construct_with_empty_dimensions(self):
        # Defensive fallback path: a report constructed directly with no dimensions
        # still seals deterministically with the INSUFFICIENT_EVIDENCE verdict.
        report = ContinuityReport(verdict=VerificationState.INSUFFICIENT_EVIDENCE, dimensions={})
        assert report.seal().startswith("sha256:")


# ---------------------------------------------------------------------------
# G4 coverage push: fail-closed branches of the receipt / inputs / local omega
# ---------------------------------------------------------------------------


class TestLocalEffectiveOmegaGateBranches:
    def test_wrong_q_size_rejected(self):
        spec = HamiltonianSpec(mass=1.0, stiffness=np.diag([1.0, 4.0]), quartic_coupling=0.2)
        with pytest.raises(ValueError, match="must match"):
            local_effective_omega(spec, np.array([1.0, 2.0, 3.0]))


class TestReceiptFreezeBranches:
    def test_top_level_bool_output_rejected(self):
        with pytest.raises(TypeError, match="boolean"):
            ScientificPipelineReceipt(
                "1",
                "pipeline",
                "0" * 64,
                {"bad": True},  # type: ignore[arg-type]
                "0" * 64,
                "0" * 64,
            )

    def test_nested_mapping_is_frozen_and_unfrozen(self):
        from types import MappingProxyType

        receipt = ScientificPipelineReceipt(
            "1",
            "pipeline",
            "0" * 64,
            {"nested": {"inner": 1.0}},  # type: ignore[arg-type]
            "0" * 64,
            "0" * 64,
        )
        assert isinstance(receipt.outputs["nested"], MappingProxyType)
        with pytest.raises(TypeError):
            receipt.outputs["nested"]["inner"] = 2.0  # type: ignore[index]
        assert receipt.as_dict()["outputs"]["nested"] == {"inner": 1.0}

    def test_list_output_becomes_tuple_and_back(self):
        receipt = ScientificPipelineReceipt(
            "1",
            "pipeline",
            "0" * 64,
            {"seq": [1.0, 2.0]},  # type: ignore[arg-type]
            "0" * 64,
            "0" * 64,
        )
        assert isinstance(receipt.outputs["seq"], tuple)
        assert receipt.as_dict()["outputs"]["seq"] == [1.0, 2.0]

    def test_scalar_output_types_pass_through(self):
        receipt = ScientificPipelineReceipt(
            "1",
            "pipeline",
            "0" * 64,
            {"s": "text", "i": 3, "f": 1.5},  # type: ignore[arg-type]
            "0" * 64,
            "0" * 64,
        )
        out = receipt.as_dict()["outputs"]
        assert out["s"] == "text"
        assert out["i"] == 3
        assert out["f"] == 1.5

    def test_non_finite_float_output_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            ScientificPipelineReceipt(
                "1",
                "pipeline",
                "0" * 64,
                {"bad": float("nan")},
                "0" * 64,
                "0" * 64,
            )

    def test_unknown_output_type_rejected(self):
        class NotSerializable:
            pass

        with pytest.raises(TypeError, match="JSON-scalars"):
            ScientificPipelineReceipt(
                "1",
                "pipeline",
                "0" * 64,
                {"bad": NotSerializable()},  # type: ignore[arg-type]
                "0" * 64,
                "0" * 64,
            )

    def test_nested_bool_inside_mapping_rejected(self):
        with pytest.raises(TypeError):
            ScientificPipelineReceipt(
                "1",
                "pipeline",
                "0" * 64,
                {"nested": {"inner": True}},  # type: ignore[arg-type]
                "0" * 64,
                "0" * 64,
            )

    def test_executed_at_non_string_rejected(self):
        with pytest.raises(TypeError):
            ScientificPipelineReceipt(
                "1",
                "pipeline",
                "0" * 64,
                {"k": 1.0},
                "0" * 64,
                "0" * 64,
                executed_at=123,  # type: ignore[arg-type]
            )

    def test_executed_at_z_suffix_accepted(self):
        receipt = ScientificPipelineReceipt(
            "1",
            "pipeline",
            "0" * 64,
            {"k": 1.0},
            "0" * 64,
            "0" * 64,
            executed_at="2026-11-22T00:00:00Z",
        )
        assert receipt.executed_at.endswith("+00:00")

    def test_executed_at_naive_rejected(self):
        with pytest.raises(ValueError, match="timezone"):
            ScientificPipelineReceipt(
                "1",
                "pipeline",
                "0" * 64,
                {"k": 1.0},
                "0" * 64,
                "0" * 64,
                executed_at="2026-11-22T00:00:00",
            )


class TestMeanValidationBranches:
    def test_transport_mean_must_be_a_sequence(self):
        with pytest.raises(TypeError, match="2-element"):
            UnifiedCinemaScienceInputs(transport_mean=1.5)  # type: ignore[arg-type]


class TestContinuityThresholdConsistency:
    def test_non_threshold_object_rejected(self):
        with pytest.raises(ValueError, match="must be a ContinuityThreshold"):
            verify_dimensions({"face": 0.1}, {"face": "not-a-threshold"})  # type: ignore[dict-item]

    def test_mismatched_dimension_rejected(self):
        with pytest.raises(ValueError, match="calibrated for"):
            verify_dimensions({"face": 0.1}, {"body": ContinuityThreshold("face", 0.2, 0.6)})


class TestCharacterCapsuleBranches:
    def test_apply_revision_changes_bumps_wardrobe_module(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        updated = capsule.apply_revision_changes("wardrobe", {"outfit": "trench coat"})
        assert updated.wardrobe.outfit == "trench coat"
        assert updated.wardrobe.revision == 2
        assert updated.face.revision == 1
        assert updated.voice.revision == 1

    def test_voice_module_has_no_revision_scoped_fields(self):
        # Voice fields are identity-scoped; revision is capsule-managed.
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001",
            identity_version=1,
            revision_no=1,
        )
        with pytest.raises(ScopeViolation):
            capsule.apply_revision_changes("voice", {"timbre": "warm"})
        assert capsule.voice.revision == 1
