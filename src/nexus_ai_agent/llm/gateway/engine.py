"""The LLM Gateway — one authority for every canonical LLM execution (W2).

Pipeline, literally as the mission specifies::

    Caller
      → LLMGateway.execute(request)
        → Policy        (route selection, capability/privacy/context gate, budget)
        → Scheduler     (bounded global + per-provider + per-tenant admission)
        → Rate limit    (provider-aware window; Retry-After aware)
        → Circuit       (provider-health only; half-open probe)
        → Provider adapter (the only place that knows a wire format)
      ← AdapterResult | typed LLMError
        → Retry / backoff (deadline-clamped, jittered, cancellation-aware)
        → Fallback        (policy-eligible kinds only, always recorded)
        → Structured output validation (caller's contract, gateway's boundary)
      ← LLMResponse | LLMError
    Caller

Invariants this class exists to guarantee
-----------------------------------------

**One authority.** There is no code path from a caller to a provider that does
not pass through :meth:`execute`. Adapters are registered, not constructed by
callers; the process-wide accessor lives in :mod:`nexus_ai_agent.llm.gateway.registry`.

**Cancellation is sacred (LAW 5).** Three distinct sources are handled
separately and none of them can leak work:

* task cancellation (``asyncio.CancelledError``) is always re-raised unchanged —
  the gateway never converts it into a typed error, because swallowing it would
  break ``TaskGroup``/``timeout()`` semantics for every caller above;
* an external cancel token (``request.cancellation``) raises the typed
  :class:`CancelledByCallerError` and stops the retry chain immediately;
* the deadline raises :class:`DeadlineExceededError`.

Cancellation is checked before admission, while queued (inside the scheduler),
before every attempt, during every backoff sleep, and — when a cancel token is
supplied — *during* the provider call itself, by racing the call against the
token and cancelling the in-flight task. Every task the gateway creates is
cancelled **and awaited** in a ``finally``, so there is no orphan task and no
"Task was destroyed but it is pending" warning under adversarial cancellation.

**Retry never destroys the caller's deadline.** One absolute deadline covers
queue wait, rate wait, every attempt and every backoff sleep. A backoff that
would not fit inside the remaining budget ends the chain instead of being
truncated, and a provider ``Retry-After`` longer than
``RetryPolicy.max_retry_after_seconds`` is declined rather than waited out.

**Bounded everything (LAW 6).** Attempts per route, routes per request, queue
depth, concurrency, rate windows, circuit state, idempotency cache and the
observability ring buffer all have explicit finite bounds. Nothing grows with
traffic.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from nexus_ai_agent.llm.errors import (
    CancelledByCallerError,
    ContextLimitError,
    DeadlineExceededError,
    GatewayClosedError,
    GatewayInternalError,
    LLMError,
    LLMErrorKind,
    OverloadedError,
    QuotaExhaustedError,
    RateLimitedError,
    StructuredOutputInvalidError,
    TransientProviderError,
)
from nexus_ai_agent.llm.gateway.adapters import AdapterResult, ProviderAdapter
from nexus_ai_agent.llm.gateway.contract import (
    MONOTONIC,
    AttemptRecord,
    Caller,
    Clock,
    ContentPart,
    GenerationParams,
    LLMOperation,
    LLMPriority,
    LLMRequest,
    LLMResponse,
    Message,
    PolicyOutcome,
    Timings,
    Usage,
    new_request_id,
)
from nexus_ai_agent.llm.gateway.observability import (
    OUTCOME_CANCELLED,
    OUTCOME_CLOSED,
    OUTCOME_DEADLINE,
    OUTCOME_DEGRADED,
    OUTCOME_ERROR,
    OUTCOME_OVERLOADED,
    OUTCOME_REFUSED,
    OUTCOME_SUCCESS,
    GatewayMetrics,
    ObservationSink,
    RequestRecord,
    StructlogSink,
    build_error_record,
    sanitize_metadata,
)
from nexus_ai_agent.llm.gateway.policy import (
    GatewayPolicy,
    Plan,
    Route,
    RouteSnapshot,
    WorldSnapshot,
)
from nexus_ai_agent.llm.gateway.resilience import (
    CircuitBreaker,
    WindowRateLimiter,
    compute_backoff,
    parse_retry_after,
)
from nexus_ai_agent.llm.gateway.scheduler import Admission, BoundedScheduler, SchedulerPort
from nexus_ai_agent.llm.gateway.usage import DEFAULT_PRICE_TABLE, ModelPrice, apply_cost
from nexus_ai_agent.observability.logging import get_logger

__all__ = ["GatewayBuilder", "LLMGateway"]

log = get_logger(__name__)


def _timeout_types() -> tuple[type[BaseException], ...]:
    """``asyncio.wait_for`` timeout classes across supported interpreters.

    CPython 3.10 raises ``asyncio.TimeoutError``, which subclasses ``Exception``
    but is *not* the builtin ``TimeoutError``; 3.11+ aliases the two
    (https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for).
    Matching both keeps the 3.10/3.11/3.12 CI parity legs on one contract.
    """

    return (TimeoutError, asyncio.TimeoutError)


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, _timeout_types())


#: How long a provider call gets to unwind after the budget expired. Short on
#: purpose: the caller is already waiting, and the adapter's own transport
#: timeout (configured from the same budget) is what finally stops it.
_CANCEL_GRACE_SECONDS = 0.25
#: How long to wait for a cancelled call in every other case (withdrawal, error).
_SETTLE_GRACE_SECONDS = 0.05
#: Administrative observer bound; not a traffic-dependent buffer.
_MAX_SINKS = 128


@dataclass
class _AttemptContext:
    """Mutable per-request execution state. One instance per :meth:`execute`."""

    request_id: str
    started: float
    deadline_at: float | None
    attempts: list[AttemptRecord]
    queued_seconds: float = 0.0
    rate_waited_seconds: float = 0.0
    executed_seconds: float = 0.0
    backoff_seconds: float = 0.0
    retries: int = 0
    total_attempts: int = 0
    fallback_used: bool = False
    fallback_from: str | None = None
    circuit_open: bool = False
    rate_limited_locally: bool = False
    #: Set when the route that finally answered is itself a degraded one (a local
    #: fallback engine, a stub). LAW 8 does not only forbid a *hidden hop*: an
    #: answer from a degraded route is degraded whether or not anything failed
    #: first, and the caller has to be able to see that.
    served_by_degraded_route: bool = False

    def remaining(self, now: float) -> float | None:
        if self.deadline_at is None:
            return None
        return self.deadline_at - now

    def timings(self, now: float) -> Timings:
        # Every phase is clamped at zero. ``MONOTONIC`` cannot go backwards, but
        # an injected or wrapped clock can (NTP step, test double), and a
        # negative duration in a record is a lie an operator would chase.
        return Timings(
            queued_seconds=max(0.0, self.queued_seconds),
            rate_waited_seconds=max(0.0, self.rate_waited_seconds),
            executed_seconds=max(0.0, self.executed_seconds),
            backoff_seconds=max(0.0, self.backoff_seconds),
            total_seconds=max(0.0, now - self.started),
        )

    def outcome(self, *, provider: str | None, model: str | None) -> PolicyOutcome:
        return PolicyOutcome(
            route_provider=provider or "",
            route_model=model or "",
            attempts=self.total_attempts,
            retries=self.retries,
            fallback_used=self.fallback_used,
            fallback_from=self.fallback_from,
            degraded=self.fallback_used or self.served_by_degraded_route,
            circuit_open=self.circuit_open,
            rate_limited_locally=self.rate_limited_locally,
        )


class LLMGateway:
    """The single LLM execution authority.

    Construct through :func:`nexus_ai_agent.llm.gateway.registry.get_llm_gateway`
    (or :class:`GatewayBuilder` in tests) so that every caller in the process
    shares one policy, one set of bulkheads and one quota view.
    """

    def __init__(
        self,
        *,
        policy: GatewayPolicy,
        adapters: Mapping[str, ProviderAdapter] | None = None,
        scheduler: SchedulerPort | None = None,
        clock: Clock = MONOTONIC,
        sinks: Sequence[ObservationSink] | None = None,
        rng: random.Random | None = None,
        price_table: Mapping[str, ModelPrice] | None = None,
        idempotency_capacity: int = 128,
        attach_default_sink: bool = True,
    ) -> None:
        if idempotency_capacity < 0:
            raise ValueError("idempotency_capacity cannot be negative")
        self._policy = policy
        self._adapters: dict[str, ProviderAdapter] = dict(adapters or {})
        self._scheduler: SchedulerPort = scheduler or BoundedScheduler(
            policy.concurrency, clock=clock
        )
        self._clock = clock
        self._rng = rng or random.Random()
        self._prices = DEFAULT_PRICE_TABLE if price_table is None else price_table
        self._sinks: list[ObservationSink] = list(sinks or [])
        if len(self._sinks) > _MAX_SINKS:
            raise ValueError("too many observation sinks")
        if attach_default_sink and not self._sinks:
            self._sinks.append(StructlogSink())
        self._metrics = GatewayMetrics()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._limiters: dict[str, WindowRateLimiter] = {}
        self._idempotency_capacity = idempotency_capacity
        self._inflight: OrderedDict[str, asyncio.Future[LLMResponse]] = OrderedDict()
        self._completed: OrderedDict[str, tuple[float, LLMResponse]] = OrderedDict()
        self._closed = False
        self._retired = False
        self._active_calls = 0
        self._owner_pid = os.getpid()
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._execution_lock = threading.Lock()
        self._executed = 0
        self._missing_adapters_logged: set[str] = set()
        self._abandoned: set[asyncio.Future[Any]] = set()

    # ── registration ─────────────────────────────────────────────────
    @property
    def policy(self) -> GatewayPolicy:
        return self._policy

    @property
    def metrics(self) -> GatewayMetrics:
        return self._metrics

    @property
    def closed(self) -> bool:
        return self._closed or self._retired

    def add_sink(self, sink: ObservationSink) -> None:
        """Attach an additional observation sink (bounded list, no duplicates)."""

        if sink not in self._sinks:
            if len(self._sinks) >= _MAX_SINKS:
                raise ValueError("too many observation sinks")
            self._sinks.append(sink)

    def register(self, adapter: ProviderAdapter, routes: Sequence[Route] | None = None) -> None:
        """Register an adapter and (optionally) its routes.

        Routes default to one per model already present in the policy for this
        provider, or a single ``adapter.name/adapter.name`` route when the policy
        has none — so ``register(GeminiHttpAdapter(...))`` is enough for the
        common single-model case.
        """

        self._adapters[adapter.name] = adapter
        if routes is None:
            existing = [r for r in self._policy.routes if r.provider == adapter.name]
            routes = existing or [
                Route(
                    provider=adapter.name,
                    model=adapter.name,
                    operations=adapter.operations,
                    modalities=adapter.modalities,
                )
            ]
        normalized: list[Route] = []
        for route in routes:
            normalized.append(
                replace(
                    route,
                    operations=route.operations & adapter.operations or route.operations,
                    modalities=route.modalities & adapter.modalities or route.modalities,
                )
            )
        merged = [r for r in self._policy.routes if r.provider != adapter.name]
        merged.extend(normalized)
        self._policy = replace(self._policy, routes=tuple(merged))

    def adapter(self, provider: str) -> ProviderAdapter | None:
        return self._adapters.get(provider)

    # ── public execution API ─────────────────────────────────────────
    def retire(self) -> None:
        """Revoke an idle registry reference; cleanup remains the owner's job.

        A synchronous reset cannot drain active async work. Refusing that reset
        is safer than publishing a second authority while the first is running.
        """
        with self._execution_lock:
            if self._active_calls or any(not task.done() for task in self._abandoned):
                raise GatewayInternalError("cannot retire an authority with active work")
            self._retired = True

    async def execute(self, request: LLMRequest) -> LLMResponse:
        loop = asyncio.get_running_loop()
        with self._execution_lock:
            if self._closed or self._retired:
                raise GatewayClosedError("LLM gateway is closed or retired")
            if os.getpid() != self._owner_pid:
                raise GatewayInternalError("inherited gateway cannot execute in a worker process")
            if self._owner_loop is not None and self._owner_loop is not loop:
                raise GatewayInternalError("gateway execution belongs to a different event loop")
            self._owner_loop = loop
            self._active_calls += 1
        try:
            return await self._execute(request)
        finally:
            with self._execution_lock:
                self._active_calls -= 1

    async def _execute(self, request: LLMRequest) -> LLMResponse:
        """Run *request* through policy, admission, provider and observability.

        Returns an :class:`LLMResponse` or raises an :class:`LLMError` (or
        re-raises ``asyncio.CancelledError`` when the caller's task was
        cancelled). It never returns an error-shaped string: a caller cannot
        accidentally treat a failure as an answer.
        """

        request_id = new_request_id()
        started = self._clock()
        current_plan = self._plan(request, started)
        context = _AttemptContext(
            request_id=request_id,
            started=started,
            deadline_at=(
                None
                if current_plan.budget.total_seconds is None
                else started + current_plan.budget.total_seconds
            ),
            attempts=[],
        )
        if request.deadline_seconds is not None:
            context.deadline_at = started + min(
                current_plan.budget.total_seconds, request.deadline_seconds
            )
        metadata = sanitize_metadata(request.metadata)
        try:
            self._check_cancellation(request, context)
        except LLMError as exc:
            self._fail(request, context, exc, outcome=_outcome_for(exc), metadata=metadata)
            raise

        if current_plan.refused:
            assert current_plan.refusal is not None
            error = current_plan.refusal.with_route(
                provider=request.provider or "",
                model=request.model or "",
                request_id=request_id,
                attempt=0,
            )
            self._fail(
                request,
                context,
                error,
                outcome=OUTCOME_REFUSED,
                metadata=metadata,
            )
            raise error

        context_gate = self._context_gate(request, current_plan)
        if context_gate is not None:
            self._fail(request, context, context_gate, outcome=OUTCOME_ERROR, metadata=metadata)
            raise context_gate

        capacity = (
            self._policy.concurrency.max_inflight_global + self._policy.concurrency.max_queued
        )
        if self._active_calls > capacity or any(not task.done() for task in self._abandoned):
            error = OverloadedError("gateway has no safe execution capacity", request_id=request_id)
            self._fail(request, context, error, outcome=OUTCOME_OVERLOADED, metadata=metadata)
            raise error
        if request.idempotency_key:
            return await self._execute_idempotent(request, context, metadata)
        return await self._run(request, current_plan, context, metadata)

    async def generate(
        self,
        prompt: str,
        *,
        caller: Caller,
        system: str | None = None,
        purpose: str = "chat",
        operation: LLMOperation = LLMOperation.CHAT,
        messages: Sequence[Message] = (),
        parts: Sequence[ContentPart] = (),
        model: str | None = None,
        provider: str | None = None,
        priority: LLMPriority = LLMPriority.NORMAL,
        deadline_seconds: float | None = None,
        allow_fallback: bool = True,
        generation: GenerationParams | None = None,
        output_validator: Any | None = None,
        idempotency_key: str | None = None,
        cancellation: Any | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> LLMResponse:
        """Convenience wrapper that builds an :class:`LLMRequest` and executes it."""

        request = LLMRequest(
            caller=caller,
            purpose=purpose,
            operation=operation,
            prompt=prompt,
            system=system,
            messages=tuple(messages),
            parts=tuple(parts),
            model=model,
            provider=provider,
            priority=priority,
            deadline_seconds=deadline_seconds,
            allow_fallback=allow_fallback,
            generation=generation or GenerationParams(),
            output_validator=output_validator,
            idempotency_key=idempotency_key,
            cancellation=cancellation,
            metadata=metadata or {},
        )
        return await self.execute(request)

    async def text(self, prompt: str, *, caller: Caller, **kwargs: Any) -> str:
        """``generate`` but returning only the text — the migration-friendly shape."""

        response = await self.generate(prompt, caller=caller, **kwargs)
        return response.text

    async def embed(self, text: str, *, caller: Caller, **kwargs: Any) -> tuple[float, ...]:
        """Embeddings through the same authority, with the same policy."""

        request = LLMRequest(
            caller=caller,
            purpose=kwargs.pop("purpose", "embeddings"),
            operation=LLMOperation.EMBEDDINGS,
            prompt=text,
            model=kwargs.pop("model", None),
            provider=kwargs.pop("provider", None),
            priority=kwargs.pop("priority", LLMPriority.NORMAL),
            deadline_seconds=kwargs.pop("deadline_seconds", None),
            cancellation=kwargs.pop("cancellation", None),
            metadata=kwargs.pop("metadata", None) or {},
        )
        if kwargs:
            raise TypeError(f"embed() got unexpected keyword arguments: {sorted(kwargs)}")
        response = await self.execute(request)
        if response.embedding is None:
            raise GatewayInternalError(
                "embedding route returned no vector",
                provider=response.provider,
                model=response.model,
                request_id=response.request_id,
            )
        return response.embedding

    # ── lifecycle & introspection ────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """Operator view of the authority. Bounded, secret-free, JSON-safe."""

        return {
            "closed": self._closed,
            "policy_version": self._policy.version,
            "routes": [
                {
                    "key": route.key,
                    "rank": route.rank,
                    "degraded": route.degraded,
                    "operations": sorted(o.value for o in route.operations),
                    "modalities": sorted(m.value for m in route.modalities),
                    "circuit": self._breaker(route.key).snapshot(self._clock()),
                }
                for route in self._policy.routes
            ],
            "adapters": sorted(self._adapters),
            "scheduler": self._scheduler.snapshot(),
            "rate_limits": {
                key: limiter.snapshot(self._clock())
                for key, limiter in self._limiters.items()
                # ``None`` is the stored answer for "this provider has no
                # configured limit" (see :meth:`_limiter`); it is not a limiter
                # and must not be reported as one.
                if limiter is not None
            },
            "idempotency": {
                "inflight": len(self._inflight),
                "cached": len(self._completed),
                "capacity": self._idempotency_capacity,
                "ttl_seconds": self._policy.idempotency_ttl_seconds,
            },
            "executed": self._executed,
            "metrics": self._metrics.as_dict(),
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._metrics.recent(limit)

    async def aclose(self) -> None:
        """Shut the authority down: refuse new work, settle waiters, release adapters.

        Ordered and progress-preserving: one adapter raising during close never
        prevents the remaining adapters, the scheduler or the HTTP pools from
        being released. Idempotent.
        """

        if self._closed:
            return
        self._closed = True
        try:
            await self._scheduler.aclose()
        except Exception:  # noqa: BLE001 — shutdown must continue
            log.error("llm_gateway_scheduler_close_failed")
        for name, adapter in list(self._adapters.items()):
            try:
                await adapter.aclose()
            except asyncio.CancelledError:
                log.warning("llm_gateway_adapter_close_cancelled", adapter=name)
                raise
            except Exception:  # noqa: BLE001
                log.error("llm_gateway_adapter_close_failed", adapter=name)
        for task in list(self._abandoned):
            if not task.done():
                task.cancel()
        # Done callbacks remove settled tasks. Never forget a still-live task.
        for future in list(self._inflight.values()):
            if not future.done():
                future.cancel()
        self._inflight.clear()
        self._completed.clear()
        log.info("llm_gateway_closed", executed=self._executed)

    # ── planning & gating ────────────────────────────────────────────
    def _plan(self, request: LLMRequest, now: float) -> Plan:
        from nexus_ai_agent.llm.gateway.policy import plan

        return plan(request, self._policy, self._world(), now=now)

    def _world(self) -> WorldSnapshot:
        now = self._clock()
        routes: dict[str, RouteSnapshot] = {}
        for route in self._policy.routes:
            limiter = self._limiters.get(route.provider)
            routes[route.key] = RouteSnapshot(
                circuit_open=self._breaker(route.key).is_open(now),
                cooling_down=self._breaker(route.key).state(now).value != "closed",
                inflight=0,
                minute_window_used=(limiter.snapshot(now).get("minute_used", 0) if limiter else 0),
                day_window_used=(limiter.snapshot(now).get("day_used", 0) if limiter else 0),
            )
        scheduler_state = self._scheduler.snapshot()
        per_provider = scheduler_state.get("inflight_per_provider", {})
        return WorldSnapshot(
            routes=routes,
            global_inflight=int(scheduler_state.get("inflight_global", 0)),
            queued=int(scheduler_state.get("queued", 0)),
            per_provider_inflight={str(k): int(v) for k, v in per_provider.items()},
            per_tenant_inflight={
                int(k): int(v) for k, v in scheduler_state.get("inflight_per_tenant", {}).items()
            },
        )

    def _context_gate(self, request: LLMRequest, current_plan: Plan) -> LLMError | None:
        """Refuse an oversized request deterministically, before spending quota."""

        size = request.prompt_size_chars()
        for route in current_plan.routes:
            limit = route.context_window_chars
            if limit is None:
                return None  # no declared gate on the primary route → no opinion
            if size > limit:
                return ContextLimitError(
                    f"payload of {size} chars exceeds the {route.key} gate ({limit})",
                    provider=route.provider,
                    model=route.model,
                    detail=f"payload_chars={size} limit_chars={limit}",
                )
        return None

    def _breaker(self, key: str) -> CircuitBreaker:
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(key=key, policy=self._policy.circuit, clock=self._clock)
            self._breakers[key] = breaker
        return breaker

    def _limiter(self, provider: str) -> WindowRateLimiter | None:
        if provider in self._limiters:
            return self._limiters[provider]
        limits = self._policy.rate_limit.for_provider(provider)
        if limits.requests_per_minute is None and limits.requests_per_day is None:
            self._limiters[provider] = None  # type: ignore[assignment]
            return None
        limiter = WindowRateLimiter(
            key=provider,
            requests_per_minute=limits.requests_per_minute,
            requests_per_day=limits.requests_per_day,
            clock=self._clock,
        )
        self._limiters[provider] = limiter
        return limiter

    # ── idempotency ──────────────────────────────────────────────────
    async def _execute_idempotent(
        self,
        request: LLMRequest,
        context: _AttemptContext,
        metadata: Mapping[str, str],
    ) -> LLMResponse:
        """Single-flight + short-lived result reuse for caller-declared duplicate keys.

        Two identical keys in flight share ONE provider execution (so a retry
        storm cannot double-spend quota); a key that already completed inside the
        TTL returns the stored response with ``idempotency_hit=True``. Waiters
        shield the shared future so one caller's cancellation cannot strand the
        others.
        """

        assert request.idempotency_key is not None
        key = _scoped_idempotency_key(request)
        ttl = self._policy.idempotency_ttl_seconds
        now = self._clock()

        if ttl > 0:
            cached = self._completed.get(key)
            if cached is not None:
                expires_at, response = cached
                if expires_at > now:
                    return self._reuse_response(request, response, context, metadata)
                self._completed.pop(key, None)

        loop = asyncio.get_running_loop()
        self._check_cancellation(request, context)
        existing = self._inflight.get(key)
        if existing is not None:
            cancel_task = (
                _watch_cancel(request.cancellation) if request.cancellation is not None else None
            )
            watched = {existing}
            if cancel_task is not None:
                watched.add(cancel_task)
            try:
                done, _ = await asyncio.wait(
                    watched,
                    timeout=context.remaining(self._clock()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task is not None and cancel_task in done:
                    raise CancelledByCallerError(request_id=context.request_id)
                if existing not in done:
                    raise self._deadline_error(request, context, None)
                try:
                    return self._reuse_response(request, existing.result(), context, metadata)
                except asyncio.CancelledError:
                    raise CancelledByCallerError(
                        "shared execution was cancelled by its owner",
                        request_id=context.request_id,
                        detail="shared_execution_cancelled",
                    ) from None
            except asyncio.CancelledError:
                self._fail(
                    request,
                    context,
                    CancelledByCallerError(request_id=context.request_id),
                    outcome=OUTCOME_CANCELLED,
                    metadata=metadata,
                )
                raise
            except LLMError as exc:
                self._fail(request, context, exc, outcome=_outcome_for(exc), metadata=metadata)
                raise
            finally:
                if cancel_task is not None:
                    cancel_task.cancel()
                    await self._settle(
                        (cancel_task,),
                        adapter="idempotency",
                        request_id=context.request_id,
                        grace=_SETTLE_GRACE_SECONDS,
                    )

        if len(self._inflight) >= max(1, self._idempotency_capacity):
            error = OverloadedError("idempotency admission is full", request_id=context.request_id)
            self._fail(request, context, error, outcome=OUTCOME_OVERLOADED, metadata=metadata)
            raise error
        future: asyncio.Future[LLMResponse] = loop.create_future()
        self._inflight[key] = future
        self._inflight.move_to_end(key)
        try:
            response = await self._run(request, self._plan(request, now), context, metadata)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
                # Mark observed even when there were no coalesced waiters.
                future.exception()
            raise
        else:
            if not future.done():
                future.set_result(response)
            if ttl > 0 and self._idempotency_capacity > 0:
                self._completed[key] = (self._clock() + ttl, response)
                self._completed.move_to_end(key)
                while len(self._completed) > self._idempotency_capacity:
                    self._completed.popitem(last=False)
            return response
        finally:
            if self._inflight.get(key) is future:
                del self._inflight[key]
            if not future.done():
                future.cancel()

    # ── core execution loop ──────────────────────────────────────────
    async def _run(
        self,
        request: LLMRequest,
        current_plan: Plan,
        context: _AttemptContext,
        metadata: Mapping[str, str],
    ) -> LLMResponse:
        budget = current_plan.budget
        last_error: LLMError | None = None
        tried_any = False

        for hop, route in enumerate(current_plan.routes):
            if hop > 0:
                context.fallback_used = True
                if context.fallback_from is None:
                    context.fallback_from = current_plan.routes[0].key
            adapter = self._adapters.get(route.provider)
            if adapter is None:
                if route.provider not in self._missing_adapters_logged:
                    self._missing_adapters_logged.add(route.provider)
                    log.error("llm_gateway_route_without_adapter", provider=route.provider)
                last_error = GatewayInternalError(
                    f"route {route.key} has no registered adapter",
                    provider=route.provider,
                    model=route.model,
                    request_id=context.request_id,
                )
                continue

            breaker = self._breaker(route.key)
            if breaker.is_open(self._clock()):
                context.circuit_open = True
                last_error = TransientProviderError(
                    f"circuit breaker open for {route.key}",
                    kind=LLMErrorKind.TRANSIENT_PROVIDER,
                    provider=route.provider,
                    model=route.model,
                    request_id=context.request_id,
                    detail="circuit_open",
                )
                continue

            try:
                return await self._attempt_route(
                    request,
                    route,
                    adapter,
                    breaker,
                    current_plan,
                    context,
                    metadata,
                )
            except asyncio.CancelledError:
                self._fail(
                    request,
                    context,
                    CancelledByCallerError(request_id=context.request_id),
                    outcome=OUTCOME_CANCELLED,
                    metadata=metadata,
                )
                raise
            except LLMError as exc:
                last_error = exc
                tried_any = True
                if not exc.fallback_eligible or not self._policy.fallback.allows(exc.kind):
                    self._fail(request, context, exc, outcome=_outcome_for(exc), metadata=metadata)
                    raise
                if exc.kind is LLMErrorKind.DEADLINE_EXCEEDED:
                    self._fail(request, context, exc, outcome=OUTCOME_DEADLINE, metadata=metadata)
                    raise
                log.info(
                    "llm_gateway_route_failed_trying_fallback",
                    request_id=context.request_id,
                    route=route.key,
                    error_kind=exc.kind.value,
                    hop=hop,
                )
                continue

        error = last_error or GatewayInternalError(
            "gateway produced no route outcome", request_id=context.request_id
        )
        if not tried_any and context.circuit_open:
            error = TransientProviderError(
                "every candidate route has an open circuit breaker",
                provider=error.provider,
                model=error.model,
                request_id=context.request_id,
                detail="all_circuits_open",
            )
        _ = budget
        self._fail(request, context, error, outcome=_outcome_for(error), metadata=metadata)
        raise error

    async def _attempt_route(
        self,
        request: LLMRequest,
        route: Route,
        adapter: ProviderAdapter,
        breaker: CircuitBreaker,
        current_plan: Plan,
        context: _AttemptContext,
        metadata: Mapping[str, str],
    ) -> LLMResponse:
        """Retry loop for ONE route. Raises the terminal typed error."""

        budget = current_plan.budget
        retry = current_plan.retry
        last_error: LLMError | None = None
        previous_delay: float | None = None
        attempt_index = 0

        while True:
            now = self._clock()
            self._check_cancellation(request, context)
            if self._closed:
                # Shutdown stops the chain at the next boundary: the adapters'
                # transport pools are being released, so another attempt would
                # fail against a closed client and report a transport error
                # instead of the truth (LAW 5: stopping means stopping).
                raise GatewayClosedError(
                    "LLM gateway closed while a retry chain was in flight",
                    provider=route.provider,
                    model=route.model,
                    request_id=context.request_id,
                    attempt=attempt_index + 1,
                )
            remaining = context.remaining(now)
            if remaining is not None and remaining <= 0:
                raise self._deadline_error(request, context, last_error)

            if attempt_index >= retry.max_attempts:
                assert last_error is not None  # loop only continues after an error
                raise last_error

            # ── admission (bounded queue + bulkheads) ─────────────────
            queue_budget = budget.queue_wait_seconds
            if remaining is not None:
                queue_budget = min(queue_budget, max(0.0, remaining))
            admission: Admission | None = None
            try:
                admission = await self._scheduler.acquire(
                    provider=route.provider,
                    tenant_id=request.caller.tenant_id,
                    priority=request.priority,
                    queue_budget_seconds=queue_budget,
                    cancellation=request.cancellation,
                )
            except OverloadedError as exc:
                context.queued_seconds += 0.0
                raise exc.with_route(
                    provider=route.provider,
                    model=route.model,
                    request_id=context.request_id,
                    attempt=attempt_index + 1,
                ) from None
            except GatewayClosedError as exc:
                raise exc.with_route(
                    provider=route.provider,
                    model=route.model,
                    request_id=context.request_id,
                    attempt=attempt_index + 1,
                ) from None
            except asyncio.CancelledError:
                raise
            context.queued_seconds += admission.queued_seconds

            context.served_by_degraded_route = route.degraded
            permit = breaker.acquire(self._clock())
            try:
                if any(not task.done() for task in self._abandoned):
                    raise OverloadedError(
                        "provider cleanup has not settled", request_id=context.request_id
                    )
                if permit is None:
                    context.circuit_open = True
                    raise TransientProviderError(
                        "circuit breaker denies this attempt",
                        provider=route.provider,
                        model=route.model,
                        request_id=context.request_id,
                        detail="circuit_open",
                    )
                # ── provider-aware rate gate (inside the held slot) ───
                await self._rate_gate(route, request, context)

                # ── one provider attempt under its own timeout ────────
                now = self._clock()
                remaining = context.remaining(now)
                if remaining is not None and remaining <= 0:
                    raise self._deadline_error(request, context, last_error)
                attempt_timeout = budget.per_attempt_seconds
                clamped = False
                if remaining is not None:
                    attempt_timeout = min(attempt_timeout, remaining)
                    clamped = attempt_timeout < budget.per_attempt_seconds
                started_attempt = self._clock()
                context.total_attempts += 1
                try:
                    result = await self._invoke(
                        adapter,
                        request,
                        route,
                        budget=budget,
                        request_id=context.request_id,
                        attempt=attempt_index + 1,
                        timeout=attempt_timeout,
                        clamped=clamped,
                    )
                except asyncio.CancelledError:
                    duration = self._clock() - started_attempt
                    context.executed_seconds += duration
                    context.attempts.append(
                        AttemptRecord(
                            index=attempt_index + 1,
                            provider=route.provider,
                            model=route.model,
                            started_at=started_attempt,
                            duration_seconds=duration,
                            outcome="cancelled",
                        )
                    )
                    permit.release()
                    raise
                except LLMError as exc:
                    duration = self._clock() - started_attempt
                    context.executed_seconds += duration
                    stamped = exc.with_route(
                        provider=route.provider,
                        model=route.model,
                        request_id=context.request_id,
                        attempt=attempt_index + 1,
                    )
                    context.attempts.append(
                        AttemptRecord(
                            index=attempt_index + 1,
                            provider=route.provider,
                            model=route.model,
                            started_at=started_attempt,
                            duration_seconds=duration,
                            outcome="error",
                            error_kind=stamped.kind.value,
                            status_code=stamped.status_code,
                        )
                    )
                    permit.failure(stamped.kind, self._clock())
                    last_error = stamped
                    # Retry decision — the only place in the repository that
                    # makes one for an LLM call (LAW 4, LAW 7).
                    if not retry.allows(stamped.kind) or attempt_index + 1 >= retry.max_attempts:
                        raise stamped from exc
                    delay = self._next_delay(stamped, attempt_index, retry, previous_delay)
                    previous_delay = delay
                    if delay is None:
                        # Provider asked for a wait we refuse to serve: fail now
                        # instead of holding the caller's deadline hostage.
                        raise stamped from exc
                    if not await self._backoff(delay, request, context):
                        raise self._deadline_error(request, context, stamped) from exc
                    attempt_index += 1
                    context.retries += 1
                    continue
                else:
                    duration = self._clock() - started_attempt
                    context.executed_seconds += duration
                    # ``asyncio.wait_for`` only enforces a cap if the coroutine
                    # honours cancellation. An adapter that catches
                    # ``CancelledError`` and answers anyway would otherwise hand
                    # back a *late* answer as though the budget had been kept —
                    # and the budget is the one promise the caller can rely on
                    # (LAW 7). Both overruns are reported instead of absorbed.
                    if context.deadline_at is not None and self._clock() >= context.deadline_at:
                        permit.release()
                        raise self._deadline_error(request, context, last_error)
                    tolerance = max(0.05, attempt_timeout * 0.1)
                    if duration > attempt_timeout + tolerance:
                        permit.release()
                        raise self._timeout_error(
                            route, context.request_id, attempt_index + 1, clamped
                        )
                    if not isinstance(result, AdapterResult):
                        # LAW 1: the adapter boundary is typed. A provider
                        # adapter that returns anything else is a gateway bug,
                        # and it must surface as one — not as an AttributeError
                        # three frames deeper where nobody can classify it.
                        context.attempts.append(
                            AttemptRecord(
                                index=attempt_index + 1,
                                provider=route.provider,
                                model=route.model,
                                started_at=started_attempt,
                                duration_seconds=duration,
                                outcome="error",
                                error_kind=LLMErrorKind.GATEWAY_INTERNAL.value,
                            )
                        )
                        permit.release()
                        raise GatewayInternalError(
                            f"adapter {adapter.name} returned "
                            f"{type(result).__name__} instead of AdapterResult",
                            provider=route.provider,
                            model=route.model,
                            request_id=context.request_id,
                            attempt=attempt_index + 1,
                        )
                    context.attempts.append(
                        AttemptRecord(
                            index=attempt_index + 1,
                            provider=route.provider,
                            model=route.model,
                            started_at=started_attempt,
                            duration_seconds=duration,
                            outcome="success",
                        )
                    )
                    permit.success(self._clock())
                    break
            finally:
                # Release the owned permit even if scheduler cleanup raises.
                if permit is not None:
                    permit.release()
                try:
                    self._scheduler.release(admission)
                except Exception:
                    raise GatewayInternalError(
                        "scheduler cleanup failed",
                        provider=route.provider,
                        model=route.model,
                        request_id=context.request_id,
                    ) from None
        # Only publish success after owned resources have been released.
        return self._succeed(request, route, result, context, metadata)

    async def _invoke(
        self,
        adapter: ProviderAdapter,
        request: LLMRequest,
        route: Route,
        *,
        budget: Any,
        request_id: str,
        attempt: int,
        timeout: float,
        clamped: bool,
    ) -> Any:
        """One provider call, bounded by ``timeout`` and by the cancel token.

        The call always runs as a task raced against the budget (and, when the
        caller supplied one, its cancellation token). Two properties matter and
        ``asyncio.wait_for`` alone does not give them:

        * an adapter that *swallows* ``CancelledError`` cannot hand back a late
          answer as if the cap had been kept — when the budget expires the
          attempt is a timeout, whatever the adapter does next; and
        * cleanup is bounded. A provider call that ignores cancellation is
          given a short grace to unwind, then abandoned (tracked and logged)
          rather than allowed to hang the caller's cancellation.

        ``asyncio.wait_for`` wraps its coroutine in a task internally anyway, so
        racing explicitly costs nothing on the hot path: when the task is already
        done, no grace wait and no extra bookkeeping happen at all.
        """

        if timeout <= 0:
            raise DeadlineExceededError(
                "no time budget remained for a provider attempt",
                provider=route.provider,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        coroutine = adapter.execute(
            request, route, budget=budget, request_id=request_id, attempt=attempt
        )
        provider_task: asyncio.Task[Any] = asyncio.ensure_future(coroutine)
        cancel_task = (
            _watch_cancel(request.cancellation) if request.cancellation is not None else None
        )
        pending: set[asyncio.Future[Any]] = {provider_task}
        if cancel_task is not None:
            pending.add(cancel_task)

        try:
            done, _ = await asyncio.wait(
                pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # Our own caller was cancelled. Stop the provider call and unwind
            # immediately — spending a grace period here would delay the very
            # cancellation we are honouring (LAW 5).
            if cancel_task is not None:
                cancel_task.cancel()
            provider_task.cancel()
            self._abandon((provider_task, cancel_task), adapter=adapter.name, request_id=request_id)
            raise

        budget_expired = provider_task not in done
        withdrawn = cancel_task is not None and cancel_task in done
        if cancel_task is not None:
            cancel_task.cancel()
        if not provider_task.done():
            provider_task.cancel()
        await self._settle(
            (provider_task, cancel_task),
            adapter=adapter.name,
            request_id=request_id,
            grace=_CANCEL_GRACE_SECONDS if budget_expired else _SETTLE_GRACE_SECONDS,
        )

        if withdrawn:
            raise CancelledByCallerError(
                "LLM request cancelled by caller during provider call",
                provider=route.provider,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        if budget_expired:
            # Reported even if the adapter produced an answer afterwards: the
            # budget is the promise, and a late answer is not a kept promise.
            raise self._timeout_error(route, request_id, attempt, clamped)

        task_exc = provider_task.exception()
        if task_exc is None:
            return provider_task.result()
        if _is_timeout(task_exc):
            raise self._timeout_error(route, request_id, attempt, clamped) from task_exc
        if isinstance(task_exc, asyncio.CancelledError):
            raise task_exc
        if not isinstance(task_exc, Exception):
            # KeyboardInterrupt / SystemExit are the interpreter talking, not a
            # provider failing; converting them would make Ctrl-C a no-op.
            raise task_exc
        if isinstance(task_exc, LLMError):
            raise task_exc
        raise GatewayInternalError(
            f"unexpected adapter failure: {type(task_exc).__name__}",
            provider=route.provider,
            model=route.model,
            request_id=request_id,
            attempt=attempt,
        ) from task_exc

    def _abandon(
        self,
        tasks: tuple[asyncio.Future[Any] | None, ...],
        *,
        adapter: str,
        request_id: str,
    ) -> None:
        """Track tasks we could not wait for, so ``aclose()`` can stop them.

        Bounded: a hostile adapter cannot make this list grow without limit.
        """

        for task in tasks:
            if task is None or task.done():
                continue
            self._abandoned.add(task)
            task.add_done_callback(self._abandoned.discard)
            task.add_done_callback(_observe_task_outcome)
            log.error(
                "llm_gateway_call_abandoned_during_cancellation",
                adapter=adapter,
                request_id=request_id,
                abandoned=len(self._abandoned),
            )

    async def _settle(
        self,
        tasks: tuple[asyncio.Future[Any] | None, ...],
        *,
        adapter: str,
        request_id: str,
        grace: float,
    ) -> None:
        """Await cancelled tasks for at most ``grace``; abandon what ignores us."""

        live = {task for task in tasks if task is not None and not task.done()}
        if not live:
            return
        if grace <= 0:
            self._abandon(tuple(live), adapter=adapter, request_id=request_id)
            return
        try:
            _, still_pending = await asyncio.wait(live, timeout=grace)
        except asyncio.CancelledError:
            # Cancellation can arrive *during* timeout cleanup, not just the
            # provider race. Keep ownership before propagating it unchanged.
            self._abandon(tuple(live), adapter=adapter, request_id=request_id)
            raise
        if still_pending:
            self._abandon(tuple(still_pending), adapter=adapter, request_id=request_id)

    @staticmethod
    def _timeout_error(route: Route, request_id: str, attempt: int, clamped: bool) -> LLMError:
        """Distinguish "the provider was slow" from "our whole budget ran out"."""

        if clamped:
            return DeadlineExceededError(
                f"total request budget expired during the attempt on {route.key}",
                provider=route.provider,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        return TransientProviderError(
            f"{route.key} did not answer within the per-attempt timeout",
            kind=LLMErrorKind.UPSTREAM_TIMEOUT,
            provider=route.provider,
            model=route.model,
            request_id=request_id,
            attempt=attempt,
        )

    async def _rate_gate(self, route: Route, request: LLMRequest, context: _AttemptContext) -> None:
        """Wait for local provider quota, or fail typed. Charges exactly one unit."""

        limiter = self._limiter(route.provider)
        if limiter is None:
            return
        policy_wait = self._policy.rate_limit.max_wait_seconds
        while True:
            self._check_cancellation(request, context)
            now = self._clock()
            wait = limiter.seconds_until_capacity(now)
            if wait == 0.0:
                if limiter.charge(now):
                    return
                continue
            if wait == float("inf") or wait > policy_wait:
                # Order matters: ``inf`` means the daily budget is gone, which
                # no deadline can fix. Comparing against the remaining budget
                # first would report QUOTA_EXHAUSTED as DEADLINE_EXCEEDED and
                # send the caller down the wrong recovery path (LAW 3).
                if wait == float("inf"):
                    # Daily budget: no amount of waiting inside this request can
                    # help, so this is exhaustion. QUOTA_EXHAUSTED is not
                    # retryable but IS fallback-eligible — another route can
                    # still serve the caller (LAW 8: policy decides, not chance).
                    limiter.record_throttled()
                    raise QuotaExhaustedError(
                        f"local {route.provider} daily request budget exhausted",
                        provider=route.provider,
                        model=route.model,
                        request_id=context.request_id,
                        detail="daily",
                    )
                # Per-minute budget with a wait policy refuses to serve: that is
                # a rate limit, not a deadline failure. The caller's remaining
                # budget is irrelevant to *why* this was refused, and reporting
                # DEADLINE_EXCEEDED here would tell an operator the request was
                # too slow when the truth is the window was full (LAW 3).
                limiter.record_throttled()
                raise RateLimitedError(
                    f"local {route.provider} rate limit: {wait:.2f}s of wait needed, "
                    f"policy allows {policy_wait:.2f}s",
                    provider=route.provider,
                    model=route.model,
                    request_id=context.request_id,
                    status_code=429,
                    detail="local_rate_limit",
                    retry_after=wait,
                )
            bounded = wait
            remaining = context.remaining(now)
            if remaining is not None:
                if remaining <= 0:
                    raise self._deadline_error(request, context, None)
                if wait > remaining:
                    # Policy would allow the wait, but the caller's deadline ends
                    # first: this *is* a deadline failure, and now it is the only
                    # thing that can be reported as one. Quota caused it, so it
                    # is counted as a throttle as well.
                    limiter.record_throttled()
                    raise self._deadline_error(request, context, None)
                bounded = min(wait, remaining)
            slept = await self._sleep(bounded, request.cancellation)
            context.rate_waited_seconds += slept
            context.rate_limited_locally = True
            if slept < bounded:
                # Cancelled while waiting for quota.
                raise CancelledByCallerError(
                    "cancelled while waiting for provider rate capacity",
                    provider=route.provider,
                    model=route.model,
                    request_id=context.request_id,
                )

    def _next_delay(
        self,
        error: LLMError,
        attempt_index: int,
        retry: Any,
        previous_delay: float | None,
    ) -> float | None:
        """Backoff for the next attempt, or ``None`` when the wait must be declined."""

        provider_wait = parse_retry_after(error.retry_after) if retry.respect_retry_after else None
        if provider_wait is not None and provider_wait > retry.max_retry_after_seconds:
            return None
        if provider_wait is not None and provider_wait > self._policy.timeout.max_backoff_seconds:
            return None
        computed = compute_backoff(
            attempt_index, retry, rng=self._rng, previous_delay=previous_delay
        )
        # ``max_delay_seconds`` is the ceiling on a wait *we* invented from a
        # jitter curve. A wait the provider explicitly asked for is bounded by
        # its own ceiling (``max_retry_after_seconds``, checked above) and by the
        # budget's single-sleep cap — clamping it with the jitter ceiling would
        # silently shorten an ask we claimed to honour (RFC 9110 §10.2.3).
        capped = min(computed, retry.max_delay_seconds, self._policy.timeout.max_backoff_seconds)
        if provider_wait is None:
            return capped
        return min(max(capped, provider_wait), self._policy.timeout.max_backoff_seconds)

    async def _backoff(self, delay: float, request: LLMRequest, context: _AttemptContext) -> bool:
        """Sleep before a retry. ``False`` when the deadline made the sleep impossible."""

        now = self._clock()
        remaining = context.remaining(now)
        if remaining is not None and delay >= remaining:
            # Do not silently shorten a wait the provider asked for, and do not
            # overrun the caller's deadline: stop the chain instead (LAW 7).
            return False
        slept = await self._sleep(delay, request.cancellation)
        context.backoff_seconds += slept
        if slept < delay:
            raise CancelledByCallerError(
                "cancelled during retry backoff", request_id=context.request_id
            )
        return True

    async def _sleep(self, delay: float, cancellation: Any | None) -> float:
        """Cancellation-aware sleep. Returns how long it actually slept."""

        if delay <= 0:
            return 0.0
        if cancellation is None:
            await asyncio.sleep(delay)
            return delay
        started = self._clock()
        watcher = _watch_cancel(cancellation)
        if watcher is None:
            await asyncio.sleep(delay)
            return delay
        try:
            # shield(): a timeout must cancel OUR wait, not the caller's event,
            # which other layers may still be watching.
            await asyncio.wait_for(asyncio.shield(watcher), timeout=delay)
        except BaseException as exc:
            if not _is_timeout(exc):
                raise
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        # Reaching here means either the token fired (short sleep) or the timer
        # elapsed (full sleep); the caller distinguishes them by the duration.
        return self._clock() - started

    def _check_cancellation(self, request: LLMRequest, context: _AttemptContext) -> None:
        """Raise the typed cancellation error if the caller withdrew the request."""

        cancellation = request.cancellation
        if cancellation is None:
            return
        is_set = getattr(cancellation, "is_set", None)
        if callable(is_set) and bool(is_set()):
            raise CancelledByCallerError(
                "LLM request cancelled by caller", request_id=context.request_id
            )

    def _deadline_error(
        self, request: LLMRequest, context: _AttemptContext, cause: LLMError | None
    ) -> LLMError:
        budget = (
            None if context.deadline_at is None else round(context.deadline_at - context.started, 3)
        )
        error = DeadlineExceededError(
            f"LLM request deadline of {budget}s expired before a provider answer",
            provider=request.provider,
            model=request.model,
            request_id=context.request_id,
            attempt=context.total_attempts,
        )
        if cause is not None:
            error.__cause__ = cause
        return error

    # ── terminal transitions ─────────────────────────────────────────
    def _succeed(
        self,
        request: LLMRequest,
        route: Route,
        result: Any,
        context: _AttemptContext,
        metadata: Mapping[str, str],
    ) -> LLMResponse:
        """Validate structured output, attach usage/cost, emit, and return."""

        text = result.text if isinstance(result.text, str) else ""
        structured: Any = None
        if request.output_validator is not None:
            try:
                structured = request.output_validator(text)
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 — a bad answer is a typed failure
                error = StructuredOutputInvalidError(
                    f"provider output violated the requested contract: {type(exc).__name__}",
                    provider=route.provider,
                    model=route.model,
                    request_id=context.request_id,
                    attempt=context.total_attempts,
                )
                raise error from exc

        usage: Usage = result.usage if isinstance(result.usage, Usage) else Usage()
        model = route.model
        if usage.reported_model:
            model = usage.reported_model
        usage = apply_cost(usage, model=model, table=self._prices)

        now = self._clock()
        policy_outcome = context.outcome(provider=route.provider, model=route.model)
        response = LLMResponse(
            request_id=context.request_id,
            text=text,
            provider=route.provider,
            model=route.model,
            operation=request.operation,
            caller=request.caller,
            purpose=request.purpose,
            usage=usage,
            finish_reason=result.finish_reason,
            policy=replace(policy_outcome, idempotency_hit=False),
            timings=context.timings(now),
            attempts=tuple(context.attempts),
            structured=structured,
            embedding=result.embedding,
        )
        self._executed += 1
        self._emit(
            RequestRecord(
                request_id=context.request_id,
                caller=request.caller,
                purpose=request.purpose,
                operation=request.operation,
                outcome=OUTCOME_DEGRADED if response.degraded else OUTCOME_SUCCESS,
                provider=route.provider,
                model=route.model,
                usage=usage,
                finish_reason=result.finish_reason,
                timings=response.timings,
                policy=response.policy,
                attempts=response.attempts,
                started_at=context.started,
                ended_at=now,
                prompt_chars=request.prompt_size_chars(),
                payload_bytes=request.payload_bytes(),
                metadata=metadata,
                idempotency_key=request.idempotency_key,
            )
        )
        return response

    def _reuse_response(
        self,
        request: LLMRequest,
        original: LLMResponse,
        context: _AttemptContext,
        metadata: Mapping[str, str],
    ) -> LLMResponse:
        """One logical caller, one trace; result reuse does not spend tokens again."""
        now = self._clock()
        outcome = (
            replace(original.policy, idempotency_hit=True, attempts=0, retries=0)
            if (original.policy is not None)
            else PolicyOutcome(
                route_provider=original.provider, route_model=original.model, idempotency_hit=True
            )
        )
        response = replace(
            original,
            request_id=context.request_id,
            caller=request.caller,
            policy=outcome,
            timings=context.timings(now),
            attempts=(),
            usage=Usage(),
        )
        self._executed += 1
        self._emit(
            RequestRecord(
                request_id=context.request_id,
                caller=request.caller,
                purpose=request.purpose,
                operation=request.operation,
                outcome=OUTCOME_DEGRADED if response.degraded else OUTCOME_SUCCESS,
                provider=response.provider,
                model=response.model,
                usage=response.usage,
                finish_reason=response.finish_reason,
                timings=response.timings,
                policy=outcome,
                started_at=context.started,
                ended_at=now,
                prompt_chars=request.prompt_size_chars(),
                payload_bytes=request.payload_bytes(),
                metadata=metadata,
                idempotency_key=request.idempotency_key,
            )
        )
        return response

    def _fail(
        self,
        request: LLMRequest,
        context: _AttemptContext,
        error: BaseException,
        *,
        outcome: str,
        metadata: Mapping[str, str],
    ) -> None:
        """Emit exactly one failure record. Never raises into the request path."""

        now = self._clock()
        provider = request.provider
        model = request.model
        if context.attempts:
            provider = context.attempts[-1].provider
            model = context.attempts[-1].model
        policy_outcome = context.outcome(provider=provider, model=model)
        record = build_error_record(
            request_id=context.request_id,
            caller=request.caller,
            purpose=request.purpose,
            operation=request.operation,
            outcome=outcome,
            error=error,
            provider=provider,
            model=model,
            timings=context.timings(now),
            policy=policy_outcome if context.total_attempts or outcome == OUTCOME_REFUSED else None,
            attempts=tuple(context.attempts),
            started_at=context.started,
            prompt_chars=request.prompt_size_chars(),
            payload_bytes=request.payload_bytes(),
            metadata=metadata,
            idempotency_key=request.idempotency_key,
        )
        self._executed += 1
        self._emit(record)

    def _emit(self, record: RequestRecord) -> None:
        self._metrics.observe(record)
        for sink in self._sinks:
            try:
                sink.emit(record)
            except Exception:  # noqa: BLE001 — telemetry must never break a request
                log.error("llm_gateway_sink_failed", sink=type(sink).__name__)


def _scoped_idempotency_key(request: LLMRequest) -> str:
    """The cache key for a caller-declared idempotency key.

    A bare caller-supplied string is not safe to key a *result* on: two tenants
    that both send ``idempotency_key="summarize"`` would share one answer, and a
    caller that reuses a key for a different prompt would be handed a stale one.
    Both are answer leaks, so the key is scoped to the payload it was minted for
    — tenant, operation, pin, and a digest of the prompt/system/messages.

    The digest is one-way and bounded: it never stores prompt content, only a
    fingerprint of it (LAW 6, LAW 10).
    """

    digest = hashlib.sha256()

    def add(value: bytes) -> None:
        # Length framing prevents delimiter injection and preserves boundaries.
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    def text(value: object) -> None:
        add(str(value).encode("utf-8", "replace"))

    def part(value: ContentPart) -> None:
        text(value.mime_type)
        add(value.data)
        text(value.text)

    for value in (
        request.idempotency_key,
        request.caller.tenant_id,
        request.caller.label,
        request.operation.value,
        request.provider,
        request.model,
        request.purpose,
        request.prompt,
        request.system,
        request.generation,
        request.allow_fallback,
        id(request.output_validator) if request.output_validator else None,
    ):
        text(value)
    text(len(request.messages))
    for message in request.messages:
        text(message.role)
        text(message.content)
        text(len(message.parts))
        for message_part in message.parts:
            part(message_part)
    text(len(request.parts))
    for request_part in request.parts:
        part(request_part)
    return digest.hexdigest()


def _observe_task_outcome(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()  # retrieve, never log provider-owned exception content


def _outcome_for(error: LLMError) -> str:
    """Map a terminal typed error onto a bounded outcome label."""

    if error.kind is LLMErrorKind.CANCELLED:
        return OUTCOME_CANCELLED
    if error.kind is LLMErrorKind.DEADLINE_EXCEEDED:
        return OUTCOME_DEADLINE
    if error.kind is LLMErrorKind.OVERLOADED:
        return OUTCOME_OVERLOADED
    if error.kind is LLMErrorKind.GATEWAY_CLOSED:
        return OUTCOME_CLOSED
    if error.kind is LLMErrorKind.POLICY_REFUSAL:
        return OUTCOME_REFUSED
    return OUTCOME_ERROR


def _watch_cancel(cancellation: Any) -> asyncio.Future[Any] | None:
    """Wrap an external cancellation signal as a Future, or ``None`` if unusable.

    Accepts anything with an awaitable ``wait()`` (``asyncio.Event``) or an
    awaitable object. An unsupported shape is *ignored* rather than guessed at:
    turning a caller bug into a spurious cancellation would be worse than
    dropping the token, and the gateway reports the mismatch once through its
    internal-error log channel.
    """

    if cancellation is None:
        return None
    wait = getattr(cancellation, "wait", None)
    if callable(wait):
        try:
            result = wait()
        except Exception:  # noqa: BLE001 — a broken token must not break a request
            log.error("llm_gateway_cancel_token_failed", token=type(cancellation).__name__)
            return None
        if asyncio.isfuture(result):
            return asyncio.ensure_future(result)
        if isinstance(result, Awaitable):
            return asyncio.ensure_future(_await_one(result))
        return None
    if isinstance(cancellation, Awaitable):
        return asyncio.ensure_future(_await_one(cancellation))
    log.error("llm_gateway_unsupported_cancel_token", token=type(cancellation).__name__)
    return None


async def _await_one(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


class GatewayBuilder:
    """Fluent, dependency-free construction helper — mostly for tests and CLI.

    ``build_gateway_from_settings`` in :mod:`nexus_ai_agent.llm.gateway.registry`
    is the production path; this exists so a test can assemble a gateway with a
    fake adapter and an injected clock in three lines instead of thirty.
    """

    def __init__(self, policy: GatewayPolicy | None = None) -> None:
        from nexus_ai_agent.llm.gateway.policy import default_policy

        self._policy = policy or default_policy()
        self._adapters: dict[str, ProviderAdapter] = {}
        self._routes: list[Route] = []
        self._clock: Clock = MONOTONIC
        self._sinks: list[ObservationSink] = []
        self._scheduler: SchedulerPort | None = None
        self._rng: random.Random | None = None

    def with_clock(self, clock: Clock) -> GatewayBuilder:
        self._clock = clock
        return self

    def with_rng(self, rng: random.Random) -> GatewayBuilder:
        self._rng = rng
        return self

    def with_sink(self, sink: ObservationSink) -> GatewayBuilder:
        self._sinks.append(sink)
        return self

    def with_scheduler(self, scheduler: SchedulerPort) -> GatewayBuilder:
        self._scheduler = scheduler
        return self

    def with_policy(self, **overrides: Any) -> GatewayBuilder:
        from dataclasses import replace as _replace

        self._policy = _replace(self._policy, **overrides)
        return self

    def with_adapter(
        self, adapter: ProviderAdapter, routes: Sequence[Route] | None = None
    ) -> GatewayBuilder:
        self._adapters[adapter.name] = adapter
        if routes:
            self._routes.extend(routes)
        else:
            self._routes.append(
                Route(
                    provider=adapter.name,
                    model=adapter.name,
                    operations=adapter.operations,
                    modalities=adapter.modalities,
                )
            )
        return self

    def build(self) -> LLMGateway:
        policy = self._policy
        if self._routes:
            keys = {r.key for r in policy.routes}
            merged = list(policy.routes) + [r for r in self._routes if r.key not in keys]
            policy = replace(policy, routes=tuple(merged))
        gateway = LLMGateway(
            policy=policy,
            adapters=self._adapters,
            scheduler=self._scheduler or BoundedScheduler(policy.concurrency, clock=self._clock),
            clock=self._clock,
            sinks=self._sinks,
            rng=self._rng,
        )
        for name, adapter in self._adapters.items():
            routes = [r for r in policy.routes if r.provider == name]
            gateway.register(adapter, routes or None)
        return gateway
