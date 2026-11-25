"""Typed capability contracts: every creative capability is a morphism.

A contract declares a domain (input ports), a codomain (output ports), explicit
media/rate/channel/colour/shape types, bounded resources and canonical
parameters.  Composition rejects a type mismatch at compile time, before any
compute is spent.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

__all__ = [
    "CapabilityContract",
    "CompositionContractError",
    "ConfigValue",
    "PortSpec",
    "PortType",
]

ConfigValue = str | int | float | bool


class CompositionContractError(ValueError):
    """Raised when a contract or port type violates the typed morphism law."""


@dataclass(frozen=True, slots=True)
class PortType:
    media: str
    sample_rate: int | None = None
    channels: int | None = None
    color_space: str | None = None
    shape: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.media, str) or not self.media:
            raise CompositionContractError("media must be a non-empty string")
        if self.sample_rate is not None and (
            isinstance(self.sample_rate, bool)
            or not isinstance(self.sample_rate, int)
            or self.sample_rate <= 0
        ):
            raise CompositionContractError("sample_rate must be a positive int or None")
        if self.channels is not None and (
            isinstance(self.channels, bool)
            or not isinstance(self.channels, int)
            or self.channels <= 0
        ):
            raise CompositionContractError("channels must be a positive int or None")
        if self.color_space is not None and (
            not isinstance(self.color_space, str) or not self.color_space
        ):
            raise CompositionContractError("color_space must be a non-empty string or None")
        shape = tuple(self.shape)
        for dimension in shape:
            if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
                raise CompositionContractError("shape dimensions must be positive ints")
        object.__setattr__(self, "shape", shape)

    def is_subtype_of(self, other: PortType) -> bool:
        """``self ≼ other``: media must match; ``None``/empty on ``other`` is a wildcard."""

        if not isinstance(other, PortType):
            raise TypeError("other must be a PortType")
        if self.media != other.media:
            return False
        if other.sample_rate is not None and self.sample_rate != other.sample_rate:
            return False
        if other.channels is not None and self.channels != other.channels:
            return False
        if other.color_space is not None and self.color_space != other.color_space:
            return False
        if other.shape and self.shape != other.shape:
            return False
        return True


@dataclass(frozen=True, slots=True)
class PortSpec:
    name: str
    port_type: PortType

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise CompositionContractError("port name must be a non-empty string")
        if not isinstance(self.port_type, PortType):
            raise TypeError("port_type must be a PortType")


def _validate_parameters(parameters: Mapping[str, ConfigValue]) -> Mapping[str, ConfigValue]:
    frozen: dict[str, ConfigValue] = {}
    for key, value in parameters.items():
        if not isinstance(key, str) or not key:
            raise CompositionContractError("parameter keys must be non-empty strings")
        if isinstance(value, bool):
            frozen[key] = value
        elif isinstance(value, int):
            frozen[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise CompositionContractError("float parameters must be finite")
            frozen[key] = value
        elif isinstance(value, str):
            frozen[key] = value
        else:
            raise CompositionContractError("parameters must be JSON scalar values")
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class CapabilityContract:
    capability_id: str
    version: str
    inputs: tuple[PortSpec, ...]
    outputs: tuple[PortSpec, ...]
    budget_seconds: float
    budget_cost: float
    parameters: Mapping[str, ConfigValue] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not isinstance(self.capability_id, str) or not self.capability_id:
            raise CompositionContractError("capability_id must be a non-empty string")
        if not isinstance(self.version, str) or not self.version:
            raise CompositionContractError("version must be a non-empty string")
        inputs = tuple(self.inputs)
        outputs = tuple(self.outputs)
        if not outputs:
            raise CompositionContractError("a morphism needs at least one output")
        if any(not isinstance(port, PortSpec) for port in (*inputs, *outputs)):
            raise TypeError("ports must be PortSpec values")
        input_names = [port.name for port in inputs]
        output_names = [port.name for port in outputs]
        if len(set(input_names)) != len(input_names):
            raise CompositionContractError("input port names must be unique")
        if len(set(output_names)) != len(output_names):
            raise CompositionContractError("output port names must be unique")
        if not math.isfinite(self.budget_seconds) or self.budget_seconds <= 0.0:
            raise CompositionContractError("budget_seconds must be finite and positive")
        if not math.isfinite(self.budget_cost) or self.budget_cost < 0.0:
            raise CompositionContractError("budget_cost must be finite and non-negative")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "parameters", _validate_parameters(self.parameters))

    def input(self, name: str) -> PortSpec:
        for port in self.inputs:
            if port.name == name:
                return port
        raise CompositionContractError(f"unknown input port {name!r}")

    def output(self, name: str) -> PortSpec:
        for port in self.outputs:
            if port.name == name:
                return port
        raise CompositionContractError(f"unknown output port {name!r}")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "version": self.version,
            "inputs": [
                {
                    "name": port.name,
                    "media": port.port_type.media,
                    "sample_rate": port.port_type.sample_rate,
                    "channels": port.port_type.channels,
                    "color_space": port.port_type.color_space,
                    "shape": list(port.port_type.shape),
                }
                for port in self.inputs
            ],
            "outputs": [
                {
                    "name": port.name,
                    "media": port.port_type.media,
                    "sample_rate": port.port_type.sample_rate,
                    "channels": port.port_type.channels,
                    "color_space": port.port_type.color_space,
                    "shape": list(port.port_type.shape),
                }
                for port in self.outputs
            ],
            "budget_seconds": self.budget_seconds,
            "budget_cost": self.budget_cost,
            "parameters": dict(self.parameters),
        }
