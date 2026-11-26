"""The production capability registry: every builtin pack, admission gate bound.

:func:`nexus_ai_agent.creative.packs.runtime.build_runtime_registry` is the
*engine*: it composes every builtin pack but cannot bind the perception
admission gate, because the pack substrate may import only
``packs``/``studio``/``temporal`` (``docs/architecture/MODULE_MAP.md`` R31).

This module is the composition root that closes that gap. It sits outside the
restricted packages, imports the gate once, and hands it to the engine. Every
production caller (render lane, creative passport, truth projection, CLI) builds
its registry here, so a registry with an unbound gate is a *test* artefact and
never a runtime one.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.packs.registry import PackRegistry
from nexus_ai_agent.creative.packs.runtime import (
    build_pack_registry as _build_pack_registry,
)
from nexus_ai_agent.creative.packs.runtime import (
    build_runtime_registry as _build_runtime_registry,
)
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.creative.verification.perception_gate import gate_perception

__all__ = ["build_production_pack_registry", "build_production_registry"]


def build_production_registry(
    *,
    tone_library: Any | None = None,
    compositions: Any | None = None,
) -> CapabilityRegistry:
    """Every builtin pack's operations, with the perception gate bound."""
    return _build_runtime_registry(
        tone_library=tone_library,
        compositions=compositions,
        perception_gate=gate_perception,
    )


def build_production_pack_registry(
    *,
    current_version: str | None = None,
    tone_library: Any | None = None,
    compositions: Any | None = None,
) -> PackRegistry:
    """The :class:`PackRegistry` over :func:`build_production_registry`."""
    return _build_pack_registry(
        current_version=current_version,
        tone_library=tone_library,
        compositions=compositions,
        perception_gate=gate_perception,
    )
