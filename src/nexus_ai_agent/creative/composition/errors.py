"""Typed errors for the AETHEL composition package."""

from __future__ import annotations


class CompositionError(Exception):
    """Base class for every composition-package failure."""


class RationalTimeError(CompositionError):
    """An invalid rate, value or range."""


class RateMismatchError(RationalTimeError):
    """Two times at different rates were combined without a declared common rate."""


class CyclicGraphError(CompositionError):
    """A TimelineDAG contains a cycle; the cycle nodes are reported."""


class UnknownNodeError(CompositionError):
    """An edge references a node that does not exist in the graph."""


class TimeWarpError(CompositionError):
    """A time-warp curve is non-monotonic or out of range."""


class CompositeError(CompositionError):
    """A Porter-Duff composition input is malformed or out of bounds."""


__all__ = [
    "CompositionError",
    "RationalTimeError",
    "RateMismatchError",
    "CyclicGraphError",
    "UnknownNodeError",
    "TimeWarpError",
    "CompositeError",
]
