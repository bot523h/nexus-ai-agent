"""Offline astronomical sky, single-scattering spectra, stars and .cube grades.

Coordinates are local east/north/up, angles are degrees, azimuth is clockwise
from north. Observer.hour is CIVIL time with an explicit UTC offset, not solar
time. Solar coordinates use the NOAA/Meeus low-order equations; the Moon uses
an elliptic orbit plus the principal Schlyter perturbations and topocentric
parallax. These are cinematic ephemerides (roughly arcminutes for the Sun and
sub-degree for the Moon in 1900--2100), NOT an occultation/navigation service.
Eclipses use geometric disc overlap, without refraction or limb darkening.

The atmosphere integrates a spherical, exponential Rayleigh/Mie atmosphere,
Earth shadow and optical depths on both light and view paths. It is a sampled
single-scattering approximation, not a multiple-scattering spectral renderer.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

import numpy as np

from ._determinism import (
    MAX_POINTS,
    RGB,
    FloatArray,
    Seed,
    Vec3,
    array_digest,
    digest,
    finite,
    frozen_floats,
    integer,
    normal,
    seed_bytes,
    uniform,
)

_EARTH_KM = 6378.137
_AU_KM = 149_597_870.7
_SUN_KM = 695_700.0
_MOON_KM = 1737.4
_WAVELENGTHS = np.arange(380.0, 781.0, 5.0)


def _sin(degrees: float) -> float:
    return math.sin(math.radians(degrees))


def _cos(degrees: float) -> float:
    return math.cos(math.radians(degrees))


@dataclass(frozen=True, slots=True)
class Observer:
    latitude_deg: float
    day_of_year: int
    hour: float
    year: int = 2026
    longitude_deg: float = 0.0
    utc_offset_hours: float = 0.0
    elevation_m: float = 0.0

    def __post_init__(self) -> None:
        finite(self.latitude_deg, "latitude", -90, 90)
        integer(self.year, "year", 1900, 2100)
        integer(self.day_of_year, "day_of_year", 1, 366 if calendar.isleap(self.year) else 365)
        finite(self.hour, "hour", 0, math.nextafter(24.0, 0.0))
        finite(self.longitude_deg, "longitude", -180, 180)
        finite(self.utc_offset_hours, "UTC offset", -14, 14)
        finite(self.elevation_m, "elevation", 0, 9000)

    @property
    def julian_day(self) -> float:
        instant = datetime(self.year, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=self.day_of_year - 1, hours=self.hour - self.utc_offset_hours
        )
        return 2440587.5 + instant.timestamp() / 86400.0

    def after(self, elapsed_seconds: float) -> Observer:
        """Advance civil time with correct day, leap-year and year rollover."""
        finite(elapsed_seconds, "elapsed seconds", -1e9, 1e9)
        instant = datetime(self.year, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=self.day_of_year - 1, hours=self.hour, seconds=elapsed_seconds
        )
        hour = (
            instant.hour + instant.minute / 60 + (instant.second + instant.microsecond / 1e6) / 3600
        )
        return Observer(
            self.latitude_deg,
            instant.timetuple().tm_yday,
            hour,
            instant.year,
            self.longitude_deg,
            self.utc_offset_hours,
            self.elevation_m,
        )

    @property
    def sidereal_deg(self) -> float:
        d = self.julian_day - 2451545.0
        t = d / 36525.0
        return (
            280.46061837
            + 360.98564736629 * d
            + 0.000387933 * t * t
            - t**3 / 38710000
            + self.longitude_deg
        ) % 360.0


@dataclass(frozen=True, slots=True)
class CelestialPosition:
    azimuth_deg: float
    elevation_deg: float
    direction: Vec3
    distance_km: float
    angular_radius_deg: float
    right_ascension_deg: float
    declination_deg: float


@dataclass(frozen=True, slots=True)
class LunarPhase:
    illuminated_fraction: float
    cycle_fraction: float
    bright_limb_angle_deg: float

    def __post_init__(self) -> None:
        finite(self.illuminated_fraction, "illuminated fraction", 0, 1)
        finite(self.cycle_fraction, "lunar cycle", 0, math.nextafter(1.0, 0.0))
        finite(self.bright_limb_angle_deg, "limb angle", -180, 180)

    @property
    def waxing(self) -> bool:
        return self.cycle_fraction < 0.5


@dataclass(frozen=True, slots=True)
class EclipseState:
    """Local solar obscuration (zero below horizon), geocentric lunar shadow."""

    solar_obscuration: float
    lunar_umbra_fraction: float
    lunar_penumbra_fraction: float


class SolarPeriod(str, Enum):
    DAWN = "dawn"
    MORNING = "morning"
    NOON = "noon"
    AFTERNOON = "afternoon"
    GOLDEN_HOUR = "golden_hour"
    TWILIGHT = "twilight"
    MIDNIGHT = "midnight"


@dataclass(frozen=True, slots=True)
class SkyState:
    observer: Observer
    sun: CelestialPosition
    moon: CelestialPosition
    lunar_phase: LunarPhase
    eclipse: EclipseState
    period: SolarPeriod


def _sun_equatorial(jd: float) -> tuple[FloatArray, float, float]:
    t = (jd - 2451545.0) / 36525.0
    mean_long = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360
    mean_anomaly = (357.52911 + t * (35999.05029 - 0.0001537 * t)) % 360
    eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    correction = (
        _sin(mean_anomaly) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + _sin(2 * mean_anomaly) * (0.019993 - 0.000101 * t)
        + 0.000289 * _sin(3 * mean_anomaly)
    )
    omega = 125.04 - 1934.136 * t
    longitude = mean_long + correction - 0.00569 - 0.00478 * _sin(omega)
    obliquity = (
        23
        + (26 + (21.448 - t * (46.815 + t * (0.00059 - 0.001813 * t))) / 60) / 60
        + 0.00256 * _cos(omega)
    )
    distance = (
        _AU_KM
        * 1.000001018
        * (1 - eccentricity**2)
        / (1 + eccentricity * _cos(mean_anomaly + correction))
    )
    xyz = distance * np.array(
        [_cos(longitude), _sin(longitude) * _cos(obliquity), _sin(longitude) * _sin(obliquity)]
    )
    return xyz, longitude, obliquity


def _moon_equatorial(jd: float, obliquity: float) -> tuple[FloatArray, float]:
    d = jd - 2451543.5
    node = (125.1228 - 0.0529538083 * d) % 360
    perigee = (318.0634 + 0.1643573223 * d) % 360
    anomaly = (115.3654 + 13.0649929509 * d) % 360
    e = 0.0549
    eccentric = math.radians(anomaly)
    for _ in range(8):
        eccentric -= (eccentric - e * math.sin(eccentric) - math.radians(anomaly)) / (
            1 - e * math.cos(eccentric)
        )
    xv, yv = (
        60.2666 * (math.cos(eccentric) - e),
        (60.2666 * math.sqrt(1 - e * e) * math.sin(eccentric)),
    )
    true_anomaly = math.degrees(math.atan2(yv, xv))
    radius = math.hypot(xv, yv)
    argument = true_anomaly + perigee
    x = _cos(node) * _cos(argument) - _sin(node) * _sin(argument) * _cos(5.1454)
    y = _sin(node) * _cos(argument) + _cos(node) * _sin(argument) * _cos(5.1454)
    z = _sin(argument) * _sin(5.1454)
    longitude = math.degrees(math.atan2(y, x))
    latitude = math.degrees(math.atan2(z, math.hypot(x, y)))
    sun_anomaly = (356.0470 + 0.9856002585 * d) % 360
    sun_long = sun_anomaly + 282.9404 + 0.0000470935 * d
    moon_long = node + perigee + anomaly
    elongation = moon_long - sun_long
    argument_lat = moon_long - node
    longitude += (
        -1.274 * _sin(anomaly - 2 * elongation)
        + 0.658 * _sin(2 * elongation)
        - 0.186 * _sin(sun_anomaly)
        - 0.059 * _sin(2 * anomaly - 2 * elongation)
        - 0.057 * _sin(anomaly - 2 * elongation + sun_anomaly)
        + 0.053 * _sin(anomaly + 2 * elongation)
        + 0.046 * _sin(2 * elongation - sun_anomaly)
        + 0.041 * _sin(anomaly - sun_anomaly)
        - 0.035 * _sin(elongation)
        - 0.031 * _sin(anomaly + sun_anomaly)
        - 0.015 * _sin(2 * argument_lat - 2 * elongation)
        + 0.011 * _sin(anomaly - 4 * elongation)
    )
    latitude += (
        -0.173 * _sin(argument_lat - 2 * elongation)
        - 0.055 * _sin(anomaly - argument_lat - 2 * elongation)
        - 0.046 * _sin(anomaly + argument_lat - 2 * elongation)
        + 0.033 * _sin(argument_lat + 2 * elongation)
        + 0.017 * _sin(2 * anomaly + argument_lat)
    )
    radius += -0.58 * _cos(anomaly - 2 * elongation) - 0.46 * _cos(2 * elongation)
    xe, ye, ze = (
        radius * _cos(longitude) * _cos(latitude),
        radius * _sin(longitude) * _cos(latitude),
        radius * _sin(latitude),
    )
    return _EARTH_KM * np.array(
        [
            xe,
            ye * _cos(obliquity) - ze * _sin(obliquity),
            ye * _sin(obliquity) + ze * _cos(obliquity),
        ]
    ), longitude % 360


def _position(xyz: FloatArray, radius_km: float, observer: Observer) -> CelestialPosition:
    lat, sidereal = math.radians(observer.latitude_deg), math.radians(observer.sidereal_deg)
    e2 = 0.00669437999014
    n = _EARTH_KM / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    height = observer.elevation_m / 1000
    local_origin = np.array(
        [
            (n + height) * math.cos(lat) * math.cos(sidereal),
            (n + height) * math.cos(lat) * math.sin(sidereal),
            (n * (1 - e2) + height) * math.sin(lat),
        ]
    )
    topocentric = xyz - local_origin
    distance = float(np.linalg.norm(topocentric))
    ra = math.atan2(float(topocentric[1]), float(topocentric[0]))
    dec = math.asin(float(topocentric[2]) / distance)
    hour_angle = sidereal - ra
    east = -math.cos(dec) * math.sin(hour_angle)
    north = math.sin(dec) * math.cos(lat) - math.cos(dec) * math.cos(hour_angle) * math.sin(lat)
    up = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(hour_angle) * math.cos(lat)
    return CelestialPosition(
        math.degrees(math.atan2(east, north)) % 360,
        math.degrees(math.asin(max(-1.0, min(1.0, up)))),
        (east, north, up),
        distance,
        math.degrees(math.asin(radius_km / distance)),
        math.degrees(ra) % 360,
        math.degrees(dec),
    )


def disc_overlap_fraction(radius: float, occluder_radius: float, separation: float) -> float:
    """Fraction of the first disc hidden by the second (all units must agree)."""
    r = finite(radius, "disc radius", 1e-12, 1e9)
    s = finite(occluder_radius, "occluder radius", 1e-12, 1e9)
    d = finite(separation, "separation", 0, 1e12)
    if d >= r + s:
        return 0.0
    if d <= abs(r - s):
        return min(1.0, (s / r) ** 2)
    a = math.acos(max(-1.0, min(1.0, (d * d + r * r - s * s) / (2 * d * r))))
    b = math.acos(max(-1.0, min(1.0, (d * d + s * s - r * r) / (2 * d * s))))
    triangle = math.sqrt(max(0.0, (-d + r + s) * (d + r - s) * (d - r + s) * (d + r + s))) / 2
    return max(0.0, min(1.0, (r * r * a + s * s * b - triangle) / (math.pi * r * r)))


def solar_period(elevation_deg: float, hour_angle_deg: float) -> SolarPeriod:
    altitude = finite(elevation_deg, "solar elevation", -90, 90)
    angle = finite(hour_angle_deg, "hour angle", -180, 180)
    if altitude < -18:
        return SolarPeriod.MIDNIGHT
    if altitude < -0.833:
        return SolarPeriod.DAWN if angle < 0 and altitude >= -12 else SolarPeriod.TWILIGHT
    if altitude < 10:
        return SolarPeriod.GOLDEN_HOUR
    if abs(angle) <= 15:
        return SolarPeriod.NOON
    return SolarPeriod.MORNING if angle < 0 else SolarPeriod.AFTERNOON


def celestial_state(observer: Observer) -> SkyState:
    sun_xyz, sun_long, obliquity = _sun_equatorial(observer.julian_day)
    moon_xyz, moon_long = _moon_equatorial(observer.julian_day, obliquity)
    sun = _position(sun_xyz, _SUN_KM, observer)
    moon = _position(moon_xyz, _MOON_KM, observer)
    moon_distance = float(np.linalg.norm(moon_xyz))
    sun_distance = float(np.linalg.norm(sun_xyz))
    from_moon_to_sun = sun_xyz - moon_xyz
    cos_phase = float(np.dot(-moon_xyz, from_moon_to_sun)) / (
        moon_distance * float(np.linalg.norm(from_moon_to_sun))
    )
    delta_ra = sun.right_ascension_deg - moon.right_ascension_deg
    limb = math.degrees(
        math.atan2(
            _cos(sun.declination_deg) * _sin(delta_ra),
            _sin(sun.declination_deg) * _cos(moon.declination_deg)
            - _cos(sun.declination_deg) * _sin(moon.declination_deg) * _cos(delta_ra),
        )
    )
    phase = LunarPhase(
        max(0.0, min(1.0, (1 + cos_phase) / 2)), ((moon_long - sun_long) % 360) / 360, limb
    )
    separation = math.degrees(
        math.acos(float(np.clip(np.dot(sun.direction, moon.direction), -1, 1)))
    )
    anti_sun_angle = math.degrees(
        math.acos(
            float(np.clip(-np.dot(moon_xyz, sun_xyz) / (moon_distance * sun_distance), -1, 1))
        )
    )
    umbra_km = _EARTH_KM - moon_distance * (_SUN_KM - _EARTH_KM) / sun_distance
    penumbra_km = _EARTH_KM + moon_distance * (_SUN_KM + _EARTH_KM) / sun_distance
    moon_radius = math.degrees(math.asin(_MOON_KM / moon_distance))
    eclipse = EclipseState(
        disc_overlap_fraction(sun.angular_radius_deg, moon.angular_radius_deg, separation)
        if sun.elevation_deg + sun.angular_radius_deg > 0
        else 0.0,
        disc_overlap_fraction(
            moon_radius, math.degrees(math.asin(umbra_km / moon_distance)), anti_sun_angle
        ),
        disc_overlap_fraction(
            moon_radius, math.degrees(math.asin(penumbra_km / moon_distance)), anti_sun_angle
        ),
    )
    hour_angle = (observer.sidereal_deg - sun.right_ascension_deg + 180) % 360 - 180
    return SkyState(
        observer, sun, moon, phase, eclipse, solar_period(sun.elevation_deg, hour_angle)
    )


def lunar_disc(phase: LunarPhase, size: int = 64) -> FloatArray:
    """Lambertian sphere with its geometric terminator; outside the disc is zero."""
    integer(size, "lunar disc size", 4, 512)
    grid = np.linspace(-1, 1, size)
    x, y = np.meshgrid(grid, grid)
    inside = x * x + y * y <= 1
    z = np.sqrt(np.maximum(0, 1 - x * x - y * y))
    cos_phase = 2 * phase.illuminated_fraction - 1
    lateral = math.sqrt(max(0.0, 1 - cos_phase * cos_phase))
    angle = math.radians(phase.bright_limb_angle_deg)
    light = lateral * (x * math.sin(angle) + y * math.cos(angle)) + z * cos_phase
    return frozen_floats(np.where(inside, np.maximum(0, light), 0))


def rayleigh_coefficient(wavelength_nm: float, pressure_pa: float = 101325.0) -> float:
    wavelength = finite(wavelength_nm, "wavelength nm", 100, 100_000)
    finite(pressure_pa, "pressure", 0, 200_000)
    return 1.32e-5 * (550 / wavelength) ** 4 * pressure_pa / 101325


def mie_coefficient(wavelength_nm: float, turbidity: float = 2.0, angstrom: float = 1.3) -> float:
    wavelength = finite(wavelength_nm, "wavelength nm", 100, 100_000)
    finite(turbidity, "turbidity", 0, 20)
    finite(angstrom, "Angstrom exponent", 0, 4)
    return float(2e-6 * turbidity * (550 / wavelength) ** angstrom)


def rayleigh_phase(cos_gamma: float) -> float:
    finite(cos_gamma, "phase cosine", -1, 1)
    return 3 * (1 + cos_gamma * cos_gamma) / (16 * math.pi)


def henyey_greenstein(cos_gamma: float, g: float = 0.76) -> float:
    finite(cos_gamma, "phase cosine", -1, 1)
    finite(g, "Mie anisotropy", -0.999, 0.999)
    return float((1 - g * g) / (4 * math.pi * (1 + g * g - 2 * g * cos_gamma) ** 1.5))


def planck_radiance(wavelength_nm: float, temperature_k: float) -> float:
    """Planck spectral radiance in W sr^-1 m^-3 (wavelength input is nm)."""
    wavelength = finite(wavelength_nm, "wavelength nm", 100, 100_000) * 1e-9
    temperature = finite(temperature_k, "temperature K", 100, 1_000_000)
    exponent = 0.014387768775039337 / (wavelength * temperature)
    if exponent > 700:
        return 0.0  # Wien tail underflows physically; do not overflow expm1.
    return 1.1910429723971884e-16 / (wavelength**5 * math.expm1(exponent))


def _planck(wavelengths: FloatArray, temperatures: FloatArray) -> FloatArray:
    wavelengths_m = wavelengths * 1e-9
    return 1.1910429723971884e-16 / (
        wavelengths_m**5 * np.expm1(0.014387768775039337 / (temperatures * wavelengths_m))
    )


def _cie_rgb(spectra: FloatArray) -> FloatArray:
    """Analytic CIE 1931 fits (Wyman et al.), XYZ -> linear sRGB, no gamma."""
    w = _WAVELENGTHS

    def gaussian(center: float, left: float, right: float) -> FloatArray:
        return np.exp(-0.5 * ((w - center) * np.where(w < center, left, right)) ** 2)

    x = (
        0.362 * gaussian(442, 0.0624, 0.0374)
        + 1.056 * gaussian(599.8, 0.0264, 0.0323)
        - 0.065 * gaussian(501.1, 0.049, 0.0382)
    )
    y = 0.821 * gaussian(568.8, 0.0213, 0.0247) + 0.286 * gaussian(530.9, 0.0613, 0.0322)
    z = 1.217 * gaussian(437, 0.0845, 0.0278) + 0.681 * gaussian(459, 0.0385, 0.0725)
    xyz = spectra @ np.stack([x, y, z], axis=1) / float(np.sum(y))
    matrix = np.array(
        [[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415], [0.0557, -0.2040, 1.0570]]
    )
    return np.asarray(np.maximum(0, xyz @ matrix.T), dtype=np.float64)


def blackbody_rgb(temperature_k: float) -> RGB:
    finite(temperature_k, "stellar temperature K", 3000, 28000)
    rgb = _cie_rgb(_planck(_WAVELENGTHS, np.array(temperature_k)))
    rgb /= float(np.max(rgb))
    return float(rgb[0]), float(rgb[1]), float(rgb[2])


@dataclass(frozen=True, slots=True)
class Atmosphere:
    pressure_pa: float = 101325.0
    turbidity: float = 2.0
    mie_g: float = 0.76
    rayleigh_scale_height_m: float = 8000.0
    mie_scale_height_m: float = 1200.0
    view_samples: int = 24
    light_samples: int = 12

    def __post_init__(self) -> None:
        finite(self.pressure_pa, "pressure", 0, 200_000)
        finite(self.turbidity, "turbidity", 0, 20)
        finite(self.mie_g, "Mie g", -0.999, 0.999)
        finite(self.rayleigh_scale_height_m, "Rayleigh height", 100, 20000)
        finite(self.mie_scale_height_m, "Mie height", 100, 10000)
        integer(self.view_samples, "view samples", 8, 128)
        integer(self.light_samples, "light samples", 8, 64)


@dataclass(frozen=True, slots=True)
class SkySpectrum:
    wavelengths_nm: FloatArray
    radiance: FloatArray
    transmittance: FloatArray
    linear_rgb: RGB
    sun_elevation_deg: float
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("wavelengths_nm", "radiance", "transmittance"):
            values = frozen_floats(getattr(self, name))
            if values.shape != (81,):
                raise ValueError("sky spectra have 81 samples from 380 to 780 nm")
            object.__setattr__(self, name, values)
        if (self.radiance < 0).any() or ((self.transmittance < 0) | (self.transmittance > 1)).any():
            raise ValueError("radiance must be nonnegative and transmittance in [0, 1]")
        object.__setattr__(
            self,
            "sha256",
            digest(
                (
                    array_digest(self.wavelengths_nm),
                    array_digest(self.radiance),
                    array_digest(self.transmittance),
                    self.linear_rgb,
                    self.sun_elevation_deg,
                )
            ),
        )


_DEFAULT_ATMOSPHERE = Atmosphere()


def sky_spectrum(
    state: SkyState,
    view_azimuth_deg: float = 0.0,
    view_elevation_deg: float = 35.0,
    atmosphere: Atmosphere = _DEFAULT_ATMOSPHERE,
) -> SkySpectrum:
    finite(view_azimuth_deg, "view azimuth", 0, 360)
    finite(view_elevation_deg, "view elevation", 0, 90)
    radius, top = 6_360_000.0, 6_440_000.0
    origin = np.array([0.0, 0.0, radius + state.observer.elevation_m])
    direction = np.array(
        [
            _sin(view_azimuth_deg) * _cos(view_elevation_deg),
            _cos(view_azimuth_deg) * _cos(view_elevation_deg),
            _sin(view_elevation_deg),
        ]
    )
    projection = float(np.dot(origin, direction))
    length = -projection + math.sqrt(projection**2 + top**2 - float(origin @ origin))
    ds = length / atmosphere.view_samples
    points = origin + ((np.arange(atmosphere.view_samples) + 0.5) * ds)[:, None] * direction
    heights = np.linalg.norm(points, axis=1) - radius
    density_r = np.exp(-heights / atmosphere.rayleigh_scale_height_m)
    density_m = np.exp(-heights / atmosphere.mie_scale_height_m)
    view_r = (np.cumsum(density_r) - 0.5 * density_r) * ds
    view_m = (np.cumsum(density_m) - 0.5 * density_m) * ds
    beta_r = 1.32e-5 * (550 / _WAVELENGTHS) ** 4 * atmosphere.pressure_pa / 101325
    beta_m = 2e-6 * atmosphere.turbidity * (550 / _WAVELENGTHS) ** 1.3
    radiance = np.zeros_like(_WAVELENGTHS)
    solar = _planck(_WAVELENGTHS, np.array(5778.0))
    solar /= float(np.max(solar))
    sources = (
        (state.sun, 20.0 * (1 - state.eclipse.solar_obscuration)),
        (
            state.moon,
            0.00005
            * state.lunar_phase.illuminated_fraction
            * (1 - 0.97 * state.eclipse.lunar_umbra_fraction),
        ),
    )
    for light, intensity in sources:
        light_dir = np.asarray(light.direction)
        dot = points @ light_dir
        r2 = np.sum(points * points, axis=1)
        sun_length = -dot + np.sqrt(dot * dot + top * top - r2)
        light_step = sun_length / atmosphere.light_samples
        distances = (np.arange(atmosphere.light_samples) + 0.5)[None, :] * light_step[:, None]
        samples = points[:, None, :] + distances[:, :, None] * light_dir
        light_heights = np.linalg.norm(samples, axis=2) - radius
        blocked = (dot < 0) & (r2 - dot * dot < radius * radius)
        light_heights = np.maximum(light_heights, 0)
        light_r = np.sum(np.exp(-light_heights / atmosphere.rayleigh_scale_height_m), axis=1)
        light_m = np.sum(np.exp(-light_heights / atmosphere.mie_scale_height_m), axis=1)
        optical = (view_r + light_r * light_step)[:, None] * beta_r + (
            view_m + light_m * light_step
        )[:, None] * beta_m
        attenuation = np.exp(-optical) * (~blocked)[:, None]
        cos_gamma = float(np.clip(direction @ light_dir, -1, 1))
        scatter = density_r[:, None] * beta_r * rayleigh_phase(cos_gamma) + density_m[
            :, None
        ] * beta_m * henyey_greenstein(cos_gamma, atmosphere.mie_g)
        radiance += np.sum(attenuation * scatter, axis=0) * ds * solar * intensity
    # Weak night airglow; a dark vacuum needs both pressure and turbidity zero.
    radiance += (1e-7 * atmosphere.pressure_pa / 101325) * np.exp(
        -0.5 * ((_WAVELENGTHS - 558) / 30) ** 2
    )
    transmittance = np.exp(-(np.sum(density_r) * ds * beta_r + np.sum(density_m) * ds * beta_m))
    rgb = _cie_rgb(radiance)
    return SkySpectrum(
        _WAVELENGTHS,
        radiance,
        transmittance,
        (float(rgb[0]), float(rgb[1]), float(rgb[2])),
        state.sun.elevation_deg,
    )


@dataclass(frozen=True, slots=True)
class CubeLUT:
    size: int
    values: FloatArray
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        integer(self.size, "cube size", 2, 33)
        values = frozen_floats(self.values)
        if values.shape != (self.size**3, 3) or ((values < 0) | (values > 1)).any():
            raise ValueError("invalid 3D LUT dimensions or domain")
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "sha256", digest((self.size, array_digest(values))))

    def to_cube(self) -> str:
        """Adobe/Resolve .cube, red index varies fastest; values are linear RGB."""
        header = (
            f'TITLE "NAGAR World Backlot v1"\nLUT_3D_SIZE {self.size}\n'
            "DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n"
        )
        return header + "".join(f"{r:.7f} {g:.7f} {b:.7f}\n" for r, g, b in self.values)


def generate_cube(spectrum: SkySpectrum, size: int = 9, exposure_ev: float = 0.0) -> CubeLUT:
    integer(size, "cube size", 2, 33)
    finite(exposure_ev, "exposure EV", -10, 10)
    axis = np.linspace(0, 1, size)
    blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")
    values = np.stack([red.ravel(), green.ravel(), blue.ravel()], axis=1)
    sky = np.asarray(spectrum.linear_rgb)
    illuminant = sky / max(float(np.mean(sky)), 1e-12)
    # A direct daylight component prevents complementary-blue sunset shadows
    # dominating the grade. At night the photopic grade is deliberately cool.
    warmth = math.exp(-(((spectrum.sun_elevation_deg - 1) / 10) ** 2))
    night = 1 / (1 + math.exp((spectrum.sun_elevation_deg + 8) / 2))
    tint = (1 - night) * ((0.8 + 0.2 * illuminant) + warmth * np.array([0.35, 0, -0.25]))
    tint += night * np.array([0.65, 0.82, 1.2])
    graded = values * tint * 2**exposure_ev
    luminance = values @ np.array([0.2126, 0.7152, 0.0722])
    haze = 0.018 * (1 - luminance[:, None]) * np.minimum(illuminant, 1.5)
    return CubeLUT(size, np.clip(graded + haze, 0, 1))


@dataclass(frozen=True, slots=True)
class StarField:
    equatorial_directions: FloatArray
    temperatures_k: FloatArray
    magnitudes: FloatArray
    linear_rgb: FloatArray
    scintillation_phase: FloatArray
    scintillation_hz: FloatArray
    seed_sha256: str
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        count = len(self.temperatures_k)
        integer(count, "star count", 0, MAX_POINTS)
        names = (
            "equatorial_directions",
            "temperatures_k",
            "magnitudes",
            "linear_rgb",
            "scintillation_phase",
            "scintillation_hz",
        )
        hashes: list[str] = []
        for name in names:
            array = frozen_floats(getattr(self, name))
            shape = (count, 3) if name in ("equatorial_directions", "linear_rgb") else (count,)
            if array.shape != shape:
                raise ValueError("inconsistent starfield shapes")
            object.__setattr__(self, name, array)
            hashes.append(array_digest(array))
        object.__setattr__(self, "sha256", digest((self.seed_sha256, hashes)))

    def sample(
        self, state: SkyState, elapsed_seconds: float = 0.0
    ) -> tuple[FloatArray, FloatArray]:
        """Horizontal directions and extinction/scintillation/daylight-weighted flux."""
        finite(elapsed_seconds, "elapsed seconds", -1e9, 1e9)
        if elapsed_seconds:
            state = celestial_state(state.observer.after(elapsed_seconds))
        sidereal = math.radians(state.observer.sidereal_deg)
        lat = math.radians(state.observer.latitude_deg)
        transform = np.array(
            [
                [-math.sin(sidereal), math.cos(sidereal), 0],
                [
                    -math.sin(lat) * math.cos(sidereal),
                    -math.sin(lat) * math.sin(sidereal),
                    math.cos(lat),
                ],
                [
                    math.cos(lat) * math.cos(sidereal),
                    math.cos(lat) * math.sin(sidereal),
                    math.sin(lat),
                ],
            ]
        )
        directions = self.equatorial_directions @ transform.T
        up = directions[:, 2]
        airmass = 1 / np.maximum(0.03, up)
        extinction = np.exp(-0.12 * airmass)
        amplitude = np.minimum(0.7, 0.06 * np.sqrt(airmass))
        twinkle = 1 + amplitude * np.sin(
            2 * math.pi * self.scintillation_hz * elapsed_seconds + self.scintillation_phase
        )
        visibility = 1 / (1 + math.exp((state.sun.elevation_deg + 12) / 1.5))
        flux = 10 ** (-0.4 * self.magnitudes) * extinction * twinkle * visibility * (up > 0)
        return frozen_floats(directions), frozen_floats(flux)


def generate_starfield(seed: Seed, count: int = 4096, milky_way_fraction: float = 0.3) -> StarField:
    integer(count, "star count", 0, MAX_POINTS)
    finite(milky_way_fraction, "Milky Way fraction", 0, 1)
    azimuth = 2 * math.pi * uniform(seed, "stars/longitude", count)
    z = 2 * uniform(seed, "stars/latitude", count) - 1
    xy = np.sqrt(1 - z * z)
    directions = np.stack([xy * np.cos(azimuth), xy * np.sin(azimuth), z], axis=1)
    pole = np.array(
        [_cos(27.12825) * _cos(192.85948), _cos(27.12825) * _sin(192.85948), _sin(27.12825)]
    )
    tangent = np.cross(pole, [0.0, 0.0, 1.0])
    tangent /= np.linalg.norm(tangent)
    bitangent = np.cross(pole, tangent)
    latitude = np.clip(normal(seed, "stars/galactic-latitude", count) * 0.10, -0.5, 0.5)
    band = (
        np.cos(latitude)[:, None]
        * (np.cos(azimuth)[:, None] * tangent + np.sin(azimuth)[:, None] * bitangent)
        + np.sin(latitude)[:, None] * pole
    )
    mask = uniform(seed, "stars/membership", count) < milky_way_fraction
    directions[mask] = band[mask]
    temperatures = 3000 * (28000 / 3000) ** uniform(seed, "stars/temperature", count)
    magnitude = (
        np.log10(
            10 ** (-0.45) + (10**2.4 - 10 ** (-0.45)) * uniform(seed, "stars/magnitude", count)
        )
        / 0.3
    )
    table_t = np.linspace(3000, 28000, 256)
    table_rgb = _cie_rgb(_planck(_WAVELENGTHS[None, :], table_t[:, None]))
    table_rgb /= np.max(table_rgb, axis=1)[:, None]
    rgb = np.stack([np.interp(temperatures, table_t, table_rgb[:, i]) for i in range(3)], axis=1)
    return StarField(
        directions,
        temperatures,
        magnitude,
        rgb,
        2 * math.pi * uniform(seed, "stars/scintillation-phase", count),
        3 + 12 * uniform(seed, "stars/scintillation-frequency", count),
        seed_bytes(seed, "stars").hex(),
    )
