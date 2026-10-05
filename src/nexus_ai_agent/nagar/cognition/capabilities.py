"""Derive the *offered* operations from the authoritative registry.

The cognition boundary narrows a producer to a closed set of operations
(:class:`ProposalSchema.allowed_operations`).  That set must never be invented
by a caller or a model: it is the intersection of what the runtime's
``CapabilityRegistry`` actually exposes.  This module is the one place that
reads the registry to build that set, so the allow-list has a single, auditable
origin instead of being hand-written at each call site.

A caller may *narrow* the offered set (offer fewer operations); it must never
*widen* it past the registry.  :func:`offered_operations` reads the registry
directly, so a caller cannot inject an operation the runtime does not have; the
command bus would reject it anyway, but it must never even be offered.
"""

from __future__ import annotations

from typing import Protocol


class _OperationSource(Protocol):
    """The minimal registry surface this module needs (structural, not imported)."""

    def list_operations(self) -> list[str]: ...


def offered_operations(registry: _OperationSource) -> frozenset[str]:
    """The closed set of operations a proposal may name, from the registry.

    ``CapabilityRegistry`` is the single operation allow-list consulted by the
    command bus; deriving ``allowed_operations`` from it means the cognition
    boundary can never offer what the deterministic substrate would reject.
    """
    return frozenset(registry.list_operations())


def offered_operations_within(
    registry: _OperationSource,
    requested: frozenset[str],
) -> frozenset[str]:
    """Narrow ``requested`` to the registry's operations.

    Lets a caller *prefer* a subset without ever granting more than the runtime
    exposes: the result is ``requested ∩ registry``.  A caller asking for a
    forbidden operation simply gets it silently dropped here — and if that
    leaves nothing, the producer is offered an empty set and can only refuse.
    """
    return offered_operations(registry) & requested


__all__ = ["offered_operations", "offered_operations_within"]
