"""NAGAR cinema science: photochemistry, optics, optimal transport, symplectic dynamics.

This package holds the mathematically-verified science modules behind the
NAGAR unified cinema science pipeline.  Each module is pure and framework-free
(NumPy + stdlib) and its contracts are pinned by ``tests/unit/test_w6_*``.
"""

from __future__ import annotations

from nexus_ai_agent.creative.science.photochemical_emulsion import (
    CANONICAL_LAYERS,
    PAIR_HIGH,
    PAIR_LOW,
    R_GAS,
    DirFilmState,
    EmulsionLayer,
    arrhenius_gamma,
    beer_lambert_rgb,
    canonical_dir_chain,
    dir_film_state,
    dir_reaction_diffusion,
    hurt_driffield_slope_ln,
    hurt_driffield_slope_log10,
    hurter_driffield_density,
    layer_gamma,
    schwarzschild_reciprocity,
)

__all__ = [
    "PAIR_HIGH",
    "PAIR_LOW",
    "R_GAS",
    "CANONICAL_LAYERS",
    "DirFilmState",
    "EmulsionLayer",
    "arrhenius_gamma",
    "beer_lambert_rgb",
    "canonical_dir_chain",
    "dir_film_state",
    "dir_reaction_diffusion",
    "hurter_driffield_density",
    "hurt_driffield_slope_ln",
    "hurt_driffield_slope_log10",
    "layer_gamma",
    "schwarzschild_reciprocity",
]
