"""Hyperion color operation contract (pure data).

Declares which color operations exist and the Kelvin range they accept.  The
NumPy kernels live in ``color_imaging`` and are bound to these names by
``color_ops``.  The contract sits here, not under ``creative/packs/``, because
the pack substrate is NumPy-free and import-restricted by ``tests/architecture``
and cannot host kernel bindings.
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
