"""WAVE 22 — tests for the spectral ocean and shallow-water equations."""

from __future__ import annotations

import numpy as np
import pytest

from nexus_ai_agent.creative.atlas import spectral_ocean_swe as o


def _grid(n: int = 32) -> tuple[np.ndarray, np.ndarray]:
    kx, ky = np.meshgrid(np.linspace(-0.5, 0.5, n), np.linspace(-0.5, 0.5, n))
    return kx, ky


def test_deep_water_dispersion_matches_analytic() -> None:
    k = np.array([0.05, 0.1, 1.0, 5.0])
    got = o.deep_water_omega(k)
    want = np.sqrt(o.GRAVITY * k)
    np.testing.assert_allclose(got, want, rtol=1e-12)


def test_jonswap_spectrum_significant_wave_height() -> None:
    kx, ky = _grid(64)
    spec = o.jonswap_spectrum(kx, ky, wind_speed=10.0)
    h = o.spectral_height_field(spec, seed=7, extent=100.0)
    # Pierson-Moskowitz H_s = 0.21 U^2 / g; 4*std estimates it.
    h_s_expected = 0.21 * 10.0**2 / o.GRAVITY
    assert 4.0 * h.std() == pytest.approx(h_s_expected, rel=0.05)


def test_spectrum_peak_near_pm_peak() -> None:
    kx, ky = _grid(128)
    spec = o.jonswap_spectrum(kx, ky, wind_speed=10.0)
    omega_p = 0.8772 * o.GRAVITY / 10.0
    # The largest-amplitude bin should be near the peak frequency.
    idx = np.unravel_index(np.argmax(spec.amplitudes), spec.amplitudes.shape)
    assert spec.omega[idx] == pytest.approx(omega_p, rel=0.35)


def test_height_field_is_deterministic() -> None:
    kx, ky = _grid()
    spec = o.jonswap_spectrum(kx, ky)
    a = o.spectral_height_field(spec, seed=42, extent=50.0)
    b = o.spectral_height_field(spec, seed=42, extent=50.0)
    assert np.array_equal(a, b)


def test_height_field_changes_with_seed() -> None:
    kx, ky = _grid()
    spec = o.jonswap_spectrum(kx, ky)
    a = o.spectral_height_field(spec, seed=1, extent=50.0)
    b = o.spectral_height_field(spec, seed=2, extent=50.0)
    assert not np.array_equal(a, b)


def test_gerstner_displacement_is_bounded_and_directional() -> None:
    h = np.zeros((8, 8))
    dx, dy = o.gerstner_displacement(h, steepness=0.1, wind_dir=(1.0, 0.0), wavelength=20.0)
    assert np.allclose(dy, 0.0)  # wind along +x -> no y displacement
    assert np.allclose(dx, 0.0)  # zero height -> zero displacement


def test_gerstner_displacement_amplitude() -> None:
    # For h = wavelength/4, sin(2*pi/h * h)=sin(pi/2)=1 -> disp = steepness/k.
    wl = 20.0
    h = np.full((4, 4), wl / 4.0)
    dx, _ = o.gerstner_displacement(h, steepness=0.5, wind_dir=(1.0, 0.0), wavelength=wl)
    k = 2.0 * np.pi / wl
    assert dx[0, 0] == pytest.approx(0.5 / k)


def test_swe_1d_cfl_refusal() -> None:
    st = o.SWEState(h=np.ones(16), hu=np.zeros(16), hv=None, dx=1.0)
    with pytest.raises(o.OceanError):
        o.swe_step_1d(st, dt=10.0)  # far beyond CFL


def test_swe_1d_conserves_total_mass() -> None:
    h = np.ones(64)
    h[32] = 1.2
    st = o.SWEState(h=h, hu=np.zeros(64), hv=None, dx=1.0)
    m0 = st.h.sum()
    out = st
    for _ in range(5):
        out = o.swe_step_1d(out, dt=0.05)
    # First-order conservative flux: total mass is preserved to round-off.
    assert out.h.sum() == pytest.approx(m0, rel=1e-10)


def test_swe_2d_mass_conservation() -> None:
    H = np.ones((24, 24))
    H[12, 12] = 1.3
    st = o.SWEState(h=H, hu=np.zeros((24, 24)), hv=np.zeros((24, 24)), dx=1.0)
    m0 = st.h.sum()
    out = st
    for _ in range(5):
        out = o.swe_step_2d(out, dt=0.02)
    assert out.h.sum() == pytest.approx(m0, rel=1e-9)


def test_swe_2d_requires_hv() -> None:
    H = np.ones((8, 8))
    st = o.SWEState(h=H, hu=np.zeros((8, 8)), hv=None, dx=1.0)
    with pytest.raises(o.OceanError):
        o.swe_step_2d(st, 0.01)


def test_swe_1d_requires_1d() -> None:
    H = np.ones((8, 8))
    st = o.SWEState(h=H, hu=np.zeros((8, 8)), hv=np.zeros((8, 8)), dx=1.0)
    with pytest.raises(o.OceanError):
        o.swe_step_1d(st, 0.01)


def test_cfl_number_formula() -> None:
    st = o.SWEState(h=np.full(4, 1.0), hu=np.full(4, 2.0), hv=None, dx=1.0)
    # speed = |u| + sqrt(g h) = 2 + sqrt(g)
    want = (2.0 + np.sqrt(o.GRAVITY)) * 0.1 / 1.0
    assert o.cfl_number(st, 0.1) == pytest.approx(want)


# --- adversarial / refusal --------------------------------------------------------


def test_refuse_bad_seed() -> None:
    with pytest.raises(o.OceanError):
        o.spectral_height_field(o.jonswap_spectrum(*_grid()), seed=-1)
    with pytest.raises(o.OceanError):
        o.spectral_height_field(o.jonswap_spectrum(*_grid()), seed=True)  # type: ignore[arg-type]


def test_refuse_negative_wavenumber() -> None:
    with pytest.raises(o.OceanError):
        o.deep_water_omega(np.array([-1.0]))


def test_refuse_zero_wind() -> None:
    with pytest.raises(o.OceanError):
        o.jonswap_spectrum(*_grid(), wind_speed=0.0)


def test_refuse_bad_sharpness_and_fetch() -> None:
    with pytest.raises(o.OceanError):
        o.jonswap_spectrum(*_grid(), peak_sharpness=0.0)
    with pytest.raises(o.OceanError):
        o.jonswap_spectrum(*_grid(), fetch=-1.0)


def test_refuse_mismatched_k_grids() -> None:
    with pytest.raises(o.OceanError):
        o.jonswap_spectrum(np.zeros((4, 4)), np.zeros((5, 5)))


def test_refuse_nonfinite_wavenumber() -> None:
    kx, ky = _grid()
    kx[0, 0] = np.nan
    with pytest.raises(o.OceanError):
        o.jonswap_spectrum(kx, ky)


def test_refuse_zero_wind_dir() -> None:
    with pytest.raises(o.OceanError):
        o.jonswap_spectrum(*_grid(), wind_dir=(0.0, 0.0))


def test_refuse_oversized_grid() -> None:
    with pytest.raises(o.OceanError):
        o.spectral_height_field(
            o.jonswap_spectrum(np.zeros((2, o.MAX_GRID + 1)), np.zeros((2, o.MAX_GRID + 1))),
            seed=0,
        )


def test_refuse_bad_steepness() -> None:
    with pytest.raises(o.OceanError):
        o.gerstner_displacement(np.zeros((4, 4)), steepness=1.5)


def test_refuse_nonfinite_height() -> None:
    h = np.zeros((4, 4))
    h[0, 0] = np.inf
    with pytest.raises(o.OceanError):
        o.gerstner_displacement(h)


def test_refuse_zero_wavelength() -> None:
    with pytest.raises(o.OceanError):
        o.gerstner_displacement(np.zeros((4, 4)), wavelength=0.0)


def test_refuse_nonpositive_depth() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.array([1.0, 0.0, 1.0]), hu=np.zeros(3), hv=None, dx=1.0)


def test_refuse_negative_dt() -> None:
    st = o.SWEState(h=np.ones(4), hu=np.zeros(4), hv=None, dx=1.0)
    with pytest.raises(o.OceanError):
        o.cfl_number(st, -0.1)


def test_refuse_shape_mismatch_state() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.ones(4), hu=np.zeros(5), hv=None, dx=1.0)


def test_refuse_3d_state() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.ones((2, 2, 2)), hu=np.ones((2, 2, 2)), hv=None, dx=1.0)


def test_refuse_hv_in_1d() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.ones(4), hu=np.zeros(4), hv=np.zeros(4), dx=1.0)


def test_refuse_empty_spectrum() -> None:
    with pytest.raises(o.OceanError):
        o.OceanSpectrum(
            amplitudes=np.zeros((0, 0)), omega=np.zeros((0, 0)), wind_dir=np.array([1.0, 0.0])
        )


def test_refuse_spectrum_shape_mismatch() -> None:
    with pytest.raises(o.OceanError):
        o.OceanSpectrum(
            amplitudes=np.zeros((4, 4)), omega=np.zeros((4, 5)), wind_dir=np.array([1.0, 0.0])
        )


def test_refuse_negative_amplitude() -> None:
    a = np.zeros((4, 4))
    a[0, 0] = -1.0
    with pytest.raises(o.OceanError):
        o.OceanSpectrum(amplitudes=a, omega=np.zeros((4, 4)), wind_dir=np.array([1.0, 0.0]))


def test_refuse_bad_wind_dir_shape() -> None:
    with pytest.raises(o.OceanError):
        o.OceanSpectrum(
            amplitudes=np.zeros((4, 4)), omega=np.zeros((4, 4)), wind_dir=np.array([1.0, 0.0, 0.0])
        )


def test_refuse_nonfinite_time() -> None:
    with pytest.raises(o.OceanError):
        o.spectral_height_field(o.jonswap_spectrum(*_grid()), seed=0, time=np.inf)


def test_refuse_negative_extent() -> None:
    with pytest.raises(o.OceanError):
        o.spectral_height_field(o.jonswap_spectrum(*_grid()), seed=0, extent=-1.0)


def test_refuse_nonfinite_hu() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.ones(4), hu=np.array([0.0, np.nan, 0.0, 0.0]), hv=None, dx=1.0)


def test_refuse_hv_shape_mismatch() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.ones((4, 4)), hu=np.zeros((4, 4)), hv=np.zeros((5, 5)), dx=1.0)


def test_refuse_negative_amplitude_scale() -> None:
    with pytest.raises(o.OceanError):
        o.jonswap_spectrum(*_grid(), amplitude_scale=-1.0)


def test_refuse_nonfinite_amplitude() -> None:
    a = np.zeros((4, 4))
    a[0, 0] = np.inf
    with pytest.raises(o.OceanError):
        o.OceanSpectrum(amplitudes=a, omega=np.zeros((4, 4)), wind_dir=np.array([1.0, 0.0]))


def test_refuse_zero_wind_dir_in_spectrum() -> None:
    with pytest.raises(o.OceanError):
        o.OceanSpectrum(
            amplitudes=np.zeros((4, 4)), omega=np.zeros((4, 4)), wind_dir=np.array([0.0, 0.0])
        )


def test_refuse_nonpositive_gravity_omega() -> None:
    with pytest.raises(o.OceanError):
        o.deep_water_omega(np.array([1.0]), gravity=0.0)


def test_refuse_nonpositive_gravity_spectrum() -> None:
    with pytest.raises(o.OceanError):
        o.jonswap_spectrum(*_grid(), gravity=0.0)


def test_refuse_bad_wind_dir_shape_spectrum() -> None:
    with pytest.raises(o.OceanError):
        o.jonswap_spectrum(*_grid(), wind_dir=(1.0, 0.0, 0.0))


def test_refuse_1d_height_in_gerstner() -> None:
    with pytest.raises(o.OceanError):
        o.gerstner_displacement(np.zeros(4))


def test_refuse_bad_wind_dir_shape_gerstner() -> None:
    with pytest.raises(o.OceanError):
        o.gerstner_displacement(np.zeros((4, 4)), wind_dir=(1.0, 0.0, 0.0))


def test_refuse_zero_wind_dir_gerstner() -> None:
    with pytest.raises(o.OceanError):
        o.gerstner_displacement(np.zeros((4, 4)), wind_dir=(0.0, 0.0))


def test_refuse_empty_state() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.zeros(0), hu=np.zeros(0), hv=None, dx=1.0)


def test_refuse_oversized_state_grid() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.ones(o.MAX_GRID + 1), hu=np.zeros(o.MAX_GRID + 1), hv=None, dx=1.0)


def test_refuse_nonpositive_dx() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.ones(4), hu=np.zeros(4), hv=None, dx=0.0)


def test_refuse_nonpositive_g() -> None:
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.ones(4), hu=np.zeros(4), hv=None, dx=1.0, g=0.0)


def test_refuse_nonfinite_hv() -> None:
    hv = np.zeros((4, 4))
    hv[0, 0] = np.nan
    with pytest.raises(o.OceanError):
        o.SWEState(h=np.ones((4, 4)), hu=np.zeros((4, 4)), hv=hv, dx=1.0)


def test_swe_2d_cfl_refusal() -> None:
    st = o.SWEState(h=np.ones((8, 8)), hu=np.zeros((8, 8)), hv=np.zeros((8, 8)), dx=1.0)
    with pytest.raises(o.OceanError):
        o.swe_step_2d(st, dt=10.0)
