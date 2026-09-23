"""Correlation ID lifecycle — M0 audit and implementation.

Audit of existing correlation-id usage (2026-09-23):

Existing code:
  - bot/handlers.py: correlation_id = str(uuid4()),
    bound via structlog.contextvars.bind_contextvars,
    then state["correlation_id"] = correlation_id
  - orchestration/state.py: TypedDict NexusState has correlation_id
  - storage/models.py: correlation_id field indexed
  - cli.py: correlation_id per CLI command via uuid4()

Lifecycle answers:
  - per update? YES: new uuid4 per Telegram Update in handlers.py
  - per job? PARTIAL: job payload does NOT yet carry correlation_id
  - per external effect? NO: external calls do not yet receive
    correlation_id header/field

This module provides deterministic, non-secret, non-PII correlation ID:

  - generation: uuid4 hex, or deterministic from update_id
  - never secret: uuid4 random, not credential, no API key
  - never user-sensitive: no telegram_id, no user text
  - never high-cardinality metric label: NOT used as metric label

Propagation:
  - bind_correlation_id(id): binds to structlog contextvars
  - get_correlation_id(): retrieves current bound id
  - clear_correlation_id(): clears after request
  - new_correlation_id(): random id (hex, 32 chars)
  - deterministic_correlation_id(seed): deterministic id from seed
  - extract_from_payload(payload): extracts correlation_id if present
  - inject_into_payload(payload, cid): injects correlation_id

Tests must prove:
  - generation unique per call
  - deterministic generation stable for same seed
  - propagation via contextvars works
  - correlation_id never used as metric label (guard test)
  - payload injection/extraction round-trips
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import structlog

CORRELATION_ID_KEY: str = "correlation_id"


def new_correlation_id() -> str:
    """Generate a new random correlation ID (32 hex chars, no dashes)."""
    return uuid.uuid4().hex


def deterministic_correlation_id(seed: str | int) -> str:
    """Deterministic correlation ID from a seed (e.g., update_id).

    Uses SHA-256 of seed string, truncated to 32 hex chars.
    Deterministic enough for tracing, but not reversible to original seed
    if seed is sensitive (though seed should never be sensitive).

    Example:
      update_id 12345 -> deterministic id "a1b2c3..." (stable)
    """
    h = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
    return h[:32]


def bind_correlation_id(correlation_id: str) -> None:
    """Bind correlation_id to structlog contextvars for log inclusion."""
    structlog.contextvars.bind_contextvars(**{CORRELATION_ID_KEY: correlation_id})


def get_correlation_id() -> str | None:
    """Get current correlation_id from contextvars, if any."""
    try:
        ctx = structlog.contextvars.get_contextvars()
        return ctx.get(CORRELATION_ID_KEY)
    except Exception:
        return None


def clear_correlation_id() -> None:
    """Clear correlation_id from contextvars."""
    try:
        structlog.contextvars.unbind_contextvars(CORRELATION_ID_KEY)
    except Exception:
        pass


def extract_from_payload(payload: dict[str, Any]) -> str | None:
    """Extract correlation_id from a job payload dict, if present."""
    cid = payload.get(CORRELATION_ID_KEY)
    if isinstance(cid, str) and cid:
        return cid
    return None


def inject_into_payload(payload: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Inject correlation_id into payload (new dict, non-destructive)."""
    new_payload = dict(payload)
    new_payload[CORRELATION_ID_KEY] = correlation_id
    return new_payload


def ensure_correlation_id(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Ensure payload has a correlation_id; generate if missing.

    Returns (new_payload, correlation_id)
    """
    existing = extract_from_payload(payload)
    if existing:
        return payload, existing
    cid = new_correlation_id()
    return inject_into_payload(payload, cid), cid


# Lifecycle documentation as constants for audit
LIFECYCLE_DOC: dict[str, str] = {
    "per_update": (
        "YES — new uuid4 per Telegram Update in handlers.py "
        "message_handler, bound to contextvars, stored in NexusState"
    ),
    "per_job": (
        "SHOULD — job payload SHOULD carry correlation_id via "
        "inject_into_payload; completion hook and diagnostics SHOULD log it; "
        "currently PARTIAL in production, M0 provides helpers"
    ),
    "per_external_effect": (
        "SHOULD — external effect (LLM, image gen, R2) SHOULD include "
        "correlation_id as log field, never as metric label; not yet as "
        "header, M0 provides contract"
    ),
    "deterministic": (
        "deterministic_correlation_id(update_id) allows tracing same update "
        "across retries; random new_correlation_id() for new work"
    ),
    "never_secret": (
        "correlation_id is uuid4 hex, not credential, not API key, not bot token; safe to log"
    ),
    "never_pii": (
        "correlation_id contains no telegram_id, no username, no user text, no raw payload"
    ),
    "never_metric_label": (
        "correlation_id MUST NOT be used as metric label — high cardinality, "
        "would explode TSDB; use only as log field"
    ),
}


__all__ = [
    "CORRELATION_ID_KEY",
    "LIFECYCLE_DOC",
    "bind_correlation_id",
    "clear_correlation_id",
    "deterministic_correlation_id",
    "ensure_correlation_id",
    "extract_from_payload",
    "get_correlation_id",
    "inject_into_payload",
    "new_correlation_id",
]
