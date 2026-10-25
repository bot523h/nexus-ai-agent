"""Trusted, project-scoped authorization seam for the Nagar command bus.

The envelope's actor is a *claim*. The composition root must bind the real
principal and project membership in an authorizer; neither a command nor a
capability snapshot can create a grant. For ephemeral local projects a trusted
service can use ProjectAccess directly. A future API must inject an adapter
backed by its authenticated user/project membership store, not build grants
from request JSON. The pure studio layer knows no database or web framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError


@dataclass(frozen=True)
class ProjectAccess:
    """A trusted principal's permissions for exactly one project."""

    actor: ActorIdentity
    project_id: str
    permissions: frozenset[str]

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        if self.actor != actor or self.project_id != project_id:
            raise AuthorizationError("actor is not authorized for this project")
        return self

    def require_permissions(self, required: tuple[str, ...]) -> None:
        if not set(required).issubset(self.permissions):
            raise AuthorizationError("actor lacks the operation's project permissions")


class ProjectAuthorizer(Protocol):
    """Adapter boundary: the caller authenticates before invoking this port."""

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess: ...
