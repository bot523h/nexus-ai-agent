"""Generated media fixtures for the Wave 2 slideshow tests.

The repository deliberately ships no binary media: every fixture is synthesized
here (Pillow for images, numpy + the standard library ``wave`` module for
audio), so the tests stay reproducible and the git history stays small.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

CLICK_TRACK_SECONDS = 61.0
CLICK_TRACK_BPM = 120.0
SAMPLE_RATE = 22050


@dataclass(frozen=True)
class SlideshowMedia:
    """Paths to one generated fixture set."""

    directory: Path
    images: tuple[Path, ...]
    audio: Path
    blurred_image: Path
    crisp_image: Path

    @property
    def image_paths(self) -> list[str]:
        return [str(path) for path in self.images]


def make_image(path: Path, seed: int, *, blur: bool = False) -> Path:
    rng = np.random.default_rng(seed)
    image = Image.new("RGB", (960, 540), (28 + seed * 11, 64 + (seed * 7) % 120, 96))
    draw = ImageDraw.Draw(image)
    for _ in range(45):
        x, y = int(rng.integers(0, 900)), int(rng.integers(0, 480))
        radius = int(rng.integers(6, 70))
        draw.ellipse(
            [x, y, x + radius, y + radius],
            fill=(
                int(rng.integers(0, 255)),
                int(rng.integers(0, 255)),
                int(rng.integers(0, 255)),
            ),
        )
    draw.rectangle([20, 20, 260, 90], outline=(255, 255, 255), width=4)
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(6.0))
    image.save(path, quality=90)
    return path


def make_click_track(path: Path, *, bpm: float, seconds: float, active: bool = True) -> Path:
    """A synthetic click track: 4-on-the-floor accent every four beats."""
    rng = np.random.default_rng(11)
    samples = (0.02 * rng.standard_normal(int(SAMPLE_RATE * seconds))).astype(np.float32)
    if active:
        for beat, start in enumerate(np.arange(0.0, seconds, 60.0 / bpm)):
            index = int(start * SAMPLE_RATE)
            length = int(0.05 * SAMPLE_RATE)
            if index + length >= samples.size:
                break
            amplitude = 0.9 if beat % 4 == 0 else 0.5
            envelope = np.exp(-np.linspace(0.0, 12.0, length))
            tone = np.sin(
                2 * np.pi * (180.0 if beat % 4 == 0 else 320.0) * np.arange(length) / SAMPLE_RATE
            )
            samples[index : index + length] += (amplitude * envelope * tone).astype(np.float32)
    peak = float(np.max(np.abs(samples))) or 1.0
    pcm = (samples / peak * 0.85 * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
    return path


def build_media_set(directory: Path) -> SlideshowMedia:
    images = [
        make_image(directory / f"shot_{index:02d}.jpg", index, blur=index == 3)
        for index in range(12)
    ]
    audio = make_click_track(
        directory / "beat_120.wav", bpm=CLICK_TRACK_BPM, seconds=CLICK_TRACK_SECONDS
    )
    return SlideshowMedia(
        directory=directory,
        images=tuple(images),
        audio=audio,
        blurred_image=images[3],
        crisp_image=images[0],
    )
