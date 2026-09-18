"""Structured, redacted lifecycle event logging (O1).

Every lifecycle event is logged once, as named key-value fields, with
secrets redacted *before* anything reaches a log line or a metric
(invariant I13).  The helper never raises: observability must not affect
the runtime, and a redaction failure fails safe (drop the field, keep the
event name).
"""

from __future__ import annotations

import logging

log = logging.getLogger("nexus_ai_agent.lifecycle")


def log_lifecycle_event(level: int, name: str, **fields: object) -> None:
    """Log one structured event with redacted key-value fields."""
    try:
        from nexus_ai_agent.infrastructure.observability.redaction import redact_fields

        safe_fields = redact_fields(fields)
    except Exception:
        # Fail-safe: if redaction itself breaks, never log raw field values.
        safe_fields = {key: "[REDACTED]" for key in fields}
    try:
        log.log(level, name, extra={"event": name, **safe_fields})
    except Exception:
        # Logging must never take the runtime down with it.
        pass


def error_field(exc: BaseException) -> str:
    """A redaction-safe one-line error description (no raw traceback)."""
    return f"{type(exc).__name__}: {exc}"
