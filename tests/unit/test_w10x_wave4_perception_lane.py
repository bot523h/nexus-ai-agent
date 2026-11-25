"""Wave 4: BS.1770 psychoacoustics, radiometric cut detection and composition."""

from __future__ import annotations

import math

import pytest

from nexus_ai_agent.creative.perception import psychoacoustics_bs1770 as audio
from nexus_ai_agent.creative.perception.analyzer import (
    AudioPerception,
    PerceptionAnalyzer,
    PerceptionReport,
    ScenePerception,
)
from nexus_ai_agent.creative.perception.psychoacoustics_bs1770 import (
    AudioAnalysisError,
    BiquadCoefficients,
    bark_scale,
    bark_spectrum_centroid,
    biquad_process,
    block_loudness,
    design_k_weighting,
    integrated_loudness,
    k_weighting_filter,
    loudness_range,
    momentary_loudness,
    short_term_loudness,
    silence_segments,
    true_peak_dbtp,
)
from nexus_ai_agent.creative.perception.radiometric_scene_cut import (
    CutConfig,
    CutDetectionError,
    FrameTransition,
    SceneCutDetector,
    SceneFrame,
    TransitionKind,
    analyze_sequence,
    classify_transition,
    edge_gradient_flux,
    jensen_shannon_divergence,
    js_distance,
    linear_luminance,
    luminance_histogram,
    shannon_entropy,
    srgb_to_linear,
)


def _sine(fs: int, seconds: float, frequency: float, amplitude: float = 1.0) -> list[float]:
    return [
        amplitude * math.sin(2.0 * math.pi * frequency * index / fs)
        for index in range(int(fs * seconds))
    ]


def _frame(histogram: tuple[float, ...], grid: tuple[tuple[float, ...], ...]) -> SceneFrame:
    return SceneFrame(histogram=histogram, entropy=shannon_entropy(histogram), luminance_grid=grid)


BLACK = ((0.0, 0.0), (0.0, 0.0))
WHITE = ((1.0, 1.0), (1.0, 1.0))


# ---------------------------------------------------------------- psychoacoustics


def test_k_weighting_design_across_rates() -> None:
    for fs in (16000, 44100, 48000, 96000, 192000):
        shelf, highpass = design_k_weighting(fs)
        assert shelf.b0 != 0.0 and highpass.b0 != 0.0
    for bad in (8000, 400000):
        with pytest.raises(AudioAnalysisError):
            design_k_weighting(bad)
    for bad in (math.nan, math.inf):
        with pytest.raises(AudioAnalysisError):
            design_k_weighting(bad)
    with pytest.raises(TypeError):
        design_k_weighting(True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        design_k_weighting("48000")  # type: ignore[arg-type]


def test_biquad_identity_and_validation() -> None:
    identity = BiquadCoefficients(1.0, 0.0, 0.0, 0.0, 0.0)
    assert biquad_process(identity, [1.0, -0.5, 0.25]) == (1.0, -0.5, 0.25)
    with pytest.raises(AudioAnalysisError):
        BiquadCoefficients(math.nan, 0.0, 0.0, 0.0, 0.0)


def test_audio_validation_errors() -> None:
    with pytest.raises(AudioAnalysisError):
        k_weighting_filter([], 48000)
    with pytest.raises(AudioAnalysisError):
        k_weighting_filter([[]], 48000)
    with pytest.raises(AudioAnalysisError):
        k_weighting_filter([[0.0], [0.0, 1.0]], 48000)
    with pytest.raises(AudioAnalysisError):
        k_weighting_filter([[True]], 48000)
    with pytest.raises(AudioAnalysisError):
        k_weighting_filter([[math.nan]], 48000)
    with pytest.raises(AudioAnalysisError):
        block_loudness([[0.0] * 100], 48000, block_ms=0.0)
    with pytest.raises(AudioAnalysisError):
        block_loudness([[0.0] * 100], 48000, overlap=1.0)
    with pytest.raises(AudioAnalysisError):
        block_loudness([[0.0] * 20000], 48000, weights=[1.0, 1.0])
    with pytest.raises(AudioAnalysisError):
        block_loudness([[0.0] * 20000], 48000, weights=[-1.0])
    with pytest.raises(AudioAnalysisError):
        block_loudness([[0.0] * 20000], 48000, weights=[math.inf])
    weighted = block_loudness([_sine(48000, 0.5, 1000.0, 0.5)], 48000, weights=[1.0])
    assert weighted
    assert integrated_loudness([_sine(48000, 0.5, 1000.0, 0.5)], 48000, weights=[1.0]) < 0.0


def test_loudness_of_kilohertz_tone() -> None:
    fs = 48000
    tone = _sine(fs, 2.0, 1000.0, 0.5)
    value = integrated_loudness([tone], fs)
    assert -12.0 < value < -7.0
    moments = momentary_loudness([tone], fs)
    assert len(moments) > 0
    assert all(item > -120.0 for item in moments)


def test_silence_is_negative_infinity() -> None:
    fs = 48000
    silence = [0.0] * (fs * 2)
    assert integrated_loudness([silence], fs) == -math.inf
    assert all(item == -math.inf for item in momentary_loudness([silence], fs))


def test_relative_gate_ignores_quiet_tail() -> None:
    fs = 48000
    loud = _sine(fs, 2.0, 1000.0, 0.5)
    quiet = _sine(fs, 4.0, 1000.0, 0.0005)
    loud_only = integrated_loudness([loud], fs)
    with_tail = integrated_loudness([loud + quiet], fs)
    assert abs(loud_only - with_tail) < 0.5


def test_short_term_and_loudness_range() -> None:
    fs = 48000
    tone = _sine(fs, 6.0, 1000.0, 0.5)
    short = short_term_loudness([tone], fs)
    assert len(short) >= 2
    assert loudness_range([tone], fs) < 1.0
    modulated = [
        sample * (0.1 if index < len(tone) // 2 else 1.0) for index, sample in enumerate(tone)
    ]
    assert loudness_range([modulated], fs) > 1.0
    assert loudness_range([[0.0] * 1000], fs) == 0.0


def test_loudness_range_relative_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "short_term_loudness", lambda *a, **k: (0.0, -69.0))
    assert loudness_range([[0.0] * 1000], 48000) == 0.0
    monkeypatch.setattr(audio, "short_term_loudness", lambda *a, **k: (-20.0,))
    assert loudness_range([[0.0] * 1000], 48000) == 0.0


def test_true_peak_measurements() -> None:
    fs = 48000
    full = _sine(fs, 0.1, 1000.0)
    assert abs(true_peak_dbtp([full], fs) - 0.0) < 0.5
    assert true_peak_dbtp([[0.0] * 100], fs) == -math.inf
    assert true_peak_dbtp([full], fs, oversample=1) < 0.01
    with pytest.raises(TypeError):
        true_peak_dbtp([full], fs, oversample=True)
    for bad in (0, 17):
        with pytest.raises(AudioAnalysisError):
            true_peak_dbtp([full], fs, oversample=bad)


def test_bark_scale_and_centroid() -> None:
    assert bark_scale(0.0) == 0.0
    assert abs(bark_scale(1000.0) - 8.5) < 0.1
    with pytest.raises(AudioAnalysisError):
        bark_scale(-1.0)
    with pytest.raises(AudioAnalysisError):
        bark_scale(math.nan)
    fs = 48000
    tone = _sine(fs, 0.2, 1000.0, 0.8)
    assert abs(bark_spectrum_centroid(tone, fs, n_fft=4096) - 8.5) < 1.0
    assert bark_spectrum_centroid([0.0] * 4096, fs, n_fft=4096) == 0.0
    with pytest.raises(AudioAnalysisError):
        bark_spectrum_centroid([], fs)
    with pytest.raises(AudioAnalysisError):
        bark_spectrum_centroid(tone, fs, n_fft=1000)
    with pytest.raises(AudioAnalysisError):
        bark_spectrum_centroid(tone, fs, n_fft=1)
    with pytest.raises(AudioAnalysisError):
        audio._fft([1j, 2j, 3j])
    assert audio._sinc(0.0) == 1.0
    assert abs(audio._sinc(1.0)) < 1e-12
    with pytest.raises(AudioAnalysisError):
        audio._percentile([], 0.5)
    assert audio._percentile([1.0], 0.5) == 1.0
    assert audio._percentile([1.0, 3.0], 0.0) == 1.0
    assert bark_spectrum_centroid([0.0] * 100, fs, n_fft=1024) == 0.0


def test_silence_segments() -> None:
    fs = 48000
    samples = [0.0] * (fs // 2) + _sine(fs, 0.5, 1000.0, 0.5)
    segments = silence_segments([samples], fs)
    assert len(segments) == 1
    start, end = segments[0]
    assert start == 0.0
    assert 0.4 <= end <= 0.6
    all_silent = silence_segments([[0.0] * fs], fs)
    assert all_silent and all_silent[0][0] == 0.0
    assert silence_segments([_sine(fs, 0.2, 1000.0, 0.5)], fs) == ()
    assert silence_segments([[0.0] * fs], fs, min_ms=10_000.0) == ()
    with pytest.raises(AudioAnalysisError):
        silence_segments([[0.0] * 100], fs, threshold_dbfs=math.nan)
    with pytest.raises(AudioAnalysisError):
        silence_segments([[0.0] * 100], fs, min_ms=-1.0)
    short_run = [0.0] * (fs // 10) + _sine(fs, 0.2, 1000.0, 0.5)
    assert silence_segments([short_run], fs, min_ms=200.0) == ()


# ---------------------------------------------------------------- radiometry / cuts


def test_srgb_transfer_function() -> None:
    assert srgb_to_linear(0.0) == 0.0
    assert srgb_to_linear(1.0) == pytest.approx(1.0)
    assert srgb_to_linear(0.02) == pytest.approx(0.02 / 12.92)
    assert srgb_to_linear(0.5) == pytest.approx(((0.5 + 0.055) / 1.055) ** 2.4)
    for bad in (-0.1, 1.1, math.nan):
        with pytest.raises(CutDetectionError):
            srgb_to_linear(bad)
    with pytest.raises(CutDetectionError):
        srgb_to_linear(True)  # type: ignore[arg-type]


def test_luminance_and_histogram() -> None:
    assert linear_luminance((0.0, 0.0, 0.0)) == 0.0
    assert linear_luminance((1.0, 1.0, 1.0)) == pytest.approx(1.0)
    with pytest.raises(CutDetectionError):
        linear_luminance((1.0, 1.0))  # type: ignore[arg-type]
    histogram = luminance_histogram([(0.0, 0.0, 0.0)] * 3 + [(1.0, 1.0, 1.0)], bins=4)
    assert sum(histogram) == pytest.approx(1.0)
    assert histogram[0] == pytest.approx(0.75)
    assert histogram[-1] == pytest.approx(0.25)
    with pytest.raises(CutDetectionError):
        luminance_histogram([])
    with pytest.raises(CutDetectionError):
        luminance_histogram([(0.0, 0.0, 0.0)], bins=1)
    with pytest.raises(CutDetectionError):
        luminance_histogram([(0.0, 0.0)])  # type: ignore[list-item]


def test_entropy_and_js_divergence() -> None:
    uniform = [1.0 / 32.0] * 32
    assert shannon_entropy(uniform) == pytest.approx(math.log2(32))
    assert shannon_entropy([1.0, 0.0]) == pytest.approx(0.0)
    assert jensen_shannon_divergence(uniform, uniform) == pytest.approx(0.0)
    assert jensen_shannon_divergence([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
    assert js_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
    with pytest.raises(CutDetectionError):
        shannon_entropy([])
    with pytest.raises(CutDetectionError):
        shannon_entropy([-0.1, 1.1])
    with pytest.raises(CutDetectionError):
        shannon_entropy([0.0, 0.0])
    with pytest.raises(CutDetectionError):
        shannon_entropy([True, 1.0])
    with pytest.raises(CutDetectionError):
        jensen_shannon_divergence([1.0, 0.0], [1.0, 0.0, 0.0])


def test_edge_gradient_flux() -> None:
    grid = ((0.0, 0.5), (0.5, 1.0))
    assert edge_gradient_flux(grid, grid) == 0.0
    assert edge_gradient_flux(BLACK, WHITE) > 0.0
    assert edge_gradient_flux(((0.0,),), ((1.0,),)) == 0.0
    with pytest.raises(CutDetectionError):
        edge_gradient_flux(BLACK, ((0.0, 0.0, 0.0),))
    with pytest.raises(CutDetectionError):
        edge_gradient_flux((), ())
    with pytest.raises(CutDetectionError):
        edge_gradient_flux(((math.nan,),), ((0.0,),))
    with pytest.raises(CutDetectionError):
        edge_gradient_flux(((0.0, 0.0), (0.0,)), ((0.0, 0.0), (0.0, 0.0)))


def test_scene_frame_validation() -> None:
    frame = _frame((0.5, 0.5), BLACK)
    assert frame.entropy == pytest.approx(1.0)
    with pytest.raises(CutDetectionError):
        SceneFrame(histogram=(1.0,), entropy=0.0, luminance_grid=BLACK)
    with pytest.raises(CutDetectionError):
        SceneFrame(histogram=(0.0, 0.0), entropy=0.0, luminance_grid=BLACK)
    with pytest.raises(CutDetectionError):
        SceneFrame(histogram=(0.5, 0.5), entropy=math.nan, luminance_grid=BLACK)
    with pytest.raises(CutDetectionError):
        SceneFrame(histogram=(0.5, 0.5), entropy=0.0, luminance_grid=())
    with pytest.raises(CutDetectionError):
        SceneFrame.from_rgb_grid([])
    with pytest.raises(CutDetectionError):
        SceneFrame.from_rgb_grid([[(0.0, 0.0, 0.0)], [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)]])
    with pytest.raises(CutDetectionError):
        SceneFrame.from_rgb_grid([[(0.0, 0.0)]])  # type: ignore[list-item]
    built = SceneFrame.from_rgb_grid([[(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)]], bins=4)
    assert built.luminance_grid == ((0.0, 1.0),)


def test_cut_config_validation() -> None:
    CutConfig()
    with pytest.raises(CutDetectionError):
        CutConfig(hard_cut_distance=-1.0)
    with pytest.raises(CutDetectionError):
        CutConfig(hard_cut_distance=math.nan)
    with pytest.raises(CutDetectionError):
        CutConfig(dissolve_distance=0.5, hard_cut_distance=0.1)
    with pytest.raises(CutDetectionError):
        CutConfig(static_distance=0.5, dissolve_distance=0.2)


def test_transition_record_validation() -> None:
    good = FrameTransition(TransitionKind.STATIC_HOLD, 0.0, 0.0, 0.0, 1.0, 1.0)
    assert good.kind is TransitionKind.STATIC_HOLD
    with pytest.raises(CutDetectionError):
        FrameTransition("STATIC_HOLD", 0.0, 0.0, 0.0, 1.0, 1.0)  # type: ignore[arg-type]
    with pytest.raises(CutDetectionError):
        FrameTransition(TransitionKind.STATIC_HOLD, -1.0, 0.0, 0.0, 1.0, 1.0)


def test_classify_transition_kinds() -> None:
    config = CutConfig()
    black = _frame((1.0, 0.0, 0.0, 0.0), BLACK)
    white = _frame((0.0, 0.0, 0.0, 1.0), WHITE)
    near_black = _frame((0.95, 0.05, 0.0, 0.0), BLACK)
    shifted = _frame((0.90, 0.10, 0.0, 0.0), BLACK)
    slight = _frame((0.985, 0.015, 0.0, 0.0), ((0.1, 0.1), (0.1, 0.1)))

    assert classify_transition(black, black, None, config).kind is TransitionKind.STATIC_HOLD
    assert classify_transition(black, white, None, config).kind is TransitionKind.HARD_CUT
    assert classify_transition(near_black, shifted, None, config).kind is TransitionKind.DISSOLVE
    # Below the dissolve threshold but with large flux must not be called static.
    assert classify_transition(black, slight, None, config).kind is TransitionKind.DISSOLVE
    flash = classify_transition(black, white, black, config)
    assert flash.kind is TransitionKind.FLASH_TRANSIENT
    assert classify_transition(black, white, white, config).kind is TransitionKind.HARD_CUT
    # lookahead present, moderate change -> DISSOLVE
    assert (
        classify_transition(near_black, shifted, near_black, config).kind is TransitionKind.DISSOLVE
    )
    # lookahead present, identical frames -> STATIC_HOLD
    assert classify_transition(black, black, black, config).kind is TransitionKind.STATIC_HOLD
    micro_a = _frame((0.9995, 0.0005, 0.0, 0.0), BLACK)
    micro_b = _frame((0.99925, 0.00075, 0.0, 0.0), ((0.1, 0.1), (0.1, 0.1)))
    sensitive = CutConfig(static_distance=0.01, static_flux=0.0)
    # tiny histogram change but large flux -> not static, classified DISSOLVE
    assert classify_transition(black, micro_b, micro_a, sensitive).kind is TransitionKind.DISSOLVE
    assert classify_transition(black, micro_b, None, sensitive).kind is TransitionKind.DISSOLVE


def test_analyze_sequence_and_streaming_detector() -> None:
    config = CutConfig()
    black = _frame((1.0, 0.0, 0.0, 0.0), BLACK)
    white = _frame((0.0, 0.0, 0.0, 1.0), WHITE)
    assert analyze_sequence([black], config) == ()
    transitions = analyze_sequence([black, white, black], config)
    assert len(transitions) == 2
    assert transitions[0].kind is TransitionKind.FLASH_TRANSIENT

    detector = SceneCutDetector(config)
    assert detector.push(black) is None
    first = detector.push(white)
    assert first is not None and first.kind is TransitionKind.HARD_CUT
    detector.push(black)
    assert len(detector.transitions) == 2
    finalized = detector.finalize()
    assert finalized[0].kind is TransitionKind.FLASH_TRANSIENT
    with pytest.raises(CutDetectionError):
        detector.push(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------- analyzer


def test_perception_report_composition() -> None:
    fs = 48000
    analyzer = PerceptionAnalyzer()
    tone = _sine(fs, 2.0, 1000.0, 0.5)
    frames = [
        _frame((1.0, 0.0, 0.0, 0.0), BLACK),
        _frame((0.0, 0.0, 0.0, 1.0), WHITE),
        _frame((1.0, 0.0, 0.0, 0.0), BLACK),
    ]
    report = analyzer.analyze([tone], fs, frames, n_fft=2048)
    assert isinstance(report, PerceptionReport)
    assert report.audio.integrated_lkfs < 0.0
    assert report.scene.total_cuts == 2
    assert report.scene.flashes == 1
    assert 0 <= report.scene.mean_entropy <= math.log2(4)
    assert analyzer.analyze_scene([]).transitions == ()
    assert analyzer.analyze_scene([]).mean_entropy == 0.0


def test_perception_receipt_validation() -> None:
    with pytest.raises(ValueError):
        AudioPerception(
            integrated_lkfs=math.nan,
            momentary_lkfs=(),
            short_term_lkfs=(),
            loudness_range_lu=0.0,
            true_peak_dbtp=0.0,
            bark_centroid=0.0,
            silences=(),
        )
    with pytest.raises(ValueError):
        AudioPerception(
            integrated_lkfs=0.0,
            momentary_lkfs=(math.inf,),
            short_term_lkfs=(),
            loudness_range_lu=0.0,
            true_peak_dbtp=0.0,
            bark_centroid=0.0,
            silences=(),
        )
    with pytest.raises(ValueError):
        AudioPerception(
            integrated_lkfs=0.0,
            momentary_lkfs=(),
            short_term_lkfs=(),
            loudness_range_lu=-1.0,
            true_peak_dbtp=0.0,
            bark_centroid=0.0,
            silences=(),
        )
    with pytest.raises(ValueError):
        AudioPerception(
            integrated_lkfs=0.0,
            momentary_lkfs=(),
            short_term_lkfs=(),
            loudness_range_lu=0.0,
            true_peak_dbtp=0.0,
            bark_centroid=-1.0,
            silences=(),
        )
    with pytest.raises(ValueError):
        AudioPerception(
            integrated_lkfs=0.0,
            momentary_lkfs=(),
            short_term_lkfs=(),
            loudness_range_lu=0.0,
            true_peak_dbtp=0.0,
            bark_centroid=0.0,
            silences=((1.0, 0.5),),
        )

    transition = FrameTransition(TransitionKind.HARD_CUT, 1.0, 1.0, 1.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        ScenePerception((transition,), 0, 0, 0, 0, 0.0)
    with pytest.raises(ValueError):
        ScenePerception((), -1, 0, 0, 0, 0.0)
    with pytest.raises(ValueError):
        ScenePerception((), 0, 0, 0, 0, math.nan)
    good_scene = ScenePerception((transition,), 1, 0, 0, 0, 1.0)
    assert good_scene.total_cuts == 1
    good_audio = AudioPerception(
        integrated_lkfs=-math.inf,
        momentary_lkfs=(),
        short_term_lkfs=(),
        loudness_range_lu=0.0,
        true_peak_dbtp=-math.inf,
        bark_centroid=0.0,
        silences=(),
    )
    with_silence = AudioPerception(
        integrated_lkfs=0.0,
        momentary_lkfs=(),
        short_term_lkfs=(),
        loudness_range_lu=0.0,
        true_peak_dbtp=0.0,
        bark_centroid=0.0,
        silences=((0.0, 1.0),),
    )
    assert with_silence.silences == ((0.0, 1.0),)
    assert PerceptionReport(audio=good_audio, scene=good_scene).scene is good_scene
    with pytest.raises(TypeError):
        PerceptionReport(audio="x", scene=good_scene)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PerceptionReport(audio=good_audio, scene="x")  # type: ignore[arg-type]


def test_perception_dataclasses_normalise_mutable_sequences() -> None:
    audio = AudioPerception(
        integrated_lkfs=-20.0,
        momentary_lkfs=[-20.0],  # type: ignore[arg-type]
        short_term_lkfs=[-20.0],  # type: ignore[arg-type]
        loudness_range_lu=0.0,
        true_peak_dbtp=-1.0,
        bark_centroid=1.0,
        silences=[[0.0, 1.0]],  # type: ignore[arg-type]
    )
    assert audio.momentary_lkfs == (-20.0,)
    assert audio.short_term_lkfs == (-20.0,)
    assert audio.silences == ((0.0, 1.0),)
    with pytest.raises(AttributeError):
        audio.momentary_lkfs.append(-20.0)  # type: ignore[attr-defined]

    scene = ScenePerception([], 0, 0, 0, 0, 0.0)  # type: ignore[arg-type]
    assert scene.transitions == ()

    frame = SceneFrame([0.5, 0.5], 1.0, [[0.1, 0.9]])  # type: ignore[arg-type]
    assert frame.histogram == (0.5, 0.5)
    assert frame.luminance_grid == ((0.1, 0.9),)
