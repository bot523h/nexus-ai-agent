"""Composition and the process-wide authority accessor (W2, LAW 1 + LAW 2).

Two jobs:

1. :func:`build_gateway_from_settings` — turn :class:`Settings` into a fully
   wired gateway. This is the *only* place that knows which providers exist,
   which keys are configured, and in what order they should be tried. Nothing
   downstream re-derives that.
2. :func:`get_llm_gateway` — the process-wide accessor. Same shape as the
   repository's existing ``get_settings()`` / ``get_http_client()`` singletons, so
   the convention is familiar and there is exactly one authority per process —
   including when two callers reach first use at the same instant, which is why
   the lazy build is double-checked under a lock: the accessor is a process global
   that synchronous constructors call, and this repository runs synchronous work in
   ``asyncio.to_thread`` workers, so "one authority" has to survive real threads
   and not just a single caller asking twice.
   A composition root (W1's ``RuntimeContext``) may install its own instance
   with :func:`set_llm_gateway`; whoever installs first wins. A second live
   installation is rejected. Reset revokes idle references, and refuses active
   work: it cannot silently create a second execution authority.

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

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.llm.errors import GatewayInternalError, LLMErrorKind, OverloadedError
from nexus_ai_agent.llm.gateway.adapters import (
    GeminiHttpAdapter,
    LegacyProviderAdapter,
    LitellmRouterAdapter,
)
from nexus_ai_agent.llm.gateway.contract import LLMOperation
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

#: "Exactly one authority per process" is a promise about *concurrent* first use
#: too. This accessor is a process global that **synchronous** constructors call
#: (``SummarizerEngine.__init__`` resolves its gateway inline), and this repository
#: runs plenty of synchronous work in threads (``asyncio.to_thread`` in the job
#: queue, in whisper, in the ffmpeg/render pipeline, in RAG) and starts fresh loops
#: with ``asyncio.run`` (CLI, maintenance). A lock-free check-then-build lets two
#: threads that arrive together each construct an authority: one wins the global,
#: the other keeps serving its caller — a silent split brain with two sets of
#: concurrency bounds, rate windows, breaker state and metrics, plus an adapter
#: whose HTTP pool nobody owns and therefore nobody closes. Both locks are held
#: only around in-memory construction, and neither is ever held while calling the
#: other accessor, so they cannot deadlock each other.
_AUTHORITY_LOCK = threading.Lock()
_CREDENTIAL_LOCK = threading.Lock()


def get_llm_gateway(settings: Settings | None = None) -> LLMGateway:
    """Return the process-wide authority, building it from settings on first use.

    Building is lazy so importing a module never touches the network or reads
    credentials, and so a test can install its own gateway before anything asks.
    The build is double-checked under :data:`_AUTHORITY_LOCK`: a thread that had
    to wait returns the authority that won instead of constructing a second one.
    """

    global _gateway
    current = _gateway
    if current is not None and not current.closed:
        return current
    with _AUTHORITY_LOCK:
        current = _gateway
        if current is not None and not current.closed:
            return current
        from nexus_ai_agent.config.settings import get_settings

        if current is not None:
            current.retire()
        built = build_gateway_from_settings(settings or get_settings())
        _gateway = built
        log.info(
            "llm_gateway_installed",
            routes=[route.key for route in built.policy.routes],
            adapters=sorted(built._adapters),  # noqa: SLF001 — same package, status only
        )
        return built


def set_llm_gateway(gateway: LLMGateway | None) -> LLMGateway | None:
    """Install once, or revoke an idle authority with ``None``.

    All writers use the getter's lock. A different live installation is a typed
    conflict, not a warning followed by split brain. The loser remains owned by
    its builder and must be closed there. Returned retired instances still need
    ``aclose()``; revocation is not transport cleanup.
    """
    global _gateway
    with _AUTHORITY_LOCK:
        previous = _gateway
        if previous is gateway:
            return previous
        if previous is not None:
            if gateway is not None and not previous.closed:
                gateway.retire()
                raise GatewayInternalError("a live LLM authority is already installed")
            previous.retire()
        _gateway = gateway
        return previous


install_llm_gateway = set_llm_gateway


def reset_llm_gateway() -> LLMGateway | None:
    """Revoke idle references atomically; caller closes the returned old gateway.

    Active work must first be drained/cancelled by the async lifecycle owner.
    This is not a way to create a parallel authority during an in-flight call.
    """
    return set_llm_gateway(None)


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
            adapter, route, embedding_adapter = chain
            adapters.extend((adapter, embedding_adapter))
            routes.extend((route, embedding_adapter.route(rank=10)))
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
        # The Router already walks deployments. Never retry its entire chain.
        retry=RetryPolicy(max_attempts=1 if degraded_allowed else 3),
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


def _build_routing_adapter(settings: Settings) -> tuple[Any, Route, Any] | None:
    """Wrap the litellm routing chain as one gateway route."""

    try:
        from nexus_ai_agent.llm.litellm_provider import (
            LiteLLMRoutingProvider,
            classify_router_failure,
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

    # Do not wrap provider.generate(): that method is a compatibility facade
    # which would enter its own private gateway and duplicate all policy state.
    adapter = LitellmRouterAdapter(
        provider._router,
        primary_name=provider.chain_names[0],
        chain_names=provider.chain_names,
        model=provider.chain_names[0],
        classify=classify_router_failure,
        on_response=provider._record,
    )
    # Preserve the historical deterministic 384-dimensional local embeddings.
    # This adapter exposes embed ONLY, never the provider's generate method.
    embedding_adapter = LegacyProviderAdapter(
        provider,
        name="routing-embedding",
        model="local-hash-384",
        operations=frozenset({LLMOperation.EMBEDDINGS}),
    )
    route = Route(
        provider=adapter.name,
        model=adapter.model,
        operations=adapter.operations,
        modalities=adapter.modalities,
        rank=10,
        label="litellm-chain:" + ">".join(provider.chain_names),
    )
    return adapter, route, embedding_adapter


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

    The cache is capacity-bounded. Live entries are never evicted: new scopes
    are refused until the composition owner closes an existing gateway.
    """

    from nexus_ai_agent.config.settings import get_settings

    settings = get_settings()
    if api_key and api_key == settings.gemini_api_key:
        return get_llm_gateway(settings)
    if not api_key:
        return get_llm_gateway(settings)

    cache_key = (api_key, model)
    # One credential set must resolve to one gateway even when two threads ask at
    # the same instant: a duplicated scoped gateway silently halves its rate
    # window and its concurrency bound, and the loser is never closed.
    with _CREDENTIAL_LOCK:
        existing = _CREDENTIAL_GATEWAYS.get(cache_key)
        if existing is not None and not existing.closed:
            _CREDENTIAL_GATEWAYS.move_to_end(cache_key)
            return existing

        # An LRU eviction can detach a still-live authority (especially without
        # a running loop). Reject new scopes rather than split the same quota.
        for key, stale in tuple(_CREDENTIAL_GATEWAYS.items()):
            if stale.closed:
                stale.retire()
                del _CREDENTIAL_GATEWAYS[key]
        if len(_CREDENTIAL_GATEWAYS) >= _CREDENTIAL_GATEWAY_CAP:
            raise OverloadedError("credential authority capacity is full")

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
        log.info(
            "llm_gateway_scoped_installed",
            model=model,
            routes=[r.key for r in policy.routes],
            cached=len(_CREDENTIAL_GATEWAYS),
        )
        return gateway
