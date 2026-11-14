"""Declarative contracts for the AETHEL ``nexus.scene.imaging`` pack (Wave 3).

This package holds only declarative contracts (no numpy, no I/O, no
cross-package imports beyond ``creative.packs``/``creative.studio``): typed
input models, typed errors, the manifest, and ``operations.py`` — the
declarative registrar (spec table + fail-closed delegates). The NumPy kernels
live in the composition zone (``creative/composition/``), behind the
``context.scene_kernel`` contract from ``creative.studio``.
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.scene.errors import (
    DegenerateGeometryError,
    InvalidImageError,
    InvalidMaskError,
    SceneError,
)
from nexus_ai_agent.creative.packs.scene.models import (
    DOMAIN,
    SCENE_OPERATIONS,
    SCENE_PACKAGE_ID,
    FilmEmulationInput,
    HdrTonemapInput,
    OpticalFlowRetimeInput,
    RadiativeTransferHarmonizeInput,
)

__all__ = [
    "DOMAIN",
    "SCENE_OPERATIONS",
    "SCENE_PACKAGE_ID",
    "FilmEmulationInput",
    "HdrTonemapInput",
    "OpticalFlowRetimeInput",
    "RadiativeTransferHarmonizeInput",
    "DegenerateGeometryError",
    "InvalidImageError",
    "InvalidMaskError",
    "SceneError",
]
