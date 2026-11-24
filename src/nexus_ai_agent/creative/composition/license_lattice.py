"""Bounded join-semilattice of distribution licenses.

``PUBLIC_DOMAIN`` is the identity, ``INCOMPATIBLE_CONFLICT`` (top) is absorbing,
and the join is idempotent, commutative and associative.  A non-commercial
regime combined with commercial intent, or share-alike combined with a
proprietary commercial regime, evaluates to top and fails closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from functools import reduce

__all__ = [
    "LicenseCombination",
    "LicenseError",
    "LicenseRegime",
    "combine_licenses",
    "join",
]


class LicenseError(ValueError):
    """Raised when a license combination is invalid or mixed with bad intent."""


class LicenseRegime(Enum):
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    PERMISSIVE_MIT_APACHE = "PERMISSIVE_MIT_APACHE"
    ATTRIBUTION_CC_BY = "ATTRIBUTION_CC_BY"
    SHARE_ALIKE_COPYLEFT = "SHARE_ALIKE_COPYLEFT"
    NON_COMMERCIAL_ONLY = "NON_COMMERCIAL_ONLY"
    PROPRIETARY_COMMERCIAL_RESTRICTED = "PROPRIETARY_COMMERCIAL_RESTRICTED"
    INCOMPATIBLE_CONFLICT = "INCOMPATIBLE_CONFLICT"


_TOP = LicenseRegime.INCOMPATIBLE_CONFLICT
_PD = LicenseRegime.PUBLIC_DOMAIN
_PERMISSIVE = LicenseRegime.PERMISSIVE_MIT_APACHE
_ATTRIBUTION = LicenseRegime.ATTRIBUTION_CC_BY
_SHARE_ALIKE = LicenseRegime.SHARE_ALIKE_COPYLEFT
_NON_COMMERCIAL = LicenseRegime.NON_COMMERCIAL_ONLY
_PROPRIETARY = LicenseRegime.PROPRIETARY_COMMERCIAL_RESTRICTED


def join(first: LicenseRegime, second: LicenseRegime) -> LicenseRegime:
    """Least upper bound in the license lattice."""

    if not isinstance(first, LicenseRegime) or not isinstance(second, LicenseRegime):
        raise TypeError("join requires two LicenseRegime values")
    if _TOP in (first, second):
        return _TOP
    if first is second:
        return first
    pair = frozenset((first, second))
    if _PD in pair:
        return second if first is _PD else first
    if pair == frozenset((_PERMISSIVE, _ATTRIBUTION)):
        return _ATTRIBUTION
    if pair == frozenset((_PERMISSIVE, _SHARE_ALIKE)):
        return _SHARE_ALIKE
    if pair == frozenset((_PERMISSIVE, _NON_COMMERCIAL)):
        return _NON_COMMERCIAL
    if pair == frozenset((_ATTRIBUTION, _SHARE_ALIKE)):
        return _SHARE_ALIKE
    if pair == frozenset((_ATTRIBUTION, _NON_COMMERCIAL)):
        return _NON_COMMERCIAL
    # Share-alike vs non-commercial, and proprietary vs anything else, conflict.
    return _TOP


@dataclass(frozen=True, slots=True)
class LicenseCombination:
    regimes: tuple[LicenseRegime, ...]
    supremum: LicenseRegime
    commercial_intent: bool

    def __post_init__(self) -> None:
        regimes = tuple(self.regimes)
        if not regimes:
            raise LicenseError("at least one license regime is required")
        if any(not isinstance(regime, LicenseRegime) for regime in regimes):
            raise TypeError("regimes must contain LicenseRegime values")
        if not isinstance(self.supremum, LicenseRegime):
            raise TypeError("supremum must be a LicenseRegime")
        if not isinstance(self.commercial_intent, bool):
            raise TypeError("commercial_intent must be a bool")
        object.__setattr__(self, "regimes", regimes)

    @property
    def compatible(self) -> bool:
        return self.supremum is not _TOP


def combine_licenses(
    regimes: Sequence[LicenseRegime], *, commercial_intent: bool = False
) -> LicenseCombination:
    if not isinstance(commercial_intent, bool):
        raise TypeError("commercial_intent must be a bool")
    values = tuple(regimes)
    if not values:
        raise LicenseError("at least one license regime is required")
    if any(not isinstance(regime, LicenseRegime) for regime in values):
        raise TypeError("regimes must contain LicenseRegime values")
    supremum = reduce(join, values, _PD)
    if commercial_intent and _NON_COMMERCIAL in values:
        supremum = _TOP
    if commercial_intent and _SHARE_ALIKE in values and _PROPRIETARY in values:
        supremum = _TOP
    return LicenseCombination(
        regimes=values,
        supremum=supremum,
        commercial_intent=commercial_intent,
    )
