"""Authorization adapters for the Asset Library.

The library never treats ``project_id`` as proof of access.  These adapters
reuse the existing Studio ``ProjectAuthorizer`` seam (the composition root
injects a real principal/membership store) and translate its typed errors into
the library's :class:`AuthorizationFailure`.

``GrantTableAuthorizer`` is a small, explicit authorizer for the local runtime
and tests: it holds a *trusted* grant table, so a request's JSON can never mint
its own permission.
"""

from __future__ import annotations

from collections.abc import Mapping

from nexus_ai_agent.creative.assets.errors import AuthorizationFailure
from nexus_ai_agent.creative.studio.authorization import ProjectAccess, ProjectAuthorizer
from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError


class StudioAuthorizationSeam:
    """Bind the Asset Library's ``AuthorizationSeam`` to a ``ProjectAuthorizer``."""

    def __init__(self, authorizer: ProjectAuthorizer) -> None:
        self._authorizer = authorizer

    def require(self, actor_kind: str, actor_id: str, project_id: str, permission: str) -> None:
        try:
            actor = ActorIdentity(kind=actor_kind, actor_id=actor_id)  # type: ignore[arg-type]
        except Exception as exc:  # invalid identity claim
            raise AuthorizationFailure("invalid actor identity") from exc
        try:
            access = self._authorizer.authorize(actor, project_id)
            access.require_permissions((permission,))
        except AuthorizationError as exc:
            raise AuthorizationFailure(str(exc)) from exc


class GrantTableAuthorizer:
    """A trusted, explicit grant table implementing the ``ProjectAuthorizer`` port.

    Keys are ``(actor_id, project_id)``; values are the permission set.  An
    unknown pair is denied — absence is never a grant.
    """

    def __init__(self, grants: Mapping[tuple[str, str], frozenset[str]]) -> None:
        self._grants = dict(grants)

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        permissions = self._grants.get((actor.actor_id, project_id))
        if permissions is None:
            raise AuthorizationError("actor is not authorized for this project")
        return ProjectAccess(actor=actor, project_id=project_id, permissions=permissions)
