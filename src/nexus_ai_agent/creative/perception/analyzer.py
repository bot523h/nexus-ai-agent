"""Composition of the perception primitives into one immutable report."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from nexus_ai_agent.creative.perception.psychoacoustics_bs1770 import (
    bark_spectrum_centroid,
    integrated_loudness,
    loudness_range,
    momentary_loudness,
    short_term_loudness,
    silence_segments,
    true_peak_dbtp,
)
from nexus_ai_agent.creative.perception.radiometric_scene_cut import (
    CutConfig,
    FrameTransition,
    SceneFrame,
    TransitionKind,
    analyze_sequence,
)

__all__ = [
    "AudioPerception",
    "PerceptionAnalyzer",
    "PerceptionReport",
    "ScenePerception",
]


def _loudness_value(value: float, label: str) -> None:
    if math.isnan(value) or value == math.inf:
        raise ValueError(f"{label} must be finite or -infinity")


@dataclass(frozen=True, slots=True)
class AudioPerception:
    integrated_lkfs: float
    momentary_lkfs: tuple[float, ...]
    short_term_lkfs: tuple[float, ...]
    loudness_range_lu: float
    true_peak_dbtp: float
    bark_centroid: float
    silences: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        momentary = tuple(float(value) for value in self.momentary_lkfs)
        short_term = tuple(float(value) for value in self.short_term_lkfs)
        silences = tuple((float(start), float(end)) for start, end in self.silences)
        _loudness_value(self.integrated_lkfs, "integrated_lkfs")
        _loudness_value(self.true_peak_dbtp, "true_peak_dbtp")
        for value in (*momentary, *short_term):
            _loudness_value(value, "block loudness")
        if not math.isfinite(self.loudness_range_lu) or self.loudness_range_lu < 0.0:
            raise ValueError("loudness_range_lu must be finite and non-negative")
        if not math.isfinite(self.bark_centroid) or self.bark_centroid < 0.0:
            raise ValueError("bark_centroid must be finite and non-negative")
        for start, end in silences:
            if not math.isfinite(start) or not math.isfinite(end) or start < 0.0 or end < start:
                raise ValueError("silence intervals must be ordered and finite")
        object.__setattr__(self, "momentary_lkfs", momentary)
        object.__setattr__(self, "short_term_lkfs", short_term)
        object.__setattr__(self, "silences", silences)


@dataclass(frozen=True, slots=True)
class ScenePerception:
    transitions: tuple[FrameTransition, ...]
    hard_cuts: int
    dissolves: int
    flashes: int
    static_holds: int
    mean_entropy: float

    def __post_init__(self) -> None:
        transitions = tuple(self.transitions)
        counts = (
            self.hard_cuts,
            self.dissolves,
            self.flashes,
            self.static_holds,
        )
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts
        ):
            raise ValueError("transition counts must be non-negative ints")
        if sum(counts) != len(transitions):
            raise ValueError("transition counts must partition the transition tuple")
        if not math.isfinite(self.mean_entropy) or self.mean_entropy < 0.0:
            raise ValueError("mean_entropy must be finite and non-negative")
        object.__setattr__(self, "transitions", transitions)

    @property
    def total_cuts(self) -> int:
        return self.hard_cuts + self.flashes


@dataclass(frozen=True, slots=True)
class PerceptionReport:
    audio: AudioPerception
    scene: ScenePerception

    def __post_init__(self) -> None:
        if not isinstance(self.audio, AudioPerception):
            raise TypeError("audio must be an AudioPerception")
        if not isinstance(self.scene, ScenePerception):
            raise TypeError("scene must be a ScenePerception")


class PerceptionAnalyzer:
    """Pure, deterministic composition of loudness and scene-cut measurements."""

    def __init__(self, cut_config: CutConfig | None = None) -> None:
        self._cut_config = cut_config if cut_config is not None else CutConfig()

    def analyze_audio(
        self,
        channels: Sequence[Sequence[float]],
        fs: float,
        *,
        weights: Sequence[float] | None = None,
        n_fft: int = 1024,
    ) -> AudioPerception:
        mono = channels[0] if channels else ()
        return AudioPerception(
            integrated_lkfs=integrated_loudness(channels, fs, weights=weights),
            momentary_lkfs=momentary_loudness(channels, fs, weights=weights),
            short_term_lkfs=short_term_loudness(channels, fs, weights=weights),
            loudness_range_lu=loudness_range(channels, fs, weights=weights),
            true_peak_dbtp=true_peak_dbtp(channels, fs),
            bark_centroid=bark_spectrum_centroid(mono, fs, n_fft=n_fft),
            silences=silence_segments(channels, fs),
        )

    def analyze_scene(self, frames: Sequence[SceneFrame]) -> ScenePerception:
        transitions = analyze_sequence(frames, self._cut_config)
        hard_cuts = sum(1 for item in transitions if item.kind is TransitionKind.HARD_CUT)
        dissolves = sum(1 for item in transitions if item.kind is TransitionKind.DISSOLVE)
        flashes = sum(1 for item in transitions if item.kind is TransitionKind.FLASH_TRANSIENT)
        static_holds = sum(1 for item in transitions if item.kind is TransitionKind.STATIC_HOLD)
        mean_entropy = sum(frame.entropy for frame in frames) / len(frames) if frames else 0.0
        return ScenePerception(
            transitions=transitions,
            hard_cuts=hard_cuts,
            dissolves=dissolves,
            flashes=flashes,
            static_holds=static_holds,
            mean_entropy=mean_entropy,
        )

    def analyze(
        self,
        channels: Sequence[Sequence[float]],
        fs: float,
        frames: Sequence[SceneFrame],
        *,
        weights: Sequence[float] | None = None,
        n_fft: int = 1024,
    ) -> PerceptionReport:
        return PerceptionReport(
            audio=self.analyze_audio(channels, fs, weights=weights, n_fft=n_fft),
            scene=self.analyze_scene(frames),
        )
