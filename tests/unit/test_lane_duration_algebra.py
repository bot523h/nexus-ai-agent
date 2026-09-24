"""Wave 8 property guard: the lane's duration algebra, checked two ways.

Golden pins prove one lane at a time.  This module proves an *invariant* over
seeded random lanes, because duration arithmetic is where a lane silently lies:
the ``-t`` handed to FFmpeg comes from the same integer the journal records, so
if the two ever disagree the master is truncated or padded and nothing errors.

Three independent checks per lane:

1. ``compiled.duration_us`` equals a from-scratch restatement of the
   microsecond algebra that never calls into the compiler;
2. the ``-t`` in the real argv equals that same integer rendered as seconds —
   two-sided accounting between the IR field and what the encoder is handed;
3. recompiling the identical IR is byte-identical (argv, filtergraph, digest).

The restatement refuses any op kind it does not know, and
``test_restatement_covers_every_declared_lane_op`` pins its vocabulary to the
``LaneOp`` union — so adding an op that moves the clock cannot slip past this
file by falling through a chain of ``isinstance`` checks.
"""

from __future__ import annotations

import random
import typing
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.creative.rendering import (
    DuckOp,
    ExposureOp,
    FreezeOp,
    LaneIR,
    LaneOp,
    LaneSource,
    LoudnormOp,
    LutOp,
    MeasuredLoudness,
    ReverseOp,
    SpeedOp,
    SubtitleOp,
    TitleOp,
    TrimOp,
    XfadeOp,
    compile_lane,
)

MICROSECONDS_PER_SECOND = 1_000_000
FONT = "/fonts/Vazirmatn.ttf"

MAIN = LaneSource("main", "/media/main.mp4", "video", 10_000_000)
OTHER = LaneSource("other", "/media/other.mp4", "video", 6_000_000)
VOICE = LaneSource("voice", "/media/voice.m4a", "audio", 8_000_000)

MEASURED = MeasuredLoudness.model_validate(
    {
        "input_i": -20.5,
        "input_tp": -2.1,
        "input_lra": 7.3,
        "input_thresh": -31.2,
        "target_offset": 6.5,
    }
)

# Every op kind the restatement below knows how to account for.
DURATION_NEUTRAL_KINDS = frozenset(
    {"reverse", "title", "loudnorm", "duck", "exposure", "lut", "subtitle"}
)
DURATION_AFFECTING_KINDS = frozenset({"trim", "speed", "freeze", "xfade"})
HANDLED_KINDS = DURATION_NEUTRAL_KINDS | DURATION_AFFECTING_KINDS


def _declared_lane_op_kinds() -> frozenset[str]:
    """The ``op`` discriminator of every member of the ``LaneOp`` union."""
    union = typing.get_args(LaneOp)[0]
    members = typing.get_args(union)
    assert members, "expected the LaneOp union to have members"
    return frozenset(member.model_fields["op"].default for member in members)


def _restated_duration_us(main_us: int, ops: Sequence[Any], other_us: int) -> int:
    """A from-scratch restatement of the lane's microsecond algebra.

    Deliberately shares no code with ``compiler._track_durations``: the whole
    value of the check is that two independent implementations of the same
    arithmetic must agree on every seeded lane.
    """
    remaining = main_us
    for op in ops:
        kind = op.op
        if kind == "trim":
            remaining = op.out_us - op.in_us
        elif kind == "speed":
            remaining = max(1, int(remaining / op.factor))
        elif kind == "freeze":
            remaining = op.hold_us
        elif kind == "xfade":
            remaining = remaining + other_us - op.duration_us
        elif kind in DURATION_NEUTRAL_KINDS:
            pass
        else:
            # Not a skip: an op the restatement has never heard of must be
            # classified here on purpose, or the guard is quietly incomplete.
            raise AssertionError(f"restatement does not account for op kind {kind!r}")
    return remaining


def _random_op(rng: random.Random, *, loudnorm_used: bool, running_us: int) -> tuple[object, bool]:
    """One valid random op.  Returns the op and whether loudnorm is now used.

    ``running_us`` is the lane duration at this point, so the generated xfade is
    valid by construction — the compiler rejects an overlap past either clip and
    the restatement is only meaningful on lanes the compiler accepts.
    """
    choices = [
        "trim",
        "speed",
        "reverse",
        "freeze",
        "xfade",
        "title",
        "duck",
        "exposure",
        "lut",
        "subtitle",
    ]
    if not loudnorm_used:
        choices.append("loudnorm")
    kind = rng.choice(choices)
    if kind == "trim":
        in_us = rng.randrange(0, 9_000_000)
        out_us = rng.randrange(in_us + 1, 10_000_001)
        return TrimOp(in_us=in_us, out_us=out_us), loudnorm_used
    if kind == "speed":
        return SpeedOp(factor=rng.choice([0.25, 0.5, 1.0, 1.5, 2.0, 4.0, 8.0])), loudnorm_used
    if kind == "reverse":
        return ReverseOp(), loudnorm_used
    if kind == "freeze":
        return (
            FreezeOp(
                at_us=rng.randrange(0, 5_000_000),
                hold_us=rng.randrange(1, 4_000_000),
            ),
            loudnorm_used,
        )
    if kind == "xfade":
        duration_us = rng.randrange(1, min(OTHER.duration_us, running_us) + 1)
        offset_us = rng.randrange(0, max(0, running_us - duration_us) + 1)
        return (
            XfadeOp(
                other_asset_id="other",
                offset_us=offset_us,
                duration_us=duration_us,
                kind=rng.choice(["fade", "wipeleft", "dissolve"]),
            ),
            loudnorm_used,
        )
    if kind == "title":
        return TitleOp(text=f"card {rng.randrange(1000)}", start_us=0), loudnorm_used
    if kind == "duck":
        return DuckOp(voice_asset_id="voice"), loudnorm_used
    if kind == "lut":
        return (
            LutOp(
                lut_name="warm",
                lut_path="/stage/warm.cube",
                intensity=rng.choice([0.0, 0.5, 1.0]),
            ),
            loudnorm_used,
        )
    if kind == "subtitle":
        return SubtitleOp(subtitle_path="/stage/cap.srt"), loudnorm_used
    if kind == "exposure":
        return (
            ExposureOp(
                exposure_ev=rng.choice([-4.0, -1.0, 0.0, 0.5, 1.0, 3.0, 4.0]),
                contrast=rng.choice([0.2, 1.0, 1.25, 3.0]),
                temperature_k=rng.choice([1000, 3200, 6500, 9000, 40000]),
                tint=rng.choice([-50.0, -12.5, 0.0, 25.0, 50.0]),
            ),
            loudnorm_used,
        )
    return LoudnormOp(), True


def _random_lane(seed: int) -> LaneIR:
    """A seeded lane of 24–32 ops whose xfade offsets are always satisfiable."""
    rng = random.Random(seed)
    loudnorm_used = False
    ops: list[object] = []
    running = MAIN.duration_us
    for _ in range(rng.randrange(24, 33)):
        op, loudnorm_used = _random_op(rng, loudnorm_used=loudnorm_used, running_us=running)
        # Track the running duration exactly as the algebra does, so the next
        # xfade is generated against the real clock rather than a guess.
        if isinstance(op, TrimOp):
            running = op.out_us - op.in_us
        elif isinstance(op, SpeedOp):
            running = max(1, int(running / op.factor))
        elif isinstance(op, FreezeOp):
            running = op.hold_us
        elif isinstance(op, XfadeOp):
            running = running + OTHER.duration_us - op.duration_us
        ops.append(op)
    if not any(isinstance(op, ExposureOp) for op in ops):
        # Guarantee the exposure op is exercised on every seed, so the
        # duration-neutrality test below never has to skip.  Appended — never
        # spliced over a clock-moving op: replacing a trim/speed/freeze/xfade
        # after the fact would invalidate later xfade offsets (session 3 fix;
        # seed 7 caught it when the choice list grew to ten kinds).
        ops.append(ExposureOp(exposure_ev=1.0, temperature_k=3200))
    return LaneIR(
        main=MAIN,
        ops=tuple(ops),
        extra_sources=(OTHER, VOICE),
    )


SEEDS = tuple(range(40))


@pytest.mark.parametrize("seed", SEEDS)
def test_compiled_duration_matches_an_independent_restatement(seed: int) -> None:
    ir = _random_lane(seed)
    compiled = compile_lane(ir, measured=MEASURED, fontfile=FONT)
    assert compiled.duration_us == _restated_duration_us(
        MAIN.duration_us, ir.ops, OTHER.duration_us
    )
    assert compiled.duration_us >= 1


@pytest.mark.parametrize("seed", SEEDS)
def test_the_argv_dash_t_agrees_with_the_ir_duration(seed: int) -> None:
    """Two-sided accounting: the IR integer and the encoder's ``-t`` are one value."""
    ir = _random_lane(seed)
    compiled = compile_lane(ir, measured=MEASURED, fontfile=FONT)
    argv = compiled.argv(Path("/stage/out.mp4"))
    assert argv[argv.index("-t") + 1] == f"{compiled.duration_us / MICROSECONDS_PER_SECOND:.6f}"


@pytest.mark.parametrize("seed", SEEDS)
def test_recompiling_the_same_ir_is_byte_identical(seed: int) -> None:
    ir = _random_lane(seed)
    first = compile_lane(ir, measured=MEASURED, fontfile=FONT)
    second = compile_lane(ir, measured=MEASURED, fontfile=FONT)
    out = Path("/stage/out.mp4")
    assert first.argv(out) == second.argv(out)
    assert first.filtergraph == second.filtergraph
    assert first.ir_hash == second.ir_hash
    assert first.duration_us == second.duration_us


@pytest.mark.parametrize("seed", SEEDS)
def test_exposure_never_moves_the_clock(seed: int) -> None:
    """Grading is a pixel transform: stripping every exposure op changes nothing."""
    ir = _random_lane(seed)
    ungraded = tuple(op for op in ir.ops if not isinstance(op, ExposureOp))
    assert len(ungraded) < len(ir.ops), "generator must place an exposure op on every seed"
    graded = compile_lane(ir, measured=MEASURED, fontfile=FONT)
    plain = compile_lane(
        LaneIR(main=ir.main, ops=ungraded, extra_sources=ir.extra_sources),
        measured=MEASURED,
        fontfile=FONT,
    )
    assert graded.duration_us == plain.duration_us
    assert graded.duration_us == _restated_duration_us(MAIN.duration_us, ir.ops, OTHER.duration_us)


def test_restatement_covers_every_declared_lane_op() -> None:
    """A new ``LaneOp`` must be classified here on purpose, not fall through."""
    assert _declared_lane_op_kinds() == HANDLED_KINDS


def test_generated_lanes_actually_exercise_every_op_kind() -> None:
    """Guard against a generator that quietly stops producing whole op kinds."""
    seen: set[str] = set()
    for seed in SEEDS:
        seen.update(op.op for op in _random_lane(seed).ops)
    assert seen == HANDLED_KINDS, f"generator never produced: {sorted(HANDLED_KINDS - seen)}"
