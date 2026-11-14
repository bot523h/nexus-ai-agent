"""Declarative contracts for the AETHEL ``nexus.portrait.refine`` pack (Wave 4).

This package holds only declarative contracts (no numpy, no I/O, no
cross-package imports beyond ``creative.packs``/``creative.studio``): typed
input models, typed errors, the manifest, and ``operations.py`` — the
declarative registrar (spec table + fail-closed delegates). The NumPy kernels
live in the composition zone (``creative/composition/``), behind the
``context.portrait_kernel`` contract from ``creative.studio``.
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
    SphericalHarmonicsRelightInput,
)

__all__ = [
    "DOMAIN",
    "PORTRAIT_OPERATIONS",
    "PORTRAIT_PACKAGE_ID",
    "SphericalHarmonicsRelightInput",
    "DegenerateWarpError",
    "InvalidImageError",
    "InvalidRegionError",
    "PortraitError",
]
