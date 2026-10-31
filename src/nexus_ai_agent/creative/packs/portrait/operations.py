"""Portrait pack composition entry points."""

from nexus_ai_agent.creative.packs.vision.operations import (
    register_portrait_operations as _register,
)
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry


def register_portrait_operations(registry: CapabilityRegistry) -> None:
    _register(registry)


def build_portrait_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    register_portrait_operations(registry)
    return registry


__all__ = ["build_portrait_registry", "register_portrait_operations"]
