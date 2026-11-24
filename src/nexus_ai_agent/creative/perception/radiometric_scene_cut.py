"""Scene-referred radiometry and Jensen-Shannon cut detection.

Frames are decoded through the IEC 61966-2-1 inverse EOTF into physical linear
luminance before any statistic is taken.  Adjacent frames are compared with the
Jensen-Shannon divergence, whose square root is a true metric, and the detector
rejects single-frame camera flashes instead of calling them cuts.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

__all__ = [
    "CutConfig",
    "CutDetectionError",
    "FrameTransition",
    "SceneCutDetector",
    "SceneFrame",
    "TransitionKind",
    "analyze_sequence",
    "edge_gradient_flux",
    "jensen_shannon_divergence",
    "js_distance",
    "linear_luminance",
    "luminance_histogram",
    "shannon_entropy",
    "srgb_to_linear",
]

_LUMA_R: Final[float] = 0.2126
_LUMA_G: Final[float] = 0.7152
_LUMA_B: Final[float] = 0.0722


class CutDetectionError(ValueError):
    """Raised when frame data violates a radiometric precondition."""


class TransitionKind(Enum):
    HARD_CUT = "HARD_CUT"
    DISSOLVE = "DISSOLVE"
    FLASH_TRANSIENT = "FLASH_TRANSIENT"
    STATIC_HOLD = "STATIC_HOLD"


def _unit(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CutDetectionError(f"{label} must be a real number")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise CutDetectionError(f"{label} must be finite and inside [0, 1]")
    return result


def srgb_to_linear(channel: float) -> float:
    """IEC 61966-2-1 inverse EOTF for one gamma-encoded channel."""

    value = _unit(channel, "sRGB channel")
    if value <= 0.04045:
        return value / 12.92
    return math.pow((value + 0.055) / 1.055, 2.4)


def linear_luminance(rgb: tuple[float, float, float]) -> float:
    """Rec.709 scene luminance from gamma-encoded sRGB."""

    if len(rgb) != 3:
        raise CutDetectionError("rgb must have three components")
    red, green, blue = (srgb_to_linear(component) for component in rgb)
    luminance = _LUMA_R * red + _LUMA_G * green + _LUMA_B * blue
    return max(0.0, min(1.0, luminance))


def luminance_histogram(
    pixels: Sequence[tuple[float, float, float]], bins: int = 32
) -> tuple[float, ...]:
    """Normalised histogram of scene-referred luminance."""

    if not pixels:
        raise CutDetectionError("frame has no pixels")
    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 2:
        raise CutDetectionError("bins must be an int >= 2")
    counts = [0.0] * bins
    for pixel in pixels:
        if len(pixel) != 3:
            raise CutDetectionError("each pixel must be an RGB triple")
        luminance = linear_luminance((pixel[0], pixel[1], pixel[2]))
        index = min(bins - 1, int(luminance * bins))
        counts[index] += 1.0
    total = float(len(pixels))
    return tuple(count / total for count in counts)


def _normalise(distribution: Sequence[float]) -> tuple[float, ...]:
    if not distribution:
        raise CutDetectionError("distribution must not be empty")
    values = []
    for probability in distribution:
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise CutDetectionError("probabilities must be real numbers")
        value = float(probability)
        if not math.isfinite(value) or value < 0.0:
            raise CutDetectionError("probabilities must be finite and non-negative")
        values.append(value)
    total = sum(values)
    if total <= 0.0:
        raise CutDetectionError("distribution must have positive mass")
    return tuple(value / total for value in values)


def shannon_entropy(distribution: Sequence[float]) -> float:
    """Shannon entropy in bits."""

    values = _normalise(distribution)
    return -sum(value * math.log2(value) for value in values if value > 0.0)


def jensen_shannon_divergence(first: Sequence[float], second: Sequence[float]) -> float:
    """Jensen-Shannon divergence in bits (base-2 logarithm)."""

    left = _normalise(first)
    right = _normalise(second)
    if len(left) != len(right):
        raise CutDetectionError("distributions must have the same length")
    midpoint = tuple(0.5 * (a + b) for a, b in zip(left, right, strict=True))

    def _kl(source: tuple[float, ...]) -> float:
        return sum(
            value * math.log2(value / reference)
            for value, reference in zip(source, midpoint, strict=True)
            if value > 0.0
        )

    return 0.5 * _kl(left) + 0.5 * _kl(right)


def js_distance(first: Sequence[float], second: Sequence[float]) -> float:
    """Square root of the JS divergence: a bounded metric in [0, 1]."""

    return math.sqrt(max(0.0, jensen_shannon_divergence(first, second)))


def _validate_grid(grid: Sequence[Sequence[float]], label: str) -> tuple[tuple[float, ...], ...]:
    if not grid or not grid[0]:
        raise CutDetectionError(f"{label} grid must be non-empty")
    width = len(grid[0])
    rows = []
    for row in grid:
        if len(row) != width:
            raise CutDetectionError("grid rows must have equal length")
        values = []
        for value in row:
            number = float(value)
            if not math.isfinite(number):
                raise CutDetectionError("grid values must be finite")
            values.append(number)
        rows.append(tuple(values))
    return tuple(rows)


def edge_gradient_flux(
    previous: Sequence[Sequence[float]], current: Sequence[Sequence[float]]
) -> float:
    """Mean absolute change of horizontal and vertical gradients."""

    left = _validate_grid(previous, "previous")
    right = _validate_grid(current, "current")
    if len(left) != len(right) or len(left[0]) != len(right[0]):
        raise CutDetectionError("gradient grids must share a shape")
    if len(left) < 2 or len(left[0]) < 2:
        return 0.0
    total = 0.0
    count = 0
    for row in range(len(left)):
        for column in range(len(left[0])):
            total += abs(left[row][column] - right[row][column])
            count += 1
            if column + 1 < len(left[0]):
                total += abs(
                    (left[row][column + 1] - left[row][column])
                    - (right[row][column + 1] - right[row][column])
                )
                count += 1
            if row + 1 < len(left):
                total += abs(
                    (left[row + 1][column] - left[row][column])
                    - (right[row + 1][column] - right[row][column])
                )
                count += 1
    return total / count


@dataclass(frozen=True, slots=True)
class SceneFrame:
    """A frame reduced to its scene-referred luminance statistics."""

    histogram: tuple[float, ...]
    entropy: float
    luminance_grid: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        if len(self.histogram) < 2:
            raise CutDetectionError("histogram needs at least two bins")
        _normalise(self.histogram)
        if not math.isfinite(self.entropy) or self.entropy < 0.0:
            raise CutDetectionError("entropy must be finite and non-negative")
        _validate_grid(self.luminance_grid, "luminance")

    @classmethod
    def from_rgb_grid(
        cls, grid: Sequence[Sequence[tuple[float, float, float]]], bins: int = 32
    ) -> SceneFrame:
        if not grid or not grid[0]:
            raise CutDetectionError("frame grid must be non-empty")
        width = len(grid[0])
        pixels: list[tuple[float, float, float]] = []
        luminance_rows: list[tuple[float, ...]] = []
        for row in grid:
            if len(row) != width:
                raise CutDetectionError("frame rows must have equal length")
            luminance_row = []
            for pixel in row:
                if len(pixel) != 3:
                    raise CutDetectionError("each pixel must be an RGB triple")
                pixels.append((pixel[0], pixel[1], pixel[2]))
                luminance_row.append(linear_luminance((pixel[0], pixel[1], pixel[2])))
            luminance_rows.append(tuple(luminance_row))
        histogram = luminance_histogram(pixels, bins)
        return cls(
            histogram=histogram,
            entropy=shannon_entropy(histogram),
            luminance_grid=tuple(luminance_rows),
        )


@dataclass(frozen=True, slots=True)
class CutConfig:
    hard_cut_distance: float = 0.18
    dissolve_distance: float = 0.02
    static_distance: float = 0.005
    flash_return_ratio: float = 0.5
    static_flux: float = 0.01

    def __post_init__(self) -> None:
        for name in (
            "hard_cut_distance",
            "dissolve_distance",
            "static_distance",
            "flash_return_ratio",
            "static_flux",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise CutDetectionError(f"{name} must be finite and non-negative")
        if self.static_distance > self.dissolve_distance:
            raise CutDetectionError("static_distance must not exceed dissolve_distance")
        if self.dissolve_distance > self.hard_cut_distance:
            raise CutDetectionError("dissolve_distance must not exceed hard_cut_distance")


@dataclass(frozen=True, slots=True)
class FrameTransition:
    kind: TransitionKind
    js_divergence: float
    js_distance: float
    gradient_flux: float
    entropy_before: float
    entropy_after: float

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TransitionKind):
            raise CutDetectionError("kind must be a TransitionKind")
        for name in (
            "js_divergence",
            "js_distance",
            "gradient_flux",
            "entropy_before",
            "entropy_after",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0.0:
                raise CutDetectionError(f"{name} must be finite and non-negative")


def classify_transition(
    before: SceneFrame, current: SceneFrame, after: SceneFrame | None, config: CutConfig
) -> FrameTransition:
    """Classify the transition into ``current``, using ``after`` to reject flashes."""

    divergence = jensen_shannon_divergence(before.histogram, current.histogram)
    distance = math.sqrt(max(0.0, divergence))
    flux = edge_gradient_flux(before.luminance_grid, current.luminance_grid)
    kind: TransitionKind
    if after is not None:
        forward = js_distance(current.histogram, after.histogram)
        skip = js_distance(before.histogram, after.histogram)
        if (
            distance >= config.hard_cut_distance
            and forward >= config.hard_cut_distance
            and skip < config.flash_return_ratio * distance
        ):
            kind = TransitionKind.FLASH_TRANSIENT
        elif distance >= config.hard_cut_distance:
            kind = TransitionKind.HARD_CUT
        elif distance >= config.dissolve_distance:
            kind = TransitionKind.DISSOLVE
        elif distance <= config.static_distance and flux <= config.static_flux:
            kind = TransitionKind.STATIC_HOLD
        else:
            kind = TransitionKind.DISSOLVE
    elif distance >= config.hard_cut_distance:
        kind = TransitionKind.HARD_CUT
    elif distance >= config.dissolve_distance:
        kind = TransitionKind.DISSOLVE
    elif distance <= config.static_distance and flux <= config.static_flux:
        kind = TransitionKind.STATIC_HOLD
    else:
        kind = TransitionKind.DISSOLVE
    return FrameTransition(
        kind=kind,
        js_divergence=divergence,
        js_distance=distance,
        gradient_flux=flux,
        entropy_before=before.entropy,
        entropy_after=current.entropy,
    )


def analyze_sequence(
    frames: Sequence[SceneFrame], config: CutConfig | None = None
) -> tuple[FrameTransition, ...]:
    """Classify every adjacent pair; ``after`` is supplied whenever it exists."""

    if len(frames) < 2:
        return ()
    settings = config if config is not None else CutConfig()
    transitions = []
    for index in range(1, len(frames)):
        after = frames[index + 1] if index + 1 < len(frames) else None
        transitions.append(classify_transition(frames[index - 1], frames[index], after, settings))
    return tuple(transitions)


class SceneCutDetector:
    """Streaming detector; keeps one frame of lookahead for flash rejection."""

    def __init__(self, config: CutConfig | None = None) -> None:
        self._config = config if config is not None else CutConfig()
        self._frames: list[SceneFrame] = []
        self._transitions: list[FrameTransition] = []

    def push(self, frame: SceneFrame) -> FrameTransition | None:
        if not isinstance(frame, SceneFrame):
            raise CutDetectionError("frame must be a SceneFrame")
        self._frames.append(frame)
        if len(self._frames) < 2:
            return None
        before, current = self._frames[-2], self._frames[-1]
        transition = classify_transition(before, current, None, self._config)
        self._transitions.append(transition)
        return transition

    def finalize(self) -> tuple[FrameTransition, ...]:
        """Re-classify with full lookahead so the last real frame participates."""

        self._transitions = list(analyze_sequence(self._frames, self._config))
        return tuple(self._transitions)

    @property
    def transitions(self) -> tuple[FrameTransition, ...]:
        return tuple(self._transitions)
