"""Exactness regression tests for the canonical Temporal Truth Algebra.

WHY THIS FILE EXISTS
--------------------
``tests/unit/test_temporal_algebra.py`` and ``tests/unit/test_temporal_mutation.py``
were both fully GREEN on PR #142 while that branch rationalised rates with
``Fraction(value).limit_denominator(100000)`` -- the approximate rationalisation
the architecture forbids.  Three source mutations were applied to the shipped
core and the whole temporal suite stayed green:

  M1  ``Fraction(str(value))``  -> ``Fraction(value).limit_denominator(100000)``  (float path)
  M2  ``Fraction(clean)``       -> ``Fraction(flt).limit_denominator(100000)``    (string path)
  M3  ``raise ValueError(...)`` -> ``assert rounding == RoundingPolicy.NEAREST``  (rounding guard)

Every test below exists to KILL one of those mutants.  A test here that can
pass against both the exact and the approximate implementation is not evidence,
so each assertion pins the exact rational, not "close enough".

The mutations themselves are re-applied and required to go red by
``scripts/temporal_mutations.py`` (same harness shape as
``scripts/pack_trust_mutations.py``).
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from nexus_ai_agent.creative.temporal import (
    Duration,
    FrameRateResolver,
    RoundingPolicy,
    Timebase,
)
from nexus_ai_agent.creative.temporal.core import _round_exact_ticks

# ---------------------------------------------------------------------------
# KILLS M1 -- float specifiers must rationalise exactly, never approximately.
#
# 48.0000001 is the smallest realistic probe: its exact decimal rational has a
# denominator of 10_000_000, which is *above* the 100_000 ceiling that
# ``limit_denominator`` used to impose, so the approximate implementation
# silently collapsed it to Timebase(48, 1) -- a 1e-7 rate error with no signal.
# ---------------------------------------------------------------------------


def test_float_rate_rationalises_exactly_not_approximately() -> None:
    tb = FrameRateResolver.resolve(48.0000001)

    assert (tb.numerator, tb.denominator) == (480_000_001, 10_000_000)
    # The mutant returned exactly 48/1; pin the distance so "rounded to 48" fails.
    assert tb.rate == Fraction(480_000_001, 10_000_000)
    assert tb.rate != Fraction(48, 1)


def test_float_rate_denominator_is_the_full_decimal_scale() -> None:
    """A decimal with 7 fractional digits must keep all 7, not be truncated."""
    tb = FrameRateResolver.resolve(0.1234567)

    assert (tb.numerator, tb.denominator) == (1_234_567, 10_000_000)


def test_approximate_rationalisation_of_a_large_denominator_is_rejected() -> None:
    """``limit_denominator`` turns 1e-06 into 0/1; the exact path must not."""
    tb = FrameRateResolver.resolve(1e-06)

    assert (tb.numerator, tb.denominator) == (1, 1_000_000)
    assert tb.numerator > 0, "a zero numerator would be a silent zero rate"


# ---------------------------------------------------------------------------
# KILLS M2 -- the same invariant on the *string* specifier path, which is the
# path untrusted input (LLM output, serialized IR, API payloads) actually takes.
# ---------------------------------------------------------------------------


def test_string_rate_rationalises_exactly_not_approximately() -> None:
    tb = FrameRateResolver.resolve("12.345678")

    assert (tb.numerator, tb.denominator) == (6_172_839, 500_000)
    assert tb.rate != Fraction(1_204_432, 97_559), "that is the limit_denominator answer"


def test_string_and_float_paths_agree_on_the_same_decimal() -> None:
    """One decimal must yield one Timebase regardless of how it was spelled."""
    for spec in ("48.0000001", "48.0000001fps", 48.0000001):
        tb = FrameRateResolver.resolve(spec)
        assert (tb.numerator, tb.denominator) == (480_000_001, 10_000_000), spec


# ---------------------------------------------------------------------------
# The proven NTSC invariants.  These hold through the explicit profile table,
# NOT through float parsing -- 23.976 as a decimal is 2997/125, which is *not*
# 24000/1001.  Pinning them here guards the table itself, which is the only
# thing standing between "23.976" and the NTSC rate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("23.976", (24000, 1001)),
        ("23.976fps", (24000, 1001)),
        (23.976, (24000, 1001)),
        ("24000/1001", (24000, 1001)),
        (Fraction(24000, 1001), (24000, 1001)),
        ("29.97", (30000, 1001)),
        ("29.97fps", (30000, 1001)),
        (29.97, (30000, 1001)),
        ("30000/1001", (30000, 1001)),
        (Fraction(30000, 1001), (30000, 1001)),
        ("59.94", (60000, 1001)),
        ("59.94fps", (60000, 1001)),
        (59.94, (60000, 1001)),
        ("60000/1001", (60000, 1001)),
        (Fraction(60000, 1001), (60000, 1001)),
    ],
)
def test_ntsc_profile_invariants(spec: object, expected: tuple[int, int]) -> None:
    tb = FrameRateResolver.resolve(spec)  # type: ignore[arg-type]

    assert (tb.numerator, tb.denominator) == expected


def test_ntsc_rate_is_not_the_naive_decimal_rational() -> None:
    """The profile table must win over decimal parsing for the NTSC trio."""
    assert Fraction("23.976") == Fraction(2997, 125) != Timebase.fps_23_976().rate
    assert FrameRateResolver.resolve("23.976") == Timebase.fps_23_976()


# ---------------------------------------------------------------------------
# Non-finite and non-positive specifiers must fail closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", [float("nan"), float("inf"), float("-inf"), 0, -24, 0.0, -1.5])
def test_non_finite_and_non_positive_rates_are_rejected(spec: float) -> None:
    with pytest.raises(ValueError):
        FrameRateResolver.resolve(spec)


@pytest.mark.parametrize("spec", ["24/", "24/0", "a/b", "-24/1", "24/1/2", "", "  "])
def test_malformed_string_rates_are_rejected(spec: str) -> None:
    with pytest.raises(ValueError):
        FrameRateResolver.resolve(spec)


def test_bool_is_not_a_rate() -> None:
    with pytest.raises(TypeError):
        FrameRateResolver.resolve(True)


def test_zero_and_negative_timebase_are_rejected() -> None:
    """A zero rate would make every frame count 0 -- it must fail closed."""
    with pytest.raises(ValueError, match="numerator must be positive"):
        Timebase(0, 1)
    with pytest.raises(ValueError, match="numerator must be positive"):
        Timebase(-24, 1)
    with pytest.raises(ValueError, match="denominator must be positive"):
        Timebase(24, 0)
    with pytest.raises(ValueError, match="denominator must be positive"):
        Timebase(24, -1)


# ---------------------------------------------------------------------------
# KILLS M3 -- an unknown rounding policy must raise, not fall through.
#
# ``assert`` is stripped under ``python -O``; a stripped assert turns an
# unsupported policy into a silently-wrong NEAREST conversion.  A ValueError is
# the only fail-closed outcome.
# ---------------------------------------------------------------------------


def test_unsupported_rounding_policy_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unsupported rounding policy"):
        _round_exact_ticks(Fraction(1, 2), "not-a-policy", Timebase.fps_24())  # type: ignore[arg-type]


def test_unsupported_rounding_policy_is_not_an_assertion_error() -> None:
    """Guards the ``-O`` regression: AssertionError would mean the assert mutant."""
    try:
        _round_exact_ticks(Fraction(3, 7), object(), Timebase.fps_30())  # type: ignore[arg-type]
    except ValueError:
        pass
    except AssertionError as exc:  # pragma: no cover - the mutant path
        pytest.fail(f"rounding guard degraded to an assert: {exc}")
    else:  # pragma: no cover - the silently-accepted path
        pytest.fail("an unsupported rounding policy was silently accepted")


# ---------------------------------------------------------------------------
# Zero-duration semantics: 0 seconds is 0 frames at every rate, and the
# conversion reports itself lossless.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rate", ["23.976", "29.97", "59.94", "24", "25", "30", "48000"])
def test_zero_duration_is_zero_frames(rate: str) -> None:
    outcome = Duration.from_seconds(0).to_ticks(rate, rounding=RoundingPolicy.NEAREST)

    assert outcome.value == 0
    assert outcome.lossless is True
    assert outcome.residual_seconds == Fraction(0, 1)


# ---------------------------------------------------------------------------
# Rational precision end-to-end: converting a whole number of frames back and
# forth must be exact, with no float anywhere in the loop.
# ---------------------------------------------------------------------------


def test_frame_round_trip_is_exact_at_ntsc() -> None:
    tb = Timebase.fps_23_976()
    dur = Duration.from_ticks(1001, tb)

    assert dur.seconds == Fraction(1001, 1) / tb.rate
    assert dur.seconds == Fraction(1001 * 1001, 24000)

    back = dur.to_ticks(tb, rounding=RoundingPolicy.EXACT)
    assert back.value == 1001
    assert back.lossless is True


def test_exact_policy_rejects_a_lossy_conversion_instead_of_rounding() -> None:
    # 1/7 s at 24 fps is 24/7 ticks -- genuinely lossy, so EXACT must refuse
    # rather than silently round.  (1/3 s would be exactly 8 ticks and is
    # therefore NOT a valid probe here.)
    dur = Duration.from_seconds(Fraction(1, 7))

    with pytest.raises(ValueError, match="lossy"):
        dur.to_ticks(Timebase.fps_24(), rounding=RoundingPolicy.EXACT)


def test_lossless_conversion_is_reported_as_lossless() -> None:
    """The control for the test above: 1/3 s at 24 fps is exactly 8 ticks."""
    outcome = Duration.from_seconds(Fraction(1, 3)).to_ticks(
        Timebase.fps_24(), rounding=RoundingPolicy.EXACT
    )

    assert outcome.value == 8
    assert outcome.lossless is True


def test_resolver_is_deterministic_across_repeated_calls() -> None:
    specs: list[object] = [
        "23.976",
        "29.97",
        "59.94",
        "48.0000001",
        "24000/1001",
        30,
        Fraction(1, 3),
    ]
    first = [FrameRateResolver.resolve(s) for s in specs]  # type: ignore[arg-type]
    for _ in range(3):
        assert [FrameRateResolver.resolve(s) for s in specs] == first  # type: ignore[arg-type]
