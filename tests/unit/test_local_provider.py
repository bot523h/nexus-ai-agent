from collections.abc import AsyncIterator

import pytest

from nexus_ai_agent.llm.backends import Backend
from nexus_ai_agent.llm.local_provider import LocalLLMProvider, ProviderUnavailable
from nexus_ai_agent.llm.schemas import ChatMessage, ChatRole, GenerateRequest


class FakeBackend:
    def __init__(self, name: str, response: str = "ok", fail: bool = False) -> None:
        self.name = name
        self.response = response
        self.fail = fail
        self.last_request: GenerateRequest | None = None

    async def generate(self, request: GenerateRequest) -> str:
        self.last_request = request
        if self.fail:
            raise ConnectionError("backend unavailable")
        return self.response

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        self.last_request = request
        if self.fail:
            raise ConnectionError("backend unavailable")
        for chunk in self.response.split():
            yield chunk + " "


def request(*contents: str, context_window: int = 256) -> GenerateRequest:
    return GenerateRequest(
        messages=[ChatMessage(role=ChatRole.USER, content=content) for content in contents],
        context_window=context_window,
    )


@pytest.mark.asyncio
async def test_provider_falls_back_to_next_local_backend() -> None:
    primary = FakeBackend("llama.cpp", fail=True)
    fallback = FakeBackend("ollama", response="fallback")
    provider = LocalLLMProvider([primary, fallback])

    result = await provider.generate(request("hello"))

    assert result.text == "fallback"
    assert result.backend == "ollama"


@pytest.mark.asyncio
async def test_provider_streams_chunks() -> None:
    provider = LocalLLMProvider([FakeBackend("llama.cpp", response="hello world")])

    chunks = [chunk async for chunk in provider.stream(request("hello"))]

    assert "".join(chunks) == "hello world "


@pytest.mark.asyncio
async def test_provider_raises_when_all_backends_fail() -> None:
    provider = LocalLLMProvider(
        [FakeBackend("llama.cpp", fail=True), FakeBackend("ollama", fail=True)]
    )

    with pytest.raises(ProviderUnavailable, match="llama.cpp"):
        await provider.generate(request("hello"))


def test_context_policy_keeps_system_and_recent_messages() -> None:
    provider = LocalLLMProvider([FakeBackend("fake")])
    prepared = provider.prepare(
        GenerateRequest(
            messages=[
                ChatMessage(role=ChatRole.SYSTEM, content="system"),
                ChatMessage(role=ChatRole.USER, content="old" * 100),
                ChatMessage(role=ChatRole.USER, content="new"),
            ],
            context_window=256,
        )
    )

    assert prepared.messages[0].content == "system"
    assert prepared.messages[-1].content == "new"
    assert len(prepared.messages) < 3


def test_backend_protocol_is_runtime_compatible() -> None:
    backend: Backend = FakeBackend("fake")
    assert backend.name == "fake"
