"""NEXUS Global LLM Gateway (W2) — the single authority for LLM execution.

Public surface
--------------
Everything a caller legitimately needs is re-exported here. Anything not listed
is an implementation detail and may change without notice.

    from nexus_ai_agent.llm.gateway import (
        LLMGateway, LLMRequest, LLMResponse, Caller, CallerCategory,
        get_llm_gateway, GatewayLLMProvider,
    )

    gateway = get_llm_gateway()
    response = await gateway.execute(
        LLMRequest(
            caller=Caller(CallerCategory.SUMMARIZER, "features.summarizer"),
            purpose="summarize",
            prompt=text,
            system="You are a summarization expert.",
        )
    )
    print(response.text, response.usage.source.value, response.policy.attempts)

Design summary (full rationale in ``docs/architecture/LLM_GATEWAY.md`` and
ADR 0012)::

    Caller → LLMGateway → Policy → Scheduler → RateLimit → Circuit
           → ProviderAdapter → External LLM
    External LLM → ProviderAdapter → AdapterResult | typed LLMError
                 → Gateway → LLMResponse | LLMError → Caller

Laws this package exists to enforce: single authority, no provider bypass,
typed truth, centralised policy, sacred cancellation, bounded everything, retry
with purpose, no hidden fallback, provider as adapter, observable by default,
real usage only.
"""

from __future__ import annotations

from nexus_ai_agent.llm.gateway.adapters import (
    AdapterResult,
    GeminiHttpAdapter,
    LegacyProviderAdapter,
    ProviderAdapter,
    map_transport_error,
)
from nexus_ai_agent.llm.gateway.contract import (
    AttemptRecord,
    Caller,
    CallerCategory,
    ContentPart,
    FinishReason,
    GenerationParams,
    LLMOperation,
    LLMPriority,
    LLMRequest,
    LLMResponse,
    Message,
    Modality,
    PolicyOutcome,
    Timings,
    Usage,
    UsageSource,
    new_request_id,
)
from nexus_ai_agent.llm.gateway.engine import GatewayBuilder, LLMGateway
from nexus_ai_agent.llm.gateway.facade import (
    DEGRADED_DISCLAIMER,
    UNAVAILABLE_MESSAGE,
    GatewayLLMProvider,
    agent_caller,
    json_object_validator,
    pydantic_validator,
    surface_caller,
)
from nexus_ai_agent.llm.gateway.observability import (
    CollectingSink,
    GatewayMetrics,
    ObservationSink,
    RequestRecord,
    StructlogSink,
)
from nexus_ai_agent.llm.gateway.policy import (
    CircuitPolicy,
    ConcurrencyPolicy,
    FallbackPolicy,
    GatewayPolicy,
    JitterMode,
    OverloadBehavior,
    Plan,
    PrivacyPolicy,
    ProviderRateLimit,
    RateLimitPolicy,
    RetryPolicy,
    Route,
    RouteRule,
    TimeoutBudget,
    WorldSnapshot,
    default_policy,
    plan,
)
from nexus_ai_agent.llm.gateway.registry import (
    DEGRADED_PROVIDER,
    build_gateway_from_settings,
    get_llm_gateway,
    install_llm_gateway,
    reset_llm_gateway,
    set_llm_gateway,
)
from nexus_ai_agent.llm.gateway.resilience import (
    CircuitBreaker,
    CircuitState,
    WindowRateLimiter,
    compute_backoff,
    parse_retry_after,
)
from nexus_ai_agent.llm.gateway.scheduler import Admission, BoundedScheduler, SchedulerPort
from nexus_ai_agent.llm.gateway.usage import DEFAULT_PRICE_TABLE, ModelPrice, apply_cost

__all__ = [
    "AdapterResult",
    "Admission",
    "AttemptRecord",
    "BoundedScheduler",
    "CircuitBreaker",
    "CircuitPolicy",
    "CircuitState",
    "Caller",
    "CallerCategory",
    "CollectingSink",
    "ConcurrencyPolicy",
    "ContentPart",
    "DEGRADED_DISCLAIMER",
    "DEFAULT_PRICE_TABLE",
    "DEGRADED_PROVIDER",
    "FallbackPolicy",
    "FinishReason",
    "GatewayBuilder",
    "GatewayLLMProvider",
    "GatewayMetrics",
    "GatewayPolicy",
    "GeminiHttpAdapter",
    "GenerationParams",
    "JitterMode",
    "LLMGateway",
    "LLMOperation",
    "LLMPriority",
    "LLMRequest",
    "LLMResponse",
    "LegacyProviderAdapter",
    "Message",
    "ModelPrice",
    "Modality",
    "ObservationSink",
    "OverloadBehavior",
    "Plan",
    "PolicyOutcome",
    "PrivacyPolicy",
    "ProviderRateLimit",
    "RateLimitPolicy",
    "RequestRecord",
    "RetryPolicy",
    "Route",
    "RouteRule",
    "SchedulerPort",
    "StructlogSink",
    "TimeoutBudget",
    "Timings",
    "UNAVAILABLE_MESSAGE",
    "Usage",
    "UsageSource",
    "ProviderAdapter",
    "WindowRateLimiter",
    "WorldSnapshot",
    "agent_caller",
    "apply_cost",
    "compute_backoff",
    "build_gateway_from_settings",
    "default_policy",
    "get_llm_gateway",
    "install_llm_gateway",
    "json_object_validator",
    "map_transport_error",
    "new_request_id",
    "parse_retry_after",
    "plan",
    "pydantic_validator",
    "reset_llm_gateway",
    "set_llm_gateway",
    "surface_caller",
]
