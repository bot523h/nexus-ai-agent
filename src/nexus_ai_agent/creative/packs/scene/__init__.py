"""Declarative contracts for the AETHEL ``nexus.scene.imaging`` pack (Wave 3).

This package holds only typed input models and typed errors (no numpy, no I/O,
no cross-package imports beyond ``creative.packs``/``creative.studio``). The
executable registrar and the NumPy kernels live in the composition layer:
``nexus_ai_agent.creative.composition.scene_ops`` and
``nexus_ai_agent.creative.composition.scene_imaging``.
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
    OpticalFlowRetimeInput,
    RadiativeTransferHarmonizeInput,
)

__all__ = [
    "DOMAIN",
    "SCENE_OPERATIONS",
    "SCENE_PACKAGE_ID",
    "OpticalFlowRetimeInput",
    "RadiativeTransferHarmonizeInput",
    "DegenerateGeometryError",
    "InvalidImageError",
    "InvalidMaskError",
    "SceneError",
]
