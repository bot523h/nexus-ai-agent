"""Multi-provider LLM routing engine on top of ``litellm.Router`` (v3.7.0).

Replaces the single-provider (Gemini) dependency with a priority chain of
free-tier providers:

    Ollama (local, unlimited) → Groq (free, fast) → Gemini (current)
    → OpenRouter ``:free`` (last resort)

Design notes
------------
* Only providers with credentials/settings configured enter the chain.
* Providers with daily caps (Groq/Gemini/OpenRouter) get ``allowed_fails=1``
  and a long ``cooldown_time`` (default 86_400s): litellm parks a deployment
  on cooldown *immediately* on a 429, so a drained daily quota is skipped for
  ~24h (process lifetime) instead of being hammered in a retry-storm.
* Ollama gets a short cooldown (300s / 2 fails) because local outages are
  usually temporary.
* ``NEXUS_LLM_STRICT_PRIVACY=true`` removes OpenRouter ``:free`` deployments
  from the chain — free endpoints may train on user prompts. Ollama, Groq
  and Gemini do not train on prompts and stay in the chain.
* The existing :class:`~nexus_ai_agent.llm.fallback_provider.FallbackProvider`
  remains the *outer* layer: when the router has exhausted every deployment
  this provider raises :class:`RouterExhaustedError`, whose message carries
  the rate-limit keywords the outer layer matches on, degrading to
  ``FakeLLMProvider`` instead of crashing the graph.
* ``embed()`` keeps the deterministic hash-based vector (byte-for-byte parity
  with ``GeminiProvider.embed``) so stored vectors remain compatible. Real
  embedding models are out of scope for this phase.

Usage:
    provider = LiteLLMRoutingProvider(settings)          # builds the Router
    llm = FallbackProvider(primary=provider, fallback=FakeLLMProvider())
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.fallback_provider import FallbackProvider
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

# Ollama runs locally — outages are temporary, keep the cooldown short.
OLLAMA_COOLDOWN_TIME = 300
OLLAMA_ALLOWED_FAILS = 2


class RouterExhaustedError(RuntimeError):
    """Every deployment in the routing chain failed or is cooling down.

    The message deliberately contains the rate-limit keywords matched by
    ``FallbackProvider`` ("429", "rate limit", "quota", "daily limit") so a
    drained router degrades to the FakeLLM fallback instead of propagating.
    """


@dataclass(frozen=True)
class Deployment:
    """One provider deployment in the routing chain."""

    name: str
    litellm_model: str
    api_key: str | None = None
    api_base: str | None = None
    cooldown_time: int = 86_400
    allowed_fails: int = 1


def build_routing_chain(settings: Settings) -> list[Deployment]:
    """Build the priority chain from settings — configured providers only.

    Order: Ollama → Groq → Gemini → OpenRouter. With
    ``llm_strict_privacy`` enabled, OpenRouter ``:free`` deployments are
    dropped (they may train on user prompts); a paid OpenRouter model has a
    standard no-training data policy and stays.
    """
    cloud_cooldown = settings.llm_cloud_cooldown
    chain: list[Deployment] = []

    if settings.ollama_model:
        chain.append(
            Deployment(
                name="nexus-ollama",
                litellm_model=f"ollama/{settings.ollama_model}",
                api_base=settings.ollama_base_url,
                cooldown_time=OLLAMA_COOLDOWN_TIME,
                allowed_fails=OLLAMA_ALLOWED_FAILS,
            )
        )
    if settings.groq_api_key:
        chain.append(
            Deployment(
                name="nexus-groq",
                litellm_model=f"groq/{settings.groq_model}",
                api_key=settings.groq_api_key,
                cooldown_time=cloud_cooldown,
            )
        )
    if settings.gemini_api_key:
        chain.append(
            Deployment(
                name="nexus-gemini",
                litellm_model=f"gemini/{settings.gemini_model}",
                api_key=settings.gemini_api_key,
                cooldown_time=cloud_cooldown,
            )
        )
    if settings.openrouter_api_key and settings.openrouter_model:
        if settings.llm_strict_privacy and settings.openrouter_model.endswith(":free"):
            log.info(
                "strict_privacy_skip_openrouter",
                model=settings.openrouter_model,
                reason="free endpoints may train on user prompts",
            )
        else:
            chain.append(
                Deployment(
                    name="nexus-openrouter",
                    litellm_model=f"openrouter/{settings.openrouter_model}",
                    api_key=settings.openrouter_api_key,
                    cooldown_time=cloud_cooldown,
                )
            )
    return chain


class LiteLLMRoutingProvider(LLMProvider):
    """``LLMProvider`` backed by a ``litellm.Router`` fallback chain.

    Accepts an injected ``router`` (testing) or builds a real
    ``litellm.Router`` from *settings*. Raises ``ImportError`` when litellm
    is not installed and ``ValueError`` when no provider is configured.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        router: Any | None = None,
        chain: list[Deployment] | None = None,
    ) -> None:
        self._chain: list[Deployment] = (
            list(chain) if chain is not None else build_routing_chain(settings)
        )
        if not self._chain:
            raise ValueError(
                "No LLM providers configured — routing chain is empty. "
                "Set GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY / NEXUS_OLLAMA_MODEL."
            )
        if router is None:
            # litellm fetches its pricing map from the network at import time
            # unless this switch is set.  Keep the bundled map: it removes a
            # hidden network dependency (offline and test determinism) and keeps
            # litellm's retry warnings off stdout, which the CLI's ``--json``
            # modes rely on.
            os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
            from litellm import Router

            router = Router(
                model_list=[self._deployment_to_model(d) for d in self._chain],
                fallbacks=self._fallback_rules(self._chain),
                num_retries=0,  # never retry the same deployment — move down the chain
                timeout=settings.llm_request_timeout,
                cooldown_time=settings.llm_cloud_cooldown,
                allowed_fails=1,
                routing_strategy="simple-shuffle",
            )
        self._router: Any = router
        self._primary_name = self._chain[0].name
        self._calls: dict[str, int] = {}

    # ── introspection ──────────────────────────────────────────────────
    @property
    def chain_names(self) -> list[str]:
        """Deployment names in priority order."""
        return [d.name for d in self._chain]

    @property
    def deployment_count(self) -> int:
        return len(self._chain)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "chain": self.chain_names,
            "routed_calls": dict(sorted(self._calls.items())),
        }

    # ── LLMProvider ────────────────────────────────────────────────────
    async def generate(self, prompt: str, system: str = "") -> str:
        """Route *prompt* through the chain; first healthy deployment wins."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            response = await self._router.acompletion(
                model=self._primary_name,
                messages=messages,
            )
        except Exception as exc:
            log.error("routing_chain_exhausted", error=str(exc)[:200])
            raise RouterExhaustedError(
                "All routed LLM providers are rate-limited or quota-exhausted "
                f"(429 rate limit / quota / daily limit). Last error: {str(exc)[:200]}"
            ) from exc

        content = self._extract_content(response)
        self._record(response)
        return content.strip()

    async def embed(self, text: str) -> list[float]:
        """Deterministic pseudo-embedding — parity with ``GeminiProvider.embed``."""
        import hashlib
        import random

        vec_dim = 384
        h = hashlib.sha512(text.encode()).digest()
        seed = int.from_bytes(h[:8], "little")
        rng = random.Random(seed)
        return [rng.uniform(-0.1, 0.1) for _ in range(vec_dim)]

    # ── internals ──────────────────────────────────────────────────────
    @staticmethod
    def _deployment_to_model(deployment: Deployment) -> dict[str, Any]:
        """Map a :class:`Deployment` to a litellm ``model_list`` entry."""
        params: dict[str, Any] = {"model": deployment.litellm_model}
        if deployment.api_key:
            params["api_key"] = deployment.api_key
        if deployment.api_base:
            params["api_base"] = deployment.api_base
        return {
            "model_name": deployment.name,
            "litellm_params": params,
            # Per-deployment cooldown: overrides the router defaults. litellm
            # parks a deployment on the *first* 429 immediately; with
            # cooldown_time=86400 it stays out of rotation for ~24h.
            "model_info": {
                "id": deployment.name,
                "cooldown_time": deployment.cooldown_time,
                "allowed_fails": deployment.allowed_fails,
            },
        }

    @staticmethod
    def _fallback_rules(chain: list[Deployment]) -> list[dict[str, list[str]]]:
        """Ordered fallback chain: deployment i → every later deployment."""
        names = [d.name for d in chain]
        return [{names[i]: names[i + 1 :]} for i in range(len(names) - 1)]

    @staticmethod
    def _extract_content(response: Any) -> str:
        """Pull the assistant text out of a litellm ModelResponse (or dict)."""
        try:
            choices = response["choices"]
        except (TypeError, KeyError, IndexError):
            choices = getattr(response, "choices", [])
        if not choices:
            return ""
        try:
            message = choices[0]["message"]
        except (TypeError, KeyError, IndexError):
            message = getattr(choices[0], "message", None)
        if message is None:
            return ""
        content = getattr(message, "content", None) or message.get("content")
        return content or ""

    def _record(self, response: Any) -> None:
        """Count calls per answering deployment (observability)."""
        deployment = "unknown"
        try:
            hidden = getattr(response, "_hidden_params", None)
            if hidden is None and isinstance(response, dict):
                hidden = response.get("_hidden_params")
            hidden = hidden or {}
            deployment = str(
                hidden.get("model_id")
                or hidden.get("deployment")
                or getattr(response, "model", None)
                or (response.get("model") if isinstance(response, dict) else None)
                or "unknown"
            )
        except Exception:  # noqa: BLE001 — observability must never raise
            pass
        self._calls[deployment] = self._calls.get(deployment, 0) + 1


def build_llm_provider(settings: Settings) -> tuple[LLMProvider, str]:
    """Compose the default LLM engine for ``nexus run-bot``.

    Priority:
      1. litellm routing chain (when enabled and ≥1 provider configured),
         wrapped in the existing ``FallbackProvider`` (outer layer → FakeLLM).
      2. Legacy local GGUF path (unchanged behaviour).
      3. ``FakeLLMProvider`` when nothing is available.

    Returns ``(provider, human-readable label)``.
    """
    if settings.llm_routing_enabled:
        try:
            routing = LiteLLMRoutingProvider(settings)
        except ImportError:
            log.warning("litellm_not_installed", hint="pip install 'litellm>=1.74,<2'")
        except ValueError:
            log.info("no_providers_configured_for_routing_chain")
        else:
            outer = FallbackProvider(primary=routing, fallback=FakeLLMProvider())
            label = f"litellm routing chain ({' → '.join(routing.chain_names)}) + FakeLLM outer"
            return outer, label

    model_path = Path(settings.model_path)
    if model_path.exists():
        from nexus_ai_agent.llm.local_llama_cpp import LocalLlamaCppProvider

        local = LocalLlamaCppProvider(
            settings.model_path,
            n_ctx=settings.n_ctx,
            n_gpu_layers=settings.n_gpu_layers,
        )
        return local, f"local GGUF ({settings.model_path})"

    return FakeLLMProvider(), "FakeLLM (no providers configured, no GGUF model found)"
