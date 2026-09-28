"""W2 — the typed LLM error taxonomy (LAW 3: typed truth, never substring truth).

These tests pin the *decision table*, not the wording. Every retry, backoff,
fallback and circuit-breaker decision in the repository is derived from
``LLMError.kind``; if a kind moves between sets, a behaviour changes somewhere,
so the sets themselves are the contract.
"""

from __future__ import annotations

import httpx
import pytest

from nexus_ai_agent.llm.errors import (
    FALLBACK_ELIGIBLE_KINDS,
    RETRYABLE_KINDS,
    AuthenticationError,
    CancelledByCallerError,
    ContentBlockedError,
    ContextLimitError,
    DeadlineExceededError,
    GatewayClosedError,
    GatewayInternalError,
    InvalidRequestError,
    LLMError,
    LLMErrorKind,
    MalformedResponseError,
    NetworkError,
    OverloadedError,
    PolicyRefusalError,
    QuotaExhaustedError,
    RateLimitedError,
    StructuredOutputInvalidError,
    TransientProviderError,
    UnsupportedCapabilityError,
    UpstreamTimeoutError,
    classify_kind,
    is_retryable,
)

#: Every subclass must declare a kind that matches its name's meaning, so a
#: caller can rely on ``isinstance`` and ``kind`` agreeing.
SUBCLASS_KINDS = {
    AuthenticationError: LLMErrorKind.AUTHENTICATION,
    InvalidRequestError: LLMErrorKind.INVALID_REQUEST,
    ContextLimitError: LLMErrorKind.CONTEXT_LIMIT,
    RateLimitedError: LLMErrorKind.RATE_LIMITED,
    QuotaExhaustedError: LLMErrorKind.QUOTA_EXHAUSTED,
    TransientProviderError: LLMErrorKind.TRANSIENT_PROVIDER,
    UpstreamTimeoutError: LLMErrorKind.UPSTREAM_TIMEOUT,
    NetworkError: LLMErrorKind.NETWORK,
    CancelledByCallerError: LLMErrorKind.CANCELLED,
    MalformedResponseError: LLMErrorKind.MALFORMED_RESPONSE,
    PolicyRefusalError: LLMErrorKind.POLICY_REFUSAL,
    UnsupportedCapabilityError: LLMErrorKind.UNSUPPORTED_CAPABILITY,
    GatewayInternalError: LLMErrorKind.GATEWAY_INTERNAL,
    ContentBlockedError: LLMErrorKind.CONTENT_BLOCKED,
    DeadlineExceededError: LLMErrorKind.DEADLINE_EXCEEDED,
    OverloadedError: LLMErrorKind.OVERLOADED,
    GatewayClosedError: LLMErrorKind.GATEWAY_CLOSED,
    StructuredOutputInvalidError: LLMErrorKind.STRUCTURED_OUTPUT_INVALID,
}


@pytest.mark.parametrize(
    ("cls", "kind"), sorted(SUBCLASS_KINDS.items(), key=lambda kv: kv[1].value)
)
def test_each_subclass_declares_its_own_kind(cls: type[LLMError], kind: LLMErrorKind) -> None:
    error = cls("boom")
    assert isinstance(error, LLMError)
    assert error.kind is kind
    # Every kind is a wire-stable lowercase label, safe to put in a metric key.
    assert error.kind.value == kind.value
    assert kind.value.replace("_", "").isalnum()


def test_every_kind_has_exactly_one_subclass() -> None:
    """No kind is unreachable, and none is claimed by two classes."""

    declared = {kind for kind in SUBCLASS_KINDS.values()}
    missing = sorted(kind.value for kind in set(LLMErrorKind) - declared)
    assert declared == set(LLMErrorKind), f"kinds without a subclass: {missing}"
    assert len(declared) == len(SUBCLASS_KINDS)


# ── the retry decision table ────────────────────────────────────────────


def test_retryable_kinds_are_exactly_the_transient_ones() -> None:
    """Google's own guidance: retry 408/429/5xx and network timeouts only.

    https://ai.google.dev/gemini-api/docs/troubleshooting — "never retry
    400/402/403". A retry on those burns the caller's deadline and amplifies
    the outage without any chance of a different answer.
    """

    assert RETRYABLE_KINDS == frozenset(
        {
            LLMErrorKind.RATE_LIMITED,
            LLMErrorKind.TRANSIENT_PROVIDER,
            LLMErrorKind.UPSTREAM_TIMEOUT,
            LLMErrorKind.NETWORK,
        }
    )


@pytest.mark.parametrize(
    "kind",
    [
        LLMErrorKind.AUTHENTICATION,
        LLMErrorKind.INVALID_REQUEST,
        LLMErrorKind.CONTEXT_LIMIT,
        LLMErrorKind.CONTENT_BLOCKED,
        LLMErrorKind.CANCELLED,
        LLMErrorKind.MALFORMED_RESPONSE,
        LLMErrorKind.POLICY_REFUSAL,
        LLMErrorKind.UNSUPPORTED_CAPABILITY,
        LLMErrorKind.STRUCTURED_OUTPUT_INVALID,
        LLMErrorKind.DEADLINE_EXCEEDED,
        LLMErrorKind.OVERLOADED,
    ],
)
def test_non_transient_kinds_are_never_retryable(kind: LLMErrorKind) -> None:
    assert LLMError("x", kind=kind).retryable is False
    assert is_retryable(LLMError("x", kind=kind)) is False


def test_retryable_set_is_a_subset_of_fallback_eligible() -> None:
    """If the same route may be retried, a different route is also legitimate.

    The converse is deliberately false: ``AUTHENTICATION`` and
    ``QUOTA_EXHAUSTED`` cannot be fixed by retrying the same credentials, but a
    different provider can still serve the caller.
    """

    assert RETRYABLE_KINDS <= FALLBACK_ELIGIBLE_KINDS
    assert LLMErrorKind.AUTHENTICATION in FALLBACK_ELIGIBLE_KINDS - RETRYABLE_KINDS
    assert LLMErrorKind.QUOTA_EXHAUSTED in FALLBACK_ELIGIBLE_KINDS - RETRYABLE_KINDS


def test_content_blocked_and_cancelled_are_never_fallback_eligible() -> None:
    """LAW 5 / LAW 8: a cancellation is honoured, a block is not laundered.

    Falling back on ``CONTENT_BLOCKED`` would route a blocked prompt to a
    provider with weaker safety filters — the same request, a different answer,
    and no record of why. Falling back on ``CANCELLED`` would keep spending
    quota after the caller withdrew.
    """

    assert LLMErrorKind.CONTENT_BLOCKED not in FALLBACK_ELIGIBLE_KINDS
    assert LLMErrorKind.CANCELLED not in FALLBACK_ELIGIBLE_KINDS
    assert ContentBlockedError("safety").fallback_eligible is False
    assert CancelledByCallerError("gone").retryable is False


# ── status → kind: derived from documented HTTP semantics ───────────────


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, LLMErrorKind.INVALID_REQUEST),
        (401, LLMErrorKind.AUTHENTICATION),
        (403, LLMErrorKind.AUTHENTICATION),
        (404, LLMErrorKind.INVALID_REQUEST),
        (408, LLMErrorKind.UPSTREAM_TIMEOUT),
        (429, LLMErrorKind.RATE_LIMITED),
        (500, LLMErrorKind.TRANSIENT_PROVIDER),
        (502, LLMErrorKind.TRANSIENT_PROVIDER),
        (503, LLMErrorKind.TRANSIENT_PROVIDER),
        (504, LLMErrorKind.UPSTREAM_TIMEOUT),
    ],
)
def test_documented_statuses_classify_by_number(status: int, kind: LLMErrorKind) -> None:
    assert classify_kind(status) is kind


def test_unknown_status_is_not_guessed_as_retryable() -> None:
    """An undocumented status must not land in the retryable set by accident."""

    assert classify_kind(418) not in RETRYABLE_KINDS
    assert classify_kind(None) not in RETRYABLE_KINDS
    # 429 and 503 remain retryable; 418 does not become one of them by proximity.
    assert classify_kind(429) in RETRYABLE_KINDS
    assert classify_kind(503) in RETRYABLE_KINDS


def test_is_retryable_refuses_to_guess_about_unmapped_transport_exceptions() -> None:
    """``is_retryable`` is typed-only, on purpose.

    A raw ``httpx.ReadTimeout`` carries no ``kind``, so the taxonomy declines to
    opine. The type-based evidence is read one layer down, in
    ``map_transport_error``, which turns it into an ``UPSTREAM_TIMEOUT`` — and
    *that* is retryable. Splitting the two keeps "what happened" (adapter, from
    the exception class) separate from "may we repeat it" (policy, from the
    kind), so neither has to guess.
    """

    from nexus_ai_agent.llm.gateway.adapters import map_transport_error

    assert is_retryable(UpstreamTimeoutError("slow")) is True
    assert is_retryable(httpx.ReadTimeout("slow")) is False
    assert is_retryable(ValueError("bug")) is False
    assert is_retryable(KeyError("missing")) is False

    for transport_exc in (
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.WriteTimeout("slow"),
        httpx.PoolTimeout("slow"),
    ):
        mapped = map_transport_error(transport_exc, provider="gemini", model="m")
        assert mapped.kind is LLMErrorKind.UPSTREAM_TIMEOUT
        assert mapped.retryable is True
        assert is_retryable(mapped) is True

    mapped = map_transport_error(httpx.ConnectError("refused"), provider="gemini", model="m")
    assert mapped.kind is LLMErrorKind.NETWORK
    assert mapped.retryable is True


# ── the error carries evidence, and only evidence ───────────────────────


def test_with_route_stamps_correlation_without_losing_the_cause() -> None:
    original = RateLimitedError("throttled", status_code=429, detail="RESOURCE_EXHAUSTED")
    stamped = original.with_route(
        provider="gemini", model="gemini-2.0-flash", request_id="r1", attempt=2
    )

    assert stamped is not original
    assert stamped.kind is LLMErrorKind.RATE_LIMITED
    assert stamped.provider == "gemini"
    assert stamped.model == "gemini-2.0-flash"
    assert stamped.request_id == "r1"
    assert stamped.attempt == 2
    assert stamped.status_code == 429
    assert stamped.detail == "RESOURCE_EXHAUSTED"
    assert stamped.retryable is True


def test_with_route_never_overwrites_known_route_identity_with_blanks() -> None:
    original = NetworkError("dns", provider="gemini", model="m", request_id="kept", attempt=3)
    stamped = original.with_route(provider="", model="", request_id="", attempt=None)  # type: ignore[arg-type]
    assert stamped.provider == "gemini"
    assert stamped.model == "m"
    assert stamped.request_id == "kept"
    assert stamped.attempt == 3


def test_detail_is_bounded_so_one_error_cannot_become_a_log_flood() -> None:
    error = TransientProviderError("boom", detail="x" * 5000)
    assert error.detail is not None
    assert len(error.detail) <= 512


def test_safe_fields_never_carry_prompt_or_credential_material() -> None:
    """The projection used by every log line and metric key.

    A prompt would put user data in the log pipeline; an API key would put a
    credential there. Neither is representable in this shape at all.
    """

    error = RateLimitedError(
        "throttled while sending the user's secret diary entry",
        status_code=429,
        provider="gemini",
        model="gemini-2.0-flash",
        request_id="r1",
        attempt=1,
        retry_after=1.5,
        detail="RESOURCE_EXHAUSTED",
    )
    fields = error.safe_fields()
    assert set(fields) == {
        "kind",
        "status_code",
        "provider",
        "model",
        "request_id",
        "attempt",
        "retry_after",
        "detail",
    }
    assert "diary" not in str(fields)
    assert fields["kind"] == "rate_limited"
    assert fields["retry_after"] == 1.5


def test_status_alias_matches_pr93_spelling() -> None:
    """PR #93 introduced ``.status``; W2 keeps it as an alias, not a fork."""

    error = InvalidRequestError("bad", status_code=400)
    assert error.status == 400
    assert error.status_code == 400


def test_errors_are_runtime_errors_so_legacy_except_clauses_still_catch() -> None:
    """LAW 12: migrating a caller must not silently change its error handling."""

    error = QuotaExhaustedError("drained")
    assert isinstance(error, RuntimeError)
    caught: list[str] = []
    try:
        raise error
    except RuntimeError as exc:  # the pre-W2 catch-all shape
        caught.append(type(exc).__name__)
    assert caught == ["QuotaExhaustedError"]
