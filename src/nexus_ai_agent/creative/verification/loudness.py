"""BS.1770-4 loudness and its quantisation-uncertainty model, dependency-free.

This is the arithmetic half of the loudness budget: the K-weighting *offset*
curve and the exact second-order quantisation variance of a mean-square
estimate. It imports nothing but :mod:`math` and
:mod:`~nexus_ai_agent.creative.verification.uncertainty_gum`, so a pure lane
(the perception pack) can reach the same number the FFmpeg adapter reports
without dragging the media adapter in.

Model. Samples are quantised with step ``q = 2 / 2**bit_depth`` (full scale
``+-1``); the quantisation error is uniform on ``[-q/2, q/2]`` with variance
``q^2/12`` and mean square ``q^2/12``. For ``N`` independent samples the sample
mean square ``M = (1/N) sum x_k^2`` therefore has

``E[M] = mu + q^2/12``, ``Var(M) = (Var(x^2) + q^2 mu/3 + q^4/180) / N``

and with ``Var(x^2) ~= 0`` around a stationary programme,

``u^2(M) = q^2 M / (3N) + q^4 / (180 N)``.

The first term dominates for any real programme. Using the *sample* deviation
``sqrt(q^2/(12N))`` instead (as an earlier revision did) is wrong in both
directions: it is independent of the level, so it overstates the budget on
quiet material and understates it on loud material.

``L = -0.691 + 10 log10(M)`` in LUFS, so ``u(L) = (10/ln 10) * u(M)/M`` per
neper of mean-square deviation.
"""

from __future__ import annotations

import math

from .uncertainty_gum import (
    CombinedUncertainty,
    GUMInputError,
    UncertaintyInput,
    combine,
)

__all__ = [
    "DECIBEL_PER_NEPER",
    "LUFS_OFFSET",
    "LoudnessBudget",
    "loudness_budget",
    "mean_square_deviation",
    "mean_square_from_lkfs",
    "quantisation_step",
]

#: BS.1770-4: ``L_K = -0.691 + 10 log10(mean square)``.
LUFS_OFFSET = -0.691

#: ``10 / ln 10``: decibels per natural-log unit (neper).
DECIBEL_PER_NEPER = 10.0 / math.log(10.0)

#: Resolved budget components, in the order the adapter reports them.
LoudnessBudget = tuple[float, float, float, CombinedUncertainty]


def quantisation_step(bit_depth: int) -> float:
    """``q = 2 / 2**bit_depth`` for full scale ``+-1``."""
    if isinstance(bit_depth, bool) or not isinstance(bit_depth, int):
        raise GUMInputError(f"bit_depth must be an int, got {type(bit_depth).__name__}")
    if bit_depth <= 0:
        raise GUMInputError(f"bit_depth must be positive, got {bit_depth!r}")
    return 2.0 / float(2**bit_depth)


def mean_square_from_lkfs(lkfs: float) -> float:
    """Invert the BS.1770 curve: ``M = 10 ** ((L + 0.691)/10)``."""
    if isinstance(lkfs, bool) or not isinstance(lkfs, (int, float)):
        raise GUMInputError(f"integrated_lkfs must be a real number, got {type(lkfs).__name__}")
    level = float(lkfs)
    if math.isnan(level) or level == math.inf:
        raise GUMInputError(f"integrated_lkfs must be finite, got {level!r}")
    mean_square = float(10.0 ** ((level - LUFS_OFFSET) / 10.0))
    if not math.isfinite(mean_square) or mean_square <= 0.0:
        raise GUMInputError(f"integrated_lkfs {level!r} does not map to a positive mean square")
    return mean_square


def mean_square_deviation(*, step: float, mean_square: float, samples: int) -> float:
    """``u(M) = sqrt(q^2 M / (3 N) + q^4 / (180 N))``, in ``[amplitude^2]``."""
    if isinstance(step, bool) or not isinstance(step, (int, float)) or not math.isfinite(step):
        raise GUMInputError(f"step must be a finite real number, got {step!r}")
    quantisation_step_value = float(step)
    if quantisation_step_value <= 0.0:
        raise GUMInputError(f"step must be positive, got {quantisation_step_value!r}")
    if (
        isinstance(mean_square, bool)
        or not isinstance(mean_square, (int, float))
        or not math.isfinite(mean_square)
    ):
        raise GUMInputError(f"mean_square must be a finite real number, got {mean_square!r}")
    power = float(mean_square)
    if power <= 0.0:
        raise GUMInputError(f"mean_square must be positive, got {power!r}")
    if isinstance(samples, bool) or not isinstance(samples, int):
        raise GUMInputError(f"samples must be an int, got {type(samples).__name__}")
    if samples <= 0:
        raise GUMInputError(f"samples must be positive, got {samples!r}")
    head = quantisation_step_value**2 * power / (3.0 * samples)
    tail = quantisation_step_value**4 / (180.0 * samples)
    return math.sqrt(math.fsum((head, tail)))


def loudness_budget(
    integrated_lkfs: float, *, bit_depth: int = 16, sample_count: int = 48_000
) -> LoudnessBudget:
    """Resolve ``(lkfs, mean_square, step, combined)`` for one measurement.

    ``combined`` is the first-order budget of the reported loudness, in LUFS.
    """
    step = quantisation_step(bit_depth)
    mean_square = mean_square_from_lkfs(integrated_lkfs)
    deviation = mean_square_deviation(step=step, mean_square=mean_square, samples=sample_count)
    sensitivity = DECIBEL_PER_NEPER / mean_square
    combined = combine((sensitivity,), UncertaintyInput.from_standard((mean_square,), (deviation,)))
    return float(integrated_lkfs), mean_square, step, combined
