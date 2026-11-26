"""Durable principal identity, channel binding & project-membership RBAC (Wave 3.4).

Implements ``AppPrincipal -> AppChannelBinding -> ProjectMembership -> StudioAuthorizationDecision``
backed by SQLite WAL (``BEGIN IMMEDIATE``, ``busy_timeout=5000``,
``foreign_keys=ON``, ``synchronous=FULL``) and enforces fail-closed role RBAC
across Studio API routes and ``IBPCService`` via :class:`StudioAuthorizerPort`
and :class:`CanonicalAuthorityPort`.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nexus_ai_agent.application.ports.job_queue import JobQueuePort
from nexus_ai_agent.application.principal import PrincipalId
from nexus_ai_agent.creative.studio.capabilities import CapabilityRegistry
from nexus_ai_agent.nagar.ibpc.approval import (
    DurableApprovalLedger,
    durable_approval_ledger_path,
)
from nexus_ai_agent.nagar.ibpc.approval_mac import KeyProvider
from nexus_ai_agent.nagar.ibpc.authority import (
    CanonicalAuthorityError,
    CanonicalAuthorityPort,
    PrincipalFacts,
    StudioAction,
    StudioAuthorizationDecision,
    StudioAuthorizationError,
    StudioAuthorizerPort,
    StudioRole,
)
from nexus_ai_agent.nagar.ibpc.composition import build_ibpc_service
from nexus_ai_agent.nagar.ibpc.privacy import (
    DurablePrivacyConsentStore,
    durable_privacy_consent_path,
)
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps, utc_now
from nexus_ai_agent.nagar.ibpc.session import (
    DurableLiveSessionStore,
    durable_session_store_path,
)

ROLE_ACTIONS: dict[StudioRole, frozenset[StudioAction]] = {
    StudioRole.OWNER: frozenset(StudioAction),
    StudioRole.EDITOR: frozenset(
        {
            StudioAction.READ_SESSION,
            StudioAction.READ_JOB,
            StudioAction.SUBMIT_INTENT,
            StudioAction.APPROVE_PLAN,
            StudioAction.EXECUTE_PLAN,
            StudioAction.MANAGE_CONSENT,
            StudioAction.CLOSE_SESSION,
        }
    ),
    StudioRole.VIEWER: frozenset(
        {
            StudioAction.READ_SESSION,
            StudioAction.READ_JOB,
        }
    ),
    StudioRole.AUDITOR: frozenset(
        {
            StudioAction.READ_SESSION,
            StudioAction.READ_JOB,
            StudioAction.READ_AUDIT,
        }
    ),
}


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _binding_ref_for(channel: str, external_subject: str) -> str:
    digest = hashlib.sha256(f"{channel}:{external_subject}".encode()).hexdigest()
    return f"cbn_{digest}"


class PrincipalAuthenticationError(PermissionError):
    """Fail-closed authentication refusal for missing/invalid/revoked tokens."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


class StudioPrincipalAuthStore(StudioAuthorizerPort, CanonicalAuthorityPort):
    """SQLite WAL durable principal, channel binding, and project membership store."""

    def __init__(self, path: str | Path, *, now: Callable[[], str] = utc_now) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._conn = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._transaction():
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_studio_principals (
                    principal_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    token_sha256 TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at_utc TEXT NOT NULL,
                    revoked_at_utc TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_studio_channel_bindings (
                    binding_ref TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL
                        REFERENCES nexus_studio_principals(principal_id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    external_subject TEXT NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT 0,
                    chat_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at_utc TEXT NOT NULL,
                    revoked_at_utc TEXT NOT NULL DEFAULT '',
                    UNIQUE (channel, external_subject)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_studio_projects (
                    project_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    active_revision_id TEXT NOT NULL DEFAULT '0',
                    created_at_utc TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_studio_project_memberships (
                    membership_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL
                        REFERENCES nexus_studio_projects(project_id) ON DELETE CASCADE,
                    principal_id TEXT NOT NULL
                        REFERENCES nexus_studio_principals(principal_id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    granted_by TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL,
                    revoked_at_utc TEXT NOT NULL DEFAULT '',
                    UNIQUE (project_id, principal_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_studio_auth_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    principal_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    role TEXT NOT NULL,
                    allowed INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    binding_ref TEXT NOT NULL DEFAULT '',
                    decided_at_utc TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_studio_auth_audit_no_update
                BEFORE UPDATE ON nexus_studio_auth_audit
                BEGIN
                    SELECT RAISE(ABORT, 'studio auth audit is append-only');
                END
                """
            )
            self._conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_studio_auth_audit_no_delete
                BEFORE DELETE ON nexus_studio_auth_audit
                BEGIN
                    SELECT RAISE(ABORT, 'studio auth audit is append-only');
                END
                """
            )
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def register_principal(
        self,
        principal_id: str,
        *,
        tenant_id: str,
        raw_token: str,
        display_name: str = "",
    ) -> str:
        if not raw_token or len(raw_token) < 8:
            raise ValueError("principal raw_token must be at least 8 characters")
        PrincipalId(principal_id)
        token_sha = _hash_token(raw_token)
        now_utc = self._now()
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO nexus_studio_principals (
                    principal_id, tenant_id, display_name, token_sha256,
                    status, created_at_utc, revoked_at_utc
                ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, '')
                ON CONFLICT(principal_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    display_name = excluded.display_name,
                    token_sha256 = excluded.token_sha256,
                    status = 'ACTIVE',
                    revoked_at_utc = ''
                """,
                (principal_id, tenant_id, display_name, token_sha, now_utc),
            )
        return principal_id

    def revoke_principal(self, principal_id: str) -> None:
        now_utc = self._now()
        with self._transaction():
            self._conn.execute(
                """
                UPDATE nexus_studio_principals
                SET status = 'REVOKED', revoked_at_utc = ?
                WHERE principal_id = ?
                """,
                (now_utc, principal_id),
            )

    def authenticate_token(self, raw_token: str | None) -> str:
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise PrincipalAuthenticationError(
                "missing_principal_token", "principal token required"
            )
        presented_digest = _hash_token(raw_token.strip())
        rows = self._conn.execute(
            "SELECT principal_id, token_sha256, status FROM nexus_studio_principals"
        ).fetchall()
        matched_id: str | None = None
        matched_status: str = ""
        for pid, stored_digest, status in rows:
            if hmac.compare_digest(presented_digest, str(stored_digest)):
                matched_id = str(pid)
                matched_status = str(status)
        if matched_id is None:
            raise PrincipalAuthenticationError("invalid_principal_token", "unknown principal token")
        if matched_status != "ACTIVE":
            raise PrincipalAuthenticationError(
                "principal_revoked", f"principal {matched_id} is {matched_status}"
            )
        return matched_id

    def bind_channel(
        self,
        principal_id: str,
        *,
        channel: str,
        external_subject: str,
        user_id: int = 0,
        chat_id: int = 0,
        binding_ref: str | None = None,
    ) -> str:
        ref = binding_ref or _binding_ref_for(channel, external_subject)
        now_utc = self._now()
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO nexus_studio_channel_bindings (
                    binding_ref, principal_id, channel, external_subject,
                    user_id, chat_id, status, created_at_utc, revoked_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, '')
                ON CONFLICT(binding_ref) DO UPDATE SET
                    principal_id = excluded.principal_id,
                    status = 'ACTIVE',
                    revoked_at_utc = ''
                """,
                (ref, principal_id, channel, external_subject, user_id, chat_id, now_utc),
            )
        return ref

    def revoke_channel_binding(self, binding_ref: str) -> None:
        now_utc = self._now()
        with self._transaction():
            self._conn.execute(
                """
                UPDATE nexus_studio_channel_bindings
                SET status = 'REVOKED', revoked_at_utc = ?
                WHERE binding_ref = ?
                """,
                (now_utc, binding_ref),
            )

    def ensure_project(
        self, project_id: str, *, tenant_id: str, active_revision_id: str = "0"
    ) -> None:
        now_utc = self._now()
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO nexus_studio_projects (
                    project_id, tenant_id, active_revision_id, created_at_utc
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id) DO NOTHING
                """,
                (project_id, tenant_id, active_revision_id, now_utc),
            )

    def grant_membership(
        self,
        project_id: str,
        principal_id: str,
        *,
        role: StudioRole | str,
        granted_by: str,
    ) -> str:
        resolved_role = role if isinstance(role, StudioRole) else StudioRole(role)
        now_utc = self._now()
        membership_id = f"mbr_{uuid.uuid4().hex}"
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO nexus_studio_project_memberships (
                    membership_id, project_id, principal_id, role, status,
                    granted_by, created_at_utc, revoked_at_utc
                ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, '')
                ON CONFLICT(project_id, principal_id) DO UPDATE SET
                    role = excluded.role,
                    status = 'ACTIVE',
                    granted_by = excluded.granted_by,
                    revoked_at_utc = ''
                """,
                (
                    membership_id,
                    project_id,
                    principal_id,
                    resolved_role.value,
                    granted_by,
                    now_utc,
                ),
            )
        return membership_id

    def revoke_membership(self, project_id: str, principal_id: str) -> None:
        now_utc = self._now()
        with self._transaction():
            self._conn.execute(
                """
                UPDATE nexus_studio_project_memberships
                SET status = 'REVOKED', revoked_at_utc = ?
                WHERE project_id = ? AND principal_id = ?
                """,
                (now_utc, project_id, principal_id),
            )

    def _record_decision(self, decision: StudioAuthorizationDecision) -> None:
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO nexus_studio_auth_audit (
                    principal_id, project_id, action, role, allowed, reason,
                    binding_ref, decided_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.principal_id,
                    decision.project_id,
                    decision.action,
                    decision.role,
                    1 if decision.allowed else 0,
                    decision.reason,
                    decision.binding_ref,
                    decision.decided_at_utc,
                ),
            )

    def authorize_action(
        self,
        *,
        principal_id: str,
        project_id: str,
        action: StudioAction,
        binding_ref: str = "",
    ) -> StudioAuthorizationDecision:
        now_utc = self._now()
        p_row = self._conn.execute(
            "SELECT status FROM nexus_studio_principals WHERE principal_id = ?",
            (principal_id,),
        ).fetchone()
        if p_row is None or p_row[0] != "ACTIVE":
            reason = "principal_not_found" if p_row is None else "principal_revoked"
            decision = StudioAuthorizationDecision(
                allowed=False,
                principal_id=principal_id,
                project_id=project_id,
                role="NONE",
                action=action.value,
                reason=reason,
                binding_ref=binding_ref,
                decided_at_utc=now_utc,
            )
            self._record_decision(decision)
            raise StudioAuthorizationError(decision)

        if binding_ref:
            b_row = self._conn.execute(
                """
                SELECT principal_id, status
                FROM nexus_studio_channel_bindings
                WHERE binding_ref = ?
                """,
                (binding_ref,),
            ).fetchone()
            if b_row is None or b_row[0] != principal_id or b_row[1] != "ACTIVE":
                decision = StudioAuthorizationDecision(
                    allowed=False,
                    principal_id=principal_id,
                    project_id=project_id,
                    role="NONE",
                    action=action.value,
                    reason="invalid_or_revoked_channel_binding",
                    binding_ref=binding_ref,
                    decided_at_utc=now_utc,
                )
                self._record_decision(decision)
                raise StudioAuthorizationError(decision)

        m_row = self._conn.execute(
            """
            SELECT role, status
            FROM nexus_studio_project_memberships
            WHERE project_id = ? AND principal_id = ?
            """,
            (project_id, principal_id),
        ).fetchone()
        if m_row is None or m_row[1] != "ACTIVE":
            reason = "not_a_project_member" if m_row is None else "membership_revoked"
            decision = StudioAuthorizationDecision(
                allowed=False,
                principal_id=principal_id,
                project_id=project_id,
                role=str(m_row[0]) if m_row is not None else "NONE",
                action=action.value,
                reason=reason,
                binding_ref=binding_ref,
                decided_at_utc=now_utc,
            )
            self._record_decision(decision)
            raise StudioAuthorizationError(decision)

        role = StudioRole(str(m_row[0]))
        if action not in ROLE_ACTIONS[role]:
            decision = StudioAuthorizationDecision(
                allowed=False,
                principal_id=principal_id,
                project_id=project_id,
                role=role.value,
                action=action.value,
                reason="insufficient_role",
                binding_ref=binding_ref,
                decided_at_utc=now_utc,
            )
            self._record_decision(decision)
            raise StudioAuthorizationError(decision)

        decision = StudioAuthorizationDecision(
            allowed=True,
            principal_id=principal_id,
            project_id=project_id,
            role=role.value,
            action=action.value,
            reason="authorized",
            binding_ref=binding_ref,
            decided_at_utc=now_utc,
        )
        self._record_decision(decision)
        return decision

    def resolve_principal(
        self,
        *,
        principal_id: str,
        project_id: str,
        binding_ref: str,
    ) -> PrincipalFacts:
        if not binding_ref:
            raise CanonicalAuthorityError(
                "missing_binding", "no channel binding was established for this session"
            )
        try:
            PrincipalId(principal_id)
        except ValueError as exc:
            raise CanonicalAuthorityError(
                "malformed_principal", f"{principal_id!r} is not a canonical principal"
            ) from exc
        try:
            self.authorize_action(
                principal_id=principal_id,
                project_id=project_id,
                action=StudioAction.READ_SESSION,
                binding_ref=binding_ref,
            )
        except StudioAuthorizationError as exc:
            raise CanonicalAuthorityError("authorization_denied", str(exc)) from exc
        return PrincipalFacts(
            principal_id=principal_id,
            project_id=project_id,
            channel_binding_id=binding_ref,
        )

    def default_binding_for(self, principal_id: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT binding_ref
            FROM nexus_studio_channel_bindings
            WHERE principal_id = ? AND status = 'ACTIVE'
            ORDER BY rowid ASC
            LIMIT 1
            """,
            (principal_id,),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def list_auth_audit(self, *, project_id: str | None = None) -> tuple[dict[str, Any], ...]:
        if project_id is not None:
            rows = self._conn.execute(
                """
                SELECT audit_id, principal_id, project_id, action, role,
                       allowed, reason, binding_ref, decided_at_utc
                FROM nexus_studio_auth_audit
                WHERE project_id = ?
                ORDER BY audit_id ASC
                """,
                (project_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT audit_id, principal_id, project_id, action, role,
                       allowed, reason, binding_ref, decided_at_utc
                FROM nexus_studio_auth_audit
                ORDER BY audit_id ASC
                """
            ).fetchall()
        return tuple(
            {
                "audit_id": int(r[0]),
                "principal_id": r[1],
                "project_id": r[2],
                "action": r[3],
                "role": r[4],
                "allowed": bool(r[5]),
                "reason": r[6],
                "binding_ref": r[7],
                "decided_at_utc": r[8],
            }
            for r in rows
        )


class ProjectMembershipAuthorizer(StudioAuthorizerPort):
    """Explicit authorizer facade over :class:`StudioPrincipalAuthStore`."""

    def __init__(self, store: StudioPrincipalAuthStore) -> None:
        self._store = store

    def authorize_action(
        self,
        *,
        principal_id: str,
        project_id: str,
        action: StudioAction,
        binding_ref: str = "",
    ) -> StudioAuthorizationDecision:
        return self._store.authorize_action(
            principal_id=principal_id,
            project_id=project_id,
            action=action,
            binding_ref=binding_ref,
        )


def durable_studio_auth_path(workspace: str | Path) -> Path:
    """Canonical SQLite location for Studio principal & membership authorization."""
    return Path(workspace) / "queue" / "studio_auth.sqlite3"


def build_studio_ibpc_service(
    *,
    queue: JobQueuePort,
    registry: CapabilityRegistry,
    workspace_root: Path,
    revision_provider: Callable[[], int],
    key_provider: KeyProvider | None = None,
    environ: Mapping[str, str] | None = None,
    auth_store: StudioPrincipalAuthStore | None = None,
    channel_binding_ref: str | None = None,
    now: Callable[[], str] = utc_now,
    verifier_binary: str | None = None,
) -> IBPCService:
    """Compose production-durable IBPCService wired to Studio RBAC & SQLite WAL stores."""
    resolved_auth = auth_store or StudioPrincipalAuthStore(
        durable_studio_auth_path(workspace_root), now=now
    )
    authorizer = ProjectMembershipAuthorizer(resolved_auth)
    deps = IBPCServiceDeps(
        queue=queue,
        registry=registry,
        workspace_root=workspace_root,
        revision_provider=revision_provider,
        now=now,
        verifier_binary=verifier_binary,
        authority=resolved_auth,
        authorizer=authorizer,
        channel_binding_ref=channel_binding_ref,
        durable=True,
        session_store=DurableLiveSessionStore(durable_session_store_path(workspace_root)),
        approval_ledger=DurableApprovalLedger(
            durable_approval_ledger_path(workspace_root), now=now
        ),
        privacy_store=DurablePrivacyConsentStore(durable_privacy_consent_path(workspace_root)),
    )
    return build_ibpc_service(deps, key_provider=key_provider, environ=environ)


__all__ = [
    "PrincipalAuthenticationError",
    "ProjectMembershipAuthorizer",
    "ROLE_ACTIONS",
    "StudioPrincipalAuthStore",
    "build_studio_ibpc_service",
    "durable_studio_auth_path",
]
