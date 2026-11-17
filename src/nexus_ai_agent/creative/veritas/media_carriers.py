"""Explicit raster carriers, not face detection or inferred human articulation."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


def audio_formant_envelopes(
    pcm: FloatArray, count: int, fps: float, sample_rate: int
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Per-frame RMS and deterministic spectral-band peaks on the sample clock."""
    energy, f1, f2 = np.zeros(count), np.full(count, 300.0), np.full(count, 1200.0)
    for index in range(count):
        start = min(int(index * sample_rate / fps), pcm.size - 1)
        end = min(max(start + 2, int((index + 1) * sample_rate / fps)), pcm.size)
        segment = pcm[start:end]
        energy[index] = np.sqrt(np.mean(segment**2))
        spectrum = np.abs(np.fft.rfft(segment))
        frequencies = np.fft.rfftfreq(segment.size, 1 / sample_rate)
        for lower, upper, destination in ((300, 1000, f1), (1000, 2500, f2)):
            band = np.flatnonzero((frequencies >= lower) & (frequencies <= upper))
            if band.size:
                destination[index] = frequencies[band[np.argmax(spectrum[band])]]
    return energy, f1, f2


def render_disjoint_viseme_badge(
    frames: FloatArray, pcm: FloatArray, fps: float, sample_rate: int
) -> FloatArray:
    """Append an eight-row achromatic carrier OUTSIDE the mechanical image.

    Three panels encode vertical jaw aperture, horizontal lip spread and vertical
    rounding. Complementary lower halves keep the badge's radiance sum constant.
    Opening/rounding round upward to respect articulation minima; spread rounds
    downward to respect its upper bound. This is a synthetic diagnostic badge,
    never a claim to have detected or reconstructed a real human mouth.
    """
    count, _, width, _ = frames.shape
    if width < 6:
        raise ValueError("viseme badge requires width >= 6")
    energy, f1, f2 = audio_formant_envelopes(pcm, count, fps, sample_rate)
    badge = np.zeros((count, 8, width, 3))
    boundaries = (0, width // 3, 2 * width // 3, width)
    for index in range(count):
        silent = energy[index] < 1e-4
        jaw = 0.0 if silent else float(np.clip(0.8 * (f1[index] - 300) / 700, 0, 1))
        spread = 0.0 if silent else float(np.clip((f1[index] - 300) / 1000, 0, 0.7))
        rounding = 0.0 if silent else float(np.clip(0.6 * (1200 - f2[index]) / 500, 0, 1))
        for panel, amount in enumerate((jaw, spread, rounding)):
            left, right = boundaries[panel : panel + 2]
            if panel == 1:
                columns = int(np.floor(amount * (right - left)))
                badge[index, :4, left : left + columns] = 1
            else:
                rows = int(np.ceil(amount * 4))
                badge[index, :rows, left:right] = 1
        badge[index, 4:] = 1 - badge[index, :4]
    return np.concatenate([frames, badge], axis=1)


def extract_disjoint_viseme_badge(
    frames: FloatArray, pcm: FloatArray, fps: float, sample_rate: int
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray, FloatArray, FloatArray]:
    """Measure the actual badge and independently bind it to the decoded WAV."""
    count, height, width, _ = frames.shape
    if height < 10 or width < 6:
        raise ValueError("invalid disjoint viseme carrier dimensions")
    badge = frames[:, -8:]
    if (
        not np.all((badge == 0) | (badge == 1))
        or not np.array_equal(badge[..., 0], badge[..., 1])
        or not np.array_equal(badge[..., 1], badge[..., 2])
        or not np.array_equal(badge[:, :4] + badge[:, 4:], np.ones_like(badge[:, :4]))
    ):
        raise ValueError("viseme badge raster invariant violated")
    expected = render_disjoint_viseme_badge(frames[:, :-8], pcm, fps, sample_rate)[:, -8:]
    if not np.array_equal(badge, expected):
        raise ValueError("viseme badge/audio formant synchronization mismatch")
    boundaries = (0, width // 3, 2 * width // 3, width)
    apertures = [
        np.mean(badge[:, :4, boundaries[i] : boundaries[i + 1], 0], axis=(1, 2)) for i in range(3)
    ]
    energy, f1, f2 = audio_formant_envelopes(pcm, count, fps, sample_rate)
    return apertures[0], apertures[1], f1, energy, apertures[2], f2
