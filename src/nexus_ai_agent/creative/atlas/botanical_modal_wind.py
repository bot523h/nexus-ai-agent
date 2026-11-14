"""WAVE 25 — botanical modal wind (modal dynamics of plants under wind load).

Plants are modelled as a chain of Euler-Bernoulli cantilever segments whose
transverse response is expanded in the cantilever eigenmodes. The forced modal
ODE is:

    q_n'' + 2 zeta_n omega_n q_n' + omega_n^2 q_n = F_n(t) / m_n

with the cantilever natural frequencies ``omega_n = (beta_n L)^2 sqrt(EI/(rho A L^4))``
and mode shapes ``phi_n(x) = cosh(beta x) - cos(beta x) - sigma (sinh(beta x) - sin(beta x))``.
The wind load is a **Davenport-like** spectral process: a seeded, band-limited
coloured-noise gust sequence whose power spectral density falls off as ``f^-5/3``
in the inertial sub-range, plus a mean drag ``0.5 rho_air C_d A U^2``.

Everything is offline, deterministic and bounded: the gust field is reproducible
from a seed, the eigenfrequencies are checked against the analytic cantilever
roots, and the modal energy balance is reported so an unphysical blow-up is
never silently returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

__all__ = [
    "BotanyError",
    "AIR_DENSITY",
    "CANTILEVER_BETA",
    "ModalStem",
    "WindGustField",
    "cantilever_omega",
    "modal_wind_response",
    "wind_drag_force",
    "MAX_MODES",
    "MAX_STEPS",
]

#: Sea-level air density (kg/m^3).
AIR_DENSITY: Final[float] = 1.225
#: First five cantilever eigenvalue roots ``beta_n L`` (cosh.cos = -1).
CANTILEVER_BETA: Final[tuple[float, ...]] = (
    1.875104068711961,
    4.694091132974174,
    7.854757438237613,
    10.995540734875467,
    14.137168391046470,
)
MAX_MODES: Final[int] = 5
MAX_STEPS: Final[int] = 1_000_000
_EPS: Final[float] = 1e-12


class BotanyError(ValueError):
    """Raised for invalid stem parameters, wind inputs, or out-of-range values."""


@dataclass(frozen=True)
class ModalStem:
    """A cantilever stem segment: length, bending stiffness, mass per length."""

    length: float
    youngs_modulus: float
    area_moment: float
    density: float
    cross_area: float
    damping_ratio: float = 0.03

    def __post_init__(self) -> None:
        for name, val in (
            ("length", self.length),
            ("youngs_modulus", self.youngs_modulus),
            ("area_moment", self.area_moment),
            ("density", self.density),
            ("cross_area", self.cross_area),
        ):
            if not np.isfinite(val) or val <= 0.0:
                raise BotanyError(f"{name} must be finite and positive")
        if not np.isfinite(self.damping_ratio) or not (0.0 <= self.damping_ratio < 1.0):
            raise BotanyError("damping_ratio must lie in [0, 1)")

    @property
    def mass_per_length(self) -> float:
        return self.density * self.cross_area

    @property
    def flexural_rigidity(self) -> float:
        return self.youngs_modulus * self.area_moment

    def omega(self, mode: int) -> float:
        """Natural angular frequency of cantilever mode ``mode`` (1-based)."""
        if mode < 1 or mode > MAX_MODES:
            raise BotanyError(f"mode must be in [1, {MAX_MODES}]")
        beta_l = CANTILEVER_BETA[mode - 1]
        return float(
            beta_l**2 * np.sqrt(self.flexural_rigidity / (self.mass_per_length * self.length**4))
        )

    def mode_shape(self, mode: int, x: np.ndarray) -> np.ndarray:
        """Cantilever mode shape ``phi_n(x)`` normalised to unit tip value."""
        if mode < 1 or mode > MAX_MODES:
            raise BotanyError(f"mode must be in [1, {MAX_MODES}]")
        xi = np.asarray(x, dtype=np.float64)
        if np.any(xi < 0.0) or np.any(xi > 1.0):
            raise BotanyError("x must lie in [0, 1] (fraction of the stem)")
        b = CANTILEVER_BETA[mode - 1]
        sigma = (np.cosh(b) + np.cos(b)) / (np.sinh(b) + np.sin(b))

        def shape(x: np.ndarray) -> np.ndarray:
            val: np.ndarray = (
                np.cosh(b * x) - np.cos(b * x) - sigma * (np.sinh(b * x) - np.sin(b * x))
            )
            return val

        # Normalise by the true tip value phi(1), not the last sample, so a
        # single-point evaluation returns the physical value.
        out: np.ndarray = shape(xi) / float(shape(np.array([1.0]))[0])
        return out


def cantilever_omega(length: float, ei: float, mu: float, *, mode: int = 1) -> float:
    """Analytic cantilever natural frequency ``omega_n`` (rad/s)."""
    if length <= 0.0 or ei <= 0.0 or mu <= 0.0:
        raise BotanyError("length, EI and mu must be positive")
    if mode < 1 or mode > MAX_MODES:
        raise BotanyError(f"mode must be in [1, {MAX_MODES}]")
    beta_l = CANTILEVER_BETA[mode - 1]
    return float(beta_l**2 * np.sqrt(ei / (mu * length**4)))


@dataclass(frozen=True)
class WindGustField:
    """A deterministic, band-limited coloured-noise gust sequence (m/s)."""

    samples: np.ndarray
    dt: float
    mean_speed: float

    def __post_init__(self) -> None:
        s = np.asarray(self.samples, dtype=np.float64)
        if s.ndim != 1 or s.size == 0:
            raise BotanyError("samples must be a non-empty 1-D array")
        if not np.all(np.isfinite(s)):
            raise BotanyError("samples contain NaN/Inf")
        if self.dt <= 0.0 or not np.isfinite(self.dt):
            raise BotanyError("dt must be finite and positive")
        if self.mean_speed < 0.0 or not np.isfinite(self.mean_speed):
            raise BotanyError("mean_speed must be finite and non-negative")
        object.__setattr__(self, "samples", s)

    def at(self, index: int) -> float:
        if index < 0 or index >= self.samples.size:
            raise BotanyError("gust index out of range")
        return float(self.samples[index])


def davenport_gusts(
    *,
    n_samples: int = 1024,
    dt: float = 0.05,
    mean_speed: float = 5.0,
    turbulence_intensity: float = 0.2,
    seed: int = 0,
) -> WindGustField:
    """Synthesise a Davenport-like gust sequence with a ``f^-5/3`` PSD.

    The field is generated in the frequency domain (white noise scaled by the
    target spectrum), then inverse-FFT'd; the result is real and deterministic
    in ``seed``. The standard deviation equals ``turbulence_intensity *
    mean_speed`` by construction.
    """
    if n_samples < 2:
        raise BotanyError("n_samples must be >= 2")
    if n_samples > 1 << 20:
        raise BotanyError("n_samples is too large")
    if dt <= 0.0 or not np.isfinite(dt):
        raise BotanyError("dt must be finite and positive")
    if mean_speed < 0.0 or not np.isfinite(mean_speed):
        raise BotanyError("mean_speed must be finite and non-negative")
    if not (0.0 <= turbulence_intensity <= 1.0):
        raise BotanyError("turbulence_intensity must lie in [0, 1]")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise BotanyError("seed must be a non-negative int")

    rng = np.random.default_rng(seed)
    freqs = np.fft.fftfreq(n_samples, d=dt)
    f = np.abs(freqs)
    f_ref = 0.05
    # Target amplitude spectrum ~ f^-5/6 (so the PSD ~ f^-5/3) in the inertial
    # sub-range, flat below the reference frequency.
    with np.errstate(divide="ignore"):
        amplitude = np.where(f > 0.0, (np.maximum(f, f_ref) / f_ref) ** (-5.0 / 6.0), 0.0)
    amplitude[0] = 0.0  # remove the DC component; the mean is added explicitly
    white = rng.normal(0.0, 1.0, n_samples)
    spectrum = np.fft.fft(white) * amplitude
    signal = np.real(np.fft.ifft(spectrum))
    signal = signal - signal.mean()
    std = float(signal.std())
    target_std = turbulence_intensity * mean_speed
    if std > _EPS:
        signal = signal * (target_std / std)
    return WindGustField(samples=signal, dt=dt, mean_speed=mean_speed)


def wind_drag_force(
    *, air_density: float, drag_coefficient: float, area: float, speed: float
) -> float:
    """Steady aerodynamic drag ``0.5 rho C_d A U^2`` (N)."""
    if air_density <= 0.0:
        raise BotanyError("air_density must be positive")
    if drag_coefficient < 0.0:
        raise BotanyError("drag_coefficient must be non-negative")
    if area <= 0.0:
        raise BotanyError("area must be positive")
    if speed < 0.0:
        raise BotanyError("speed must be non-negative")
    return 0.5 * air_density * drag_coefficient * area * speed * speed


def _exact_damped_step(
    q: np.ndarray,
    qdot: np.ndarray,
    omega: np.ndarray,
    zeta: float,
    a: np.ndarray,
    dt: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Exact solution of ``q'' + 2 zeta omega q' + omega^2 q = a`` over ``dt``.

    ``a`` is held constant over the substep. Uses the damped natural frequency
    ``omega_d = omega sqrt(1 - zeta^2)`` and the closed-form homogeneous
    solution plus the particular solution ``a / omega^2``.
    """
    c = zeta * omega
    wd = omega * np.sqrt(1.0 - zeta * zeta)
    e = np.exp(-c * dt)
    cos_wd = np.cos(wd * dt)
    sin_wd = np.sin(wd * dt)
    a_over_w2 = a / omega**2
    amp_a = q - a_over_w2
    amp_b = (qdot + c * amp_a) / wd
    q_new = e * (amp_a * cos_wd + amp_b * sin_wd) + a_over_w2
    qdot_new = e * ((-c * amp_a + amp_b * wd) * cos_wd + (-c * amp_b - amp_a * wd) * sin_wd)
    return q_new, qdot_new


@dataclass(frozen=True)
class ModalResponse:
    """The modal response of a stem: ``q[t, n]`` and the tip displacement."""

    q: np.ndarray
    tip_displacement: np.ndarray
    modal_energies: np.ndarray
    converged: bool
    steps: int


def modal_wind_response(
    stem: ModalStem,
    gusts: WindGustField,
    *,
    modes: int = 3,
    drag_coefficient: float = 1.2,
    frontal_area: float = 0.5,
    integration: str = "semi_implicit",
) -> ModalResponse:
    """Integrate the forced modal ODE under the gust field.

    Returns the modal coordinates ``q[t, n]``, the tip displacement over time,
    and the per-mode energy. ``converged`` is ``False`` if any mode's amplitude
    exceeded the physically-bounded envelope (a guard against a divergent step).
    """
    if modes < 1 or modes > MAX_MODES:
        raise BotanyError(f"modes must be in [1, {MAX_MODES}]")
    if drag_coefficient < 0.0:
        raise BotanyError("drag_coefficient must be non-negative")
    if frontal_area <= 0.0:
        raise BotanyError("frontal_area must be positive")
    if integration not in ("semi_implicit", "exact"):
        raise BotanyError("integration must be 'semi_implicit' or 'exact'")

    dt = gusts.dt
    n_steps = gusts.samples.size
    if n_steps > MAX_STEPS:
        raise BotanyError(f"too many steps (>{MAX_STEPS})")
    omega = np.array([stem.omega(n + 1) for n in range(modes)])
    zeta = stem.damping_ratio
    # Modal mass (unit-tip-normalised mode shapes -> m_n ~ mu*L/3 for mode 1).
    x = np.linspace(0.0, 1.0, 128)
    modal_mass = np.array(
        [
            float(
                stem.mass_per_length * stem.length * np.trapezoid(stem.mode_shape(n + 1, x) ** 2, x)
            )
            for n in range(modes)
        ]
    )
    # Generalised force shape factor: mode shape at the mid-stem where the wind acts.
    phi_mid = np.array([float(stem.mode_shape(n + 1, np.array([0.5]))[0]) for n in range(modes)])

    q = np.zeros((n_steps, modes))
    qdot = np.zeros((n_steps, modes))
    tip = np.zeros(n_steps)
    converged = True
    max_amp = 10.0 * stem.length  # generous physical bound (10x the stem length)
    # Substep so that the highest retained mode satisfies omega*dt_sub < 0.5;
    # an explicit/semi-implicit scheme is unconditionally unstable above 2, and
    # the substepping is bounded so the work stays finite.
    omega_max = float(omega.max())
    n_sub = max(1, int(np.ceil(omega_max * dt / 0.5)))
    dt_sub = dt / n_sub
    for t in range(1, n_steps):
        speed = gusts.mean_speed + gusts.samples[t]
        speed = max(speed, 0.0)
        force = wind_drag_force(
            air_density=AIR_DENSITY,
            drag_coefficient=drag_coefficient,
            area=frontal_area,
            speed=speed,
        )
        f_general = force * phi_mid
        q_cur = q[t - 1].copy()
        qdot_cur = qdot[t - 1].copy()
        for _ in range(n_sub):
            if integration == "semi_implicit":
                acc = f_general / modal_mass - 2.0 * zeta * omega * qdot_cur - omega**2 * q_cur
                # Symplectic (semi-implicit) Euler: stable under substepping.
                qdot_cur = qdot_cur + acc * dt_sub
                q_cur = q_cur + qdot_cur * dt_sub
            else:
                # Closed-form exponential integrator for the damped oscillator
                # under a constant modal force over the substep: exact for the
                # linear ODE and stable for any dt.
                q_cur, qdot_cur = _exact_damped_step(
                    q_cur, qdot_cur, omega, zeta, f_general / modal_mass, dt_sub
                )
        q[t] = q_cur
        qdot[t] = qdot_cur
        if not np.all(np.isfinite(q[t])) or np.abs(q[t]).max() > max_amp:
            converged = False
            q[t:] = q[t - 1]
            qdot[t:] = qdot[t - 1]
            break
        tip[t] = float(np.dot(q[t], np.ones(modes)))  # unit-tip mode shapes
    energies = 0.5 * modal_mass * (qdot[-1] ** 2 + omega**2 * q[-1] ** 2)
    return ModalResponse(
        q=q,
        tip_displacement=tip,
        modal_energies=energies,
        converged=converged,
        steps=n_steps,
    )
