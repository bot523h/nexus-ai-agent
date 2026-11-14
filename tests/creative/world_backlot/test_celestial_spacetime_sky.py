from __future__ import annotations

import io
import math
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from nexus_ai_agent.creative.world_backlot import (
    Atmosphere,
    CubeLUT,
    LunarPhase,
    Observer,
    SkySpectrum,
    SolarPeriod,
    blackbody_rgb,
    celestial_state,
    disc_overlap_fraction,
    generate_cube,
    generate_starfield,
    henyey_greenstein,
    lunar_disc,
    mie_coefficient,
    planck_radiance,
    rayleigh_coefficient,
    rayleigh_phase,
    sky_spectrum,
    solar_period,
)


def test_j2000_epoch_timezone_and_fractional_civil_time() -> None:
    assert Observer(0, 1, 12, 2000).julian_day == 2451545.0
    civil = Observer(32.65, 172, 12.123456, 2026, 51.67, 3.5)
    utc = replace(civil, hour=civil.hour - 3.5, utc_offset_hours=0)
    assert civil.julian_day == utc.julian_day
    np.testing.assert_allclose(
        celestial_state(civil).sun.direction, celestial_state(utc).sun.direction, atol=1e-12
    )
    later = replace(civil, hour=civil.hour + 1 / 3600)
    assert (later.julian_day - civil.julian_day) * 86400 == pytest.approx(1, abs=5e-5)
    assert (
        np.linalg.norm(
            np.subtract(
                celestial_state(later).moon.direction, celestial_state(civil).moon.direction
            )
        )
        > 1e-6
    )


def test_observer_rollover_handles_leap_days_years_and_negative_offsets() -> None:
    leap = Observer(10, 60, 23.5, 2024, 20, 3, 500).after(3600)
    assert (leap.day_of_year, leap.hour, leap.year) == (61, 0.5, 2024)
    new_year = Observer(10, 366, 23.5, 2024).after(3600)
    assert (new_year.day_of_year, new_year.hour, new_year.year) == (1, 0.5, 2025)
    previous = Observer(0, 1, 0, 2026).after(-1)
    assert (previous.year, previous.day_of_year) == (2025, 365)
    assert previous.hour == pytest.approx(24 - 1 / 3600)
    assert leap.longitude_deg == 20 and leap.utc_offset_hours == 3 and leap.elevation_m == 500


def test_sun_against_published_nrel_spa_reference_not_just_self_consistency() -> None:
    # NREL SPA example: 2003-10-17 12:30:30, Colorado, UTC-7, 1830 m.
    # Published refracted elevation ~39.8884; geometric elevation ~39.8720.
    sky = celestial_state(
        Observer(39.742476, 290, 12 + 30 / 60 + 30 / 3600, 2003, -105.1786, -7, 1830)
    )
    assert sky.sun.azimuth_deg == pytest.approx(194.3402, abs=0.08)
    assert sky.sun.elevation_deg == pytest.approx(39.8720, abs=0.04)
    assert sky.sun.distance_km == pytest.approx(149_000_000, rel=0.015)


@pytest.mark.parametrize("latitude", [-90, -66, -35, 0, 35, 66, 90])
@pytest.mark.parametrize("hour", [0.0, 5.125, 8.5, 12.0, 18.75, 23.999])
def test_solar_and_lunar_vectors_are_finite_unit_enu_vectors(latitude: float, hour: float) -> None:
    state = celestial_state(Observer(latitude, 172, hour))
    for body in (state.sun, state.moon):
        assert np.linalg.norm(body.direction) == pytest.approx(1, abs=1e-12)
        assert -90 <= body.elevation_deg <= 90
        assert 0 <= body.azimuth_deg < 360
        assert math.sin(math.radians(body.elevation_deg)) == pytest.approx(body.direction[2])
    assert 345_000 < state.moon.distance_km < 415_000
    assert 0.24 < state.moon.angular_radius_deg < 0.30


def test_seasons_and_polar_day_night_are_not_clock_only_shortcuts() -> None:
    equinox = celestial_state(Observer(35, 80, 12))
    midnight = celestial_state(Observer(35, 80, 0))
    assert equinox.sun.elevation_deg == pytest.approx(55, abs=1)
    assert midnight.sun.elevation_deg < -50
    assert celestial_state(Observer(80, 172, 0)).sun.elevation_deg > 10
    assert celestial_state(Observer(80, 355, 12)).sun.elevation_deg < -10
    assert celestial_state(Observer(-80, 355, 0)).sun.elevation_deg > 10


@pytest.mark.parametrize(
    ("altitude", "angle", "period"),
    [
        (-30, 0, SolarPeriod.MIDNIGHT),
        (-15, -70, SolarPeriod.TWILIGHT),
        (-8, -70, SolarPeriod.DAWN),
        (-8, 70, SolarPeriod.TWILIGHT),
        (2, -80, SolarPeriod.GOLDEN_HOUR),
        (20, -60, SolarPeriod.MORNING),
        (65, 0, SolarPeriod.NOON),
        (20, 60, SolarPeriod.AFTERNOON),
    ],
)
def test_geometric_solar_periods(altitude: float, angle: float, period: SolarPeriod) -> None:
    assert solar_period(altitude, angle) is period


def test_lunar_phase_and_real_eclipse_dates() -> None:
    new = celestial_state(Observer(32.7767, 99, 13.7, 2024, -96.797, -5))
    assert new.lunar_phase.illuminated_fraction < 0.001
    assert new.eclipse.solar_obscuration > 0.9
    assert new.eclipse.lunar_umbra_fraction == 0
    far = celestial_state(Observer(0, 99, 18.7, 2024, 80))
    assert far.eclipse.solar_obscuration < 0.1
    full = celestial_state(Observer(0, 85, 7, 2024))  # March 25 penumbral eclipse
    assert full.lunar_phase.illuminated_fraction > 0.999
    total = celestial_state(Observer(0, 312, 11, 2022))  # November 8 total lunar eclipse
    assert total.eclipse.lunar_umbra_fraction > 0.99
    assert total.eclipse.lunar_penumbra_fraction == 1
    quarter = celestial_state(Observer(0, 106, 19, 2024))
    assert quarter.lunar_phase.illuminated_fraction == pytest.approx(0.5, abs=0.04)
    assert quarter.lunar_phase.waxing
    waning = celestial_state(Observer(0, 116, 12, 2024))
    assert not waning.lunar_phase.waxing
    assert -180 <= quarter.lunar_phase.bright_limb_angle_deg <= 180


def test_geometric_disc_overlap_including_tangency_and_annularity() -> None:
    assert disc_overlap_fraction(1, 1, 2) == 0
    assert disc_overlap_fraction(1, 2, 0) == 1
    assert disc_overlap_fraction(2, 1, 0) == 0.25
    assert disc_overlap_fraction(1, 1, 1) == pytest.approx(2 / 3 - math.sqrt(3) / (2 * math.pi))
    assert 0 < disc_overlap_fraction(0.26, 0.25, 0.1) < 1
    with pytest.raises(ValueError):
        disc_overlap_fraction(0, 1, 1)


def test_lunar_sphere_terminator_area_and_limb_orientation() -> None:
    full = lunar_disc(LunarPhase(1, 0.5, 90), 129)
    new = lunar_disc(LunarPhase(0, 0, 90), 129)
    quarter = lunar_disc(LunarPhase(0.5, 0.25, 90), 129)
    mirrored = lunar_disc(LunarPhase(0.5, 0.75, -90), 129)
    crescent = lunar_disc(LunarPhase(0.25, 0.17, 90), 129)
    assert full[64, 64] == 1 and np.count_nonzero(new) == 0
    assert np.count_nonzero(quarter) / np.count_nonzero(full) == pytest.approx(0.5, abs=0.015)
    assert np.count_nonzero(crescent) / np.count_nonzero(full) == pytest.approx(0.25, abs=0.015)
    np.testing.assert_allclose(quarter, mirrored[:, ::-1], atol=1e-15)
    assert np.all(quarter[:, :64] < 1e-15)
    assert np.all(full >= 0) and not full.flags.writeable


def test_rayleigh_inverse_fourth_and_mie_hg_normalization() -> None:
    assert rayleigh_coefficient(440) / rayleigh_coefficient(680) == pytest.approx((680 / 440) ** 4)
    assert rayleigh_coefficient(550, 0) == 0
    assert mie_coefficient(450, 0) == 0
    assert mie_coefficient(440) > mie_coefficient(680)
    assert henyey_greenstein(0.3, 0) == pytest.approx(1 / (4 * math.pi))
    assert henyey_greenstein(1) > 100 * henyey_greenstein(-1)
    cosines = np.linspace(-1, 1, 8001)
    for phase in (rayleigh_phase, henyey_greenstein):
        values = np.array([phase(float(c)) for c in cosines])
        integral = (
            math.pi * float(np.sum(values[:-1] + values[1:])) * float(cosines[1] - cosines[0])
        )
        assert integral == pytest.approx(1, abs=5e-4)


def test_planck_law_wien_tail_and_stellar_chromaticities() -> None:
    # Independent SI-constant evaluation.
    h, c, k, wavelength, temperature = 6.62607015e-34, 299792458, 1.380649e-23, 500e-9, 5778
    expected = 2 * h * c * c / (wavelength**5 * math.expm1(h * c / (wavelength * k * temperature)))
    assert planck_radiance(500, 5778) == pytest.approx(expected, rel=1e-12)
    assert planck_radiance(100, 100) == 0
    assert planck_radiance(500, 10000) > planck_radiance(500, 3000)
    warm, hot = blackbody_rgb(3000), blackbody_rgb(28000)
    assert warm[0] > 3 * warm[2]
    assert hot[2] > hot[0]
    assert max(warm) == max(hot) == 1


def test_spherical_scattering_distinguishes_blue_noon_red_sunset_and_dark_midnight() -> None:
    noon = celestial_state(Observer(35, 80, 12))
    sunset = celestial_state(Observer(35, 80, 18))
    night = celestial_state(Observer(35, 80, 0))
    a = sky_spectrum(noon)
    b = sky_spectrum(sunset, sunset.sun.azimuth_deg, 5)
    c = sky_spectrum(night)
    assert a.linear_rgb[2] > 2 * a.linear_rgb[0]
    assert b.linear_rgb[0] > 5 * b.linear_rgb[2]
    assert np.sum(c.radiance) < np.sum(a.radiance) * 1e-3
    assert a.transmittance[0] < a.transmittance[-1]
    horizon = sky_spectrum(noon, view_elevation_deg=0)
    assert np.mean(horizon.transmittance) < np.mean(a.transmittance)
    haze = sky_spectrum(noon, atmosphere=Atmosphere(turbidity=10))
    assert haze.sha256 != a.sha256
    assert len({a.sha256, b.sha256, c.sha256}) == 3
    vacuum = sky_spectrum(noon, atmosphere=Atmosphere(pressure_pa=0, turbidity=0))
    np.testing.assert_array_equal(vacuum.radiance, np.zeros(81))
    np.testing.assert_array_equal(vacuum.transmittance, np.ones(81))
    assert a.sha256 == sky_spectrum(noon).sha256


def test_lunar_and_solar_eclipses_reduce_the_respective_light_sources() -> None:
    solar = celestial_state(Observer(32.7767, 99, 13.7, 2024, -96.797, -5))
    no_eclipse = replace(solar, eclipse=replace(solar.eclipse, solar_obscuration=0))
    assert np.sum(sky_spectrum(solar).radiance) < 0.1 * np.sum(sky_spectrum(no_eclipse).radiance)
    lunar = celestial_state(Observer(35, 85, 0, 2024))
    shadow = replace(lunar, eclipse=replace(lunar.eclipse, lunar_umbra_fraction=1))
    assert np.sum(sky_spectrum(shadow).radiance) < np.sum(sky_spectrum(lunar).radiance)


def test_cube_is_valid_red_fastest_linear_rgb_and_time_specific() -> None:
    spectrum = sky_spectrum(celestial_state(Observer(35, 80, 12)))
    cube = generate_cube(spectrum, 5)
    text = cube.to_cube()
    assert 'TITLE "NAGAR World Backlot v1"' in text
    assert "LUT_3D_SIZE 5" in text
    assert "DOMAIN_MIN 0.0 0.0 0.0" in text and "DOMAIN_MAX 1.0 1.0 1.0" in text
    decoded = np.loadtxt(io.StringIO(text), skiprows=4)
    assert decoded.shape == (125, 3)
    np.testing.assert_allclose(decoded, cube.values, atol=5.1e-8)
    assert cube.values[1, 0] > cube.values[0, 0]
    assert cube.values[5, 1] > cube.values[0, 1]
    assert cube.values[25, 2] > cube.values[0, 2]
    assert np.all((decoded >= 0) & (decoded <= 1))
    assert cube.sha256 == generate_cube(spectrum, 5).sha256
    night = generate_cube(sky_spectrum(celestial_state(Observer(35, 80, 0))), 5)
    assert night.sha256 != cube.sha256
    assert generate_cube(spectrum, 5, 1).sha256 != cube.sha256
    assert generate_cube(spectrum, 5, -1).sha256 != cube.sha256


def test_thousands_of_blackbody_stars_have_stable_geometry_and_milky_way_density() -> None:
    stars = generate_starfield("galaxy", 4096)
    again = generate_starfield("galaxy", 4096)
    assert stars.sha256 == again.sha256 != generate_starfield("other", 4096).sha256
    prefix = generate_starfield("galaxy", 17)
    for field in ("equatorial_directions", "temperatures_k", "magnitudes", "linear_rgb"):
        np.testing.assert_array_equal(getattr(prefix, field), getattr(stars, field)[:17])
    np.testing.assert_allclose(np.linalg.norm(stars.equatorial_directions, axis=1), 1, atol=1e-12)
    assert np.all((stars.temperatures_k >= 3000) & (stars.temperatures_k <= 28000))
    assert np.all((stars.magnitudes >= -1.5) & (stars.magnitudes <= 8))
    assert np.median(stars.magnitudes) > 6
    assert np.all((stars.linear_rgb >= 0) & (stars.linear_rgb <= 1))
    pole = np.array(
        [
            math.cos(math.radians(27.12825)) * math.cos(math.radians(192.85948)),
            math.cos(math.radians(27.12825)) * math.sin(math.radians(192.85948)),
            math.sin(math.radians(27.12825)),
        ]
    )
    band = generate_starfield("galaxy", 4096, 1).equatorial_directions
    isotropic = generate_starfield("galaxy", 4096, 0).equatorial_directions
    assert np.mean(np.abs(band @ pole) < 0.2) > 0.93
    assert np.mean(np.abs(isotropic @ pole) < 0.2) < 0.25
    with pytest.raises(ValueError):
        stars.linear_rgb.setflags(write=True)


def test_stars_rotate_scintillate_and_disappear_in_daylight() -> None:
    stars = generate_starfield(31, 4096)
    night = celestial_state(Observer(35, 80, 0))
    directions, flux = stars.sample(night)
    moved, twinkled = stars.sample(night, 0.15)
    assert np.max(np.abs(directions - moved)) > 1e-6
    assert np.max(np.abs(flux - twinkled)) > 1e-5
    assert np.all(flux[directions[:, 2] <= 0] == 0)
    assert np.count_nonzero(flux) > 1500
    _, daytime = stars.sample(night, 12 * 3600)
    assert np.sum(daytime) < np.sum(flux) * 1e-5
    empty = generate_starfield(0, 0)
    assert empty.sample(night)[0].shape == (0, 3)
    assert empty.sample(night)[1].size == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"latitude_deg": 91},
        {"day_of_year": 366},
        {"day_of_year": 0},
        {"hour": 24},
        {"hour": math.nan},
        {"year": 1899},
        {"longitude_deg": 181},
        {"utc_offset_hours": 15},
        {"elevation_m": -1},
    ],
)
def test_observer_rejects_ambiguous_or_unphysical_inputs(kwargs: dict[str, float]) -> None:
    values = {"latitude_deg": 35, "day_of_year": 80, "hour": 12, **kwargs}
    with pytest.raises(ValueError):
        Observer(**values)  # type: ignore[arg-type]


def test_invalid_astronomy_buffers_and_options_are_rejected() -> None:
    with pytest.raises(ValueError):
        Atmosphere(view_samples=1)
    with pytest.raises(ValueError):
        Atmosphere(light_samples=1000)
    with pytest.raises(ValueError):
        henyey_greenstein(0, 1)
    with pytest.raises(ValueError):
        rayleigh_phase(1.1)
    with pytest.raises(ValueError):
        mie_coefficient(0)
    with pytest.raises(ValueError):
        blackbody_rgb(2999)
    with pytest.raises(ValueError):
        LunarPhase(1.1, 0, 0)
    with pytest.raises(ValueError):
        lunar_disc(LunarPhase(1, 0.5, 0), 3)
    with pytest.raises(ValueError):
        generate_starfield(0, 32_769)
    with pytest.raises(ValueError):
        generate_starfield(0, 1, 1.1)
    with pytest.raises(ValueError):
        CubeLUT(2, np.ones((7, 3)))
    with pytest.raises(ValueError):
        CubeLUT(2, np.full((8, 3), 2.0))
    spectrum = sky_spectrum(celestial_state(Observer(35, 80, 12)))
    with pytest.raises(ValueError):
        generate_cube(spectrum, 34)
    with pytest.raises(ValueError):
        replace(spectrum, radiance=np.zeros(80))
    with pytest.raises(ValueError):
        replace(spectrum, radiance=-np.ones(81))
    with pytest.raises(ValueError):
        replace(spectrum, transmittance=np.full(81, 2.0))
    with pytest.raises(ValueError):
        replace(generate_starfield(0, 8), magnitudes=np.zeros(7))
    with pytest.raises(FrozenInstanceError):
        spectrum.sun_elevation_deg = 0  # type: ignore[misc]
    assert isinstance(spectrum, SkySpectrum)
