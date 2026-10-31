"""Scene pack composition entry points."""

from nexus_ai_agent.creative.packs.vision.operations import register_scene_operations as _register
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry


def register_scene_operations(registry: CapabilityRegistry) -> None:
    _register(registry)


def build_scene_registry() -> CapabilityRegistry:
    registry = CapabilityRegistry()
    register_scene_operations(registry)
    return registry


__all__ = ["build_scene_registry", "register_scene_operations"]
