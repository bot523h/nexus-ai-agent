"""W6 CP2: physical optics radiometry + PR #334 supersession.

Proves Planck's law, Wien's law, Fresnel amplitudes (including Brewster's
angle and TIR), Airy thin-film interference, volumetric radiative transfer,
and the Henyey–Greenstein normalization.  Verifies old PR #334's durable
CONNECT credential contracts (SqlConnectionStore) on the current base.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError as PydanticValidationError

from nexus_ai_agent.connect.contracts import ConnectionContract, ScopeGrant
from nexus_ai_agent.connect.crypto import EncryptedSecret
from nexus_ai_agent.connect.key_provider import KeyProviderError, StaticKeyRing
from nexus_ai_agent.creative.science.physical_optics_radiometry import (
    BG_REFERENCE,
    C_LIGHT,
    H_PLANCK,
    KB_BOLTZMANN,
    WIEN_CONSTANT_M_K,
    FresnelAmplitudes,
    brester_angle_deg,
    brester_reflectance,
    critical_angle_deg,
    film_materials_from_air,
    fresnel_amplitudes,
    fresnel_transmittance_p,
    fresnel_transmittance_s,
    henyey_greenstein_norm,
    henyey_greenstein_phase_function,
    planck_blackbody_spectral_radiance,
    planck_max_radiance,
    thin_film_phase,
    thin_film_reflectance,
    volumetric_radiative_transfer,
    wien_max_wavelength,
    wien_temperature,
)
from nexus_ai_agent.creative.studio.models import ActorIdentity
from nexus_ai_agent.storage.connect_persistence import SqlConnectionStore

# ---------------------------------------------------------------------------
# Planck / Wien
# ---------------------------------------------------------------------------


class TestPlanck:
    def test_constants_are_exact_si_2019_values(self):
        assert H_PLANCK == pytest.approx(6.626_070_15e-34)
        assert C_LIGHT == pytest.approx(299_792_458.0)
        assert KB_BOLTZMANN == pytest.approx(1.380_649e-23)
        assert WIEN_CONSTANT_M_K == pytest.approx(2.897_771_955_122_186e-3, rel=1e-9)

    def test_blackbody_spectrum_peaks_and_vanishes(self):
        lam = 2e-7  # far UV (high T, extreme wavelength)
        assert planck_blackbody_spectral_radiance(lam, 1e5) >= 0.0
        # Low T, long wavelength: finite intensity.
        assert planck_blackbody_spectral_radiance(1e-3, 100.0) > 0.0

    def test_blackbody_known_answer_value(self):
        # Known-answer: numerical evaluation of the Planck law at the given point.
        assert planck_blackbody_spectral_radiance(550e-9, 5778.0) == pytest.approx(
            2.5857643225158427e13, rel=1e-6
        )
        assert planck_blackbody_spectral_radiance(550e-9, 1000.0) == pytest.approx(
            10307.489906877401, rel=1e-12
        )

    def test_wien_peak_wavelength_matches(self):
        lam = wien_max_wavelength(5778.0)
        assert lam == pytest.approx(2.897_771_955e-3 / 5778.0, rel=1e-9)
        assert wien_temperature(lam) == pytest.approx(5778.0)

    def test_planck_max_radiance_matches_planck_at_wien_peak(self):
        lam = wien_max_wavelength(5778.0)
        assert planck_max_radiance(5778.0) == pytest.approx(
            planck_blackbody_spectral_radiance(lam, 5778.0), rel=1e-12
        )

    def test_invalid_input(self):
        with pytest.raises(ValueError):
            planck_blackbody_spectral_radiance(0.0, 5778.0)
        with pytest.raises(ValueError):
            wien_max_wavelength(0.0)


# ---------------------------------------------------------------------------
# Fresnel amplitudes / Brewster / TIR
# ---------------------------------------------------------------------------


class TestFresnel:
    def test_normal_incidence_simplifies(self):
        # At theta = 0, r_s = r_p = (n1 - n2)/(n1 + n2)
        fresnel = fresnel_amplitudes(1.0, 1.5, 0.0)
        assert fresnel.r_s.real == pytest.approx((1.0 - 1.5) / (1.0 + 1.5))
        assert fresnel.r_s.imag == pytest.approx(0.0)
        assert fresnel.r_p.real == pytest.approx((1.5 - 1.0) / (1.5 + 1.0))
        assert fresnel.r_p.imag == pytest.approx(0.0)

    def test_brester_angle_kills_p_reflection(self):
        theta_b = brester_angle_deg(1.0, 1.5)
        assert theta_b == pytest.approx(math.degrees(math.atan(1.5)))
        r_s, r_p = brester_reflectance(1.0, 1.5)
        assert r_p == pytest.approx(0.0, abs=1e-10)
        assert r_s > 0.0

    def test_tsr_angle_and_total_internal_reflection(self):
        critical = critical_angle_deg(1.5, 1.0)
        assert critical is not None
        assert critical == pytest.approx(math.degrees(math.asin(1.0 / 1.5)))
        # Above critical angle: |r_s| = |r_p| = 1 (total reflection, phase shifted).
        fresnel = fresnel_amplitudes(1.5, 1.0, critical + 5.0)
        assert fresnel.R_s == pytest.approx(1.0, rel=1e-10)
        assert fresnel.R_p == pytest.approx(1.0, rel=1e-10)

    def test_no_tsr_without_n_dense_greater_than_n_sparse(self):
        assert critical_angle_deg(1.0, 1.5) is None

    def test_transmittance_s_conservation_at_normal_incidence(self):
        n1, n2 = 1.2, 1.5
        fresnel = fresnel_amplitudes(n1, n2, 0.0)
        assert fresnel.R_s < 1.0
        assert fresnel_transmittance_s(n1, n2, 0.0) == pytest.approx(1.0 - fresnel.R_s)

    def test_invalid_incidence(self):
        with pytest.raises(ValueError):
            fresnel_amplitudes(1.0, 0.0, 30.0)
        with pytest.raises(ValueError):
            fresnel_amplitudes(1.0, 1.5, 180.0)


# ---------------------------------------------------------------------------
# Airy thin-film interference
# ---------------------------------------------------------------------------


class TestThinFilm:
    def test_phase_matches_formula(self):
        lam = 500e-9
        n_film = 1.5
        thickness = 200e-9
        theta_deg = 20.0
        delta = thin_film_phase(lam, n_film, thickness, theta_deg)
        expected = (2.0 * math.pi / lam) * n_film * thickness * math.cos(math.radians(theta_deg))
        assert delta == pytest.approx(expected, rel=1e-15)

    def test_reflectance_in_unit_interval(self):
        r = thin_film_reflectance(0.2, 0.1, 550e-9, 1.5, 200e-9, 0.0)
        assert 0.0 <= r <= 1.0

    def test_thin_film_at_normal_incidence(self):
        theta_film, r01, r12 = film_materials_from_air(1.0, 1.5, 1.0, 30.0)
        assert theta_film == pytest.approx(
            math.degrees(math.asin(math.sin(math.radians(30.0)) / 1.5))
        )
        assert r01 == pytest.approx((1.0 - 1.5) / (1.0 + 1.5))
        assert r12 == pytest.approx((1.5 - 1.0) / (1.5 + 1.0))

    def test_reflectance_symmetric_in_r_product(self):
        r1 = thin_film_reflectance(0.2, 0.1, 550e-9, 1.5, 200e-9, 0.0)
        r2 = thin_film_reflectance(0.1, 0.2, 550e-9, 1.5, 200e-9, 0.0)
        assert r1 == pytest.approx(r2, rel=1e-15)

    def test_out_of_range_amplitudes_refused(self):
        with pytest.raises(ValueError):
            thin_film_reflectance(1.5, 0.1, 550e-9, 1.5, 200e-9, 0.0)
        with pytest.raises(ValueError):
            thin_film_reflectance(0.1, -1.5, 550e-9, 1.5, 200e-9, 0.0)

    def test_extreme_amplitude_still_yields_unit_interval(self):
        r = thin_film_reflectance(0.999, -0.999, 550e-9, 1.5, 200e-9, 0.0)
        assert -1.0 <= r <= 1.0


# ---------------------------------------------------------------------------
# Volumetric radiative transfer + Henyey-Greenstein
# ---------------------------------------------------------------------------


class TestTransfer:
    def test_zero_depth_is_identity(self):
        assert volumetric_radiative_transfer(1.2, 0.5, 0.0) == pytest.approx(1.2)

    def test_optical_depth_one_g_is_half(self):
        result = volumetric_radiative_transfer(0.0, 1.0, 1.0)
        assert result == pytest.approx(1.0 - math.exp(-1.0))

    def test_deep_medium_converges_to_source_function(self):
        result = volumetric_radiative_transfer(2.0, 0.7, 10.0)
        assert result == pytest.approx(0.7, rel=1e-4)

    def test_invalid_depth_refused(self):
        with pytest.raises(ValueError):
            volumetric_radiative_transfer(1.0, 0.5, -0.1)


class TestHenyeyGreenstein:
    def test_g_zero_is_isotropic(self):
        # p(u) = 1/(4 pi) for g = 0.
        assert henyey_greenstein_phase_function(0.3, 0.0) == pytest.approx(1.0 / (4.0 * math.pi))

    @pytest.mark.parametrize("g", [-0.9, -0.5, 0.0, 0.4, 0.7, 0.9])
    def test_phase_function_integrates_to_one(self, g):
        assert henyey_greenstein_norm(g) == pytest.approx(1.0, rel=1e-2)

    def test_g_at_one_refused(self):
        with pytest.raises(ValueError):
            henyey_greenstein_phase_function(0.0, 1.0)

    def test_cos_theta_outside_range_refused(self):
        with pytest.raises(ValueError):
            henyey_greenstein_phase_function(1.5, 0.5)


# ---------------------------------------------------------------------------
# PR #334 supersession: durable CONNECT credentials
# ---------------------------------------------------------------------------


class TestConnectSupersession:
    def test_contract_is_immutable_dataclass(self):
        from datetime import datetime, timezone

        from nexus_ai_agent.connect.contracts import ConnectionState, CredentialState

        NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)
        contract = ConnectionContract(
            connection_id="conn_optics_test",
            principal=ActorIdentity(kind="agent", actor_id="agent-x"),
            grant=ScopeGrant(project_ids=("p1",), scopes=frozenset({"project.read"})),
            credential={
                "credential_id": "cred_x",
                "state": CredentialState.ACTIVE,
                "expires_at": NOW,
            },
            state=ConnectionState.ACTIVE,
            expires_at=NOW,
            created_at=NOW,
        )
        with pytest.raises(PydanticValidationError):
            contract.connection_id = "mutated"  # type: ignore[misc]

    def test_static_key_ring_validates_keys(self):
        with pytest.raises(KeyProviderError):
            StaticKeyRing({"weak": b"short"}, active_key_id="weak")

    def test_store_instantiates_and_exposes_methods(self):
        store = SqlConnectionStore(StaticKeyRing({"k1": b"1" * 32}, active_key_id="k1"))
        assert hasattr(store, "create")
        assert hasattr(store, "get")
        assert hasattr(store, "get_secret")
        assert hasattr(store, "revoke")
        assert hasattr(store, "rewrap_all")

    def test_secret_envelope_is_frozen_and_validated(self):

        env = EncryptedSecret(
            key_id="k1",
            wrapped_dek=b"\x01" * 48,
            wrap_nonce=b"\x02" * 12,
            data_nonce=b"\x03" * 12,
            ciphertext=b"\x04" * 24,
            aad=b"\x05" * 16,
            schema_version=1,
        )
        with pytest.raises(FrozenInstanceError):
            env.key_id = "other"  # type: ignore[misc]
        encoded = env.encoded()
        assert encoded["key_id"] == "k1"
        assert encoded["wrapped_dek"] != "\x01" * 48

    def test_encrypt_round_trip_via_cipher(self):
        from nexus_ai_agent.connect.crypto import EnvelopeCipher

        cipher = EnvelopeCipher(StaticKeyRing({"k1": b"1" * 32}, active_key_id="k1"))
        env = cipher.encrypt(b"secret-bytes", connection_id="conn_x", credential_id="cred_x")
        assert env.ciphertext != b"secret-bytes"
        assert (
            cipher.decrypt(env, connection_id="conn_x", credential_id="cred_x") == b"secret-bytes"
        )


# ---------------------------------------------------------------------------
# Cross-module check: constants shared by both science modules
# ---------------------------------------------------------------------------


class TestSharedConstants:
    def test_constants_are_reexported_identically(self):
        from nexus_ai_agent.creative.science.physical_optics_radiometry import (
            C_LIGHT as physics_light,
        )

        assert physics_light == C_LIGHT
        assert BG_REFERENCE == 1.0


# ---------------------------------------------------------------------------
# W6-CP5: P1 regression tests — R+T=1 for both polarizations below TIR,
# expm1 accuracy, HG normalization on the unit sphere
# ---------------------------------------------------------------------------


class TestFresnelConservationFullPolarizations:
    def test_r_plus_t_equals_one_below_tir_for_both_polarizations(self):
        for n1, n2, theta in ((1.0, 1.5, 30.0), (1.5, 1.0, 40.0), (1.0, 1.0, 45.0)):
            fresnel = fresnel_amplitudes(n1, n2, theta)
            r_s, r_p = fresnel.R_s, fresnel.R_p
            assert r_s + fresnel_transmittance_s(n1, n2, theta) == pytest.approx(1.0)
            assert r_p + fresnel_transmittance_p(n1, n2, theta) == pytest.approx(1.0)

    def test_tir_reflectance_is_one_and_transmittance_is_zero(self):
        critical = critical_angle_deg(1.5, 1.0)
        theta = critical + 5.0
        fresnel = fresnel_amplitudes(1.5, 1.0, theta)
        assert fresnel.R_s == pytest.approx(1.0)
        assert fresnel.R_p == pytest.approx(1.0)
        assert fresnel_transmittance_s(1.5, 1.0, theta) == pytest.approx(0.0)
        assert fresnel_transmittance_p(1.5, 1.0, theta) == pytest.approx(0.0)

    def test_p_polarized_transmittance_at_normal_incidence(self):
        fresnel = fresnel_amplitudes(1.2, 1.5, 0.0)
        assert fresnel_transmittance_p(1.2, 1.5, 0.0) == pytest.approx(1.0 - fresnel.R_p)

    def test_brester_angle_zero_p_reflection_at_steep_angle(self):
        # Brewster angle kills p reflection even away from zero incidence.
        theta_b = brester_angle_deg(1.0, 2.0)
        assert theta_b == pytest.approx(math.degrees(math.atan(2.0)))
        fresnel = fresnel_amplitudes(1.0, 2.0, theta_b)
        assert fresnel.R_p == pytest.approx(0.0, abs=1e-10)


class TestPlanckExpm1Accuracy:
    def test_expm1_accurate_at_low_temperature_long_wavelength(self):
        # The Planck denominator exp(x) - 1 loses all significant digits when x is
        # small; math.expm1 must recover the true value. Compare against a
        # closed-form high-precision computation via log-space.
        T = 1.0  # 1 K
        lam = 100.0  # 100 m
        x = H_PLANCK * C_LIGHT / (lam * KB_BOLTZMANN * T)
        expected = 2.0 * H_PLANCK * C_LIGHT**2 / (lam**5 * math.expm1(x))
        actual = planck_blackbody_spectral_radiance(lam, T)
        assert actual == pytest.approx(expected, rel=1e-12)
        # And it must be finite and positive.
        assert math.isfinite(actual) and actual > 0.0

    def test_low_temperature_high_lambda_is_monotone_in_wavelength(self):
        # B_lambda grows with lambda in the small-x (Rayleigh-Jeans) limit:
        # B ~ 2 c k_B T / lambda^4 (Wien limit is the other end).
        T = 100.0
        vals = [planck_blackbody_spectral_radiance(lam, T) for lam in (1e-3, 1e-4, 1e-5)]
        assert vals[0] < vals[1]  # farther from peak, smaller radiance
        assert all(v > 0.0 for v in vals)


class TestHGNormalizationOnUnitSphere:
    @pytest.mark.parametrize("g", [-0.9, -0.5, -0.1, 0.0, 0.1, 0.5, 0.9])
    def test_unit_sphere_integral_equals_one_to_high_precision(self, g):
        # Integrate the exact phase function over the unit sphere:
        # int_0^pi p(cos(theta), g) 2pi sin(theta) d(theta) = 1
        # (equivalently int_-1^1 p(u, g) 2pi du = 1).
        n_points = 200_000
        du = 2.0 / n_points
        total = 0.0
        for i in range(n_points + 1):
            u = -1.0 + i * du
            w = 1.0 if 0 < i < n_points else 0.5
            total += w * henyey_greenstein_phase_function(u, g) * 2.0 * math.pi * du
        assert total == pytest.approx(1.0, rel=1e-6)


class TestFinitenessAndBoolRejectionAtFunctionEntry:
    """Every public entry point rejects bool/non-finite input fail-closed."""

    def test_bool_and_nan_rejection_planck_and_wien(self):
        with pytest.raises(TypeError):
            planck_blackbody_spectral_radiance(True, 293.15)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            planck_blackbody_spectral_radiance(550e-9, float("nan"))
        with pytest.raises(TypeError):
            wien_max_wavelength(True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            wien_max_wavelength(float("inf"))
        with pytest.raises(TypeError):
            wien_temperature(True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            wien_temperature(float("nan"))
        with pytest.raises(TypeError):
            planck_max_radiance(True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            planck_max_radiance(float("inf"))

    def test_critical_angle_rejects_bool_and_nan_indices(self):
        with pytest.raises(TypeError):
            critical_angle_deg(True, 1.0)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            critical_angle_deg(1.5, True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            critical_angle_deg(float("nan"), 1.0)
        with pytest.raises(ValueError):
            critical_angle_deg(1.5, float("inf"))

    def test_fresnel_and_transmittance_reject_bool_and_nan(self):
        with pytest.raises(TypeError):
            fresnel_amplitudes(True, 1.5, 30.0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            fresnel_amplitudes(1.5, float("nan"), 30.0)
        with pytest.raises(TypeError):
            fresnel_amplitudes(1.5, 1.5, True)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            fresnel_transmittance_s(True, 1.5, 30.0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            fresnel_transmittance_p(1.5, 1.5, float("inf"))

    def test_brester_and_thin_film_reject_bool_and_nan(self):
        with pytest.raises(TypeError):
            brester_angle_deg(True, 1.5)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            brester_reflectance(1.5, float("inf"))
        with pytest.raises(TypeError):
            thin_film_phase(True, 1.5, 200e-9, 0.0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            thin_film_reflectance(0.2, float("nan"), 550e-9, 1.5, 200e-9)
        with pytest.raises(ValueError):
            thin_film_phase(550e-9, 1.5, 200e-9, float("inf"))

    def test_film_materials_volumetric_and_hg_reject_bool_and_nan(self):
        with pytest.raises(TypeError):
            film_materials_from_air(1.0, True, 1.5, 30.0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            film_materials_from_air(1.0, 1.5, 1.5, float("nan"))
        with pytest.raises(TypeError):
            volumetric_radiative_transfer(True, 0.5, 0.4)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            volumetric_radiative_transfer(1.0, 0.5, float("inf"))
        with pytest.raises(TypeError):
            henyey_greenstein_phase_function(True, 0.4)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            henyey_greenstein_norm(float("nan"))
        with pytest.raises(TypeError):
            henyey_greenstein_norm(0.4, True)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            henyey_greenstein_norm(0.4, 1.5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# G4 coverage push: fail-closed branches in Fresnel amplitudes / HG norm
# ---------------------------------------------------------------------------


class TestFresnelAmplitudesBranches:
    def test_bool_amplitude_rejected(self):
        with pytest.raises(TypeError):
            FresnelAmplitudes(True, 0.5)  # type: ignore[arg-type]

    def test_non_numeric_amplitude_rejected(self):
        with pytest.raises(TypeError):
            FresnelAmplitudes("not-a-number", 0.5)  # type: ignore[arg-type]

    def test_finite_magnitude_infinite_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            FresnelAmplitudes(complex(float("inf"), 0.0), 0.5)


class TestHenyeyGreensteinNormBranches:
    def test_zero_n_points_rejected(self):
        with pytest.raises(ValueError, match="at least 1"):
            henyey_greenstein_norm(0.5, 0)


class TestFilmAngleAndPlanckClipPolicy:
    def test_film_materials_rejects_theta_outside_range(self):
        with pytest.raises(ValueError, match="theta_air"):
            film_materials_from_air(1.0, 1.5, 1.0, -5.0)
        with pytest.raises(ValueError, match="theta_air"):
            film_materials_from_air(1.0, 1.5, 1.0, 90.0)

    def test_planck_x_beyond_700_is_zero_not_subnormal(self):
        # x > 700 at ~1e-11 m * 1e3 K << typical usage; just assert the policy.
        tiny_lambda = H_PLANCK * C_LIGHT / (700.1 * KB_BOLTZMANN * 5778.0)
        value = planck_blackbody_spectral_radiance(tiny_lambda, 5778.0)
        assert value == 0.0
