"""Pure contracts for NAGAR CONNECT's controlled external boundary.

The models describe an authenticated connection; they do not authenticate a
secret, persist state, mint a grant, or execute a command. Runtime adapters
must resolve the principal through the existing authorizer and delegate writes
to the canonical CommandBus.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError


def _opaque_identifier(value: str) -> str:
    if not value.strip() or value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError("identifier must be a non-blank printable value")
    return value


class ConnectionState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CredentialState(StrEnum):
    ACTIVE = "active"
    ROTATING = "rotating"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ScopeGrant(BaseModel):
    """Project-bound, read-oriented scope metadata.

    A scope is intentionally a data contract. It is not a permission grant;
    the canonical ProjectAuthorizer remains the authority at execution time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_ids: tuple[str, ...] = Field(min_length=1)
    scopes: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _validate_ids(self) -> ScopeGrant:
        if any(not value.strip() or ".." in value or "/" in value for value in self.project_ids):
            raise ValueError("project ids must be opaque local identifiers")
        if any(not value.strip() or value != value.strip() for value in self.scopes):
            raise ValueError("scopes must be non-blank identifiers")
        return self

    def allows(self, project_id: str, scope: str) -> bool:
        return project_id in self.project_ids and scope in self.scopes


class CredentialDescriptor(BaseModel):
    """Non-secret credential metadata; raw credential material never enters this model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    credential_id: str = Field(min_length=1, max_length=128)
    state: CredentialState
    expires_at: datetime | None = None
    rotated_from: str | None = None
    last_used_at: datetime | None = None

    _credential_id = field_validator("credential_id")(_opaque_identifier)

    @model_validator(mode="after")
    def _reject_expired_state_mismatch(self) -> CredentialDescriptor:
        for name in ("expires_at", "last_used_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        return self

    def is_usable(self, now: datetime | None = None) -> bool:
        instant = now or datetime.now(timezone.utc)
        if self.state is not CredentialState.ACTIVE:
            return False
        return self.expires_at is None or instant < self.expires_at


class ConnectionContract(BaseModel):
    """External connection metadata bound to canonical NEXUS ActorIdentity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str = Field(min_length=1, max_length=128)
    principal: ActorIdentity
    grant: ScopeGrant
    credential: CredentialDescriptor
    state: ConnectionState
    expires_at: datetime | None = None
    created_at: datetime
    last_used_at: datetime | None = None

    _connection_id = field_validator("connection_id")(_opaque_identifier)

    @model_validator(mode="after")
    def _validate_times(self) -> ConnectionContract:
        for name in ("created_at", "expires_at", "last_used_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        return self

    def is_usable(self, now: datetime | None = None) -> bool:
        instant = now or datetime.now(timezone.utc)
        if self.state is not ConnectionState.ACTIVE:
            return False
        if self.expires_at is not None and instant >= self.expires_at:
            return False
        return self.credential.is_usable(instant)


def authorize_read(
    connection: ConnectionContract,
    *,
    access: ProjectAccess,
    project_id: str,
    scope: str,
    now: datetime | None = None,
) -> None:
    """Pre-check a scoped read against a *canonical* project grant.

    ``access`` must come from the existing ``ProjectAuthorizer``. The local
    connection metadata is only a narrower transport constraint; it cannot
    create or replace a grant and must never be used to dispatch a write.
    """

    if now is not None and now.tzinfo is None:
        raise PermissionError("request time must be timezone-aware")
    if not connection.is_usable(now):
        raise PermissionError("connection is inactive, expired, or revoked")
    try:
        access.authorize(connection.principal, project_id)
        access.require_permissions((scope,))
    except AuthorizationError as exc:
        raise PermissionError("canonical project authorization denied this read") from exc
    if not connection.grant.allows(project_id, scope):
        raise PermissionError("connection scope does not allow this project or operation")
