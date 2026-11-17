"""Contract for the native scene kernel.

The ``nexus.scene.imaging`` pack registers *declarative* operation specs in
the common runtime registry; executing them honestly requires real NumPy
kernels, which live in the composition zone and own images the pack may
never see (packs hold no buffers and no heavy dependencies).

The application composition root injects an implementation of this protocol
as ``context.scene_kernel``. Without it, every ``scene.*`` operation fails
closed with a clear error — rendering is never simulated.

The reference implementation is
:class:`nexus_ai_agent.creative.composition.scene_ops.NativeSceneKernel`.
"""

from __future__ import annotations

from typing import Protocol

from nexus_ai_agent.creative.studio.capabilities import OperationContext, OperationOutcome
from nexus_ai_agent.creative.studio.models import Project

#: Attribute name on :class:`OperationContext` carrying the injected kernel.
KERNEL_ATTR = "scene_kernel"


class SceneKernel(Protocol):
    """Execute one ``scene.*`` operation against an in-memory image store."""

    def execute(
        self, operation_id: str, project: Project, context: OperationContext
    ) -> OperationOutcome:
        """Run ``operation_id``; raise on unknown ids or invalid payloads."""
        ...
