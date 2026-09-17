"""Provider-neutral language model port."""

from __future__ import annotations

from typing import Protocol


class LLMPort(Protocol):
    async def complete(self, prompt: str, *, idempotency_key: str | None = None) -> str: ...
