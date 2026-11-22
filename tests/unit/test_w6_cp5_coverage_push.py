"""W6 CP5: branch-coverage push over the science and character modules.

Targets the exact uncovered lines flagged by the CP4 coverage run; this file
exists so the quality gate can require >=98% coverage on the touched modules.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from nexus_ai_agent.creative.character import (
    CharacterCapsule,
    ContinuityReport,
    ContinuityThreshold,
    FaceModule,
    VerificationState,
    VoiceModule,
    WardrobeModule,
    next_character_id,
    validate_character_id,
    verify_dimensions,
)
from nexus_ai_agent.creative.character.continuity import STATE_PRECEDENCE
from nexus_ai_agent.creative.character.model import (
    CHARACTER_ID_RE,
    ScopeViolation,
    ValidationError,
)
from nexus_ai_agent.creative.science.optimal_transport_spd import (
    bures_wasserstein_2,
    matrix_inverse,
    matrix_square_root,
    mccann_geodesic,
    monge_map_tensor,
    monge_transport_map,
    optimal_transport_distance,
)
from nexus_ai_agent.creative.science.photochemical_emulsion import (
    EmulsionLayer,
    arrhenius_gamma,
    beer_lambert_rgb,
    canonical_dir_chain,
    dir_film_state,
    dir_reaction_diffusion,
    hurt_driffield_slope_log10,
    hurter_driffield_density,
    schwarzschild_reciprocity,
)
from nexus_ai_agent.creative.science.physical_optics_radiometry import (
    brester_angle_deg,
    brester_reflectance,
    critical_angle_deg,
    film_materials_from_air,
    fresnel_amplitudes,
    fresnel_transmittance_s,
    henyey_greenstein_norm,
    henyey_greenstein_phase_function,
    planck_blackbody_spectral_radiance,
    thin_film_phase,
    thin_film_reflectance,
    volumetric_radiative_transfer,
    wien_temperature,
)
from nexus_ai_agent.creative.science.symplectic_hamiltonian_pipeline import (
    HamiltonianSpec,
    UnifiedCinemaScienceInputs,
    UnifiedCinemaSciencePipeline,
    default_reference_inputs,
    hamiltonian,
    integrate_verlet,
    jacobian_of_step,
    stormer_verlet_step,
    symplectic_volume_preservation_error,
)

# ---------------------------------------------------------------------------
# Character: model validations
# ---------------------------------------------------------------------------


class TestCapsuleValidationBranches:
    def test_validate_character_id_rejects_junk(self):
        with pytest.raises(ValidationError):
            validate_character_id("not-a-character-id")

    def test_validate_character_id_matches_grammar(self):
        assert CHARACTER_ID_RE.match("NAGAR-CHAR-099999")

    def test_face_module_rejects_bad_colour(self):
        with pytest.raises(ValidationError):
            FaceModule(skin_colour="red")

    def test_face_module_rejects_bad_spacing_ratio(self):
        with pytest.raises(ValidationError):
            FaceModule(eye_spacing_ratio=0.01)
        with pytest.raises(ValidationError):
            FaceModule(eye_spacing_ratio=0.5)

    def test_face_module_rejects_bad_jaw_ratio(self):
        with pytest.raises(ValidationError):
            FaceModule(jaw_width_ratio=0.1)

    def test_face_module_rejects_bad_texture(self):
        with pytest.raises(ValidationError):
            FaceModule(skin_texture="smooth-plus")

    def test_face_module_rejects_zero_revision(self):
        with pytest.raises(ValidationError):
            FaceModule(revision=0)

    def test_face_scope_of_unknown_field(self):
        with pytest.raises(ValidationError):
            FaceModule().scope_of("nope")

    def test_voice_module_rejects_bad_f0(self):
        with pytest.raises(ValidationError):
            VoiceModule(f0_hz=10.0)
        with pytest.raises(ValidationError):
            VoiceModule(f0_hz=1000.0)

    def test_voice_module_rejects_bad_timbre(self):
        with pytest.raises(ValidationError):
            VoiceModule(timbre="growly")

    def test_voice_module_rejects_zero_revision(self):
        with pytest.raises(ValidationError):
            VoiceModule(revision=0)

    def test_voice_scope_of_unknown_field(self):
        with pytest.raises(ValidationError):
            VoiceModule().scope_of("nope")

    def test_wardrobe_module_rejects_bad_outfit(self):
        with pytest.raises(ValidationError):
            WardrobeModule(outfit="")
        with pytest.raises(ValidationError):
            WardrobeModule(outfit="x" * 129)

    def test_wardrobe_module_rejects_bad_era(self):
        with pytest.raises(ValidationError):
            WardrobeModule(era="")
        with pytest.raises(ValidationError):
            WardrobeModule(era="x" * 65)

    def test_wardrobe_module_rejects_bad_silhouette(self):
        with pytest.raises(ValidationError):
            WardrobeModule(silhouette="slim")

    def test_wardrobe_module_rejects_zero_revision(self):
        with pytest.raises(ValidationError):
            WardrobeModule(revision=0)

    def test_wardrobe_scope_of_unknown_field(self):
        with pytest.raises(ValidationError):
            WardrobeModule().scope_of("nope")

    def test_wardrobe_known_fields_property(self):
        assert WardrobeModule().known_fields == frozenset(
            {"outfit", "era", "silhouette", "revision"}
        )

    def test_module_accessor_rejects_unknown_name(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=1
        )
        with pytest.raises(ValidationError):
            capsule.module("pet")

    def test_module_accessor_returns_modules(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=1
        )
        assert isinstance(capsule.module("face"), FaceModule)
        assert isinstance(capsule.module("voice"), VoiceModule)
        assert isinstance(capsule.module("wardrobe"), WardrobeModule)

    def test_capsule_rejects_bad_identity_version(self):
        with pytest.raises(ValidationError):
            CharacterCapsule(character_id="NAGAR-CHAR-000001", identity_version=0, revision_no=1)

    def test_capsule_rejects_bad_revision(self):
        with pytest.raises(ValidationError):
            CharacterCapsule(character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=0)

    def test_capsule_rejects_bad_id(self):
        with pytest.raises(ValidationError):
            CharacterCapsule(character_id="BAD", identity_version=1, revision_no=1)

    def test_expected_revision_mismatch_refused(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=1
        )
        with pytest.raises(ValidationError):
            capsule.apply_revision_changes(
                "face", {"skin_texture": "weathered"}, expected_revision=99
            )

    def test_revision_changes_on_voice_and_wardrobe(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=1
        )
        # voice fields are all IDENTITY-scoped (except managed revision), so
        # revision changes on the wardrobe cover the elif/else line paths.
        with_wardrobe = capsule.apply_revision_changes("wardrobe", {"era": "2026"})
        assert with_wardrobe.wardrobe.era == "2026"
        assert with_wardrobe.wardrobe.revision == 2
        # a voice IDENTITY field via apply_revision_changes must still refuse:
        with pytest.raises(ScopeViolation):
            capsule.apply_revision_changes("voice", {"f0_hz": 180.0})

    def test_revision_managed_field_refused(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=1
        )
        with pytest.raises(ValidationError):
            capsule.apply_revision_changes("face", {"revision": 42})

    def test_identity_version_revision_field_refused(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=1
        )
        with pytest.raises(ValidationError):
            capsule.new_identity_version(face_changes={"revision": 2})

    def test_identity_version_unknown_field_refused(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=1
        )
        with pytest.raises(ValidationError):
            capsule.new_identity_version(face_changes={"nope": 1})

    def test_next_character_id_never_reuses(self):
        assert next_character_id(["NAGAR-CHAR-000001", "NAGAR-CHAR-000007"]) == "NAGAR-CHAR-000008"
        assert next_character_id(set()) == "NAGAR-CHAR-000001"
        assert next_character_id(["junk"]) == "NAGAR-CHAR-000001"

    def test_sealed_capsule_digest_is_stable(self):
        capsule = CharacterCapsule(
            character_id="NAGAR-CHAR-000001", identity_version=1, revision_no=1
        )
        assert capsule.to_dict()["schema"] == "character-capsule/1"
        assert capsule.seal().digest() == capsule.digest()


# ---------------------------------------------------------------------------
# Character: continuity validations
# ---------------------------------------------------------------------------


class TestContinuityValidations:
    def test_threshold_requires_nonempty_dimension(self):
        with pytest.raises(ValueError):
            ContinuityThreshold("", 0.1, 0.2)

    def test_threshold_requires_band_order(self):
        with pytest.raises(ValueError):
            ContinuityThreshold("face", 0.5, 0.2)

    def test_verify_requires_measurements(self):
        with pytest.raises(ValueError):
            verify_dimensions({}, {"face": ContinuityThreshold("face", 0.2, 0.6)})

    def test_verify_requires_threshold_for_dimension(self):
        with pytest.raises(ValueError):
            verify_dimensions({"face": 0.1}, {"voice": ContinuityThreshold("voice", 0.2, 0.6)})

    def test_verify_rejects_non_number_distance(self):
        with pytest.raises(ValueError):
            verify_dimensions(
                {"face": "soon"},
                {"face": ContinuityThreshold("face", 0.2, 0.6)},  # type: ignore[dict-item]
            )

    def test_verify_rejects_negative_distance(self):
        with pytest.raises(ValueError):
            verify_dimensions({"face": -0.1}, {"face": ContinuityThreshold("face", 0.2, 0.6)})

    def test_not_measured_only_yields_not_measured_or_insufficient(self):
        report = verify_dimensions(
            {"face": None, "voice": None},
            {
                "face": ContinuityThreshold("face", 0.2, 0.6),
                "voice": ContinuityThreshold("voice", 0.2, 0.6),
            },
        )
        # All NOT_MEASURED: precedence picks NOT_MEASURED over INSUFFICIENT_EVIDENCE.
        assert report.verdict == VerificationState.NOT_MEASURED
        assert all(r.state == VerificationState.NOT_MEASURED for r in report.dimensions.values())

    def test_report_seal_and_dict_are_stable(self):
        thresholds = {"face": ContinuityThreshold("face", 0.2, 0.6)}
        report = verify_dimensions({"face": 0.1}, thresholds, note="x")
        assert isinstance(report, ContinuityReport)
        assert report.to_dict()["verdict"] == report.verdict.value
        assert report.to_dict()["note"] == "x"
        assert report.seal().startswith("sha256:")

    def test_precedence_order_is_drift_first(self):
        assert STATE_PRECEDENCE[0] == VerificationState.IDENTITY_DRIFT


# ---------------------------------------------------------------------------
# Science: photochemical validations
# ---------------------------------------------------------------------------


class TestPhotochemicalValidations:
    def test_layer_rejects_bad_bounds(self):
        with pytest.raises(ValueError):
            EmulsionLayer("test-layer", d_min=2.0, d_max=1.0)

    def test_layer_rejects_nan_log10_h0(self):
        with pytest.raises(ValueError):
            EmulsionLayer("test-layer", log10_h0=float("nan"))

    def test_layer_rejects_inf_log10_h0(self):
        with pytest.raises(ValueError):
            EmulsionLayer("test-layer", log10_h0=float("inf"))

    def test_layer_rejects_nonphysical_gamma(self):
        with pytest.raises(ValueError):
            EmulsionLayer("test-layer", gamma_ref=0.0)

    def test_layer_rejects_nonphysical_t_ref(self):
        with pytest.raises(ValueError):
            EmulsionLayer("test-layer", t_ref=0.0)

    def test_layer_rejects_unfinite_ea(self):
        with pytest.raises(ValueError):
            EmulsionLayer("test-layer", ea=float("inf"))

    def test_layer_midpoint_property(self):
        layer = EmulsionLayer("test-layer")
        assert layer.midpoint == pytest.approx((layer.d_min + layer.d_max) / 2.0)

    def test_arrhenius_rejects_nonpositive_temperature(self):
        with pytest.raises(ValueError):
            arrhenius_gamma(2.0, 42000.0, 293.15, 0.0)

    def test_arrhenius_rejects_nonpositive_gamma(self):
        with pytest.raises(ValueError):
            arrhenius_gamma(0.0, 42000.0, 293.15, 293.15)

    def test_arrhenius_rejects_bad_r(self):
        with pytest.raises(ValueError):
            arrhenius_gamma(2.0, 42000.0, 293.15, 293.15, r=0.0)

    def test_arrhenius_rejects_unfinite_ea(self):
        with pytest.raises(ValueError):
            arrhenius_gamma(2.0, float("inf"), 293.15, 293.15)

    def test_density_rejects_none_layer(self):
        with pytest.raises(TypeError):
            hurter_driffield_density(None, 1.0, 293.15)  # type: ignore[arg-type]

    def test_density_rejects_nonfinite(self):
        with pytest.raises(ValueError):
            hurter_driffield_density(EmulsionLayer("test-layer"), float("nan"), 293.15)

    def test_slope_log10_rejects_nonfinite(self):
        with pytest.raises(ValueError):
            hurt_driffield_slope_log10(EmulsionLayer("test-layer"), float("nan"), 293.15)

    def test_reciprocity_rejects_nonfinite_p(self):
        with pytest.raises(ValueError):
            schwarzschild_reciprocity(11.0, 100.0, float("nan"))

    def test_dir_rejects_nonfinite_kappa_and_bounds(self):
        with pytest.raises(ValueError):
            dir_reaction_diffusion(np.ones(3), float("nan"), 0.0, 3.0)
        with pytest.raises(ValueError):
            dir_reaction_diffusion(np.ones(3), 0.2, float("inf"), 3.0)
        with pytest.raises(ValueError):
            dir_reaction_diffusion(np.ones(3), 0.2, 0.0, float("nan"))

    def test_dir_rejects_oversized_grid(self):
        with pytest.raises(ValueError):
            dir_reaction_diffusion(np.zeros((3, 3, 3)), 0.2, 0.0, 3.0)

    def test_dir_state_2d_square(self):
        grid = np.ones((5, 5))
        grid[2, 2] = 5.0
        state = dir_film_state(grid, 0.1, 0.0, 3.0)
        assert state.laplacian.shape == (5, 5)
        assert state.laplacian[2, 2] < 0.0

    def test_dir_state_empty_refused(self):
        with pytest.raises(ValueError):
            dir_film_state(np.array([]), 0.2, 0.0, 3.0)

    def test_beer_lambert_rejects_bad_matrix_shape(self):
        with pytest.raises(ValueError):
            beer_lambert_rgb(np.ones((2, 2)), np.array([0.1, 0.2, 0.3]))

    def test_beer_lambert_rejects_bad_density_shape(self):
        with pytest.raises(ValueError):
            beer_lambert_rgb(np.eye(3), np.array([0.1, 0.2]))

    def test_beer_lambert_rejects_nonfinite(self):
        with pytest.raises(ValueError):
            beer_lambert_rgb(np.eye(3), np.array([float("nan"), 0.2, 0.3]))

    def test_beer_lambert_rejects_negative_extinction(self):
        with pytest.raises(ValueError):
            beer_lambert_rgb(
                np.array([[-0.1, 0.0, 0.0], [0.0, 0.5, 0.0], [0.0, 0.0, 0.4]]),
                np.array([0.1, 0.1, 0.1]),
            )

    def test_beer_lambert_rejects_negative_density(self):
        with pytest.raises(ValueError):
            beer_lambert_rgb(np.eye(3), np.array([-0.1, 0.1, 0.1]))

    def test_canonical_chain_wrong_shape_refused(self):
        grid = np.ones(4)
        with pytest.raises(ValueError):
            canonical_dir_chain(grid, 0.1, np.eye(3), 0.0, 3.0)

    def test_canonical_chain_happy_path(self):
        T = canonical_dir_chain(np.array([0.1, 0.2, 0.3]), 0.1, np.eye(3), 0.0, 3.0)
        assert T.shape == (3,)
        assert np.all(T > 0.0)


# ---------------------------------------------------------------------------
# Science: physical optics validations
# ---------------------------------------------------------------------------


class TestPhysicalOpticsValidations:
    def test_planck_rejects_bad_lambda(self):
        with pytest.raises(ValueError):
            planck_blackbody_spectral_radiance(0.0, 5778.0)

    def test_planck_rejects_bad_temperature(self):
        with pytest.raises(ValueError):
            planck_blackbody_spectral_radiance(550e-9, 0.0)

    def test_wien_temperature_rejects_bad_lambda(self):
        with pytest.raises(ValueError):
            wien_temperature(0.0)

    def test_critical_angle_none_for_air_side(self):
        assert critical_angle_deg(1.0, 1.5) is None

    def test_critical_angle_none_for_equal_indices(self):
        assert critical_angle_deg(1.5, 1.5) is None

    def test_fresnel_rejects_bad_indices(self):
        with pytest.raises(ValueError):
            fresnel_amplitudes(0.0, 1.5, 45.0)

    def test_fresnel_rejects_bad_angle(self):
        with pytest.raises(ValueError):
            fresnel_amplitudes(1.5, 1.5, 90.0)

    def test_brester_rejects_bad_indices(self):
        with pytest.raises(ValueError):
            brester_angle_deg(0.0, 1.5)

    def test_brester_reflectance_vanishes_p(self):
        r_s, r_p = brester_reflectance(1.0, 1.5)
        assert r_p < 1e-6

    def test_transmittance_s_reflectance_balance(self):
        assert fresnel_transmittance_s(1.0, 1.5, 30.0) == pytest.approx(
            1.0 - fresnel_amplitudes(1.0, 1.5, 30.0).R_s
        )

    def test_thin_film_phase_rejects_bad_lambda(self):
        with pytest.raises(ValueError):
            thin_film_phase(0.0, 1.5, 1e-6, 0.0)

    def test_thin_film_phase_rejects_bad_index(self):
        with pytest.raises(ValueError):
            thin_film_phase(550e-9, 0.0, 1e-6, 0.0)

    def test_thin_film_phase_rejects_negative_thickness(self):
        with pytest.raises(ValueError):
            thin_film_phase(550e-9, 1.5, -1e-6, 0.0)

    def test_thin_film_phase_rejects_bad_angle(self):
        with pytest.raises(ValueError):
            thin_film_phase(550e-9, 1.5, 1e-6, 95.0)

    def test_thin_film_reflectance_rejects_nonfinite(self):
        with pytest.raises(ValueError):
            thin_film_reflectance(float("nan"), 0.1, 550e-9, 1.5, 1e-7)

    def test_thin_film_reflectance_rejects_oversized_amplitude(self):
        with pytest.raises(ValueError):
            thin_film_reflectance(1.0, 0.1, 550e-9, 1.5, 1e-7)

    def test_thin_film_reflectance_rejects_degenerate_denominator(self):
        # theta=0, n=1.5, d such that 2*delta = pi exactly → every term cancels.
        # amplitude 0.99999999: denominator = (1 - r^2)^2 = 4.4e-16 < 1e-15.
        lam = 550e-9
        d = (math.pi / 2.0) * lam / (2.0 * math.pi * 1.5)
        with pytest.raises(ValueError):
            thin_film_reflectance(0.99999999, 0.99999999, lam, 1.5, d, 0.0)

    def test_film_materials_rejects_bad_indices(self):
        with pytest.raises(ValueError):
            film_materials_from_air(0.0, 1.5, 1.0, 30.0)

    def test_film_materials_rejects_tir(self):
        with pytest.raises(ValueError):
            film_materials_from_air(1.6, 1.0, 1.01, 85.0)

    def test_volumetric_rejects_nonfinite(self):
        with pytest.raises(ValueError):
            volumetric_radiative_transfer(float("nan"), 0.5, 0.4)

    def test_volumetric_rejects_negative_tau(self):
        with pytest.raises(ValueError):
            volumetric_radiative_transfer(1.0, 0.5, -0.1)

    def test_hg_phase_rejects_bad_cos(self):
        with pytest.raises(ValueError):
            henyey_greenstein_phase_function(1.5, 0.4)

    def test_hg_phase_rejects_bad_g(self):
        with pytest.raises(ValueError):
            henyey_greenstein_phase_function(0.0, 1.0)

    def test_hg_norm_rejects_bad_g(self):
        with pytest.raises(ValueError):
            henyey_greenstein_norm(1.0)

    def test_planck_returns_zero_for_extreme_x(self):
        # x > 700: the radiance is effectively zero (guard against overflow).
        assert planck_blackbody_spectral_radiance(1e-9, 100.0) == 0.0

    def test_volumetric_rejects_nonfinite_source(self):
        with pytest.raises(ValueError):
            volumetric_radiative_transfer(1.0, float("nan"), 0.4)


# ---------------------------------------------------------------------------
# Science: optimal transport validations
# ---------------------------------------------------------------------------


class TestOptimalTransportValidations:
    def test_mean_must_be_nonempty(self):
        with pytest.raises(ValueError):
            bures_wasserstein_2(np.array([]), np.eye(1), np.array([0.0]), np.eye(1))

    def test_mean_must_be_finite(self):
        with pytest.raises(ValueError):
            bures_wasserstein_2(np.array([float("nan")]), np.eye(1), np.array([0.0]), np.eye(1))

    def test_covariance_must_be_square(self):
        with pytest.raises(ValueError):
            matrix_square_root(np.ones((2, 3)))

    def test_covariance_must_be_nonempty(self):
        with pytest.raises(ValueError):
            matrix_square_root(np.zeros((0, 0)))

    def test_covariance_must_be_finite(self):
        with pytest.raises(ValueError):
            matrix_square_root(np.array([[float("nan")]]))

    def test_covariance_must_be_spd(self):
        with pytest.raises(ValueError):
            matrix_square_root(np.zeros((2, 2)))

    def test_covariance_must_be_symmetric(self):
        # Asymmetric but still positive-definite after symmetrization.
        with pytest.raises(ValueError):
            matrix_square_root(np.array([[2.0, 0.1], [0.9, 2.0]]))

    def test_covariance_must_have_matching_mean_dimension(self):
        with pytest.raises(ValueError):
            bures_wasserstein_2(np.array([0.0]), np.eye(2), np.array([0.0, 0.0]), np.eye(2))

    def test_covariances_must_match_shape(self):
        with pytest.raises(ValueError):
            bures_wasserstein_2(
                np.array([0.0, 0.0]), np.eye(2), np.array([0.0, 0.0, 0.0]), np.eye(3)
            )

    def test_monge_transport_shape_checks(self):
        with pytest.raises(ValueError):
            monge_transport_map(np.array([0.0]), np.eye(2), np.array([0.0, 0.0]), np.eye(2))
        with pytest.raises(ValueError):
            monge_transport_map(
                np.array([0.0, 0.0]), np.eye(2), np.array([0.0, 0.0, 0.0]), np.eye(3)
            )

    def test_monge_map_tensor_shape_checks(self):
        with pytest.raises(ValueError):
            monge_map_tensor(np.array([0.0, 0.0]), np.eye(3), np.array([0.0]))
        map_fn = monge_map_tensor(np.array([0.0]), np.eye(1), np.array([0.0]))
        with pytest.raises(ValueError):
            map_fn(np.array([0.0, 0.0]))

    def test_mccann_rejects_bad_t_and_bad_shape(self):
        with pytest.raises(ValueError):
            mccann_geodesic(1.5, np.array([0.0]), np.eye(1), np.array([1.0]), np.eye(1))
        with pytest.raises(ValueError):
            mccann_geodesic(0.5, np.array([0.0, 0.0]), np.eye(2), np.array([0.0, 0.0]), np.eye(3))

    def test_optimal_transport_distance_is_square_root(self):
        m1 = np.zeros(2)
        m2 = np.array([3.0, 4.0])
        c = np.eye(2)
        distance = optimal_transport_distance(m1, c, m2, c)
        assert distance == pytest.approx(math.sqrt(bures_wasserstein_2(m1, c, m2, c)))

    def test_matrix_inverse_is_spd_mirror(self):
        c = np.array([[2.0, 0.5], [0.5, 1.5]])
        inv = matrix_inverse(c)
        assert np.allclose(c @ inv, np.eye(2), atol=1e-8)


# ---------------------------------------------------------------------------
# Science: symplectic pipeline validations
# ---------------------------------------------------------------------------


class TestSymplecticValidations:
    def test_spec_rejects_bad_mass(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(mass=float("nan"))

    def test_spec_rejects_non_square_stiffness(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(stiffness=np.ones((2, 3)))

    def test_spec_rejects_negative_eigenvalue(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(stiffness=np.diag([1.0, -0.5]))

    def test_spec_rejects_bad_quartic(self):
        with pytest.raises(ValueError):
            HamiltonianSpec(quartic_coupling=float("nan"))

    def test_hamiltonian_rejects_dimension_mismatch(self):
        spec = HamiltonianSpec()
        with pytest.raises(ValueError):
            hamiltonian(np.array([1.0, 0.5, 0.0]), np.array([0.1, 0.0]), spec)

    def test_verlet_rejects_bad_dt(self):
        spec = HamiltonianSpec()
        with pytest.raises(ValueError):
            stormer_verlet_step(np.array([1.0]), np.array([0.0]), 0.0, spec)

    def test_verlet_rejects_bad_damping(self):
        spec = HamiltonianSpec()
        with pytest.raises(ValueError):
            stormer_verlet_step(np.array([1.0]), np.array([0.0]), 0.05, spec, rayleigh_damping=-0.1)

    def test_verlet_rejects_mismatched_shapes(self):
        spec = HamiltonianSpec()
        with pytest.raises(ValueError):
            stormer_verlet_step(np.array([1.0, 0.0]), np.array([0.0]), 0.05, spec)

    def test_jacobian_rejects_bad_eps(self):
        spec = HamiltonianSpec()
        with pytest.raises(ValueError):
            jacobian_of_step(np.array([1.0]), np.array([0.0]), 0.05, spec, eps=0.0)

    def test_volume_preservation_rejects_bad_q_shape(self):
        spec = HamiltonianSpec()
        with pytest.raises(ValueError):
            symplectic_volume_preservation_error(
                np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0]), 0.05, spec
            )

    def test_integrate_rejects_negative_steps(self):
        spec = HamiltonianSpec()
        with pytest.raises(ValueError):
            integrate_verlet(np.array([1.0]), np.array([0.0]), 0.05, -1, spec)

    def test_integrate_returns_finite_trajectory(self):
        spec = HamiltonianSpec(stiffness=np.eye(1))
        q, p, h = integrate_verlet(np.array([1.0]), np.array([0.0]), 0.05, 10, spec)
        assert q.shape == (11, 1)
        assert h.shape == (11,)

    def test_receipt_as_dict_as_json_stability(self):
        pipe = UnifiedCinemaSciencePipeline()
        receipt = pipe.execute()
        payload = receipt.as_dict()
        assert payload["pipeline"] == "unified-cinema-science"
        assert json.loads(receipt.as_json())["receipt_digest"] == receipt.receipt_digest

    def test_pipeline_receipt_bound_to_inputs(self):
        pipe = UnifiedCinemaSciencePipeline()
        receipt = pipe.execute(UnifiedCinemaScienceInputs(temperature_k=310.0))
        assert (
            receipt.outputs["photochemical_density"]
            != pipe.execute().outputs["photochemical_density"]
        )

    def test_pipeline_digests_stable(self):
        pipe = UnifiedCinemaSciencePipeline()
        first = pipe.module_digests()
        second = pipe.module_digests()
        assert first == second

    def test_default_reference_inputs_valid(self):
        assert default_reference_inputs().temperature_k == pytest.approx(293.15)

    def test_pipeline_rejects_unphysical_inputs(self):
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(temperature_k=0.0)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(exposure_intensity=0.0)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(exposure_time_s=0.0)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(reciprocity_p=0.4)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(lambda_peak_m=2e-3)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(blackbody_temperature_k=0.0)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(film_index=0.0)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(film_thickness_m=-1e-9)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(hg_asymmetry=1.5)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(transport_mean=(1.0, 2.0, 3.0))
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(rayleigh_damping=-0.1)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(n_steps=-1)
        with pytest.raises(ValueError):
            UnifiedCinemaScienceInputs(dt=0.0)
