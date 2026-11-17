"""Registrar binding the color pack's operation contract to its NumPy kernels.

The pack declares operation names (pure data).  This adapter-side module is the
only place where those names are resolved to callables, and ``bind_operations``
refuses to build a registry that is missing or adding any declared operation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from nexus_ai_agent.creative.speech_cinema import color_imaging
from nexus_ai_agent.creative.speech_cinema.color_contract import OPERATIONS

Handler = Callable[..., object]

HANDLERS: Final[Mapping[str, Handler]] = {name: getattr(color_imaging, name) for name in OPERATIONS}


def bind_operations(
    contract: tuple[str, ...] = OPERATIONS,
    kernels: Mapping[str, Handler] | None = None,
) -> Mapping[str, Handler]:
    """Return the handler table for ``contract``; fail closed on any mismatch."""
    table: Mapping[str, Handler] = HANDLERS if kernels is None else kernels
    declared = set(contract)
    bound = set(table)
    if declared != bound:
        missing = sorted(declared - bound)
        extra = sorted(bound - declared)
        raise ValueError(f"color contract mismatch: missing={missing} extra={extra}")
    return {name: table[name] for name in contract}
