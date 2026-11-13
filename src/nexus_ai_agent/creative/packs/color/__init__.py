"""Hyperion Phase 3 color capability pack: pure data contract.

This package is part of the capability-pack substrate, which is NumPy-free and
import-restricted by ``tests/architecture``.  It only declares *which* color
operations exist.  The kernels live in
``nexus_ai_agent.creative.speech_cinema.color_imaging`` and are bound to these
names by ``nexus_ai_agent.creative.speech_cinema.color_ops``.
"""

from __future__ import annotations

from typing import Final

OPERATIONS: Final[tuple[str, ...]] = (
    "white_balance_chromatic_adaptation",
    "hdr_to_sdr_filmic_tonemap",
    "shot_to_shot_color_match_ot",
    "validate_spd_covariance",
    "matrix_sqrt_spd",
    "save_color_png",
    "load_color_png",
    "verify_color_png",
)

KELVIN_RANGE: Final[tuple[float, float]] = (1000.0, 40000.0)

__all__ = ["KELVIN_RANGE", "OPERATIONS"]
