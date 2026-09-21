"""LLM provider registry."""

from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.fallback_provider import FallbackProvider
from nexus_ai_agent.llm.gemini_provider import GeminiProvider
from nexus_ai_agent.llm.litellm_provider import (
    Deployment,
    LiteLLMRoutingProvider,
    RouterExhaustedError,
)
from nexus_ai_agent.llm.local_server_provider import (
    LlamaServerError,
    LocalLlamaServerProvider,
)
from nexus_ai_agent.llm.provider import LLMProvider

__all__ = [
    "Deployment",
    "FakeLLMProvider",
    "FallbackProvider",
    "GeminiProvider",
    "LLMProvider",
    "LiteLLMRoutingProvider",
    "LlamaServerError",
    "LocalLlamaServerProvider",
    "RouterExhaustedError",
]
