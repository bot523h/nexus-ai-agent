"""Item 4 — the ATLAS studio render adapter.

This module is the *only* ATLAS component that touches the canonical NEXUS
studio seam. It does not own a CommandBus, a project database, an
``AssetVersion`` or a passport; it is a thin, advisory **adapter** that:

1. binds the studio's *real* ``ProjectAuthorizer`` / ``ProjectAccess`` seam
   (:mod:`nexus_ai_agent.creative.studio.authorization`) into the ATLAS
   :class:`~nexus_ai_agent.creative.atlas.atlas_spine_governance.CommandBusAuthorizer`
   port, so an ATLAS dispatch is authorized by the *same* trusted principal and
   project membership the studio bus uses — never by a self-asserted claim; and
2. runs a governed ATLAS render (the TITAN staged spine or the OLYMPUS master)
   **through** the studio authorization seam, failing closed if the actor is not
   a member of the project or lacks the operation's required permissions.

Honesty boundary (why this is an adapter, not a render command):

* The canonical studio write path is ``CommandBus.dispatch`` into a *registered*
  operation handler. ATLAS is deliberately **not** registered as a studio
  capability — that is an out-of-zone change (``studio/capabilities.py`` is
  off-limits). Therefore this adapter does **not** dispatch a CommandBus
  command; it proves the authorization seam end-to-end and produces the
  governed ledger + lineage commit. The remaining wiring needed for a real
  studio dispatch is documented in the ATLAS handoff note, not faked here.
* The adapter requires a ``revision_id`` and ``operation`` and consults the
  studio grant's permissions *and* an optional ``permits`` predicate bound to
  the exact target; a blanket grant is never sufficient.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.creative.atlas.atlas_spine_governance import (
    AtlasLineageRepositoryProtocol,
    CommandBusAuthorizer,
    CommandBusDecision,
    SpineAuthorizationError,
    run_olympus_master_governed,
    run_staged_spine_governed,
)
from nexus_ai_agent.creative.atlas.titan_staged_execution_spine import SpineLedger
from nexus_ai_agent.creative.studio.authorization import ProjectAuthorizer
from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError
from nexus_ai_agent.tools.filesystem_policy import WorkspaceFilesystem

__all__ = [
    "StudioRenderAdapter",
    "StudioRenderError",
    "STUDIO_RENDER_OPERATION",
    "studio_authorizer",
]

#: The default ATLAS operation name when routing a render through the adapter.
STUDIO_RENDER_OPERATION = "atlas.render.governed"

#: The permission an ATLAS render requires on the bound project. This mirrors the
#: studio's own write baseline (``OperationSpec.effective_permissions`` uses
#: ``project:write`` for any non-IMMEDIATE level); it is *declared* here rather
#: than borrowed so the requirement is auditable in the ATLAS zone.
_REQUIRED_PERMISSION = "project:write"


class StudioRenderError(SpineAuthorizationError):
    """An ATLAS render was refused by the studio authorization seam."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _StudioAuthorizer:
    """Adapt a studio ``ProjectAuthorizer`` into the ATLAS authorizer port.

    The studio port is ``authorize(actor: ActorIdentity, project_id: str) ->
    ProjectAccess``. The ATLAS port is ``authorize(*, operation, actor,
    project_id, payload) -> Any``. This adapter binds the *trusted* studio
    principal for exactly one project (supplied by the caller, never parsed from
    the ATLAS payload) and returns a :class:`CommandBusDecision` whose
    ``permits`` predicate pins the exact target.
    """

    def __init__(
        self,
        authorizer: ProjectAuthorizer,
        actor: ActorIdentity,
        *,
        required_permissions: tuple[str, ...] = (_REQUIRED_PERMISSION,),
    ) -> None:
        self._authorizer = authorizer
        self._actor = actor
        self._required = required_permissions

    def authorize(
        self,
        *,
        operation: str,
        actor: str,
        project_id: str,
        payload: dict[str, Any],
    ) -> CommandBusDecision:
        # The ATLAS ``actor`` string is only a *label*; the trusted identity is
        # the ActorIdentity the composition root supplied. If the label was set
        # to something other than the trusted actor id, that is a mismatch and
        # must fail closed rather than silently authorize.
        if actor not in (self._actor.actor_id, "atlas"):
            raise StudioRenderError(
                "ACTOR_MISMATCH",
                f"actor label {actor!r} does not match the bound studio principal "
                f"{self._actor.actor_id!r}",
            )
        try:
            access = self._authorizer.authorize(self._actor, project_id)
            access.require_permissions(self._required)
        except AuthorizationError as exc:
            raise StudioRenderError("STUDIO_DENIED", str(exc)) from exc
        return CommandBusDecision(
            authorized=True,
            authority_mode="AUTHORIZED",
            permits=lambda p, r, o, a, f: (  # noqa: E731 - small pure predicate
                p == project_id and o == operation
            ),
        )


def studio_authorizer(
    authorizer: ProjectAuthorizer,
    actor: ActorIdentity,
    *,
    required_permissions: tuple[str, ...] = (_REQUIRED_PERMISSION,),
) -> CommandBusAuthorizer:
    """Bind a studio ``ProjectAuthorizer`` to the ATLAS authorizer port.

    Returns an object satisfying :class:`CommandBusAuthorizer` that consults the
    studio grant before any ATLAS staging, failing closed with
    ``StudioRenderError`` when the actor is not a member of the project or lacks
    the required permissions.
    """
    return _StudioAuthorizer(authorizer, actor, required_permissions=required_permissions)


class StudioRenderAdapter:
    """Run a governed ATLAS render through the canonical studio authorization.

    The adapter is a *proposal/handoff* layer: it produces the governed
    :class:`SpineLedger`, the quarantine/lineage side effects and the
    :class:`AuthorityReceipt` — it does not mint a passport or claim an
    ``AssetVersion``.
    """

    def __init__(
        self,
        authorizer: ProjectAuthorizer,
        actor: ActorIdentity,
        *,
        project_id: str,
        required_permissions: tuple[str, ...] = (_REQUIRED_PERMISSION,),
    ) -> None:
        self._bound = studio_authorizer(
            authorizer, actor, required_permissions=required_permissions
        )
        self._project_id = project_id

    @property
    def authorizer(self) -> CommandBusAuthorizer:
        """The port object, for callers that prefer to drive the spine directly."""
        return self._bound

    def render_spine(
        self,
        film: Any,
        filesystem: WorkspaceFilesystem,
        *,
        operation: str = STUDIO_RENDER_OPERATION,
        revision_id: str = "rev-1",
        attempt_id: str = "attempt-1",
        fencing_token: int = 1,
        lineage: AtlasLineageRepositoryProtocol | None = None,
        published_root: str = "",
        **spine_kwargs: Any,
    ) -> SpineLedger:
        """Authorize then run the TITAN staged spine under studio authority."""
        return run_staged_spine_governed(
            film,
            filesystem,
            command_bus_authorizer=self._bound,
            actor="atlas",
            project_id=self._project_id,
            revision_id=revision_id,
            operation=operation,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            lineage=lineage,
            published_root=published_root,
            **spine_kwargs,
        )

    def render_master(
        self,
        bible: Any,
        filesystem: WorkspaceFilesystem,
        *,
        operation: str = "atlas.render.master.governed",
        revision_id: str = "rev-1",
        attempt_id: str = "attempt-1",
        fencing_token: int = 1,
        lineage: AtlasLineageRepositoryProtocol | None = None,
        published_root: str = "",
        **master_kwargs: Any,
    ) -> Any:
        """Authorize then run the OLYMPUS master render under studio authority."""
        return run_olympus_master_governed(
            bible,
            filesystem,
            command_bus_authorizer=self._bound,
            actor="atlas",
            project_id=self._project_id,
            revision_id=revision_id,
            operation=operation,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
            lineage=lineage,
            published_root=published_root,
            **master_kwargs,
        )
