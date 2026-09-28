"""Typed, semantic LLM failure model (W2 — Global LLM Gateway, LAW 3).

The single rule this module exists to enforce:

    **A provider failure is classified by type and status, never by searching
    free-form model text or exception messages for keywords.**

Before W2 the repository detected rate limits with
``any(kw in str(error).lower() for kw in ("429", "rate limit", "quota", ...))``
in :mod:`nexus_ai_agent.llm.fallback_provider`. That is unsound in both
directions: a perfectly successful model answer that happens to mention "429"
triggers degradation, and a real throttling failure whose message is localised
or reworded does not. ``RouterExhaustedError`` even went so far as to *craft*
its own message so the substring scanner downstream would match it — the
taxonomy was being forged to satisfy a parser instead of being read from the
provider.

Every failure that leaves the gateway is an :class:`LLMError` carrying an
:class:`LLMErrorKind`. Retry, fallback, circuit and observability decisions read
``kind``/``status_code`` only. No production module in ``llm/`` inspects error
text; ``tests/architecture/test_llm_gateway_authority.py`` fails the build if a
substring-based classifier reappears.

Error taxonomy
--------------
The thirteen kinds required by the W2 mission, plus four that only exist
because the gateway itself can refuse or run out of budget:

=================================  ========  ========  ==========================
kind                               retry     fallback  primary source
=================================  ========  ========  ==========================
``AUTHENTICATION``                 no        yes       HTTP 401/403
``INVALID_REQUEST``                no        no        HTTP 400 (malformed by us)
``CONTEXT_LIMIT``                  no        no        HTTP 400 token/context size
``RATE_LIMITED``                   yes       yes       HTTP 429, short Retry-After
``QUOTA_EXHAUSTED``                no        yes       HTTP 429, drained window
``TRANSIENT_PROVIDER``             yes       yes       HTTP 500/502/503/504
``UPSTREAM_TIMEOUT``               yes       yes       read/connect/pool timeout
``NETWORK``                        yes       yes       connect/DNS/protocol error
``CANCELLED``                      no        no        caller withdrew the request
``MALFORMED_RESPONSE``             no        no        unusable provider payload
``POLICY_REFUSAL``                 no        no        local policy denied it
``UNSUPPORTED_CAPABILITY``         no        yes       adapter cannot do this
``GATEWAY_INTERNAL``               no        no        bug inside the gateway
---------------------------------  --------  --------  --------------------------
``CONTENT_BLOCKED``                no        no        provider safety/recitation
``DEADLINE_EXCEEDED``              no        no        total budget consumed
``OVERLOADED``                     no        no        bounded admission rejected
``GATEWAY_CLOSED``                 no        no        authority is shutting down
``STRUCTURED_OUTPUT_INVALID``      no        no        contract validation failed
=================================  ========  ========  ==========================

Why ``CONTENT_BLOCKED`` is *not* fallback-eligible: a safety/recitation block is
a decision about the content, not about the provider. Silently re-asking a
second provider for the same blocked content launders a moderation decision
through infrastructure — LAW 8 forbids exactly that.

Why ``QUOTA_EXHAUSTED`` is *not* retryable but *is* fallback-eligible: a drained
daily window does not refill in the seconds a retry budget covers, so retrying
burns the caller's deadline for nothing (LAW 7 — every retry needs an
engineering reason). Moving to a provider with its own separate quota is the
only response that can succeed. Google's own guidance is the same: retry the
transient codes (408/429/5xx) and never the client errors
(https://ai.google.dev/gemini-api/docs/troubleshooting#retry-strategy).

Why ``AUTHENTICATION`` *is* fallback-eligible: an invalid or revoked key is a
property of one credential, and the routing chain may hold a different
provider with a different credential. It is never retried against the same
route — that would just hammer a rejected key.

Compatibility
-------------
``LLMError`` subclasses :class:`RuntimeError`, so pre-existing
``except RuntimeError`` call sites keep working (LAW 12). Two attribute
spellings of the HTTP status are exposed on purpose:

* ``status_code`` — the structured attribute the hardened
  :class:`~nexus_ai_agent.features.request_queue.GeminiRequestQueue` classifier
  already reads (it deliberately refuses to parse messages);
* ``status`` — the spelling used by the ``LLM-FALLBACK-001`` typed-error work
  (PR #93). This module supersedes that PR's five-kind draft with the full
  taxonomy while keeping its public names (``LLMError``, ``RETRYABLE_KINDS``,
  ``is_retryable``) so a rebase reconciles instead of diverging.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

__all__ = [
    "LLMError",
    "LLMErrorKind",
    "RETRYABLE_KINDS",
    "FALLBACK_ELIGIBLE_KINDS",
    "AuthenticationError",
    "CancelledByCallerError",
    "ContentBlockedError",
    "ContextLimitError",
    "DeadlineExceededError",
    "GatewayClosedError",
    "GatewayInternalError",
    "InvalidRequestError",
    "MalformedResponseError",
    "NetworkError",
    "OverloadedError",
    "PolicyRefusalError",
    "QuotaExhaustedError",
    "RateLimitedError",
    "StructuredOutputInvalidError",
    "TransientProviderError",
    "UnsupportedCapabilityError",
    "UpstreamTimeoutError",
    "classify_kind",
    "is_retryable",
]


class LLMErrorKind(str, Enum):
    """Semantic failure classes. Values are wire-stable observability labels."""

    AUTHENTICATION = "authentication_failure"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LIMIT = "context_token_limit"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TRANSIENT_PROVIDER = "transient_provider_failure"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    NETWORK = "network_failure"
    CANCELLED = "cancelled"
    MALFORMED_RESPONSE = "malformed_provider_response"
    POLICY_REFUSAL = "policy_refusal"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    GATEWAY_INTERNAL = "internal_gateway_failure"
    # Gateway-owned extensions (see module docstring for the rationale of each).
    CONTENT_BLOCKED = "content_blocked"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    OVERLOADED = "gateway_overloaded"
    GATEWAY_CLOSED = "gateway_closed"
    STRUCTURED_OUTPUT_INVALID = "structured_output_invalid"


#: Kinds for which repeating the *same* request to the *same* route can succeed.
#: Everything else fails on the first attempt: retrying it burns the caller's
#: deadline and, for a rate limit or a dead upstream, amplifies the outage.
RETRYABLE_KINDS: frozenset[LLMErrorKind] = frozenset(
    {
        LLMErrorKind.RATE_LIMITED,
        LLMErrorKind.TRANSIENT_PROVIDER,
        LLMErrorKind.UPSTREAM_TIMEOUT,
        LLMErrorKind.NETWORK,
    }
)

#: Kinds where a *different* route can plausibly succeed. ``CONTENT_BLOCKED``
#: and ``CANCELLED`` are deliberately absent (LAW 8, LAW 5).
FALLBACK_ELIGIBLE_KINDS: frozenset[LLMErrorKind] = frozenset(
    {
        LLMErrorKind.AUTHENTICATION,
        LLMErrorKind.RATE_LIMITED,
        LLMErrorKind.QUOTA_EXHAUSTED,
        LLMErrorKind.TRANSIENT_PROVIDER,
        LLMErrorKind.UPSTREAM_TIMEOUT,
        LLMErrorKind.NETWORK,
        LLMErrorKind.UNSUPPORTED_CAPABILITY,
    }
)

#: HTTP status → kind. Derived from the provider's own documented status
#: semantics (https://ai.google.dev/gemini-api/docs/troubleshooting and
#: RFC 9110 §15), not from message text.
_STATUS_KINDS: Mapping[int, LLMErrorKind] = {
    400: LLMErrorKind.INVALID_REQUEST,
    401: LLMErrorKind.AUTHENTICATION,
    403: LLMErrorKind.AUTHENTICATION,
    404: LLMErrorKind.INVALID_REQUEST,
    408: LLMErrorKind.UPSTREAM_TIMEOUT,
    429: LLMErrorKind.RATE_LIMITED,
    500: LLMErrorKind.TRANSIENT_PROVIDER,
    502: LLMErrorKind.TRANSIENT_PROVIDER,
    503: LLMErrorKind.TRANSIENT_PROVIDER,
    504: LLMErrorKind.UPSTREAM_TIMEOUT,
}

#: Fields a caller may attach as structured context. Bounded so a hostile or
#: enormous provider body cannot be laundered into a log record verbatim.
_MAX_DETAIL_CHARS = 400


def classify_kind(status_code: int | None) -> LLMErrorKind:
    """Map an HTTP status to its semantic kind.

    Unknown 4xx → ``INVALID_REQUEST`` (our request was rejected; retrying the
    same bytes cannot help). Unknown 5xx → ``TRANSIENT_PROVIDER``. No status at
    all → ``GATEWAY_INTERNAL``, because an unmapped failure inside the gateway
    must never be dressed up as a provider problem.
    """

    if status_code is None:
        return LLMErrorKind.GATEWAY_INTERNAL
    mapped = _STATUS_KINDS.get(status_code)
    if mapped is not None:
        return mapped
    if 400 <= status_code < 500:
        return LLMErrorKind.INVALID_REQUEST
    if 500 <= status_code < 600:
        return LLMErrorKind.TRANSIENT_PROVIDER
    return LLMErrorKind.GATEWAY_INTERNAL


class LLMError(RuntimeError):
    """One typed LLM failure. Every field is machine-readable.

    ``detail`` holds a *bounded, non-secret* human hint (a provider error code,
    a status line) and is truncated on construction. It never contains the
    prompt, the API key, or the response body: the gateway's observability layer
    is the only place that renders failures, and it renders ``kind`` plus
    ``detail``, not messages scraped from payloads.
    """

    #: Semantic class of this failure.
    kind: LLMErrorKind

    def __init__(
        self,
        message: str,
        *,
        kind: LLMErrorKind = LLMErrorKind.GATEWAY_INTERNAL,
        status_code: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        request_id: str | None = None,
        attempt: int | None = None,
        retry_after: float | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.provider = provider
        self.model = model
        self.request_id = request_id
        self.attempt = attempt
        self.retry_after = retry_after
        self.detail = None if detail is None else detail[:_MAX_DETAIL_CHARS]

    # ── policy facts ─────────────────────────────────────────────────
    @property
    def retryable(self) -> bool:
        """True when the same request to the same route may be attempted again."""

        return self.kind in RETRYABLE_KINDS

    @property
    def fallback_eligible(self) -> bool:
        """True when a *different* route is a legitimate response to this failure."""

        return self.kind in FALLBACK_ELIGIBLE_KINDS

    # ── compatibility aliases ────────────────────────────────────────
    @property
    def status(self) -> int | None:
        """Alias of :attr:`status_code` (spelling used by PR #93's contract)."""

        return self.status_code

    def safe_fields(self) -> dict[str, Any]:
        """Observability-safe projection. Never contains prompt or credential data."""

        return {
            "kind": self.kind.value,
            "status_code": self.status_code,
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "attempt": self.attempt,
            "retry_after": self.retry_after,
            "detail": self.detail,
        }

    def with_route(self, *, provider: str, model: str, request_id: str, attempt: int) -> LLMError:
        """Return a copy of this error stamped with the route that produced it.

        Adapters construct errors before they know the gateway's correlation id;
        stamping happens once, at the boundary, so every logged failure carries
        ``request_id`` (LAW 10) without the adapter having to thread it through.
        """

        clone = self.__class__(
            str(self),
            kind=self.kind,
            status_code=self.status_code,
            provider=provider or self.provider,
            model=model or self.model,
            request_id=request_id or self.request_id,
            attempt=attempt if attempt is not None else self.attempt,
            retry_after=self.retry_after,
            detail=self.detail,
        )
        clone.__cause__ = self.__cause__
        return clone


# ── one concrete class per kind ──────────────────────────────────────────
# Concrete subclasses exist so a caller can write ``except RateLimitedError``
# instead of matching on ``kind`` by hand, and so the gateway's own control flow
# is readable. Each pins its default kind; ``status_code`` stays explicit.


class AuthenticationError(LLMError):
    """Credential rejected (HTTP 401/403). Never retried; may change provider."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.AUTHENTICATION)
        super().__init__(message, **kwargs)


class InvalidRequestError(LLMError):
    """The request we built is not acceptable (HTTP 400/404). Fail closed."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.INVALID_REQUEST)
        super().__init__(message, **kwargs)


class ContextLimitError(LLMError):
    """Prompt + history exceed the model's context window. Trimming is the fix."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.CONTEXT_LIMIT)
        super().__init__(message, **kwargs)


class RateLimitedError(LLMError):
    """Provider throttled this caller (HTTP 429 with a short retry window)."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.RATE_LIMITED)
        super().__init__(message, **kwargs)


class QuotaExhaustedError(LLMError):
    """The provider's window budget (per-minute or per-day) is drained."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.QUOTA_EXHAUSTED)
        super().__init__(message, **kwargs)


class TransientProviderError(LLMError):
    """Upstream server failure (HTTP 500/502/503/504). Retryable."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.TRANSIENT_PROVIDER)
        super().__init__(message, **kwargs)


class UpstreamTimeoutError(LLMError):
    """The provider did not answer inside the per-attempt timeout."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.UPSTREAM_TIMEOUT)
        super().__init__(message, **kwargs)


class NetworkError(LLMError):
    """Transport-level failure: DNS, connect, reset, protocol error."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.NETWORK)
        super().__init__(message, **kwargs)


class CancelledByCallerError(LLMError):
    """The caller withdrew the request. Sacred: never retried, never fallen back."""

    def __init__(self, message: str = "LLM request cancelled by caller", **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.CANCELLED)
        super().__init__(message, **kwargs)


class MalformedResponseError(LLMError):
    """The provider answered with something unusable. Fail closed (never retry)."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.MALFORMED_RESPONSE)
        super().__init__(message, **kwargs)


class PolicyRefusalError(LLMError):
    """Local policy denied the request (privacy, consent, forbidden route)."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.POLICY_REFUSAL)
        super().__init__(message, **kwargs)


class UnsupportedCapabilityError(LLMError):
    """The selected route cannot serve this operation/modality."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.UNSUPPORTED_CAPABILITY)
        super().__init__(message, **kwargs)


class GatewayInternalError(LLMError):
    """A defect inside the gateway. Loud, typed, and never blamed on the provider."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.GATEWAY_INTERNAL)
        super().__init__(message, **kwargs)


class ContentBlockedError(LLMError):
    """Provider safety/recitation/PII block. A content decision, not an outage."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.CONTENT_BLOCKED)
        super().__init__(message, **kwargs)


class DeadlineExceededError(LLMError):
    """The caller's total budget was consumed (queue wait + retries + attempts)."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.DEADLINE_EXCEEDED)
        super().__init__(message, **kwargs)


class OverloadedError(LLMError):
    """Bounded admission rejected the request: the gateway is saturated."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.OVERLOADED)
        super().__init__(message, **kwargs)


class GatewayClosedError(LLMError):
    """The authority is closed/closing; it accepts no new work."""

    def __init__(self, message: str = "LLM gateway is closed", **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.GATEWAY_CLOSED)
        super().__init__(message, **kwargs)


class StructuredOutputInvalidError(LLMError):
    """The provider answered, but the answer violates the requested contract."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.STRUCTURED_OUTPUT_INVALID)
        super().__init__(message, **kwargs)


def is_retryable(exc: BaseException) -> bool:
    """True only for a typed :class:`LLMError` whose kind is retryable.

    Anything untyped is *not* retryable: an unknown exception is a defect, and
    guessing "probably transient" from its shape is the substring-scanning
    failure mode this module exists to remove.
    """

    return isinstance(exc, LLMError) and exc.retryable
