"""Explicit status vocabularies for the ATLAS episodic-causality lab.

The lab never silently upgrades uncertainty into success. Two vocabularies are
defined here and used everywhere else:

``TriValue``
    A three-valued truth: ``TRUE``, ``FALSE`` or ``UNKNOWN``. A fact that was
    never explicitly established is ``UNKNOWN`` — absence of data is never
    evidence of continuity.

``Status``
    The typed outcome of an analysis or a solver run. ``PROVEN_OPTIMAL`` is
    reserved for the precisely-defined finite optimisation problem and only when
    the complete applicable search actually ran. ``UNKNOWN``/``NEEDS_REVIEW``
    cover missing evidence; ``RESOURCE_LIMIT`` covers a bound that stopped a
    conclusion short; ``UNSUPPORTED`` covers an input the lab does not model.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["TriValue", "Status", "SolverStatus"]


class TriValue(str, Enum):
    """A three-valued logic value. ``UNKNOWN`` is not ``FALSE``."""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"

    def __bool__(self) -> bool:  # pragma: no cover - guards accidental truthiness
        raise TypeError(
            "TriValue is not a boolean; compare explicitly with TriValue.TRUE/FALSE/UNKNOWN"
        )

    def negate(self) -> TriValue:
        """Classical three-valued negation (Kleene)."""
        if self is TriValue.TRUE:
            return TriValue.FALSE
        if self is TriValue.FALSE:
            return TriValue.TRUE
        return TriValue.UNKNOWN

    @classmethod
    def conjunction(cls, a: TriValue, b: TriValue) -> TriValue:
        """Kleene conjunction: FALSE dominates, then UNKNOWN, then TRUE."""
        if a is TriValue.FALSE or b is TriValue.FALSE:
            return TriValue.FALSE
        if a is TriValue.UNKNOWN or b is TriValue.UNKNOWN:
            return TriValue.UNKNOWN
        return TriValue.TRUE

    @classmethod
    def disjunction(cls, a: TriValue, b: TriValue) -> TriValue:
        """Kleene disjunction: TRUE dominates, then UNKNOWN, then FALSE."""
        if a is TriValue.TRUE or b is TriValue.TRUE:
            return TriValue.TRUE
        if a is TriValue.UNKNOWN or b is TriValue.UNKNOWN:
            return TriValue.UNKNOWN
        return TriValue.FALSE


class Status(str, Enum):
    """The typed outcome of an analysis pass or a solver run."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    INFEASIBLE = "INFEASIBLE"
    FEASIBLE_NOT_PROVEN_OPTIMAL = "FEASIBLE_NOT_PROVEN_OPTIMAL"
    PROVEN_OPTIMAL = "PROVEN_OPTIMAL"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    UNSUPPORTED = "UNSUPPORTED"


class SolverStatus(str, Enum):
    """Subset of :class:`Status` a bounded solver may return."""

    PROVEN_OPTIMAL = "PROVEN_OPTIMAL"
    FEASIBLE_NOT_PROVEN_OPTIMAL = "FEASIBLE_NOT_PROVEN_OPTIMAL"
    INFEASIBLE = "INFEASIBLE"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
