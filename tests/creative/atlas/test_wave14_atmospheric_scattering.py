"""WAVE 14 — tests for volumetric atmospheric scattering and weather advection."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import volumetric_atmospheric_scattering as va


def _sphere_integral(values: np.ndarray) -> float:
    mu = np.linspace(-1.0, 1.0, 20001)
    dmu = mu[1] - mu[0]
    return float(2.0 * np.pi * np.sum(values) * dmu)


# --- phase functions -------------------------------------------------------------


@pytest.mark.parametrize("g", [-0.5, 0.0, 0.4, 0.76])
def test_henyey_greenstein_integrates_to_one(g: float) -> None:
    mu = np.linspace(-1.0, 1.0, 20001)
    phase = va.henyey_greenstein_phase(mu, g)
    assert _sphere_integral(phase) == pytest.approx(1.0, abs=2e-3)


def test_henyey_greenstein_forward_peak() -> None:
    # Forward scattering (cos θ = 1) is much brighter than backward for g > 0.
    assert va.henyey_greenstein_phase(1.0, 0.76) > va.henyey_greenstein_phase(-1.0, 0.76)


def test_henyey_greenstein_isotropic_for_g_zero() -> None:
    assert va.henyey_greenstein_phase(0.5, 0.0) == pytest.approx(1.0 / (4.0 * np.pi))


def test_henyey_greenstein_rejects_bad_g() -> None:
    with pytest.raises(va.AtmosphericError):
        va.henyey_greenstein_phase(0.5, 1.0)


def test_henyey_greenstein_rejects_nan() -> None:
    with pytest.raises(va.AtmosphericError):
        va.henyey_greenstein_phase(np.nan, 0.5)


def test_rayleigh_phase_integrates_to_one() -> None:
    mu = np.linspace(-1.0, 1.0, 20001)
    assert _sphere_integral(va.rayleigh_phase(mu)) == pytest.approx(1.0, abs=2e-3)


def test_rayleigh_phase_is_symmetric_in_mu() -> None:
    assert va.rayleigh_phase(0.7) == pytest.approx(float(va.rayleigh_phase(-0.7)))


def test_rayleigh_phase_rejects_nan() -> None:
    with pytest.raises(va.AtmosphericError):
        va.rayleigh_phase(np.inf)


# --- beta coefficients -----------------------------------------------------------


def test_rayleigh_beta_scales_as_inverse_fourth_power() -> None:
    bR = va.rayleigh_beta()
    # 650/460 = 1.4130; ^4 = 3.9867
    assert float(bR[2] / bR[0]) == pytest.approx((650.0 / 460.0) ** 4, rel=1e-6)


def test_rayleigh_beta_custom_wavelengths() -> None:
    bR = va.rayleigh_beta(np.array([500.0, 400.0]))
    assert float(bR[1] / bR[0]) == pytest.approx((500.0 / 400.0) ** 4, rel=1e-9)


def test_rayleigh_beta_rejects_negative_scale() -> None:
    with pytest.raises(va.AtmosphericError):
        va.rayleigh_beta(scale=-1.0)


def test_rayleigh_beta_rejects_nonpositive_wavelength() -> None:
    with pytest.raises(va.AtmosphericError):
        va.rayleigh_beta(np.array([0.0, 500.0]))


def test_mie_beta_scales_as_inverse_power() -> None:
    bM = va.mie_beta(power=1.0)
    assert float(bM[2] / bM[0]) == pytest.approx(650.0 / 460.0, rel=1e-9)


def test_mie_beta_rejects_bad_power() -> None:
    with pytest.raises(va.AtmosphericError):
        va.mie_beta(power=0.0)


def test_mie_beta_rejects_bad_scale() -> None:
    with pytest.raises(va.AtmosphericError):
        va.mie_beta(scale=-1.0)


# --- atmosphere parameters -------------------------------------------------------


def test_atmosphere_params_rejects_bad_g() -> None:
    with pytest.raises(va.AtmosphericError):
        va.AtmosphereParameters(mie_g=1.0)


def test_atmosphere_params_rejects_negative_scale() -> None:
    with pytest.raises(va.AtmosphericError):
        va.AtmosphereParameters(rayleigh_scale=-1.0)


def test_atmosphere_params_rejects_nonpositive_height() -> None:
    with pytest.raises(va.AtmosphericError):
        va.AtmosphereParameters(rayleigh_height_km=0.0)


# --- single-scatter evaluation ---------------------------------------------------


def test_scattering_transmittance_beer_lambert() -> None:
    res = va.evaluate_atmospheric_scattering(
        beta_r=va.rayleigh_beta(),
        beta_m=va.mie_beta(),
        sigma_t=0.01,
        distance=100.0,
        cos_theta=0.0,
        params=va.AtmosphereParameters(),
    )
    assert res.transmittance == pytest.approx(float(np.exp(-1.0)), rel=1e-12)


def test_scattering_blue_dominates_zenith() -> None:
    res = va.evaluate_atmospheric_scattering(
        beta_r=va.rayleigh_beta(),
        beta_m=va.mie_beta(),
        sigma_t=0.02,
        distance=500.0,
        cos_theta=0.0,
        params=va.AtmosphereParameters(),
    )
    # Blue (index 2) scatters more than red (index 0).
    assert res.in_scatter[2] > res.in_scatter[0]


def test_scattering_rejects_bad_sigma_t() -> None:
    with pytest.raises(va.AtmosphericError):
        va.evaluate_atmospheric_scattering(
            beta_r=va.rayleigh_beta(),
            beta_m=va.mie_beta(),
            sigma_t=0.0,
            distance=1.0,
            cos_theta=0.0,
            params=va.AtmosphereParameters(),
        )


def test_scattering_rejects_bad_distance() -> None:
    with pytest.raises(va.AtmosphericError):
        va.evaluate_atmospheric_scattering(
            beta_r=va.rayleigh_beta(),
            beta_m=va.mie_beta(),
            sigma_t=0.1,
            distance=-1.0,
            cos_theta=0.0,
            params=va.AtmosphereParameters(),
        )


def test_scattering_rejects_bad_beta_shape() -> None:
    with pytest.raises(va.AtmosphericError):
        va.evaluate_atmospheric_scattering(
            beta_r=np.ones(2),
            beta_m=np.ones(3),
            sigma_t=0.1,
            distance=1.0,
            cos_theta=0.0,
            params=va.AtmosphereParameters(),
        )


def test_scattering_rejects_negative_beta() -> None:
    with pytest.raises(va.AtmosphericError):
        va.evaluate_atmospheric_scattering(
            beta_r=np.array([-1.0, 1.0, 1.0]),
            beta_m=np.ones(3),
            sigma_t=0.1,
            distance=1.0,
            cos_theta=0.0,
            params=va.AtmosphereParameters(),
        )


def test_scattering_rejects_bad_steps() -> None:
    with pytest.raises(va.AtmosphericError):
        va.evaluate_atmospheric_scattering(
            beta_r=va.rayleigh_beta(),
            beta_m=va.mie_beta(),
            sigma_t=0.1,
            distance=1.0,
            cos_theta=0.0,
            params=va.AtmosphereParameters(),
            steps=0,
        )


# --- crepuscular rays ------------------------------------------------------------


def _vol(n: int = 16, value: float = 0.1) -> np.ndarray:
    return np.full((n, n, 3), value)


def test_crepuscular_rays_shape_and_positivity() -> None:
    img = va.render_volumetric_crepuscular_rays(
        _vol(),
        sun_direction=np.array([0.0, 0.0, 1.0]),
        light_visibility=np.full((16, 16), 0.8),
        steps=32,
    )
    assert img.shape == (16, 16, 3)
    assert np.all(img >= 0.0)


def test_crepuscular_rays_zero_visibility_is_dark() -> None:
    img = va.render_volumetric_crepuscular_rays(
        _vol(),
        sun_direction=np.array([0.0, 0.0, 1.0]),
        light_visibility=np.zeros((16, 16)),
        steps=32,
    )
    assert np.allclose(img, 0.0)


def test_crepuscular_rays_brightness_monotone_in_visibility() -> None:
    dim = va.render_volumetric_crepuscular_rays(
        _vol(),
        sun_direction=np.array([0.0, 0.0, 1.0]),
        light_visibility=np.full((16, 16), 0.2),
        steps=32,
    )
    bright = va.render_volumetric_crepuscular_rays(
        _vol(),
        sun_direction=np.array([0.0, 0.0, 1.0]),
        light_visibility=np.full((16, 16), 0.9),
        steps=32,
    )
    assert float(bright.mean()) > float(dim.mean())


def test_crepuscular_rays_rejects_bad_volume_shape() -> None:
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            np.zeros((4, 4)),
            sun_direction=np.array([0.0, 0.0, 1.0]),
            light_visibility=np.zeros((4, 4)),
        )


def test_crepuscular_rays_rejects_negative_extinction() -> None:
    vol = _vol()
    vol[0, 0, 0] = -1.0
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            vol,
            sun_direction=np.array([0.0, 0.0, 1.0]),
            light_visibility=np.zeros((16, 16)),
        )


def test_crepuscular_rays_rejects_bad_sun_direction() -> None:
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            _vol(),
            sun_direction=np.zeros(3),
            light_visibility=np.zeros((16, 16)),
        )


def test_crepuscular_rays_rejects_bad_visibility_shape() -> None:
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            _vol(),
            sun_direction=np.array([0.0, 0.0, 1.0]),
            light_visibility=np.zeros((8, 8)),
        )


def test_crepuscular_rays_rejects_visibility_out_of_range() -> None:
    vis = np.full((16, 16), 1.5)
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            _vol(),
            sun_direction=np.array([0.0, 0.0, 1.0]),
            light_visibility=vis,
        )


def test_crepuscular_rays_rejects_bad_color() -> None:
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            _vol(),
            sun_direction=np.array([0.0, 0.0, 1.0]),
            light_visibility=np.full((16, 16), 0.5),
            inscatter_color=np.ones(2),
        )


def test_crepuscular_rays_rejects_bad_steps() -> None:
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            _vol(),
            sun_direction=np.array([0.0, 0.0, 1.0]),
            light_visibility=np.full((16, 16), 0.5),
            steps=va.MAX_RAY_STEPS + 1,
        )


# --- aerial perspective ----------------------------------------------------------


def test_aerial_perspective_closed_form() -> None:
    assert va.aerial_perspective_contrast(1.0, 0.01, 100.0) == pytest.approx(np.exp(-1.0))


def test_aerial_perspective_rejects_negative_inputs() -> None:
    with pytest.raises(va.AtmosphericError):
        va.aerial_perspective_contrast(-1.0, 0.01, 100.0)
    with pytest.raises(va.AtmosphericError):
        va.aerial_perspective_contrast(1.0, -0.01, 100.0)
    with pytest.raises(va.AtmosphericError):
        va.aerial_perspective_contrast(1.0, 0.01, -100.0)


# --- weather advection -----------------------------------------------------------


def test_advection_conserves_mass() -> None:
    rng = np.random.default_rng(0)
    rho = np.abs(rng.random((32, 32)))
    vel = np.full((32, 32, 2), 0.5)
    out = va.advect_weather_density_field(rho, vel, dt=0.1, diffusion=0.5)
    assert float(out.sum()) == pytest.approx(float(rho.sum()), rel=1e-9)
    assert float(out.min()) >= 0.0


def test_advection_without_diffusion_conserves_mass() -> None:
    rng = np.random.default_rng(1)
    rho = np.abs(rng.random((24, 24)))
    vel = np.full((24, 24, 2), 0.2)
    out = va.advect_weather_density_field(rho, vel, dt=0.05)
    assert float(out.sum()) == pytest.approx(float(rho.sum()), rel=1e-9)


def test_advection_3d_channel_field() -> None:
    rng = np.random.default_rng(2)
    rho = np.abs(rng.random((16, 16, 3)))
    vel = np.full((16, 16, 2), 0.3)
    out = va.advect_weather_density_field(rho, vel, dt=0.1, diffusion=0.2)
    assert out.shape == (16, 16, 3)
    assert float(out.sum()) == pytest.approx(float(rho.sum()), rel=1e-9)


def test_advection_zero_velocity_diffusion_smooths() -> None:
    rho = np.zeros((32, 32))
    rho[16, 16] = 1.0
    vel = np.zeros((32, 32, 2))
    out = va.advect_weather_density_field(rho, vel, dt=0.1, diffusion=2.0)
    assert float(out[16, 16]) < 1.0  # spread out
    assert float(out[16, 16]) > 0.0


def test_advection_rejects_bad_density_shape() -> None:
    with pytest.raises(va.AtmosphericError):
        va.advect_weather_density_field(np.zeros((1, 5)), np.zeros((1, 5, 2)), dt=0.1)


def test_advection_rejects_negative_density() -> None:
    with pytest.raises(va.AtmosphericError):
        va.advect_weather_density_field(np.full((4, 4), -1.0), np.zeros((4, 4, 2)), dt=0.1)


def test_advection_rejects_bad_velocity_shape() -> None:
    with pytest.raises(va.AtmosphericError):
        va.advect_weather_density_field(np.ones((4, 4)), np.ones((4, 4, 3)), dt=0.1)


def test_advection_rejects_bad_dt() -> None:
    with pytest.raises(va.AtmosphericError):
        va.advect_weather_density_field(np.ones((4, 4)), np.zeros((4, 4, 2)), dt=0.0)


def test_advection_rejects_negative_diffusion() -> None:
    with pytest.raises(va.AtmosphericError):
        va.advect_weather_density_field(
            np.ones((4, 4)), np.zeros((4, 4, 2)), dt=0.1, diffusion=-1.0
        )


def test_advection_rejects_bad_mass_tolerance() -> None:
    with pytest.raises(va.AtmosphericError):
        va.advect_weather_density_field(
            np.ones((4, 4)), np.zeros((4, 4, 2)), dt=0.1, mass_tolerance=0.0
        )


def test_rayleigh_beta_rejects_bad_ndim() -> None:
    with pytest.raises(va.AtmosphericError):
        va.rayleigh_beta(np.ones((2, 2)))


def test_rayleigh_beta_rejects_nan() -> None:
    with pytest.raises(va.AtmosphericError):
        va.rayleigh_beta(np.array([500.0, np.nan]))


def test_mie_beta_rejects_bad_ndim() -> None:
    with pytest.raises(va.AtmosphericError):
        va.mie_beta(np.ones((2, 2)))


def test_mie_beta_rejects_nan() -> None:
    with pytest.raises(va.AtmosphericError):
        va.mie_beta(np.array([500.0, np.nan]))


def test_scattering_rejects_nan_beta() -> None:
    with pytest.raises(va.AtmosphericError):
        va.evaluate_atmospheric_scattering(
            beta_r=np.array([1.0, np.nan, 1.0]),
            beta_m=np.ones(3),
            sigma_t=0.1,
            distance=1.0,
            cos_theta=0.0,
            params=va.AtmosphereParameters(),
        )


def test_crepuscular_rays_rejects_empty_volume() -> None:
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            np.zeros((0, 4, 3)),
            sun_direction=np.array([0.0, 0.0, 1.0]),
            light_visibility=np.zeros((0, 4)),
        )


def test_crepuscular_rays_rejects_nan_sun() -> None:
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            _vol(),
            sun_direction=np.array([0.0, 0.0, np.nan]),
            light_visibility=np.full((16, 16), 0.5),
        )


def test_crepuscular_rays_rejects_over_budget() -> None:
    big = np.zeros((2049, 2049, 3))
    with pytest.raises(va.AtmosphericError):
        va.render_volumetric_crepuscular_rays(
            big,
            sun_direction=np.array([0.0, 0.0, 1.0]),
            light_visibility=np.zeros((2049, 2049)),
        )


def test_advection_rejects_nan_density() -> None:
    rho = np.ones((4, 4))
    rho[0, 0] = np.nan
    with pytest.raises(va.AtmosphericError):
        va.advect_weather_density_field(rho, np.zeros((4, 4, 2)), dt=0.1)


def test_advection_rejects_nan_velocity() -> None:
    vel = np.zeros((4, 4, 2))
    vel[0, 0, 0] = np.nan
    with pytest.raises(va.AtmosphericError):
        va.advect_weather_density_field(np.ones((4, 4)), vel, dt=0.1)


def test_advection_all_zero_field_returns_zero() -> None:
    out = va.advect_weather_density_field(
        np.zeros((4, 4)), np.zeros((4, 4, 2)), dt=0.1, diffusion=0.5
    )
    assert float(out.sum()) == 0.0
