"""Trusted canonical-authority seam for the IBPC slice (Phase 1).

The IBPC service must never treat a session's ``principal_id``/``project_id``
as an authorization grant, and it must never mint a channel binding id.  It
resolves the acting identity through an **injected** :class:`CanonicalAuthorityPort`
supplied by server-side composition (the same ``SQLApplicationDirectory`` the
canonical worker re-reads independently), and it fails closed with a typed
:class:`CanonicalAuthorityError` when the principal is unknown, is not a member
of the project, or the channel binding does not belong to the acting principal.

``DirectoryAuthority`` is a boundary-safe concrete adapter: it wraps any object
exposing the directory protocol (``authorize`` / ``require_binding``) that the
composition root passes in.  It deliberately imports neither
``nexus_ai_agent.adapters`` nor the studio bus/authorization modules, so
``tests/architecture/test_ibpc_boundary.py`` stays green.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from nexus_ai_agent.application.principal import PrincipalId

__all__ = [
    "AuthorizationError",
    "AuthorityResolution",
    "CanonicalAuthorityError",
    "CanonicalAuthorityPort",
    "DirectoryAuthority",
    "MembershipRole",
    "MembershipStatus",
    "PrincipalFacts",
    "PrincipalRoleAuthority",
    "PrincipalRoleFacts",
    "PrincipalStatus",
    "BindingStatus",
    "StudioAction",
    "StudioAuthorizationDecision",
    "StudioAuthorizationError",
    "StudioAuthorizerPort",
    "StudioRole",
]


class AuthorizationError(PermissionError):
    """Typed, fail-closed authorization or resource-boundary refusal."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class StudioRole(str, Enum):
    OWNER = "OWNER"
    EDITOR = "EDITOR"
    VIEWER = "VIEWER"
    AUDITOR = "AUDITOR"


class StudioAction(str, Enum):
    READ_SESSION = "READ_SESSION"
    READ_JOB = "READ_JOB"
    READ_AUDIT = "READ_AUDIT"
    SUBMIT_INTENT = "SUBMIT_INTENT"
    APPROVE_PLAN = "APPROVE_PLAN"
    EXECUTE_PLAN = "EXECUTE_PLAN"
    #: Worker-side spelling of "execute the plan this approval was bound to".
    #: It is an *alias member* of ``EXECUTE_PLAN`` (same value, same ROLE_ACTIONS
    #: membership) so a worker proof and an RBAC decision can never disagree.
    EXECUTE_APPROVED_PLAN = "EXECUTE_PLAN"
    MANAGE_CONSENT = "MANAGE_CONSENT"
    MANAGE_MEMBERSHIP = "MANAGE_MEMBERSHIP"
    CLOSE_SESSION = "CLOSE_SESSION"


class StudioAuthorizationDecision(BaseModel):
    """Typed, auditable authorization decision produced by Studio principal RBAC."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    principal_id: str
    project_id: str
    role: str
    action: str
    reason: str
    binding_ref: str = ""
    decided_at_utc: str = ""


class StudioAuthorizationError(AuthorizationError):
    """Fail-closed authorization refusal carrying the full structured decision."""

    def __init__(self, decision: StudioAuthorizationDecision) -> None:
        super().__init__(
            decision.reason,
            f"principal={decision.principal_id} action={decision.action}",
        )
        self.decision = decision


@runtime_checkable
class StudioAuthorizerPort(Protocol):
    """Port for enforcing project membership + role authorization in Studio/IBPC."""

    def authorize_action(
        self,
        *,
        principal_id: str,
        project_id: str,
        action: StudioAction,
        binding_ref: str = "",
    ) -> StudioAuthorizationDecision: ...


class CanonicalAuthorityError(ValueError):
    """Typed refusal: the acting identity could not be trusted. Never a crash."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PrincipalFacts:
    """Server-verified identity the queue payload may carry.

    These are *references* revalidated by the canonical worker, never grants:
    ``channel_binding_id`` is the canonical ``cbn_<64hex>`` reference for the
    binding the acting principal actually owns.
    """

    principal_id: str
    project_id: str
    channel_binding_id: str


@runtime_checkable
class CanonicalAuthorityPort(Protocol):
    """Resolve and verify the acting principal for one project + binding."""

    def resolve_principal(
        self,
        *,
        principal_id: str,
        project_id: str,
        binding_ref: str,
    ) -> PrincipalFacts: ...


class DirectoryAuthority:
    """Reference adapter over a durable application directory.

    ``directory`` is injected by the composition root (e.g. an
    ``SQLApplicationDirectory``); this class never imports it.  It performs the
    same two checks the canonical worker performs independently:
    project membership (``authorize``) and binding ownership
    (``require_binding``).
    """

    def __init__(self, directory: object) -> None:
        self._directory = directory

    def resolve_principal(
        self,
        *,
        principal_id: str,
        project_id: str,
        binding_ref: str,
    ) -> PrincipalFacts:
        from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError

        if not binding_ref:
            raise CanonicalAuthorityError(
                "missing_binding", "no channel binding was established for this session"
            )
        try:
            principal = PrincipalId(principal_id)
        except ValueError as exc:
            raise CanonicalAuthorityError(
                "malformed_principal", f"{principal_id!r} is not a canonical principal"
            ) from exc
        actor = ActorIdentity(kind="user", actor_id=str(principal))
        try:
            self._directory.authorize(actor, project_id)  # type: ignore[attr-defined]
            self._directory.require_binding(binding_ref, principal)  # type: ignore[attr-defined]
        except AuthorizationError as exc:
            raise CanonicalAuthorityError("authorization_denied", str(exc)) from exc
        except ValueError as exc:
            raise CanonicalAuthorityError("malformed_reference", str(exc)) from exc
        return PrincipalFacts(
            principal_id=str(principal),
            project_id=project_id,
            channel_binding_id=binding_ref,
        )


# --------------------------------------------------------------------------- #
# Worker authority facts (Wave 4)
# --------------------------------------------------------------------------- #
#
# The *worker* path (``IBPCService`` execution, the canonical render lane) does
# not merely ask "is this principal a member?" — it has to know the durability
# status of the three rows a membership decision rests on (principal, project
# membership, channel binding) and the exact role that granted execution.  The
# three closed vocabularies below are the durable spellings those rows use; they
# are declared here (the authority seam) so an adapter store and the worker
# reader can never disagree about a status string by accident.
#
# They live in this module — not in ``adapters`` — because ``nagar.ibpc`` must
# stay free of adapter imports (``tests/architecture/test_ibpc_boundary.py``).


class PrincipalStatus(str, Enum):
    """Durable lifecycle of one principal row."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class MembershipStatus(str, Enum):
    """Durable lifecycle of one project membership row."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class BindingStatus(str, Enum):
    """Durable lifecycle of one channel-binding row."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


class MembershipRole(str, Enum):
    """The role a membership grants, in the worker's own vocabulary.

    Deliberately a *distinct* closed set from :class:`StudioRole` even though the
    four ids coincide today: ``StudioRole`` names the HTTP/RBAC surface and may
    grow a surface-only role, while ``MembershipRole`` names what a durable
    membership row on disk is allowed to say.  ``MembershipRole.from_studio``
    is the one sanctioned bridge between the two.
    """

    OWNER = "OWNER"
    EDITOR = "EDITOR"
    VIEWER = "VIEWER"
    AUDITOR = "AUDITOR"

    def to_studio(self) -> StudioRole:
        """The RBAC role this membership role denotes."""
        return StudioRole(self.value)

    @classmethod
    def from_studio(cls, role: StudioRole | str) -> MembershipRole:
        """Parse a stored/RBAC role spelling, failing closed on anything else."""
        if isinstance(role, StudioRole):
            return cls(role.value)
        if isinstance(role, str):
            try:
                return cls(role.strip().upper())
            except ValueError as exc:
                raise CanonicalAuthorityError(
                    "unknown_membership_role", f"{role!r} is not a membership role"
                ) from exc
        raise CanonicalAuthorityError(
            "unknown_membership_role", f"{type(role).__name__} is not a membership role"
        )


@dataclass(frozen=True)
class PrincipalRoleFacts:
    """Worker-verified role facts for one (principal, project, tenant) triple.

    Every field is a *read* of the durable store inside one snapshot: the
    statuses are the stored spellings, ``permitted_actions`` is the closed
    permission set the role grants, and ``verified_at_utc`` is when the read
    happened.  The facts are evidence, never a grant — the caller still passes
    them to its own gate.
    """

    principal_id: str
    project_id: str
    tenant_id: str
    binding_ref: str
    role: MembershipRole
    principal_status: PrincipalStatus
    membership_status: MembershipStatus
    binding_status: BindingStatus
    permitted_actions: tuple[StudioAction, ...]
    verified_at_utc: str

    @property
    def may_execute(self) -> bool:
        """True only when all three rows are ACTIVE (the fail-closed triple)."""
        return (
            self.principal_status is PrincipalStatus.ACTIVE
            and self.membership_status is MembershipStatus.ACTIVE
            and self.binding_status is BindingStatus.ACTIVE
        )


@runtime_checkable
class PrincipalRoleAuthority(Protocol):
    """Resolve the durable role facts the worker re-checks before execution."""

    def resolve_principal_role(
        self,
        principal_id: str,
        project_id: str,
        *,
        binding_ref: str = "",
        tenant_id: str = "",
    ) -> PrincipalRoleFacts: ...


#: The worker's full authority resolution *is* the principal/role fact set:
#: the four-way decision (principal, project tenant, membership role, binding)
#: carries nothing that ``PrincipalRoleFacts`` omits, so the alias is exact
#: rather than a second, drifting shape.
AuthorityResolution = PrincipalRoleFacts
