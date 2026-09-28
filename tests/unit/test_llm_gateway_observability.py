"""W2 — observability and truthful usage accounting (LAW 10, LAW 11).

Two promises are tested here:

1. **Every request leaves exactly one record** naming the request id, caller,
   provider, model, per-phase timings, retries, outcome and usage — and that
   record never carries a prompt, a completion, or a credential, even when the
   provider's own error message or a caller's metadata tries to smuggle one in.
2. **Usage is what the provider reported, or UNKNOWN.** A cost is attached only
   when a pinned price and reported token counts both exist; a zero price is a
   fact (free tier), a missing price is not a zero.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nexus_ai_agent.llm.errors import (
    GatewayInternalError,
    InvalidRequestError,
    LLMErrorKind,
    RateLimitedError,
    TransientProviderError,
)
from nexus_ai_agent.llm.gateway.contract import (
    AttemptRecord,
    Caller,
    CallerCategory,
    FinishReason,
    LLMOperation,
    PolicyOutcome,
    Timings,
    Usage,
    UsageSource,
)
from nexus_ai_agent.llm.gateway.observability import (
    DEFAULT_RECORD_BUFFER,
    MAX_METADATA_FIELDS,
    MAX_METADATA_VALUE_CHARS,
    OUTCOME_CANCELLED,
    OUTCOME_DEADLINE,
    OUTCOME_DEGRADED,
    OUTCOME_ERROR,
    OUTCOME_OVERLOADED,
    OUTCOME_REFUSED,
    OUTCOME_SUCCESS,
    CollectingSink,
    GatewayMetrics,
    RequestRecord,
    StructlogSink,
    build_error_record,
    collect_records,
    sanitize_metadata,
)
from nexus_ai_agent.llm.gateway.usage import (
    DEFAULT_PRICE_TABLE,
    PRICE_TABLE_VERSION,
    ModelPrice,
    apply_cost,
    price_for,
)

CALLER = Caller(category=CallerCategory.AGENT, name="test.observability", tenant_id=7)
SECRET_PROMPT = "the user's private prompt about their medical records"
SECRET_ANSWER = "the model's private completion"


def _record(**kwargs: Any) -> RequestRecord:
    base: dict[str, Any] = {
        "request_id": "req-1",
        "caller": CALLER,
        "purpose": "chat",
        "operation": LLMOperation.CHAT,
        "outcome": OUTCOME_SUCCESS,
        "provider": "gemini",
        "model": "gemini-2.0-flash",
        "timings": Timings(queued_seconds=0.01, executed_seconds=0.2, total_seconds=0.21),
        "finish_reason": FinishReason.STOP,
    }
    base.update(kwargs)
    return RequestRecord(**base)


# ═══════════════════════════════════════════════════════════════════════════
# The record contents
# ═══════════════════════════════════════════════════════════════════════════


def test_a_record_names_everything_an_operator_needs() -> None:
    payload = _record().as_dict()
    for key in (
        "request_id",
        "caller",
        "tenant_id",
        "purpose",
        "operation",
        "outcome",
        "provider",
        "model",
        "attempts",
        "retries",
        "finish_reason",
    ):
        assert key in payload, key
    assert payload["caller"] == "agent:test.observability"
    assert payload["tenant_id"] == 7
    assert payload["operation"] == "chat"


def test_the_four_timing_phases_are_reported_separately() -> None:
    """``total`` alone cannot distinguish saturation from a slow provider."""

    payload = _record(
        timings=Timings(
            queued_seconds=1.5,
            rate_waited_seconds=2.5,
            executed_seconds=0.5,
            backoff_seconds=0.25,
            total_seconds=4.75,
        )
    ).as_dict()
    assert payload["queued_seconds"] == 1.5
    assert payload["rate_waited_seconds"] == 2.5
    assert payload["executed_seconds"] == 0.5
    assert payload["backoff_seconds"] == 0.25
    assert payload["total_seconds"] == 4.75


def test_timings_are_rounded_so_a_log_line_stays_readable() -> None:
    payload = _record(timings=Timings(total_seconds=0.123456789)).as_dict()
    assert payload["total_seconds"] == 0.1235


def test_attempt_history_is_projected_without_provider_messages() -> None:
    attempts = (
        AttemptRecord(
            index=1,
            provider="gemini",
            model="gemini-2.0-flash",
            started_at=1.0,
            duration_seconds=0.3,
            outcome="error",
            error_kind=LLMErrorKind.TRANSIENT_PROVIDER.value,
            status_code=503,
        ),
        AttemptRecord(
            index=2,
            provider="gemini",
            model="gemini-2.0-flash",
            started_at=1.4,
            duration_seconds=0.2,
            outcome="success",
        ),
    )
    record = _record(attempts=attempts)
    assert record.attempts_count == 2
    assert record.retries == 1
    history = record.as_dict()["attempt_outcomes"]
    assert [entry["outcome"] for entry in history] == ["error", "success"]
    assert history[0]["status_code"] == 503
    assert "detail" not in history[0]


def test_the_policy_outcome_flags_are_visible_on_the_record() -> None:
    payload = _record(
        policy=PolicyOutcome(
            route_provider="ollama",
            route_model="llama-3.3-70b-versatile",
            attempts=2,
            retries=1,
            degraded=True,
            fallback_used=True,
            fallback_from="gemini/gemini-2.0-flash",
            circuit_open=True,
            admission_rejected=False,
            rate_limited_locally=True,
            idempotency_hit=False,
        )
    ).as_dict()
    assert payload["degraded"] is True
    assert payload["fallback_used"] is True
    assert payload["fallback_from"] == "gemini/gemini-2.0-flash"
    assert payload["circuit_open"] is True
    assert payload["idempotency_hit"] is False


def test_a_record_is_json_serialisable() -> None:
    blob = json.dumps(_record().as_dict(), default=str)
    assert "req-1" in blob


# ═══════════════════════════════════════════════════════════════════════════
# Privacy: no prompt, no completion, no credential (LAW 10)
# ═══════════════════════════════════════════════════════════════════════════


def test_the_record_carries_sizes_not_content() -> None:
    record = _record(prompt_chars=len(SECRET_PROMPT), payload_bytes=4096)
    payload = record.as_dict()
    assert payload["prompt_chars"] == len(SECRET_PROMPT)
    assert payload["payload_bytes"] == 4096
    assert SECRET_PROMPT not in json.dumps(payload, default=str)


def test_a_provider_message_carrying_the_prompt_never_reaches_the_record() -> None:
    """The hostile-content case: the provider echoes part of the prompt back."""

    error = InvalidRequestError(
        f"prompt rejected: {SECRET_PROMPT}", status_code=400, provider="gemini", model="m"
    )
    record = build_error_record(
        request_id="r",
        caller=CALLER,
        purpose="chat",
        operation=LLMOperation.CHAT,
        outcome=OUTCOME_ERROR,
        error=error,
    )
    blob = json.dumps(record.as_dict(), default=str)
    assert SECRET_PROMPT not in blob
    assert "medical records" not in blob
    # ...but the typed facts survive.
    assert record.error_kind is LLMErrorKind.INVALID_REQUEST
    assert record.status_code == 400
    assert record.retryable is False


def test_a_provider_message_carrying_a_credential_never_reaches_the_record() -> None:
    error = RateLimitedError(
        "quota exceeded for key AIzaSyDUMMY-KEY-VALUE-MUST-NOT-LEAK-1234567890",
        status_code=429,
        provider="gemini",
    )
    record = build_error_record(
        request_id="r",
        caller=CALLER,
        purpose="chat",
        operation=LLMOperation.CHAT,
        outcome=OUTCOME_ERROR,
        error=error,
    )
    blob = json.dumps(record.as_dict(), default=str)
    assert "AIzaSy" not in blob
    assert record.error_kind is LLMErrorKind.RATE_LIMITED


def test_a_non_llm_exception_yields_a_record_with_no_kind_and_no_message() -> None:
    record = build_error_record(
        request_id="r",
        caller=CALLER,
        purpose="chat",
        operation=LLMOperation.CHAT,
        outcome=OUTCOME_ERROR,
        error=RuntimeError(f"boom {SECRET_ANSWER}"),
    )
    assert record.error_kind is None
    assert SECRET_ANSWER not in json.dumps(record.as_dict(), default=str)


def test_metadata_cannot_smuggle_a_prompt_into_a_record() -> None:
    clean = sanitize_metadata(
        {
            "prompt": SECRET_PROMPT,
            "system": "secret system prompt",
            "message": SECRET_ANSWER,
            "api_key": "AIzaSyDUMMYKEY",
            "token": "bearer-token",
            "surface": "telegram",
        }
    )
    assert set(clean) == {"surface"}
    blob = json.dumps(clean)
    assert SECRET_PROMPT not in blob
    assert "AIzaSy" not in blob


def test_the_record_builder_itself_sanitizes_caller_metadata() -> None:
    """Sanitization must happen at the wiring, not only inside the helper.

    A builder that forwarded caller metadata untouched would persist a prompt or
    a credential under an innocent-looking key, and every consumer downstream —
    the log sink, the metrics buffer, ``/status``, an OTLP export — would
    inherit the leak. Testing ``sanitize_metadata`` alone cannot catch that,
    because the leak lives in the call site, not in the function.
    """

    record = build_error_record(
        request_id="r",
        caller=CALLER,
        purpose="chat",
        operation=LLMOperation.CHAT,
        outcome=OUTCOME_ERROR,
        error=RateLimitedError("throttled", status_code=429, provider="gemini"),
        metadata={
            "prompt": SECRET_PROMPT,
            "message": SECRET_ANSWER,
            "api_key": "AIzaSyDUMMYKEY",
            "surface": "telegram",
        },
    )
    assert record.metadata == {"surface": "telegram"}
    blob = json.dumps(record.as_dict(), default=str)
    assert SECRET_PROMPT not in blob
    assert SECRET_ANSWER not in blob
    assert "AIzaSy" not in blob


@pytest.mark.parametrize(
    "key",
    [
        "prompt",
        "PROMPT",
        "Prompt",
        "system",
        "text",
        "message",
        "messages",
        "content",
        "output",
        "response",
        "api_key",
        "apikey",
        "key",
        "token",
        "secret",
        "password",
        "authorization",
        "cookie",
        "image",
        "audio",
        "data",
    ],
)
def test_every_blocked_metadata_key_is_dropped_regardless_of_case(key: str) -> None:
    assert sanitize_metadata({key: "value"}) == {}


def test_metadata_is_bounded_in_field_count_and_value_length() -> None:
    many = {f"k{i}": "x" * 500 for i in range(50)}
    clean = sanitize_metadata(many)
    assert len(clean) <= MAX_METADATA_FIELDS
    assert all(len(value) <= MAX_METADATA_VALUE_CHARS for value in clean.values())


@pytest.mark.parametrize(
    "value",
    [
        "x-goog-api-key=AIzaSyDUMMYDUMMYDUMMYDUMMYDUMMYDUMMY00",
        "Authorization: Bearer abcdefghijklmnop1234567890",
        "api_key: sk-DUMMYDUMMYDUMMYDUMMY1234",
        "https://user:password@host.example/path",
    ],
)
def test_a_secret_shaped_value_surviving_in_metadata_is_redacted(value: str) -> None:
    """Metadata is caller-supplied, so it goes through the repository redactor."""

    clean = sanitize_metadata({"note": value})
    leaked = [
        fragment
        for fragment in (
            "AIzaSyDUMMYDUMMYDUMMYDUMMYDUMMYDUMMY00",
            "abcdefghijklmnop1234567890",
            "sk-DUMMYDUMMYDUMMYDUMMY1234",
            "user:password@",
        )
        if fragment in clean["note"]
    ]
    assert not leaked, leaked


def test_metadata_keys_are_truncated_so_a_hostile_key_cannot_bloat_a_record() -> None:
    clean = sanitize_metadata({"k" * 500: "v"})
    assert all(len(key) <= 64 for key in clean)


def test_otel_attributes_follow_the_genai_conventions() -> None:
    attrs = _record(
        usage=Usage(source=UsageSource.PROVIDER, input_tokens=12, output_tokens=5, total_tokens=17)
    ).otel_attributes()
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "gemini-2.0-flash"
    assert attrs["gen_ai.provider.name"] == "gemini"
    assert attrs["gen_ai.response.finish_reasons"] == ["stop"]
    assert attrs["gen_ai.usage.input_tokens"] == 12
    assert attrs["gen_ai.usage.output_tokens"] == 5
    assert attrs["nexus.llm.request_id"] == "req-1"


def test_otel_attributes_never_carry_prompt_or_completion_content() -> None:
    """The conventions mark content attributes opt-in precisely because they leak."""

    attrs = _record().otel_attributes()
    assert "gen_ai.prompt" not in attrs
    assert "gen_ai.completion" not in attrs
    blob = json.dumps(attrs, default=str)
    assert SECRET_PROMPT not in blob


def test_otel_attributes_drop_unknown_usage_instead_of_reporting_zero() -> None:
    attrs = _record(usage=Usage()).otel_attributes()
    assert "gen_ai.usage.input_tokens" not in attrs
    assert "gen_ai.usage.output_tokens" not in attrs


def test_otel_attributes_carry_the_error_type_on_failure() -> None:
    record = build_error_record(
        request_id="r",
        caller=CALLER,
        purpose="chat",
        operation=LLMOperation.CHAT,
        outcome=OUTCOME_ERROR,
        error=TransientProviderError("down", status_code=503),
    )
    assert record.otel_attributes()["error.type"] == LLMErrorKind.TRANSIENT_PROVIDER.value


def test_unknown_usage_is_declared_explicitly_in_the_projection() -> None:
    """A dashboard must be able to tell "no tokens" from "we do not know"."""

    payload = _record(usage=Usage()).as_dict()
    assert payload["usage"] == "unknown"
    assert "usage_input_tokens" not in payload


def test_reported_usage_is_projected_with_its_cost_when_known() -> None:
    usage = Usage(
        source=UsageSource.PROVIDER,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        estimated_cost_usd=0.0,
    )
    payload = _record(usage=usage).as_dict()
    assert payload["usage_input_tokens"] == 100
    assert payload["usage_output_tokens"] == 50
    assert payload["usage_total_tokens"] == 150
    assert payload["usage_estimated_cost_usd"] == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Sinks
# ═══════════════════════════════════════════════════════════════════════════


class _FakeLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("info", event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("warning", event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("error", event, kwargs))


def test_the_default_sink_logs_a_success_at_info() -> None:
    logger = _FakeLogger()
    StructlogSink(logger=logger).emit(_record())
    assert logger.calls[0][0] == "info"
    assert logger.calls[0][1] == "llm_request"


def test_the_default_sink_logs_a_provider_error_at_warning_without_a_traceback() -> None:
    logger = _FakeLogger()
    record = build_error_record(
        request_id="r",
        caller=CALLER,
        purpose="chat",
        operation=LLMOperation.CHAT,
        outcome=OUTCOME_ERROR,
        error=TransientProviderError("down", status_code=503),
    )
    StructlogSink(logger=logger).emit(record)
    level, _, kwargs = logger.calls[0]
    assert level == "warning"
    assert "exc_info" not in kwargs  # a traceback per 503 is noise


def test_a_gateway_internal_failure_is_logged_without_a_sensitive_traceback() -> None:
    """Internal errors can chain provider payloads; no traceback is safe here."""

    logger = _FakeLogger()
    record = build_error_record(
        request_id="r",
        caller=CALLER,
        purpose="chat",
        operation=LLMOperation.CHAT,
        outcome=OUTCOME_ERROR,
        error=GatewayInternalError("adapter returned nonsense"),
    )
    StructlogSink(logger=logger).emit(record)
    level, _, kwargs = logger.calls[0]
    assert level == "error"
    assert not kwargs.get("exc_info")


@pytest.mark.parametrize(
    "outcome", [OUTCOME_CANCELLED, OUTCOME_OVERLOADED, OUTCOME_DEADLINE, OUTCOME_REFUSED]
)
def test_operational_outcomes_are_logged_at_a_predictable_level(outcome: str) -> None:
    logger = _FakeLogger()
    StructlogSink(logger=logger).emit(_record(outcome=outcome))
    assert logger.calls[0][0] in {"info", "warning"}


def test_a_broken_logger_cannot_break_the_sink() -> None:
    class ExplodingLogger(_FakeLogger):
        def info(self, event: str, **kwargs: Any) -> None:
            raise RuntimeError("logger is down")

        def error(self, event: str, **kwargs: Any) -> None:
            raise RuntimeError("logger is down")

    StructlogSink(logger=ExplodingLogger()).emit(_record())  # must not raise


def test_the_collecting_sink_is_bounded() -> None:
    sink = CollectingSink(limit=4)
    for index in range(20):
        sink.emit(_record(request_id=f"r{index}"))
    assert len(sink.records) == 4
    assert sink.find("r19") is not None
    assert sink.find("r0") is None


def test_the_collecting_sink_counts_outcomes_and_error_kinds() -> None:
    sink = CollectingSink()
    sink.emit(_record(outcome=OUTCOME_SUCCESS))
    sink.emit(_record(outcome=OUTCOME_DEGRADED))
    sink.emit(
        build_error_record(
            request_id="r",
            caller=CALLER,
            purpose="chat",
            operation=LLMOperation.CHAT,
            outcome=OUTCOME_ERROR,
            error=RateLimitedError("slow", status_code=429),
        )
    )
    assert sink.outcomes()[OUTCOME_SUCCESS] == 1
    assert sink.error_kinds()[LLMErrorKind.RATE_LIMITED.value] == 1


def test_collect_records_finds_the_collecting_sink_among_others() -> None:
    collector = CollectingSink()
    found = collect_records([StructlogSink(logger=_FakeLogger()), collector])
    assert found is collector


def test_collect_records_returns_none_when_there_is_no_collector() -> None:
    assert collect_records([StructlogSink(logger=_FakeLogger())]) is None


def test_the_default_record_buffer_is_bounded() -> None:
    sink = CollectingSink()
    assert sink.records.maxlen == DEFAULT_RECORD_BUFFER


# ═══════════════════════════════════════════════════════════════════════════
# Aggregate metrics
# ═══════════════════════════════════════════════════════════════════════════


def test_metrics_count_requests_attempts_and_retries() -> None:
    metrics = GatewayMetrics()
    metrics.observe(
        _record(
            attempts=(
                AttemptRecord(
                    1,
                    "gemini",
                    "m",
                    0.0,
                    0.1,
                    "error",
                    error_kind=LLMErrorKind.TRANSIENT_PROVIDER.value,
                ),
                AttemptRecord(2, "gemini", "m", 0.2, 0.1, "success"),
            )
        )
    )
    payload = metrics.as_dict()
    assert payload["requests"] == 1
    assert payload["attempts"] == 2
    assert payload["retries"] == 1
    assert payload["outcomes"][OUTCOME_SUCCESS] == 1


def test_metrics_do_not_key_on_caller_or_purpose() -> None:
    """Unbounded cardinality is how an in-process metric map becomes a leak."""

    metrics = GatewayMetrics()
    for index in range(200):
        metrics.observe(
            _record(
                request_id=f"r{index}",
                caller=Caller(CallerCategory.AGENT, f"caller-{index}", tenant_id=index),
                purpose=f"purpose-{index}",
            )
        )
    payload = metrics.as_dict()
    assert len(payload["outcomes"]) == 1
    assert payload["provider_errors"] == {}
    assert payload["buffer_size"] <= DEFAULT_RECORD_BUFFER


def test_provider_error_counters_are_keyed_by_provider_and_kind_only() -> None:
    metrics = GatewayMetrics()
    for index in range(50):
        metrics.observe(
            build_error_record(
                request_id=f"r{index}",
                caller=Caller(CallerCategory.AGENT, f"c{index}", tenant_id=index),
                purpose=f"p{index}",
                operation=LLMOperation.CHAT,
                outcome=OUTCOME_ERROR,
                error=TransientProviderError("down", status_code=503, provider="gemini"),
                provider="gemini",
            )
        )
    payload = metrics.as_dict()
    assert len(payload["provider_errors"]) == 1
    assert payload["provider_errors"]["gemini:transient_provider_failure"] == 50


def test_metrics_count_fallbacks_degraded_and_admission_rejections() -> None:
    metrics = GatewayMetrics()
    metrics.observe(
        _record(
            outcome=OUTCOME_DEGRADED,
            policy=PolicyOutcome(
                route_provider="ollama",
                route_model="m",
                attempts=1,
                retries=0,
                degraded=True,
                fallback_used=True,
                fallback_from="gemini/m",
            ),
        )
    )
    metrics.observe(
        _record(
            outcome=OUTCOME_OVERLOADED,
            policy=PolicyOutcome(
                route_provider="gemini",
                route_model="m",
                attempts=0,
                retries=0,
                admission_rejected=True,
            ),
        )
    )
    payload = metrics.as_dict()
    assert payload["fallbacks"] == 1
    assert payload["degraded"] == 1
    assert payload["admission_rejected"] == 1


def test_metrics_separate_reported_usage_from_unknown_usage() -> None:
    metrics = GatewayMetrics()
    metrics.observe(
        _record(
            usage=Usage(
                source=UsageSource.PROVIDER,
                input_tokens=10,
                output_tokens=4,
                total_tokens=14,
                estimated_cost_usd=0.0,
            )
        )
    )
    metrics.observe(_record(request_id="r2", usage=Usage()))
    payload = metrics.as_dict()
    assert payload["usage_reported"] == 1
    assert payload["usage_unknown"] == 1
    assert payload["usage_input_tokens"] == 10
    assert payload["usage_output_tokens"] == 4


def test_recent_returns_newest_first_and_honours_the_limit() -> None:
    metrics = GatewayMetrics()
    for index in range(10):
        metrics.observe(_record(request_id=f"r{index}"))
    recent = metrics.recent(limit=3)
    assert [entry["request_id"] for entry in recent] == ["r9", "r8", "r7"]


def test_average_timings_are_zero_rather_than_a_division_error_when_idle() -> None:
    payload = GatewayMetrics().as_dict()
    assert payload["avg_execution_seconds"] == 0.0
    assert payload["avg_queue_wait_seconds"] == 0.0


def test_metrics_are_json_serialisable() -> None:
    metrics = GatewayMetrics()
    metrics.observe(_record())
    assert json.dumps(metrics.as_dict(), default=str)


# ═══════════════════════════════════════════════════════════════════════════
# Cost accounting (LAW 11)
# ═══════════════════════════════════════════════════════════════════════════


def test_the_price_table_is_versioned_so_a_cost_can_be_attributed() -> None:
    assert PRICE_TABLE_VERSION == "2026-09-28"


def test_an_explicitly_free_endpoint_costs_exactly_zero() -> None:
    usage = Usage(
        source=UsageSource.PROVIDER,
        input_tokens=1_000_000,
        output_tokens=500_000,
        total_tokens=1_500_000,
    )
    priced = apply_cost(usage, model="meta-llama/llama-3.3-70b-instruct:free")
    assert priced.estimated_cost_usd == 0.0
    assert priced.source is UsageSource.PROVIDER


def test_an_unpriced_model_leaves_the_cost_unknown() -> None:
    """A missing price is not a zero price — inventing one would be a lie."""

    usage = Usage(source=UsageSource.PROVIDER, input_tokens=1000, output_tokens=500)
    priced = apply_cost(usage, model="gpt-5-turbo-not-in-table")
    assert priced.estimated_cost_usd is None
    assert priced is usage  # unchanged, not a copy with a guess


def test_a_priced_model_computes_the_cost_from_reported_tokens() -> None:
    table = {"paid-model": ModelPrice(input_usd_per_million=2.0, output_usd_per_million=8.0)}
    usage = Usage(source=UsageSource.PROVIDER, input_tokens=1_000_000, output_tokens=1_000_000)
    priced = apply_cost(usage, model="paid-model", table=table)
    assert priced.estimated_cost_usd == pytest.approx(10.0)


def test_a_partial_price_is_treated_as_unknown() -> None:
    table = {"half-priced": ModelPrice(input_usd_per_million=2.0, output_usd_per_million=None)}
    usage = Usage(source=UsageSource.PROVIDER, input_tokens=1000, output_tokens=10)
    assert apply_cost(usage, model="half-priced", table=table).estimated_cost_usd is None


def test_usage_the_provider_did_not_report_is_never_costed() -> None:
    """``UsageSource`` has exactly two members by design: reported, or unknown.

    A third "estimated" source would be an invitation to invent numbers, so the
    enum does not have one (LAW 11).
    """

    assert {member.value for member in UsageSource} == {"provider", "unknown"}
    usage = Usage(source=UsageSource.UNKNOWN, input_tokens=1000, output_tokens=10)
    assert (
        apply_cost(
            usage, model="gemini-2.0-flash", table={"gemini-2.0-flash": ModelPrice(2.0, 8.0)}
        )
        is usage
    )


def test_reported_tokens_without_a_count_are_not_costed() -> None:
    usage = Usage(source=UsageSource.PROVIDER, input_tokens=None, output_tokens=None)
    assert (
        apply_cost(
            usage, model="gemini-2.0-flash", table={"gemini-2.0-flash": ModelPrice(2.0, 8.0)}
        ).estimated_cost_usd
        is None
    )


def test_costing_preserves_the_reported_model_and_token_counts() -> None:
    usage = Usage(
        source=UsageSource.PROVIDER,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        reported_model="gemini-2.0-flash-001",
    )
    priced = apply_cost(usage, model="gemini-2.0-flash")
    assert priced.reported_model == "gemini-2.0-flash-001"
    assert (priced.input_tokens, priced.output_tokens, priced.total_tokens) == (10, 5, 15)


def test_price_lookup_returns_none_for_an_unknown_model() -> None:
    assert price_for("model-that-does-not-exist") is None
    assert price_for("gemini-2.0-flash") is not None


def test_every_model_the_repository_configures_by_default_has_a_price_entry() -> None:
    """The table is evidence-based: it covers what config/settings.py can select."""

    for model in (
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "llama-3.3-70b-versatile",
        "local-model",
    ):
        assert model in DEFAULT_PRICE_TABLE, model


def test_a_negative_cost_is_never_produced() -> None:
    table = {"weird": ModelPrice(input_usd_per_million=0.0, output_usd_per_million=0.0)}
    usage = Usage(source=UsageSource.PROVIDER, input_tokens=0, output_tokens=0)
    assert apply_cost(usage, model="weird", table=table).estimated_cost_usd == 0.0


@pytest.mark.parametrize(
    "model",
    [
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-2.5-flash-image",
        "llama-3.3-70b-versatile",
        "local-model",
    ],
)
def test_model_identity_does_not_identify_billing_contract(model: str) -> None:
    usage = Usage(source=UsageSource.PROVIDER, input_tokens=1000, output_tokens=500)
    assert apply_cost(usage, model=model).estimated_cost_usd is None


@pytest.mark.parametrize(
    "input_price,output_price",
    [
        (-1, 0),
        (0, -1),
        (float("nan"), 0),
        (0, float("inf")),
        (float("-inf"), None),
    ],
)
def test_pinned_prices_are_finite_and_nonnegative(input_price: Any, output_price: Any) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ModelPrice(input_price, output_price)


@pytest.mark.parametrize("price,expected", [(0.0, 0.0), (2.0, 3.0)])
def test_operator_can_pin_a_verified_billing_contract(price: float, expected: float) -> None:
    usage = Usage(source=UsageSource.PROVIDER, input_tokens=1_000_000, output_tokens=500_000)
    result = apply_cost(
        usage, model="gemini-2.0-flash", table={"gemini-2.0-flash": ModelPrice(price, price)}
    )
    assert result.estimated_cost_usd == expected


def test_aggregate_does_not_present_unknown_spend_as_free() -> None:
    metrics = GatewayMetrics()
    metrics.observe(
        _record(usage=Usage(source=UsageSource.PROVIDER, input_tokens=1000, output_tokens=500))
    )
    assert metrics.estimated_cost_usd is None
    assert metrics.as_dict()["estimated_cost_usd"] is None
    assert metrics.as_dict()["cost_unknown_requests"] == 1
    metrics.observe(
        _record(
            usage=Usage(
                source=UsageSource.PROVIDER,
                input_tokens=1000,
                output_tokens=500,
                estimated_cost_usd=2.0,
            )
        )
    )
    assert metrics.as_dict()["estimated_cost_usd"] is None
    assert metrics.as_dict()["known_cost_subtotal_usd"] == 2.0


def test_reuse_and_pre_execution_refusal_do_not_invent_unknown_provider_spend() -> None:
    metrics = GatewayMetrics()
    for hit in (True, False):
        metrics.observe(
            _record(
                policy=PolicyOutcome(
                    route_provider="p", route_model="m", attempts=0, idempotency_hit=hit
                )
            )
        )
    assert metrics.estimated_cost_usd == 0.0
    assert metrics.cost_unknown_requests == 0


def test_retry_cost_subtotal_does_not_claim_knowledge_of_failed_attempt_spend() -> None:
    metrics = GatewayMetrics()
    metrics.observe(
        _record(
            attempts=(
                AttemptRecord(1, "p", "m", 0, 0.1, "error"),
                AttemptRecord(2, "p", "m", 0.1, 0.1, "success"),
            ),
            policy=PolicyOutcome(route_provider="p", route_model="m", attempts=2, retries=1),
            usage=Usage(
                source=UsageSource.PROVIDER, input_tokens=1, output_tokens=1, estimated_cost_usd=1.0
            ),
        )
    )
    assert metrics.estimated_cost_usd is None
    assert metrics.as_dict()["known_cost_subtotal_usd"] == 1.0
    assert metrics.cost_unknown_requests == 1
