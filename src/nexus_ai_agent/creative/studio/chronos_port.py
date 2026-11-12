"""Stable handoff contract for deterministic Chronos operations on CommandBus."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChronosCommand:
    operation: str
    inputs: tuple[str, ...]
    parameters: Mapping[str, str]
    deterministic: bool = True
    external_api_allowed: bool = False

    def __post_init__(self) -> None:
        if (
            not self.operation
            or not self.inputs
            or not self.deterministic
            or self.external_api_allowed
        ):
            raise ValueError("Chronos handoffs must be named, deterministic, and zero-egress")
