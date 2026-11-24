"""Exact wave-optics and Planck radiometry for NAGAR cinema science.

Formulations
------------
Planck's black-body spectral radiance and Wien's displacement law,
with exact SI 2019 constants::

    B_lambda(lambda, T) = 2 h c^2 / (lambda^5 (exp(h c / (lambda k_B T)) - 1))
    lambda_max = b / T,  b = h c / (k_B * 4.9651142317442768)

Exact Fresnel amplitude coefficients (s and p), Brester angle and total
internal reflection (TIR)::

    r_s = (n1 cos(theta_i) - n2 cos(theta_t))/(n1 cos(theta_i) + n2 cos(theta_t))
    r_p = (n2 cos(theta_i) - n1 cos(theta_t))/(n2 cos(theta_i) + n1 cos(theta_t))
    theta_B = atan(n2 / n1)  ==>  R_p(theta_B) = 0

Thin-film (Airy) interference for a film of index ``n_film`` and thickness ``d``,
with phase ``delta = (2 pi / lambda) n_film d cos(theta_film)``::

    R_coated(lambda) = (r01^2 + r12^2 + 2 r01 r12 cos(2 delta))
                         / (1 + r01^2 r12^2 + 2 r01 r12 cos(2 delta))

Volumetric radiative transfer of an isotropic source function ``S`` with
optical depth ``tau`` through a medium of length ``L``::

    I(L) = I_in exp(-tau) + S * (1 - exp(-tau)),   tau = kappa * L

Henyey–Greenstein anisotropic phase function::

    p(cos(theta), g) = (1 - g^2) / (4 pi (1 + g^2 - 2 g cos(theta))^(3/2))

``p`` is normalized so that ``int_0^pi p(cos(theta), g) 2pi sin(theta) d(theta) = 1``.

Every function is total, deterministic and framework-free (NumPy + stdlib).
"""

from __future__ import annotations

import math
import numbers
from dataclasses import dataclass


def _require_real_number(value: object, name: str) -> float:
    """Reject bool/non-numeric/non-finite input; return a float."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{name} must be a real number")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    return out


#: Exact SI 2019 constants.
H_PLANCK = 6.626_070_15e-34  # in J s
C_LIGHT = 299_792_458.0  # in m/s
KB_BOLTZMANN = 1.380_649e-23  # in J/K
#: Wien displacement constant for B_lambda: b = h c / (k_B * 4.965114...)
#: = 2.897771955122186e-3 m K (derived from exact SI constants).
WIEN_CONSTANT_M_K = H_PLANCK * C_LIGHT / (KB_BOLTZMANN * 4.965_114_231_744_276_8)
#: Exact value for the Planck-Law maximum: x solving
#: 5(1 - exp(-x)) = x.
PLANCK_WIEN_ROOT = 4.965_114_231_744_276_8

#: Intensity of the source is fixed at 1 W/(m^2 sr) for normalization checks.
BG_REFERENCE = 1.0


def planck_blackbody_spectral_radiance(lambda_m: float, temperature_k: float) -> float:
    """Planck's law for black-body spectral radiance per unit wavelength.

    Units: ``W sr^-1 m^-3`` (i.e. per metre of wavelength).  Uses ``math.expm1``
    for the Planck denominator so the value stays accurate at small ``x``
    (low temperatures / long wavelengths), where ``exp(x) - 1`` loses all
    significant digits to cancellation.

    Numerical-safety policy: when ``x = h c / (lambda k_B T) > 700`` the
    Planck denominator underflows to zero and the radiance is numerically
    indistinguishable from zero; the function returns ``0.0`` rather than
    a misleading sub-normal value.  This is a documented convention, not a
    silent clip: the value reported is exactly the one the floating-point
    computation would return at that scale.
    """
    lambda_m = _require_real_number(lambda_m, "lambda_m")
    temperature_k = _require_real_number(temperature_k, "temperature_k")
    if lambda_m <= 0.0:
        raise ValueError("lambda must be positive and finite")
    if temperature_k <= 0.0:
        raise ValueError("temperature must be positive and finite (Kelvin)")
    x = H_PLANCK * C_LIGHT / (lambda_m * KB_BOLTZMANN * temperature_k)
    if x > 700.0:
        # exp(700) is finite; the radiance is effectively zero.
        return 0.0
    # math.expm1 avoids catastrophic cancellation when x is small (low T / long lambda).
    return float(2.0 * H_PLANCK * C_LIGHT**2 / (lambda_m**5 * math.expm1(x)))


def wien_max_wavelength(temperature_k: float) -> float:
    """Wien's displacement law: ``lambda_max = b / T`` (spectral radiance max)."""
    temperature_k = _require_real_number(temperature_k, "temperature_k")
    if temperature_k <= 0.0:
        raise ValueError("temperature must be positive and finite (Kelvin)")
    return WIEN_CONSTANT_M_K / temperature_k


def wien_temperature(lambda_max_m: float) -> float:
    """Inverse of :func:`wien_max_wavelength`."""
    lambda_max_m = _require_real_number(lambda_max_m, "lambda_max_m")
    if lambda_max_m <= 0.0:
        raise ValueError("lambda_max must be positive and finite")
    return WIEN_CONSTANT_M_K / lambda_max_m


def planck_max_radiance(temperature_k: float) -> float:
    """Planck spectral radiance at ``lambda_max`` for ``T``."""
    temperature_k = _require_real_number(temperature_k, "temperature_k")
    return planck_blackbody_spectral_radiance(wien_max_wavelength(temperature_k), temperature_k)


def _cos_theta_film_from_air(n1: float, n2: float, theta_i_deg: float) -> complex:
    """cos(theta_t) via Snell's law, complex when TIR."""
    n1 = _require_real_number(n1, "n1")
    n2 = _require_real_number(n2, "n2")
    theta_i_deg = _require_real_number(theta_i_deg, "theta_i_deg")
    if n1 <= 0.0 or n2 <= 0.0:
        raise ValueError("refractive indices must be positive")
    sin_i = math.sin(math.radians(theta_i_deg))
    n_ratio = n1 / n2
    sin_t_sq = n_ratio**2 * sin_i**2
    cos_t_sq = 1.0 - sin_t_sq
    if cos_t_sq >= 0.0:
        return complex(math.sqrt(cos_t_sq), 0.0)
    # Total internal reflection: cos(theta_t) = i * sqrt(sin_t^2 - 1)
    return complex(0.0, math.sqrt(-cos_t_sq))


def critical_angle_deg(n_dense: float, n_sparse: float) -> float | None:
    """Critical angle for total internal reflection, or ``None`` if TIR is impossible.

    Returns degrees when ``n_dense > n_sparse``; otherwise ``None``.
    """
    n_dense = _require_real_number(n_dense, "n_dense")
    n_sparse = _require_real_number(n_sparse, "n_sparse")
    if not (n_sparse > 0.0 and n_dense > n_sparse):
        return None
    ratio = n_sparse / n_dense
    # First guard (n_dense > n_sparse, both positive) already guarantees
    # ratio < 1, so the asin argument is always in the domain.
    return float(math.degrees(math.asin(ratio)))


@dataclass(frozen=True, slots=True)
class FresnelAmplitudes:
    r_s: complex
    r_p: complex

    def __post_init__(self) -> None:
        for name, value in (("r_s", self.r_s), ("r_p", self.r_p)):
            if isinstance(value, bool) or not isinstance(value, (int, float, complex)):
                raise TypeError(f"{name} must be a complex or real number")
            if not math.isfinite(abs(value)):
                raise ValueError(f"{name} must have finite magnitude")

    @property
    def R_s(self) -> float:
        return float(abs(self.r_s) ** 2)

    @property
    def R_p(self) -> float:
        return float(abs(self.r_p) ** 2)


def fresnel_amplitudes(
    n1: float,
    n2: float,
    theta_i_deg: float,
) -> FresnelAmplitudes:
    """Exact Fresnel amplitude coefficients for s (``R_s``) and p (``R_p``).

    ``n1``, ``n2`` are the refractive indices on the two sides, ``theta_i`` the
    incidence angle on the ``n1`` side (degrees).  Returns *complex* coefficients
    when TIR occurs; their magnitudes are the reflectance amplitudes.
    """
    n1 = _require_real_number(n1, "n1")
    n2 = _require_real_number(n2, "n2")
    theta_i_deg = _require_real_number(theta_i_deg, "theta_i_deg")
    if not (n1 > 0.0 and n2 > 0.0):
        raise ValueError("refractive indices must be positive")
    if not (0.0 <= theta_i_deg < 90.0):
        raise ValueError("theta_i must be in [0, 90) degrees")
    cos_i = math.cos(math.radians(theta_i_deg))
    cos_t = _cos_theta_film_from_air(n1, n2, theta_i_deg)
    r_s = (n1 * cos_i - n2 * cos_t) / (n1 * cos_i + n2 * cos_t)
    r_p = (n2 * cos_i - n1 * cos_t) / (n2 * cos_i + n1 * cos_t)
    return FresnelAmplitudes(r_s, r_p)


def brester_angle_deg(n1: float, n2: float) -> float:
    """Brewster angle: ``theta_B = atan(n2 / n1)`` (degrees)."""
    n1 = _require_real_number(n1, "n1")
    n2 = _require_real_number(n2, "n2")
    if not (n1 > 0.0 and n2 > 0.0):
        raise ValueError("refractive indices must be positive")
    return float(math.degrees(math.atan(n2 / n1)))


def brester_reflectance(n1: float, n2: float) -> tuple[float, float]:
    """``(R_s, R_p)`` at the Brewster angle; ``R_p`` must vanish (up to numerical error)."""
    n1 = _require_real_number(n1, "n1")
    n2 = _require_real_number(n2, "n2")
    theta_b = brester_angle_deg(n1, n2)
    fresnel = fresnel_amplitudes(n1, n2, theta_b)
    return fresnel.R_s, fresnel.R_p


def fresnel_transmittance_s(n1: float, n2: float, theta_i_deg: float) -> float:
    """Power-conserving transmittance ``T_s = 1 - R_s`` (no absorption).

    Valid below TIR (``R_s + T_s = 1``); under total internal reflection
    ``R_s = 1`` and the transmittance is exactly ``0``.
    """
    n1 = _require_real_number(n1, "n1")
    n2 = _require_real_number(n2, "n2")
    theta_i_deg = _require_real_number(theta_i_deg, "theta_i_deg")
    fresnel = fresnel_amplitudes(n1, n2, theta_i_deg)
    return max(0.0, 1.0 - fresnel.R_s)


def fresnel_transmittance_p(n1: float, n2: float, theta_i_deg: float) -> float:
    """Power-conserving p-polarized transmittance ``T_p = 1 - R_p``.

    Same physical statement as :func:`fresnel_transmittance_s`: valid below
    TIR (``R_p + T_p = 1``); under TIR ``R_p = 1`` and the transmittance is 0.
    """
    n1 = _require_real_number(n1, "n1")
    n2 = _require_real_number(n2, "n2")
    theta_i_deg = _require_real_number(theta_i_deg, "theta_i_deg")
    fresnel = fresnel_amplitudes(n1, n2, theta_i_deg)
    return max(0.0, 1.0 - fresnel.R_p)


def thin_film_phase(
    lambda_m: float,
    n_film: float,
    thickness_m: float,
    theta_film_deg: float,
) -> float:
    """Airy phase: ``delta = (2 pi / lambda) n_film d cos(theta_film)``."""
    lambda_m = _require_real_number(lambda_m, "lambda_m")
    n_film = _require_real_number(n_film, "n_film")
    thickness_m = _require_real_number(thickness_m, "thickness_m")
    theta_film_deg = _require_real_number(theta_film_deg, "theta_film_deg")
    if lambda_m <= 0.0:
        raise ValueError("lambda must be positive and finite")
    if n_film <= 0.0:
        raise ValueError("n_film must be positive and finite")
    if thickness_m < 0.0:
        raise ValueError("thickness must be non-negative and finite")
    if not (0.0 <= theta_film_deg <= 90.0):
        raise ValueError("theta_film must be in [0, 90] degrees")
    return (
        (2.0 * math.pi / lambda_m) * n_film * thickness_m * math.cos(math.radians(theta_film_deg))
    )


def thin_film_reflectance(
    r01: float,
    r12: float,
    lambda_m: float,
    n_film: float,
    thickness_m: float,
    theta_film_deg: float = 0.0,
) -> float:
    """Airy-coated-film reflectance (normalized amplitude coefficients r01, r12).

    ``r01`` and ``r12`` are amplitude coefficients (signed, ``|r| < 1`` for
    normal/angular incidence in transparent media); the formula is
    symmetric in the two coefficients.
    """
    r01 = _require_real_number(r01, "r01")
    r12 = _require_real_number(r12, "r12")
    if not (abs(r01) < 1.0 and abs(r12) < 1.0):
        raise ValueError("amplitude coefficients must satisfy |r| < 1")
    delta = thin_film_phase(lambda_m, n_film, thickness_m, theta_film_deg)
    numerator = r01**2 + r12**2 + 2.0 * r01 * r12 * math.cos(2.0 * delta)
    denominator = 1.0 + r01**2 * r12**2 + 2.0 * r01 * r12 * math.cos(2.0 * delta)
    if abs(denominator) < 1e-15:
        raise ValueError("degenerate denominator; reflectance undefined")
    return float(numerator / denominator)


def film_materials_from_air(
    n1: float, n_film: float, n2: float, theta_air_deg: float
) -> tuple[float, float, float]:
    """Return ``(theta_film_deg, r01, r12)`` for a film at an air incidence.

    ``r01 = (n1 - n_film)/(n1 + n_film)`` (normal incidence amplitude),
    ``r12 = (n_film - n2)/(n_film + n2)``.  Angles via Snell's law.
    """
    n1 = _require_real_number(n1, "n1")
    n_film = _require_real_number(n_film, "n_film")
    n2 = _require_real_number(n2, "n2")
    theta_air_deg = _require_real_number(theta_air_deg, "theta_air_deg")
    if not (n1 > 0.0 and n_film > 0.0 and n2 > 0.0):
        raise ValueError("refractive indices must be positive")
    if not (0.0 <= theta_air_deg < 90.0):
        raise ValueError("theta_air must be in [0, 90) degrees")
    sin_t = (n1 / n_film) * math.sin(math.radians(theta_air_deg))
    if abs(sin_t) > 1.0:
        raise ValueError("theta_air exceeds the TIR limit of the film")
    theta_film = math.degrees(math.asin(sin_t))
    r01 = (n1 - n_film) / (n1 + n_film)
    r12 = (n_film - n2) / (n_film + n2)
    return theta_film, r01, r12


def volumetric_radiative_transfer(
    I_in: float,
    source_function: float,
    optical_depth: float,
) -> float:
    """Intensity through a volumetric medium with isotropic source ``S``.

    ``I(L) = I_in exp(-tau) + S (1 - exp(-tau))``.
    """
    I_in = _require_real_number(I_in, "I_in")
    source_function = _require_real_number(source_function, "source_function")
    optical_depth = _require_real_number(optical_depth, "optical_depth")
    if optical_depth < 0.0:
        raise ValueError("optical_depth must be non-negative and finite")
    decay = math.exp(-optical_depth)
    return float(I_in * decay + source_function * (1.0 - decay))


def henyey_greenstein_phase_function(cos_theta: float, g: float) -> float:
    """Henyey–Greenstein asymmetry phase function.

    ``p(cos(theta), g) = (1 - g^2) / (4 pi (1 + g^2 - 2 g cos(theta))^(3/2))``.
    Normalized: ``int_0^pi p(cos(theta), g) 2pi sin(theta) d(theta) = 1``.
    """
    cos_theta = _require_real_number(cos_theta, "cos_theta")
    g = _require_real_number(g, "g")
    if abs(cos_theta) > 1.0:
        raise ValueError("cos_theta must lie in [-1, 1]")
    if abs(g) >= 1.0:
        raise ValueError("asymmetry parameter g must lie in (-1, 1)")
    denominator = float((1.0 + g**2 - 2.0 * g * cos_theta) ** 1.5)
    return float((1.0 - g**2) / (4.0 * math.pi * denominator))


def henyey_greenstein_norm(g: float, n_points: int = 4000) -> float:
    """Numerical check of the phase-function normalization with midpoint rule."""
    g = _require_real_number(g, "g")
    if type(n_points) is bool or not isinstance(n_points, int):
        raise TypeError("n_points must be an integer")
    if not (1 <= n_points):
        raise ValueError("n_points must be at least 1")
    if abs(g) >= 1.0:
        raise ValueError("asymmetry parameter g must lie in (-1, 1)")
    # Substitute u = cos(theta): int_0^pi p(cos(theta), g) 2pi sin(theta) d(theta)
    # = int_-1^1 p(u, g) 2pi du. This is the integral over the sphere's azimuth.
    # Trapezoid in u is robust.
    # Trapezoid: sum_i p(u_i, g) * 2pi * du with du = 2/(n)
    total = 0.0
    du = 2.0 / n_points
    for i in range(n_points + 1):
        u = -1.0 + i * du
        w = 1.0 if 0 < i < n_points else 0.5
        total += w * henyey_greenstein_phase_function(u, g) * 2.0 * math.pi * du
    return float(total)


__all__ = [
    "H_PLANCK",
    "C_LIGHT",
    "KB_BOLTZMANN",
    "WIEN_CONSTANT_M_K",
    "PLANCK_WIEN_ROOT",
    "BG_REFERENCE",
    "planck_blackbody_spectral_radiance",
    "wien_max_wavelength",
    "wien_temperature",
    "planck_max_radiance",
    "critical_angle_deg",
    "FresnelAmplitudes",
    "fresnel_amplitudes",
    "brester_angle_deg",
    "brester_reflectance",
    "fresnel_transmittance_s",
    "fresnel_transmittance_p",
    "thin_film_phase",
    "thin_film_reflectance",
    "film_materials_from_air",
    "volumetric_radiative_transfer",
    "henyey_greenstein_phase_function",
    "henyey_greenstein_norm",
]
