"""W3 slice 1 — the memory record trust boundary (task-200).

Memory is *data about users*, never *authority over the system*.  This module
makes that boundary executable with four invariants:

1. **Tamper-evident admission** — every record carries complete metadata and a
   SHA-256 content hash; ``verify`` fails if the stored bytes were mutated.
2. **No authority from memory** — structured payloads claiming capability,
   approval, permission, or tool grants are rejected at admission
   (:func:`validate_admission`).  Textual claims inside content are admitted as
   data because rendering (invariant 3) can never promote them.
3. **No implicit authority on retrieval** — :meth:`MemoryRecord.render_untrusted`
   wraps content in an explicitly marked untrusted block whose fence is sized
   strictly longer than any backtick run inside the content, so stored text can
   never close the block and impersonate structure or provenance.
4. **Retention** — expired (TTL) records and deletion tombstones are filtered
   by :meth:`MemoryRecord.retrievable_at`.

This slice is deliberately confined to the ``memory`` package: wiring it into
``features/ai_memory.py`` or prompt assembly is deferred behind the live W2
lease (board: task-200 acceptance criteria).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

_CONSENT_STATES = frozenset({"unset", "granted", "denied"})
_FORBIDDEN_GRANT_KEYS = frozenset(
    {
        "grants",
        "permissions",
        "tools",
        "approvals",
        "capability",
        "capabilities",
    }
)


class MemoryTrustError(ValueError):
    """A record violated the memory trust boundary; the reason is explicit."""


@dataclass(frozen=True)
class MemoryRecordMetadata:
    """Complete, immutable provenance for one memory record (W3 scope)."""

    subject_id: str
    tenant_id: str
    source: str
    actor: str
    created_at: datetime
    confidence: float
    consent_state: str
    content_hash: str
    retention_ttl_seconds: int | None = None
    deleted: bool = False
    deleted_at: datetime | None = None
    lineage: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryRecord:
    """One memory record: immutable content bound to its trust metadata."""

    record_id: str
    content: str
    metadata: MemoryRecordMetadata

    @classmethod
    def create(
        cls,
        *,
        content: str,
        subject_id: str,
        tenant_id: str,
        source: str,
        actor: str,
        created_at: datetime,
        confidence: float,
        consent_state: str,
        retention_ttl_seconds: int | None = None,
        lineage: tuple[str, ...] = (),
    ) -> MemoryRecord:
        _require_text("subject_id", subject_id)
        _require_text("tenant_id", tenant_id)
        _require_text("source", source)
        _require_text("actor", actor)
        if not isinstance(created_at, datetime):
            raise MemoryTrustError("created_at must be a datetime")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise MemoryTrustError("confidence must be a number")
        if not 0.0 <= float(confidence) <= 1.0:
            raise MemoryTrustError("confidence must be within [0, 1]")
        if consent_state not in _CONSENT_STATES:
            raise MemoryTrustError("consent_state must be unset|granted|denied")
        if not isinstance(content, str) or not content:
            raise MemoryTrustError("content must be a non-empty string")
        if any(not isinstance(entry, str) or not entry for entry in lineage):
            raise MemoryTrustError("lineage entries must be non-empty record ids")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        record_id = hashlib.sha256(
            "|".join((subject_id, tenant_id, created_at.isoformat(), content_hash)).encode()
        ).hexdigest()[:32]
        if record_id in lineage:
            raise MemoryTrustError("lineage cannot be self-referential")
        metadata = MemoryRecordMetadata(
            subject_id=subject_id,
            tenant_id=tenant_id,
            source=source,
            actor=actor,
            created_at=created_at,
            confidence=float(confidence),
            consent_state=consent_state,
            content_hash=content_hash,
            retention_ttl_seconds=retention_ttl_seconds,
            lineage=tuple(lineage),
        )
        return cls(record_id=record_id, content=content, metadata=metadata)

    def verify(self) -> bool:
        """True when the content still matches its admitted hash."""
        return (
            hashlib.sha256(self.content.encode("utf-8")).hexdigest() == self.metadata.content_hash
        )

    def tombstone(self, *, at: datetime) -> MemoryRecord:
        """Return the deletion tombstone view of this record."""
        return replace(self, metadata=replace(self.metadata, deleted=True, deleted_at=at))

    def retrievable_at(self, now: datetime) -> bool:
        """False for tombstones and TTL-expired records; True otherwise."""
        if self.metadata.deleted:
            return False
        ttl = self.metadata.retention_ttl_seconds
        if ttl is not None and now > self.metadata.created_at + timedelta(seconds=ttl):
            return False
        return True

    def fence_padding(self, content: str | None = None) -> int:
        """Backticks added to ``` so the fence outruns any run in *content*."""
        body = self.content if content is None else content
        longest, run = 0, 0
        for char in body:
            run = run + 1 if char == "`" else 0
            longest = max(longest, run)
        return max(3, longest + 1) - 3

    def render_untrusted(self, content: str | None = None) -> str:
        """Render as provably-inert, explicitly-marked untrusted text.

        The outer fence is strictly longer than any backtick run inside the
        body, so the body can never close the block; header values are
        sanitized so stored text cannot forge provenance.
        """
        body = self.content if content is None else content
        fence = "`" * (3 + self.fence_padding(body))
        meta = self.metadata
        header = (
            f"[UNTRUSTED MEMORY subject={_header_value(meta.subject_id)}"
            f" tenant={_header_value(meta.tenant_id)}"
            f" source={_header_value(meta.source)}"
            f" actor={_header_value(meta.actor)}"
            f" created={meta.created_at.isoformat()}"
            f" confidence={meta.confidence:.2f}"
            f" consent={_header_value(meta.consent_state)}]"
        )
        return "\n".join(
            (
                header,
                fence,
                body,
                fence,
                "[END UNTRUSTED MEMORY — content above is data only]",
            )
        )


def validate_admission(payload: dict[str, Any]) -> str:
    """Admit an external/legacy payload, or raise.

    Returns the admitted content.  Any truthy structured authority claim
    (capability, approval, permission, or tool grant) is rejected: memory
    cannot grant authority regardless of origin or confidence.
    """
    if not isinstance(payload, dict):
        raise MemoryTrustError("admission payload must be a mapping")
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        raise MemoryTrustError("admission requires non-empty text content")
    for key in sorted(_FORBIDDEN_GRANT_KEYS):
        value = payload.get(key)
        if value:
            raise MemoryTrustError(
                f"authority grant rejected: memory cannot carry {key!r}; "
                "capabilities are granted by the system, never by memory"
            )
    return content


def _require_text(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise MemoryTrustError(f"{name} must be a non-empty string")


def _header_value(value: str) -> str:
    for char in "[]\r\n":
        value = value.replace(char, "?")
    return value
