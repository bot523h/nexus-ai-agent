from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import httpx
from pydantic import BaseModel, Field

from nexus_ai_agent.llm.backends import Backend
from nexus_ai_agent.llm.schemas import ChatMessage, GenerateRequest, GenerateResponse


class ContextPolicy(BaseModel):
    """Deterministic context policy that budgets system and conversation messages."""

    reserve_tokens: int = Field(default=512, ge=1)
    chars_per_token: int = Field(default=4, ge=1)

    def fit(self, messages: Sequence[ChatMessage], context_window: int) -> list[ChatMessage]:
        effective_reserve = min(self.reserve_tokens, max(context_window // 2, 1))
        budget = max((context_window - effective_reserve) * self.chars_per_token, 1)
        system_messages = [message for message in messages if message.role.value == "system"]
        conversation = [message for message in messages if message.role.value != "system"]

        selected: list[ChatMessage] = []
        used = 0
        system_budget = max(budget // 2, 1)
        for message in system_messages:
            remaining = system_budget - used
            if remaining <= 0:
                break
            content = message.content[:remaining]
            selected.append(message.model_copy(update={"content": content}))
            used += len(content)

        recent: list[ChatMessage] = []
        used = min(used, system_budget)

        for message in reversed(conversation):
            remaining = budget - used
            if remaining <= 0:
                break
            content = message.content[:remaining]
            recent.append(message.model_copy(update={"content": content}))
            used += len(content)
        recent.reverse()
        return selected + recent


class ProviderUnavailable(RuntimeError):
    """Raised when every configured local backend is unavailable."""


class LocalLLMProvider:
    """Provider-agnostic local LLM facade with graceful backend fallback."""

    def __init__(
        self, backends: Sequence[Backend], context_policy: ContextPolicy | None = None
    ) -> None:
        if not backends:
            raise ValueError("At least one local LLM backend is required")
        self._backends = tuple(backends)
        self._context_policy = context_policy or ContextPolicy()

    def prepare(self, request: GenerateRequest) -> GenerateRequest:
        messages = self._context_policy.fit(request.messages, request.context_window)
        return request.model_copy(update={"messages": messages})

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        prepared = self.prepare(request)
        failures: list[str] = []
        for backend in self._backends:
            try:
                text = await backend.generate(prepared)
                return GenerateResponse(text=text, backend=backend.name)
            except (ConnectionError, TimeoutError, OSError, httpx.TransportError) as exc:
                failures.append(f"{backend.name}: {exc}")
        raise ProviderUnavailable("; ".join(failures) or "No local backend succeeded")

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        prepared = self.prepare(request)
        failures: list[str] = []
        for backend in self._backends:
            try:
                async for chunk in backend.stream(prepared):
                    yield chunk
                return
            except (ConnectionError, TimeoutError, OSError, httpx.TransportError) as exc:
                failures.append(f"{backend.name}: {exc}")
        raise ProviderUnavailable("; ".join(failures) or "No local backend succeeded")
