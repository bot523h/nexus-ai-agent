"""Idempotency contract for the Nagar command bus — in-memory today, durable tomorrow.

The bus guarantees that a redelivery with the same ``(project_id, operation,
idempotency_key)`` and identical logical payload returns the exact original
``CommandResult`` without re-executing the handler; the same key with a
different payload is a deterministic ``IdempotencyConflictError``. The store is
the only stateful piece of that guarantee:

* **Today:** :class:`InMemoryIdempotencyStore` holds reservations in a
  process-local dict protected by the bus's re-entrant lock. It is *not*
  durable across restart, crash or multi-process execution. The docstring and
  the tests state this explicitly (``idempotency_is_process_local``), so a
  caller cannot accidentally assume durability.
* **Tomorrow:** a durable store (e.g. a DB row or a file-backed KV) can replace
  the in-memory dict by implementing the same :class:`IdempotencyStore`
  protocol and being injected at composition time. The bus's pipeline
  (reserve → check preconditions → apply → commit) already uses only the
  protocol's methods, so swapping the backend does not change error semantics.
  A reference durable backend :class:`adapters.file_idempotency.FileIdempotencyStore`
  is shipped (atomic ``.tmp → rename`` JSON, ``RLock`` + reload-on-read) — it
  proves the protocol can be made durable across ``FileIdempotencyStore``
  instances sharing the same path and across process restart, while the bus
  still holds the external lock. A DB or SQLite backend would follow the same
  shape.

The abstraction is intentionally minimal: a typed key, a fingerprint, an
optional result, and a clear “in-flight” marker (``result is None``). No TTL,
no eviction and no distributed lock are introduced in this phase — those are
future extensions that must not destabilize the core contract. The tests prove
the contract, not the backend.

This satisfies Target G of the Vision hardening: the smallest correct
abstraction that can evolve toward durable idempotency, with an explicit,
testable failure mode for restart/crash.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from nexus_ai_agent.creative.studio.models import CommandResult


@dataclass(frozen=True)
class Reservation:
    """One idempotency reservation.

    ``fingerprint`` is the SHA-256 of the canonical command payload (see
    :func:`nexus_ai_agent.creative.studio.bus._fingerprint`). ``result`` is
    ``None`` while the handler is in flight and becomes the committed
    ``CommandResult`` afterwards.
    """

    fingerprint: str
    result: CommandResult | None = None


IdempotencyKey = tuple[str, str, str]  # (project_id, operation, idempotency_key)


class IdempotencyStore(Protocol):
    """Protocol for idempotency backends (in-memory today, durable later)."""

    def get(self, key: IdempotencyKey) -> Reservation | None: ...

    def put(self, key: IdempotencyKey, reservation: Reservation) -> None: ...

    def delete(self, key: IdempotencyKey) -> None: ...

    def clear(self) -> None: ...


class InMemoryIdempotencyStore:
    """Process-local, lock-free dict store (bus holds the external lock).

    Thread-safe for callers that hold the bus's ``RLock`` externally, but also
    safe to call without it (it has its own ``RLock``). No persistence,
    no cross-process sharing, no restart durability — documented and tested.
    """

    def __init__(self) -> None:
        self._store: dict[IdempotencyKey, Reservation] = {}
        self._lock = threading.RLock()

    def get(self, key: IdempotencyKey) -> Reservation | None:
        with self._lock:
            return self._store.get(key)

    def put(self, key: IdempotencyKey, reservation: Reservation) -> None:
        with self._lock:
            self._store[key] = reservation

    def delete(self, key: IdempotencyKey) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    # Exposed for introspection/tests only
    def __len__(self) -> int:  # pragma: no cover - convenience
        with self._lock:
            return len(self._store)

    def __contains__(self, key: object) -> bool:  # pragma: no cover
        with self._lock:
            return key in self._store


__all__ = ["IdempotencyKey", "IdempotencyStore", "InMemoryIdempotencyStore", "Reservation"]
