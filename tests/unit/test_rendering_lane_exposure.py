"""Wave 8 color/exposure lane: golden pins for the ``exposure`` op.

Pure tests — the compiler never runs a process, so every assertion here is a
byte-exact statement about the argv a real encode would receive.

The ranges asserted below are **contracts against FFmpeg itself**, read off the
filter sources (D-0007): ``vf_eq.c`` clips gamma to [0.1, 10.0] and contrast to
[-1000.0, 1000.0] via ``av_clipf``; ``vf_colortemperature.c`` declares
``temperature`` as ``{.dbl=6500}, 1000, 40000``; ``vf_colorbalance.c`` declares
``gm`` as ``{.dbl=0}, -1, 1``.  If FFmpeg ever narrows or widens those bounds
these tests go red on purpose — a silent range change must not slip into a
master file unreviewed.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_ai_agent.creative.rendering import (
    ExposureOp,
    LaneIR,
    LaneSource,
    compile_lane,
    ev_to_gamma,
    tint_to_gm,
)
from nexus_ai_agent.creative.rendering.compiler import _exposure_stage
from nexus_ai_agent.creative.rendering.ir import (
    COLOR_TEMPERATURE_MAX_K,
    COLOR_TEMPERATURE_MIN_K,
    COLOR_TEMPERATURE_NEUTRAL_K,
    COLORBALANCE_LIMIT,
    EQ_CONTRAST_MAX,
    EQ_CONTRAST_MIN,
    EQ_GAMMA_MAX,
    EQ_GAMMA_MIN,
    TINT_FULL_SCALE,
    LaneError,
)

MAIN = LaneSource("main", "/media/main.mp4", "video", 10_000_000)
AUDIO_MAIN = LaneSource("main", "/media/main.m4a", "audio", 10_000_000)


def _compiled(*ops: object, main: LaneSource = MAIN) -> str:
    """Compile a single-source lane and return the filtergraph."""
    compiled = compile_lane(LaneIR(main=main, ops=ops))  # type: ignore[arg-type]
    return compiled.filtergraph


def _exposure_line(graph: str) -> str:
    """The one filtergraph line that carries the exposure chain."""
    matches = [line for line in graph.split(";") if "eq=gamma=" in line]
    assert len(matches) == 1, f"expected exactly one eq stage, got {matches}"
    return matches[0]


# ---------------------------------------------------------------------------
# photometric mapping: EV -> eq gamma
# ---------------------------------------------------------------------------


def test_ev_maps_photometrically_one_stop_doubles_exposure() -> None:
    """``eq``'s LUT is ``v ** (1/gamma)``, so gamma = 2**EV is one stop per EV."""
    assert ev_to_gamma(0.0) == 1.0
    assert ev_to_gamma(1.0) == 2.0
    assert ev_to_gamma(-1.0) == 0.5
    assert ev_to_gamma(2.0) == 4.0
    assert ev_to_gamma(3.0) == 8.0
    # Positive EV must brighten: gamma > 1 means exponent 1/gamma < 1.
    assert ev_to_gamma(1.0) > 1.0 > ev_to_gamma(-1.0)


def test_gamma_ceiling_is_pinned_to_ffmpeg_eq_max() -> None:
    """EV=+4 is 2**4=16, above eq's 10.0 ceiling — the clamp is a contract.

    FFmpeg would clip 16 silently (``av_clipf``), so the lane clamps first and
    the golden string below is what makes a future FFmpeg range change loud.
    """
    op = ExposureOp(exposure_ev=4.0)
    assert 2.0**4.0 == 16.0  # the unclamped photometric value
    assert op.gamma == EQ_GAMMA_MAX == 10.0
    assert op.gamma_is_clamped is True
    assert "eq=gamma=10.000000:contrast=1.000000" in _exposure_line(_compiled(op))


def test_gamma_floor_is_pinned_to_ffmpeg_eq_min() -> None:
    """The −4 EV end clamps too: 2**-4 = 0.0625 is below eq's 0.1 floor."""
    op = ExposureOp(exposure_ev=-4.0)
    assert 2.0**-4.0 == 0.0625
    assert op.gamma == EQ_GAMMA_MIN == 0.1
    assert op.gamma_is_clamped is True
    assert "eq=gamma=0.100000:contrast=1.000000" in _exposure_line(_compiled(op))


def test_unclamped_band_is_exactly_log2_of_the_eq_gamma_range() -> None:
    """Inside ±log2(10) EV the mapping is the pure exponential — no clamping."""
    limit = math.log2(EQ_GAMMA_MAX)
    assert limit == pytest.approx(3.3219280948873626)
    for i in range(-24, 25):
        ev = limit * i / 24
        op = ExposureOp(exposure_ev=ev)
        assert op.gamma == pytest.approx(2.0**ev)
        assert op.gamma_is_clamped is False, f"EV={ev} clamped unexpectedly"


def test_gamma_is_monotonic_in_ev_across_the_whole_declared_domain() -> None:
    """A brighter stop must never produce a smaller gamma, clamped or not."""
    gammas = [ExposureOp(exposure_ev=ev / 10).gamma for ev in range(-40, 41)]
    assert gammas == sorted(gammas)
    assert min(gammas) == EQ_GAMMA_MIN
    assert max(gammas) == EQ_GAMMA_MAX


# ---------------------------------------------------------------------------
# contrast
# ---------------------------------------------------------------------------


def test_contrast_passes_through_and_stays_well_inside_the_eq_range() -> None:
    """The pack's 0.2–3.0 slope is an identity mapping into eq's −1000..1000."""
    low = ExposureOp(contrast=0.2)
    high = ExposureOp(contrast=3.0)
    assert "eq=gamma=1.000000:contrast=0.200000" in _exposure_line(_compiled(low))
    assert "eq=gamma=1.000000:contrast=3.000000" in _exposure_line(_compiled(high))
    # The lane's whole usable contrast domain must sit inside eq's own bounds.
    assert EQ_CONTRAST_MIN <= 0.2 and 3.0 <= EQ_CONTRAST_MAX
    # And inside eq's fast path: vf_eq.c only takes the non-LUT branch while
    # |contrast| < 7.9, so 0.2–3.0 never pays for the 256-entry LUT rebuild.
    assert max(abs(0.2), abs(3.0)) < 7.9


# ---------------------------------------------------------------------------
# white balance: temperature + tint
# ---------------------------------------------------------------------------


def test_neutral_exposure_emits_eq_only_and_skips_the_rgb_round_trip() -> None:
    """6500 K + tint 0 elides both RGB filters *and* the format round trip.

    ``kelvin2rgb(6500)`` is not an exact identity and neither RGB filter
    short-circuits, so eliding them is a correctness win, not just a speed one.
    """
    graph = _compiled(ExposureOp())
    line = _exposure_line(graph)
    assert line == "[vbase]eq=gamma=1.000000:contrast=1.000000[v1]"
    assert "format=rgb24" not in graph
    assert "colortemperature" not in graph
    assert "colorbalance" not in graph


def test_temperature_emits_exactly_one_rgb_round_trip() -> None:
    """eq stays in YUV; the RGB stages share a single conversion each way."""
    op = ExposureOp(exposure_ev=0.5, temperature_k=3200)
    line = _exposure_line(_compiled(op))
    assert line == (
        "[vbase]eq=gamma=1.414214:contrast=1.000000,format=rgb24,"
        "colortemperature=temperature=3200:pl=1,format=yuv420p[v1]"
    )
    assert line.count("format=rgb24") == 1
    assert line.count("colortemperature=") == 1
    assert "colorbalance" not in line


def test_tint_maps_onto_colorbalance_gm_at_its_declared_bounds() -> None:
    """tint/50 puts ±50 exactly on colorbalance's ±1, and +tint means +green."""
    assert tint_to_gm(0.0) == 0.0
    assert tint_to_gm(TINT_FULL_SCALE) == COLORBALANCE_LIMIT == 1.0
    assert tint_to_gm(-TINT_FULL_SCALE) == -COLORBALANCE_LIMIT == -1.0
    assert tint_to_gm(25.0) == 0.5
    line = _exposure_line(_compiled(ExposureOp(tint=25.0)))
    assert "colorbalance=gm=0.500000:pl=1" in line
    # tint-only still needs the round trip, but not colortemperature.
    assert "colortemperature" not in line
    assert line.count("format=rgb24") == 1


def test_temperature_and_tint_together_share_one_round_trip() -> None:
    """Both stages present, one conversion each way, temperature first."""
    op = ExposureOp(temperature_k=5600, tint=-12.5)
    line = _exposure_line(_compiled(op))
    assert line == (
        "[vbase]eq=gamma=1.000000:contrast=1.000000,format=rgb24,"
        "colortemperature=temperature=5600:pl=1,"
        "colorbalance=gm=-0.250000:pl=1,format=yuv420p[v1]"
    )


def test_preserve_lightness_is_explicit_and_defaults_to_on() -> None:
    """``pl`` is emitted rather than left to each filter's own default (0)."""
    assert ExposureOp().preserve_lightness is True
    assert ":pl=1" in _exposure_stage(ExposureOp(temperature_k=4000))
    assert ":pl=0" in _exposure_stage(ExposureOp(temperature_k=4000, preserve_lightness=False))
    assert ":pl=1" not in _exposure_stage(ExposureOp(temperature_k=4000, preserve_lightness=False))


def test_neutral_temperature_constant_is_the_filter_default_not_a_guess() -> None:
    """6500 K is ``vf_colortemperature.c``'s own ``{.dbl=6500}`` default."""
    assert COLOR_TEMPERATURE_NEUTRAL_K == 6500
    assert COLOR_TEMPERATURE_MIN_K == 1000
    assert COLOR_TEMPERATURE_MAX_K == 40000
    # The neutral value must elide the stage, otherwise "neutral" would still
    # cost a pixel-format round trip on every plain exposure edit.
    assert ExposureOp(temperature_k=COLOR_TEMPERATURE_NEUTRAL_K).needs_rgb_pass is False
    assert ExposureOp(temperature_k=COLOR_TEMPERATURE_MIN_K).needs_rgb_pass is True
    assert ExposureOp(temperature_k=COLOR_TEMPERATURE_MAX_K).needs_rgb_pass is True


# ---------------------------------------------------------------------------
# fail-closed contracts
# ---------------------------------------------------------------------------


def test_out_of_range_values_are_rejected_by_the_model_not_by_ffmpeg() -> None:
    """Every bound is enforced in the IR, so the argv is never a silent clip."""
    for field, value in (
        ("exposure_ev", 4.5),
        ("exposure_ev", -4.5),
        ("contrast", 3.5),
        ("contrast", 0.1),
        ("temperature_k", COLOR_TEMPERATURE_MIN_K - 1),
        ("temperature_k", COLOR_TEMPERATURE_MAX_K + 1),
        ("tint", TINT_FULL_SCALE + 0.5),
        ("tint", -TINT_FULL_SCALE - 0.5),
    ):
        with pytest.raises(ValidationError):
            ExposureOp(**{field: value})
    with pytest.raises(ValidationError):
        ExposureOp(saturation=1.2)  # extra="forbid": no undocumented knobs


def test_exposure_on_an_audio_only_lane_fails_closed() -> None:
    """An exposure op on audio would be silently dropped — so it is an error."""
    with pytest.raises(LaneError, match="needs a video main asset"):
        compile_lane(LaneIR(main=AUDIO_MAIN, ops=(ExposureOp(exposure_ev=1.0),)))


# ---------------------------------------------------------------------------
# lane integration
# ---------------------------------------------------------------------------


def test_exposure_is_duration_neutral_and_composes_in_order() -> None:
    """The op sits in the video chain at its position and never moves the clock."""
    from nexus_ai_agent.creative.rendering import TitleOp, TrimOp

    compiled = compile_lane(
        LaneIR(
            main=MAIN,
            ops=(
                TrimOp(in_us=1_000_000, out_us=6_000_000),
                ExposureOp(exposure_ev=1.0, temperature_k=4300, tint=10.0),
                TitleOp(text="graded"),
            ),
        ),
        fontfile="/fonts/Vazirmatn.ttf",
    )
    assert compiled.duration_us == 5_000_000
    labels = [line.split("]")[-1].strip("]") for line in compiled.filtergraph.split(";")]
    # v1 carries the exposure chain; the title lands after it, not before.
    assert compiled.filtergraph.index("eq=gamma=") < compiled.filtergraph.index("drawtext=")
    assert "[v1]" in compiled.filtergraph and "[v2]" in compiled.filtergraph
    assert "trim=start=1.000000" in compiled.filtergraph
    assert labels  # guard against an empty graph sneaking through


def test_exposure_op_survives_the_ir_hash_and_model_round_trip() -> None:
    """The op is part of the compiled digest, so two different grades differ."""
    ir = LaneIR(main=MAIN, ops=(ExposureOp(exposure_ev=1.0),))
    first = compile_lane(ir)
    second = compile_lane(ir)
    other = compile_lane(LaneIR(main=MAIN, ops=(ExposureOp(exposure_ev=2.0),)))
    assert first.ir_hash == second.ir_hash
    assert first.ir_hash != other.ir_hash
    # The graded graph really is what the encoder would be handed.
    argv = first.argv(Path("/stage/out.mp4"))
    assert argv[argv.index("-filter_complex") + 1] == first.filtergraph
    restored = ExposureOp.model_validate(ExposureOp(exposure_ev=1.0).model_dump(mode="json"))
    assert restored == ExposureOp(exposure_ev=1.0)
