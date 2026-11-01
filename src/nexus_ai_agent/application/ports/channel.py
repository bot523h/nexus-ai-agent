"""ChannelPort: the one seam between the application core and any transport.

Telegram, a PWA/WebSocket gateway, a mobile push service and a CLI are all
channels. The core addresses a **Principal**, never a transport id; the
adapter resolves the Principal to its own binding. Pure typing: no transport
import is allowed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nexus_ai_agent.application.principal import ChannelBinding, PrincipalId


@dataclass(frozen=True)
class InboundMessage:
    """A user utterance as an adapter hands it to the core."""

    binding: ChannelBinding
    text: str
    message_id: str | None = None


@dataclass(frozen=True)
class OutboundMessage:
    """What the core wants said to a Principal; the adapter decides how."""

    text: str
    reply_to: str | None = None


@runtime_checkable
class ChannelPort(Protocol):
    """Implemented once per transport (``adapters/``), consumed by the core."""

    @property
    def channel(self) -> str:
        """The slug used in this channel's :class:`ChannelBinding` values."""
        ...

    async def send(self, principal: PrincipalId, message: OutboundMessage) -> bool:
        """Deliver ``message``; ``False`` when the Principal has no binding here."""
        ...


__all__ = ["ChannelPort", "InboundMessage", "OutboundMessage"]
