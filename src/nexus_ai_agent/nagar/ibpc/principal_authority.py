"""Durable Studio principal authority: principal → binding → membership → action.

The IBPC service must never treat a ``principal_id``/``project_id`` pair as a
grant (Phase 1 lesson), and a *studio* action needs one more link than plain
project membership: the principal must be reachable through an
**authenticated channel binding**, must hold a **role** on the target project,
and that role must be authorized for the **exact action** being attempted.

This module makes the whole chain durable and mechanical::

    AppPrincipal -> AppChannelBinding -> ProjectMembership -> StudioActionPermission

Storage is one SQLite file in WAL mode with ``BEGIN IMMEDIATE`` writes, and the
four tables mirror the application-owned identity model
(``storage.models.AppPrincipal`` / ``AppChannelBinding`` /
``AppProjectMembership``) without importing it: ``nagar.ibpc`` may not import the
execution/authority surfaces (``tests/architecture/test_ibpc_boundary.py``), so
this service owns its own minimal projection of the same vocabulary and is
wired by the composition root as a
:class:`~nexus_ai_agent.nagar.ibpc.authority.CanonicalAuthorityPort`.

Authorization semantics:

* roles are a closed set — ``OWNER`` > ``EDITOR`` > ``VIEWER``, plus the
  read-only ``AUDITOR``;
* ``StudioAction`` is a closed set of IBPC/Studio actions, each with the
  minimum role(s) that may perform it;
* every check fails closed with :class:`StudioAuthorizationError` (never a bare
  ``False`` that a caller could ignore) and the reason is a stable literal, so
  an audit can distinguish "unknown principal" from "wrong role" from
  "binding not owned";
* a binding that belongs to another principal is refused — possession of a
  binding reference is never evidence of identity (cross-user IDOR);
* a membership row for a *different* project never authorizes the target
  project (cross-project IDOR).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from nexus_ai_agent.nagar.ibpc.approval import (
    ApprovalBindingPayload,
    ApprovalError,
    ApprovalIdentity,
    ApprovalLedger,
)
from nexus_ai_agent.nagar.ibpc.models import ApprovalBinding
from nexus_ai_agent.nagar.ibpc.privacy import ConsentAction, ConsentScope, PrivacyViolation

__all__ = [
    "ACTION_ROLES",
    "AuthorizationRefusalError",
    "MEMBERSHIP_ROLES",
    "PROJECT_GOVERNANCE_ROLES",
    "DurableStudioAuthorizationService",
    "MembershipRole",
    "PrincipalFacts",
    "StudioAction",
    "StudioAuthorizationError",
    "StudioAuthorizationPort",
    "durable_studio_authorization_path",
]

#: ``busy_timeout`` for every connection opened by the durable authorization store.
_BUSY_TIMEOUT_MS = 5000


class StudioAuthorizationError(PermissionError):
    """Typed refusal: the principal may not perform this action on this project."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class AuthorizationRefusalError(StudioAuthorizationError):
    """Typed refusal: role authority is required but could not be established.

    Raised by the composition root when a durable service is built without a
    principal authority, and by the service when a role-gated action is attempted
    with no verified role (Wave 3-R2b).  It is a
    :class:`StudioAuthorizationError`, so every existing ``except`` keeps working.
    """


class MembershipRole(str, Enum):
    """Project membership ladder (closed set, least privilege first)."""

    VIEWER = "VIEWER"
    AUDITOR = "AUDITOR"
    EDITOR = "EDITOR"
    OWNER = "OWNER"


class StudioAction(str, Enum):
    """Every Studio/IBPC action that requires project membership."""

    SESSION_START = "SESSION_START"
    TURN_CREATE = "TURN_CREATE"
    PREVIEW_GENERATE = "PREVIEW_GENERATE"
    APPROVAL_CONSUME = "APPROVAL_CONSUME"
    REVISION_RESTORE = "REVISION_RESTORE"
    PROJECT_READ = "PROJECT_READ"
    AUDIT_READ = "AUDIT_READ"


#: The minimum roles that may perform one action.  ``OWNER`` is deliberately the
#: only role for destructive/revision actions; ``AUDITOR`` is read-only.
ACTION_ROLES: dict[StudioAction, frozenset[MembershipRole]] = {
    StudioAction.SESSION_START: frozenset({MembershipRole.OWNER, MembershipRole.EDITOR}),
    StudioAction.TURN_CREATE: frozenset({MembershipRole.OWNER, MembershipRole.EDITOR}),
    StudioAction.PREVIEW_GENERATE: frozenset({MembershipRole.OWNER, MembershipRole.EDITOR}),
    StudioAction.APPROVAL_CONSUME: frozenset({MembershipRole.OWNER, MembershipRole.EDITOR}),
    StudioAction.REVISION_RESTORE: frozenset({MembershipRole.OWNER}),
    StudioAction.PROJECT_READ: frozenset(MembershipRole),
    StudioAction.AUDIT_READ: frozenset(
        {MembershipRole.OWNER, MembershipRole.EDITOR, MembershipRole.AUDITOR}
    ),
}

#: Every role that may hold or modify a project's governance records.
PROJECT_GOVERNANCE_ROLES = frozenset({MembershipRole.OWNER})

#: Every role in the closed set (audit / iteration seam).
MEMBERSHIP_ROLES = frozenset(MembershipRole)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS studio_principals ("
    " principal_id TEXT PRIMARY KEY, display_name TEXT, created_at_utc TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS studio_channel_bindings ("
    " binding_id TEXT PRIMARY KEY,"
    " principal_id TEXT NOT NULL REFERENCES studio_principals(principal_id),"
    " channel TEXT NOT NULL,"
    " external_id TEXT NOT NULL,"
    " created_at_utc TEXT NOT NULL,"
    " UNIQUE(channel, external_id))",
    "CREATE TABLE IF NOT EXISTS studio_projects ("
    " project_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS studio_memberships ("
    " project_id TEXT NOT NULL REFERENCES studio_projects(project_id),"
    " principal_id TEXT NOT NULL REFERENCES studio_principals(principal_id),"
    " role TEXT NOT NULL CHECK(role IN ('OWNER', 'EDITOR', 'VIEWER', 'AUDITOR')),"
    " created_at_utc TEXT NOT NULL,"
    " PRIMARY KEY (project_id, principal_id))",
)


@dataclass(frozen=True)
class PrincipalFacts:
    """The verified identity of one acting principal on one project."""

    principal_id: str
    project_id: str
    channel_binding_id: str
    role: MembershipRole


@dataclass(frozen=True)
class MembershipRecord:
    project_id: str
    principal_id: str
    role: MembershipRole
    created_at_utc: str


class StudioAuthorizationPort:
    """Protocol-ish seam: any object with ``resolve_principal`` satisfies it."""

    def resolve_principal(
        self, *, principal_id: str, project_id: str, binding_ref: str
    ) -> PrincipalFacts:  # pragma: no cover - structural seam
        raise NotImplementedError


class DurableStudioAuthorizationService:
    """SQLite-WAL authority for principal → binding → membership → action."""

    def __init__(self, db_path: str | Path, *, now: Callable[[], str]) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._transaction():
            for statement in _SCHEMA:
                self._conn.execute(statement)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if self._depth > 0:
                yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._depth = 0
                self._conn.execute("ROLLBACK")
                raise
            self._depth = 0
            self._conn.execute("COMMIT")

    # -- provisioning (composition root only) --------------------------------

    def register_principal(self, principal_id: str, *, display_name: str = "") -> None:
        """Idempotently register a canonical principal."""
        if not principal_id:
            raise StudioAuthorizationError("invalid_principal", "a principal id is required")
        with self._transaction():
            self._conn.execute(
                "INSERT INTO studio_principals (principal_id, display_name, created_at_utc)"
                " VALUES (?, ?, ?) ON CONFLICT(principal_id) DO NOTHING",
                (principal_id, display_name, self._now()),
            )

    def bind_channel(self, *, principal_id: str, channel: str, external_id: str) -> str:
        """Bind one external channel account to a principal; returns the binding id."""
        if not channel or not external_id:
            raise StudioAuthorizationError(
                "invalid_binding", "channel and external_id are required"
            )
        with self._transaction():
            if self._fetch_principal(principal_id) is None:
                raise StudioAuthorizationError(
                    "unknown_principal", "cannot bind a channel to an unknown principal"
                )
            row = self._conn.execute(
                "SELECT binding_id, principal_id FROM studio_channel_bindings"
                " WHERE channel = ? AND external_id = ?",
                (channel, external_id),
            ).fetchone()
            if row is not None:
                if row[1] != principal_id:
                    raise StudioAuthorizationError(
                        "binding_conflict", "the channel account is bound to another principal"
                    )
                return str(row[0])
            binding_id = f"cbn_{uuid.uuid4().hex}{uuid.uuid4().hex}"
            self._conn.execute(
                "INSERT INTO studio_channel_bindings (binding_id, principal_id, channel,"
                " external_id, created_at_utc) VALUES (?, ?, ?, ?, ?)",
                (binding_id, principal_id, channel, external_id, self._now()),
            )
            return binding_id

    def grant_membership(self, *, project_id: str, principal_id: str, role: MembershipRole) -> None:
        """Grant (or change) one principal's role on one project."""
        if not isinstance(role, MembershipRole):
            raise StudioAuthorizationError("invalid_role", f"{role!r} is not a MembershipRole")
        if not project_id:
            raise StudioAuthorizationError("invalid_project", "a project id is required")
        with self._transaction():
            if self._fetch_principal(principal_id) is None:
                raise StudioAuthorizationError(
                    "unknown_principal", "cannot grant membership to an unknown principal"
                )
            self._conn.execute(
                "INSERT INTO studio_projects (project_id, created_at_utc) VALUES (?, ?)"
                " ON CONFLICT(project_id) DO NOTHING",
                (project_id, self._now()),
            )
            self._conn.execute(
                "INSERT INTO studio_memberships (project_id, principal_id, role, created_at_utc)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(project_id, principal_id) DO UPDATE SET role=excluded.role",
                (project_id, principal_id, role.value, self._now()),
            )

    def revoke_membership(self, *, project_id: str, principal_id: str) -> None:
        with self._transaction():
            cursor = self._conn.execute(
                "DELETE FROM studio_memberships WHERE project_id = ? AND principal_id = ?",
                (project_id, principal_id),
            )
            if cursor.rowcount != 1:
                raise StudioAuthorizationError(
                    "unknown_membership", f"{principal_id} is not a member of {project_id}"
                )

    # -- authorization -------------------------------------------------------

    def resolve_principal(
        self, *, principal_id: str, project_id: str, binding_ref: str
    ) -> PrincipalFacts:
        """Verify the full chain and return the acting principal's facts.

        Fails closed with a typed :class:`StudioAuthorizationError`:
        ``unknown_principal``, ``missing_binding``, ``unknown_binding``,
        ``binding_owner_mismatch`` (cross-user IDOR), ``not_a_member``
        (cross-project IDOR).
        """
        with self._transaction():
            if self._fetch_principal(principal_id) is None:
                raise StudioAuthorizationError("unknown_principal", principal_id)
            if not binding_ref:
                raise StudioAuthorizationError(
                    "missing_binding", "no channel binding was presented"
                )
            binding = self._conn.execute(
                "SELECT principal_id FROM studio_channel_bindings WHERE binding_id = ?",
                (binding_ref,),
            ).fetchone()
            if binding is None:
                raise StudioAuthorizationError("unknown_binding", binding_ref)
            if binding[0] != principal_id:
                raise StudioAuthorizationError(
                    "binding_owner_mismatch", "the binding belongs to another principal"
                )
            membership = self._fetch_membership(project_id, principal_id)
            if membership is None:
                raise StudioAuthorizationError(
                    "not_a_member", f"{principal_id} is not a member of {project_id}"
                )
            return PrincipalFacts(
                principal_id=principal_id,
                project_id=project_id,
                channel_binding_id=binding_ref,
                role=membership.role,
            )

    def authorize_action(
        self,
        *,
        principal_id: str,
        project_id: str,
        binding_ref: str,
        action: StudioAction,
    ) -> PrincipalFacts:
        """Resolve the identity, then require the action's minimum role."""
        if not isinstance(action, StudioAction):
            raise StudioAuthorizationError("invalid_action", f"{action!r} is not a StudioAction")
        facts = self.resolve_principal(
            principal_id=principal_id, project_id=project_id, binding_ref=binding_ref
        )
        allowed = ACTION_ROLES[action]
        if facts.role not in allowed:
            raise StudioAuthorizationError(
                "insufficient_role",
                f"{action.value} requires one of {sorted(r.value for r in allowed)},"
                f" principal holds {facts.role.value}",
            )
        return facts

    # -- reads ---------------------------------------------------------------

    def get_membership(self, *, project_id: str, principal_id: str) -> MembershipRecord:
        record = self._fetch_membership(project_id, principal_id)
        if record is None:
            raise StudioAuthorizationError(
                "not_a_member", f"{principal_id} is not a member of {project_id}"
            )
        return record

    def members(self, project_id: str) -> tuple[MembershipRecord, ...]:
        rows = self._conn.execute(
            "SELECT project_id, principal_id, role, created_at_utc FROM studio_memberships"
            " WHERE project_id = ? ORDER BY principal_id",
            (project_id,),
        ).fetchall()
        return tuple(
            MembershipRecord(row[0], row[1], MembershipRole(row[2]), row[3]) for row in rows
        )

    def bindings_of(self, principal_id: str) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT binding_id FROM studio_channel_bindings WHERE principal_id = ?"
            " ORDER BY binding_id",
            (principal_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    # -- internals -----------------------------------------------------------

    def _fetch_principal(self, principal_id: str) -> Any | None:
        return self._conn.execute(
            "SELECT principal_id FROM studio_principals WHERE principal_id = ?",
            (principal_id,),
        ).fetchone()

    def _fetch_membership(self, project_id: str, principal_id: str) -> MembershipRecord | None:
        row = self._conn.execute(
            "SELECT project_id, principal_id, role, created_at_utc FROM studio_memberships"
            " WHERE project_id = ? AND principal_id = ?",
            (project_id, principal_id),
        ).fetchone()
        if row is None:
            return None
        return MembershipRecord(row[0], row[1], MembershipRole(row[2]), row[3])


# --------------------------------------------------------------------------- #
# Attachment: authoritative principal gate for IBPC mutations
# --------------------------------------------------------------------------- #
#
# The gate below is the object the IBPC service holds.  It composes the durable
# authorization service with the durable canonical approval ledger and the
# durable consent ledger, so ONE call verifies membership + role + canonical
# approval binding + processing consent for the exact action being attempted.
# It also guarantees the canonical binding payload can never disagree with the
# session's own identity (project/session/turn/base-revision/principal).


@dataclass(frozen=True)
class PrincipalAuthorization:
    """Verified facts for one authorized IBPC mutation."""

    facts: PrincipalFacts
    action: StudioAction


class IBPCAuthorizationGate:
    """Compose membership, canonical approval binding and consent for IBPC.

    ``authorize`` is the single entry point every session start, turn creation,
    preview generation, approval consumption and revision restore goes through.
    """

    def __init__(
        self,
        *,
        authorization: DurableStudioAuthorizationService,
        approvals: ApprovalLedger,
        binding_ref: str,
    ) -> None:
        self._authorization = authorization
        self._approvals = approvals
        self._binding_ref = binding_ref

    @property
    def binding_ref(self) -> str:
        return self._binding_ref

    def authorize(
        self,
        *,
        principal_id: str,
        project_id: str,
        action: StudioAction,
        binding_ref: str | None = None,
    ) -> PrincipalFacts:
        """Membership + role check for one action (no approval needed)."""
        return self._authorization.authorize_action(
            principal_id=principal_id,
            project_id=project_id,
            binding_ref=self._binding_ref if binding_ref is None else binding_ref,
            action=action,
        )

    def authorize_consume(
        self,
        *,
        principal_id: str,
        project_id: str,
        approval_id: str,
        identity: ApprovalIdentity,
        payload: ApprovalBindingPayload,
        job_id: str | None = None,
    ) -> ApprovalBinding:
        """Authorize + consume exactly once, verifying the canonical binding.

        Order: membership/role (``APPROVAL_CONSUME``) → canonical payload must
        agree with the session's own identity → atomic canonical consume.
        """
        facts = self._authorization.authorize_action(
            principal_id=principal_id,
            project_id=project_id,
            binding_ref=self._binding_ref,
            action=StudioAction.APPROVAL_CONSUME,
        )
        if (
            payload.project_id != facts.project_id
            or payload.principal_id != facts.principal_id
            or payload.session_id != identity.session_id
            or payload.turn_id != identity.turn_id
        ):
            raise StudioAuthorizationError(
                "binding_identity_mismatch",
                "the canonical approval binding does not match the authorized identity",
            )
        consume = getattr(self._approvals, "consume_approval", None)
        if consume is None:
            raise ApprovalError(
                "canonical_binding_unavailable",
                "the approval ledger does not enforce canonical bindings",
            )
        return consume(approval_id, identity=identity, payload=payload, job_id=job_id)

    def require_processing_consent(
        self,
        *,
        consent_store: Any,
        principal_id: str,
        project_id: str,
        provider_id: str,
        data_category: ConsentScope,
        action: ConsentAction,
    ) -> Any:
        """Delegate to the durable consent gate; raises ``PrivacyViolation``."""
        evaluate = getattr(consent_store, "evaluate_processing_consent", None)
        if evaluate is None:
            raise PrivacyViolation(
                "consent_store_unavailable", "no durable consent ledger is configured"
            )
        return evaluate(
            principal_id=principal_id,
            project_id=project_id,
            provider_id=provider_id,
            data_category=data_category,
            action=action,
        )


def durable_studio_authorization_path(workspace: str | Path) -> Path:
    """Canonical SQLite location for the durable studio authorization store."""
    return Path(workspace) / "queue" / "studio_authorization.sqlite3"
