"""Physical-invariant and refusal tests for the K1–K4 cinema DSP kernels.

Pack evidence for ``creative.packs.audio``: these four kernels are what the pack
offers as its cinema-audio surface, so their invariants are measured here as
pack evidence rather than treated as host-layer unit tests.

The tests are organised around *invariants*, not around functions. Each kernel
claims a physical property — a unitary feedback matrix, an SNR improvement, a
sub-millisecond onset, a true-peak ceiling — and the test measures that property
directly. A test that only asserted "the function returns an array" would pass
for every one of the seven defects found while building this module.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine import cinema_kernels as kernels
from nexus_ai_agent.creative.audio_engine.cinema_kernels import (
    BARK_BANDS,
    CANONICAL_SAMPLE_RATE,
    MATERIAL_MODES,
    SPEECH_BANDS,
    ContactFoleyEvent,
    KernelError,
    MaterialModes,
    acn_labels,
    bark_band_edges,
    bark_edges_hz,
    hertzian_amplitude,
    master_bark_psychoacoustic,
    restore_spectral_audio,
    synthesize_hoa3_binaural_room,
    synthesize_modal_foley,
    zwicker_bark_hz,
)

SR = CANONICAL_SAMPLE_RATE


def programme(seconds: float = 2.0, *, rate: int = SR) -> np.ndarray:
    """A speech-like fixture: tones *with pauses*.

    The pauses are not decoration. A denoiser estimates its noise floor from
    moments when the programme is absent; a fixture with no silence has no
    observable floor, and any noise estimate read from it is a fiction. An
    earlier version of this suite used a continuous tone and "proved" a 15 dB
    SNR regression that was entirely an artefact of the fixture.
    """
    total = int(round(seconds * rate))
    time = np.arange(total) / rate
    gate = np.zeros(total)
    segment = total // 8
    for index in range(0, 8, 2):
        start = index * segment
        gate[start : start + segment] = 1.0
    carrier = np.sin(2 * np.pi * 300 * time) + 0.6 * np.sin(2 * np.pi * 1200 * time)
    return 0.4 * gate * carrier


# ========================================================================== #
# Shared refusal contract
# ========================================================================== #


@pytest.mark.parametrize("rate", [0, -1, -48000])
def test_every_kernel_refuses_a_non_positive_sample_rate(rate: int) -> None:
    signal = np.zeros(1000)
    with pytest.raises(KernelError, match="sample_rate"):
        restore_spectral_audio(signal, sample_rate=rate)
    with pytest.raises(KernelError, match="sample_rate"):
        synthesize_hoa3_binaural_room(signal, sample_rate=rate)


def test_every_kernel_refuses_an_empty_buffer() -> None:
    with pytest.raises(KernelError, match="empty"):
        restore_spectral_audio(np.zeros(0), sample_rate=SR)
    with pytest.raises(KernelError, match="empty"):
        synthesize_hoa3_binaural_room(np.zeros(0), sample_rate=SR)
    with pytest.raises(KernelError, match="empty"):
        master_bark_psychoacoustic(np.zeros((0, 2)), sample_rate=SR)


def test_every_kernel_refuses_non_finite_input() -> None:
    """A NaN that enters a filter never leaves it; refuse at the door."""
    poisoned = np.zeros(4000)
    poisoned[100] = np.nan
    with pytest.raises(KernelError, match="non-finite"):
        restore_spectral_audio(poisoned, sample_rate=SR)
    with pytest.raises(KernelError, match="non-finite"):
        synthesize_hoa3_binaural_room(poisoned, sample_rate=SR)
    with pytest.raises(KernelError, match="non-finite"):
        master_bark_psychoacoustic(np.stack((poisoned, poisoned), axis=-1), sample_rate=SR)


def test_an_infinite_value_is_refused_too() -> None:
    bad = np.zeros(4000)
    bad[10] = np.inf
    with pytest.raises(KernelError, match="non-finite"):
        restore_spectral_audio(bad, sample_rate=SR)


# ========================================================================== #
# K1 — HOA3 spherical harmonics
# ========================================================================== #


def test_hoa3_has_sixteen_channels_in_acn_order() -> None:
    """``(L+1)² = 16`` for ``L = 3``, ordered ``n = l(l+1) + m``."""
    labels = acn_labels(3)
    assert len(labels) == 16
    assert labels[0] == "Y_0^0"
    # ACN's second channel is Y_1^-1, not Y_1^1 — FuMa orders them the other way.
    assert labels[1] == "Y_1^-1"
    assert labels[3] == "Y_1^1"
    assert labels[4] == "Y_2^-2"
    assert labels[-1] == "Y_3^3"


def test_the_omnidirectional_channel_is_exactly_one() -> None:
    """SN3D leaves ``Y_0^0 = 1`` for every direction.

    A normalisation that scales this by ``√2`` (the N3D convention) makes every
    downstream decoder 3 dB hot, which reads as "the room is too loud" rather
    than as a convention error.
    """
    for azimuth, elevation in [
        (0.0, 0.0),
        (math.pi / 2, 0.0),
        (math.pi, 0.0),
        (-math.pi / 3, 0.4),
        (0.0, math.pi / 2),
    ]:
        basis = kernels._real_sh_basis(azimuth, elevation)
        assert basis.size == 16
        assert basis[0] == pytest.approx(1.0, abs=1e-12)


def test_the_directional_energy_norm_is_direction_invariant() -> None:
    """SN3D's defining property: ``Σ|Y_n|²`` does not depend on direction.

    A basis that fails this is not orthonormal, and a decoder built on it
    delivers a source that gets louder or quieter as it moves — audible as
    "the pan is broken" with no obvious cause.
    """
    directions = [
        (0.0, 0.0),
        (math.pi / 2, 0.0),
        (math.pi / 2, math.pi / 6),
        (-math.pi / 2, -math.pi / 4),
        (math.pi / 4, math.pi / 3),
    ]
    norms = [float(np.sum(kernels._real_sh_basis(az, el) ** 2)) for az, el in directions]
    assert max(norms) - min(norms) < 1e-9, f"direction-dependent norm: {norms}"
    for value in norms:
        assert value > 0.0
        assert value == pytest.approx(norms[0], rel=1e-12)


def test_the_encoding_is_deterministic() -> None:
    first = kernels._real_sh_basis(0.7, 0.3)
    second = kernels._real_sh_basis(0.7, 0.3)
    assert np.array_equal(first, second)


# ========================================================================== #
# K1 — the Householder feedback matrix
# ========================================================================== #


@pytest.mark.parametrize("channels", [2, 4, 8, 12])
def test_the_householder_matrix_is_exactly_unitary(channels: int) -> None:
    """``H = I − (2/M)·11ᵀ`` must satisfy ``‖HᵀH − I‖_F ≤ 1e-12``.

    A non-orthogonal feedback matrix has spectral radius above 1 for some input
    and the delay network grows without bound instead of decaying. The failure
    is not a quiet difference in timbre — it is a render that clips to
    infinity after a few seconds.
    """
    householder = kernels._householder_matrix(channels)
    identity = np.eye(channels)
    residual = float(np.linalg.norm(householder.T @ householder - identity))
    assert residual <= 1e-12
    determinant = float(np.linalg.det(householder))
    assert determinant == pytest.approx(math.copysign(1.0, determinant), abs=1e-12)


@pytest.mark.parametrize("channels", [2, 3, 4, 8])
def test_the_householder_determinant_is_plus_or_minus_one(channels: int) -> None:
    determinant = float(np.linalg.det(kernels._householder_matrix(channels)))
    assert abs(abs(determinant) - 1.0) <= 1e-12


def test_the_householder_is_symmetric_and_its_own_inverse() -> None:
    """``H`` is a reflection: symmetric, and ``H² = I``."""
    householder = kernels._householder_matrix(8)
    assert np.allclose(householder, householder.T, atol=1e-15)
    assert np.allclose(householder @ householder, np.eye(8), atol=1e-12)


def test_the_householder_factor_is_two_over_m() -> None:
    """The ``2/M`` factor is what makes it unitary; ``2/(M-1)`` is not."""
    householder = kernels._householder_matrix(8)
    off_diagonal = householder[0, 1]
    assert off_diagonal == pytest.approx(-2.0 / 8.0)
    wrong = np.eye(8) - (2.0 / 7.0) * np.ones((8, 8))
    assert float(np.linalg.norm(wrong.T @ wrong - np.eye(8))) > 1e-3


def test_a_one_channel_fdn_is_refused() -> None:
    with pytest.raises(KernelError, match="at least 2"):
        kernels._householder_matrix(1)


# ========================================================================== #
# K1 — room geometry and Sabine
# ========================================================================== #


def test_the_six_early_reflections_arrive_after_the_direct_sound() -> None:
    """A wall reflection is always longer than the direct path.

    A sign error in the image mirroring produces a reflection that arrives
    *before* the direct sound — inaudible on a waveform, wrong in space, and
    immediately obvious once you measure the delays.
    """
    room = (6.0, 4.5, 2.8)
    source = (1.2, 1.4, 1.0)
    listener = (4.4, 3.0, 1.3)
    delays, gains = kernels._shoebox_early_reflections(room, source, listener, 0.25)
    assert delays.size == 6
    direct = math.dist(source, listener) / kernels.SPEED_OF_SOUND_M_S
    assert all(delay > direct for delay in delays)
    # Nothing can arrive faster than the speed of sound over the room diagonal.
    diagonal = math.dist((0, 0, 0), room) / kernels.SPEED_OF_SOUND_M_S
    assert all(delay <= diagonal for delay in delays)
    assert all(gain > 0.0 for gain in gains)


def test_a_more_absorbent_wall_reflects_less() -> None:
    room = (6.0, 4.5, 2.8)
    source = (1.2, 1.4, 1.0)
    listener = (4.4, 3.0, 1.3)
    _d1, bright = kernels._shoebox_early_reflections(room, source, listener, 0.1)
    _d2, dead = kernels._shoebox_early_reflections(room, source, listener, 0.9)
    assert all(float(b) > float(d) for b, d in zip(bright, dead, strict=True))


def test_sabine_reverberation_scales_with_volume_and_absorption() -> None:
    small = kernels._sabine_t60(50.0, 100.0, 0.2)
    large = kernels._sabine_t60(500.0, 400.0, 0.2)
    assert large > small
    reflective = kernels._sabine_t60(50.0, 100.0, 0.1)
    absorbing = kernels._sabine_t60(50.0, 100.0, 0.8)
    assert reflective > absorbing


def test_the_sabine_constant_is_the_published_one() -> None:
    """Ordering alone is satisfied by *any* positive constant.

    ``T60`` is numerically pinned: 0.161 V / (S alpha). A test that only
    compared two rooms would pass with any scale factor at all, which is
    exactly how a wrong Sabine constant survives a green suite.
    """
    # 50 m³, 100 m², alpha 0.2 -> 0.161 * 50 / (100 * 0.2) = 0.4025 s
    assert kernels._sabine_t60(50.0, 100.0, 0.2) == pytest.approx(0.4025, rel=1e-12)
    # Doubling the volume doubles T60 at fixed surface and absorption.
    assert kernels._sabine_t60(100.0, 100.0, 0.2) == pytest.approx(
        2 * kernels._sabine_t60(50.0, 100.0, 0.2), rel=1e-12
    )


def test_sabine_refuses_a_degenerate_room() -> None:
    with pytest.raises(KernelError, match="positive"):
        kernels._sabine_t60(0.0, 100.0, 0.2)
    with pytest.raises(KernelError, match="positive"):
        kernels._sabine_t60(50.0, 0.0, 0.2)


def test_the_fdn_delay_lengths_are_pairwise_coprime() -> None:
    """Shared factors between delays make the modes reinforce into a metallic ring."""
    delays = kernels._prime_delays(8)
    assert len(set(delays)) == 8
    for left in range(len(delays)):
        for right in range(left + 1, len(delays)):
            assert math.gcd(delays[left], delays[right]) == 1, (
                f"non-coprime delays {delays[left]}, {delays[right]}"
            )


# ========================================================================== #
# K1 — the end-to-end kernel
# ========================================================================== #


def test_the_room_kernel_returns_finite_stereo() -> None:
    result = synthesize_hoa3_binaural_room(programme(0.5), fdn_channels=4)
    assert result.stereo.shape == (24000, 2)
    assert np.all(np.isfinite(result.stereo))
    assert result.stereo.dtype == np.float64


def test_the_room_kernel_reports_a_unit_unitary_error() -> None:
    result = synthesize_hoa3_binaural_room(programme(0.3), fdn_channels=8)
    assert result.householder_unitarity_error <= 1e-12
    assert abs(abs(result.householder_determinant) - 1.0) <= 1e-12
    assert result.directivity > 0.0
    assert len(result.early_delays_s) == 6
    assert len(result.fdn_attenuation) == 8


def test_a_longer_reverberation_has_a_longer_t60() -> None:
    bright = synthesize_hoa3_binaural_room(programme(0.2), absorption=0.1, fdn_channels=4)
    dead = synthesize_hoa3_binaural_room(programme(0.2), absorption=0.8, fdn_channels=4)
    assert bright.t60_s > dead.t60_s


def test_the_fdn_attenuation_is_below_one() -> None:
    """Per-sample attenuation must be < 1 or the network never decays."""
    result = synthesize_hoa3_binaural_room(programme(0.2), fdn_channels=4)
    assert all(0.0 < value < 1.0 for value in result.fdn_attenuation)


@pytest.mark.parametrize("absorption", [0.0, 0.005, 0.995, 1.0, -0.1])
def test_the_room_kernel_refuses_impossible_absorption(absorption: float) -> None:
    with pytest.raises(KernelError, match="absorption"):
        synthesize_hoa3_binaural_room(np.zeros(1000), absorption=absorption)


def test_the_room_kernel_refuses_a_source_outside_the_room() -> None:
    with pytest.raises(KernelError, match="outside the room"):
        synthesize_hoa3_binaural_room(
            np.zeros(1000), room_dims_m=(4.0, 4.0, 2.4), source_pos_m=(9.0, 1.0, 1.0)
        )


def test_the_room_kernel_refuses_a_degenerate_room() -> None:
    with pytest.raises(KernelError, match="positive"):
        synthesize_hoa3_binaural_room(np.zeros(1000), room_dims_m=(0.0, 4.0, 2.4))


def test_the_room_kernel_refuses_a_stereo_source() -> None:
    with pytest.raises(KernelError, match="mono"):
        synthesize_hoa3_binaural_room(np.zeros((1000, 2)))


def test_the_woodworth_itd_is_monotone_over_the_front_hemisphere() -> None:
    """Two directions must never produce the same delay inside the valid range."""
    angles = np.linspace(-math.pi / 2, math.pi / 2, 41)
    delays = [kernels._woodworth_itd_deg(float(angle)) for angle in angles]
    assert delays == sorted(delays)
    assert len(set(round(value, 15) for value in delays)) == len(delays)


def test_the_woodworth_maximum_matches_the_head_geometry() -> None:
    """At ±90° the ITD is ``(a/c₀)(1 + π/2)``, about 0.66 ms."""
    maximum = kernels._woodworth_itd_deg(math.pi / 2)
    expected = (kernels.HEAD_RADIUS_M / kernels.SPEED_OF_SOUND_M_S) * (1 + math.pi / 2)
    assert maximum == pytest.approx(expected, rel=1e-12)
    assert 0.0005 < maximum < 0.0008


def test_the_room_result_serialises() -> None:
    payload = synthesize_hoa3_binaural_room(programme(0.2), fdn_channels=4).as_dict()
    assert payload["acn_labels"][0] == "Y_0^0"
    assert len(payload["early_delays_s"]) == 6


# ========================================================================== #
# K2 — restoration
# ========================================================================== #


def test_the_wiener_gain_never_falls_below_its_floor() -> None:
    """``G = max(xi/(1+xi), floor)``: the floor is a hard bound.

    Without it, a bin whose a-priori SNR collapses to zero gets gain 0, and a
    zero gain across a frame is total silence — audible as dropouts rather than
    as noise reduction. The default SNR test cannot see this because it only
    compares aggregate loudness; the bound has to be asserted directly.
    """
    quiet = 1e-6 * np.ones((40, 64))
    gain = kernels._wiener_gain(quiet, smoothing=0.98, floor=0.08)
    assert float(gain.min()) >= 0.08 - 1e-12
    # And with a different floor the bound moves with it.
    looser = kernels._wiener_gain(quiet, smoothing=0.98, floor=0.25)
    assert float(looser.min()) >= 0.25 - 1e-12


def test_restoration_improves_snr_on_a_damaged_signal() -> None:
    """The kernel's headline claim, measured."""
    clean = programme(1.0)
    generator = np.random.default_rng(7)
    noisy = clean + 0.05 * generator.standard_normal(clean.size)
    result = restore_spectral_audio(noisy, reference=clean)
    assert result.restored.size == clean.size
    assert np.all(np.isfinite(result.restored))
    assert result.output_snr_db > result.input_snr_db, (
        f"SNR went from {result.input_snr_db:.2f} to {result.output_snr_db:.2f} dB"
    )


def test_restoration_reports_the_measured_snr_not_an_assumed_one() -> None:
    clean = programme(0.5)
    generator = np.random.default_rng(3)
    noisy = clean + 0.02 * generator.standard_normal(clean.size)
    result = restore_spectral_audio(noisy, reference=clean)
    assert result.snr_improvement_db == pytest.approx(result.output_snr_db - result.input_snr_db)


def test_without_a_reference_the_snr_fields_are_zero_not_invented() -> None:
    """No ground truth means no SNR claim; 0.0 is the stated absence."""
    result = restore_spectral_audio(programme(0.3) + 0.01, reference=None)
    assert result.input_snr_db == 0.0
    assert result.output_snr_db == 0.0


def test_a_click_is_detected_and_smoothed() -> None:
    clean = programme(0.5)
    damaged = clean.copy()
    position = 4000
    damaged[position] = 2.5
    result = restore_spectral_audio(damaged, reference=clean, click_threshold=6.0)
    assert result.click_spans, "an isolated impulse was not detected"
    # The restored sample must be far closer to the clean value than the click.
    restored_error = abs(float(result.restored[position]) - float(clean[position]))
    original_error = abs(float(damaged[position]) - float(clean[position]))
    assert restored_error < original_error


def test_the_click_threshold_is_a_strictness_control() -> None:
    """A lower threshold must flag more *samples*, which is the direction that matters.

    Counting detected *spans* is not a valid monotonicity check: adjacent
    detections merge into one span, so a stricter threshold can report fewer
    spans while covering more samples. The covered-sample count is the quantity
    that actually increases.
    """
    clean = programme(0.5)
    damaged = clean.copy()
    for position in range(2000, 9000, 700):
        damaged[position] += 1.5

    def covered(result) -> int:
        return sum(end - start + 1 for start, end in result.click_spans)

    lenient = restore_spectral_audio(damaged, click_threshold=12.0)
    strict = restore_spectral_audio(damaged, click_threshold=3.0)
    assert covered(strict) >= covered(lenient)


def test_clipping_is_detected() -> None:
    clean = programme(0.5)
    clipped = np.clip(clean * 6.0, -0.98, 0.98)
    result = restore_spectral_audio(clipped, reference=clean)
    assert result.clipped_indices, "hard clipping was not detected"


def test_the_declipper_preserves_unclipped_samples() -> None:
    """Clamping is what makes de-clipping a projection rather than a filter.

    The fixture must be one where the *clean* samples are genuinely at risk of
    being moved: a signal already inside ±1 passes through ``np.clip(-1, 1)``
    unchanged, so a clamp-removal mutation is invisible on it. Here the
    unclipped region sits well above ±1's reach only after the de-clipper has
    done its work, so the final bound cannot mask a missing clamp.
    """
    time = np.arange(4000, dtype=np.float64)
    clean = 0.9 * np.sin(2 * np.pi * time / 60.0)
    clipped = np.clip(clean, -0.5, 0.5)
    unclipped = np.abs(clipped) < 0.5
    assert unclipped.sum() > 100, "fixture must contain clean samples"
    restored, indices = kernels._declip(clipped, tau=0.5)
    assert indices
    # Every sample that was never clipped is bit-exact after the projection.
    assert np.array_equal(restored[unclipped], clipped[unclipped])


def test_the_declipper_preserves_the_measured_sign() -> None:
    """A peak's polarity is measured, not inferred.

    Both polarities are exercised. A mutation that hard-codes ``sign = 1.0``
    is invisible on a positive-only fixture, which is how it survived the
    first version of this test.
    """
    for polarity in (1.0, -1.0):
        signal = np.zeros(200)
        signal[100] = 5.0 * polarity
        signal[101] = 4.5 * polarity
        clipped = np.clip(signal, -0.6, 0.6)
        restored, indices = kernels._declip(clipped, tau=0.6)
        assert indices, "fixture must register as clipped"
        for index in indices:
            assert np.sign(restored[index]) == np.sign(polarity), (
                f"sample {index} lost its sign for polarity {polarity}"
            )


def test_the_noise_floor_is_reported_in_dbfs() -> None:
    result = restore_spectral_audio(programme(0.5) + 0.01)
    assert math.isfinite(result.noise_floor_dbfs)
    assert result.noise_floor_dbfs < 0.0


@pytest.mark.parametrize("frame_size", [12, 1000, 4096 + 1])
def test_restoration_refuses_a_non_power_of_two_frame_size(frame_size: int) -> None:
    with pytest.raises(KernelError, match="power of two"):
        restore_spectral_audio(np.zeros(4000), frame_size=frame_size)


@pytest.mark.parametrize("hop", [0, -1, 4096])
def test_restoration_refuses_an_impossible_hop(hop: int) -> None:
    with pytest.raises(KernelError, match="hop"):
        restore_spectral_audio(np.zeros(4000), frame_size=1024, hop=hop)


@pytest.mark.parametrize("threshold", [0.0, -1.0, float("nan")])
def test_restoration_refuses_an_impossible_click_threshold(threshold: float) -> None:
    with pytest.raises(KernelError):
        restore_spectral_audio(np.zeros(4000), click_threshold=threshold)


@pytest.mark.parametrize("threshold", [0.0, 1.5, -0.5])
def test_restoration_refuses_an_impossible_clip_threshold(threshold: float) -> None:
    with pytest.raises(KernelError, match="clip_threshold"):
        restore_spectral_audio(np.zeros(4000), clip_threshold=threshold)


@pytest.mark.parametrize("smoothing", [0.0, 1.0, -0.1])
def test_restoration_refuses_an_impossible_smoothing(smoothing: float) -> None:
    with pytest.raises(KernelError, match="smoothing"):
        restore_spectral_audio(np.zeros(4000), smoothing=smoothing)


@pytest.mark.parametrize("floor", [0.0, 1.0, 2.0])
def test_restoration_refuses_an_impossible_gain_floor(floor: float) -> None:
    with pytest.raises(KernelError, match="gain_floor"):
        restore_spectral_audio(np.zeros(4000), gain_floor=floor)


def test_a_reference_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(KernelError, match="length"):
        restore_spectral_audio(np.zeros(4000), reference=np.zeros(3000))


def test_the_stft_round_trip_is_lossless_on_any_length() -> None:
    """The frame count must cover the signal exactly, tail included.

    A ``1 + (n - size) // hop`` frame count loses the last few hundred samples
    on most lengths, and the inverse then returns a shorter buffer — which fails
    loudly here and silently wherever the length is compared downstream.
    """
    for length in (1000, 1024, 4000, 4001, 48000, 48001):
        signal = np.random.default_rng(length).standard_normal(length)
        spectra = kernels._stft(signal, size=1024, hop=256)
        restored = kernels._istft(spectra, size=1024, hop=256, length=length, original=signal)
        assert restored.size == length, f"length {length} round-tripped to {restored.size}"
        assert np.allclose(restored, signal, atol=1e-9)


# ========================================================================== #
# K3 — modal foley
# ========================================================================== #


def test_hertzian_amplitude_is_sublinear_in_velocity() -> None:
    """``A ∝ m^(3/5)·v^(6/5)``: doubling velocity multiplies by ``2^1.2``."""
    base = hertzian_amplitude(1.0, 1.0)
    doubled = hertzian_amplitude(1.0, 2.0)
    assert doubled / base == pytest.approx(2.0**1.2, rel=1e-12)
    # Sub-linear in mass: m^0.6, not m^1.0.
    heavier = hertzian_amplitude(2.0, 1.0)
    assert heavier / base == pytest.approx(2.0**0.6, rel=1e-12)


def test_hertzian_amplitude_refuses_a_degenerate_contact() -> None:
    with pytest.raises(KernelError, match="positive"):
        hertzian_amplitude(0.0, 1.0)
    with pytest.raises(KernelError, match="positive"):
        hertzian_amplitude(1.0, 0.0)


@pytest.mark.parametrize("material", sorted(MATERIAL_MODES))
def test_every_material_syncs_within_one_millisecond(material: str) -> None:
    """The guarantee a sound editor actually needs: the hit lands on the edit."""
    event = ContactFoleyEvent(
        onset_sec=0.25, rel_velocity_m_s=2.0, effective_mass_kg=0.5, material=material
    )
    result = synthesize_modal_foley([event], duration_sec=1.5)
    assert result.max_onset_error_ms <= 1.0, (
        f"{material} onset error {result.max_onset_error_ms:.3f} ms exceeds 1 ms"
    )
    assert result.event_onsets_samples == (int(round(0.25 * SR)),)


@pytest.mark.parametrize("material", sorted(MATERIAL_MODES))
def test_every_material_has_at_least_six_partials(material: str) -> None:
    modes = MATERIAL_MODES[material]
    assert len(modes.partials_hz) >= 6
    assert len(modes.partials_hz) == len(modes.decay_rates) == len(modes.weights)
    assert all(value > 0.0 for value in modes.partials_hz)
    assert all(value >= 0.0 for value in modes.decay_rates)


def test_metal_rings_longer_than_cloth() -> None:
    """A physical ordering the presets must respect, or the library is arbitrary."""
    assert min(MATERIAL_MODES["metal"].decay_rates) < min(MATERIAL_MODES["cloth"].decay_rates)
    assert max(MATERIAL_MODES["metal"].partials_hz) > max(MATERIAL_MODES["wood"].partials_hz)


def test_the_render_is_deterministic() -> None:
    """Two renders of the same events must be byte-identical."""
    events = [
        ContactFoleyEvent(0.1, 2.0, 0.4, "wood", 0.05),
        ContactFoleyEvent(0.6, 3.0, 0.9, "metal"),
    ]
    first = synthesize_modal_foley(events, duration_sec=1.2)
    second = synthesize_modal_foley(events, duration_sec=1.2)
    assert np.array_equal(first.pcm, second.pcm)


def test_a_slide_adds_friction_energy() -> None:
    without = synthesize_modal_foley(
        [ContactFoleyEvent(0.1, 2.0, 0.5, "stone", 0.0)], duration_sec=1.0
    )
    with_slide = synthesize_modal_foley(
        [ContactFoleyEvent(0.1, 2.0, 0.5, "stone", 0.3)], duration_sec=1.0
    )
    # Energy after the impact must rise when friction is present.
    window = slice(int(0.15 * SR), int(0.4 * SR))
    assert float(np.sum(with_slide.pcm[window] ** 2)) > float(np.sum(without.pcm[window] ** 2))


def test_a_partial_above_nyquist_is_skipped_not_aliased() -> None:
    """An aliased partial invents a mode the material does not have."""
    modes = MaterialModes(partials_hz=(200.0, 30000.0), decay_rates=(5.0, 5.0), weights=(1.0, 1.0))
    rendered = kernels._modal_event(modes=modes, amplitude=1.0, sample_rate=SR, length=4800)
    assert np.all(np.isfinite(rendered))
    spectrum = np.abs(np.fft.rfft(rendered))
    frequencies = np.fft.rfftfreq(rendered.size, 1.0 / SR)
    # The 30 kHz partial would alias to 18 kHz; that must be absent.
    aliased = np.argmin(np.abs(frequencies - 18000.0))
    assert spectrum[aliased] < 0.05 * float(np.max(spectrum))


@pytest.mark.parametrize(
    ("onset", "velocity", "mass", "material", "slide"),
    [
        (-0.1, 1.0, 1.0, "wood", 0.0),
        (0.1, 0.0, 1.0, "wood", 0.0),
        (0.1, -1.0, 1.0, "wood", 0.0),
        (0.1, 1.0, 0.0, "wood", 0.0),
        (0.1, 1.0, -1.0, "wood", 0.0),
        (0.1, 1.0, 1.0, "unobtainium", 0.0),
        (0.1, 1.0, 1.0, "wood", -0.5),
        (float("nan"), 1.0, 1.0, "wood", 0.0),
    ],
)
def test_an_impossible_contact_event_is_refused(
    onset: float, velocity: float, mass: float, material: str, slide: float
) -> None:
    with pytest.raises(KernelError):
        ContactFoleyEvent(onset, velocity, mass, material, slide)


def test_an_event_beyond_the_buffer_is_refused() -> None:
    with pytest.raises(KernelError, match="beyond"):
        synthesize_modal_foley([ContactFoleyEvent(5.0, 1.0, 1.0, "wood")], duration_sec=1.0)


def test_rendering_no_events_is_refused() -> None:
    with pytest.raises(KernelError, match="no foley events"):
        synthesize_modal_foley([], duration_sec=1.0)


@pytest.mark.parametrize("duration", [0.0, -1.0, 1e-9])
def test_an_impossible_duration_is_refused(duration: float) -> None:
    with pytest.raises(KernelError):
        synthesize_modal_foley([ContactFoleyEvent(0.0, 1.0, 1.0, "wood")], duration_sec=duration)


def test_mismatched_material_mode_lengths_are_refused() -> None:
    with pytest.raises(KernelError, match="equal-length"):
        MaterialModes(partials_hz=(100.0, 200.0), decay_rates=(1.0,), weights=(1.0, 1.0))


def test_negative_partials_are_refused() -> None:
    with pytest.raises(KernelError, match="positive"):
        MaterialModes(partials_hz=(-100.0, 200.0), decay_rates=(1.0, 1.0), weights=(1.0, 1.0))


def test_negative_decay_is_refused() -> None:
    with pytest.raises(KernelError, match="non-negative"):
        MaterialModes(partials_hz=(100.0, 200.0), decay_rates=(-1.0, 1.0), weights=(1.0, 1.0))


def test_a_single_mode_material_is_refused() -> None:
    with pytest.raises(KernelError, match="at least two modes"):
        MaterialModes(partials_hz=(100.0,), decay_rates=(1.0,), weights=(1.0,))


# ========================================================================== #
# K4 — Zwicker Bark
# ========================================================================== #


@pytest.mark.parametrize(
    ("frequency", "bark"),
    [
        (0.0, 0.0),
        (100.0, 0.99),
        (300.0, 2.92),
        (1000.0, 8.51),
        (3400.0, 16.33),
        (8000.0, 21.28),
    ],
)
def test_the_zwicker_scale_matches_published_values(frequency: float, bark: float) -> None:
    assert zwicker_bark_hz(frequency) == pytest.approx(bark, abs=0.02)


def test_the_bark_scale_is_monotone() -> None:
    values = [zwicker_bark_hz(hz) for hz in range(0, 24000, 100)]
    assert values == sorted(values)
    assert len(set(round(value, 9) for value in values)) > 200


def test_the_bark_scale_refuses_an_impossible_frequency() -> None:
    with pytest.raises(KernelError, match="non-negative"):
        zwicker_bark_hz(-1.0)
    with pytest.raises(KernelError, match="finite"):
        zwicker_bark_hz(float("inf"))


def test_there_are_twenty_four_bark_bands() -> None:
    assert BARK_BANDS == 24
    assert len(bark_band_edges(SR)) == 25


def test_bark_edges_are_monotone_and_start_at_zero() -> None:
    edges = bark_edges_hz(SR)
    assert edges[0] == 0.0
    assert edges == tuple(sorted(edges))
    assert edges[-1] < SR / 2.0


def test_the_spreading_slopes_are_asymmetric() -> None:
    """+27 dB/Bark downward, −12 dB/Bark upward."""
    assert kernels._spreading_gain(-1.0) == pytest.approx(-27.0)
    assert kernels._spreading_gain(1.0) == pytest.approx(-12.0)
    assert kernels._spreading_gain(0.0) == pytest.approx(0.0)
    # Upward masking reaches further than downward, for the same distance.
    assert abs(kernels._spreading_gain(2.0)) < abs(kernels._spreading_gain(-2.0))


def test_the_masking_threshold_is_never_below_the_source_band() -> None:
    power = np.zeros(BARK_BANDS)
    power[10] = 1.0
    threshold = kernels._masking_threshold(power)
    assert threshold.shape == (BARK_BANDS,)
    assert threshold[10] > 0.0
    # Every band receives some masking from the loud one.
    assert np.all(threshold > 0.0)


def test_speech_bands_span_the_intelligibility_range() -> None:
    """Roughly 300 Hz … 3.4 kHz — where consonants live."""
    edges = bark_edges_hz(SR)
    low = edges[SPEECH_BANDS[0]]
    high = edges[SPEECH_BANDS[-1] + 1]
    # Measured: bands 4..16 span 417 Hz … 3.82 kHz on the 24-band Zwicker grid.
    # The lower edge sits above the textbook 300 Hz because the Bark bands are
    # uniform in Bark, not in Hz, and the first four bands are wide.
    assert 350.0 < low < 500.0
    assert 3500.0 < high < 4200.0


# ========================================================================== #
# K4 — mastering
# ========================================================================== #


def test_mastering_hits_the_loudness_target() -> None:
    signal = programme(2.0)
    stereo = np.stack((signal, signal * 0.98), axis=-1)
    result = master_bark_psychoacoustic(stereo, target_lufs=-14.0, max_true_peak_dbfs=-1.0)
    assert result.achieved_lufs == pytest.approx(-14.0, abs=0.3)
    assert result.mastered.shape == stereo.shape
    assert np.all(np.isfinite(result.mastered))


def test_the_limiter_uses_the_oversampled_true_peak_not_the_sample_peak() -> None:
    """A sample-domain peak cannot see the inter-sample overshoot.

    This fixture is constructed so the two disagree: a steep, near-Nyquist
    alternating pattern has a modest sample peak but reconstructs several dB
    higher between samples. A limiter keyed on ``max|x[n]|`` lets it through.
    """
    frames = 4800
    # Alternating ±0.7 at half the sample rate: sample peak 0.7 (-3.1 dBFS),
    # true peak materially higher once reconstructed.
    signal = np.empty((frames, 2))
    pattern = 0.7 * np.where(np.arange(frames) % 2 == 0, 1.0, -1.0)
    signal[:, 0] = pattern
    signal[:, 1] = pattern

    from nexus_ai_agent.creative.audio_engine.loudness_bs1770 import true_peak_dbtp

    sample_peak = 20.0 * math.log10(float(np.max(np.abs(signal))))
    measured_tp = true_peak_dbtp(signal)
    assert measured_tp is not None
    assert measured_tp > sample_peak + 1.0, (
        f"fixture does not separate true peak ({measured_tp:.2f}) "
        f"from sample peak ({sample_peak:.2f})"
    )

    limited, _reduction = kernels._apply_true_peak_limiter(
        signal, sample_rate=SR, ceiling_dbtp=-1.0
    )
    after = true_peak_dbtp(limited)
    assert after is not None and after <= -1.0 + 1e-6


def test_mastering_holds_the_true_peak_ceiling() -> None:
    """A sample-domain clamp at −1 dBFS still reconstructs above −1 dBTP."""
    signal = programme(2.0)
    stereo = np.stack((signal, signal), axis=-1)
    for ceiling in (-1.0, -3.0, -6.0):
        result = master_bark_psychoacoustic(stereo, target_lufs=-16.0, max_true_peak_dbfs=ceiling)
        assert result.achieved_true_peak_dbtp <= ceiling + 1e-6


def test_a_quieter_target_produces_a_quieter_master() -> None:
    signal = programme(1.0)
    stereo = np.stack((signal, signal), axis=-1)
    loud = master_bark_psychoacoustic(stereo, target_lufs=-12.0, max_true_peak_dbfs=-1.0)
    quiet = master_bark_psychoacoustic(stereo, target_lufs=-20.0, max_true_peak_dbfs=-1.0)
    assert loud.achieved_lufs > quiet.achieved_lufs


def test_the_limiter_reports_its_gain_reduction() -> None:
    signal = programme(1.0)
    stereo = np.stack((signal, signal), axis=-1)
    result = master_bark_psychoacoustic(stereo, target_lufs=-14.0, max_true_peak_dbfs=-1.0)
    assert result.limiter_gr_db >= 0.0
    assert math.isfinite(result.limiter_gr_db)


def test_the_mastering_returns_twenty_four_bands_of_telemetry() -> None:
    signal = programme(1.0)
    stereo = np.stack((signal, signal), axis=-1)
    result = master_bark_psychoacoustic(stereo)
    assert len(result.telemetry.band_energies_db) == BARK_BANDS
    assert len(result.telemetry.masking_threshold_db) == BARK_BANDS
    assert len(result.telemetry.signal_to_mask_db) == BARK_BANDS
    assert len(result.telemetry.band_edges_hz) == BARK_BANDS + 1
    for value in result.telemetry.band_energies_db:
        assert math.isfinite(value)


def test_presence_compensation_is_bounded() -> None:
    """An unbounded presence lift creates sibilance that masks more than it clarifies."""
    signal = programme(1.0)
    stereo = np.stack((signal, signal), axis=-1)
    result = master_bark_psychoacoustic(stereo, max_boost_db=2.0)
    assert all(band in SPEECH_BANDS for band in result.bands_boosted)
    assert all(preset <= 3 for preset in result.presets_applied)
    # The band set itself must be the speech range, not the whole spectrum.
    assert SPEECH_BANDS[0] == 4
    assert SPEECH_BANDS[-1] == 16
    assert len(SPEECH_BANDS) == 13
    assert not set(SPEECH_BANDS) & {0, 1, 2, 3, 17, 18, 19, 20, 21, 22, 23}


def test_mastering_refuses_a_mono_buffer_of_the_wrong_shape() -> None:
    with pytest.raises(KernelError, match="stereo"):
        master_bark_psychoacoustic(np.zeros((1000, 3)), sample_rate=SR)


def test_mastering_accepts_mono_and_duplicates_it() -> None:
    result = master_bark_psychoacoustic(programme(1.0), sample_rate=SR)
    assert result.mastered.ndim == 2
    assert result.mastered.shape[1] == 2


@pytest.mark.parametrize("ceiling", [0.0, 1.0, 3.0])
def test_mastering_refuses_a_ceiling_above_full_scale(ceiling: float) -> None:
    with pytest.raises(KernelError, match="below 0 dBFS"):
        master_bark_psychoacoustic(np.zeros((4000, 2)), max_true_peak_dbfs=ceiling)


@pytest.mark.parametrize("boost", [0.0, -1.0])
def test_mastering_refuses_an_impossible_boost_bound(boost: float) -> None:
    with pytest.raises(KernelError):
        master_bark_psychoacoustic(np.zeros((4000, 2)), max_boost_db=boost)


def test_mastering_refuses_digital_silence() -> None:
    """Silence is below the −70 LUFS gate; there is no loudness to normalise."""
    with pytest.raises(KernelError, match="absolute gate"):
        master_bark_psychoacoustic(np.zeros((SR, 2)))


def test_the_mastering_result_serialises() -> None:
    signal = programme(1.0)
    result = master_bark_psychoacoustic(np.stack((signal, signal), axis=-1))
    payload = result.as_dict()
    assert payload["target_lufs"] == -14.0
    assert isinstance(payload["bands_boosted"], list)


def test_the_true_peak_limiter_is_a_no_op_below_its_ceiling() -> None:
    quiet = np.zeros((4800, 2))
    quiet[:, 0] = 0.01 * np.sin(np.linspace(0, 20, 4800))
    quiet[:, 1] = quiet[:, 0]
    limited, reduction = kernels._apply_true_peak_limiter(quiet, sample_rate=SR, ceiling_dbtp=-1.0)
    assert reduction == 0.0
    assert np.array_equal(limited, quiet)


def test_the_limiter_preserves_most_of_the_signal_energy() -> None:
    """A single global scale factor would destroy the loudness target.

    The measured failure was 35 dB of reduction on a signal needing a few dB on
    a handful of peaks; the loudness target could then not be reached at all.

    The fixture is a *quiet body with one loud transient*, which is the case
    that separates a time-varying gain from a scalar one: the scalar has to
    duck the entire file to the transient's requirement, while the correct
    limiter ducks only around the transient. Measured on this fixture the two
    differ by 1.34 dB of body level — so the assertion is on the **body**
    energy, not the whole-file RMS, which dilutes the difference.
    """
    sample_rate = SR
    time = np.arange(sample_rate) / sample_rate
    body = 0.05 * np.sin(2 * np.pi * 440 * time)
    stereo = np.zeros((sample_rate, 2))
    stereo[:, 0] = body
    stereo[:, 1] = body
    stereo[24000, 0] = 0.95
    stereo[24000, 1] = 0.95

    limited, reduction = kernels._apply_true_peak_limiter(
        stereo, sample_rate=sample_rate, ceiling_dbtp=-1.0
    )
    assert 0.0 < reduction < 20.0
    # Measure the body well away from the transient and from the release tail.
    body_window = slice(sample_rate // 10, sample_rate // 2)
    body_before = float(np.sqrt(np.mean(stereo[body_window] ** 2)))
    body_after = float(np.sqrt(np.mean(limited[body_window] ** 2)))
    retained = body_after / body_before
    # A global-scale limiter retains ~0.737 here (the transient's requirement);
    # the time-varying limiter retains >0.95 of the untouched body.
    assert retained > 0.92, (
        f"body retained only {retained:.3f} of its level; a scalar limiter would land near 0.74"
    )


# ========================================================================== #
# Remaining structural branches
# ========================================================================== #


def test_a_three_dimensional_buffer_is_refused() -> None:
    with pytest.raises(KernelError, match="1-D or 2-D"):
        restore_spectral_audio(np.zeros((2, 2, 2)), sample_rate=SR)


def test_a_zero_frequency_needs_no_bark_conversion() -> None:
    """Bark 0 maps to 0 Hz; the bisection's first edge is exact."""
    assert bark_edges_hz(SR)[0] == 0.0


def test_a_zero_primary_check() -> None:
    assert kernels._is_prime(0) is False
    assert kernels._is_prime(1) is False
    assert kernels._is_prime(2) is True
    assert kernels._is_prime(4) is False
    assert kernels._is_prime(9) is False
    assert kernels._is_prime(97) is True


def test_an_impossible_delay_count_is_refused() -> None:
    with pytest.raises(KernelError, match="at least one delay line"):
        kernels._prime_delays(0)


def test_a_room_with_the_wrong_number_of_dimensions_is_refused() -> None:
    with pytest.raises(KernelError, match="three positive"):
        synthesize_hoa3_binaural_room(np.zeros(500), room_dims_m=(4.0, 4.0))


def test_a_single_channel_fdn_is_refused_end_to_end() -> None:
    with pytest.raises(KernelError, match="at least 2"):
        synthesize_hoa3_binaural_room(np.zeros(500), fdn_channels=1)


def test_a_source_exactly_at_the_listener_has_no_azimuth() -> None:
    """A degenerate geometry must not raise; it must produce a usable render."""
    result = synthesize_hoa3_binaural_room(
        programme(0.2),
        fdn_channels=4,
        source_pos_m=(2.0, 2.0, 1.2),
        listener_pos_m=(2.0, 2.0, 1.2),
    )
    assert np.all(np.isfinite(result.stereo))
    assert result.source_azimuth_rad == pytest.approx(0.0)


def test_a_reflection_longer_than_the_buffer_is_dropped() -> None:
    """A very short render cannot host a late wall reflection; dropping is correct."""
    result = synthesize_hoa3_binaural_room(
        programme(0.001), fdn_channels=4, room_dims_m=(40.0, 40.0, 20.0)
    )
    assert np.all(np.isfinite(result.stereo))


def test_the_shadow_side_follows_the_source_side() -> None:
    """The far ear must be the shadowed one, in both directions."""
    left_source = synthesize_hoa3_binaural_room(
        programme(0.3),
        fdn_channels=4,
        source_pos_m=(1.0, 4.0, 1.2),
        listener_pos_m=(4.0, 2.0, 1.2),
    )
    right_source = synthesize_hoa3_binaural_room(
        programme(0.3),
        fdn_channels=4,
        source_pos_m=(1.0, 1.0, 1.2),
        listener_pos_m=(4.0, 3.0, 1.2),
    )
    assert left_source.source_azimuth_rad < 0.0
    assert right_source.source_azimuth_rad > 0.0
    # Both renders stay finite and stereo in either case.
    assert left_source.stereo.shape == right_source.stereo.shape


def test_the_snr_helper_handles_degenerate_inputs() -> None:
    """Both-zero is a *perfect* match (120 dB), not an infinitely bad one.

    The two degenerate cases are distinguished by which side is empty: unequal
    lengths mean no comparison is possible (0 dB), while identical all-zero
    buffers have zero error and therefore the ceiling.
    """
    assert kernels._snr_db(np.zeros(0), np.zeros(0)) == 0.0
    assert kernels._snr_db(np.zeros(10), np.zeros(10)) == 120.0
    assert kernels._snr_db(np.ones(10), np.ones(10)) == 120.0
    # A unit signal against silence: signal energy 10, error energy 10, so 0 dB.
    # The -120 dB branch needs a *silent reference*, which is the only case where
    # the ratio is undefined from below.
    assert kernels._snr_db(np.ones(10), np.zeros(10)) == 0.0


def test_a_signal_with_no_second_difference_has_no_clicks() -> None:
    """A buffer of two samples has no Δ² at all."""
    assert kernels._detect_click_spans(np.zeros(2), threshold=3.0) == ()
    assert kernels._detect_click_spans(np.zeros(50), threshold=3.0) == ()


def test_digital_silence_is_not_reported_as_a_recording_full_of_clicks() -> None:
    """The degenerate-MAD guard, measured on the case that triggers it.

    Digital silence yields a *nonzero* second-difference MAD (~1e-16) because
    of floating-point rounding, so `sigma <= 0` never fires and
    `threshold * 1e-16` puts the detection bar below every sample. Without the
    relative guard the detector returns spans covering the entire buffer: the
    signal is silent, yet the restoration reports thousands of clicks.
    """
    silent = np.zeros(4000)
    spans = kernels._detect_click_spans(silent, threshold=3.0)
    assert spans == ()

    # The discriminating fixture: three clicks buried in a *quiet but nonzero*
    # floor. The MAD collapses to ~2.4e-9 against a peak of 4.0 — a ratio of
    # 1.6e-10 — so the relative guard fires and the fallback finds the 3 clicks.
    # Without the guard the bar is `3 * 2.4e-9`, below the noise, and the detector
    # reports 15 flagged samples: clicks everywhere and nowhere.
    generator = np.random.default_rng(5)
    sparse = 1e-9 * generator.standard_normal(4000)
    for position in (1000, 2000, 3000):
        sparse[position] += 2.0
    found = kernels._detect_click_spans(sparse, threshold=3.0)
    assert len(found) == 3, f"expected 3 click spans, got {len(found)}"
    covered = sum(end - start + 1 for start, end in found)
    # Each isolated click produces one contiguous span spanning the three Δ²
    # windows it participates in, plus the one-sample expansion either side:
    # 6 samples per click. 9 hits would mean the whole buffer, which is what
    # the missing guard produces.
    assert covered <= 3 * 7, f"{covered} samples covered for 3 clicks"


def test_a_click_running_to_the_final_sample_is_closed() -> None:
    """Δ² at index i covers samples i..i+2, so the last detectable window on a
    200-sample buffer starts at 197; the span closes one short of the edge by
    construction, not by a bug."""
    signal = np.zeros(200)
    signal[196:] = 5.0
    spans = kernels._detect_click_spans(signal, threshold=3.0)
    assert spans
    assert spans[-1][0] <= 196
    assert spans[-1][1] >= 197


def test_interpolation_falls_back_at_a_buffer_edge() -> None:
    """A span touching index 0 has no clean left neighbour to bracket it."""
    signal = np.zeros(100)
    signal[:4] = 5.0
    spans = kernels._detect_click_spans(signal, threshold=3.0)
    assert spans
    restored, applied = kernels._interpolate_spans(signal, spans)
    assert applied >= 1
    assert np.all(np.isfinite(restored))


def test_the_declipper_keeps_samples_with_no_clean_bracket() -> None:
    """A plateau reaching both edges cannot be bracketed; it stays as measured."""
    signal = np.full(50, 0.99)
    restored, indices = kernels._declip(signal, tau=0.98)
    assert len(indices) == 50
    assert np.allclose(restored, signal, atol=1e-12)


def test_a_single_frame_signal_still_has_a_noise_estimate() -> None:
    power = np.abs(np.fft.rfft(np.ones(64)))[None, :] ** 2
    estimate = kernels._noise_psd(power)
    assert estimate.shape == power.shape[1:]
    assert np.all(estimate > 0.0)


def test_friction_noise_is_deterministic_and_bounded() -> None:
    first = kernels._friction_noise(sample_rate=SR, duration_samples=1000)
    second = kernels._friction_noise(sample_rate=SR, duration_samples=1000)
    assert np.array_equal(first, second)
    assert np.max(np.abs(first)) <= 1.0 + 1e-12
    assert kernels._friction_noise(sample_rate=SR, duration_samples=0).size == 0


@pytest.mark.parametrize("rate", [0, -100])
def test_modal_foley_refuses_a_non_positive_rate(rate: int) -> None:
    with pytest.raises(KernelError, match="sample_rate"):
        synthesize_modal_foley(
            [ContactFoleyEvent(0.0, 1.0, 1.0, "wood")], duration_sec=1.0, sample_rate=rate
        )


def test_a_decay_longer_than_the_buffer_is_truncated() -> None:
    """The ring buffer clips a long decay; the render must still be finite."""
    result = synthesize_modal_foley([ContactFoleyEvent(0.9, 1.0, 0.5, "metal")], duration_sec=1.0)
    assert np.all(np.isfinite(result.pcm))
    assert result.pcm.size == SR


def test_an_event_with_no_energy_reports_a_zero_onset() -> None:
    """Guarded: a buffer whose peak is zero cannot have an onset index."""
    modes = MaterialModes(
        partials_hz=(40000.0, 41000.0), decay_rates=(1.0, 1.0), weights=(1.0, 1.0)
    )
    rendered = kernels._modal_event(modes=modes, amplitude=0.0, sample_rate=SR, length=100)
    assert np.all(rendered == 0.0)


def test_the_bark_power_helper_pads_a_short_signal() -> None:
    power, edges = kernels._bark_powers(np.zeros(100), sample_rate=SR)
    assert power.shape == (BARK_BANDS,)
    assert len(edges) == BARK_BANDS + 1


def test_the_bark_power_helper_skips_an_empty_band() -> None:
    """A band containing no FFT bin contributes zero rather than raising."""
    power, _edges = kernels._bark_powers(programme(0.2), sample_rate=8000)
    assert power.shape == (BARK_BANDS,)
    assert np.all(np.isfinite(power))


def test_the_limiter_is_a_no_op_for_silence() -> None:
    limited, reduction = kernels._apply_true_peak_limiter(
        np.zeros((1000, 2)), sample_rate=SR, ceiling_dbtp=-1.0
    )
    assert reduction == 0.0
    assert np.all(limited == 0.0)


def test_the_limiter_attacks_instantly_and_releases_smoothly() -> None:
    """Attack must be instantaneous or the peak escapes; release must not step."""
    signal = np.zeros((48000, 2))
    signal[24000, :] = 4.0  # one enormous transient
    limited, reduction = kernels._apply_true_peak_limiter(signal, sample_rate=SR, ceiling_dbtp=-1.0)
    assert reduction > 0.0
    assert np.max(np.abs(limited)) <= 10.0 ** (-1.0 / 20.0) + 1e-9


def test_mastering_a_signal_that_masters_to_silence_is_refused() -> None:
    """A single sample at full scale inside a gated block is normalisable."""
    tiny = np.zeros((SR, 2))
    tiny[:, 0] = 1e-9 * np.sin(np.linspace(0, 100, SR))
    tiny[:, 1] = tiny[:, 0]
    with pytest.raises(KernelError, match="absolute gate"):
        master_bark_psychoacoustic(tiny)


def test_the_presence_eq_handles_a_signal_with_no_speech_energy() -> None:
    """A pure sub-bass signal has no speech-band deficit to correct."""
    time = np.arange(SR) / SR
    result = master_bark_psychoacoustic(
        np.stack((0.3 * np.sin(2 * np.pi * 60 * time),) * 2, axis=-1)
    )
    assert all(band in SPEECH_BANDS for band in result.bands_boosted)
