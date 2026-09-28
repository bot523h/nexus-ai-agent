"""The LLM Gateway contract (W2) — what crosses the authority boundary.

One request type in, one response type out, one typed error channel for
failure. Everything a caller needs to declare and everything the gateway needs
to report lives here, and nothing else: no provider names in payloads, no HTTP
shapes, no retry knobs.

Deliberate size limit (the mission's "do not make the contract a giant for no
reason"): the request carries *what* to do and *on whose behalf*, never *how* to
do it. ``how`` — auth header, URL, wire format, error mapping, usage extraction
— belongs to the provider adapter and is invisible here (LAW 9).

Field naming follows the OpenTelemetry GenAI semantic conventions where one
exists (``gen_ai.operation.name``, ``gen_ai.request.model``,
``gen_ai.usage.input_tokens``), so a future OTel exporter is a mapping, not a
redesign:
https://opentelemetry.io/docs/specs/semconv/gen-ai/
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4

__all__ = [
    "AttemptRecord",
    "Caller",
    "CallerCategory",
    "ContentPart",
    "FinishReason",
    "GenerationParams",
    "LLMOperation",
    "LLMPriority",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "Modality",
    "PolicyOutcome",
    "Timings",
    "Usage",
    "UsageSource",
    "new_request_id",
]

#: Monotonic clock used for every deadline and duration. Injectable in tests so
#: timing behaviour is deterministic rather than wall-clock dependent.
Clock = Callable[[], float]

MONOTONIC: Clock = time.monotonic


def new_request_id() -> str:
    """Correlation id for one logical request (LAW 10).

    ``uuid4().hex`` — globally unique, unguessable, and free of any user data,
    so it is safe to log, to return to a caller, and to use as an idempotency
    component. Truncated to 16 hex chars for log readability while keeping
    64 bits of entropy (collision-free at any scale this process reaches).
    """

    return uuid4().hex[:16]


class LLMOperation(str, Enum):
    """What kind of model work this is (OTel ``gen_ai.operation.name``)."""

    CHAT = "chat"
    TEXT_COMPLETION = "text_completion"
    GENERATE_CONTENT = "generate_content"  # multimodal in / text out
    EMBEDDINGS = "embeddings"


class Modality(str, Enum):
    """Input/output modalities a request needs. Used for capability negotiation."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class CallerCategory(str, Enum):
    """Coarse caller class — the dimension operators actually filter on.

    Kept small and closed on purpose: a free-form ``caller`` string is also
    carried for the precise site, but dashboards and quota policy need a bounded
    cardinality or every metric becomes a memory leak.
    """

    AGENT = "agent"
    SURFACE = "surface"  # Telegram/API command handlers
    SUMMARIZER = "summarizer"
    PLANNER = "planner"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    CREATIVE = "creative"
    BACKGROUND_JOB = "background_job"
    SYSTEM = "system"


class LLMPriority(IntEnum):
    """Scheduling tier. Lower value wins.

    The integer values are deliberately identical to
    :class:`nexus_ai_agent.features.request_queue.Priority` so a request can
    cross either scheduler without translation, and so the gateway's fairness
    story composes with the hardened legacy queue instead of contradicting it.
    """

    OWNER = 0
    REFERRAL_BONUS = 1
    NORMAL = 2
    LOW = 3


class FinishReason(str, Enum):
    """Why generation stopped. ``UNKNOWN`` is honest; a guess is not."""

    STOP = "stop"
    MAX_TOKENS = "max_tokens"
    CONTENT_BLOCKED = "content_blocked"
    ERROR = "error"
    UNKNOWN = "unknown"


class UsageSource(str, Enum):
    """Where a usage number came from (LAW 11 — never invent usage)."""

    PROVIDER = "provider"  # reported by the provider in its response
    UNKNOWN = "unknown"  # the provider did not report it; we do not guess


@dataclass(frozen=True)
class Caller:
    """Who is asking. Required on every request: an anonymous LLM call cannot
    be attributed, rate-limited fairly, or debugged."""

    category: CallerCategory
    name: str
    tenant_id: int = 0

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Caller.name must be a non-empty identifier")
        if len(self.name) > 128:
            raise ValueError("Caller.name must be at most 128 characters")

    @property
    def label(self) -> str:
        return f"{self.category.value}:{self.name}"


@dataclass(frozen=True)
class Message:
    """One turn of chat history.

    ``content`` is the turn's text. ``parts`` carries any non-text payload that
    belongs to *this* turn (an inline image in a vision turn, an audio clip), so
    a multi-turn history can be forwarded losslessly instead of being flattened
    into text. Flattening would silently drop user input; an adapter that cannot
    express a part refuses the request with a typed ``UNSUPPORTED_CAPABILITY``.
    """

    role: str  # "system" | "user" | "model" | "assistant" | "tool"
    content: str = ""
    parts: tuple[ContentPart, ...] = ()

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "model", "assistant", "tool"}:
            raise ValueError(f"unsupported message role: {self.role!r}")

    @property
    def modalities(self) -> frozenset[Modality]:
        return frozenset({part.modality for part in self.parts} | {Modality.TEXT})

    @property
    def size_bytes(self) -> int:
        return sum(part.size_bytes for part in self.parts)


@dataclass(frozen=True)
class ContentPart:
    """A non-text input part (image/audio bytes) or a text part.

    Bytes are carried, never persisted or logged: the observability layer
    records ``len(part.data)`` and the mime type only.
    """

    mime_type: str
    data: bytes = b""
    text: str | None = None

    @property
    def modality(self) -> Modality:
        top = self.mime_type.split("/", 1)[0].lower()
        if top == "image":
            return Modality.IMAGE
        if top == "audio":
            return Modality.AUDIO
        if top == "video":
            return Modality.VIDEO
        return Modality.TEXT

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class GenerationParams:
    """Sampling/output knobs. All optional: the adapter applies its own defaults.

    These are *caller intent*, not provider wire format. An adapter that cannot
    express one of them must ignore it deliberately (and may say so in the
    response), never silently reinterpret it.
    """

    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    response_mime_type: str | None = None
    stop_sequences: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least 1")
        if self.temperature is not None and not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be within [0.0, 2.0]")
        if self.top_p is not None and not 0.0 < self.top_p <= 1.0:
            raise ValueError("top_p must be within (0.0, 1.0]")
        if self.top_k is not None and self.top_k < 1:
            raise ValueError("top_k must be at least 1")


def _empty_metadata() -> Mapping[str, str]:
    """Shared immutable empty mapping (a frozen dataclass default must be a factory)."""

    return MappingProxyType({})


@dataclass(frozen=True)
class LLMRequest:
    """One unit of LLM work submitted to the authority.

    ``prompt``/``system`` is the simple path used by almost every caller;
    ``messages`` carries real multi-turn history; ``parts`` carries multimodal
    payloads. An adapter declares which of these it can serve through
    ``supported_operations``/``supported_modalities`` and the gateway refuses a
    mismatch with a typed ``UNSUPPORTED_CAPABILITY`` error rather than quietly
    flattening the request into something else.
    """

    caller: Caller
    purpose: str = "chat"
    operation: LLMOperation = LLMOperation.CHAT
    prompt: str = ""
    system: str | None = None
    messages: tuple[Message, ...] = ()
    parts: tuple[ContentPart, ...] = ()
    model: str | None = None
    provider: str | None = None
    priority: LLMPriority = LLMPriority.NORMAL
    deadline_seconds: float | None = None
    #: A caller that pins a provider often means "this provider or nothing" —
    #: a summarizer must not silently receive a locally faked answer. Setting
    #: this to ``False`` makes the pin a hard constraint: on exhaustion the
    #: caller gets the typed failure instead of a different provider's answer.
    #: ``True`` (default) leaves the decision to :class:`FallbackPolicy`.
    allow_fallback: bool = True
    generation: GenerationParams = field(default_factory=GenerationParams)
    #: Optional contract validator for structured output. Given the raw model
    #: text it returns the validated object or raises. Validation lives at the
    #: gateway so provider-specific parsing never spreads into callers, while
    #: the *shape* of the contract stays the caller's own decision.
    output_validator: Callable[[str], Any] | None = None
    #: Caller-supplied deduplication key. Two requests with the same key inside
    #: the gateway's idempotency window share one provider execution.
    idempotency_key: str | None = None
    #: External cancellation signal. Distinct from ``asyncio.CancelledError``:
    #: a caller can withdraw a request without cancelling its own task.
    cancellation: Any | None = None  # asyncio.Event | None (avoid importing asyncio here)
    #: Free-form, log-safe provenance (W3 memory provenance, trace ids, prompt
    #: version). Values are rendered into observability records, so callers must
    #: not put prompt text or credentials here; the gateway redacts anyway.
    metadata: Mapping[str, str] = field(default_factory=_empty_metadata)

    def __post_init__(self) -> None:
        if not self.purpose or not self.purpose.strip():
            raise ValueError("LLMRequest.purpose must be non-empty")
        if len(self.purpose) > 64:
            raise ValueError("LLMRequest.purpose must be at most 64 characters")
        if self.deadline_seconds is not None and self.deadline_seconds < 0:
            raise ValueError("deadline_seconds cannot be negative")
        if self.model is not None and not self.model.strip():
            raise ValueError("model must be non-empty when provided")
        if self.provider is not None and not self.provider.strip():
            raise ValueError("provider must be non-empty when provided")
        if self.operation is LLMOperation.EMBEDDINGS and not (self.prompt or self.messages):
            raise ValueError("an embeddings request needs text to embed")
        if self.operation is not LLMOperation.EMBEDDINGS and not (
            self.prompt or self.messages or self.parts
        ):
            raise ValueError("a generation request needs a prompt, messages, or parts")
        for key in self.metadata:
            if len(key) > 64:
                raise ValueError("metadata keys must be at most 64 characters")

    def all_parts(self) -> tuple[ContentPart, ...]:
        """Every content part in the request, top-level and per-message."""

        return tuple(self.parts) + tuple(p for m in self.messages for p in m.parts)

    @property
    def modalities(self) -> frozenset[Modality]:
        found = {part.modality for part in self.all_parts()}
        found.add(Modality.TEXT)
        return frozenset(found)

    def has_multimodal_input(self) -> bool:
        return any(part.modality is not Modality.TEXT for part in self.all_parts())

    def prompt_size_chars(self) -> int:
        """Character volume of the request payload — logged instead of the payload."""

        total = len(self.prompt) + len(self.system or "")
        total += sum(len(m.content) for m in self.messages)
        total += sum(len(p.text or "") for p in self.all_parts())
        return total

    def payload_bytes(self) -> int:
        return sum(p.size_bytes for p in self.all_parts())


@dataclass(frozen=True)
class Usage:
    """Token/cost accounting. Truthful or unknown — never invented (LAW 11).

    ``estimated_cost_usd`` is populated only when the gateway holds a price for
    the answering model *and* the provider reported token counts. Either one
    missing leaves it ``None``: a plausible-looking number with no provenance is
    worse than an honest gap, because it gets believed.
    """

    source: UsageSource = UsageSource.UNKNOWN
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: float | None = None
    #: Model the provider says actually answered (may differ from the request).
    reported_model: str | None = None

    @property
    def is_known(self) -> bool:
        return self.source is UsageSource.PROVIDER

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "reported_model": self.reported_model,
        }


UNKNOWN_USAGE = Usage(source=UsageSource.UNKNOWN)


@dataclass(frozen=True)
class Timings:
    """Where the wall-clock went, per phase (LAW 10).

    Phases are additive and cover the whole life of the request:
    ``total ≈ queued + rate_waited + executed + backoff``. A caller that only
    sees ``total`` cannot tell saturation from a slow provider; these four make
    that distinction a query instead of a guess.
    """

    queued_seconds: float = 0.0
    rate_waited_seconds: float = 0.0
    executed_seconds: float = 0.0
    backoff_seconds: float = 0.0
    total_seconds: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "queued_seconds": round(self.queued_seconds, 4),
            "rate_waited_seconds": round(self.rate_waited_seconds, 4),
            "executed_seconds": round(self.executed_seconds, 4),
            "backoff_seconds": round(self.backoff_seconds, 4),
            "total_seconds": round(self.total_seconds, 4),
        }


@dataclass(frozen=True)
class AttemptRecord:
    """One provider attempt. Kept so retries are explainable, not just counted."""

    index: int
    provider: str
    model: str
    started_at: float
    duration_seconds: float
    outcome: str  # "success" | "error" | "cancelled"
    error_kind: str | None = None
    status_code: int | None = None


@dataclass(frozen=True)
class PolicyOutcome:
    """What the policy layer decided, and why the answer looks the way it does.

    ``degraded`` is the LAW 8 flag: a fallback happened, it is recorded on the
    response, and it is never silent. ``fallback_from`` names the route that
    failed so an operator can see the hop, not just the landing.
    """

    route_provider: str
    route_model: str
    attempts: int = 0
    retries: int = 0
    fallback_used: bool = False
    fallback_from: str | None = None
    degraded: bool = False
    admission_rejected: bool = False
    circuit_open: bool = False
    rate_limited_locally: bool = False
    idempotency_hit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_provider": self.route_provider,
            "route_model": self.route_model,
            "attempts": self.attempts,
            "retries": self.retries,
            "fallback_used": self.fallback_used,
            "fallback_from": self.fallback_from,
            "degraded": self.degraded,
            "admission_rejected": self.admission_rejected,
            "circuit_open": self.circuit_open,
            "rate_limited_locally": self.rate_limited_locally,
            "idempotency_hit": self.idempotency_hit,
        }


@dataclass(frozen=True)
class LLMResponse:
    """One successful (possibly degraded) answer.

    ``text`` is the model output. ``structured`` is the validated object when
    the request carried an ``output_validator`` — validation already happened,
    so a caller never re-parses provider text.
    """

    request_id: str
    text: str
    provider: str
    model: str
    operation: LLMOperation
    caller: Caller
    purpose: str
    usage: Usage = UNKNOWN_USAGE
    finish_reason: FinishReason = FinishReason.UNKNOWN
    policy: PolicyOutcome | None = None
    timings: Timings = field(default_factory=Timings)
    attempts: Sequence[AttemptRecord] = ()
    structured: Any | None = None
    #: Embeddings result when ``operation is EMBEDDINGS``.
    embedding: tuple[float, ...] | None = None

    @property
    def degraded(self) -> bool:
        """True when a fallback/degraded route produced this answer (LAW 8)."""

        return bool(self.policy is not None and self.policy.degraded)

    def as_record_dict(self) -> dict[str, Any]:
        """Log-safe projection: identity, routing, timings, outcome, usage.

        Contains no prompt text, no model output, no credential material.
        """

        return {
            "request_id": self.request_id,
            "caller": self.caller.label,
            "tenant_id": self.caller.tenant_id,
            "purpose": self.purpose,
            "operation": self.operation.value,
            "provider": self.provider,
            "model": self.model,
            "outcome": "degraded_success" if self.degraded else "success",
            "finish_reason": self.finish_reason.value,
            "usage": self.usage.as_dict(),
            "timings": self.timings.as_dict(),
            "policy": self.policy.as_dict() if self.policy is not None else None,
        }
