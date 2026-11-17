"""Typed errors for the AETHEL ``nexus.portrait.refine`` pack (Wave 4)."""

from __future__ import annotations


class PortraitError(Exception):
    """Base class for every portrait-pack refusal."""


class InvalidImageError(PortraitError):
    """The supplied image (or depth map) is malformed or out of range."""


class InvalidRegionError(PortraitError):
    """A mask, region, or numeric parameter is malformed or out of range."""


class DegenerateWarpError(PortraitError):
    """A geometric warp is non-invertible (folds over)."""


__all__ = ["PortraitError", "InvalidImageError", "InvalidRegionError", "DegenerateWarpError"]
