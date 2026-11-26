"""Privacy / trust modes and durable revocable consent ledger (IBPC Wave 3).

Three modes, one default, explicit revocable consent:

* ``LOCAL_VAULT`` — **default**; audio/transcript/artifacts never leave the
  process; records carry digests only, never raw transcript or audio bytes;
* ``TRUSTED_CLOUD`` / ``EXPERIMENTAL_CLOUD`` — selectable **only** with an
  explicit, active, non-revoked, non-expired recorded consent; when no external
  provider is wired, any egress request remains a typed refusal.

In production/durable mode, consents and their append-only audit trail live in
``DurablePrivacyConsentStore`` (SQLite WAL with ``BEGIN IMMEDIATE``).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import (
    ConsentRecord,
    DataCategory,
    DurableConsentGrant,
    PrivacyConsentAuditEntry,
    PrivacyMode,
    ProcessingScope,
)

#: Fields that must never appear in a log/record as raw text — digests only.
SENSITIVE_FIELDS = frozenset(
    {
        "transcript",
        "text",
        "audio",
        "audio_bytes",
        "prompt",
        "content",
        "biometric",
        "face_roi",
        "raw_media",
        "token",
        "secret",
    }
)

VALID_PROCESSING_SCOPES = frozenset({s.value for s in ProcessingScope})
VALID_DATA_CATEGORIES = frozenset({c.value for c in DataCategory})


class PrivacyViolation(RuntimeError):
    """Typed refusal: an operation would move or expose protected data."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


def _parse_utc(ts: str) -> datetime:
    if not isinstance(ts, str) or not ts.strip():
        raise PrivacyViolation("invalid_timestamp", "timestamp must be a non-empty ISO-8601 string")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivacyViolation("invalid_timestamp", str(exc)) from exc
    if dt.tzinfo is None:
        raise PrivacyViolation("invalid_timestamp", "timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)


def redact_mapping(record: dict[str, Any]) -> dict[str, Any]:
    """Return a log-safe copy: sensitive fields are replaced by their digest.

    A value under a sensitive key is never copied into the result — only
    ``sha256:<hex>`` of its canonical spelling (or of the raw bytes) appears.
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key.lower() in SENSITIVE_FIELDS and value is not None:
            if isinstance(value, (bytes, bytearray)):
                from nexus_ai_agent.nagar.ibpc.canonical import sha256_bytes

                out[key] = sha256_bytes(bytes(value))
            else:
                out[key] = digest_of({"redacted": str(value)})
        else:
            out[key] = value
    return out


class DurablePrivacyConsentStore:
    """SQLite WAL-backed, revocable privacy consent ledger + append-only audit log.

    Every write runs inside ``BEGIN IMMEDIATE`` with ``busy_timeout=5000``,
    ``foreign_keys=ON``, and ``synchronous=FULL``. ``nexus_privacy_consent_audit``
    is protected by SQLite ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers so
    audit entries are strictly append-only and never carry raw sensitive media
    or transcripts.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self._closed = False
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
                CREATE TABLE IF NOT EXISTS nexus_privacy_consents (
                    consent_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    principal_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    processing_scope TEXT NOT NULL,
                    data_category TEXT NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT '',
                    granted_at_utc TEXT NOT NULL,
                    expires_at_utc TEXT NOT NULL DEFAULT '',
                    revoked_at_utc TEXT NOT NULL DEFAULT '',
                    revoked_by TEXT NOT NULL DEFAULT '',
                    revoke_reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            consent_cols = {
                row[1] for row in self._conn.execute("PRAGMA table_info(nexus_privacy_consents)")
            }
            if "tenant_id" not in consent_cols:
                self._conn.execute(
                    "ALTER TABLE nexus_privacy_consents "
                    "ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''"
                )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_privacy_consent_lookup
                ON nexus_privacy_consents (
                    project_id, principal_id, processing_scope, data_category, mode
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexus_privacy_consent_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    consent_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    processing_scope TEXT NOT NULL,
                    data_category TEXT NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    occurred_at_utc TEXT NOT NULL,
                    metadata_digest TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_privacy_audit_no_update
                BEFORE UPDATE ON nexus_privacy_consent_audit
                BEGIN
                    SELECT RAISE(ABORT, 'privacy consent audit log is append-only');
                END
                """
            )
            self._conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_privacy_audit_no_delete
                BEFORE DELETE ON nexus_privacy_consent_audit
                BEGIN
                    SELECT RAISE(ABORT, 'privacy consent audit log is append-only');
                END
                """
            )
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> DurablePrivacyConsentStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

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

    @staticmethod
    def _row_to_grant(row: tuple[Any, ...]) -> DurableConsentGrant:
        return DurableConsentGrant(
            consent_id=row[0],
            project_id=row[1],
            tenant_id=row[2],
            principal_id=row[3],
            mode=PrivacyMode(row[4]),
            processing_scope=row[5],
            data_category=row[6],
            provider_id=row[7],
            granted_at_utc=row[8],
            expires_at_utc=row[9],
            revoked_at_utc=row[10],
            revoked_by=row[11],
            revoke_reason=row[12],
        )

    @staticmethod
    def _metadata_digest(metadata: dict[str, Any] | None) -> str:
        if not metadata:
            return ""
        safe = redact_mapping(dict(metadata))
        return digest_of({"safe_metadata": safe})

    def _append_audit_locked(
        self,
        *,
        consent_id: str,
        event_type: Literal["GRANTED", "REVOKED", "DENIED", "EXPIRED"],
        project_id: str,
        principal_id: str,
        mode: str,
        processing_scope: str,
        data_category: str,
        provider_id: str,
        actor_id: str,
        reason: str,
        occurred_at_utc: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO nexus_privacy_consent_audit (
                consent_id, event_type, project_id, principal_id, mode,
                processing_scope, data_category, provider_id, actor_id,
                reason, occurred_at_utc, metadata_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                consent_id,
                event_type,
                project_id,
                principal_id,
                mode,
                processing_scope,
                data_category,
                provider_id,
                actor_id,
                reason,
                occurred_at_utc,
                self._metadata_digest(metadata),
            ),
        )
        return int(cur.lastrowid or 0)

    def grant_consent(
        self,
        *,
        project_id: str,
        principal_id: str,
        mode: PrivacyMode,
        processing_scope: str,
        data_category: str,
        granted_at_utc: str,
        tenant_id: str = "",
        expires_at_utc: str = "",
        provider_id: str = "",
        consent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DurableConsentGrant:
        if not isinstance(mode, PrivacyMode):
            raise PrivacyViolation("invalid_mode", f"unknown privacy mode {mode!r}")
        if not project_id or not principal_id:
            raise PrivacyViolation("invalid_identity", "project_id and principal_id are required")
        granted_dt = _parse_utc(granted_at_utc)
        if expires_at_utc:
            exp_dt = _parse_utc(expires_at_utc)
            if exp_dt <= granted_dt:
                raise PrivacyViolation(
                    "invalid_expiry", "expires_at_utc must be strictly after granted_at_utc"
                )
        cid = consent_id or f"consent_{uuid.uuid4().hex}"
        grant = DurableConsentGrant(
            consent_id=cid,
            project_id=project_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode=mode,
            processing_scope=processing_scope,
            data_category=data_category,
            provider_id=provider_id,
            granted_at_utc=granted_at_utc,
            expires_at_utc=expires_at_utc,
        )
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO nexus_privacy_consents (
                    consent_id, project_id, tenant_id, principal_id, mode,
                    processing_scope, data_category, provider_id,
                    granted_at_utc, expires_at_utc, revoked_at_utc,
                    revoked_by, revoke_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '')
                """,
                (
                    grant.consent_id,
                    grant.project_id,
                    grant.tenant_id,
                    grant.principal_id,
                    grant.mode.value,
                    grant.processing_scope,
                    grant.data_category,
                    grant.provider_id,
                    grant.granted_at_utc,
                    grant.expires_at_utc,
                ),
            )
            self._append_audit_locked(
                consent_id=grant.consent_id,
                event_type="GRANTED",
                project_id=grant.project_id,
                principal_id=grant.principal_id,
                mode=grant.mode.value,
                processing_scope=grant.processing_scope,
                data_category=grant.data_category,
                provider_id=grant.provider_id,
                actor_id=grant.principal_id,
                reason="consent_granted",
                occurred_at_utc=granted_at_utc,
                metadata=metadata,
            )
        return grant

    def get_consent(self, consent_id: str) -> DurableConsentGrant | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT consent_id, project_id, tenant_id, principal_id, mode,
                       processing_scope, data_category, provider_id,
                       granted_at_utc, expires_at_utc, revoked_at_utc,
                       revoked_by, revoke_reason
                FROM nexus_privacy_consents
                WHERE consent_id = ?
                """,
                (consent_id,),
            ).fetchone()
        return self._row_to_grant(row) if row is not None else None

    def revoke_consent(
        self,
        consent_id: str,
        *,
        revoked_by: str,
        revoked_at_utc: str,
        reason: str = "user_revoked",
        project_id: str | None = None,
        expected_tenant_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DurableConsentGrant:
        _parse_utc(revoked_at_utc)
        with self._transaction():
            existing = self.get_consent(consent_id)
            if existing is None:
                if project_id is not None:
                    raise PrivacyViolation("consent_not_found", f"consent {consent_id!r} not found")
                raise PrivacyViolation("unknown_consent", f"consent {consent_id!r} not found")
            if project_id is not None and existing.project_id != project_id:
                raise PrivacyViolation(
                    "consent_not_found",
                    f"consent {consent_id!r} does not belong to project {project_id!r}",
                )
            if expected_tenant_id is not None and existing.tenant_id != expected_tenant_id:
                raise PrivacyViolation(
                    "consent_not_found",
                    f"consent {consent_id!r} does not belong to tenant {expected_tenant_id!r}",
                )
            if existing.revoked_at_utc:
                return existing
            self._conn.execute(
                """
                UPDATE nexus_privacy_consents
                SET revoked_at_utc = ?, revoked_by = ?, revoke_reason = ?
                WHERE consent_id = ?
                """,
                (revoked_at_utc, revoked_by, reason, consent_id),
            )
            self._append_audit_locked(
                consent_id=existing.consent_id,
                event_type="REVOKED",
                project_id=existing.project_id,
                principal_id=existing.principal_id,
                mode=existing.mode.value,
                processing_scope=existing.processing_scope,
                data_category=existing.data_category,
                provider_id=existing.provider_id,
                actor_id=revoked_by,
                reason=reason,
                occurred_at_utc=revoked_at_utc,
                metadata=metadata,
            )
            updated = self.get_consent(consent_id)
            assert updated is not None
            return updated

    def backfill_legacy_tenant_id(self, *, project_id: str, tenant_id: str) -> int:
        """Deterministically backfill legacy consent rows with empty tenant_id."""
        if not project_id or not tenant_id:
            raise PrivacyViolation(
                "invalid_identity", "project_id and tenant_id are required for backfill"
            )
        with self._transaction():
            cur = self._conn.execute(
                """
                UPDATE nexus_privacy_consents
                SET tenant_id = ?
                WHERE project_id = ? AND tenant_id = ''
                """,
                (tenant_id, project_id),
            )
            return int(cur.rowcount or 0)

    def check_consent_status(
        self,
        *,
        project_id: str,
        principal_id: str,
        mode: PrivacyMode,
        processing_scope: str,
        data_category: str,
        now_utc: str,
        provider_id: str = "",
        tenant_id: str = "",
    ) -> tuple[str, DurableConsentGrant | None]:
        """Return ``('active' | 'revoked' | 'expired' | 'missing', grant_or_None)``."""
        now_dt = _parse_utc(now_utc)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT consent_id, project_id, tenant_id, principal_id, mode,
                       processing_scope, data_category, provider_id,
                       granted_at_utc, expires_at_utc, revoked_at_utc,
                       revoked_by, revoke_reason
                FROM nexus_privacy_consents
                WHERE project_id = ?
                  AND principal_id = ?
                  AND mode = ?
                  AND processing_scope = ?
                  AND data_category = ?
                ORDER BY rowid DESC
                """,
                (project_id, principal_id, mode.value, processing_scope, data_category),
            ).fetchall()
        grants = [self._row_to_grant(r) for r in rows]
        if processing_scope == ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value:
            matching = [
                g
                for g in grants
                if provider_id.strip() and g.provider_id.strip() == provider_id.strip()
            ]
        else:
            matching = [
                g
                for g in grants
                if not (provider_id.strip() or g.provider_id.strip())
                or g.provider_id.strip() == provider_id.strip()
            ]
        resolved_tenant = tenant_id.strip()
        if resolved_tenant or any(g.tenant_id.strip() for g in matching):
            tenant_matching = [g for g in matching if g.tenant_id.strip() == resolved_tenant]
            if matching and not tenant_matching:
                raise PrivacyViolation(
                    "consent_tenant_mismatch",
                    f"consent for project {project_id!r} does not belong to tenant {tenant_id!r}",
                )
            matching = tenant_matching
        if not matching:
            return "missing", None
        saw_revoked: DurableConsentGrant | None = None
        saw_expired: DurableConsentGrant | None = None
        for grant in matching:
            if grant.revoked_at_utc:
                saw_revoked = saw_revoked or grant
                continue
            if grant.expires_at_utc:
                exp_dt = _parse_utc(grant.expires_at_utc)
                if now_dt >= exp_dt:
                    saw_expired = saw_expired or grant
                    continue
            return "active", grant
        if saw_revoked is not None:
            return "revoked", saw_revoked
        return "expired", saw_expired

    def record_audit_event(
        self,
        *,
        consent_id: str,
        event_type: Literal["GRANTED", "REVOKED", "DENIED", "EXPIRED"],
        project_id: str,
        principal_id: str,
        mode: str,
        processing_scope: str,
        data_category: str,
        provider_id: str = "",
        actor_id: str,
        reason: str,
        occurred_at_utc: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self._transaction():
            return self._append_audit_locked(
                consent_id=consent_id,
                event_type=event_type,
                project_id=project_id,
                principal_id=principal_id,
                mode=mode,
                processing_scope=processing_scope,
                data_category=data_category,
                provider_id=provider_id,
                actor_id=actor_id,
                reason=reason,
                occurred_at_utc=occurred_at_utc,
                metadata=metadata,
            )

    def list_consents(
        self, *, project_id: str | None = None, principal_id: str | None = None
    ) -> tuple[DurableConsentGrant, ...]:
        clauses: list[str] = []
        params: list[str] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if principal_id is not None:
            clauses.append("principal_id = ?")
            params.append(principal_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT consent_id, project_id, tenant_id, principal_id, mode,
                       processing_scope, data_category, provider_id,
                       granted_at_utc, expires_at_utc, revoked_at_utc,
                       revoked_by, revoke_reason
                FROM nexus_privacy_consents
                {where}
                ORDER BY rowid ASC
                """,
                params,
            ).fetchall()
        return tuple(self._row_to_grant(r) for r in rows)

    def list_audit(
        self, *, project_id: str | None = None, principal_id: str | None = None
    ) -> tuple[PrivacyConsentAuditEntry, ...]:
        clauses: list[str] = []
        params: list[str] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if principal_id is not None:
            clauses.append("principal_id = ?")
            params.append(principal_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT audit_id, consent_id, event_type, project_id, principal_id,
                       mode, processing_scope, data_category, provider_id,
                       actor_id, reason, occurred_at_utc, metadata_digest
                FROM nexus_privacy_consent_audit
                {where}
                ORDER BY audit_id ASC
                """,
                params,
            ).fetchall()
        return tuple(
            PrivacyConsentAuditEntry(
                audit_id=int(r[0]),
                consent_id=r[1],
                event_type=r[2],
                project_id=r[3],
                principal_id=r[4],
                mode=r[5],
                processing_scope=r[6],
                data_category=r[7],
                provider_id=r[8],
                actor_id=r[9],
                reason=r[10],
                occurred_at_utc=r[11],
                metadata_digest=r[12],
            )
            for r in rows
        )


def durable_privacy_consent_path(workspace: str | Path) -> Path:
    """Canonical SQLite location for the studio's durable privacy consent store."""
    return Path(workspace) / "queue" / "privacy_consents.sqlite3"


class PrivacyPolicy:
    """Mode + consent enforcement for live sessions (in-memory or durable SQLite)."""

    DEFAULT_MODE = PrivacyMode.LOCAL_VAULT

    def __init__(
        self,
        *,
        now: Callable[[], str],
        store: DurablePrivacyConsentStore | None = None,
        configured_providers: frozenset[str] = frozenset(),
    ) -> None:
        self._now = now
        self._store = store
        self._configured_providers = frozenset(configured_providers)
        self._consents: list[ConsentRecord] = []

    @property
    def store(self) -> DurablePrivacyConsentStore | None:
        return self._store

    @property
    def consents(self) -> tuple[ConsentRecord, ...]:
        if self._store is not None:
            return tuple(
                ConsentRecord(
                    consent_id=g.consent_id,
                    mode=g.mode,
                    principal_id=g.principal_id,
                    scope=f"{g.processing_scope}:{g.data_category}",
                    granted_at=g.granted_at_utc,
                    project_id=g.project_id,
                    tenant_id=g.tenant_id,
                    processing_scope=g.processing_scope,
                    data_category=g.data_category,
                    provider_id=g.provider_id,
                    expires_at_utc=g.expires_at_utc,
                )
                for g in self._store.list_consents()
                if not g.revoked_at_utc
            )
        return tuple(self._consents)

    def require_local(self, mode: PrivacyMode, data_kind: str) -> None:
        """Local processing is allowed under LOCAL_VAULT; validates mode enum."""
        if not isinstance(mode, PrivacyMode):
            raise PrivacyViolation("invalid_mode", f"unknown privacy mode {mode!r}")

    def check_egress(
        self,
        mode: PrivacyMode,
        data_kind: str,
        *,
        consented: bool,
        provider_id: str = "",
    ) -> None:
        """May ``data_kind`` leave the local vault under ``mode``?"""
        if mode is PrivacyMode.LOCAL_VAULT:
            raise PrivacyViolation("egress_refused", f"{data_kind} must not leave the local vault")
        if not consented:
            raise PrivacyViolation(
                "consent_missing",
                f"{mode.value} requires an explicit recorded consent for {data_kind}",
            )
        if not provider_id or provider_id not in self._configured_providers:
            raise PrivacyViolation(
                "no_provider_wired",
                f"{mode.value} has no configured provider in this slice; {data_kind} stays local",
            )

    def record_consent(
        self,
        mode: PrivacyMode,
        *,
        principal_id: str,
        scope: str,
        project_id: str = "",
        processing_scope: str = "",
        data_category: str | None = None,
        expires_at_utc: str = "",
        provider_id: str = "",
        tenant_id: str = "",
    ) -> ConsentRecord:
        """Record an explicit consent (persisted durably when store is configured)."""
        if not isinstance(mode, PrivacyMode):
            raise PrivacyViolation("invalid_mode", f"unknown privacy mode {mode!r}")
        now_utc = self._now()
        effective_processing_scope = processing_scope or (
            ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value
            if provider_id
            else ProcessingScope.LOCAL_PROCESSING.value
        )
        effective_data_category = data_category or scope
        record = ConsentRecord(
            mode=mode,
            principal_id=principal_id,
            scope=scope,
            granted_at=now_utc,
            project_id=project_id,
            tenant_id=tenant_id,
            processing_scope=effective_processing_scope,
            data_category=effective_data_category,
            provider_id=provider_id,
            expires_at_utc=expires_at_utc,
        )
        if self._store is not None:
            category = data_category or scope.upper()
            self._store.grant_consent(
                consent_id=record.consent_id,
                project_id=project_id or "_session",
                tenant_id=tenant_id,
                principal_id=principal_id,
                mode=mode,
                processing_scope=effective_processing_scope,
                data_category=category,
                provider_id=provider_id,
                granted_at_utc=now_utc,
                expires_at_utc=expires_at_utc,
            )
        self._consents.append(record)
        return record

    def has_consent(
        self,
        mode: PrivacyMode,
        *,
        principal_id: str,
        scope: str,
        project_id: str = "",
        processing_scope: str = "",
        data_category: str = "",
        tenant_id: str = "",
        provider_id: str = "",
    ) -> bool:
        effective_ps = (
            processing_scope
            or (
                ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value
                if provider_id
                else ProcessingScope.LOCAL_PROCESSING.value
            )
        ).strip()
        if self._store is not None:
            try:
                status, _grant = self._store.check_consent_status(
                    project_id=project_id or "_session",
                    principal_id=principal_id,
                    mode=mode,
                    processing_scope=effective_ps,
                    data_category=(data_category or scope).upper(),
                    now_utc=self._now(),
                    provider_id=provider_id,
                    tenant_id=tenant_id,
                )
            except PrivacyViolation:
                return False
            return status == "active"
        now_dt = _parse_utc(self._now())
        is_cloud_or_external = (
            mode is not PrivacyMode.LOCAL_VAULT
            or effective_ps == ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value
        )
        requested_cat = (data_category or scope).strip()
        for c in self._consents:
            if c.mode is not mode or c.principal_id != principal_id:
                continue
            record_ps = (
                c.processing_scope
                or (
                    ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value
                    if c.provider_id
                    else ProcessingScope.LOCAL_PROCESSING.value
                )
            ).strip()
            if record_ps != effective_ps:
                continue
            record_cat = (c.data_category or c.scope).strip()
            if not (
                record_cat == requested_cat
                or (not data_category and c.scope.strip() == scope.strip())
                or record_cat == "*"
                or c.scope.strip() == "*"
            ):
                continue
            if (project_id or c.project_id) and c.project_id != project_id:
                continue
            if (tenant_id or c.tenant_id) and c.tenant_id != tenant_id:
                continue
            if (
                is_cloud_or_external
                and (project_id or c.project_id or c.tenant_id)
                and not tenant_id.strip()
            ):
                continue
            if effective_ps == ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value:
                if (
                    not provider_id.strip()
                    or not c.provider_id.strip()
                    or c.provider_id != provider_id
                ):
                    continue
            elif (provider_id.strip() or c.provider_id.strip()) and c.provider_id != provider_id:
                continue
            if c.expires_at_utc and now_dt >= _parse_utc(c.expires_at_utc):
                continue
            return True
        return False

    def grant_durable_consent(
        self,
        *,
        project_id: str,
        principal_id: str,
        mode: PrivacyMode,
        processing_scope: ProcessingScope | str,
        data_category: DataCategory | str,
        tenant_id: str = "",
        provider_id: str = "",
        expires_at_utc: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> DurableConsentGrant:
        if self._store is None:
            raise PrivacyViolation(
                "store_unconfigured", "durable privacy consent store is not wired"
            )
        ps = (
            processing_scope.value
            if isinstance(processing_scope, ProcessingScope)
            else processing_scope
        )
        dc = data_category.value if isinstance(data_category, DataCategory) else data_category
        if ps not in VALID_PROCESSING_SCOPES:
            raise PrivacyViolation("invalid_scope", f"unsupported processing scope {ps!r}")
        if dc not in VALID_DATA_CATEGORIES:
            raise PrivacyViolation("invalid_category", f"unsupported data category {dc!r}")
        return self._store.grant_consent(
            project_id=project_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode=mode,
            processing_scope=ps,
            data_category=dc,
            provider_id=provider_id,
            granted_at_utc=self._now(),
            expires_at_utc=expires_at_utc,
            metadata=metadata,
        )

    def revoke_durable_consent(
        self,
        consent_id: str,
        *,
        actor_id: str,
        project_id: str | None = None,
        expected_tenant_id: str | None = None,
        reason: str = "user_revoked",
        metadata: dict[str, Any] | None = None,
    ) -> DurableConsentGrant:
        if self._store is None:
            raise PrivacyViolation(
                "store_unconfigured", "durable privacy consent store is not wired"
            )
        revoked = self._store.revoke_consent(
            consent_id,
            revoked_by=actor_id,
            revoked_at_utc=self._now(),
            reason=reason,
            project_id=project_id,
            expected_tenant_id=expected_tenant_id,
            metadata=metadata,
        )
        # Drop any matching legacy in-memory mirror as well.
        self._consents = [c for c in self._consents if c.consent_id != consent_id]
        return revoked

    def enforce_processing_consent(
        self,
        *,
        project_id: str,
        principal_id: str,
        mode: PrivacyMode,
        processing_scope: ProcessingScope | str,
        data_category: DataCategory | str,
        provider_id: str = "",
        tenant_id: str = "",
        require_local_biometric_consent: bool = True,
    ) -> DurableConsentGrant | None:
        """Fail-closed consent + egress gate with append-only audit on denial/expiry."""
        ps = (
            processing_scope.value
            if isinstance(processing_scope, ProcessingScope)
            else processing_scope
        )
        dc = data_category.value if isinstance(data_category, DataCategory) else data_category
        if ps not in VALID_PROCESSING_SCOPES:
            raise PrivacyViolation("invalid_scope", f"unsupported processing scope {ps!r}")
        if dc not in VALID_DATA_CATEGORIES:
            raise PrivacyViolation("invalid_category", f"unsupported data category {dc!r}")

        now_utc = self._now()
        if (
            ps == ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value
            and mode is PrivacyMode.LOCAL_VAULT
        ):
            if self._store is not None:
                self._store.record_audit_event(
                    consent_id="",
                    event_type="DENIED",
                    project_id=project_id,
                    principal_id=principal_id,
                    mode=mode.value,
                    processing_scope=ps,
                    data_category=dc,
                    provider_id=provider_id,
                    actor_id=principal_id,
                    reason="egress_refused_in_local_vault",
                    occurred_at_utc=now_utc,
                )
            raise PrivacyViolation("egress_refused", f"{dc} must not leave the local vault")

        needs_consent = (
            ps == ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value
            or mode is not PrivacyMode.LOCAL_VAULT
            or (require_local_biometric_consent and dc == DataCategory.BIOMETRIC_FACE_ROI.value)
        )
        if not needs_consent:
            return None

        if self._store is None:
            raise PrivacyViolation("consent_missing", f"no durable consent for {ps}:{dc}")

        try:
            status, grant = self._store.check_consent_status(
                project_id=project_id,
                principal_id=principal_id,
                mode=mode,
                processing_scope=ps,
                data_category=dc,
                now_utc=now_utc,
                provider_id=provider_id,
                tenant_id=tenant_id,
            )
        except PrivacyViolation as exc:
            self._store.record_audit_event(
                consent_id="",
                event_type="DENIED",
                project_id=project_id,
                principal_id=principal_id,
                mode=mode.value,
                processing_scope=ps,
                data_category=dc,
                provider_id=provider_id,
                actor_id=principal_id,
                reason=exc.reason,
                occurred_at_utc=now_utc,
            )
            raise
        if grant is not None and (
            (grant.tenant_id.strip() or tenant_id.strip())
            and grant.tenant_id.strip() != tenant_id.strip()
        ):
            self._store.record_audit_event(
                consent_id=grant.consent_id,
                event_type="DENIED",
                project_id=project_id,
                principal_id=principal_id,
                mode=mode.value,
                processing_scope=ps,
                data_category=dc,
                provider_id=provider_id,
                actor_id=principal_id,
                reason="consent_tenant_mismatch",
                occurred_at_utc=now_utc,
            )
            raise PrivacyViolation(
                "consent_tenant_mismatch",
                f"consent for project {project_id!r} does not belong to tenant {tenant_id!r}",
            )
        if status != "active":
            event_type: Literal["DENIED", "EXPIRED"] = (
                "EXPIRED" if status == "expired" else "DENIED"
            )
            reason = f"consent_{status}"
            self._store.record_audit_event(
                consent_id=grant.consent_id if grant is not None else "",
                event_type=event_type,
                project_id=project_id,
                principal_id=principal_id,
                mode=mode.value,
                processing_scope=ps,
                data_category=dc,
                provider_id=provider_id,
                actor_id=principal_id,
                reason=reason,
                occurred_at_utc=now_utc,
            )
            raise PrivacyViolation(reason, f"{ps}:{dc} failed ({status})")

        if ps == ProcessingScope.EXTERNAL_PROVIDER_DISPATCH.value:
            if not provider_id or provider_id not in self._configured_providers:
                self._store.record_audit_event(
                    consent_id=grant.consent_id if grant is not None else "",
                    event_type="DENIED",
                    project_id=project_id,
                    principal_id=principal_id,
                    mode=mode.value,
                    processing_scope=ps,
                    data_category=dc,
                    provider_id=provider_id,
                    actor_id=principal_id,
                    reason="no_provider_wired",
                    occurred_at_utc=now_utc,
                )
                raise PrivacyViolation(
                    "no_provider_wired",
                    f"{mode.value} has no configured provider {provider_id!r}",
                )
        return grant


__all__ = [
    "DurablePrivacyConsentStore",
    "PrivacyPolicy",
    "PrivacyViolation",
    "SENSITIVE_FIELDS",
    "VALID_DATA_CATEGORIES",
    "VALID_PROCESSING_SCOPES",
    "durable_privacy_consent_path",
    "redact_mapping",
]
