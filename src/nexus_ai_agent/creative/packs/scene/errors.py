"""Typed errors for the AETHEL scene pack."""

from __future__ import annotations


class SceneError(Exception):
    """Base class for scene-pack failures."""


class InvalidImageError(SceneError):
    """An image buffer is malformed (shape, dtype, non-finite values)."""


class InvalidMaskError(SceneError):
    """A mask is malformed, empty, or incompatible with the image."""


class DegenerateGeometryError(SceneError):
    """A geometric input (homography, horizon angle) is degenerate."""


__all__ = ["SceneError", "InvalidImageError", "InvalidMaskError", "DegenerateGeometryError"]
