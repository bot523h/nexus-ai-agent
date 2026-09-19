"""Durable message history port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class ConversationStorePort(Protocol):
    async def append_message(self, thread_id: str, role: str, content: str) -> str: ...

    async def list_messages(self, thread_id: str) -> Sequence[dict[str, str]]: ...
