#!/usr/bin/env python3
"""Mutation probes for the W2 Global LLM Gateway Authority.

Every mutation weakens exactly one law the gateway claims to keep — a retry
decision, a cancellation check, a bulkhead, a privacy filter, a redaction pass,
a usage provenance rule — and each one must be *killed* by a named behavioural
test. A surviving mutant is a law the test suite does not actually enforce, so
the harness exits non-zero and names the survivor.

The harness copies ``src/nexus_ai_agent`` to a temporary directory, mutates the
copy, runs the matching test against that copy, and restores it. It never edits
the working tree. A green baseline, every mutant killed, and a green restored
copy are all required for exit status 0.

Usage::

    python scripts/llm_gateway_mutations.py
    python scripts/llm_gateway_mutations.py --list
    python scripts/llm_gateway_mutations.py --only engine_retry_ignores_kind
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "nexus_ai_agent"
GATEWAY = Path("llm") / "gateway"

ENGINE = "tests/unit/test_llm_gateway_engine.py"
POLICY = "tests/unit/test_llm_gateway_policy.py"
SCHEDULER = "tests/unit/test_llm_gateway_scheduler.py"
RESILIENCE = "tests/unit/test_llm_gateway_resilience.py"
ADAPTERS = "tests/unit/test_llm_gateway_adapters.py"
OBSERVABILITY = "tests/unit/test_llm_gateway_observability.py"
CANCELLATION = "tests/unit/test_llm_gateway_cancellation.py"
ADVERSARIAL = "tests/unit/test_llm_gateway_adversarial.py"
LOAD = "tests/unit/test_llm_gateway_load.py"
SECURITY = "tests/unit/test_llm_gateway_security.py"
FACADE = "tests/unit/test_llm_gateway_facade.py"
REGISTRY_RACE = "tests/unit/test_llm_gateway_registry_race.py"

#: Every gateway test file: the baseline and the restored copy must be green.
ALL_TESTS: tuple[str, ...] = (
    "tests/unit/test_llm_gateway_errors.py",
    "tests/unit/test_llm_gateway_contract.py",
    POLICY,
    RESILIENCE,
    SCHEDULER,
    ADAPTERS,
    ENGINE,
    OBSERVABILITY,
    CANCELLATION,
    ADVERSARIAL,
    LOAD,
    SECURITY,
    FACADE,
    REGISTRY_RACE,
)


@dataclass(frozen=True)
class Mutation:
    """One weakened invariant, the test that must notice, and the law it breaks."""

    name: str
    relative: Path
    old: str
    new: str
    test: str
    test_id: str
    invariant: str


MUTATIONS: tuple[Mutation, ...] = (
    # ── LAW 7: a retry has a reason and a bound ────────────────────────────
    Mutation(
        "engine_retry_ignores_the_error_kind",
        GATEWAY / "engine.py",
        "                    if not retry.allows(stamped.kind) or attempt_index + 1 "
        ">= retry.max_attempts:",
        "                    if attempt_index + 1 >= retry.max_attempts:",
        ENGINE,
        "test_a_non_retryable_failure_costs_exactly_one_attempt",
        "a non-retryable kind (400, auth, blocked) must cost exactly one attempt",
    ),
    Mutation(
        "engine_backoff_ignores_the_caller_deadline",
        GATEWAY / "engine.py",
        """        now = self._clock()
        remaining = context.remaining(now)
        if remaining is not None and delay >= remaining:
            # Do not silently shorten a wait the provider asked for, and do not
            # overrun the caller's deadline: stop the chain instead (LAW 7).
            return False""",
        """        now = self._clock()
        remaining = context.remaining(now)
        if False:
            return False""",
        ENGINE,
        "test_a_backoff_that_would_outlive_the_budget_is_declined_not_slept",
        "a backoff that cannot fit in the remaining budget is declined, never slept",
    ),
    Mutation(
        "engine_serves_a_retry_after_it_refused",
        GATEWAY / "engine.py",
        """        if provider_wait is not None and provider_wait > retry.max_retry_after_seconds:
            return None""",
        """        if provider_wait is not None and provider_wait > retry.max_retry_after_seconds:
            return retry.max_delay_seconds""",
        ENGINE,
        "test_a_retry_after_beyond_policy_is_declined_not_served",
        "a Retry-After beyond policy must be declined, not truncated and served",
    ),
    Mutation(
        "engine_backoff_is_not_jittered_or_bounded",
        GATEWAY / "engine.py",
        "        capped = min(computed, retry.max_delay_seconds, "
        "self._policy.timeout.max_backoff_seconds)",
        "        capped = computed",
        ADVERSARIAL,
        "test_a_lying_rng_cannot_push_a_backoff_past_the_policy_ceiling",
        "a backoff can never exceed the policy ceiling, whatever the rng returns",
    ),
    # ── LAW 8: fallback is policy-driven and visible ───────────────────────
    Mutation(
        "engine_falls_back_on_a_blocked_answer",
        GATEWAY / "engine.py",
        "                if not exc.fallback_eligible or "
        "not self._policy.fallback.allows(exc.kind):",
        "                if not self._policy.fallback.allows(exc.kind):",
        ENGINE,
        "test_a_misconfigured_fallback_policy_cannot_launder_a_content_block",
        "a typed error's own veto outranks fallback configuration",
    ),
    Mutation(
        "policy_ignores_the_caller_fallback_veto",
        GATEWAY / "policy.py",
        "    if request.allow_fallback and policy.fallback.enabled "
        "and policy.fallback.max_hops > 0:",
        "    if policy.fallback.enabled and policy.fallback.max_hops > 0:",
        ENGINE,
        "test_a_caller_veto_on_fallback_yields_the_typed_failure",
        "a caller that vetoed fallback must receive the typed failure",
    ),
    Mutation(
        "engine_hides_that_a_degraded_route_answered",
        GATEWAY / "engine.py",
        "            degraded=self.fallback_used or self.served_by_degraded_route,",
        "            degraded=self.fallback_used,",
        FACADE,
        "test_the_typed_path_reports_degradation_as_a_flag_not_as_text",
        "an answer from a degraded route is degraded even with no hop",
    ),
    Mutation(
        "facade_drops_the_degraded_disclaimer",
        GATEWAY / "facade.py",
        "        if response.degraded and self.degraded_disclaimer:",
        "        if False and self.degraded_disclaimer:",
        FACADE,
        "test_a_degraded_answer_carries_the_verbatim_disclaimer",
        "a degraded answer must carry the verbatim disclaimer",
    ),
    # ── LAW 5: cancellation is sacred ──────────────────────────────────────
    Mutation(
        "engine_stops_checking_the_cancellation_token",
        GATEWAY / "engine.py",
        """            now = self._clock()
            self._check_cancellation(request, context)
            if self._closed:""",
        """            now = self._clock()
            if self._closed:""",
        CANCELLATION,
        "test_a_withdrawal_token_set_before_execution_never_reaches_a_provider",
        "a withdrawn request must never reach a provider",
    ),
    Mutation(
        "engine_ignores_a_withdrawal_during_backoff",
        GATEWAY / "engine.py",
        "        slept = await self._sleep(delay, request.cancellation)",
        "        slept = await self._sleep(delay, None)",
        CANCELLATION,
        "test_a_withdrawal_during_backoff_stops_the_retry_chain",
        "a backoff sleep is cancellation-aware: withdrawal lands during it, not after",
    ),
    Mutation(
        "engine_keeps_retrying_after_shutdown",
        GATEWAY / "engine.py",
        """            if self._closed:
                # Shutdown stops the chain at the next boundary: the adapters'
                # transport pools are being released, so another attempt would
                # fail against a closed client and report a transport error
                # instead of the truth (LAW 5: stopping means stopping).
                raise GatewayClosedError(""",
        """            if False:
                raise GatewayClosedError(""",
        CANCELLATION,
        "test_closing_the_gateway_stops_an_inflight_retry_chain",
        "shutdown must stop an in-flight retry chain at the next boundary",
    ),
    Mutation(
        "engine_accepts_a_late_answer_past_the_attempt_cap",
        GATEWAY / "engine.py",
        """        if budget_expired:
            # Reported even if the adapter produced an answer afterwards: the
            # budget is the promise, and a late answer is not a kept promise.
            raise self._timeout_error(route, request_id, attempt, clamped)""",
        """        if budget_expired and provider_task.exception() is not None:
            raise self._timeout_error(route, request_id, attempt, clamped)""",
        ADVERSARIAL,
        "test_an_adapter_that_swallows_cancellation_cannot_overrun_the_budget",
        "an adapter that ignores cancellation cannot beat the attempt budget",
    ),
    Mutation(
        "engine_swallows_interpreter_signals",
        GATEWAY / "engine.py",
        """        if not isinstance(task_exc, Exception):
            # KeyboardInterrupt / SystemExit are the interpreter talking, not a
            # provider failing; converting them would make Ctrl-C a no-op.
            raise task_exc""",
        """        if False:
            raise task_exc""",
        ADVERSARIAL,
        "test_a_signal_carried_by_the_provider_future_is_never_converted",
        "a BaseException from the provider is re-raised, never wrapped as a gateway failure",
    ),
    Mutation(
        "scheduler_does_not_count_a_cancelled_waiter",
        GATEWAY / "scheduler.py",
        """        except asyncio.CancelledError:
            # The caller's *task* went away while queued. Count it here too: an
            # operator reading ``cancelled`` must see every abandonment, whether
            # it was signalled through a token or through the event loop. The
            # slot bookkeeping already happened in ``_wait_for_admission``.
            self._cancelled += 1
            raise""",
        """        except asyncio.CancelledError:
            raise""",
        SCHEDULER,
        "test_a_task_cancelled_while_queued_is_counted_as_a_cancellation",
        "a task cancelled while queued must be counted as a cancellation",
    ),
    # ── LAW 6: bounded everything ──────────────────────────────────────────
    Mutation(
        "scheduler_ignores_the_per_provider_bulkhead",
        GATEWAY / "scheduler.py",
        (
            "        if self._provider_inflight.get(provider, 0) "
            ">= self._policy.max_inflight_per_provider:\n            return False"
        ),
        """        if False:
            return False""",
        LOAD,
        "test_the_per_provider_bound_holds_across_two_providers",
        "the per-provider bulkhead must hold under load",
    ),
    Mutation(
        "scheduler_grows_the_queue_instead_of_shedding",
        GATEWAY / "scheduler.py",
        """            if self._policy.overload_behavior is OverloadBehavior.REJECT:
                self._rejected += 1
                raise OverloadedError(
                    "LLM gateway queue is full; request shed to protect the process",""",
        """            if False:
                self._rejected += 1
                raise OverloadedError(
                    "LLM gateway queue is full; request shed to protect the process",""",
        ENGINE,
        "test_saturation_sheds_with_a_typed_overload_instead_of_growing",
        "saturation must shed typed load, never grow the queue without bound",
    ),
    Mutation(
        "scheduler_strands_a_waiter_beside_idle_capacity",
        GATEWAY / "scheduler.py",
        """        # this way cannot jump ahead of anybody already waiting in it.
        self._pump()
        try:""",
        """        # this way cannot jump ahead of anybody already waiting in it.
        try:""",
        SCHEDULER,
        "test_a_waiter_joining_an_empty_queue_is_not_stranded_beside_idle_capacity",
        "a waiter is granted free capacity instead of stranding until its budget ends",
    ),
    Mutation(
        "scheduler_accepts_work_after_close",
        GATEWAY / "scheduler.py",
        """        if self._closed:
            raise GatewayClosedError("LLM gateway scheduler is closed")""",
        """        if False:
            raise GatewayClosedError("LLM gateway scheduler is closed")""",
        SCHEDULER,
        "test_a_closed_scheduler_refuses_new_work_with_a_typed_error",
        "a closed scheduler refuses new admissions with a typed error",
    ),
    Mutation(
        "scheduler_gives_up_after_one_queue_slot_wakeup",
        GATEWAY / "scheduler.py",
        """            while len(self._waiters) >= self._policy.max_queued:
                await self._await_queue_slot(remaining, cancellation)""",
        """            for _ in range(1):
                await self._await_queue_slot(remaining, cancellation)""",
        SCHEDULER,
        "test_a_thundering_herd_wakeup_never_overfills_the_bounded_queue",
        "a herd wakeup cannot push the bounded queue past its bound",
    ),
    Mutation(
        "scheduler_leaks_the_slot_of_a_withdrawn_waiter",
        GATEWAY / "scheduler.py",
        """        try:
            await self._await_grant(waiter, remaining, cancellation)
        except BaseException:
            self._withdraw(waiter)
            raise""",
        """        try:
            await self._await_grant(waiter, remaining, cancellation)
        except BaseException:
            raise""",
        SCHEDULER,
        "test_a_grant_that_lands_during_cancellation_is_released_not_leaked",
        "a waiter that gives up must release its queue slot and any grant",
    ),
    Mutation(
        "engine_idempotency_cache_is_unbounded",
        GATEWAY / "engine.py",
        """                self._completed.move_to_end(key)
                while len(self._completed) > self._idempotency_capacity:
                    self._completed.popitem(last=False)""",
        """                self._completed.move_to_end(key)""",
        ENGINE,
        "test_the_idempotency_cache_is_bounded",
        "the idempotency cache must stay within its declared capacity",
    ),
    Mutation(
        "engine_shares_cached_answers_across_tenants",
        GATEWAY / "engine.py",
        "        key = _scoped_idempotency_key(request)",
        '        key = request.idempotency_key or ""',
        ADVERSARIAL,
        "test_the_same_key_from_two_tenants_does_not_share_an_answer",
        "one tenant's cached answer must never be served to another",
    ),
    # ── LAW 4: policy is decided in one place ──────────────────────────────
    Mutation(
        "policy_skips_the_privacy_filter_at_selection",
        GATEWAY / "policy.py",
        "    allowed = [route for route in policy.routes if not policy.privacy.forbids(route)]",
        "    allowed = list(policy.routes)",
        ENGINE,
        "test_strict_privacy_refuses_before_any_egress",
        "strict privacy must remove a route before any request is planned",
    ),
    Mutation(
        "policy_substitutes_a_route_for_an_unmatched_pin",
        GATEWAY / "policy.py",
        """        if not matching:
            return ()""",
        """        if not matching:
            return tuple(allowed)[:1]""",
        POLICY,
        "test_a_pin_that_matches_nothing_says_what_is_registered",
        "an unmatched provider/model pin must be refused, not silently substituted",
    ),
    Mutation(
        "policy_lets_an_incapable_route_serve_the_request",
        GATEWAY / "policy.py",
        """    def capable(route: Route) -> bool:
        if not policy.fallback.require_capability_match:
            return True
        return route.serves(request)

    allowed = [route for route in policy.routes if not policy.privacy.forbids(route)]""",
        """    def capable(route: Route) -> bool:
        return True

    allowed = [route for route in policy.routes if not policy.privacy.forbids(route)]""",
        POLICY,
        "test_a_pin_to_a_route_that_cannot_serve_the_request_is_refused_not_substituted",
        "capability must be checked before a route is allowed to serve",
    ),
    Mutation(
        "engine_skips_the_context_gate",
        GATEWAY / "engine.py",
        """        size = request.prompt_size_chars()
        for route in current_plan.routes:""",
        """        size = request.prompt_size_chars()
        for route in ():""",
        ENGINE,
        "test_a_route_context_gate_refuses_an_oversized_payload_before_sending_it",
        "an oversized payload must be refused before it is sent",
    ),
    Mutation(
        "engine_does_not_charge_local_quota",
        GATEWAY / "engine.py",
        """            if wait == 0.0:
                if limiter.charge(now):
                    return
                continue""",
        """            if wait == 0.0:
                return""",
        ENGINE,
        "test_the_local_minute_window_serves_a_burst_then_throttles_typed",
        "the local rate window must actually be charged and enforced",
    ),
    Mutation(
        "engine_reports_a_drained_daily_budget_as_a_deadline",
        GATEWAY / "engine.py",
        """            if wait == float("inf") or wait > policy_wait:
                # Order matters: ``inf`` means the daily budget is gone, which""",
        """            if wait == float("inf") or wait > policy_wait:
                remaining = context.remaining(now)
                if remaining is not None and wait > remaining:
                    raise self._deadline_error(request, context, None)
                # Order matters: ``inf`` means the daily budget is gone, which""",
        ENGINE,
        "test_a_drained_daily_budget_is_exhaustion_not_a_throttle",
        "an exhausted daily budget is QUOTA_EXHAUSTED, not DEADLINE_EXCEEDED",
    ),
    Mutation(
        "engine_reports_a_policy_refusal_to_wait_as_a_deadline",
        GATEWAY / "engine.py",
        """                # Per-minute budget with a wait policy refuses to serve: that is
                # a rate limit, not a deadline failure.""",
        """                remaining = context.remaining(now)
                if remaining is not None and wait > remaining:
                    raise self._deadline_error(request, context, None)
                # Per-minute budget with a wait policy refuses to serve: that is
                # a rate limit, not a deadline failure.""",
        LOAD,
        "test_a_rate_limit_refusal_stays_a_rate_limit_under_a_short_deadline",
        "a full local window is RATE_LIMITED, not DEADLINE_EXCEEDED",
    ),
    # ── LAW 3: typed truth, no substring classification ────────────────────
    Mutation(
        "adapters_scan_the_error_message_for_keywords",
        GATEWAY / "adapters.py",
        '    status = error.get("status")',
        "    status = (\n"
        '        "RESOURCE_EXHAUSTED" if "rate limit" in str(error).lower()\n'
        '        else error.get("status")\n'
        "    )",
        ADAPTERS,
        "test_a_message_that_mentions_a_different_status_cannot_change_the_classification",
        "classification never scans free text: the banned substring shape",
    ),
    Mutation(
        "adapters_classify_by_message_substring",
        GATEWAY / "adapters.py",
        '    status = error.get("status")',
        '    status = error.get("message")',
        ADAPTERS,
        "test_a_message_that_mentions_a_different_status_cannot_change_the_classification",
        "classification reads declared fields, never message substrings",
    ),
    Mutation(
        "adapters_treat_a_200_rate_limit_message_as_a_failure",
        GATEWAY / "adapters.py",
        "        return AdapterResult(",
        """        if 'rate limit' in str(payload).lower():
            raise RateLimitedError('rate limit')
        return AdapterResult(""",
        ADAPTERS,
        "test_a_200_whose_body_says_rate_limit_is_still_a_success",
        "a successful 200 body is a success, whatever words it contains",
    ),
    Mutation(
        "adapters_invent_usage_the_provider_did_not_report",
        GATEWAY / "adapters.py",
        """    if not isinstance(metadata, dict):
        return UNKNOWN_USAGE""",
        """    if not isinstance(metadata, dict):
        return Usage(
            source=UsageSource.PROVIDER, input_tokens=0, output_tokens=0, total_tokens=0
        )""",
        ADAPTERS,
        "test_absent_usage_metadata_is_unknown_not_zero",
        "absent usage is UNKNOWN, never a fabricated zero",
    ),
    Mutation(
        "adapters_put_the_key_in_the_url",
        GATEWAY / "adapters.py",
        '        url = f"{self._base_url}/models/{model}:generateContent"',
        '        url = f"{self._base_url}/models/{model}:generateContent?key={self._api_key}"',
        SECURITY,
        "test_the_key_travels_in_a_header_and_never_in_the_url",
        "the credential must ride in a header, never in a URL",
    ),
    Mutation(
        "adapters_allow_a_cleartext_remote_endpoint",
        GATEWAY / "adapters.py",
        """    if parsed.scheme == "http" and parsed.host.lower() not in _LOOPBACK_HOSTS:""",
        """    if False:""",
        SECURITY,
        "test_a_cleartext_endpoint_for_a_remote_host_is_refused",
        "a non-loopback http endpoint would broadcast the key and must be refused",
    ),
    Mutation(
        "adapters_drop_the_blocking_finish_reason",
        GATEWAY / "adapters.py",
        "        if finish is not None and finish.upper() in _BLOCKING_FINISH_REASONS:",
        "        if False:",
        ADAPTERS,
        "test_a_blocking_finish_reason_is_content_blocked",
        "a safety block must be typed CONTENT_BLOCKED, not an empty success",
    ),
    # ── LAW 10/11: observability and truthful accounting ───────────────────
    Mutation(
        "observability_copies_the_provider_message_into_the_record",
        GATEWAY / "observability.py",
        """    return RequestRecord(
        request_id=request_id,
        caller=caller,
        purpose=purpose,
        operation=operation,
        outcome=outcome,
        provider=provider,
        model=model,
        error_kind=kind,""",
        """    if isinstance(error, LLMError):
        provider = f"{provider} {error}"[:4000]
    return RequestRecord(
        request_id=request_id,
        caller=caller,
        purpose=purpose,
        operation=operation,
        outcome=outcome,
        provider=provider,
        model=model,
        error_kind=kind,""",
        OBSERVABILITY,
        "test_a_provider_message_carrying_the_prompt_never_reaches_the_record",
        "a provider message (which can echo the prompt) must never be persisted",
    ),
    Mutation(
        "observability_stops_sanitizing_metadata",
        GATEWAY / "observability.py",
        "        metadata=sanitize_metadata(metadata),",
        "        metadata=dict(metadata or {}),",
        OBSERVABILITY,
        "test_the_record_builder_itself_sanitizes_caller_metadata",
        "the record builder sanitizes metadata at the call site, not only in the helper",
    ),
    Mutation(
        "observability_reports_unknown_usage_as_zero_tokens",
        GATEWAY / "observability.py",
        """        else:
            # LAW 11: say "unknown" explicitly instead of omitting the field and
            # letting a dashboard render it as zero.
            data["usage"] = "unknown\"""",
        """        else:
            data["usage_input_tokens"] = 0
            data["usage_output_tokens"] = 0""",
        OBSERVABILITY,
        "test_unknown_usage_is_declared_explicitly_in_the_projection",
        "unknown usage must be declared, not rendered as zero",
    ),
    Mutation(
        "usage_invents_a_price_for_an_unpriced_model",
        GATEWAY / "usage.py",
        """    price = price_for(model, table=table)
    if price is None or not price.is_known:
        return usage""",
        """    price = price_for(model, table=table) or ModelPrice(0.0, 0.0)
    if not price.is_known:
        return usage""",
        OBSERVABILITY,
        "test_an_unpriced_model_leaves_the_cost_unknown",
        "a model with no pinned price has an unknown cost, not a zero one",
    ),
    Mutation(
        "usage_costs_tokens_the_provider_never_reported",
        GATEWAY / "usage.py",
        """    if usage.source is not UsageSource.PROVIDER:
        return usage""",
        """    if False:
        return usage""",
        OBSERVABILITY,
        "test_usage_the_provider_did_not_report_is_never_costed",
        "only provider-reported usage may be costed",
    ),
    Mutation(
        "engine_skips_the_failure_record",
        GATEWAY / "engine.py",
        """        record = build_error_record(
            request_id=context.request_id,""",
        """        return
        record = build_error_record(
            request_id=context.request_id,""",
        ENGINE,
        "test_a_failed_request_is_recorded_too",
        "a failed request must still produce exactly one record",
    ),
    # ── LAW 3: resilience primitives ───────────────────────────────────────
    Mutation(
        "resilience_allows_a_negative_backoff",
        GATEWAY / "resilience.py",
        """    # An injected rng is a seam (tests, future pluggable strategies) and a draw
    # outside [0, 1] would produce a negative sleep. A delay is a duration: it
    # is clamped here so no caller can ever schedule one in the past.
    return max(0.0, draw)""",
        """    return draw""",
        ADVERSARIAL,
        "test_a_negative_rng_draw_cannot_produce_a_negative_sleep",
        "a backoff delay can never be negative",
    ),
    Mutation(
        "resilience_accepts_a_negative_retry_after",
        GATEWAY / "resilience.py",
        """    if not math.isfinite(seconds) or seconds < 0.0:
        return None""",
        """    if False:
        return None""",
        RESILIENCE,
        "test_retry_after_returns_none_for_anything_unusable",
        "a non-finite or negative Retry-After must be rejected, not honoured",
    ),
    Mutation(
        "resilience_opens_the_breaker_on_a_content_block",
        GATEWAY / "resilience.py",
        """        if not self.policy.enabled or not self.policy.counts(kind):
            return""",
        """        if not self.policy.enabled:
            return""",
        ENGINE,
        "test_a_content_block_does_not_open_the_circuit",
        "our caller's content is not evidence about provider health",
    ),
    Mutation(
        "resilience_breaker_never_recovers",
        GATEWAY / "resilience.py",
        """        if moment - self._opened_at < self.policy.recovery_seconds:
            return CircuitState.OPEN
        return CircuitState.HALF_OPEN""",
        """        return CircuitState.OPEN""",
        ENGINE,
        "test_a_half_open_probe_lets_the_route_recover",
        "a cooled-down breaker must allow a half-open probe",
    ),
    # ── LAW 1/12: one authority, compatible surface ────────────────────────
    Mutation(
        "registry_builds_the_process_authority_without_taking_the_lock",
        GATEWAY / "registry.py",
        """    with _AUTHORITY_LOCK:
        current = _gateway""",
        """    if True:
        current = _gateway""",
        REGISTRY_RACE,
        "test_concurrent_first_use_builds_exactly_one_process_authority",
        "one process has one authority even when threads reach first use together",
    ),
    Mutation(
        "registry_resolves_a_credential_without_taking_the_lock",
        GATEWAY / "registry.py",
        """    with _CREDENTIAL_LOCK:
        existing = _CREDENTIAL_GATEWAYS.get(cache_key)""",
        """    if True:
        existing = _CREDENTIAL_GATEWAYS.get(cache_key)""",
        REGISTRY_RACE,
        "test_concurrent_scoped_credential_resolution_builds_exactly_one_gateway",
        "one credential set resolves to one gateway, so its bounds are not split",
    ),
    Mutation(
        "engine_accepts_work_after_close",
        GATEWAY / "engine.py",
        """        if self._closed:
            raise GatewayClosedError(""",
        """        if False:
            raise GatewayClosedError(""",
        ENGINE,
        "test_a_closed_gateway_refuses_new_work_typed",
        "a closed authority must refuse new work with a typed error",
    ),
    Mutation(
        "engine_does_not_validate_the_adapter_result_type",
        GATEWAY / "engine.py",
        "                    if not isinstance(result, AdapterResult):",
        "                    if False:",
        ENGINE,
        "test_an_adapter_returning_a_non_result_is_a_gateway_internal_error",
        "the adapter boundary is typed: a wrong return is a gateway error",
    ),
    Mutation(
        "facade_renders_provider_detail_to_a_chat_surface",
        GATEWAY / "facade.py",
        """        if exc.kind in _BUSY_KINDS:
            return "⏳ The AI service is busy right now. Please try again in a moment."
        return UNAVAILABLE_MESSAGE""",
        """        return f"{exc.kind.value}: {exc}"[:400]""",
        FACADE,
        "test_a_rendered_failure_never_leaks_provider_detail_to_a_chat_surface",
        "a user-facing failure message must not carry provider detail",
    ),
)


def _run_pytest(
    package_root: Path, targets: tuple[str, ...] | str, timeout: int
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    python_paths = [str(package_root), str(ROOT / "src")]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    if isinstance(targets, str):
        targets = (targets,)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "-o",
            "asyncio_mode=auto",
            *targets,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _tail(output: str, lines: int = 10) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


def _is_test_failure(result: subprocess.CompletedProcess[str]) -> bool:
    output = result.stdout + result.stderr
    return result.returncode == 1 and re.search(r"(?m)^\d+ failed(?:,|\s|$)", output) is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list the mutations and exit")
    parser.add_argument("--only", metavar="NAME", help="run a single mutation by name")
    parser.add_argument("--baseline-timeout", type=int, default=900)
    parser.add_argument("--mutation-timeout", type=int, default=180)
    args = parser.parse_args()

    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.name}\n    {mutation.invariant}")
            print(f"    killed by {mutation.test}::{mutation.test_id}")
        return 0

    selected = MUTATIONS
    if args.only:
        selected = tuple(m for m in MUTATIONS if m.name == args.only)
        if not selected:
            print(f"no mutation named {args.only!r}")
            return 2

    with tempfile.TemporaryDirectory(prefix="nexus-llm-gateway-mutations-") as temporary:
        package_root = Path(temporary) / "src"
        shutil.copytree(PACKAGE, package_root / "nexus_ai_agent")
        mutated_root = package_root / "nexus_ai_agent"

        print(f"baseline ({len(ALL_TESTS)} gateway test files) ... ", end="", flush=True)
        baseline = _run_pytest(package_root, ALL_TESTS, args.baseline_timeout)
        if baseline.returncode != 0:
            print("RED — refusing to mutate a failing baseline")
            print(_tail(baseline.stdout + baseline.stderr, lines=40))
            return 2
        print("GREEN")

        killed = 0
        survivors: list[str] = []
        for mutation in selected:
            path = mutated_root / mutation.relative
            current = path.read_text(encoding="utf-8")
            if mutation.old not in current:
                print(f"{mutation.name}: DOES NOT APPLY — source drifted")
                survivors.append(mutation.name)
                continue

            path.write_text(current.replace(mutation.old, mutation.new, 1), encoding="utf-8")
            try:
                result = _run_pytest(
                    package_root,
                    f"{mutation.test}::{mutation.test_id}",
                    args.mutation_timeout,
                )
            finally:
                path.write_text(current, encoding="utf-8")

            if result.returncode == 0:
                print(f"{mutation.name}: SURVIVED — {mutation.invariant}")
                survivors.append(mutation.name)
            elif _is_test_failure(result):
                killed += 1
                print(f"{mutation.name}: killed")
            else:
                print(f"{mutation.name}: ERROR — mutant did not produce a pytest failure")
                print(_tail(result.stdout + result.stderr, lines=40))
                return 4

        print("restored baseline ... ", end="", flush=True)
        restored = _run_pytest(package_root, ALL_TESTS, args.baseline_timeout)
        if restored.returncode != 0:
            print("RED — restored copy failed")
            print(_tail(restored.stdout + restored.stderr, lines=40))
            return 3
        print("GREEN")

    if survivors:
        print(f"{killed}/{len(selected)} mutants killed; survivors: {', '.join(survivors)}")
        return 1
    print(f"{killed}/{len(selected)} mutants killed; every law above is enforced by a test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
