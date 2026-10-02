#!/usr/bin/env python3
"""Adversarial mutation harness for the canonical Temporal Truth Algebra.

Evidence rule: a temporal invariant that has never been attacked is not evidence.
This script rewrites the shipped temporal core (and the one production consumer
of it, ``delivery.export_otio``) with a specific weakening, re-runs the temporal
suite, and requires the suite to turn RED.  Every source file is restored
afterwards -- including on failure -- and the final run must be GREEN again.

It exists because all three of the first mutations below SURVIVED the temporal
suite as shipped on PR #142: the branch rationalised rates with
``Fraction(value).limit_denominator(100000)`` and 29 tests stayed green.  The
killing assertions now live in ``tests/unit/test_temporal_exactness.py``.

Usage::

    python scripts/temporal_mutations.py           # run every mutation
    python scripts/temporal_mutations.py --list

Exit code 0 means "N/N mutants killed"; anything else means an invariant is fake.
"""

# ruff: noqa: E501 - the MUTATIONS table holds verbatim source snippets; they must
# match the shipped files byte-for-byte, so they cannot be re-wrapped.
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPORAL = ROOT / "src" / "nexus_ai_agent" / "creative" / "temporal"
DELIVERY = ROOT / "src" / "nexus_ai_agent" / "creative" / "packs" / "delivery"
TESTS = (
    "tests/unit/test_temporal_exactness.py",
    "tests/unit/test_temporal_algebra.py",
    "tests/unit/test_delivery_pack.py",
)


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    why: str


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "float_rate_rationalised_approximately",
        TEMPORAL / "core.py",
        "            frac = Fraction(str(value))",
        "            frac = Fraction(value).limit_denominator(100000)",
        "a decimal rate must rationalise exactly; limit_denominator silently "
        "collapses 48.0000001 to 48/1 (this mutant survived PR #142's suite)",
    ),
    Mutation(
        "string_rate_rationalised_approximately",
        TEMPORAL / "core.py",
        "                    frac = Fraction(clean)",
        "                    frac = Fraction(flt).limit_denominator(100000)",
        "the string path is the one untrusted input takes (LLM output, serialized "
        "IR, API payloads); it must be exact too",
    ),
    Mutation(
        "rounding_guard_degraded_to_assert",
        TEMPORAL / "core.py",
        '    else:\n        raise ValueError(f"Unsupported rounding policy: {rounding}")',
        "    else:\n        assert rounding == RoundingPolicy.NEAREST",
        "assert is stripped under python -O, turning an unsupported policy into a "
        "silently-wrong NEAREST conversion",
    ),
    Mutation(
        "ntsc_profile_table_bypassed",
        TEMPORAL / "core.py",
        '        23.976: Timebase.fps_23_976(),\n        "24000/1001": Timebase.fps_23_976(),',
        '        "24000/1001": Timebase.fps_23_976(),',
        "23.976 as a decimal is 2997/125, NOT 24000/1001; only the explicit "
        "profile table yields the NTSC rate",
    ),
    Mutation(
        "zero_rate_silently_accepted",
        TEMPORAL / "core.py",
        "            or self.numerator <= 0\n        ):",
        "            or self.numerator < 0\n        ):",
        "a zero numerator makes every frame count 0 -- it must fail closed",
    ),
    Mutation(
        "zero_denominator_accepted",
        TEMPORAL / "core.py",
        "            or self.denominator <= 0\n        ):",
        "            or self.denominator < 0\n        ):",
        "a zero denominator is a division by zero waiting to happen in rate math",
    ),
    Mutation(
        "lossy_conversion_silently_rounded_under_exact",
        TEMPORAL / "core.py",
        '    if rounding == RoundingPolicy.EXACT:\n        raise ValueError(\n            f"Conversion to timebase {timebase.numerator}/{timebase.denominator} "\n            f"is lossy (exact ticks: {exact_ticks}), but EXPLICIT policy was EXACT."\n        )',
        "    if rounding == RoundingPolicy.EXACT:\n        pass",
        "EXACT must refuse a lossy conversion rather than quietly round it",
    ),
    Mutation(
        "zero_duration_emitted_as_one_frame",
        DELIVERY / "operations.py",
        "        emitted_frames = max(1, raw_conv_frames) if has_missing_dur else raw_conv_frames",
        "        emitted_frames = max(1, raw_conv_frames)",
        "0 duration must be 0 frames; forcing >=1 is the pre-fix truncation bug",
    ),
    Mutation(
        "otio_export_reverts_to_float_truncation",
        DELIVERY / "operations.py",
        "        raw_conv_frames = dur.to_ticks(target_tb, rounding=RoundingPolicy.NEAREST).value",
        "        raw_conv_frames = int(float(dur.seconds) * float(target_tb.rate))",
        "float truncation loses sub-frame precision at NTSC rates and reports the "
        "conversion as lossless when it is not",
    ),
)


def run_tests() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", *TESTS],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}: {mutation.why}")
        return 0

    print("baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — refusing to mutate a suite that is already failing")
        return 2
    print("GREEN")

    killed = 0
    survivors: list[str] = []
    for mutation in MUTATIONS:
        original = mutation.path.read_text(encoding="utf-8")
        if mutation.old not in original:
            print(f"{mutation.name}: MUTATION DOES NOT APPLY (source drifted)")
            survivors.append(mutation.name)
            continue
        mutation.path.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
        try:
            green = run_tests()
        finally:
            mutation.path.write_text(original, encoding="utf-8")
        if green:
            print(f"{mutation.name}: SURVIVED — {mutation.why}")
            survivors.append(mutation.name)
        else:
            killed += 1
            print(f"{mutation.name}: killed")

    print("restored baseline ... ", end="", flush=True)
    if not run_tests():
        print("RED — restore failed")
        return 3
    print("GREEN")
    print(f"{killed}/{len(MUTATIONS)} killed")
    if survivors:
        print("survivors: " + ", ".join(survivors))
    return 0 if killed == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
