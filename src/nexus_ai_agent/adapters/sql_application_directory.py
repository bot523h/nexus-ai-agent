"""Durable application-owned principal and project authorization directory.

This is the storage adapter behind the transport-free principal vocabulary and
the studio's ``ProjectAuthorizer`` protocol.  Composition code may establish a
first-contact binding or membership.  Execution workers only call the read-only
``authorize``/``require_binding`` methods: IDs carried by a queue row are lookup
keys, never evidence of authority by themselves.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexus_ai_agent.application.principal import (
    BindingConflictError,
    ChannelBinding,
    PrincipalId,
)
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError
from nexus_ai_agent.storage.db import sync_engine
from nexus_ai_agent.storage.models import (
    AppChannelBinding,
    AppPrincipal,
    AppProject,
    AppProjectMembership,
)

DEFAULT_PROJECT_PERMISSIONS = frozenset({"project:read", "project:write"})
_PROJECT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]*")


def channel_binding_id(binding: ChannelBinding) -> str:
    """Stable opaque reference for an already validated channel binding."""

    digest = hashlib.sha256(f"{binding.channel}\x00{binding.external_id}".encode()).hexdigest()
    return f"cbn_{digest}"


def _validate_project_id(project_id: str) -> None:
    if (
        not isinstance(project_id, str)
        or len(project_id) > 128
        or not _PROJECT_ID.fullmatch(project_id)
        or ".." in project_id
    ):
        raise ValueError("project reference must be an opaque local identifier")


def _permissions(raw: str) -> frozenset[str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AuthorizationError("stored project membership permissions are malformed") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AuthorizationError("stored project membership permissions are malformed")
    return frozenset(value)


class SQLApplicationDirectory:
    """SQLite/PostgreSQL directory using the application's one sync engine."""

    def __init__(self, engine: Any | None = None) -> None:  # noqa: ANN401 - SQLAlchemy Engine
        self._engine = engine if engine is not None else sync_engine()

    @staticmethod
    def _binding_row(session: Session, binding: ChannelBinding) -> AppChannelBinding | None:
        statement = select(AppChannelBinding).where(
            AppChannelBinding.channel == binding.channel,  # type: ignore[arg-type]
            AppChannelBinding.external_id == binding.external_id,  # type: ignore[arg-type]
        )
        return session.execute(statement).scalars().first()

    def resolve(self, binding: ChannelBinding) -> PrincipalId | None:
        with Session(self._engine) as session:
            row = self._binding_row(session, binding)
            return PrincipalId(row.principal_id) if row is not None else None

    def resolve_or_create(self, binding: ChannelBinding) -> PrincipalId:
        """Resolve first contact atomically; a racing winner owns the binding."""

        existing = self.resolve(binding)
        if existing is not None:
            return existing
        principal = PrincipalId.new()
        try:
            with Session(self._engine) as session:
                with session.begin():
                    session.add(AppPrincipal(principal_id=str(principal)))
                    session.add(
                        AppChannelBinding(
                            binding_id=channel_binding_id(binding),
                            principal_id=str(principal),
                            channel=binding.channel,
                            external_id=binding.external_id,
                        )
                    )
            return principal
        except IntegrityError:
            # The UNIQUE(channel, external_id) constraint, not a Python lock,
            # arbitrates processes and hosts.  The losing transaction rolls
            # back its newly minted principal before reading the winner.
            winner = self.resolve(binding)
            if winner is not None:
                return winner
            raise

    def bind(self, principal: PrincipalId, binding: ChannelBinding) -> None:
        """Add a channel to an existing principal without silently moving it."""

        with Session(self._engine) as session:
            row = self._binding_row(session, binding)
            if row is not None:
                if row.principal_id == str(principal):
                    return
                raise BindingConflictError("binding already belongs to another principal")
            if session.get(AppPrincipal, str(principal)) is None:
                raise BindingConflictError("cannot bind a channel to an unknown principal")
        try:
            with Session(self._engine) as session:
                with session.begin():
                    session.add(
                        AppChannelBinding(
                            binding_id=channel_binding_id(binding),
                            principal_id=str(principal),
                            channel=binding.channel,
                            external_id=binding.external_id,
                        )
                    )
        except IntegrityError as exc:
            with Session(self._engine) as session:
                row = self._binding_row(session, binding)
            if row is not None and row.principal_id == str(principal):
                return
            raise BindingConflictError("binding already belongs to another principal") from exc

    def bindings_of(self, principal: PrincipalId) -> frozenset[ChannelBinding]:
        with Session(self._engine) as session:
            rows = (
                session.execute(
                    select(AppChannelBinding).where(
                        AppChannelBinding.principal_id == str(principal)  # type: ignore[arg-type]
                    )
                )
                .scalars()
                .all()
            )
        return frozenset(ChannelBinding(row.channel, row.external_id) for row in rows)

    def require_binding(self, binding_id: str, principal: PrincipalId) -> ChannelBinding:
        """Verify that a payload's binding reference belongs to ``principal``."""

        with Session(self._engine) as session:
            row = session.get(AppChannelBinding, binding_id)
        if row is None or row.principal_id != str(principal):
            raise AuthorizationError("channel binding is not bound to the acting principal")
        return ChannelBinding(row.channel, row.external_id)

    @staticmethod
    def _access(
        session: Session,
        membership: AppProjectMembership,
        *,
        required: frozenset[str] = frozenset(),
    ) -> ProjectAccess:
        if (
            session.get(AppPrincipal, membership.principal_id) is None
            or session.get(AppProject, membership.project_id) is None
        ):
            raise AuthorizationError("durable membership references an unknown principal/project")
        access = ProjectAccess(
            actor=ActorIdentity(kind="user", actor_id=membership.principal_id),
            project_id=membership.project_id,
            permissions=_permissions(membership.permissions_json),
        )
        access.require_permissions(tuple(sorted(required)))
        return access

    def ensure_project(
        self,
        project_id: str,
        principal: PrincipalId,
        *,
        permissions: frozenset[str] = DEFAULT_PROJECT_PERMISSIONS,
    ) -> ProjectAccess:
        """Composition-only first use; never widen an existing membership."""

        _validate_project_id(project_id)
        for _attempt in range(2):
            with Session(self._engine) as session:
                if session.get(AppPrincipal, str(principal)) is None:
                    raise AuthorizationError("cannot grant an unknown principal")
                membership = session.get(
                    AppProjectMembership,
                    (project_id, str(principal)),
                )
                if membership is not None:
                    return self._access(session, membership, required=permissions)
            try:
                with Session(self._engine) as session:
                    with session.begin():
                        if session.get(AppProject, project_id) is None:
                            session.add(AppProject(project_id=project_id))
                        session.add(
                            AppProjectMembership(
                                project_id=project_id,
                                principal_id=str(principal),
                                permissions_json=json.dumps(sorted(permissions)),
                            )
                        )
                with Session(self._engine) as session:
                    membership = session.get(AppProjectMembership, (project_id, str(principal)))
                    if membership is None:  # pragma: no cover - committed row must be visible
                        raise AuthorizationError("committed project membership is unavailable")
                    return self._access(session, membership, required=permissions)
            except IntegrityError:
                # Another first-use transaction created the project or this
                # membership.  Re-read it; never overwrite its permissions.
                continue
        raise AuthorizationError("concurrent project membership could not be resolved")

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        """Read-only trusted authorizer used before and during CommandBus dispatch."""

        if actor.kind != "user":
            raise AuthorizationError("application directory authorizes user principals only")
        try:
            principal = PrincipalId(actor.actor_id)
            _validate_project_id(project_id)
        except ValueError as exc:
            raise AuthorizationError("malformed canonical actor or project identity") from exc
        with Session(self._engine) as session:
            membership = session.get(AppProjectMembership, (project_id, str(principal)))
            if membership is None:
                raise AuthorizationError("actor is not a member of this project")
            return self._access(session, membership)


__all__ = [
    "DEFAULT_PROJECT_PERMISSIONS",
    "SQLApplicationDirectory",
    "channel_binding_id",
]
