"""Pure contracts for durable plan deduplication.

SQLite and filesystem access live in the infrastructure adapter.
"""

from __future__ import annotations

from dataclasses import dataclass


class PlanReplayConflictError(ValueError):
    """An existing plan identity has a different fingerprint."""


class PlanInDoubtQuarantineError(RuntimeError):
    """An existing plan may have performed effects and must not be replayed."""


class PlanStoreCapacityError(RuntimeError):
    """The bounded store cannot accept another durable identity."""


class PlanStoreStateError(RuntimeError):
    """A requested transition is not permitted."""


@dataclass(frozen=True)
class DurablePlanRecord:
    plan_id: str
    plan_hash: str
    status: str
    external_effects: bool
    evidence_json: str | None
