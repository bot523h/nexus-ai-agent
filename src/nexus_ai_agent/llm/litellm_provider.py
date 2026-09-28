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
* W2 (Global LLM Gateway): ``generate()`` executes through
  :class:`~nexus_ai_agent.llm.gateway.engine.LLMGateway`. The Router stays the
  *deployment* selector inside one provider route; the gateway owns the caller's
  timeout budget, concurrency bound, local quota gate, circuit breaking, typed
  error classification, correlation id and observability record. The route is
  registered with ``max_attempts=1`` so the gateway never nests a second retry
  loop over the Router's own fallback walk.
* The existing :class:`~nexus_ai_agent.llm.fallback_provider.FallbackProvider`
  remains the *outer* degradation layer: when the router has exhausted every
  deployment this provider raises :class:`RouterExhaustedError` — which is now a
  typed :class:`~nexus_ai_agent.llm.errors.QuotaExhaustedError`, so the outer
  layer decides by error *kind*, not by matching keywords in a message. The
  message still carries the historical wording (a pinned test asserts it) but
  nothing reads it to make a decision any more.
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
from nexus_ai_agent.llm.errors import (
    AuthenticationError,
    LLMError,
    LLMErrorKind,
    NetworkError,
    QuotaExhaustedError,
    TransientProviderError,
    UpstreamTimeoutError,
)
from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.llm.fallback_provider import FallbackProvider
from nexus_ai_agent.llm.gateway.adapters import LitellmRouterAdapter
from nexus_ai_agent.llm.gateway.contract import (
    Caller,
    CallerCategory,
    LLMOperation,
    LLMRequest,
    Message,
)
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.policy import (
    ConcurrencyPolicy,
    FallbackPolicy,
    RateLimitPolicy,
    RetryPolicy,
    Route,
    TimeoutBudget,
    default_policy,
)
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

# Ollama runs locally — outages are temporary, keep the cooldown short.
OLLAMA_COOLDOWN_TIME = 300
OLLAMA_ALLOWED_FAILS = 2


class RouterExhaustedError(QuotaExhaustedError):
    """Every deployment in the routing chain failed or is cooling down.

    Typed as :class:`~nexus_ai_agent.llm.errors.QuotaExhaustedError`
    (``kind=QUOTA_EXHAUSTED``): not retryable on the same route — hammering a
    drained chain makes the outage worse — but *fallback-eligible*, so the outer
    degradation layer (or the gateway's own :class:`FallbackPolicy`) may serve
    the caller from another route. ``FallbackProvider`` reads that kind; it no
    longer scans a message.

    The message keeps the historical wording ("429 rate limit / quota / daily
    limit") because a pinned test asserts it and because it is genuinely
    informative for an operator. It is *documentation*, not a control signal.
    """

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.QUOTA_EXHAUSTED)
        kwargs.setdefault("provider", "routing")
        kwargs.setdefault("detail", "router_exhausted")
        super().__init__(message, **kwargs)


#: litellm exception *types* → typed gateway kinds. Matched by class identity
#: (``isinstance``), never by message text: a provider that changes its wording
#: must not silently change our retry behaviour. Populated lazily because
#: litellm is an optional dependency.
_LITELLM_KINDS: list[tuple[type[BaseException], LLMErrorKind]] | None = None


def _litellm_error_types() -> list[tuple[type[BaseException], LLMErrorKind]]:
    global _LITELLM_KINDS
    if _LITELLM_KINDS is not None:
        return _LITELLM_KINDS
    table: list[tuple[type[BaseException], LLMErrorKind]] = []
    try:
        import litellm
    except ImportError:
        _LITELLM_KINDS = table
        return table
    # Order matters: the most specific class first.
    for name, kind in (
        ("RateLimitError", LLMErrorKind.RATE_LIMITED),
        ("BudgetExceededError", LLMErrorKind.QUOTA_EXHAUSTED),
        ("AuthenticationError", LLMErrorKind.AUTHENTICATION),
        ("PermissionDeniedError", LLMErrorKind.AUTHENTICATION),
        ("NotFoundError", LLMErrorKind.INVALID_REQUEST),
        ("BadRequestError", LLMErrorKind.INVALID_REQUEST),
        ("ContextWindowExceededError", LLMErrorKind.CONTEXT_LIMIT),
        ("Timeout", LLMErrorKind.UPSTREAM_TIMEOUT),
        ("APITimeoutError", LLMErrorKind.UPSTREAM_TIMEOUT),
        ("ServiceUnavailableError", LLMErrorKind.TRANSIENT_PROVIDER),
        ("InternalServerError", LLMErrorKind.TRANSIENT_PROVIDER),
        ("APIConnectionError", LLMErrorKind.NETWORK),
    ):
        candidate = getattr(litellm, name, None) or getattr(litellm.exceptions, name, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            table.append((candidate, kind))
    _LITELLM_KINDS = table
    return table


def classify_router_failure(exc: BaseException) -> LLMError:
    """Map a Router failure onto the typed taxonomy by exception *class*.

    The default branch is :class:`RouterExhaustedError`: a ``litellm.Router``
    that cannot produce a completion has, by construction, walked its whole
    fallback list, so "chain exhausted" is the truthful summary even when the
    underlying exception type is one we have no specific mapping for. What must
    never happen is dressing an unrelated programming error as a rate limit — so
    the original exception is always chained (``__cause__``) and logged.
    """

    for error_type, kind in _litellm_error_types():
        if isinstance(exc, error_type):
            status = getattr(exc, "status_code", None)
            message = f"routed LLM provider failed ({type(exc).__name__})"
            if kind is LLMErrorKind.RATE_LIMITED:
                from nexus_ai_agent.llm.errors import RateLimitedError

                return RateLimitedError(
                    message, provider="routing", status_code=status, detail=type(exc).__name__
                )
            if kind is LLMErrorKind.AUTHENTICATION:
                return AuthenticationError(
                    message, provider="routing", status_code=status, detail=type(exc).__name__
                )
            if kind is LLMErrorKind.UPSTREAM_TIMEOUT:
                return UpstreamTimeoutError(
                    message, provider="routing", status_code=status, detail=type(exc).__name__
                )
            if kind is LLMErrorKind.NETWORK:
                return NetworkError(
                    message, provider="routing", status_code=status, detail=type(exc).__name__
                )
            if kind is LLMErrorKind.TRANSIENT_PROVIDER:
                return TransientProviderError(
                    message, provider="routing", status_code=status, detail=type(exc).__name__
                )
            return LLMError(
                message,
                kind=kind,
                provider="routing",
                status_code=status,
                detail=type(exc).__name__,
            )
    return RouterExhaustedError(
        "All routed LLM providers are rate-limited or quota-exhausted "
        f"(429 rate limit / quota / daily limit). Last error: {str(exc)[:200]}"
    )


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
        self._timeout_seconds = float(getattr(settings, "llm_request_timeout", 60) or 60)
        self._gateway: LLMGateway | None = None

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

    # ── W2: the authority this provider executes through ───────────────
    @property
    def gateway(self) -> LLMGateway:
        """The gateway that owns policy for this routing chain (built lazily).

        Lazy on purpose: constructing a provider must not build a gateway, read
        credentials again or emit telemetry. Tests that inject a ``FakeRouter``
        get a gateway scoped to that router, so the injected double is the one
        actually called.
        """

        if self._gateway is None:
            adapter = LitellmRouterAdapter(
                self._router,
                primary_name=self._primary_name,
                chain_names=self.chain_names,
                model=self._primary_name,
                classify=classify_router_failure,
                on_response=self._record,
            )
            route = Route(
                provider=adapter.name,
                model=self._primary_name,
                operations=adapter.operations,
                modalities=adapter.modalities,
                rank=10,
                label="litellm-chain:" + ">".join(self.chain_names),
            )
            self._gateway = LLMGateway(
                policy=default_policy(
                    routes=(route,),
                    timeout=TimeoutBudget(
                        total_seconds=float(self._timeout_seconds),
                        queue_wait_seconds=min(30.0, float(self._timeout_seconds)),
                        connect_seconds=5.0,
                        read_seconds=max(5.0, float(self._timeout_seconds) - 10.0),
                        per_attempt_seconds=max(10.0, float(self._timeout_seconds) - 5.0),
                    ).validate(),
                    # ONE attempt: the Router already walks its own fallback list
                    # and parks deployments on cooldown. Retrying here would
                    # multiply attempts across every free-tier quota in the chain.
                    retry=RetryPolicy(max_attempts=1),
                    concurrency=ConcurrencyPolicy(
                        max_inflight_global=4,
                        max_inflight_per_provider=4,
                        max_inflight_per_tenant=2,
                        max_queued=32,
                    ),
                    rate_limit=RateLimitPolicy(),
                    fallback=FallbackPolicy(enabled=False, max_hops=0, allow_degraded_routes=False),
                )
            )
            self._gateway.register(adapter, (route,))
        return self._gateway

    # ── LLMProvider ────────────────────────────────────────────────────
    async def generate(self, prompt: str, system: str = "") -> str:
        """Route *prompt* through the chain; first healthy deployment wins.

        Executes through the gateway (W2). Failures surface as typed
        :class:`~nexus_ai_agent.llm.errors.LLMError` subclasses — this method
        never returns an error-shaped string, so no caller downstream has to
        guess whether a reply is an answer or a failure.
        """

        request = LLMRequest(
            caller=Caller(category=CallerCategory.AGENT, name="llm.litellm_provider"),
            purpose="routing",
            operation=LLMOperation.CHAT,
            messages=(Message(role="user", content=prompt),),
            system=system or None,
            provider="routing",
            model=self._primary_name,
            # The Router owns deployment selection; a second hop at the gateway
            # level would be a hidden fallback (LAW 8).
            allow_fallback=False,
        )
        response = await self.gateway.execute(request)
        return response.text.strip()

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
      2. llama.cpp server (when ``llama_server_base_url`` is set).
      3. Legacy local GGUF path (unchanged behaviour).
      4. ``FakeLLMProvider`` when nothing is available.

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

    if settings.llama_server_base_url:
        from nexus_ai_agent.llm.local_server_provider import (
            LocalLlamaServerProvider,
        )

        server = LocalLlamaServerProvider(
            settings.llama_server_base_url,
            model=settings.llama_server_model,
            timeout=float(settings.llama_server_timeout),
            max_tokens=settings.llama_server_max_tokens,
        )
        return server, f"llama.cpp server ({settings.llama_server_base_url})"

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
