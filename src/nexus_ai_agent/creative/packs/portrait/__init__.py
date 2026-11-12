"""Declarative contracts for the AETHEL ``nexus.portrait.refine`` pack (Wave 4).

This package holds only typed input models and typed errors (no numpy, no I/O).
The executable registrar and NumPy kernels live in the composition layer:
``nexus_ai_agent.creative.composition.portrait_ops`` and
``nexus_ai_agent.creative.composition.portrait_imaging``.
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.portrait.errors import (
    DegenerateWarpError,
    InvalidImageError,
    InvalidRegionError,
    PortraitError,
)
from nexus_ai_agent.creative.packs.portrait.models import (
    DOMAIN,
    PORTRAIT_OPERATIONS,
    PORTRAIT_PACKAGE_ID,
)

__all__ = [
    "DOMAIN",
    "PORTRAIT_OPERATIONS",
    "PORTRAIT_PACKAGE_ID",
    "DegenerateWarpError",
    "InvalidImageError",
    "InvalidRegionError",
    "PortraitError",
]
