"""Typed errors for the AETHEL director package."""

from __future__ import annotations


class DirectorError(Exception):
    """Base class for every director-package failure."""


class ScreenplayParseError(DirectorError):
    """The screenplay text is malformed or violates a declared bound."""


class InputBoundError(DirectorError):
    """An input exceeded a declared resource bound."""


class GeometryError(DirectorError):
    """A camera/geometry constraint cannot be satisfied by any plan."""


class AxisViolationError(GeometryError):
    """A proposed camera crosses the 180-degree line without a declared reset."""


class JumpCutError(GeometryError):
    """Two consecutive shots on one subject violate the 30-degree / focal rule."""


class EyelineMismatchError(GeometryError):
    """Two facing singles do not form a consistent eyeline pair."""


class ProxemicsError(GeometryError):
    """An actor placement violates the minimum separation or Hall band."""


__all__ = [
    "DirectorError",
    "ScreenplayParseError",
    "InputBoundError",
    "GeometryError",
    "AxisViolationError",
    "JumpCutError",
    "EyelineMismatchError",
    "ProxemicsError",
]
