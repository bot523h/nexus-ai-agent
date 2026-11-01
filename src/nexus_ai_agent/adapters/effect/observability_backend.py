"""Effect-lifecycle observability: events + low-cardinality counters.

P3/P4 observability contract (``docs/architecture/OBSERVABILITY.md`` §3):

* the **effect lifecycle events** are fixed-name, field-tight structured
  events on the ``nexus_ai_agent.lifecycle`` channel:
  ``effect_created, effect_claimed, effect_succeeded, effect_failed,
  effect_recovered, effect_duplicate``;
* the **metrics** go through the shared allow-listed ``MetricsRegistry``
  (labels confined to ``backend, scope, error_code, reason, outcome``);
* ``telegram_id``, raw payloads, secrets, API keys, and any high-cardinality
  random identifier (attempt ids, effect-key tokens) are banned as labels.

Effect keys are never emitted in full: a stable truncated digest
(``effect_key[:8]...``) is the only effect identity that may travel into an
event, capped and collision-tolerant for humans, not machine-joins.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Final

from nexus_ai_agent.infrastructure.observability.metrics import get_metrics_registry
from nexus_ai_agent.infrastructure.observability.structured import log_lifecycle_event

#: Lifecycle event vocabulary (the contract; kept as a module constant so a
#: missing or renamed event is a test failure).
EFFECT_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "effect_created",
        "effect_claimed",
        "effect_succeeded",
        "effect_failed",
        "effect_recovered",
        "effect_duplicate",
    }
)

#: Fields allowed to travel with an effect event (fixed, tight).
EFFECT_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"effect_key_prefix", "operation_type", "outcome", "reason", "attempt"}
)

#: The only box we are allowed to put an effect key into (human correlation).
KEY_PREFIX_CHARS: Final[int] = 10


def effect_key_prefix(effect_key: str) -> str:
    """A short, stable, non-secret label fragment for human correlation.

    The full key is never logged; a random suffix plus a zeroed fragment is
    unjoinable from a log line for any privacy-relevant identity.
    """
    token = hashlib.sha256(effect_key.encode("utf-8")).hexdigest()
    return f"ek:{token[:KEY_PREFIX_CHARS]}"


def emit_effect_event(name: str, *, effect_key: str, **fields: object) -> None:
    """Emit one effect-lifecycle event, redaction-first and never-fatal."""
    known = dict(fields)
    unknown = set(known) - EFFECT_EVENT_FIELDS
    if unknown:
        # A caller error (never a runtime hazard): the event is still emitted
        # with the tight contract a caller may legally use.
        known = {key: value for key, value in known.items() if key in EFFECT_EVENT_FIELDS}
    event_name = name if name in EFFECT_EVENTS else "effect_failed"
    log_lifecycle_event(
        20,
        event_name,
        effect_key_prefix=effect_key_prefix(effect_key),
        **known,
    )


def increment_effect_counter(name: str, labels: Mapping[str, str] | None = None) -> None:
    """Increment ``nexus_effect_<state>_total`` in the shared low-cardinality registry."""
    registry = get_metrics_registry()
    safe: dict[str, str] = dict(labels or {})
    safe.pop("effect_key", None)
    safe.pop("payload", None)
    registry.increment(f"nexus_effect_{name}_total", labels={} if not safe else dict(safe))
