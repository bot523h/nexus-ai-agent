"""Gateway observability (W2, LAW 10 — observable by default, secret-free by construction).

Every request that enters the authority produces exactly one
:class:`RequestRecord`, whether it succeeded, degraded, failed, was cancelled or
was refused. The record answers the operational questions without ever carrying
the payload:

    request id · caller category/name · tenant · purpose · operation
    provider · model · start/end · queue wait · rate wait · execution time
    retries · attempts (per-attempt outcome) · final outcome · error class
    usage (provider-reported or UNKNOWN) · degraded/fallback flag

What is *never* in a record: prompt text, system prompt, model output, message
history, inline media bytes, API keys, tokens, or any free-form provider error
message. Payload presence is expressed as ``prompt_chars`` and
``payload_bytes`` — enough to correlate "large request → context limit" without
reproducing the request. Defence in depth: every string that does enter a record
passes through the repository's existing redaction layer
(:func:`nexus_ai_agent.observability.logging.redact_secrets`), so an accidental
secret in a caller-supplied metadata value is masked rather than trusted to
never happen. No second telemetry stack is introduced (LAW: no duplicate
observability).

Field names follow the OpenTelemetry GenAI semantic conventions
(``gen_ai.operation.name``, ``gen_ai.request.model``, ``gen_ai.provider.name``,
``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``, ``error.type``)
so exporting to a real OTel collector later is a mapping, not a redesign — see
:meth:`RequestRecord.otel_attributes`. The gateway does **not** depend on the
OpenTelemetry SDK: adding a runtime dependency for an exporter nobody runs yet
is exactly the unjustified complexity the mission forbids.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import islice
from typing import Any, Protocol

from nexus_ai_agent.llm.errors import LLMError, LLMErrorKind
from nexus_ai_agent.llm.gateway.contract import (
    AttemptRecord,
    Caller,
    FinishReason,
    LLMOperation,
    PolicyOutcome,
    Timings,
    Usage,
)
from nexus_ai_agent.observability.logging import get_logger, redact_secrets

__all__ = [
    "GatewayMetrics",
    "ObservationSink",
    "RequestRecord",
    "StructlogSink",
    "collect_records",
]

log = get_logger(__name__)

#: Outcome labels. A closed set — an unbounded outcome dimension is how a metric
#: backend turns into a memory leak.
OUTCOME_SUCCESS = "success"
OUTCOME_DEGRADED = "degraded_success"
OUTCOME_ERROR = "error"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_DEADLINE = "deadline_exceeded"
OUTCOME_OVERLOADED = "overloaded"
OUTCOME_REFUSED = "policy_refusal"
OUTCOME_CLOSED = "gateway_closed"

#: Maximum number of records retained in the in-process ring buffer.
DEFAULT_RECORD_BUFFER = 256
#: Maximum number of metadata key/value pairs copied into a record.
MAX_METADATA_FIELDS = 8
#: Maximum length of a metadata value copied into a record.
MAX_METADATA_VALUE_CHARS = 128
#: Keys that must never be copied from caller metadata, whatever the caller says.
_BLOCKED_METADATA_KEYS = frozenset(
    {
        "prompt",
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
    }
)


class ObservationSink(Protocol):
    """Where records go. Implementations must never raise into the request path."""

    def emit(self, record: RequestRecord) -> None: ...


@dataclass(frozen=True)
class RequestRecord:
    """One immutable observation of one logical request."""

    request_id: str
    caller: Caller
    purpose: str
    operation: LLMOperation
    outcome: str
    provider: str | None = None
    model: str | None = None
    error_kind: LLMErrorKind | None = None
    status_code: int | None = None
    retryable: bool | None = None
    usage: Usage = field(default_factory=Usage)
    finish_reason: FinishReason = FinishReason.UNKNOWN
    timings: Timings = field(default_factory=Timings)
    policy: PolicyOutcome | None = None
    attempts: Sequence[AttemptRecord] = ()
    started_at: float = 0.0
    ended_at: float = 0.0
    prompt_chars: int = 0
    payload_bytes: int = 0
    metadata: Mapping[str, str] = field(default_factory=dict)
    #: Present only when the caller supplied one; never derived from content.
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        # Sanitize before *any* sink sees a record, not only the default logger.
        object.__setattr__(
            self, "caller", replace(self.caller, name=redact_secrets(self.caller.name)[:128])
        )
        for name in ("purpose", "provider", "model"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, redact_secrets(value[:256]))
        if self.idempotency_key is not None:
            object.__setattr__(
                self,
                "idempotency_key",
                hashlib.sha256(self.idempotency_key.encode("utf-8", "replace")).hexdigest(),
            )

    @property
    def attempts_count(self) -> int:
        return len(self.attempts)

    @property
    def retries(self) -> int:
        return max(0, len(self.attempts) - 1)

    def as_dict(self) -> dict[str, Any]:
        """Log-safe flat projection. This is what reaches structlog."""

        data: dict[str, Any] = {
            "request_id": self.request_id,
            "caller": self.caller.label,
            "tenant_id": self.caller.tenant_id,
            "purpose": self.purpose,
            "operation": self.operation.value,
            "outcome": self.outcome,
            "provider": self.provider,
            "model": self.model,
            "attempts": self.attempts_count,
            "retries": self.retries,
            "prompt_chars": self.prompt_chars,
            "payload_bytes": self.payload_bytes,
            "finish_reason": self.finish_reason.value,
            **self.timings.as_dict(),
        }
        if self.error_kind is not None:
            data["error_kind"] = self.error_kind.value
            data["error_retryable"] = self.retryable
        if self.status_code is not None:
            data["status_code"] = self.status_code
        if self.usage.is_known:
            data["usage_input_tokens"] = self.usage.input_tokens
            data["usage_output_tokens"] = self.usage.output_tokens
            data["usage_total_tokens"] = self.usage.total_tokens
            if self.usage.estimated_cost_usd is not None:
                data["usage_estimated_cost_usd"] = self.usage.estimated_cost_usd
        else:
            # LAW 11: say "unknown" explicitly instead of omitting the field and
            # letting a dashboard render it as zero.
            data["usage"] = "unknown"
        if self.policy is not None:
            data["degraded"] = self.policy.degraded
            data["fallback_used"] = self.policy.fallback_used
            data["fallback_from"] = self.policy.fallback_from
            data["circuit_open"] = self.policy.circuit_open
            data["admission_rejected"] = self.policy.admission_rejected
            data["idempotency_hit"] = self.policy.idempotency_hit
        if self.attempts:
            data["attempt_outcomes"] = [
                {
                    "index": a.index,
                    "provider": a.provider,
                    "model": a.model,
                    "duration_seconds": round(a.duration_seconds, 4),
                    "outcome": a.outcome,
                    "error_kind": a.error_kind,
                    "status_code": a.status_code,
                }
                for a in self.attempts
            ]
        if self.metadata:
            data["meta"] = dict(self.metadata)
        if self.idempotency_key is not None:
            data["idempotency_key_hash"] = self.idempotency_key
        return data

    def otel_attributes(self) -> dict[str, Any]:
        """OpenTelemetry GenAI semantic-convention attribute mapping.

        Produced on demand; the gateway never imports the OTel SDK. Prompt and
        completion *content* attributes (``gen_ai.prompt``, ``gen_ai.completion``)
        are deliberately absent: the conventions mark them opt-in precisely
        because they carry user data.
        """

        attrs: dict[str, Any] = {
            "gen_ai.operation.name": self.operation.value,
            "gen_ai.request.model": self.model,
            "gen_ai.provider.name": self.provider,
            "error.type": self.error_kind.value if self.error_kind is not None else None,
            "gen_ai.response.finish_reasons": [self.finish_reason.value],
            "nexus.llm.request_id": self.request_id,
            "nexus.llm.caller": self.caller.label,
            "nexus.llm.purpose": self.purpose,
            "nexus.llm.outcome": self.outcome,
            "nexus.llm.attempts": self.attempts_count,
            "nexus.llm.queue_wait_seconds": round(self.timings.queued_seconds, 4),
            "nexus.llm.execution_seconds": round(self.timings.executed_seconds, 4),
            "nexus.llm.total_seconds": round(self.timings.total_seconds, 4),
        }
        if self.usage.is_known:
            attrs["gen_ai.usage.input_tokens"] = self.usage.input_tokens
            attrs["gen_ai.usage.output_tokens"] = self.usage.output_tokens
        return {k: v for k, v in attrs.items() if v is not None}


def sanitize_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    """Bound and redact caller metadata before it can reach a record.

    Three rules, applied in order: drop blocked keys (a caller cannot smuggle a
    prompt through ``metadata={'prompt': ...}``), truncate values, and run the
    repository redaction pass over whatever survives.
    """

    if not metadata:
        return {}
    clean: dict[str, str] = {}
    for key, value in islice(metadata.items(), MAX_METADATA_FIELDS):
        if key.lower() in _BLOCKED_METADATA_KEYS:
            continue
        text = value if isinstance(value, str) else str(value)
        text = redact_secrets(text[:MAX_METADATA_VALUE_CHARS])
        clean[key[:64]] = text
    return clean


class StructlogSink:
    """Default sink: one structured log line per request, level chosen by outcome.

    Errors are logged at ``error`` *without* a traceback: the typed error already
    carries the classification, and a traceback per throttled request is noise
    that hides the signal. Internal failures also omit tracebacks: chained raw
    adapter exceptions may contain credentials or prompt text.
    """

    def __init__(self, logger: Any | None = None) -> None:
        self._log = logger if logger is not None else log

    def emit(self, record: RequestRecord) -> None:
        event = "llm_request"
        payload = record.as_dict()
        try:
            if record.outcome in (OUTCOME_SUCCESS, OUTCOME_DEGRADED):
                self._log.info(event, **payload)
            elif record.outcome in (OUTCOME_CANCELLED, OUTCOME_OVERLOADED, OUTCOME_CLOSED):
                self._log.info(event, **payload)
            elif record.error_kind is LLMErrorKind.GATEWAY_INTERNAL:
                self._log.error(event, **payload)
            else:
                self._log.warning(event, **payload)
        except Exception:  # noqa: BLE001 — observability must never break a request
            # Last-resort: a sink that raises would turn telemetry into an outage.
            try:
                self._log.error("llm_request_record_dropped", request_id=record.request_id)
            except Exception:  # noqa: BLE001
                pass


@dataclass
class CollectingSink:
    """Test/inspection sink: keeps records in memory. Bounded like everything else."""

    limit: int = DEFAULT_RECORD_BUFFER
    records: deque[RequestRecord] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("CollectingSink.limit must be at least 1")
        self.records = deque(maxlen=self.limit)

    def emit(self, record: RequestRecord) -> None:
        self.records.append(record)

    def outcomes(self) -> Counter[str]:
        return Counter(r.outcome for r in self.records)

    def error_kinds(self) -> Counter[str]:
        return Counter(r.error_kind.value for r in self.records if r.error_kind is not None)

    def find(self, request_id: str) -> RequestRecord | None:
        for record in reversed(self.records):
            if record.request_id == request_id:
                return record
        return None


def collect_records(sinks: Iterable[ObservationSink]) -> CollectingSink | None:
    """Return the first :class:`CollectingSink` among *sinks* (test helper)."""

    for sink in sinks:
        if isinstance(sink, CollectingSink):
            return sink
    return None


@dataclass
class GatewayMetrics:
    """Aggregate counters, bounded cardinality, no external metric backend.

    Counters are keyed by ``(outcome,)`` and ``(provider, error_kind)`` only.
    Purpose and caller are *not* metric keys: their cardinality is caller-driven
    and unbounded, which is the classic way an in-process metric map becomes a
    leak. Per-request detail lives in the ring buffer instead.
    """

    requests: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    attempts: int = 0
    retries: int = 0
    fallbacks: int = 0
    degraded: int = 0
    admission_rejected: int = 0
    circuit_open_events: int = 0
    provider_errors: Counter[str] = field(default_factory=Counter)
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_reported: int = 0
    usage_unknown: int = 0
    known_cost_subtotal_usd: float = 0.0
    cost_unknown_requests: int = 0
    total_execution_seconds: float = 0.0
    total_queue_wait_seconds: float = 0.0
    buffer: deque[RequestRecord] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_RECORD_BUFFER)
    )

    @property
    def estimated_cost_usd(self) -> float | None:
        """Complete total only when every physical request has a known cost."""
        return None if self.cost_unknown_requests else self.known_cost_subtotal_usd

    def observe(self, record: RequestRecord) -> None:
        self.requests += 1
        self.outcomes[record.outcome] += 1
        self.attempts += record.attempts_count
        self.retries += record.retries
        self.total_execution_seconds += record.timings.executed_seconds
        self.total_queue_wait_seconds += record.timings.queued_seconds
        if record.policy is not None:
            if record.policy.fallback_used:
                self.fallbacks += 1
            if record.policy.degraded:
                self.degraded += 1
            if record.policy.admission_rejected:
                self.admission_rejected += 1
            if record.policy.circuit_open:
                self.circuit_open_events += 1
        if record.error_kind is not None and record.provider:
            key = f"{record.provider}:{record.error_kind.value}"
            if key not in self.provider_errors and len(self.provider_errors) >= 128:
                key = "<other>"
            self.provider_errors[key] += 1
        if record.usage.is_known:
            self.usage_reported += 1
            self.usage_input_tokens += record.usage.input_tokens or 0
            self.usage_output_tokens += record.usage.output_tokens or 0
            if record.usage.estimated_cost_usd is not None:
                self.known_cost_subtotal_usd += record.usage.estimated_cost_usd
        else:
            self.usage_unknown += 1
        no_execution = record.policy is not None and (
            record.policy.idempotency_hit or record.policy.attempts == 0
        )
        # Only the final answering attempt carries usage. Earlier failed
        # attempts may have spent tokens too; the subtotal is not an invoice.
        if not no_execution and (
            not record.usage.is_known
            or record.usage.estimated_cost_usd is None
            or record.attempts_count > 1
        ):
            self.cost_unknown_requests += 1
        self.buffer.append(record)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "outcomes": dict(self.outcomes),
            "attempts": self.attempts,
            "retries": self.retries,
            "fallbacks": self.fallbacks,
            "degraded": self.degraded,
            "admission_rejected": self.admission_rejected,
            "circuit_open_events": self.circuit_open_events,
            "provider_errors": dict(self.provider_errors),
            "usage_reported": self.usage_reported,
            "usage_unknown": self.usage_unknown,
            "usage_input_tokens": self.usage_input_tokens,
            "usage_output_tokens": self.usage_output_tokens,
            "estimated_cost_usd": (
                round(self.known_cost_subtotal_usd, 6) if not self.cost_unknown_requests else None
            ),
            "known_cost_subtotal_usd": round(self.known_cost_subtotal_usd, 6),
            "cost_unknown_requests": self.cost_unknown_requests,
            "avg_execution_seconds": (
                round(self.total_execution_seconds / self.attempts, 4) if self.attempts else 0.0
            ),
            "avg_queue_wait_seconds": (
                round(self.total_queue_wait_seconds / self.requests, 4) if self.requests else 0.0
            ),
            "buffer_size": len(self.buffer),
            "buffer_capacity": self.buffer.maxlen,
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent records, newest first — the /status view of the authority."""

        return [r.as_dict() for r in list(self.buffer)[-limit:][::-1]]


def build_error_record(
    *,
    request_id: str,
    caller: Caller,
    purpose: str,
    operation: LLMOperation,
    outcome: str,
    error: BaseException,
    provider: str | None = None,
    model: str | None = None,
    timings: Timings | None = None,
    policy: PolicyOutcome | None = None,
    attempts: Sequence[AttemptRecord] = (),
    started_at: float = 0.0,
    prompt_chars: int = 0,
    payload_bytes: int = 0,
    metadata: Mapping[str, str] | None = None,
    idempotency_key: str | None = None,
) -> RequestRecord:
    """Build a failure record from an exception, extracting only typed facts.

    ``str(error)`` is never copied into the record. A provider message can
    contain a fragment of the prompt, an echoed credential, or arbitrary hostile
    content (the mission's "logger receives hostile content" case); the typed
    kind plus the status code is what an operator needs, and it is all that is
    safe to persist.
    """

    kind: LLMErrorKind | None = None
    status_code: int | None = None
    retryable: bool | None = None
    if isinstance(error, LLMError):
        kind = error.kind
        status_code = error.status_code
        retryable = error.retryable
        provider = provider or error.provider
        model = model or error.model
    return RequestRecord(
        request_id=request_id,
        caller=caller,
        purpose=purpose,
        operation=operation,
        outcome=outcome,
        provider=provider,
        model=model,
        error_kind=kind,
        status_code=status_code,
        retryable=retryable,
        timings=timings or Timings(total_seconds=max(0.0, time.monotonic() - started_at)),
        policy=policy,
        attempts=tuple(attempts),
        started_at=started_at,
        ended_at=time.monotonic(),
        prompt_chars=prompt_chars,
        payload_bytes=payload_bytes,
        metadata=sanitize_metadata(metadata),
        idempotency_key=idempotency_key,
    )
