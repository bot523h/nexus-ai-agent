"""Privacy / trust modes for live editing sessions.

Three modes, one default, explicit consent:

* ``LOCAL_VAULT`` — **default**; audio/transcript/artifacts never leave the
  process; records carry digests only, never raw transcript or audio bytes;
* ``TRUSTED_CLOUD`` / ``EXPERIMENTAL_CLOUD`` — selectable **only** with an
  explicit recorded :class:`ConsentRecord`; no cloud provider is wired in this
  slice, so any egress request under a cloud mode is a typed refusal and no
  user data leaves the vault.

Changing the mode is explicit and recorded; the mode is never silently cloud.
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
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from nexus_ai_agent.nagar.ibpc.canonical import digest_of
from nexus_ai_agent.nagar.ibpc.models import ConsentRecord, PrivacyMode

#: Fields that must never appear in a log/record as raw text — digests only.
SENSITIVE_FIELDS = frozenset({"transcript", "text", "audio", "audio_bytes", "prompt", "content"})


class PrivacyViolation(RuntimeError):
    """Typed refusal: an operation would move or expose protected data."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}".strip(": "))
        self.reason = reason
        self.detail = detail


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


class PrivacyPolicy:
    """Mode + consent enforcement for one session."""

    DEFAULT_MODE = PrivacyMode.LOCAL_VAULT

    def __init__(self, *, now: Callable[[], str]) -> None:
        self._now = now
        self._consents: list[ConsentRecord] = []

    @property
    def consents(self) -> tuple[ConsentRecord, ...]:
        return tuple(self._consents)

    def require_local(self, mode: PrivacyMode, data_kind: str) -> None:
        """Local processing is always allowed; this is a no-op gate that
        documents the data-flow direction (nothing leaves)."""
        if not isinstance(mode, PrivacyMode):
            raise PrivacyViolation("invalid_mode", f"unknown privacy mode {mode!r}")

    def check_egress(self, mode: PrivacyMode, data_kind: str, *, consented: bool) -> None:
        """May ``data_kind`` leave the local vault under ``mode``?

        LOCAL_VAULT: egress is refused outright (there is nowhere to send).
        Cloud modes: refused unless an explicit consent record exists — and
        even then, no provider is wired in this slice, so the answer stays a
        typed refusal. User data is never sent to an unknown provider.
        """
        if mode is PrivacyMode.LOCAL_VAULT:
            raise PrivacyViolation("egress_refused", f"{data_kind} must not leave the local vault")
        if not consented:
            raise PrivacyViolation(
                "consent_missing",
                f"{mode.value} requires an explicit recorded consent for {data_kind}",
            )
        raise PrivacyViolation(
            "no_provider_wired",
            f"{mode.value} has no configured provider in this slice; {data_kind} stays local",
        )

    def record_consent(self, mode: PrivacyMode, *, principal_id: str, scope: str) -> ConsentRecord:
        """Record an explicit consent for a non-default mode (auditable)."""
        if not isinstance(mode, PrivacyMode):
            raise PrivacyViolation("invalid_mode", f"unknown privacy mode {mode!r}")
        record = ConsentRecord(
            mode=mode, principal_id=principal_id, scope=scope, granted_at=self._now()
        )
        self._consents.append(record)
        return record

    def has_consent(self, mode: PrivacyMode, *, principal_id: str, scope: str) -> bool:
        return any(
            c.mode is mode and c.principal_id == principal_id and scope in c.scope
            for c in self._consents
        )


__all__ = ["PrivacyPolicy", "PrivacyViolation", "SENSITIVE_FIELDS", "redact_mapping"]


# --------------------------------------------------------------------------- #
# Durable, revocable consent ledger (Wave 3, slice 3.3)
# --------------------------------------------------------------------------- #
#
# The in-memory ``PrivacyPolicy`` above is the *session* gate.  The ledger below
# is the durability + revocation authority behind it:
#
# * consent is granted per ``(principal_id, project_id, provider_id,
#   data_category)`` for one ``ConsentScope`` and one processing ``ConsentAction``
#   (local processing vs external provider upload), time-bounded by
#   ``expires_at_utc``;
# * revocation is immediate and append-only: the grant row is flipped to
#   REVOKED *and* an audit row is written in the same ``BEGIN IMMEDIATE``
#   transaction, so no reader can observe a revoked-but-still-usable window;
# * the gate ``evaluate_processing_consent`` fails closed with a typed
#   ``PrivacyViolation`` for every reason (missing, expired, revoked, wrong
#   provider, wrong action, wrong category);
# * no raw sensitive media payload is ever persisted — only ids, enums,
#   timestamps and digests.

#: Default human-readable reason recorded with a consent grant.
DEFAULT_CONSENT_REASON = "ibpc-processing"

#: ``busy_timeout`` for every connection opened by the durable consent store.
_BUSY_TIMEOUT_MS = 5000


class ConsentAction(str, Enum):
    """Which processing direction a consent authorizes."""

    LOCAL_PROCESSING = "LOCAL_PROCESSING"
    EXTERNAL_PROVIDER_UPLOAD = "EXTERNAL_PROVIDER_UPLOAD"


class ConsentScope(str, Enum):
    """The media/data categories a consent can cover (closed literal set)."""

    AUDIO = "AUDIO"
    VIDEO = "VIDEO"
    IMAGE = "IMAGE"
    TRANSCRIPT = "TRANSCRIPT"
    BIOMETRIC_FACE_ROI = "BIOMETRIC_FACE_ROI"


class ConsentState(str, Enum):
    GRANTED = "GRANTED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class PrivacyConsentGrant:
    """One durable consent grant (ids, enums, timestamps — never payload)."""

    consent_id: str
    principal_id: str
    project_id: str
    provider_id: str
    data_category: ConsentScope
    action: ConsentAction
    reason: str
    granted_at_utc: str
    expires_at_utc: str
    state: ConsentState
    revoked_at_utc: str | None


class ConsentStoreError(PrivacyViolation):
    """Typed refusal from the durable consent store."""


_CONSENT_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS privacy_consents ("
    " consent_id TEXT PRIMARY KEY,"
    " principal_id TEXT NOT NULL,"
    " project_id TEXT NOT NULL,"
    " provider_id TEXT NOT NULL,"
    " data_category TEXT NOT NULL,"
    " action TEXT NOT NULL,"
    " reason TEXT NOT NULL,"
    " granted_at_utc TEXT NOT NULL,"
    " expires_at_utc TEXT NOT NULL,"
    " state TEXT NOT NULL,"
    " revoked_at_utc TEXT,"
    " consent_sha256 TEXT NOT NULL,"
    " UNIQUE(principal_id, project_id, provider_id, data_category, action))",
    "CREATE TABLE IF NOT EXISTS privacy_consent_audit_log ("
    " event_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " consent_id TEXT NOT NULL,"
    " principal_id TEXT NOT NULL,"
    " project_id TEXT NOT NULL,"
    " provider_id TEXT NOT NULL,"
    " data_category TEXT NOT NULL,"
    " action TEXT NOT NULL,"
    " event TEXT NOT NULL CHECK(event IN ('GRANTED', 'REVOKED')),"
    " recorded_at_utc TEXT NOT NULL,"
    " previous_sha256 TEXT,"
    " event_sha256 TEXT NOT NULL)",
)


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ConsentStoreError("invalid_timestamp", f"{value!r} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


class DurablePrivacyConsentStore:
    """SQLite-WAL consent ledger with immediate revocation and an audit trail."""

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
            for statement in _CONSENT_SCHEMA:
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

    # -- grant ---------------------------------------------------------------

    def grant_consent(
        self,
        *,
        principal_id: str,
        project_id: str,
        provider_id: str,
        data_category: ConsentScope,
        action: ConsentAction,
        expires_at_utc: str,
        reason: str = DEFAULT_CONSENT_REASON,
    ) -> PrivacyConsentGrant:
        """Record (or idempotently re-affirm) one time-bounded consent grant."""
        if not isinstance(data_category, ConsentScope):
            raise ConsentStoreError("invalid_category", f"{data_category!r} is not a ConsentScope")
        if not isinstance(action, ConsentAction):
            raise ConsentStoreError("invalid_action", f"{action!r} is not a ConsentAction")
        for name, value in (
            ("principal_id", principal_id),
            ("project_id", project_id),
            ("provider_id", provider_id),
            ("reason", reason),
        ):
            if not isinstance(value, str) or not value:
                raise ConsentStoreError("invalid_grant", f"{name} must be a non-empty string")
        if len(reason) > 256:
            raise ConsentStoreError("invalid_grant", "reason is longer than 256 characters")
        expires = _parse_instant(expires_at_utc)
        granted_at = _parse_instant(self._now())
        if expires <= granted_at:
            raise ConsentStoreError(
                "invalid_window", f"expires_at_utc {expires_at_utc!r} is not in the future"
            )
        with self._transaction():
            existing = self._fetch_one(principal_id, project_id, provider_id, data_category, action)
            digest = digest_of(
                {
                    "principal_id": principal_id,
                    "project_id": project_id,
                    "provider_id": provider_id,
                    "data_category": data_category.value,
                    "action": action.value,
                    "expires_at_utc": expires_at_utc,
                }
            )
            if existing is not None and existing.state is ConsentState.GRANTED:
                if existing.expires_at_utc == expires_at_utc:
                    return existing
                raise ConsentStoreError(
                    "duplicate_grant", "an active grant already exists with a different expiry"
                )
            consent_id = f"consent_{uuid.uuid4().hex}"
            stamp = self._now()
            self._conn.execute(
                "INSERT INTO privacy_consents (consent_id, principal_id, project_id, provider_id,"
                " data_category, action, reason, granted_at_utc, expires_at_utc, state,"
                " revoked_at_utc, consent_sha256)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)"
                " ON CONFLICT(principal_id, project_id, provider_id, data_category, action)"
                " DO UPDATE SET consent_id=excluded.consent_id, reason=excluded.reason,"
                " granted_at_utc=excluded.granted_at_utc, expires_at_utc=excluded.expires_at_utc,"
                " state=excluded.state, revoked_at_utc=NULL,"
                " consent_sha256=excluded.consent_sha256",
                (
                    consent_id,
                    principal_id,
                    project_id,
                    provider_id,
                    data_category.value,
                    action.value,
                    reason,
                    stamp,
                    expires_at_utc,
                    ConsentState.GRANTED.value,
                    digest,
                ),
            )
            self._append_audit(
                consent_id=consent_id,
                principal_id=principal_id,
                project_id=project_id,
                provider_id=provider_id,
                data_category=data_category,
                action=action,
                event="GRANTED",
                stamp=stamp,
            )
            record = self._fetch_one(principal_id, project_id, provider_id, data_category, action)
        if record is None:  # pragma: no cover - a committed row is always visible
            raise ConsentStoreError("grant_failed", "the committed grant could not be read back")
        return record

    # -- revoke --------------------------------------------------------------

    def revoke_consent(
        self,
        *,
        principal_id: str,
        project_id: str,
        provider_id: str,
        data_category: ConsentScope,
        action: ConsentAction,
    ) -> PrivacyConsentGrant:
        """Revoke immediately: flip the row and append the audit event atomically."""
        with self._transaction():
            existing = self._fetch_one(principal_id, project_id, provider_id, data_category, action)
            if existing is None:
                raise ConsentStoreError("unknown_consent", "no matching consent grant exists")
            if existing.state is ConsentState.REVOKED:
                return existing
            stamp = self._now()
            self._conn.execute(
                "UPDATE privacy_consents SET state = ?, revoked_at_utc = ? WHERE consent_id = ?",
                (ConsentState.REVOKED.value, stamp, existing.consent_id),
            )
            self._append_audit(
                consent_id=existing.consent_id,
                principal_id=principal_id,
                project_id=project_id,
                provider_id=provider_id,
                data_category=data_category,
                action=action,
                event="REVOKED",
                stamp=stamp,
            )
            record = self._fetch_one(principal_id, project_id, provider_id, data_category, action)
        if record is None:  # pragma: no cover - the revoked row must be readable
            raise ConsentStoreError("revoke_failed", "the revoked grant could not be read back")
        return record

    def revoke_all_for_principal(self, principal_id: str) -> tuple[PrivacyConsentGrant, ...]:
        """Revoke every live grant of one principal (account deletion / opt-out)."""
        with self._transaction():
            rows = self._conn.execute(
                "SELECT principal_id, project_id, provider_id, data_category, action"
                " FROM privacy_consents WHERE principal_id = ? AND state = ?",
                (principal_id, ConsentState.GRANTED.value),
            ).fetchall()
            revoked: list[PrivacyConsentGrant] = []
            for row in rows:
                revoked.append(
                    self.revoke_consent(
                        principal_id=row[0],
                        project_id=row[1],
                        provider_id=row[2],
                        data_category=ConsentScope(row[3]),
                        action=ConsentAction(row[4]),
                    )
                )
            return tuple(revoked)

    # -- gate ----------------------------------------------------------------

    def evaluate_processing_consent(
        self,
        *,
        principal_id: str,
        project_id: str,
        provider_id: str,
        data_category: ConsentScope,
        action: ConsentAction,
    ) -> PrivacyConsentGrant:
        """Fail-closed gate: may this processing step run right now?

        Every failure raises :class:`PrivacyViolation` with a stable ``reason``;
        success returns the GRANTED, unexpired :class:`PrivacyConsentGrant`.  Reading
        runs inside a transaction so a concurrent ``revoke_consent`` cannot be
        observed half-applied.
        """
        if not isinstance(data_category, ConsentScope):
            raise ConsentStoreError("invalid_category", f"{data_category!r} is not a ConsentScope")
        if not isinstance(action, ConsentAction):
            raise ConsentStoreError("invalid_action", f"{action!r} is not a ConsentAction")
        with self._transaction():
            record = self._fetch_one(principal_id, project_id, provider_id, data_category, action)
            if record is None:
                raise ConsentStoreError(
                    "consent_missing",
                    f"no {action.value} consent for {data_category.value} on {provider_id!r}",
                )
            if record.state is ConsentState.REVOKED:
                raise ConsentStoreError(
                    "consent_revoked", f"consent {record.consent_id} was revoked"
                )
            if _parse_instant(self._now()) >= _parse_instant(record.expires_at_utc):
                raise ConsentStoreError(
                    "consent_expired",
                    f"consent {record.consent_id} expired at {record.expires_at_utc}",
                )
            return record

    # -- read ----------------------------------------------------------------

    def get_consent(
        self,
        *,
        principal_id: str,
        project_id: str,
        provider_id: str,
        data_category: ConsentScope,
        action: ConsentAction,
    ) -> PrivacyConsentGrant:
        record = self._fetch_one(principal_id, project_id, provider_id, data_category, action)
        if record is None:
            raise ConsentStoreError("unknown_consent", "no matching consent grant exists")
        return record

    def audit_log(self) -> tuple[dict[str, str], ...]:
        """The append-only audit trail (ids/enums/timestamps/digests only)."""
        rows = self._conn.execute(
            "SELECT event_id, consent_id, principal_id, project_id, provider_id, data_category,"
            " action, event, recorded_at_utc, previous_sha256, event_sha256"
            " FROM privacy_consent_audit_log ORDER BY event_id"
        ).fetchall()
        return tuple(
            {
                "event_id": str(row[0]),
                "consent_id": row[1],
                "principal_id": row[2],
                "project_id": row[3],
                "provider_id": row[4],
                "data_category": row[5],
                "action": row[6],
                "event": row[7],
                "recorded_at_utc": row[8],
                "previous_sha256": row[9] or "",
                "event_sha256": row[10],
            }
            for row in rows
        )

    def verify_audit_chain(self) -> bool:
        """Recompute the audit chain; a deleted/edited event never verifies."""
        previous = ""
        for event in self.audit_log():
            expected = digest_of(
                {
                    "consent_id": event["consent_id"],
                    "principal_id": event["principal_id"],
                    "project_id": event["project_id"],
                    "provider_id": event["provider_id"],
                    "data_category": event["data_category"],
                    "action": event["action"],
                    "event": event["event"],
                    "recorded_at_utc": event["recorded_at_utc"],
                    "previous_sha256": previous,
                }
            )
            if expected != event["event_sha256"]:
                return False
            previous = expected
        return True

    def _append_audit(
        self,
        *,
        consent_id: str,
        principal_id: str,
        project_id: str,
        provider_id: str,
        data_category: ConsentScope,
        action: ConsentAction,
        event: str,
        stamp: str,
    ) -> None:
        row = self._conn.execute(
            "SELECT event_sha256 FROM privacy_consent_audit_log ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous = "" if row is None else str(row[0])
        digest = digest_of(
            {
                "consent_id": consent_id,
                "principal_id": principal_id,
                "project_id": project_id,
                "provider_id": provider_id,
                "data_category": data_category.value,
                "action": action.value,
                "event": event,
                "recorded_at_utc": stamp,
                "previous_sha256": previous,
            }
        )
        self._conn.execute(
            "INSERT INTO privacy_consent_audit_log (consent_id, principal_id, project_id,"
            " provider_id, data_category, action, event, recorded_at_utc, previous_sha256,"
            " event_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                consent_id,
                principal_id,
                project_id,
                provider_id,
                data_category.value,
                action.value,
                event,
                stamp,
                previous,
                digest,
            ),
        )

    def _fetch_one(
        self,
        principal_id: str,
        project_id: str,
        provider_id: str,
        data_category: ConsentScope,
        action: ConsentAction,
    ) -> PrivacyConsentGrant | None:
        row = self._conn.execute(
            "SELECT consent_id, principal_id, project_id, provider_id, data_category, action,"
            " reason, granted_at_utc, expires_at_utc, state, revoked_at_utc"
            " FROM privacy_consents WHERE principal_id = ? AND project_id = ? AND provider_id = ?"
            " AND data_category = ? AND action = ?",
            (principal_id, project_id, provider_id, data_category.value, action.value),
        ).fetchone()
        if row is None:
            return None
        return PrivacyConsentGrant(
            consent_id=row[0],
            principal_id=row[1],
            project_id=row[2],
            provider_id=row[3],
            data_category=ConsentScope(row[4]),
            action=ConsentAction(row[5]),
            reason=row[6],
            granted_at_utc=row[7],
            expires_at_utc=row[8],
            state=ConsentState(row[9]),
            revoked_at_utc=row[10],
        )


def durable_privacy_consent_path(workspace: str | Path) -> Path:
    """Canonical SQLite location for the durable privacy-consent ledger."""
    return Path(workspace) / "queue" / "ibpc_privacy_consent.sqlite3"


__all__ = [
    "ConsentAction",
    "ConsentScope",
    "ConsentState",
    "ConsentStoreError",
    "DurablePrivacyConsentStore",
    "PrivacyConsentGrant",
    "PrivacyPolicy",
    "PrivacyViolation",
    "SENSITIVE_FIELDS",
    "durable_privacy_consent_path",
    "redact_mapping",
]
