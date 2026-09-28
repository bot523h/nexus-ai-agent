"""Deterministic gateway policy (W2, LAW 4 — one place owns retry/timeout/fallback/concurrency).

The whole point of this module is that *deciding* is separated from *doing*.
:func:`plan` is a pure function of ``(request, policy, route table, world
snapshot, now)``: no I/O, no clock reads inside, no randomness, no adapter
calls. That makes every policy decision unit-testable as a table, and it makes
the execution engine dumb in the best sense — it just carries out a plan and
re-plans when the world changes.

Evidence behind each mechanism (LAW 13 — no complexity without a reason):

*Retry / backoff / jitter.* Google's own Gemini guidance is to retry only the
transient codes (408/429/5xx) with exponential backoff **and jitter**, and to
cap the attempt count
(https://ai.google.dev/gemini-api/docs/troubleshooting#retry-strategy). The
jitter formula is AWS's "full jitter" — ``uniform(0, min(cap, base * 2**n))`` —
which Marc Brooker measured as the lowest-total-work / lowest-server-load option
among the three candidates
(https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/).
Full jitter is the default here.

*Deadline discipline.* The mission's rule — "retry must not silently destroy the
caller's total timeout" — is enforced structurally: every sleep and every
attempt is clamped against one absolute deadline owned by the budget, and a
backoff that would not fit inside the remaining budget ends the retry chain
instead of shortening a wait the provider asked for.

*Retry-After.* RFC 9110 §10.2.3 defines it as either delta-seconds or an
HTTP-date, and a 429 means the server *refused to service* the request, so
honouring it exactly is both correct and safe
(https://www.rfc-editor.org/rfc/rfc9110#name-retry-after). A Retry-After
longer than ``max_retry_after_seconds`` is *declined*, not truncated: waiting
past the caller's budget to satisfy an upstream hint is how a 1-hour quota
reset becomes a hung request.

*Circuit breaking.* A breaker that opens on every failure is worse than none,
because our own malformed requests would take a healthy provider out of
rotation. Only provider-health kinds (transient, timeout, network, rate limit,
quota, auth) count toward opening; ``INVALID_REQUEST``,
``MALFORMED_RESPONSE`` and ``CONTENT_BLOCKED`` do not.

*Bulkhead / bounded queues.* Concurrency is bounded globally and per provider,
and the wait queue itself is bounded: saturation produces a typed
``OVERLOADED`` refusal (backpressure) rather than unbounded memory growth. This
is the standard bulkhead + load-shedding pair
(https://learn.microsoft.com/en-us/azure/architecture/patterns/bulkhead,
https://aws.amazon.com/builders-library/using-load-shedding-to-avoid-overload/).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from nexus_ai_agent.llm.errors import (
    FALLBACK_ELIGIBLE_KINDS,
    RETRYABLE_KINDS,
    LLMErrorKind,
    PolicyRefusalError,
)
from nexus_ai_agent.llm.gateway.contract import (
    LLMOperation,
    LLMPriority,
    LLMRequest,
    Modality,
)

__all__ = [
    "CircuitPolicy",
    "ConcurrencyPolicy",
    "FallbackPolicy",
    "GatewayPolicy",
    "JitterMode",
    "OverloadBehavior",
    "Plan",
    "PrivacyPolicy",
    "RateLimitPolicy",
    "Route",
    "RouteRule",
    "RouteSnapshot",
    "RetryPolicy",
    "TimeoutBudget",
    "WorldSnapshot",
    "default_policy",
    "plan",
]


class JitterMode(str, Enum):
    """Backoff jitter strategy (AWS taxonomy)."""

    NONE = "none"
    FULL = "full"  # uniform(0, min(cap, base*2**n)) — lowest server load
    EQUAL = "equal"  # half deterministic + half random
    DECORRELATED = "decorrelated"  # sleep = min(cap, random(base, prev*3))


class OverloadBehavior(str, Enum):
    """What happens when the bounded queue is full."""

    REJECT = "reject"  # typed OVERLOADED immediately — real backpressure
    WAIT = "wait"  # block until the queue-wait budget expires, then OVERLOADED


# ═══════════════════════════════════════════════════════════════════════════
# Timeout budget
# ═══════════════════════════════════════════════════════════════════════════


#: Smallest budget the policy layer will hand out. Anything tighter is answered
#: with a typed ``DEADLINE_EXCEEDED`` instead of a transport-level surprise.
_MIN_BUDGET_SECONDS = 1e-3


@dataclass(frozen=True)
class TimeoutBudget:
    """Layered timeouts that are *mutually consistent* by construction.

    Layers, outermost to innermost::

        total_seconds          caller deadline: queue + rate wait + attempts + backoff
          ├─ queue_wait_seconds   max time waiting for a concurrency slot
          ├─ per_attempt_seconds  wall-clock cap for ONE provider attempt
          │    ├─ connect_seconds   transport connect bound
          │    └─ read_seconds      transport read bound
          └─ max_backoff_seconds    cap on a single retry sleep

    Invariants (checked in :meth:`validate`, so a mis-configured policy fails at
    construction rather than at 3 a.m. under load):

    * ``connect + read <= per_attempt`` — the transport must time out *before*
      the gateway's outer cap, otherwise every slow provider surfaces as a
      gateway cancellation instead of a classifiable ``UPSTREAM_TIMEOUT``;
    * ``per_attempt <= total`` — one attempt can never exceed the whole budget;
    * ``queue_wait <= total`` — waiting cannot consume more than everything.
    """

    total_seconds: float = 60.0
    queue_wait_seconds: float = 30.0
    connect_seconds: float = 5.0
    read_seconds: float = 45.0
    per_attempt_seconds: float = 50.0
    max_backoff_seconds: float = 8.0

    def validate(self) -> TimeoutBudget:
        for name in (
            "total_seconds",
            "queue_wait_seconds",
            "connect_seconds",
            "read_seconds",
            "per_attempt_seconds",
            "max_backoff_seconds",
        ):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"TimeoutBudget.{name} must be positive, got {value}")
        if self.connect_seconds + self.read_seconds > self.per_attempt_seconds:
            raise ValueError(
                "TimeoutBudget.connect_seconds + read_seconds "
                f"({self.connect_seconds + self.read_seconds}) must not exceed "
                f"per_attempt_seconds ({self.per_attempt_seconds}): the transport "
                "must time out before the gateway's outer cap so the failure stays "
                "classifiable as UPSTREAM_TIMEOUT"
            )
        if self.per_attempt_seconds > self.total_seconds:
            raise ValueError(
                f"TimeoutBudget.per_attempt_seconds ({self.per_attempt_seconds}) must not "
                f"exceed total_seconds ({self.total_seconds})"
            )
        if self.queue_wait_seconds > self.total_seconds:
            raise ValueError(
                f"TimeoutBudget.queue_wait_seconds ({self.queue_wait_seconds}) must not "
                f"exceed total_seconds ({self.total_seconds})"
            )
        if self.max_backoff_seconds > self.total_seconds:
            raise ValueError(
                f"TimeoutBudget.max_backoff_seconds ({self.max_backoff_seconds}) must not "
                f"exceed total_seconds ({self.total_seconds})"
            )
        return self

    def with_total(self, total_seconds: float) -> TimeoutBudget:
        """Scale the inner layers down to fit a caller-supplied total deadline.

        A caller may shorten the total budget (never lengthen it past policy).
        The inner layers are clamped, keeping the invariants true, so a tight
        caller deadline cannot produce a configuration where one attempt is
        allowed to outlive the whole request.
        """

        # A caller deadline tighter than the transport's own connect+read floor
        # is *not* a configuration error — it is a request that cannot possibly
        # be served. Clamping here (instead of raising) keeps the failure typed:
        # the engine sees a near-zero budget and answers DEADLINE_EXCEEDED rather
        # than leaking a ValueError out of policy evaluation.
        total = max(min(self.total_seconds, float(total_seconds)), _MIN_BUDGET_SECONDS)
        per_attempt = max(min(self.per_attempt_seconds, total), _MIN_BUDGET_SECONDS)
        # Split per-attempt between connect and read *without* floors: a floor on
        # either half would push connect+read back over per_attempt and make
        # validate() reject a budget the policy layer itself just produced —
        # turning a caller's tight deadline into an infrastructure ValueError.
        # Halving guarantees connect + read == per_attempt exactly, and both
        # halves stay strictly positive because per_attempt >= _MIN > 0.
        connect = min(self.connect_seconds, per_attempt * 0.5)
        read = per_attempt - connect
        return TimeoutBudget(
            total_seconds=total,
            queue_wait_seconds=max(min(self.queue_wait_seconds, total), _MIN_BUDGET_SECONDS),
            connect_seconds=connect,
            read_seconds=read,
            per_attempt_seconds=per_attempt,
            max_backoff_seconds=max(min(self.max_backoff_seconds, total), _MIN_BUDGET_SECONDS),
        ).validate()


# ═══════════════════════════════════════════════════════════════════════════
# Retry / rate limit / concurrency / fallback / circuit / privacy
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class RetryPolicy:
    """Central retry policy. One owner, one reason per retry (LAW 7)."""

    max_attempts: int = 3  # total attempts including the first
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    multiplier: float = 2.0
    jitter: JitterMode = JitterMode.FULL
    retryable_kinds: frozenset[LLMErrorKind] = RETRYABLE_KINDS
    respect_retry_after: bool = True
    #: A provider-asked wait longer than this is declined (fail now, don't hang).
    max_retry_after_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("RetryPolicy.max_attempts must be at least 1")
        if self.base_delay_seconds < 0:
            raise ValueError("RetryPolicy.base_delay_seconds cannot be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("RetryPolicy.max_delay_seconds must be >= base_delay_seconds")
        if self.multiplier < 1.0:
            raise ValueError("RetryPolicy.multiplier must be >= 1.0")
        if self.max_retry_after_seconds <= 0:
            raise ValueError("RetryPolicy.max_retry_after_seconds must be positive")

    def allows(self, kind: LLMErrorKind) -> bool:
        return kind in self.retryable_kinds


@dataclass(frozen=True)
class ProviderRateLimit:
    """Provider-scoped request budget (a mini bulkhead on the quota dimension)."""

    requests_per_minute: int | None = None
    requests_per_day: int | None = None


@dataclass(frozen=True)
class RateLimitPolicy:
    """Local, provider-aware rate limiting — *not* random sleeps.

    The gateway tracks its own rolling minute window and UTC day counter per
    provider and refuses/queues before spending quota, then honours the
    provider's own ``Retry-After`` when it does get throttled. Gemini's limits
    are expressed as RPM/TPM/RPD
    (https://ai.google.dev/gemini-api/docs/rate-limits), so those are the axes
    modelled here; token-per-minute accounting needs provider-reported usage and
    is recorded as a residual gap rather than guessed.
    """

    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    per_provider: Mapping[str, ProviderRateLimit] = field(default_factory=dict)
    #: How long a caller may wait for a local rate-limit slot before the
    #: gateway fails it with QUOTA_EXHAUSTED instead of holding the deadline.
    max_wait_seconds: float = 5.0

    def for_provider(self, provider: str) -> ProviderRateLimit:
        return self.per_provider.get(
            provider,
            ProviderRateLimit(
                requests_per_minute=self.requests_per_minute,
                requests_per_day=self.requests_per_day,
            ),
        )


@dataclass(frozen=True)
class ConcurrencyPolicy:
    """Bounded everything (LAW 6)."""

    max_inflight_global: int = 4
    max_inflight_per_provider: int = 2
    max_inflight_per_tenant: int = 2
    #: Hard bound on waiting requests. Saturation sheds load; it never grows.
    max_queued: int = 32
    overload_behavior: OverloadBehavior = OverloadBehavior.REJECT

    def __post_init__(self) -> None:
        if self.max_inflight_global < 1:
            raise ValueError("max_inflight_global must be at least 1")
        if self.max_inflight_per_provider < 1:
            raise ValueError("max_inflight_per_provider must be at least 1")
        if self.max_inflight_per_tenant < 1:
            raise ValueError("max_inflight_per_tenant must be at least 1")
        if self.max_queued < 0:
            raise ValueError("max_queued cannot be negative")
        if self.max_inflight_per_provider > self.max_inflight_global:
            raise ValueError(
                "max_inflight_per_provider must not exceed max_inflight_global: "
                "a per-provider bound looser than the global one is not a bound"
            )


@dataclass(frozen=True)
class FallbackPolicy:
    """Fallback as a policy decision, never ``except Exception: use provider B``."""

    enabled: bool = True
    #: Maximum number of *hops* to a different route (0 = primary only).
    max_hops: int = 2
    eligible_kinds: frozenset[LLMErrorKind] = FALLBACK_ELIGIBLE_KINDS
    #: A fallback route must be able to serve the request as asked. Without this
    #: the gateway would happily "degrade" a vision request to a text-only route
    #: and hand back an answer to a question the model never saw.
    require_capability_match: bool = True
    #: Allow routes flagged ``degraded`` (local/fake) as the last hop.
    allow_degraded_routes: bool = True

    def __post_init__(self) -> None:
        if self.max_hops < 0:
            raise ValueError("FallbackPolicy.max_hops cannot be negative")

    def allows(self, kind: LLMErrorKind) -> bool:
        return self.enabled and self.max_hops > 0 and kind in self.eligible_kinds


@dataclass(frozen=True)
class CircuitPolicy:
    """Per-route circuit breaker with an explicit half-open probe."""

    enabled: bool = True
    failure_threshold: int = 5
    recovery_seconds: float = 30.0
    half_open_max_probes: int = 1
    success_threshold: int = 1
    #: Only provider-health failures count toward opening the circuit.
    counted_kinds: frozenset[LLMErrorKind] = frozenset(
        {
            LLMErrorKind.TRANSIENT_PROVIDER,
            LLMErrorKind.UPSTREAM_TIMEOUT,
            LLMErrorKind.NETWORK,
            LLMErrorKind.RATE_LIMITED,
            LLMErrorKind.QUOTA_EXHAUSTED,
            LLMErrorKind.AUTHENTICATION,
        }
    )

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("CircuitPolicy.failure_threshold must be at least 1")
        if self.recovery_seconds <= 0:
            raise ValueError("CircuitPolicy.recovery_seconds must be positive")
        if self.half_open_max_probes < 1:
            raise ValueError("CircuitPolicy.half_open_max_probes must be at least 1")
        if self.success_threshold < 1:
            raise ValueError("CircuitPolicy.success_threshold must be at least 1")

    def counts(self, kind: LLMErrorKind) -> bool:
        return kind in self.counted_kinds


@dataclass(frozen=True)
class PrivacyPolicy:
    """Data-egress policy. Enforced at route selection, before any request."""

    #: When true, endpoints that may train on prompts are removed from the chain.
    strict: bool = False
    forbidden_providers: frozenset[str] = frozenset()
    #: Model-name suffixes considered non-private (OpenRouter ``:free``).
    forbidden_model_suffixes: tuple[str, ...] = (":free",)

    def forbids(self, route: Route) -> bool:
        if route.provider in self.forbidden_providers:
            return True
        if self.strict:
            return any(route.model.endswith(suffix) for suffix in self.forbidden_model_suffixes)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Route:
    """One executable (provider, model) pair with its declared capabilities.

    Capabilities are declared by the adapter when it registers the route, so
    :func:`plan` can refuse an impossible request *before* spending quota —
    a typed ``UNSUPPORTED_CAPABILITY`` instead of a provider 400.
    """

    provider: str
    model: str
    operations: frozenset[LLMOperation] = frozenset(
        {LLMOperation.CHAT, LLMOperation.TEXT_COMPLETION, LLMOperation.GENERATE_CONTENT}
    )
    modalities: frozenset[Modality] = frozenset({Modality.TEXT})
    #: Lower is tried first when several rules match.
    rank: int = 100
    #: A locally served, non-provider route (degraded answers, disclaimer added).
    degraded: bool = False
    #: Free-form, log-safe label used in observability records.
    label: str = ""
    #: Optional conservative request-side context gate, in characters. When set,
    #: a request whose payload exceeds it is refused with a typed CONTEXT_LIMIT
    #: *before* any bytes are sent. This exists because Gemini reports both
    #: "your request is malformed" and "your prompt is too long" as the same
    #: ``400 INVALID_ARGUMENT``; distinguishing them would require reading the
    #: provider's free-text message, which LAW 3 forbids. A character budget is
    #: an explicit, documented approximation — not a token count.
    context_window_chars: int | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    def serves(self, request: LLMRequest) -> bool:
        if request.operation not in self.operations:
            return False
        return request.modalities.issubset(self.modalities)


@dataclass(frozen=True)
class RouteRule:
    """Model routing: ``purpose``/``operation`` → route. First match wins.

    Kept intentionally flat (no expressions, no weights, no learning): the
    repository has four real purposes today (chat, summarize, plan, embed). A
    rule engine bigger than the number of rules it holds is decoration.
    """

    provider: str
    model: str
    purpose: str | None = None
    operation: LLMOperation | None = None
    priority: LLMPriority | None = None

    def matches(self, request: LLMRequest) -> bool:
        if self.purpose is not None and self.purpose != request.purpose:
            return False
        if self.operation is not None and self.operation is not request.operation:
            return False
        if self.priority is not None and self.priority is not request.priority:
            return False
        return True


@dataclass(frozen=True)
class RouteSnapshot:
    """Per-route world state read by the planner (never mutated by it)."""

    circuit_open: bool = False
    cooling_down: bool = False
    inflight: int = 0
    minute_window_used: int = 0
    day_window_used: int = 0


@dataclass(frozen=True)
class WorldSnapshot:
    """Everything :func:`plan` is allowed to know about the live system."""

    routes: Mapping[str, RouteSnapshot] = field(default_factory=dict)
    global_inflight: int = 0
    queued: int = 0
    per_provider_inflight: Mapping[str, int] = field(default_factory=dict)
    per_tenant_inflight: Mapping[int, int] = field(default_factory=dict)

    def route(self, key: str) -> RouteSnapshot:
        return self.routes.get(key, RouteSnapshot())


@dataclass(frozen=True)
class GatewayPolicy:
    """The complete, versioned policy of the authority."""

    routes: tuple[Route, ...] = ()
    rules: tuple[RouteRule, ...] = ()
    timeout: TimeoutBudget = field(default_factory=TimeoutBudget)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    concurrency: ConcurrencyPolicy = field(default_factory=ConcurrencyPolicy)
    rate_limit: RateLimitPolicy = field(default_factory=RateLimitPolicy)
    fallback: FallbackPolicy = field(default_factory=FallbackPolicy)
    circuit: CircuitPolicy = field(default_factory=CircuitPolicy)
    privacy: PrivacyPolicy = field(default_factory=PrivacyPolicy)
    #: Idempotency window for caller-supplied keys (seconds). 0 disables reuse.
    idempotency_ttl_seconds: float = 300.0
    version: str = "1"

    def __post_init__(self) -> None:
        self.timeout.validate()
        keys = [r.key for r in self.routes]
        if len(set(keys)) != len(keys):
            duplicates = sorted({k for k in keys if keys.count(k) > 1})
            raise ValueError(f"duplicate routes in policy: {duplicates}")
        if self.idempotency_ttl_seconds < 0:
            raise ValueError("idempotency_ttl_seconds cannot be negative")

    def route_by_key(self, key: str) -> Route | None:
        for route in self.routes:
            if route.key == key:
                return route
        return None

    def with_routes(self, routes: Sequence[Route]) -> GatewayPolicy:
        return replace(self, routes=tuple(routes))


# ═══════════════════════════════════════════════════════════════════════════
# Planning
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Plan:
    """The plan the engine executes. Immutable, inspectable, testable."""

    request: LLMRequest
    #: Ordered candidate routes: primary first, then policy-selected fallbacks.
    routes: tuple[Route, ...]
    budget: TimeoutBudget
    retry: RetryPolicy
    fallback: FallbackPolicy
    idempotency_key: str | None = None
    #: Set when policy refuses the request outright; the engine raises it.
    refusal: PolicyRefusalError | None = None

    @property
    def refused(self) -> bool:
        return self.refusal is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "routes": [r.key for r in self.routes],
            "budget_total_seconds": self.budget.total_seconds,
            "budget_per_attempt_seconds": self.budget.per_attempt_seconds,
            "max_attempts": self.retry.max_attempts,
            "fallback_hops": self.fallback.max_hops,
            "idempotency_key": self.idempotency_key,
            "refused": self.refused,
        }


def _available(
    route: Route,
    policy: GatewayPolicy,
    world: WorldSnapshot,
    *,
    ignore_circuit: bool = False,
) -> bool:
    """True when *route* may be used right now, according to policy + world."""

    if policy.privacy.forbids(route):
        return False
    if not ignore_circuit and policy.circuit.enabled:
        snapshot = world.route(route.key)
        if snapshot.circuit_open:
            return False
    if policy.concurrency.max_inflight_per_provider <= world.per_provider_inflight.get(
        route.provider, 0
    ):
        return False
    return True


def plan(
    request: LLMRequest,
    policy: GatewayPolicy,
    world: WorldSnapshot,
    *,
    now: float,
) -> Plan:
    """Decide routes, budget and retry allowance for *request*. Pure.

    Order of evaluation is the mission's fallback pipeline made literal::

        select route → capability check → privacy check → circuit/quota check
        → budget → fallback candidates

    ``now`` is accepted (rather than read) so the function stays deterministic
    and its deadline arithmetic is reproducible in tests.
    """

    _ = now  # deadlines are computed by the engine from the loop clock
    budget = policy.timeout
    if request.deadline_seconds is not None:
        budget = policy.timeout.with_total(request.deadline_seconds)

    candidates = _select_routes(request, policy, world)
    if not candidates:
        reason = _refusal_reason(request, policy, world)
        return Plan(
            request=request,
            routes=(),
            budget=budget,
            retry=policy.retry,
            fallback=policy.fallback,
            idempotency_key=request.idempotency_key,
            refusal=PolicyRefusalError(
                reason,
                provider=request.provider,
                model=request.model,
            ),
        )

    return Plan(
        request=request,
        routes=candidates,
        budget=budget,
        retry=policy.retry,
        fallback=policy.fallback,
        idempotency_key=request.idempotency_key,
    )


def _select_routes(
    request: LLMRequest, policy: GatewayPolicy, world: WorldSnapshot
) -> tuple[Route, ...]:
    """Primary route first, then policy-eligible fallbacks in rank order.

    Three constraints are applied *before* ranking, because each of them is a
    hard "may not" rather than a preference:

    1. **Privacy** — a route forbidden by :class:`PrivacyPolicy` is not a
       candidate at all. Filtering it after selection would let a strict-privacy
       deployment send prompts to a training endpoint on the fallback hop.
    2. **A caller pin is an identity constraint** — if the caller named a
       provider or model and nothing registered matches it, the answer is a
       typed refusal. Silently answering from a different model would be a
       hidden fallback (LAW 8) and, for a summarizer or a memory extractor, a
       fabricated record.
    3. **Capability** — a route that cannot serve the requested operation or
       modality is not a candidate. Refusing before any bytes are sent costs no
       quota and yields an actionable error instead of a provider 400.

    Circuit state is *not* a filter: an open circuit must stay in the plan so the
    engine can run its bounded half-open probe, otherwise a tripped route could
    never recover. It only affects ordering — healthy routes are tried first.
    """

    def capable(route: Route) -> bool:
        if not policy.fallback.require_capability_match:
            return True
        return route.serves(request)

    allowed = [route for route in policy.routes if not policy.privacy.forbids(route)]
    if not allowed:
        return ()

    def ordered(routes: list[Route]) -> list[Route]:
        """Healthiest first: rank, then circuit state, then a stable key."""

        return sorted(
            routes,
            key=lambda r: (r.rank, world.route(r.key).circuit_open, r.key),
        )

    primary: Route | None = None

    # 1. Explicit caller pin (provider and/or model) — a hard constraint.
    if request.provider or request.model:
        matching = ordered(
            [
                route
                for route in allowed
                if (request.provider is None or route.provider == request.provider)
                and (request.model is None or route.model == request.model)
            ]
        )
        if not matching:
            return ()
        capable_matches = [route for route in matching if capable(route)]
        if not capable_matches:
            # The caller named a real route that cannot serve *this* request.
            # Substituting a sibling model would answer a different question.
            return ()
        primary = capable_matches[0]
    else:
        # 2. Routing rules (first match wins).
        for rule in policy.rules:
            if not rule.matches(request):
                continue
            for route in ordered(
                [
                    r
                    for r in allowed
                    if r.provider == rule.provider and r.model == rule.model and capable(r)
                ]
            ):
                primary = route
                break
            if primary is not None:
                break

        # 3. Lowest-rank capable, non-degraded, healthy route.
        if primary is None:
            preferred = ordered([r for r in allowed if capable(r) and not r.degraded])
            primary = preferred[0] if preferred else None

        # 4. Degraded local routes are the last resort, never the first choice.
        if primary is None:
            anything = ordered([r for r in allowed if capable(r)])
            primary = anything[0] if anything else None

    if primary is None:
        return ()

    selected: list[Route] = [primary]
    if request.allow_fallback and policy.fallback.enabled and policy.fallback.max_hops > 0:
        rest = ordered(
            [
                route
                for route in allowed
                if route.key != primary.key and capable(route) and not policy.privacy.forbids(route)
            ]
        )
        for route in rest[: policy.fallback.max_hops]:
            if route.degraded and not policy.fallback.allow_degraded_routes:
                continue
            selected.append(route)
    return tuple(selected)


def _refusal_reason(request: LLMRequest, policy: GatewayPolicy, world: WorldSnapshot) -> str:
    """Explain *why* no route was selected — precisely enough to be actionable.

    The order of checks mirrors :func:`_select_routes` so the reason names the
    constraint that actually fired, not the first one that happens to be true.
    """

    if not policy.routes:
        return "no LLM route is registered with the gateway"

    allowed = [r for r in policy.routes if not policy.privacy.forbids(r)]
    forbidden = [r.key for r in policy.routes if policy.privacy.forbids(r)]
    if not allowed:
        return f"every registered route is forbidden by privacy policy: {forbidden}"

    def capable(route: Route) -> bool:
        if not policy.fallback.require_capability_match:
            return True
        return route.serves(request)

    if request.provider or request.model:
        matching = [
            r
            for r in allowed
            if (request.provider is None or r.provider == request.provider)
            and (request.model is None or r.model == request.model)
        ]
        if not matching:
            return (
                f"no route matches the pinned provider/model "
                f"({request.provider or '*'}/{request.model or '*'}); "
                f"registered={[r.key for r in policy.routes]}"
            )
        if not any(capable(r) for r in matching):
            needed = sorted(m.value for m in request.modalities)
            return (
                f"the pinned route {[r.key for r in matching]} cannot serve "
                f"operation={request.operation.value} modalities={needed}"
            )

    served = [r for r in allowed if capable(r)]
    if not served:
        needed = sorted(m.value for m in request.modalities)
        return (
            f"no registered route serves operation={request.operation.value} "
            f"modalities={needed}; registered={[r.key for r in allowed]}"
        )

    cooling = [r.key for r in served if policy.circuit.enabled and world.route(r.key).circuit_open]
    if cooling and len(cooling) == len(served):
        # Not a selection failure — the engine keeps these in the plan so it can
        # probe — but if a caller ever asks, this is the honest description.
        return f"every capable route has an open circuit breaker: {cooling}"

    return f"no route available for {[r.key for r in served]}"


def default_policy(
    routes: Sequence[Route] = (),
    rules: Sequence[RouteRule] = (),
    **overrides: Any,
) -> GatewayPolicy:
    """A conservative, evidence-based starting policy.

    The defaults are not arbitrary:

    * ``max_attempts=3`` with full jitter and a 0.5s→8s envelope matches the
      shape of Google's own SDK defaults (initial ~1s, capped, bounded attempts)
      while staying inside a 60s caller budget;
    * ``total=60s`` is the existing ``NEXUS_LLM_REQUEST_TIMEOUT`` default, so
      migrating a caller onto the gateway does not silently change its deadline;
    * ``connect=5s``/``read=45s``/``per_attempt=50s`` satisfy the budget
      invariant ``connect + read <= per_attempt <= total``;
    * concurrency 4 global / 2 per provider keeps a free-tier quota from being
      burned by a burst while still allowing parallel tenants;
    * the wait queue is bounded at 32 and *rejects* on saturation — shedding
      load is the honest response to overload, growing memory is not.

    ``overrides`` are keyword names of :class:`GatewayPolicy` fields, so a
    deployment can tighten one layer without rebuilding the rest.
    """

    kwargs: dict[str, Any] = {"routes": tuple(routes), "rules": tuple(rules)}
    kwargs.update(overrides)
    return GatewayPolicy(**kwargs)
