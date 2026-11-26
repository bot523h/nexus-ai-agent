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
import json
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
from nexus_ai_agent.nagar.ibpc.approval_mac import KeyProvider
from nexus_ai_agent.nagar.ibpc.authority import (
    AuthorizationError,
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
from nexus_ai_agent.nagar.ibpc.service import IBPCService, IBPCServiceDeps, utc_now

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
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_studio_admin_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    target_principal_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    binding_ref TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    occurred_at_utc TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_studio_admin_audit_no_update
                BEFORE UPDATE ON nexus_studio_admin_audit
                BEGIN
                    SELECT RAISE(ABORT, 'studio admin audit is append-only');
                END
                """
            )
            self._conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_studio_admin_audit_no_delete
                BEFORE DELETE ON nexus_studio_admin_audit
                BEGIN
                    SELECT RAISE(ABORT, 'studio admin audit is append-only');
                END
                """
            )
        self._closed = False
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self) -> StudioPrincipalAuthStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _record_admin_event_locked(
        self,
        *,
        event_type: str,
        actor_id: str,
        tenant_id: str = "",
        project_id: str = "",
        target_principal_id: str = "",
        role: str = "",
        binding_ref: str = "",
        detail: str = "",
        occurred_at_utc: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO nexus_studio_admin_audit (
                event_type, actor_id, tenant_id, project_id, target_principal_id,
                role, binding_ref, detail, occurred_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                actor_id,
                tenant_id,
                project_id,
                target_principal_id,
                role,
                binding_ref,
                detail,
                occurred_at_utc,
            ),
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _assert_actor_project_owner_locked(
        self,
        conn: sqlite3.Connection,
        *,
        actor_principal_id: str,
        project_id: str,
        expected_tenant_id: str = "",
    ) -> tuple[bool, str, str]:
        if not project_id.strip():
            return False, "project_scope_required", ""
        if not actor_principal_id.strip():
            return False, "actor_principal_required", ""
        actor_row = conn.execute(
            "SELECT tenant_id, status FROM nexus_studio_principals WHERE principal_id = ?",
            (actor_principal_id,),
        ).fetchone()
        if actor_row is None or str(actor_row[1]) != "ACTIVE":
            return False, "actor_principal_inactive_or_missing", ""
        actor_tenant = str(actor_row[0])
        if expected_tenant_id.strip() and actor_tenant != expected_tenant_id:
            return False, "cross_tenant_principal_access", ""
        m_row = conn.execute(
            """
            SELECT m.role, m.status, p.tenant_id
            FROM nexus_studio_project_memberships m
            JOIN nexus_studio_projects p ON p.project_id = m.project_id
            WHERE m.principal_id = ? AND m.project_id = ?
            LIMIT 1
            """,
            (actor_principal_id, project_id),
        ).fetchone()
        if (
            m_row is None
            or str(m_row[2]) != actor_tenant
            or str(m_row[1]) != "ACTIVE"
            or str(m_row[0]) != StudioRole.OWNER.value
            or StudioAction.MANAGE_MEMBERSHIP not in ROLE_ACTIONS[StudioRole.OWNER]
        ):
            return False, "unauthorized_project_owner", ""
        return True, "authorized", actor_tenant

    def register_principal(
        self,
        principal_id: str,
        *,
        tenant_id: str,
        raw_token: str,
        display_name: str = "",
        actor_id: str = "system",
        project_id: str = "",
        allow_system_bootstrap: bool = False,
    ) -> str:
        if not raw_token or len(raw_token) < 8:
            raise ValueError("principal raw_token must be at least 8 characters")
        PrincipalId(principal_id)
        token_sha = _hash_token(raw_token)
        now_utc = self._now()
        resolved_project = (project_id or "").strip()
        is_bootstrap_seed = actor_id == "system" and (
            not resolved_project or allow_system_bootstrap
        )
        with self._transaction():
            if not is_bootstrap_seed:
                ok, reason, _ = self._assert_actor_project_owner_locked(
                    self._conn,
                    actor_principal_id=actor_id,
                    project_id=resolved_project,
                    expected_tenant_id=tenant_id,
                )
                if not ok:
                    raise AuthorizationError(
                        reason,
                        f"actor {actor_id!r} cannot register principal in {resolved_project!r}",
                    )
            existing = self._conn.execute(
                "SELECT tenant_id, status FROM nexus_studio_principals WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
            if existing is not None:
                raise AuthorizationError(
                    "principal_already_exists",
                    f"principal {principal_id!r} already exists",
                )
            token_owner = self._conn.execute(
                "SELECT principal_id FROM nexus_studio_principals WHERE token_sha256 = ?",
                (token_sha,),
            ).fetchone()
            if token_owner is not None:
                raise AuthorizationError(
                    "principal_token_conflict",
                    "principal token is already registered",
                )
            self._conn.execute(
                """
                INSERT INTO nexus_studio_principals (
                    principal_id, tenant_id, display_name, token_sha256,
                    status, created_at_utc, revoked_at_utc
                ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, '')
                """,
                (principal_id, tenant_id, display_name, token_sha, now_utc),
            )
            self._record_admin_event_locked(
                event_type="register_principal",
                actor_id=actor_id,
                tenant_id=tenant_id,
                project_id=resolved_project,
                target_principal_id=principal_id,
                detail=display_name,
                occurred_at_utc=now_utc,
            )
        return principal_id

    def _assert_project_scoped_revocation_locked(
        self,
        *,
        principal_id: str,
        actor_id: str,
        project_id: str,
        tenant_id: str,
    ) -> tuple[str, ...]:
        in_project = self._conn.execute(
            """
            SELECT 1
            FROM nexus_studio_project_memberships m
            JOIN nexus_studio_projects p ON p.project_id = m.project_id
            WHERE m.principal_id = ?
              AND m.project_id = ?
              AND p.tenant_id = ?
              AND m.status = 'ACTIVE'
            LIMIT 1
            """,
            (principal_id, project_id, tenant_id),
        ).fetchone()
        if in_project is None:
            raise AuthorizationError(
                "principal_not_in_project",
                f"principal {principal_id!r} has no active membership in project {project_id!r}",
            )
        active_rows = self._conn.execute(
            """
            SELECT DISTINCT m.project_id
            FROM nexus_studio_project_memberships m
            JOIN nexus_studio_projects p ON p.project_id = m.project_id
            WHERE m.principal_id = ?
              AND p.tenant_id = ?
              AND m.status = 'ACTIVE'
            ORDER BY m.project_id ASC
            """,
            (principal_id, tenant_id),
        ).fetchall()
        active_project_ids = [str(r[0]) for r in active_rows]
        for other_project_id in active_project_ids:
            if other_project_id == project_id:
                continue
            ok, _reason, _ = self._assert_actor_project_owner_locked(
                self._conn,
                actor_principal_id=actor_id,
                project_id=other_project_id,
                expected_tenant_id=tenant_id,
            )
            if not ok:
                raise AuthorizationError(
                    "principal_active_in_other_projects",
                    f"principal {principal_id!r} is active in other project {other_project_id!r}",
                )
        return tuple(sorted({project_id, *active_project_ids}))

    def revoke_principal(
        self,
        principal_id: str,
        *,
        actor_id: str = "system",
        expected_tenant_id: str | None = None,
        project_id: str = "",
        allow_system_bootstrap: bool = False,
    ) -> None:
        now_utc = self._now()
        resolved_project = (project_id or "").strip()
        is_bootstrap = actor_id == "system" and allow_system_bootstrap
        with self._transaction():
            actor_tenant = ""
            if not is_bootstrap:
                ok, reason, actor_tenant = self._assert_actor_project_owner_locked(
                    self._conn,
                    actor_principal_id=actor_id,
                    project_id=resolved_project,
                    expected_tenant_id=expected_tenant_id or "",
                )
                if not ok:
                    raise AuthorizationError(
                        reason,
                        f"actor {actor_id!r} cannot revoke principal in {resolved_project!r}",
                    )
            p_row = self._conn.execute(
                "SELECT tenant_id FROM nexus_studio_principals WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
            if p_row is None:
                raise AuthorizationError(
                    "principal_not_found", f"unknown principal {principal_id!r}"
                )
            target_tenant = str(p_row[0])
            if expected_tenant_id is not None and target_tenant != expected_tenant_id:
                raise AuthorizationError(
                    "principal_tenant_mismatch",
                    f"principal {principal_id!r} belongs to tenant {target_tenant!r}",
                )
            if not is_bootstrap and target_tenant != actor_tenant:
                raise AuthorizationError(
                    "principal_tenant_mismatch",
                    f"principal {principal_id!r} belongs to tenant {target_tenant!r}",
                )
            affected_projects: tuple[str, ...] = ()
            if not is_bootstrap or resolved_project:
                affected_projects = self._assert_project_scoped_revocation_locked(
                    principal_id=principal_id,
                    actor_id=actor_id,
                    project_id=resolved_project,
                    tenant_id=target_tenant,
                )
            self._conn.execute(
                """
                UPDATE nexus_studio_principals
                SET status = 'REVOKED', revoked_at_utc = ?
                WHERE principal_id = ?
                """,
                (now_utc, principal_id),
            )
            self._conn.execute(
                """
                UPDATE nexus_studio_channel_bindings
                SET status = 'REVOKED', revoked_at_utc = ?
                WHERE principal_id = ? AND status = 'ACTIVE'
                """,
                (now_utc, principal_id),
            )
            if affected_projects:
                detail_json = json.dumps(
                    {
                        "initiating_project_id": resolved_project,
                        "affected_projects": list(affected_projects),
                    },
                    sort_keys=True,
                )
                for affected_pid in affected_projects:
                    self._record_admin_event_locked(
                        event_type="REVOKE_PRINCIPAL",
                        actor_id=actor_id,
                        tenant_id=target_tenant,
                        project_id=affected_pid,
                        target_principal_id=principal_id,
                        detail=detail_json,
                        occurred_at_utc=now_utc,
                    )
            else:
                self._record_admin_event_locked(
                    event_type="REVOKE_PRINCIPAL",
                    actor_id=actor_id,
                    tenant_id=target_tenant,
                    project_id=resolved_project,
                    target_principal_id=principal_id,
                    occurred_at_utc=now_utc,
                )

    def authenticate_principal_context(self, raw_token: str | None) -> tuple[str, str]:
        if not isinstance(raw_token, str) or not raw_token.strip():
            raise PrincipalAuthenticationError(
                "missing_principal_token", "principal token required"
            )
        presented_digest = _hash_token(raw_token.strip())
        rows = self._conn.execute(
            "SELECT principal_id, tenant_id, token_sha256, status FROM nexus_studio_principals"
        ).fetchall()
        matched_id: str | None = None
        matched_tenant: str = ""
        matched_status: str = ""
        for pid, tenant_id, stored_digest, status in rows:
            if hmac.compare_digest(presented_digest, str(stored_digest)):
                matched_id = str(pid)
                matched_tenant = str(tenant_id)
                matched_status = str(status)
        if matched_id is None:
            raise PrincipalAuthenticationError("invalid_principal_token", "unknown principal token")
        if matched_status != "ACTIVE":
            raise PrincipalAuthenticationError(
                "principal_revoked", f"principal {matched_id} is {matched_status}"
            )
        return matched_id, matched_tenant

    def authenticate_token(self, raw_token: str | None) -> str:
        principal_id, _tenant_id = self.authenticate_principal_context(raw_token)
        return principal_id

    def bind_channel(
        self,
        principal_id: str,
        *,
        channel: str,
        external_subject: str,
        user_id: int = 0,
        chat_id: int = 0,
        binding_ref: str | None = None,
        actor_id: str = "system",
        project_id: str | None = "",
        allow_system_bootstrap: bool = False,
    ) -> str:
        ref = binding_ref or _binding_ref_for(channel, external_subject)
        now_utc = self._now()
        resolved_project = (project_id or "").strip()
        is_bootstrap = actor_id == "system" and allow_system_bootstrap
        with self._transaction():
            actor_tenant = ""
            if not is_bootstrap:
                ok, reason, actor_tenant = self._assert_actor_project_owner_locked(
                    self._conn,
                    actor_principal_id=actor_id,
                    project_id=resolved_project,
                )
                if not ok:
                    raise AuthorizationError(
                        reason,
                        f"actor {actor_id!r} cannot bind channel in {resolved_project!r}",
                    )
            p_row = self._conn.execute(
                "SELECT tenant_id, status FROM nexus_studio_principals WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
            if p_row is None:
                raise AuthorizationError(
                    "principal_not_found", f"unknown principal {principal_id!r}"
                )
            tenant_id = str(p_row[0])
            if not is_bootstrap and tenant_id != actor_tenant:
                raise AuthorizationError(
                    "cross_tenant_principal_access",
                    f"principal {principal_id!r} belongs to tenant {tenant_id!r}",
                )
            if str(p_row[1]) != "ACTIVE":
                raise AuthorizationError(
                    "principal_revoked",
                    f"principal {principal_id!r} is {p_row[1]}",
                )
            if resolved_project:
                active_rows = self._conn.execute(
                    """
                    SELECT m.project_id, p.tenant_id
                    FROM nexus_studio_project_memberships m
                    JOIN nexus_studio_projects p ON p.project_id = m.project_id
                    WHERE m.principal_id = ? AND m.status = 'ACTIVE'
                    """,
                    (principal_id,),
                ).fetchall()
                if active_rows and not any(
                    str(r[0]) == resolved_project and str(r[1]) == tenant_id for r in active_rows
                ):
                    raise AuthorizationError(
                        "principal_not_in_project",
                        f"principal {principal_id!r} has no active membership "
                        f"in project {resolved_project!r}",
                    )
            existing = self._conn.execute(
                """
                SELECT binding_ref
                FROM nexus_studio_channel_bindings
                WHERE binding_ref = ? OR (channel = ? AND external_subject = ?)
                LIMIT 1
                """,
                (ref, channel, external_subject),
            ).fetchone()
            if existing is not None:
                raise AuthorizationError(
                    "binding_already_exists",
                    f"channel binding {ref!r} or subject "
                    f"{channel}:{external_subject} already exists",
                )
            self._conn.execute(
                """
                INSERT INTO nexus_studio_channel_bindings (
                    binding_ref, principal_id, channel, external_subject,
                    user_id, chat_id, status, created_at_utc, revoked_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, '')
                """,
                (ref, principal_id, channel, external_subject, user_id, chat_id, now_utc),
            )
            self._record_admin_event_locked(
                event_type="bind_channel",
                actor_id=actor_id,
                tenant_id=tenant_id,
                project_id=resolved_project,
                target_principal_id=principal_id,
                binding_ref=ref,
                detail=f"{channel}:{external_subject}",
                occurred_at_utc=now_utc,
            )
        return ref

    def revoke_channel_binding(
        self,
        binding_ref: str,
        *,
        actor_id: str = "system",
        expected_tenant_id: str | None = None,
        expected_principal_id: str | None = None,
        project_id: str = "",
        allow_system_bootstrap: bool = False,
    ) -> None:
        now_utc = self._now()
        resolved_project = (project_id or "").strip()
        is_bootstrap = actor_id == "system" and allow_system_bootstrap
        with self._transaction():
            actor_tenant = ""
            if not is_bootstrap:
                ok, reason, actor_tenant = self._assert_actor_project_owner_locked(
                    self._conn,
                    actor_principal_id=actor_id,
                    project_id=resolved_project,
                    expected_tenant_id=expected_tenant_id or "",
                )
                if not ok:
                    raise AuthorizationError(
                        reason,
                        f"actor {actor_id!r} cannot revoke binding in {resolved_project!r}",
                    )
            row = self._conn.execute(
                """
                SELECT b.principal_id, p.tenant_id
                FROM nexus_studio_channel_bindings b
                JOIN nexus_studio_principals p ON p.principal_id = b.principal_id
                WHERE b.binding_ref = ?
                """,
                (binding_ref,),
            ).fetchone()
            if row is None:
                raise AuthorizationError(
                    "binding_not_found", f"unknown channel binding {binding_ref!r}"
                )
            target_principal = str(row[0])
            target_tenant = str(row[1])
            if expected_principal_id is not None and target_principal != expected_principal_id:
                raise AuthorizationError(
                    "binding_not_found",
                    f"binding {binding_ref!r} does not belong to {expected_principal_id!r}",
                )
            if expected_tenant_id is not None and target_tenant != expected_tenant_id:
                raise AuthorizationError(
                    "principal_tenant_mismatch",
                    f"binding {binding_ref!r} belongs to tenant {target_tenant!r}",
                )
            if not is_bootstrap and target_tenant != actor_tenant:
                raise AuthorizationError(
                    "principal_tenant_mismatch",
                    f"binding {binding_ref!r} belongs to tenant {target_tenant!r}",
                )
            affected_projects: tuple[str, ...] = ()
            if not is_bootstrap or resolved_project:
                affected_projects = self._assert_project_scoped_revocation_locked(
                    principal_id=target_principal,
                    actor_id=actor_id,
                    project_id=resolved_project,
                    tenant_id=target_tenant,
                )
            self._conn.execute(
                """
                UPDATE nexus_studio_channel_bindings
                SET status = 'REVOKED', revoked_at_utc = ?
                WHERE binding_ref = ?
                """,
                (now_utc, binding_ref),
            )
            if affected_projects:
                detail_json = json.dumps(
                    {
                        "initiating_project_id": resolved_project,
                        "affected_projects": list(affected_projects),
                    },
                    sort_keys=True,
                )
                for affected_pid in affected_projects:
                    self._record_admin_event_locked(
                        event_type="REVOKE_CHANNEL_BINDING",
                        actor_id=actor_id,
                        tenant_id=target_tenant,
                        project_id=affected_pid,
                        target_principal_id=target_principal,
                        binding_ref=binding_ref,
                        detail=detail_json,
                        occurred_at_utc=now_utc,
                    )
            else:
                self._record_admin_event_locked(
                    event_type="REVOKE_CHANNEL_BINDING",
                    actor_id=actor_id,
                    tenant_id=target_tenant,
                    project_id=resolved_project,
                    target_principal_id=target_principal,
                    binding_ref=binding_ref,
                    occurred_at_utc=now_utc,
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

    def get_principal_tenant(self, principal_id: str) -> str:
        row = self._conn.execute(
            "SELECT tenant_id FROM nexus_studio_principals WHERE principal_id = ?",
            (principal_id,),
        ).fetchone()
        if row is None:
            raise AuthorizationError("principal_not_found", f"unknown principal {principal_id!r}")
        return str(row[0])

    def get_project_tenant(self, project_id: str) -> str:
        row = self._conn.execute(
            "SELECT tenant_id FROM nexus_studio_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise AuthorizationError("project_not_found", f"unknown project {project_id!r}")
        return str(row[0])

    def get_project_revision(self, project_id: str) -> str:
        row = self._conn.execute(
            "SELECT active_revision_id FROM nexus_studio_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise AuthorizationError("project_not_found", f"unknown project {project_id!r}")
        return str(row[0])

    def set_project_revision(self, project_id: str, active_revision_id: str) -> None:
        with self._transaction():
            cur = self._conn.execute(
                """
                UPDATE nexus_studio_projects
                SET active_revision_id = ?
                WHERE project_id = ?
                """,
                (active_revision_id, project_id),
            )
            if cur.rowcount == 0:
                raise AuthorizationError("project_not_found", f"unknown project {project_id!r}")

    def assert_same_tenant(self, *, principal_id: str, project_id: str, tenant_id: str) -> None:
        p_tenant = self.get_principal_tenant(principal_id)
        proj_tenant = self.get_project_tenant(project_id)
        if p_tenant != tenant_id or proj_tenant != tenant_id or p_tenant != proj_tenant:
            raise AuthorizationError(
                "tenant_mismatch",
                f"tenant {tenant_id!r} conflicts with "
                f"principal={p_tenant!r}/project={proj_tenant!r}",
            )

    def has_owner_role(
        self,
        principal_id: str,
        tenant_id: str,
        *,
        project_id: str | None = None,
    ) -> bool:
        if project_id is not None:
            row = self._conn.execute(
                """
                SELECT 1
                FROM nexus_studio_project_memberships m
                JOIN nexus_studio_projects p ON p.project_id = m.project_id
                WHERE m.principal_id = ?
                  AND m.project_id = ?
                  AND m.status = 'ACTIVE'
                  AND m.role = 'OWNER'
                  AND p.tenant_id = ?
                LIMIT 1
                """,
                (principal_id, project_id, tenant_id),
            ).fetchone()
            return row is not None
        row = self._conn.execute(
            """
            SELECT 1
            FROM nexus_studio_project_memberships m
            JOIN nexus_studio_projects p ON p.project_id = m.project_id
            WHERE m.principal_id = ?
              AND m.status = 'ACTIVE'
              AND m.role = 'OWNER'
              AND p.tenant_id = ?
            LIMIT 1
            """,
            (principal_id, tenant_id),
        ).fetchone()
        return row is not None

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
            proj_row = self._conn.execute(
                "SELECT tenant_id FROM nexus_studio_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if proj_row is None:
                raise AuthorizationError("project_not_found", f"unknown project {project_id!r}")
            target_row = self._conn.execute(
                "SELECT tenant_id FROM nexus_studio_principals WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
            if target_row is None:
                raise AuthorizationError(
                    "principal_not_found", f"unknown principal {principal_id!r}"
                )
            granter_row = self._conn.execute(
                "SELECT tenant_id FROM nexus_studio_principals WHERE principal_id = ?",
                (granted_by,),
            ).fetchone()
            if granter_row is None:
                raise AuthorizationError("principal_not_found", f"unknown principal {granted_by!r}")
            project_tenant = str(proj_row[0])
            if str(target_row[0]) != project_tenant or str(granter_row[0]) != project_tenant:
                raise AuthorizationError(
                    "cross_tenant_membership_denied",
                    f"cross-tenant membership grant denied for project {project_id!r}",
                )
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
            self._record_admin_event_locked(
                event_type="grant_membership",
                actor_id=granted_by,
                tenant_id=project_tenant,
                project_id=project_id,
                target_principal_id=principal_id,
                role=resolved_role.value,
                occurred_at_utc=now_utc,
            )
        return membership_id

    def revoke_membership(
        self, project_id: str, principal_id: str, *, revoked_by: str = "system"
    ) -> None:
        now_utc = self._now()
        with self._transaction():
            proj_row = self._conn.execute(
                "SELECT tenant_id FROM nexus_studio_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            tenant_id = str(proj_row[0]) if proj_row is not None else ""
            self._conn.execute(
                """
                UPDATE nexus_studio_project_memberships
                SET status = 'REVOKED', revoked_at_utc = ?
                WHERE project_id = ? AND principal_id = ?
                """,
                (now_utc, project_id, principal_id),
            )
            self._record_admin_event_locked(
                event_type="revoke_membership",
                actor_id=revoked_by,
                tenant_id=tenant_id,
                project_id=project_id,
                target_principal_id=principal_id,
                occurred_at_utc=now_utc,
            )

    def list_memberships(self, project_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._conn.execute(
            """
            SELECT membership_id, project_id, principal_id, role, status,
                   granted_by, created_at_utc, revoked_at_utc
            FROM nexus_studio_project_memberships
            WHERE project_id = ?
            ORDER BY created_at_utc ASC, principal_id ASC
            """,
            (project_id,),
        ).fetchall()
        return tuple(
            {
                "membership_id": r[0],
                "project_id": r[1],
                "principal_id": r[2],
                "role": r[3],
                "status": r[4],
                "granted_by": r[5],
                "created_at_utc": r[6],
                "revoked_at_utc": r[7],
            }
            for r in rows
        )

    def list_admin_audit(
        self,
        *,
        project_id: str | None = None,
        target_principal_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if target_principal_id is not None:
            clauses.append("target_principal_id = ?")
            params.append(target_principal_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""
            SELECT audit_id, event_type, actor_id, tenant_id, project_id,
                   target_principal_id, role, binding_ref, detail, occurred_at_utc
            FROM nexus_studio_admin_audit
            {where}
            ORDER BY audit_id ASC
            """,
            params,
        ).fetchall()
        return tuple(
            {
                "audit_id": int(r[0]),
                "event_type": r[1],
                "actor_id": r[2],
                "tenant_id": r[3],
                "project_id": r[4],
                "target_principal_id": r[5],
                "role": r[6],
                "binding_ref": r[7],
                "detail": r[8],
                "occurred_at_utc": r[9],
            }
            for r in rows
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
            "SELECT status, tenant_id FROM nexus_studio_principals WHERE principal_id = ?",
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

        proj_row = self._conn.execute(
            "SELECT tenant_id FROM nexus_studio_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if proj_row is not None and str(p_row[1]) != str(proj_row[0]):
            decision = StudioAuthorizationDecision(
                allowed=False,
                principal_id=principal_id,
                project_id=project_id,
                role="NONE",
                action=action.value,
                reason="cross_tenant_denied",
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

    def get_project_tenant(self, project_id: str) -> str:
        return self._store.get_project_tenant(project_id)

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

    def assert_same_tenant(self, *, principal_id: str, project_id: str, tenant_id: str) -> None:
        self._store.assert_same_tenant(
            principal_id=principal_id, project_id=project_id, tenant_id=tenant_id
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
    authorized_asset_root: Path | None = None,
    project_revision_resolver: Callable[[str], str] | None = None,
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
        authorized_asset_root=authorized_asset_root or workspace_root,
        project_revision_resolver=project_revision_resolver or resolved_auth.get_project_revision,
        owned_closables=(resolved_auth,) if auth_store is None else (),
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
