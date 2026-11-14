"""Hyper compiler package — isolated, memory-bounded video/audio processing.

This package implements the "Nagar Phase 1" hyper-compiler:

* :mod:`bulletproof_normalizer` — mobile/cinema input normalisation (rotation,
  VFR → CFR, injected silence, atomic MP4 output).
* :mod:`optical_spectral_analyzer` — streaming proxy-frame analyser (OKLab
  colour space, Jensen–Shannon shot boundaries, Euler–Lagrange trajectory
  smoothing, 16 kHz speech/silence analysis).
* :mod:`differential_filtergraph_compiler` — FFmpeg ``filter_complex`` compiler,
  :file:`.cube` 3D LUT generator and content-addressed segment renderer.

Every module is safe to import without running a subprocess; only the explicit
``normalize``/``render``/``analyse`` entry points execute FFmpeg, always with
``shell=False`` and a bounded ``timeout``.
"""

from __future__ import annotations

from .models import (
    HDR,
    SDR,
    ColorSpace,
    MediaStreamProbe,
    TimelineDAG,
    TimelineDifferentialNode,
    canonical_params_hash,
)

__all__ = [
    "ColorSpace",
    "HDR",
    "MediaStreamProbe",
    "SDR",
    "TimelineDAG",
    "TimelineDifferentialNode",
    "canonical_params_hash",
]
