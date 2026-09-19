"""Unit tests for the multi-provider litellm routing engine (v3.7.0).

All tests run against a fake/injected router — no network access.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.fallback_provider import FallbackProvider
from nexus_ai_agent.llm.litellm_provider import (
    LiteLLMRoutingProvider,
    RouterExhaustedError,
    build_llm_provider,
    build_routing_chain,
)


class FakeRouter:
    """Stand-in for litellm.Router — records calls, replays canned responses."""

    def __init__(
        self,
        content: str = "ok",
        error: Exception | None = None,
        model: str = "groq/llama-3.3-70b-versatile",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._content = content
        self._error = error
        self._model = model

    async def acompletion(self, model: str, messages: list[dict], **kwargs: Any) -> Any:
        self.calls.append({"model": model, "messages": messages})
        if self._error is not None:
            raise self._error
        return {
            "choices": [{"message": {"role": "assistant", "content": self._content}}],
            "model": self._model,
            "_hidden_params": {"model_id": "groq-1"},
        }


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "GROQ_API_KEY": "groq-key",
        "GEMINI_API_KEY": "gemini-key",
        "OPENROUTER_API_KEY": "or-key",
        "NEXUS_OLLAMA_MODEL": "llama3.2",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


# ── chain construction ─────────────────────────────────────────────────────


def test_chain_priority_order() -> None:
    chain = build_routing_chain(make_settings())
    assert [d.name for d in chain] == [
        "nexus-ollama",
        "nexus-groq",
        "nexus-gemini",
        "nexus-openrouter",
    ]
    assert [d.litellm_model for d in chain] == [
        "ollama/llama3.2",
        "groq/llama-3.3-70b-versatile",
        "gemini/gemini-2.0-flash",
        "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    ]


def test_only_configured_providers_enter_chain() -> None:
    chain = build_routing_chain(
        make_settings(
            **{
                "GROQ_API_KEY": None,
                "GEMINI_API_KEY": None,
                "OPENROUTER_API_KEY": None,
                "NEXUS_OLLAMA_MODEL": "",
            }
        )
    )
    assert chain == []


def test_empty_chain_raises_value_error() -> None:
    with pytest.raises(ValueError, match="routing chain is empty"):
        LiteLLMRoutingProvider(
            make_settings(
                **{
                    "GROQ_API_KEY": None,
                    "GEMINI_API_KEY": None,
                    "OPENROUTER_API_KEY": None,
                    "NEXUS_OLLAMA_MODEL": "",
                }
            ),
            router=FakeRouter(),
        )


def test_strict_privacy_drops_free_openrouter() -> None:
    chain = build_routing_chain(make_settings(NEXUS_LLM_STRICT_PRIVACY="true"))
    assert "nexus-openrouter" not in [d.name for d in chain]
    # Ollama/Groq/Gemini do not train on prompts — they stay.
    assert [d.name for d in chain] == ["nexus-ollama", "nexus-groq", "nexus-gemini"]


def test_strict_privacy_keeps_paid_openrouter_model() -> None:
    chain = build_routing_chain(
        make_settings(
            NEXUS_LLM_STRICT_PRIVACY="true",
            NEXUS_OPENROUTER_MODEL="openai/gpt-oss-120b",
        )
    )
    assert "nexus-openrouter" in [d.name for d in chain]


def test_cloud_cooldown_params() -> None:
    chain = build_routing_chain(make_settings())
    for d in chain:
        if d.name == "nexus-ollama":
            assert (d.cooldown_time, d.allowed_fails) == (300, 2)
        else:
            assert (d.cooldown_time, d.allowed_fails) == (86_400, 1)


def test_cooldown_overridable_via_settings() -> None:
    chain = build_routing_chain(make_settings(NEXUS_LLM_CLOUD_COOLDOWN="3600"))
    groq = next(d for d in chain if d.name == "nexus-groq")
    assert groq.cooldown_time == 3600


# ── router wiring ──────────────────────────────────────────────────────────


def test_deployment_to_model_entry() -> None:
    entry = LiteLLMRoutingProvider._deployment_to_model(build_routing_chain(make_settings())[1])
    assert entry["model_name"] == "nexus-groq"
    assert entry["litellm_params"] == {
        "model": "groq/llama-3.3-70b-versatile",
        "api_key": "groq-key",
    }
    assert entry["model_info"] == {
        "id": "nexus-groq",
        "cooldown_time": 86_400,
        "allowed_fails": 1,
    }


def test_fallback_rules_follow_priority_order() -> None:
    chain = build_routing_chain(make_settings())
    rules = LiteLLMRoutingProvider._fallback_rules(chain)
    assert rules == [
        {"nexus-ollama": ["nexus-groq", "nexus-gemini", "nexus-openrouter"]},
        {"nexus-groq": ["nexus-gemini", "nexus-openrouter"]},
        {"nexus-gemini": ["nexus-openrouter"]},
    ]


def test_real_litellm_router_accepts_wiring() -> None:
    """Regression guard: the real litellm.Router must accept our model_list,
    fallbacks and cooldown kwargs (no network involved in construction)."""
    litellm = pytest.importorskip("litellm")
    settings = make_settings(
        **{
            "GROQ_API_KEY": None,
            "GEMINI_API_KEY": None,
            "OPENROUTER_API_KEY": None,
            "NEXUS_OLLAMA_MODEL": "llama3.2",
        }
    )
    provider = LiteLLMRoutingProvider(settings)  # builds a real Router
    assert provider.deployment_count == 1
    assert provider.chain_names == ["nexus-ollama"]
    assert isinstance(provider._router, litellm.Router)


# ── generate / embed ───────────────────────────────────────────────────────


async def test_generate_routes_via_primary_and_extracts_content() -> None:
    router = FakeRouter(content="hello!")
    provider = LiteLLMRoutingProvider(make_settings(), router=router)
    result = await provider.generate("hi", system="be nice")
    assert result == "hello!"
    assert router.calls[0]["model"] == "nexus-ollama"
    assert router.calls[0]["messages"] == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "hi"},
    ]
    assert provider.stats["routed_calls"] == {"groq-1": 1}


async def test_generate_without_system_only_sends_user_message() -> None:
    router = FakeRouter()
    provider = LiteLLMRoutingProvider(make_settings(), router=router)
    await provider.generate("hi")
    assert router.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


async def test_router_exhausted_error_carries_rate_limit_keywords() -> None:
    router = FakeRouter(error=RuntimeError("boom"))
    provider = LiteLLMRoutingProvider(make_settings(), router=router)
    with pytest.raises(RouterExhaustedError) as exc_info:
        await provider.generate("hi")
    message = str(exc_info.value).lower()
    assert any(kw in message for kw in ("429", "rate limit", "quota", "daily limit"))


async def test_drained_router_degrades_to_fake_llm_via_outer_layer() -> None:
    """The phase-2 contract: router exhausted → FallbackProvider → FakeLLM."""
    router = FakeRouter(error=RuntimeError("no deployments available"))
    routing = LiteLLMRoutingProvider(make_settings(), router=router)
    llm = FallbackProvider(primary=routing, fallback=FakeLLMProvider())
    result = await llm.generate("hi")
    assert result.startswith("[FAKE] Response to: hi")
    assert "Fallback mode" in result
    assert llm.stats["fallback_calls"] == 1


async def test_embed_is_deterministic_and_parity_with_gemini_provider() -> None:
    provider = LiteLLMRoutingProvider(make_settings(), router=FakeRouter())
    vec_a = await provider.embed("same text")
    vec_b = await provider.embed("same text")
    assert vec_a == vec_b
    assert len(vec_a) == 384

    from nexus_ai_agent.llm.gemini_provider import GeminiProvider

    gemini = GeminiProvider(api_key="")
    assert await gemini.embed("same text") == vec_a


# ── factory ────────────────────────────────────────────────────────────────


async def test_factory_builds_routing_chain_wrapped_in_fallback() -> None:
    llm, label = build_llm_provider(make_settings())
    assert isinstance(llm, FallbackProvider)
    assert isinstance(llm.primary, LiteLLMRoutingProvider)
    assert "nexus-ollama" in label


async def test_factory_respects_routing_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir("/")  # keep away from any repo-level model files
    llm, label = build_llm_provider(
        make_settings(
            NEXUS_LLM_ROUTING_ENABLED="false",
            **{
                "GROQ_API_KEY": None,
                "GEMINI_API_KEY": None,
                "OPENROUTER_API_KEY": None,
                "NEXUS_OLLAMA_MODEL": "",
                "NEXUS_MODEL_PATH": "/nonexistent/model.gguf",
            },
        )
    )
    assert isinstance(llm, FakeLLMProvider)
    assert "FakeLLM" in label
