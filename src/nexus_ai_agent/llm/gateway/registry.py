"""Composition and the process-wide authority accessor (W2, LAW 1 + LAW 2).

Two jobs:

1. :func:`build_gateway_from_settings` — turn :class:`Settings` into a fully
   wired gateway. This is the *only* place that knows which providers exist,
   which keys are configured, and in what order they should be tried. Nothing
   downstream re-derives that.
2. :func:`get_llm_gateway` — the process-wide accessor. Same shape as the
   repository's existing ``get_settings()`` / ``get_http_client()`` singletons, so
   the convention is familiar and there is exactly one authority per process.
   A composition root (W1's ``RuntimeContext``) may install its own instance
   with :func:`set_llm_gateway`; whoever installs first wins, and a second,
   *different* installation is logged rather than silently replacing the
   authority — a split brain must be visible, not hidden.

Composition mirrors the documented pre-W2 priority in
``llm/litellm_provider.build_llm_provider`` so migrating a caller onto the
gateway does not change which provider answers:

===========================  =========================================
settings                     routes registered (rank order)
===========================  =========================================
``llm_routing_enabled`` +    ``routing/<primary>`` (litellm chain, rank 10)
  a buildable chain          ``+ local-degraded/fake`` (rank 900) — this is
                             today's ``FallbackProvider → FakeLLM`` outer layer
``gemini_api_key``           ``gemini/<gemini_model>`` (rank 10)
``llama_server_base_url``    ``llama-server/<model>`` (rank 20)
``model_path`` exists        ``llama-cpp/<model>`` (rank 30)
nothing configured           ``local-degraded/fake`` (rank 900)
===========================  =========================================

The degraded local route is registered **only** where the legacy composition
already degraded (the routing-chain path). The direct-Gemini path historically
surfaced a localised "try again later" message rather than a fake answer, and
that behaviour is preserved exactly: no degraded route means a typed failure the
caller renders, not a fabricated answer with a disclaimer.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import Any

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.llm.errors import LLMErrorKind
from nexus_ai_agent.llm.gateway.adapters import GeminiHttpAdapter, LegacyProviderAdapter
from nexus_ai_agent.llm.gateway.contract import LLMOperation, Modality
from nexus_ai_agent.llm.gateway.engine import LLMGateway
from nexus_ai_agent.llm.gateway.policy import (
    ConcurrencyPolicy,
    FallbackPolicy,
    PrivacyPolicy,
    ProviderRateLimit,
    RateLimitPolicy,
    RetryPolicy,
    Route,
    RouteRule,
    TimeoutBudget,
    default_policy,
)
from nexus_ai_agent.observability.logging import get_logger

__all__ = [
    "DEGRADED_PROVIDER",
    "build_gateway_from_settings",
    "gateway_for_credentials",
    "get_llm_gateway",
    "install_llm_gateway",
    "reset_llm_gateway",
    "set_llm_gateway",
]

log = get_logger(__name__)

#: Name of the local, non-network degraded route (legacy ``FakeLLMProvider``).
DEGRADED_PROVIDER = "local-degraded"

_gateway: LLMGateway | None = None


def get_llm_gateway(settings: Settings | None = None) -> LLMGateway:
    """Return the process-wide authority, building it from settings on first use.

    Building is lazy so importing a module never touches the network or reads
    credentials, and so a test can install its own gateway before anything asks.
    """

    global _gateway
    if _gateway is None or _gateway.closed:
        from nexus_ai_agent.config.settings import get_settings

        _gateway = build_gateway_from_settings(settings or get_settings())
        log.info(
            "llm_gateway_installed",
            routes=[route.key for route in _gateway.policy.routes],
            adapters=sorted(_gateway._adapters),  # noqa: SLF001 — same package, status only
        )
    return _gateway


def set_llm_gateway(gateway: LLMGateway | None) -> LLMGateway | None:
    """Install (or clear, with ``None``) the process-wide authority.

    Returns the previously installed gateway so a composition root can close it.
    Installing a *different* gateway over a live one is allowed — a composition
    root owns that decision — but it is logged, because two authorities in one
    process is the split-brain this module exists to prevent.
    """

    global _gateway
    previous = _gateway
    if (
        previous is not None
        and gateway is not None
        and previous is not gateway
        and not previous.closed
    ):
        log.warning(
            "llm_gateway_replaced_while_live",
            previous_routes=[route.key for route in previous.policy.routes],
            new_routes=[route.key for route in gateway.policy.routes],
        )
    _gateway = gateway
    return previous


#: Explicit alias: composition roots read better with the intent-revealing name.
install_llm_gateway = set_llm_gateway


def reset_llm_gateway() -> None:
    """Clear the accessor without closing the gateway (test isolation helper)."""

    global _gateway
    _gateway = None


def build_gateway_from_settings(
    settings: Settings,
    *,
    policy_overrides: dict[str, Any] | None = None,
    attach_default_sink: bool = True,
) -> LLMGateway:
    """Build the canonical gateway for this deployment."""

    adapters: list[Any] = []
    routes: list[Route] = []
    rules: list[RouteRule] = []
    degraded_allowed = False

    timeout = TimeoutBudget(
        total_seconds=float(settings.llm_request_timeout or 60),
        queue_wait_seconds=min(30.0, float(settings.llm_request_timeout or 60)),
        connect_seconds=5.0,
        read_seconds=max(5.0, float(settings.llm_request_timeout or 60) - 10.0),
        per_attempt_seconds=max(10.0, float(settings.llm_request_timeout or 60) - 5.0),
        max_backoff_seconds=8.0,
    ).validate()

    # 1. Multi-provider routing chain (litellm) — the documented v3.7.0 primary.
    if settings.llm_routing_enabled:
        chain = _build_routing_adapter(settings)
        if chain is not None:
            adapter, route = chain
            adapters.append(adapter)
            routes.append(route)
            rules.append(RouteRule(provider=route.provider, model=route.model, purpose=None))
            # This is exactly where the legacy composition wrapped the chain in
            # FallbackProvider(primary=chain, fallback=FakeLLMProvider()), so the
            # degraded route is registered here and only here.
            degraded_allowed = True

    # 2. Direct Gemini (the bot chat / summarize / vision / code path).
    if settings.gemini_api_key:
        gemini = GeminiHttpAdapter(
            api_key=settings.gemini_api_key,
            default_model=settings.gemini_model,
        )
        adapters.append(gemini)
        routes.append(
            Route(
                provider=gemini.name,
                model=settings.gemini_model,
                operations=gemini.operations,
                modalities=gemini.modalities,
                rank=10 if not routes else 20,
                label="gemini-rest",
                context_window_chars=None,
            )
        )

    # 3. Local llama.cpp server (OpenAI-compatible HTTP).
    if settings.llama_server_base_url:
        server_adapter = _build_llama_server_adapter(settings)
        if server_adapter is not None:
            adapters.append(server_adapter)
            routes.append(
                Route(
                    provider=server_adapter.name,
                    model=settings.llama_server_model,
                    operations=server_adapter.operations,
                    modalities=server_adapter.modalities,
                    rank=40,
                    label="llama-server",
                )
            )

    # 4. Legacy in-process GGUF model.
    if not routes and settings.model_path and Path(settings.model_path).exists():
        cpp_adapter = _build_llama_cpp_adapter(settings)
        if cpp_adapter is not None:
            adapters.append(cpp_adapter)
            routes.append(
                Route(
                    provider=cpp_adapter.name,
                    model=Path(settings.model_path).name,
                    operations=cpp_adapter.operations,
                    modalities=cpp_adapter.modalities,
                    rank=50,
                    label="llama-cpp",
                )
            )

    # 5. Degraded local route (never a first choice; only where legacy degraded).
    if degraded_allowed:
        fake_adapter = _build_fake_adapter()
        if fake_adapter is not None:
            adapters.append(fake_adapter)
            routes.append(
                Route(
                    provider=fake_adapter.name,
                    model="fake",
                    operations=fake_adapter.operations,
                    modalities=fake_adapter.modalities,
                    rank=900,
                    degraded=True,
                    label="local-degraded",
                )
            )

    policy = default_policy(
        routes=routes,
        rules=tuple(rules),
        timeout=timeout,
        retry=RetryPolicy(max_attempts=3),
        concurrency=ConcurrencyPolicy(
            max_inflight_global=4,
            max_inflight_per_provider=2,
            max_inflight_per_tenant=2,
            max_queued=32,
        ),
        rate_limit=RateLimitPolicy(
            per_provider={
                "gemini": ProviderRateLimit(
                    requests_per_minute=settings.gemini_max_rpm or None,
                    requests_per_day=settings.gemini_max_daily or None,
                )
            }
        ),
        fallback=FallbackPolicy(
            enabled=len(routes) > 1,
            max_hops=max(0, len(routes) - 1),
            allow_degraded_routes=degraded_allowed,
        ),
        privacy=PrivacyPolicy(strict=bool(settings.llm_strict_privacy)),
    )
    if policy_overrides:
        from dataclasses import replace

        policy = replace(policy, **policy_overrides)

    gateway = LLMGateway(
        policy=policy,
        attach_default_sink=attach_default_sink,
    )
    for adapter in adapters:
        adapter_routes = [r for r in routes if r.provider == adapter.name]
        gateway.register(adapter, adapter_routes or None)
    if not routes:
        log.warning(
            "llm_gateway_no_routes",
            hint=(
                "no provider credentials or local model are configured; every request "
                "will fail with a typed POLICY_REFUSAL instead of hanging or guessing"
            ),
        )
    return gateway


# ── adapter construction (import-guarded: optional dependencies) ────────


def _build_routing_adapter(settings: Settings) -> tuple[Any, Route] | None:
    """Wrap the litellm routing chain as one gateway route."""

    try:
        from nexus_ai_agent.llm.litellm_provider import (
            LiteLLMRoutingProvider,
            RouterExhaustedError,
        )
    except ImportError:
        log.warning("litellm_not_installed", hint="pip install 'litellm>=1.74,<2'")
        return None
    try:
        provider = LiteLLMRoutingProvider(settings)
    except ImportError:  # pragma: no cover — guarded above, kept explicit
        return None
    except ValueError:
        log.info("no_providers_configured_for_routing_chain")
        return None

    adapter = LegacyProviderAdapter(
        provider,
        name="routing",
        model=provider.chain_names[0] if provider.chain_names else "routing",
        operations=frozenset(
            {LLMOperation.CHAT, LLMOperation.TEXT_COMPLETION, LLMOperation.EMBEDDINGS}
        ),
        modalities=frozenset({Modality.TEXT}),
        # Typed-by-class mapping. ``RouterExhaustedError`` *means* "every
        # deployment is rate-limited or quota-exhausted"; that meaning comes from
        # the exception's identity, not from scanning its message for "429".
        error_kinds={RouterExhaustedError: LLMErrorKind.QUOTA_EXHAUSTED},
    )
    route = Route(
        provider="routing",
        model=adapter._model,  # noqa: SLF001 — same package composition code
        operations=adapter.operations,
        modalities=adapter.modalities,
        rank=10,
        label="litellm-chain:" + ">".join(provider.chain_names),
    )
    return adapter, route


def _build_llama_server_adapter(settings: Settings) -> Any | None:
    try:
        from nexus_ai_agent.llm.local_server_provider import (
            LlamaServerError,
            LocalLlamaServerProvider,
        )
    except ImportError:  # pragma: no cover — stdlib+httpx only
        return None
    provider = LocalLlamaServerProvider(
        settings.llama_server_base_url,
        model=settings.llama_server_model,
        timeout=float(settings.llama_server_timeout),
        max_tokens=settings.llama_server_max_tokens,
    )
    return LegacyProviderAdapter(
        provider,
        name="llama-server",
        model=settings.llama_server_model,
        error_kinds={LlamaServerError: LLMErrorKind.TRANSIENT_PROVIDER},
    )


def _build_llama_cpp_adapter(settings: Settings) -> Any | None:
    try:
        from nexus_ai_agent.llm.local_llama_cpp import LocalLlamaCppProvider
    except ImportError:
        log.warning("llama_cpp_not_installed")
        return None
    try:
        provider = LocalLlamaCppProvider(
            settings.model_path,
            n_ctx=settings.n_ctx,
            n_gpu_layers=settings.n_gpu_layers,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        log.warning("llama_cpp_model_unavailable", error=type(exc).__name__)
        return None
    return LegacyProviderAdapter(provider, name="llama-cpp", model="local-gguf")


def _build_fake_adapter() -> Any | None:
    from nexus_ai_agent.llm.fake_llm import FakeLLMProvider

    return LegacyProviderAdapter(
        FakeLLMProvider(),
        name=DEGRADED_PROVIDER,
        model="fake",
        degraded=True,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Credential-scoped resolution
# ═══════════════════════════════════════════════════════════════════════════

#: One authority per credential set, bounded (LAW 6). Engines are constructed a
#: handful of times per process; the cap exists so a caller that minted a gateway
#: per request could not grow this without limit.
_CREDENTIAL_GATEWAYS: OrderedDict[tuple[str, str], LLMGateway] = OrderedDict()
_CREDENTIAL_GATEWAY_CAP = 16


def gateway_for_credentials(
    api_key: str,
    model: str,
    *,
    requests_per_minute: int | None = None,
    requests_per_day: int | None = None,
    base_url: str | None = None,
) -> LLMGateway:
    """Resolve the authority that may speak with *these* credentials.

    Three cases, in order:

    1. ``api_key`` equals the deployment key in :class:`Settings` → the process
       authority from :func:`get_llm_gateway`. One gateway, one policy, one set
       of metrics for the whole deployment. This is the production path.
    2. A different, non-empty key (a second tenant, a test double, a CLI flag) →
       a dedicated single-route gateway for that credential set, cached so
       repeated engines share it. It is still *a* gateway: same policy engine,
       same typed errors, same observability. It is not a provider bypass — the
       adapter is the same :class:`GeminiHttpAdapter`, constructed with the key
       the caller was given.
    3. An empty key → the process authority, which has no Gemini route and
       therefore answers with a typed ``POLICY_REFUSAL`` instead of pretending.

    The cache is LRU-bounded; an evicted gateway is closed on the running loop
    when there is one, so its pooled HTTP client is not leaked.
    """

    from nexus_ai_agent.config.settings import get_settings

    settings = get_settings()
    if api_key and api_key == settings.gemini_api_key:
        return get_llm_gateway(settings)
    if not api_key:
        return get_llm_gateway(settings)

    cache_key = (api_key, model)
    existing = _CREDENTIAL_GATEWAYS.get(cache_key)
    if existing is not None and not existing.closed:
        _CREDENTIAL_GATEWAYS.move_to_end(cache_key)
        return existing

    adapter = GeminiHttpAdapter(api_key=api_key, default_model=model, base_url=base_url)
    route = Route(
        provider=adapter.name,
        model=model,
        operations=adapter.operations,
        modalities=adapter.modalities,
        rank=10,
        label="gemini-rest-scoped",
    )
    policy = default_policy(
        routes=(route,),
        timeout=TimeoutBudget(),
        retry=RetryPolicy(max_attempts=3),
        rate_limit=RateLimitPolicy(
            per_provider={
                adapter.name: ProviderRateLimit(
                    requests_per_minute=requests_per_minute,
                    requests_per_day=requests_per_day,
                )
            }
        ),
        # A scoped gateway has exactly one route, so there is nothing to fall
        # back to. Saying so explicitly keeps LAW 8 honest: no hidden hop.
        fallback=FallbackPolicy(enabled=False, max_hops=0, allow_degraded_routes=False),
    )
    gateway = LLMGateway(policy=policy)
    gateway.register(adapter, (route,))
    _CREDENTIAL_GATEWAYS[cache_key] = gateway
    while len(_CREDENTIAL_GATEWAYS) > _CREDENTIAL_GATEWAY_CAP:
        _evict_oldest_credential_gateway()
    log.info(
        "llm_gateway_scoped_installed",
        model=model,
        routes=[r.key for r in policy.routes],
        cached=len(_CREDENTIAL_GATEWAYS),
    )
    return gateway


def _evict_oldest_credential_gateway() -> None:
    """Drop the least-recently-used scoped gateway and close it if we can."""

    _key, stale = _CREDENTIAL_GATEWAYS.popitem(last=False)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("llm_gateway_scoped_evicted_without_loop")
        return
    loop.create_task(stale.aclose())
