"""Effect delivery adapters (P4): the external effect behind a key-aware contract.

Each adapter implements the effect half of ``OutboxPort.EffectAdapter``. The
properties that make the seam honest are documented once here and honoured by
every concrete effect (and by ``tests/unit/test_effect_dedup.py`` mutation
checks):

Semantic ladder (also in ``docs/architecture/DATA_AND_STORAGE.md``):

* **database insert**  — at-most-once *by construction* when the effect key is
  the table's unique key: effectively-once.
* **object upload (PUT)** — idempotent when the key names the object: the
  second PUT is a no-op overwrite of the same content; effectively-once.
* **Telegram send** — at-least-once. The provider has no idempotency token; a
  connect-lost-after-send followed by a retry *is a genuine second message*.
  The outbox limits the logical effect to one *intent* and makes each attempt
  visible; it cannot, and must not claim to, stop the network.
* **HTTP callback**  — at-least-once unless the receiver honours the
  ``Idempotency-Key`` header this adapter adds for every attempt.

The ``_unavailable_effect_factory`` registry makes a *missing optional effect*
(e.g. no caption adapter installed) resolve to an explicit, retryable=False
``DeliveryError`` — never a silent no-op that would record SUCCEEDED for a
send that never happened.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from nexus_ai_agent.application.ports.outbox_port import DeliveryError

EffectHandler = Callable[..., Awaitable[dict[str, object]]]

#: Sanity ceiling on effect registry input (each import is explicit).
MAX_EFFECT_ADAPTERS: Final[int] = 16


def unavailable_effect(
    operation_type: str,
    reason: str = "effect adapter not wired",
) -> EffectHandler:
    """Return a fail-closed handler: missing effect resolves to a permanent
    failure, never to a fabricated SUCCEEDED."""

    async def _root(**_kwargs: object) -> dict[str, object]:
        raise DeliveryError(
            f"{operation_type}: {reason}",
            retryable=False,
        )

    _root.__name__ = f"unavailable_{operation_type}"
    return _root


async def deliver_through_handler(
    handler: EffectHandler,
    *,
    effect_key: str,
    destination: str,
    payload: dict[str, object],
) -> dict[str, object]:
    """Invoke one effect handler with the key-aware contract.

    The handler is expected to accept ``effect_key`` and ``destination`` as
    keyword arguments together with the payload fields. Raises ``DeliveryError``
    on a failed observable effect.
    """
    return await handler(effect_key=effect_key, destination=destination, payload=payload)
