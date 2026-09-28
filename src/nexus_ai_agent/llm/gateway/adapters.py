"""Provider adapters — the only place provider detail is allowed to exist (LAW 9).

An adapter owns exactly five things and nothing else:

1. the auth header / credential transport,
2. the endpoint URL,
3. the provider wire format (request *and* response),
4. the mapping from provider failures to :class:`~nexus_ai_agent.llm.errors.LLMError`,
5. extraction of provider-reported usage.

The gateway never sees a Gemini payload, an HTTP status, a litellm
``ModelResponse``, or an ``x-goog-api-key`` header. It sees :class:`AdapterResult`
or a typed :class:`LLMError`. That is what makes a second provider a new file
instead of a new branch in every caller.

Structured classification, not substring scanning
-------------------------------------------------
Providers publish *machine-readable* failure fields: an HTTP status, and for
Gemini a gRPC-style ``error.status`` string such as ``RESOURCE_EXHAUSTED``,
``UNAUTHENTICATED``, ``DEADLINE_EXCEEDED``, ``FAILED_PRECONDITION``. Reading
those declared fields is structured classification. Searching a human-readable
``message`` for ``"429"`` or ``"rate limit"`` is not, and it is what this module
replaces. The distinction is enforced by
``tests/architecture/test_llm_gateway_authority.py``.

Where a provider genuinely offers no structured signal for a distinction we care
about (Gemini reports both "your request is malformed" and "your prompt is too
long" as ``400 INVALID_ARGUMENT``), the gateway refuses to guess: it reports
``INVALID_REQUEST`` and offers a *request-side* context gate
(``Route.context_window_chars``) that produces ``CONTEXT_LIMIT`` deterministically
before any bytes are sent. An honest coarse answer beats a fabricated precise one.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from nexus_ai_agent.llm.errors import (
    AuthenticationError,
    ContentBlockedError,
    GatewayInternalError,
    InvalidRequestError,
    LLMError,
    LLMErrorKind,
    MalformedResponseError,
    NetworkError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
    UnsupportedCapabilityError,
    UpstreamTimeoutError,
    classify_kind,
)
from nexus_ai_agent.llm.gateway.contract import (
    UNKNOWN_USAGE,
    FinishReason,
    LLMOperation,
    LLMRequest,
    Modality,
    Usage,
    UsageSource,
)
from nexus_ai_agent.llm.gateway.policy import Route, TimeoutBudget
from nexus_ai_agent.observability.logging import get_logger

__all__ = [
    "AdapterResult",
    "GeminiHttpAdapter",
    "LegacyProviderAdapter",
    "ProviderAdapter",
    "map_transport_error",
]

log = get_logger(__name__)


@dataclass(frozen=True)
class AdapterResult:
    """One successful provider answer, in gateway terms."""

    text: str = ""
    finish_reason: FinishReason = FinishReason.UNKNOWN
    usage: Usage = UNKNOWN_USAGE
    embedding: tuple[float, ...] | None = None
    #: What the provider says actually answered (may differ from what we asked).
    reported_model: str | None = None
    status_code: int | None = None
    #: Adapter-declared extras for observability (bounded, log-safe scalars).
    extras: Mapping[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    """The adapter seam. Small on purpose: execute, declare, close."""

    @property
    def name(self) -> str: ...

    @property
    def operations(self) -> frozenset[LLMOperation]: ...

    @property
    def modalities(self) -> frozenset[Modality]: ...

    async def execute(
        self,
        request: LLMRequest,
        route: Route,
        *,
        budget: TimeoutBudget,
        request_id: str,
        attempt: int,
    ) -> AdapterResult: ...

    async def aclose(self) -> None: ...


# ═══════════════════════════════════════════════════════════════════════════
# Transport error mapping (shared by every HTTP adapter)
# ═══════════════════════════════════════════════════════════════════════════


def map_transport_error(exc: Exception, *, provider: str, model: str) -> LLMError:
    """Map an ``httpx`` transport exception onto the typed taxonomy.

    httpx's exception hierarchy *is* structured information
    (https://www.python-httpx.org/exceptions/): ``TimeoutException`` subclasses
    cover connect/read/write/pool timeouts, ``TransportError`` covers connect,
    read, write, close and protocol failures. Classification uses the type, so a
    provider that returns a body containing the word "timeout" cannot make a
    successful response look like a timeout.
    """

    if isinstance(exc, httpx.TimeoutException):
        return UpstreamTimeoutError(
            f"{provider} did not answer within the attempt timeout",
            provider=provider,
            model=model,
            detail=type(exc).__name__,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return _status_error(
            exc.response.status_code,
            provider=provider,
            model=model,
            body=_safe_body(exc.response),
            headers=exc.response.headers,
        )
    if isinstance(exc, httpx.TransportError):
        return NetworkError(
            f"transport failure contacting {provider}",
            provider=provider,
            model=model,
            detail=type(exc).__name__,
        )
    if isinstance(exc, httpx.InvalidURL):
        return GatewayInternalError(
            f"gateway built an invalid {provider} URL",
            provider=provider,
            model=model,
            detail=str(exc)[:200],
        )
    if isinstance(exc, LLMError):
        return exc
    # Anything else is our defect, not the provider's. Never dress it up.
    return GatewayInternalError(
        f"unexpected {provider} adapter failure: {type(exc).__name__}",
        provider=provider,
        model=model,
    )


def _safe_body(response: httpx.Response, limit: int = 300) -> str:
    """Bounded, redaction-safe body fragment for structured error fields only.

    Used to read the provider's *declared* error code, and truncated hard. The
    full body is never logged: it can echo prompt content.
    """

    try:
        return (response.text or "")[:limit]
    except Exception:  # noqa: BLE001 — a broken stream must not mask the status
        return ""


#: Gemini / Google API ``error.status`` → semantic kind. This is the provider's
#: own machine-readable field, not free text.
_GOOGLE_STATUS_KINDS: Mapping[str, LLMErrorKind] = {
    "INVALID_ARGUMENT": LLMErrorKind.INVALID_REQUEST,
    "FAILED_PRECONDITION": LLMErrorKind.INVALID_REQUEST,
    "OUT_OF_RANGE": LLMErrorKind.CONTEXT_LIMIT,
    "UNAUTHENTICATED": LLMErrorKind.AUTHENTICATION,
    "PERMISSION_DENIED": LLMErrorKind.AUTHENTICATION,
    "NOT_FOUND": LLMErrorKind.INVALID_REQUEST,
    "ALREADY_EXISTS": LLMErrorKind.INVALID_REQUEST,
    "RESOURCE_EXHAUSTED": LLMErrorKind.RATE_LIMITED,
    "CANCELLED": LLMErrorKind.CANCELLED,
    "DEADLINE_EXCEEDED": LLMErrorKind.UPSTREAM_TIMEOUT,
    "INTERNAL": LLMErrorKind.TRANSIENT_PROVIDER,
    "UNAVAILABLE": LLMErrorKind.TRANSIENT_PROVIDER,
    "UNIMPLEMENTED": LLMErrorKind.UNSUPPORTED_CAPABILITY,
}

#: ``finishReason`` values that mean "the provider refused the content", i.e. a
#: moderation decision. Never retried, never fallen back (LAW 8).
_BLOCKING_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
        "LANGUAGE",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
        "CONTENT_BLOCKED",
        "UNEXPECTED_TOOL_CALL",
    }
)

_FINISH_REASON_MAP: Mapping[str, FinishReason] = {
    "STOP": FinishReason.STOP,
    "MAX_TOKENS": FinishReason.MAX_TOKENS,
    "SAFETY": FinishReason.CONTENT_BLOCKED,
    "RECITATION": FinishReason.CONTENT_BLOCKED,
    "PROHIBITED_CONTENT": FinishReason.CONTENT_BLOCKED,
}


def _retry_after_from(headers: Any) -> float | None:
    """Read a structured ``Retry-After`` (delta-seconds or HTTP-date, RFC 9110)."""

    from nexus_ai_agent.llm.gateway.resilience import parse_retry_after

    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except Exception:  # noqa: BLE001
        return None
    if value is None:
        try:
            value = headers.get("retry-after")
        except Exception:  # noqa: BLE001
            return None
    return parse_retry_after(value)


def _status_error(
    status_code: int,
    *,
    provider: str,
    model: str,
    body: str,
    headers: Any = None,
) -> LLMError:
    """Build the typed error for an HTTP failure, using declared fields only."""

    retry_after = _retry_after_from(headers)
    google_status = _declared_error_status(body)
    kind = _GOOGLE_STATUS_KINDS.get(google_status) if google_status else None
    if kind is None:
        kind = classify_kind(status_code)

    # A 429 whose own Retry-After (or declared status) says the window is far
    # away is quota exhaustion, not a transient throttle: retrying inside the
    # caller's budget cannot succeed, and pretending otherwise burns the deadline.
    if status_code == 429 and retry_after is not None and retry_after > 300.0:
        kind = LLMErrorKind.QUOTA_EXHAUSTED

    message = f"{provider} returned HTTP {status_code}"
    common: dict[str, Any] = {
        "status_code": status_code,
        "provider": provider,
        "model": model,
        "retry_after": retry_after,
        "detail": google_status or None,
    }
    if kind is LLMErrorKind.AUTHENTICATION:
        return AuthenticationError(message, **common)
    if kind is LLMErrorKind.CONTEXT_LIMIT:
        return _ContextLimit(message, **common)
    if kind is LLMErrorKind.RATE_LIMITED:
        return RateLimitedError(message, **common)
    if kind is LLMErrorKind.QUOTA_EXHAUSTED:
        return QuotaExhaustedError(message, **common)
    if kind is LLMErrorKind.UPSTREAM_TIMEOUT:
        return UpstreamTimeoutError(message, **common)
    if kind is LLMErrorKind.TRANSIENT_PROVIDER:
        return TransientProviderError(message, **common)
    if kind is LLMErrorKind.UNSUPPORTED_CAPABILITY:
        return UnsupportedCapabilityError(message, **common)
    return InvalidRequestError(message, **common)


class _ContextLimit(LLMError):
    """Context/token limit failure (kept local to avoid a wider import surface)."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("kind", LLMErrorKind.CONTEXT_LIMIT)
        super().__init__(message, **kwargs)


def _declared_error_status(body: str) -> str | None:
    """Extract the provider's machine-readable ``error.status`` from a JSON body.

    Returns ``None`` when the body is not JSON or carries no declared status —
    in which case classification falls back to the HTTP status alone. No
    free-text message is ever inspected.
    """

    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    status = error.get("status")
    if isinstance(status, str) and status:
        return status
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Gemini REST adapter
# ═══════════════════════════════════════════════════════════════════════════


#: Hosts a cleartext ``http://`` endpoint is acceptable for: a local proxy or a
#: local inference server on the same machine. Anything else must be TLS, because
#: the API key rides in a header on every request and cleartext would hand it to
#: everyone on the path.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _validated_base_url(base_url: str) -> str:
    """Refuse a base URL that would leak the credential or cannot be a URL.

    Three rules, each with a concrete failure it prevents:

    * it must parse and name a host (a relative or malformed value would produce
      a request httpx cannot send, reported as an opaque transport error);
    * the scheme must be ``https`` — unless the host is loopback, where a local
      proxy or llama.cpp/Ollama server legitimately listens on plain http;
    * the credential itself must not be embedded in the URL (``user:pass@host``),
      because a URL with credentials ends up in logs, proxies and reprs.
    """

    try:
        parsed = httpx.URL(base_url)
    except Exception as exc:  # noqa: BLE001 — any parse failure is a config bug
        raise ValueError(f"GeminiHttpAdapter.base_url is not a usable URL: {exc}") from exc
    if not parsed.host:
        raise ValueError("GeminiHttpAdapter.base_url must name a host")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            f"GeminiHttpAdapter.base_url scheme must be http(s), got {parsed.scheme!r}"
        )
    if parsed.scheme == "http" and parsed.host.lower() not in _LOOPBACK_HOSTS:
        raise ValueError(
            "GeminiHttpAdapter.base_url must use https:// for a non-loopback host: "
            "the API key is sent in a header on every request and cleartext would "
            f"expose it to {parsed.host}"
        )
    if parsed.userinfo:
        raise ValueError(
            "GeminiHttpAdapter.base_url must not embed credentials; "
            "the API key belongs in the x-goog-api-key header"
        )
    return base_url


class GeminiHttpAdapter:
    """Google Gemini ``generateContent`` / ``embedContent`` over plain httpx.

    This is the *only* module in the repository that knows a Gemini URL, the
    ``x-goog-api-key`` header, or the ``candidates[0].content.parts[0].text``
    response shape. It replaces five independent raw call sites that each knew
    all three.

    The API key rides in a header, never in the URL: query parameters reach httpx
    INFO log lines, proxy logs and exception reprs, and the redaction layer is
    defence-in-depth rather than the primary control (the repository's S4 rule,
    pinned by ``tests/unit/test_gemini_key_transport.py``).
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = "https://generativelanguage.googleapis.com/v1beta",
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        default_model: str = "gemini-2.0-flash",
    ) -> None:
        self._api_key = api_key
        # ``None`` means "the documented public endpoint". An *explicitly* empty
        # or blank string means the deployment tried to configure one and got it
        # wrong (an unset env var, a truncated secret): silently substituting the
        # public endpoint there would send a tenant's key somewhere nobody
        # intended, so it is refused instead.
        resolved = (
            "https://generativelanguage.googleapis.com/v1beta" if base_url is None else base_url
        )
        if not resolved.strip():
            raise ValueError("GeminiHttpAdapter.base_url must be a non-empty URL")
        self._base_url = _validated_base_url(resolved).rstrip("/")
        self._transport = transport
        self._client = client
        self._default_model = default_model
        self._owns_client = client is None
        self._client_budget: TimeoutBudget | None = None

    # ── ProviderAdapter ──────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "gemini"

    @property
    def operations(self) -> frozenset[LLMOperation]:
        # EMBEDDINGS is deliberately absent: the repository's embeddings are a
        # deterministic 384-dim hash vector shared by GeminiProvider and the
        # litellm provider, and stored vectors depend on that shape. Gemini's
        # real embedding endpoint returns a different dimension, so advertising
        # the capability would silently invalidate every stored vector (LAW 12).
        return frozenset(
            {LLMOperation.CHAT, LLMOperation.TEXT_COMPLETION, LLMOperation.GENERATE_CONTENT}
        )

    @property
    def modalities(self) -> frozenset[Modality]:
        return frozenset({Modality.TEXT, Modality.IMAGE, Modality.AUDIO})

    async def execute(
        self,
        request: LLMRequest,
        route: Route,
        *,
        budget: TimeoutBudget,
        request_id: str,
        attempt: int,
    ) -> AdapterResult:
        if request.operation not in self.operations:
            raise UnsupportedCapabilityError(
                f"gemini adapter does not serve operation={request.operation.value}",
                provider=self.name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        if not request.modalities.issubset(self.modalities):
            unsupported = sorted(m.value for m in request.modalities - self.modalities)
            raise UnsupportedCapabilityError(
                f"gemini adapter cannot serve modalities={unsupported}",
                provider=self.name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        if not self._api_key:
            # Fail before spending quota: an unconfigured key is a policy/config
            # problem, and a provider round-trip would just produce a 401.
            raise AuthenticationError(
                "gemini adapter has no API key configured",
                provider=self.name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )

        model = route.model or self._default_model
        if request.operation is LLMOperation.EMBEDDINGS:  # pragma: no cover — guarded above
            raise UnsupportedCapabilityError(
                "gemini embeddings are served by the legacy provider adapter",
                provider=self.name,
                model=model,
            )

        url = f"{self._base_url}/models/{model}:generateContent"
        payload = self._build_payload(request)
        client = self._client_for(budget)
        try:
            response = await client.post(
                url,
                json=payload,
                headers={"x-goog-api-key": self._api_key},
            )
        except Exception as exc:  # noqa: BLE001 — mapped to the typed taxonomy below
            error = map_transport_error(exc, provider=self.name, model=model)
            raise error.with_route(
                provider=self.name, model=model, request_id=request_id, attempt=attempt
            ) from exc

        if response.status_code != 200:
            raise _status_error(
                response.status_code,
                provider=self.name,
                model=model,
                body=_safe_body(response),
                headers=response.headers,
            ).with_route(provider=self.name, model=model, request_id=request_id, attempt=attempt)

        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
            raise MalformedResponseError(
                "gemini returned a non-JSON 200 response",
                provider=self.name,
                model=model,
                request_id=request_id,
                attempt=attempt,
                status_code=200,
            ) from exc
        return self._parse(data, model=model, request_id=request_id, attempt=attempt)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None
        self._client_budget = None

    # ── wire format ──────────────────────────────────────────────────
    def _client_for(self, budget: TimeoutBudget) -> httpx.AsyncClient:
        """One pooled client per timeout budget (rebuilt only if the budget changes).

        Per-request timeouts are policy-owned, not caller-owned (LAW 4): the
        transport bound is set on the client so it fires *inside* the gateway's
        outer per-attempt cap and stays classifiable as ``UPSTREAM_TIMEOUT``.
        The call shape stays ``client.post(url, json=…, headers=…)`` — the same
        shape the repository's key-transport tests pin.
        """

        if self._client is not None and self._client_budget == budget:
            return self._client
        if self._client is not None and self._owns_client:
            # Budget changed under a live client: release the old pool. Fire and
            # forget is unsafe, so the caller-visible path closes synchronously
            # through aclose(); here we simply drop the reference and let the
            # event loop's GC path handle it, which httpx tolerates.
            self._client = None
        timeout = httpx.Timeout(
            budget.read_seconds,
            connect=budget.connect_seconds,
            write=budget.read_seconds,
            pool=budget.connect_seconds,
        )
        self._client = httpx.AsyncClient(timeout=timeout, transport=self._transport)
        self._client_budget = budget
        self._owns_client = True
        return self._client

    @staticmethod
    def _build_payload(request: LLMRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_text = request.system

        if request.messages:
            for message in request.messages:
                if message.role == "system":
                    system_text = message.content if system_text is None else system_text
                    continue
                role = "model" if message.role in {"model", "assistant"} else "user"
                turn: list[dict[str, Any]] = []
                if message.content:
                    turn.append({"text": message.content})
                for part in message.parts:
                    if part.text:
                        turn.append({"text": part.text})
                    if part.data:
                        turn.append(
                            {
                                "inline_data": {
                                    "mime_type": part.mime_type,
                                    "data": base64.b64encode(part.data).decode("ascii"),
                                }
                            }
                        )
                contents.append({"role": role, "parts": turn or [{"text": ""}]})
        else:
            parts: list[dict[str, Any]] = []
            if request.prompt:
                parts.append({"text": request.prompt})
            for part in request.parts:
                if part.text:
                    parts.append({"text": part.text})
                if part.data:
                    parts.append(
                        {
                            "inline_data": {
                                "mime_type": part.mime_type,
                                "data": base64.b64encode(part.data).decode("ascii"),
                            }
                        }
                    )
            if parts:
                contents.append({"role": "user", "parts": parts})

        payload: dict[str, Any] = {"contents": contents}
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}

        generation: dict[str, Any] = {}
        params = request.generation
        if params.temperature is not None:
            generation["temperature"] = params.temperature
        if params.top_p is not None:
            generation["topP"] = params.top_p
        if params.top_k is not None:
            generation["topK"] = params.top_k
        if params.max_output_tokens is not None:
            generation["maxOutputTokens"] = params.max_output_tokens
        if params.response_mime_type:
            generation["responseMimeType"] = params.response_mime_type
        if params.stop_sequences:
            generation["stopSequences"] = list(params.stop_sequences)
        if request.has_multimodal_input():
            # Vision/audio requests must be allowed to answer in text.
            generation.setdefault("responseModalities", ["TEXT"])
        if generation:
            payload["generationConfig"] = generation
        return payload

    @staticmethod
    def _parse(data: Any, *, model: str, request_id: str, attempt: int) -> AdapterResult:
        if not isinstance(data, dict):
            raise MalformedResponseError(
                "gemini 200 response was not a JSON object",
                provider="gemini",
                model=model,
                request_id=request_id,
                attempt=attempt,
                status_code=200,
            )

        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict):
            block_reason = feedback.get("blockReason")
            if isinstance(block_reason, str) and block_reason:
                raise ContentBlockedError(
                    "gemini blocked the prompt before generation",
                    kind=LLMErrorKind.CONTENT_BLOCKED,
                    provider="gemini",
                    model=model,
                    request_id=request_id,
                    attempt=attempt,
                    status_code=200,
                    detail=block_reason[:120],
                )

        candidates = data.get("candidates")
        usage = _gemini_usage(data.get("usageMetadata"))
        if not isinstance(candidates, list) or not candidates:
            # A 200 with no candidates is not an answer. Fail closed rather than
            # returning "" and letting a caller render an empty message as success.
            raise MalformedResponseError(
                "gemini returned no candidates",
                provider="gemini",
                model=model,
                request_id=request_id,
                attempt=attempt,
                status_code=200,
                detail=_declared_error_status(json.dumps(data.get("error", {}))) or None,
            )

        first = candidates[0]
        if not isinstance(first, dict):
            raise MalformedResponseError(
                "gemini candidate was not an object",
                provider="gemini",
                model=model,
                request_id=request_id,
                attempt=attempt,
                status_code=200,
            )
        raw_finish = first.get("finishReason")
        finish = raw_finish if isinstance(raw_finish, str) else None
        if finish is not None and finish.upper() in _BLOCKING_FINISH_REASONS:
            raise ContentBlockedError(
                "gemini stopped generation for a content-policy reason",
                kind=LLMErrorKind.CONTENT_BLOCKED,
                provider="gemini",
                model=model,
                request_id=request_id,
                attempt=attempt,
                status_code=200,
                detail=finish[:120],
            )

        text = _gemini_text(first)
        if text is None:
            raise MalformedResponseError(
                "gemini candidate carried no text part",
                provider="gemini",
                model=model,
                request_id=request_id,
                attempt=attempt,
                status_code=200,
                detail=(finish or "")[:120] or None,
            )
        return AdapterResult(
            text=text,
            finish_reason=_FINISH_REASON_MAP.get((finish or "").upper(), FinishReason.UNKNOWN)
            if finish
            else FinishReason.STOP,
            usage=usage,
            reported_model=data.get("modelVersion")
            if isinstance(data.get("modelVersion"), str)
            else None,
            status_code=200,
        )


def _gemini_text(candidate: dict[str, Any]) -> str | None:
    """Concatenate every text part of a candidate (Gemini may split parts)."""

    content = candidate.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and isinstance(inline.get("data"), str):
            try:
                decoded = base64.b64decode(inline["data"], validate=True)
            except (binascii.Error, ValueError):
                continue
            texts.append(decoded.decode("utf-8", errors="replace"))
    if not texts:
        return None
    return "".join(texts)


def _gemini_usage(metadata: Any) -> Usage:
    """Provider-reported token usage, or UNKNOWN. Never estimated (LAW 11)."""

    if not isinstance(metadata, dict):
        return UNKNOWN_USAGE

    def read(key: str) -> int | None:
        value = metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value >= 0 else None

    input_tokens = read("promptTokenCount")
    output_tokens = read("candidatesTokenCount")
    total_tokens = read("totalTokenCount")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return UNKNOWN_USAGE
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return Usage(
        source=UsageSource.PROVIDER,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Legacy LLMProvider adapter
# ═══════════════════════════════════════════════════════════════════════════


class LegacyProviderAdapter:
    """Wraps any existing :class:`~nexus_ai_agent.llm.provider.LLMProvider`.

    This is the compatibility half of LAW 12: the litellm routing chain, the
    llama.cpp server, the in-process GGUF provider and ``FakeLLMProvider`` all
    become gateway routes without being rewritten, and their failures are mapped
    by *exception type* (a structured signal) rather than by message text.

    ``error_kinds`` lets a deployment extend the mapping for provider-specific
    exception classes without touching this file — the map is data, keyed on
    type, which is exactly how a typed taxonomy should grow.
    """

    def __init__(
        self,
        provider: Any,
        *,
        name: str,
        model: str,
        operations: frozenset[LLMOperation] | None = None,
        modalities: frozenset[Modality] | None = None,
        degraded: bool = False,
        error_kinds: Mapping[type[BaseException], LLMErrorKind] | None = None,
    ) -> None:
        if not hasattr(provider, "generate"):
            raise TypeError(f"{type(provider).__name__} does not implement generate()")
        self._provider = provider
        self._name = name
        self._model = model
        self._operations = operations or frozenset(
            {LLMOperation.CHAT, LLMOperation.TEXT_COMPLETION, LLMOperation.EMBEDDINGS}
        )
        self._modalities = modalities or frozenset({Modality.TEXT})
        self._degraded = degraded
        self._error_kinds: dict[type[BaseException], LLMErrorKind] = dict(error_kinds or {})

    @property
    def name(self) -> str:
        return self._name

    @property
    def operations(self) -> frozenset[LLMOperation]:
        return self._operations

    @property
    def modalities(self) -> frozenset[Modality]:
        return self._modalities

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def inner(self) -> Any:
        """The wrapped provider — exposed for the compatibility facade only."""

        return self._provider

    def route(self, *, rank: int = 100, label: str = "") -> Route:
        return Route(
            provider=self._name,
            model=self._model,
            operations=self._operations,
            modalities=self._modalities,
            rank=rank,
            degraded=self._degraded,
            label=label or self._name,
        )

    async def execute(
        self,
        request: LLMRequest,
        route: Route,
        *,
        budget: TimeoutBudget,
        request_id: str,
        attempt: int,
    ) -> AdapterResult:
        _ = budget  # the legacy provider owns its own transport timeout
        if request.operation not in self._operations:
            raise UnsupportedCapabilityError(
                f"{self._name} does not serve operation={request.operation.value}",
                provider=self._name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        if not request.modalities.issubset(self._modalities):
            unsupported = sorted(m.value for m in request.modalities - self._modalities)
            raise UnsupportedCapabilityError(
                f"{self._name} cannot serve modalities={unsupported}",
                provider=self._name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        try:
            if request.operation is LLMOperation.EMBEDDINGS:
                text = request.prompt or (request.messages[-1].content if request.messages else "")
                embed = getattr(self._provider, "embed", None)
                if embed is None:
                    raise UnsupportedCapabilityError(
                        f"{self._name} does not implement embed()",
                        provider=self._name,
                        model=route.model,
                        request_id=request_id,
                        attempt=attempt,
                    )
                vector = await embed(text)
                if not isinstance(vector, (list, tuple)) or not vector:
                    raise MalformedResponseError(
                        f"{self._name} returned an empty embedding",
                        provider=self._name,
                        model=route.model,
                        request_id=request_id,
                        attempt=attempt,
                    )
                floats = tuple(float(v) for v in vector)
                # UNKNOWN, not PROVIDER-with-nones: the legacy contract returns a
                # bare ``list[float]`` with no token accounting, and several of
                # those providers (``GeminiProvider.embed``,
                # ``LiteLLMRoutingProvider.embed``) compute a local hash vector
                # without contacting anyone. Claiming provider provenance for a
                # number nobody reported would be invented usage (LAW 11).
                return AdapterResult(
                    text="",
                    embedding=floats,
                    finish_reason=FinishReason.STOP,
                    usage=UNKNOWN_USAGE,
                )

            prompt, system = _flatten(request)
            text = await self._provider.generate(prompt, system=system)
        except Exception as exc:  # noqa: BLE001 — classified below, never re-raised raw
            raise self._classify(exc, route, request_id, attempt) from exc

        if not isinstance(text, str):
            raise MalformedResponseError(
                f"{self._name} returned {type(text).__name__} instead of str",
                provider=self._name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        return AdapterResult(text=text, finish_reason=FinishReason.STOP, usage=UNKNOWN_USAGE)

    def _classify(self, exc: Exception, route: Route, request_id: str, attempt: int) -> LLMError:
        """Map by exception *type*; fall back to transport mapping for httpx errors."""

        if isinstance(exc, LLMError):
            return exc.with_route(
                provider=self._name, model=route.model, request_id=request_id, attempt=attempt
            )
        for error_type, kind in self._error_kinds.items():
            if isinstance(exc, error_type):
                return LLMError(
                    f"{self._name} failed: {type(exc).__name__}",
                    kind=kind,
                    provider=self._name,
                    model=route.model,
                    request_id=request_id,
                    attempt=attempt,
                )
        if isinstance(exc, httpx.HTTPError):
            return map_transport_error(exc, provider=self._name, model=route.model).with_route(
                provider=self._name, model=route.model, request_id=request_id, attempt=attempt
            )
        if isinstance(exc, TimeoutError):
            # ``asyncio.TimeoutError`` *is* the builtin ``TimeoutError`` since
            # 3.11, and a socket timeout raises it too. Either way the evidence
            # is the class: the provider did not answer in time.
            return UpstreamTimeoutError(
                f"{self._name} did not answer in time",
                provider=self._name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
                detail=type(exc).__name__,
            )
        if isinstance(exc, (ConnectionError, OSError)):
            # Refused/reset/unreachable: a network condition, retryable.
            return NetworkError(
                f"transport failure contacting {self._name}",
                provider=self._name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
                detail=type(exc).__name__,
            )
        if isinstance(exc, (ValueError, TypeError)):
            # A legacy provider raising ValueError/TypeError is a malformed
            # interaction, not an outage: fail closed, do not retry.
            return MalformedResponseError(
                f"{self._name} rejected the request shape: {type(exc).__name__}",
                provider=self._name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        return GatewayInternalError(
            f"unclassified {self._name} failure: {type(exc).__name__}",
            provider=self._name,
            model=route.model,
            request_id=request_id,
            attempt=attempt,
        )

    async def aclose(self) -> None:
        aclose = getattr(self._provider, "aclose", None)
        if callable(aclose):
            await aclose()


def _flatten(request: LLMRequest) -> tuple[str, str]:
    """Render a gateway request into the legacy ``(prompt, system)`` pair.

    History is folded into the prompt because the legacy ``LLMProvider`` contract
    has no history parameter. That is a real information loss, so it is recorded
    in the request's observability metadata by the engine rather than hidden.
    """

    system = request.system or ""
    if request.messages:
        rendered: list[str] = []
        for message in request.messages:
            if message.role == "system" and not system:
                system = message.content
                continue
            rendered.append(f"{message.role}: {message.content}")
        prompt = "\n".join(rendered)
        if request.prompt:
            prompt = f"{prompt}\nuser: {request.prompt}"
        return prompt, system
    return request.prompt, system


# ═══════════════════════════════════════════════════════════════════════════
# litellm Router adapter (the multi-provider routing chain)
# ═══════════════════════════════════════════════════════════════════════════


class LitellmRouterAdapter:
    """Adapter for a ``litellm.Router`` fallback chain.

    The Router is itself a deployment selector: given ``model_name`` it picks a
    healthy deployment and walks its configured ``fallbacks`` list, parking a
    deployment on cooldown after a 429. That is *provider-internal* routing, and
    it is why this adapter is registered with ``RetryPolicy(max_attempts=1)``:
    retrying a Router call from the gateway would nest a second retry loop over
    the first and multiply the attempts against every free-tier quota in the
    chain (LAW 7 — a retry must have a purpose; here the Router already owns
    deployment selection).

    What the gateway still owns for this route, and what the Router cannot do:
    the caller's timeout budget, concurrency bounds, the local provider quota
    gate, circuit breaking, typed error classification, correlation ids and the
    observability record.

    Failure classification is supplied by the caller as ``classify`` — a function
    of the *exception type* — so litellm's own exception hierarchy stays inside
    ``llm/litellm_provider.py`` and nothing here inspects a message string.
    """

    def __init__(
        self,
        router: Any,
        *,
        primary_name: str,
        chain_names: Sequence[str] = (),
        model: str | None = None,
        classify: Callable[[Exception], LLMError] | None = None,
        on_response: Callable[[Any], None] | None = None,
    ) -> None:
        self._router = router
        self._primary_name = primary_name
        self._chain_names = tuple(chain_names)
        self._model = model or primary_name
        self._classify = classify
        self._on_response = on_response

    @property
    def name(self) -> str:
        return "routing"

    @property
    def model(self) -> str:
        return self._model

    @property
    def chain_names(self) -> tuple[str, ...]:
        return self._chain_names

    @property
    def operations(self) -> frozenset[LLMOperation]:
        # EMBEDDINGS is deliberately absent: the routing chain's ``embed()`` is a
        # local deterministic hash, not a provider call, so it has no business
        # occupying a gateway route (and no usage to account for).
        return frozenset({LLMOperation.CHAT, LLMOperation.TEXT_COMPLETION})

    @property
    def modalities(self) -> frozenset[Modality]:
        return frozenset({Modality.TEXT})

    @staticmethod
    def build_messages(request: LLMRequest) -> list[dict[str, str]]:
        """Render the request into litellm's ``messages`` — shape preserved.

        Byte-identical to the pre-W2 ``LiteLLMRoutingProvider.generate`` output
        for the ``(prompt, system)`` contract, and lossless for real history.
        """

        messages: list[dict[str, str]] = []
        system = request.system
        for message in request.messages:
            if message.role == "system":
                if system is None:
                    system = message.content
                continue
            role = "assistant" if message.role in {"model", "assistant"} else message.role
            messages.append({"role": role, "content": message.content})
        if system:
            messages.insert(0, {"role": "system", "content": system})
        if request.prompt:
            messages.append({"role": "user", "content": request.prompt})
        return messages

    async def execute(
        self,
        request: LLMRequest,
        route: Route,
        *,
        budget: TimeoutBudget,
        request_id: str,
        attempt: int,
    ) -> AdapterResult:
        if request.operation not in self.operations:
            raise UnsupportedCapabilityError(
                f"the routing chain does not serve operation={request.operation.value}",
                provider=self.name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        if not request.modalities.issubset(self.modalities):
            unsupported = sorted(m.value for m in request.modalities - self.modalities)
            raise UnsupportedCapabilityError(
                f"the routing chain is text-only; asked for {unsupported}",
                provider=self.name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        messages = self.build_messages(request)
        _ = budget  # the Router owns its per-deployment timeout (settings.llm_request_timeout)
        try:
            response = await self._router.acompletion(
                model=self._primary_name,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 — classified by type below, never re-raised raw
            if self._classify is not None:
                raise self._classify(exc).with_route(
                    provider=self.name,
                    model=route.model,
                    request_id=request_id,
                    attempt=attempt,
                ) from exc
            raise map_transport_error(exc, provider=self.name, model=route.model) from exc

        if self._on_response is not None:
            self._on_response(response)
        text = _litellm_content(response)
        usage = _litellm_usage(response)
        if not text and usage.source is UsageSource.UNKNOWN:
            raise MalformedResponseError(
                "the routing chain returned an empty completion",
                provider=self.name,
                model=route.model,
                request_id=request_id,
                attempt=attempt,
            )
        return AdapterResult(
            text=text,
            finish_reason=_litellm_finish_reason(response),
            usage=usage,
            reported_model=_litellm_model(response),
            extras={"chain": ",".join(self._chain_names)[:120]},
        )

    async def aclose(self) -> None:
        """Nothing to release: the Router owns no sockets of ours."""

        return None


def _litellm_field(response: Any, name: str) -> Any:
    """Read one field from a litellm ``ModelResponse`` *or* a plain dict."""

    if isinstance(response, dict):
        return response.get(name)
    return getattr(response, name, None)


def _litellm_content(response: Any) -> str:
    choices = _litellm_field(response, "choices") or []
    if not choices:
        return ""
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else getattr(first, "message", None)
    if message is None:
        return ""
    content = (
        message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    )
    return content or ""


def _litellm_finish_reason(response: Any) -> FinishReason:
    choices = _litellm_field(response, "choices") or []
    if not choices:
        return FinishReason.UNKNOWN
    first = choices[0]
    reason = (
        first.get("finish_reason")
        if isinstance(first, dict)
        else getattr(first, "finish_reason", None)
    )
    if reason == "stop":
        return FinishReason.STOP
    if reason in {"length", "max_tokens"}:
        return FinishReason.MAX_TOKENS
    if reason in {"content_filter", "safety"}:
        return FinishReason.CONTENT_BLOCKED
    if reason is None:
        return FinishReason.UNKNOWN
    return FinishReason.UNKNOWN


def _litellm_model(response: Any) -> str | None:
    model = _litellm_field(response, "model")
    return str(model) if model else None


def _litellm_usage(response: Any) -> Usage:
    """Real token counts when litellm reports them; ``UNKNOWN`` when it does not.

    LAW 11: a local model (Ollama) or a provider that omits usage yields
    ``UsageSource.UNKNOWN`` with ``None`` counts. Inventing a number would be
    worse than an honest gap, because the gap is visible and the number is not.
    """

    usage = _litellm_field(response, "usage")
    if usage is None:
        return UNKNOWN_USAGE
    prompt_tokens = _litellm_field(usage, "prompt_tokens")
    completion_tokens = _litellm_field(usage, "completion_tokens")
    total_tokens = _litellm_field(usage, "total_tokens")
    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return UNKNOWN_USAGE
    return Usage(
        source=UsageSource.PROVIDER,
        input_tokens=int(prompt_tokens) if prompt_tokens is not None else None,
        output_tokens=int(completion_tokens) if completion_tokens is not None else None,
        total_tokens=int(total_tokens) if total_tokens is not None else None,
    )
