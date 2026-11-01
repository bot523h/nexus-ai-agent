"""Outbox: the durable delivery-intent contract (P3).

The port is the *application-owned* half of the transactional-outbox seam. It
splits "please perform this external effect" into two duties that MUST NOT
live in one try/except again:

1. ``append_intent`` — write the delivery intent durably **with the business
   change that justifies it** (inside the same transaction, by whichever
   adapter owns the shared connection). The adapter either shares the caller's
   transaction (an exchanged session) or refuses the call — it must never
   opportunistically remember an intent that belongs to a rolled-back change.
2. ``inspect_intent`` — read a snapshot back for dispatch, dedupe and
   reconciliation.

The ``dispatcher`` (reference implementation: ``adapters/effect/outbox_dispatcher.py``)
does the claiming, the at-least-once delivery, and the outcome recording; a
``pending effect`` here is only *intent*, never *execution*.

Framework-free by design (R1): the module imports nothing, and the frozen
baseline test keeps it that way.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nexus_ai_agent.domain.policies.outbox_policy import EffectStatus


@runtime_checkable
class EffectIntent(Protocol):
    """Read-only view of one delivery-intent row.

    Implementations return a lightweight value object — never a live ORM row,
    never a raw payload. ``effect_key`` is the *typed* logical key
    (``operation.token``); ``attempt_id`` exists per claim so the mechanism and
    the intent stay told apart (P4 guardrail).
    """

    effect_key: str
    operation_type: str
    status: EffectStatus
    attempt: int
    claim_token: str | None
    last_error: str | None


class OutboxPort(Protocol):
    """Durable delivery-intent contract (persistence + dispatch lifecycle)."""

    async def append_intent(
        self,
        *,
        operation_type: str,
        effect_key: str,
        destination: str,
        logical_entity: str,
        payload: dict[str, object],
    ) -> str:
        """Durably record one delivery intent; returns the row's local identifier.

        The adapter must write this *with* the owning business change (shared
        transaction), or raise ``EffectConsistencyError`` when the environment
        cannot make that atomic.
        """
        ...

    async def inspect_intent(self, effect_key: str) -> EffectIntent | None:
        """Return the current snapshot for an effect key (or None when unseen)."""
        ...


class EffectAdapter(Protocol):
    """The external effect itself, behind a key-aware contract.

    ``effect_key`` is given to every downstream so a provider that supports an
    idempotency token (Stripe-style) can dedupe server-side; providers that
    cannot are deduped by serving the same key whether or not the first
    deliver actually succeeded (honest at-least-once + at-most-once-logical).
    """

    async def deliver(
        self,
        *,
        effect_key: str,
        payload: dict[str, object],
        destination: str,
    ) -> dict[str, object]:
        """Perform the effect; must raise ``DeliveryError`` on failure."""
        ...


class DeliveryError(Exception):
    """A failed delivery attempt, tagged with retryability.

    ``retryable=True`` means transient (timeout, 5xx, dependency blip);
    ``retryable=False`` means the destination definitively refused (e.g.
    Telegram "chat not found", a 4xx policy rejection) — no retry is useful,
    and the effect goes straight to FAILED_PERMANENT.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        effect_key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.effect_key = effect_key


class EffectConsistencyError(Exception):
    """The environment cannot honour the delivery-intent atomicity contract."""
