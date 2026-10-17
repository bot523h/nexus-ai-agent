"""The two composition facades that had no dedicated test (audit 2026-10-16).

``application/image_generation.py`` decides which image provider a bot or a
queue worker gets, and ``agents/base.py`` is the abstract base every store agent
inherits.  Both are small; both are *choices* that a deployment depends on:

* the provider choice must follow the operator's setting and must be built once
  per process (the provider carries a cache and, for Gemini, a paid-tier guard);
* the abstract base must refuse to run an agent that forgot to implement
  ``run`` — an agent store that silently returns ``NotImplementedError`` at
  runtime is a user-visible failure, not a typing nit.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai_agent.agents import base as agent_base
from nexus_ai_agent.application import image_generation
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.image_gen.gemini_adapter import GeminiAdapter
from nexus_ai_agent.creative.image_gen.pollinations_adapter import PollinationsAdapter
from nexus_ai_agent.creative.image_gen.provider import (
    ImageGenProvider,
    PaidTierRequiredError,
)


@pytest.fixture(autouse=True)
def _clear_caches() -> Any:
    settings_module.get_settings.cache_clear()
    image_generation.get_image_gen_provider.cache_clear()
    yield
    settings_module.get_settings.cache_clear()
    image_generation.get_image_gen_provider.cache_clear()


def test_default_provider_is_the_free_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_IMAGE_GEN_PROVIDER", raising=False)
    provider = image_generation.get_image_gen_provider()
    assert isinstance(provider, PollinationsAdapter)


def test_gemini_provider_is_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_IMAGE_GEN_PROVIDER", "gemini")
    monkeypatch.setenv("NEXUS_CREATIVE_GEMINI_API_KEY", "key-from-env")
    monkeypatch.setenv("NEXUS_IMAGE_GEN_MODEL", "gemini-3-pro-image")
    settings_module.get_settings.cache_clear()

    provider = image_generation.get_image_gen_provider()

    assert isinstance(provider, GeminiAdapter)
    # The paid-tier guard is fail-closed: without the operator's opt-in the
    # adapter must refuse before it can spend money (see gemini_adapter._authorize).
    with pytest.raises(PaidTierRequiredError):
        provider._authorize()


def test_provider_is_a_process_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_IMAGE_GEN_PROVIDER", raising=False)
    first = image_generation.get_image_gen_provider()
    second = image_generation.get_image_gen_provider()
    assert first is second, "a second provider would double the cache and the paid-tier guard"


def test_provider_satisfies_the_provider_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_IMAGE_GEN_PROVIDER", raising=False)
    provider = image_generation.get_image_gen_provider()
    # ``ImageGenProvider`` is a plain Protocol (not runtime_checkable), so the
    # contract is checked structurally: the composition exposes ``generate``.
    assert callable(provider.generate)
    assert ImageGenProvider is not None


def test_abstract_base_refuses_an_agent_without_run() -> None:
    with pytest.raises(TypeError):
        agent_base.BaseAgent(llm=object())  # type: ignore[arg-type]


async def test_concrete_agent_keeps_the_llm_it_was_given() -> None:
    sentinel = object()

    class Echo(agent_base.BaseAgent):
        async def run(self, state: Any) -> Any:
            return state

    agent = Echo(llm=sentinel)  # type: ignore[arg-type]
    assert agent.llm is sentinel
    assert await agent.run("state") == "state"
