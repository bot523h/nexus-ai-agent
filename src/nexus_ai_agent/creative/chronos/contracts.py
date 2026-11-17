"""Shared typed contracts and exact time arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction


class KernelError(ValueError):
    """Invalid or unsafe input to a deterministic media kernel."""


class Continuity(str, Enum):
    OK = "OK"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RationalTimestamp:
    """A presentation timestamp expressed in integer ticks and seconds/tick."""

    pts: int
    time_base: Fraction

    def __post_init__(self) -> None:
        if self.time_base <= 0:
            raise KernelError("time_base must be positive")

    @property
    def seconds(self) -> Fraction:
        return self.pts * self.time_base

    def rescale(self, time_base: Fraction) -> RationalTimestamp:
        if time_base <= 0:
            raise KernelError("time_base must be positive")
        ticks = self.seconds / time_base
        if ticks.denominator != 1:
            raise KernelError("timestamp is not exactly representable in target time base")
        return RationalTimestamp(ticks.numerator, time_base)

    def __add__(self, ticks: int) -> RationalTimestamp:
        return RationalTimestamp(self.pts + ticks, self.time_base)
