"""Typed contract for every external-intelligence boundary (v3.13.0).

Why this module exists
----------------------
Before this module, ``knowledge/`` and ``integrations/`` shared one habit: they
turned *failure* into a *success-shaped value*.  ``core/http_client.py``
deliberately returns ``""`` from :meth:`get_text` and ``{}`` from
:meth:`get_json` on any error — a reasonable convenience for a caller that
genuinely does not care, and a catastrophe for a knowledge system, because:

* a dead network and an empty article become the same value;
* a 404 "Wikipedia does not have an article with this exact name" page is
  scraped into user-visible "knowledge";
* a currency provider that omits a rate yields the number ``0.0``;
* a search provider that throws yields ``[]`` — same as "no results".

The rule this module enforces is: **a source either produced data, or it
raised a typed error.**  There is no third state and no success-shaped
placeholder.  Callers that must degrade gracefully do so *explicitly*, and
the degradation is recorded in :class:`Provenance`.

The three primitives
--------------------
:class:`ExternalSourceError`
    Typed, redacted, classified (:class:`ExternalErrorKind`) failure.  Carries
    ``retryable`` so a caller can distinguish "try again" from "never works".
:class:`Provenance`
    Where a payload came from, when, whether it was truncated, whether the
    path was degraded, and which fallbacks were taken to get it.
:class:`SourceResult`
    A payload that is *inseparable* from its provenance, plus the non-fatal
    per-source failures that happened while assembling it.

Everything else here (URL construction, redaction, blocking-call offload,
bounded HTTP fetch, HTML extraction) exists so that no call site has to
re-implement a security or resource boundary by hand.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Generic, TypeVar
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from nexus_ai_agent.core.http_client import CircuitOpenError, ResilientHttpClient
from nexus_ai_agent.core.ssrf_guard import SSRFBlockError
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# ── resource bounds ────────────────────────────────────────────────────────
# Every bound is a named constant so a test can assert against the contract
# rather than against a magic number buried in a call site.
MAX_QUERY_CHARS = 256
"""Longest accepted user query.  Anything longer is INVALID_INPUT, not a
1 MB row in ``KnowledgeCache`` and a 1 MB LLM prompt."""

MAX_URL_SEGMENT_CHARS = 512
"""Longest accepted single URL path segment before percent-encoding."""

MAX_TEXT_RESPONSE_BYTES = 1_000_000
"""Text/HTML bodies are truncated to this and marked ``truncated=True``."""

MAX_JSON_RESPONSE_BYTES = 2_000_000
"""JSON bodies above this are OVERSIZED_RESPONSE — a partial JSON document
cannot be honestly truncated, so it is refused instead."""

MAX_EXTRACTED_TEXT_CHARS = 20_000
"""Upper bound on text handed back from HTML extraction."""


def utcnow() -> datetime:
    """Timezone-aware current UTC time (``datetime.utcnow`` is deprecated)."""
    return datetime.now(timezone.utc)


def naive_utcnow() -> datetime:
    """Naive UTC timestamp for the plain ``DateTime`` columns in storage/.

    The storage models use naive ``DateTime`` columns, so the repository-wide
    convention is *naive values are UTC*.  This helper makes that conversion
    explicit and keeps the deprecated ``datetime.utcnow()`` out of the zone.
    """
    return utcnow().replace(tzinfo=None)


# ── failure taxonomy ───────────────────────────────────────────────────────
class ExternalErrorKind(str, Enum):
    """Why an external source did not produce data.

    The split that matters operationally is ``retryable`` (see
    :attr:`ExternalSourceError.retryable`): a timeout deserves another attempt,
    a malformed URL never will.
    """

    INVALID_INPUT = "invalid_input"
    """Caller/user input was rejected before any network was touched."""

    DEPENDENCY_MISSING = "dependency_missing"
    """An optional third-party package required for this path is not installed."""

    BLOCKED_URL = "blocked_url"
    """The SSRF guard refused the URL (non-https, private/loopback address...)."""

    NETWORK = "network"
    """Connection-level failure."""

    TIMEOUT = "timeout"
    """The request exceeded its deadline."""

    CIRCUIT_OPEN = "circuit_open"
    """The per-host circuit breaker short-circuited the call."""

    HTTP_STATUS = "http_status"
    """The server answered with an error status."""

    NOT_FOUND = "not_found"
    """The resource genuinely does not exist (HTTP 404 / provider says so)."""

    MALFORMED_RESPONSE = "malformed_response"
    """The response could not be interpreted (bad JSON, wrong content type,
    missing mandatory fields)."""

    OVERSIZED_RESPONSE = "oversized_response"
    """The response exceeded the resource bound for its kind."""

    PROVIDER_ERROR = "provider_error"
    """A third-party SDK raised."""

    EMPTY_RESULT = "empty_result"
    """The source worked and authoritatively returned nothing."""

    CANCELLED = "cancelled"
    """The operation was cancelled.  Never produced by swallowing
    ``asyncio.CancelledError`` — that is always re-raised."""


_RETRYABLE_KINDS = frozenset(
    {
        ExternalErrorKind.NETWORK,
        ExternalErrorKind.TIMEOUT,
        ExternalErrorKind.CIRCUIT_OPEN,
        ExternalErrorKind.HTTP_STATUS,
        ExternalErrorKind.PROVIDER_ERROR,
    }
)


class SourceType(str, Enum):
    """What *kind* of thing produced a payload, for provenance reporting."""

    ENCYCLOPEDIA = "encyclopedia"
    WEB_SEARCH = "web_search"
    WEB_PAGE = "web_page"
    NEWS_API = "news_api"
    NEWS_SEARCH = "news_search"
    VIDEO_SEARCH = "video_search"
    WEATHER_API = "weather_api"
    FX_API = "fx_api"
    LLM_SYNTHESIS = "llm_synthesis"
    CACHE = "cache"


# ── secret redaction ───────────────────────────────────────────────────────
_SENSITIVE_PARAM_RE = re.compile(
    r"(?i)\b(api[-_]?key|apikey|access[-_]?token|auth[-_]?token|token|key|secret|password|"
    r"signature|sig)\b"
)
_SENSITIVE_HEADER_NAMES = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-goog-api-key"}
)
REDACTED = "***"


def redact_url(url: str | None) -> str | None:
    """Return *url* with the value of every sensitive query parameter removed.

    Used on **every** URL that reaches a log line, an exception message or a
    :class:`Provenance` record.  A URL is not a secret; the credentials people
    keep putting inside one are.
    """
    if url is None:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if not parts.query:
        return url
    kept: list[str] = []
    for chunk in parts.query.split("&"):
        if not chunk:
            continue
        name, sep, _value = chunk.partition("=")
        if sep and _SENSITIVE_PARAM_RE.fullmatch(name.strip()):
            kept.append(f"{name}={REDACTED}")
        else:
            kept.append(chunk)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), parts.fragment))


def redact_text(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Scrub *secrets* and sensitive URL parameters out of arbitrary text.

    ``secrets`` is the caller's own credential material (an API key it holds).
    Short values are ignored so that a one-character "key" cannot blank out
    the whole message.
    """
    scrubbed = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            scrubbed = scrubbed.replace(secret, REDACTED)
    # kill `?apiKey=xyz` / `&token=xyz` style leftovers regardless of source
    scrubbed = re.sub(
        r"(?i)([?&](?:api[-_]?key|apikey|access[-_]?token|auth[-_]?token|token|key|secret|"
        r"password|signature|sig)=)[^&\s\"']+",
        rf"\1{REDACTED}",
        scrubbed,
    )
    return scrubbed


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return *headers* with every credential-bearing value replaced."""
    return {
        name: (REDACTED if name.lower() in _SENSITIVE_HEADER_NAMES else value)
        for name, value in headers.items()
    }


# ── provenance ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Provenance:
    """Where a payload came from and how trustworthy/complete it is.

    Rule 8 ("data has provenance") is enforced structurally: a payload can only
    be carried by :class:`SourceResult`, and a :class:`SourceResult` cannot be
    constructed without one of these.
    """

    source: str
    """Human-readable source id, e.g. ``"wikipedia:fa"``."""

    source_type: SourceType
    url: str | None = None
    """Already redacted — :func:`Provenance.create` guarantees it."""

    retrieved_at: datetime = field(default_factory=utcnow)
    truncated: bool = False
    """True when the payload is a prefix of what the source actually returned."""

    degraded: bool = False
    """True when the payload is real but obtained on a lower-quality path
    (fallback language, fallback parser, partial source set...)."""

    fallback_path: tuple[str, ...] = ()
    """Ordered record of the fallbacks taken, e.g. ``("fa:not_found", "en")``."""

    notes: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        source: str,
        source_type: SourceType,
        *,
        url: str | None = None,
        truncated: bool = False,
        degraded: bool = False,
        fallback_path: Sequence[str] = (),
        notes: Sequence[str] = (),
    ) -> Provenance:
        """Build a provenance record with the URL redacted at the boundary."""
        return cls(
            source=source,
            source_type=source_type,
            url=redact_url(url),
            truncated=truncated,
            degraded=degraded,
            fallback_path=tuple(fallback_path),
            notes=tuple(notes),
        )

    def with_notes(self, *notes: str) -> Provenance:
        return replace(self, notes=self.notes + tuple(notes))

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view, for structured logs and API payloads."""
        return {
            "source": self.source,
            "source_type": self.source_type.value,
            "url": self.url,
            "retrieved_at": self.retrieved_at.isoformat(),
            "truncated": self.truncated,
            "degraded": self.degraded,
            "fallback_path": list(self.fallback_path),
            "notes": list(self.notes),
        }


# ── typed failure ──────────────────────────────────────────────────────────
class ExternalSourceError(RuntimeError):
    """The single failure type of the external-intelligence zone.

    ``detail`` is redacted on construction, so this exception is safe to log,
    to put in a traceback and to show to an operator.  It never contains the
    credential that produced it.
    """

    def __init__(
        self,
        kind: ExternalErrorKind,
        source: str,
        detail: str = "",
        *,
        url: str | None = None,
        status_code: int | None = None,
        secrets: Iterable[str | None] = (),
        provenance: Provenance | None = None,
    ) -> None:
        self.kind = kind
        self.source = source
        self.status_code = status_code
        self.url = redact_url(url)
        self.detail = redact_text(detail, secrets) if detail else ""
        self.provenance = provenance
        message = f"[{kind.value}] {source}"
        if self.detail:
            message = f"{message}: {self.detail}"
        if self.url:
            message = f"{message} (url={self.url})"
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        """True when repeating the identical call could plausibly succeed."""
        return self.kind in _RETRYABLE_KINDS

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "source": self.source,
            "detail": self.detail,
            "url": self.url,
            "status_code": self.status_code,
            "retryable": self.retryable,
        }


# ── result envelope ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SourceResult(Generic[T]):
    """A payload that cannot be separated from where it came from."""

    value: T
    provenance: Provenance
    partial_failures: tuple[ExternalSourceError, ...] = ()
    """Sources that failed while this (still usable) result was assembled."""

    @property
    def degraded(self) -> bool:
        """True when something was lost: a fallback, a truncation, a dead source."""
        return bool(self.partial_failures) or self.provenance.degraded or self.provenance.truncated

    def as_dict(self) -> dict[str, Any]:
        return {
            "provenance": self.provenance.as_dict(),
            "degraded": self.degraded,
            "partial_failures": [failure.as_dict() for failure in self.partial_failures],
        }


# ── input normalisation ────────────────────────────────────────────────────
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BIDI_CONTROL_CHARS = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f"
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_query(raw: str, *, source: str = "query", max_chars: int = MAX_QUERY_CHARS) -> str:
    """Normalise hostile free text into a query that is safe to key a cache on.

    Applied: Unicode NFC (so ``"ﻋﻠﻰ"`` and its composed form are one key),
    removal of C0/C1 control characters and bidi overrides (an RTL override can
    make a URL or a cached answer *render* as something it is not), whitespace
    collapsing, and a hard length bound.

    Raises:
        ExternalSourceError: ``INVALID_INPUT`` when the query is empty after
            normalisation or longer than *max_chars*.  Emptiness is a caller
            bug, not "no results", so it must not reach the network.
    """
    if not isinstance(raw, str):  # defensive: handlers pass through user data
        raise ExternalSourceError(
            ExternalErrorKind.INVALID_INPUT, source, f"query must be str, got {type(raw).__name__}"
        )
    text = unicodedata.normalize("NFC", raw)
    text = _CONTROL_CHARS_RE.sub("", text)
    text = text.translate({ord(char): None for char in _BIDI_CONTROL_CHARS})
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        raise ExternalSourceError(
            ExternalErrorKind.INVALID_INPUT, source, "query is empty after normalisation"
        )
    if len(text) > max_chars:
        raise ExternalSourceError(
            ExternalErrorKind.INVALID_INPUT,
            source,
            f"query is {len(text)} chars, limit is {max_chars}",
        )
    return text


def cache_key_for(query: str) -> str:
    """Canonical cache key: normalised, case-folded, NFC.

    ``"  Hoosh  AI "``, ``"hoosh ai"`` and ``"HOOSH\u00a0AI"`` are one topic and
    must be one cache entry and one LLM call, not three.
    """
    return unicodedata.normalize("NFC", query.strip().casefold())


def safe_path_segment(value: str, *, source: str, field_name: str = "segment") -> str:
    """Percent-encode *value* for use as exactly one URL path segment.

    ``quote(safe="")`` encodes ``/``, ``?``, ``#``, ``&`` and ``%`` too, so a
    user string can change neither the path depth, nor the query, nor the
    fragment of the URL it lands in.

    Raises:
        ExternalSourceError: ``INVALID_INPUT`` for empty or oversized input.
    """
    if not value or not value.strip():
        raise ExternalSourceError(ExternalErrorKind.INVALID_INPUT, source, f"{field_name} is empty")
    if len(value) > MAX_URL_SEGMENT_CHARS:
        raise ExternalSourceError(
            ExternalErrorKind.INVALID_INPUT,
            source,
            f"{field_name} is {len(value)} chars, limit is {MAX_URL_SEGMENT_CHARS}",
        )
    cleaned = _CONTROL_CHARS_RE.sub("", value)
    return quote(cleaned, safe="")


def build_url(
    *,
    scheme: str = "https",
    host: str,
    segments: Sequence[str],
    params: Mapping[str, str] | None = None,
    source: str,
) -> str:
    """Compose a URL where the host is fixed by the caller and cannot be injected.

    *segments* are percent-encoded individually and *params* are urlencoded, so
    no user-supplied value can escape into the authority, the path structure or
    another query parameter.

    Raises:
        ExternalSourceError: ``INVALID_INPUT`` if the host is not a plain
            hostname (this is what stops ``lang="evil.com/x?a="`` from turning
            ``https://{lang}.wikipedia.org/...`` into a request to ``evil.com``).
    """
    if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host) or ".." in host:
        raise ExternalSourceError(
            ExternalErrorKind.INVALID_INPUT, source, f"illegal host component {host!r}"
        )
    path = "/" + "/".join(safe_path_segment(seg, source=source) for seg in segments if seg != "")
    query = urlencode(dict(params or {}), doseq=False)
    return urlunsplit((scheme, host, path, query, ""))


# ── blocking-call offload ──────────────────────────────────────────────────
async def run_blocking(
    func: Callable[..., T],
    /,
    *args: Any,
    source: str,
    kind: ExternalErrorKind = ExternalErrorKind.PROVIDER_ERROR,
    secrets: Iterable[str | None] = (),
    **kwargs: Any,
) -> T:
    """Run a synchronous third-party call off the event loop, with typed errors.

    Rule 6: a synchronous SDK (``DDGS``) or a CPU-heavy parse (BeautifulSoup on
    a multi-megabyte document) inside an ``async def`` stalls **every** other
    coroutine in the process — measured at 0 heartbeat ticks during a 0.6 s
    search and during a 0.34 s parse before this change.

    ``asyncio.CancelledError`` is re-raised untouched: a cancelled request must
    stay cancelled, never be laundered into a provider error.  The worker
    thread itself cannot be killed (a stdlib limitation of ``to_thread``); the
    awaiting task is released immediately and the thread is abandoned.
    """
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except asyncio.CancelledError:
        raise
    except ExternalSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 — deliberately re-typed, never swallowed
        raise ExternalSourceError(
            kind, source, f"{type(exc).__name__}: {exc}", secrets=secrets
        ) from exc


# ── bounded, honest HTTP ───────────────────────────────────────────────────
_TEXTUAL_CONTENT_TYPES = ("text/", "application/xhtml", "application/xml", "+xml")
_JSON_CONTENT_TYPES = ("application/json", "text/json", "+json")


def _classify_http_exception(
    exc: BaseException, source: str, url: str, secrets: Iterable[str | None]
) -> ExternalSourceError:
    """Map a transport exception onto the taxonomy, preserving the cause."""
    if isinstance(exc, SSRFBlockError):
        kind = ExternalErrorKind.BLOCKED_URL
    elif isinstance(exc, CircuitOpenError):
        kind = ExternalErrorKind.CIRCUIT_OPEN
    elif isinstance(exc, httpx.TimeoutException):
        kind = ExternalErrorKind.TIMEOUT
    elif isinstance(exc, httpx.HTTPStatusError):
        kind = ExternalErrorKind.HTTP_STATUS
    elif isinstance(exc, httpx.HTTPError):
        kind = ExternalErrorKind.NETWORK
    else:
        kind = ExternalErrorKind.NETWORK
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return ExternalSourceError(
        kind,
        source,
        f"{type(exc).__name__}: {exc}",
        url=url,
        status_code=status,
        secrets=secrets,
    )


def _raise_for_status(
    response: httpx.Response, source: str, url: str, secrets: Iterable[str | None]
) -> None:
    """Turn an error status into a typed error.

    ``ResilientHttpClient.get`` returns 4xx responses as *successes* — it only
    raises for 5xx after exhausting retries.  Without this check a 404 body is
    parsed as content, which is exactly how the "Wikipedia does not have an
    article with this exact name" page became user-visible knowledge.
    """
    status = response.status_code
    if status < 400:
        return
    kind = ExternalErrorKind.NOT_FOUND if status in (404, 410) else ExternalErrorKind.HTTP_STATUS
    raise ExternalSourceError(
        kind, source, f"HTTP {status}", url=url, status_code=status, secrets=secrets
    )


def _content_type(response: httpx.Response) -> str:
    return (response.headers.get("content-type") or "").split(";")[0].strip().lower()


async def fetch_text(
    client: ResilientHttpClient,
    url: str,
    *,
    source: str,
    source_type: SourceType,
    headers: Mapping[str, str] | None = None,
    max_bytes: int = MAX_TEXT_RESPONSE_BYTES,
    secrets: Iterable[str | None] = (),
    timeout: float | None = None,
) -> SourceResult[str]:
    """GET a textual document. Failure raises; oversize truncates *and* is flagged.

    Deliberately built on :meth:`ResilientHttpClient.get` rather than
    ``get_text``: the convenience helper discards the status code and returns
    ``""`` for every error, which makes an honest contract impossible.

    Raises:
        ExternalSourceError: for every non-success outcome, classified.
    """
    kwargs: dict[str, Any] = {"headers": dict(headers or {})}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        response = await client.get(url, **kwargs)
    except asyncio.CancelledError:
        raise
    except ExternalSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-typed below, never swallowed
        raise _classify_http_exception(exc, source, url, secrets) from exc

    _raise_for_status(response, source, url, secrets)

    ctype = _content_type(response)
    if ctype and not any(token in ctype for token in _TEXTUAL_CONTENT_TYPES):
        raise ExternalSourceError(
            ExternalErrorKind.MALFORMED_RESPONSE,
            source,
            f"expected a textual content-type, got {ctype!r}",
            url=url,
            status_code=response.status_code,
            secrets=secrets,
        )

    body = response.content
    truncated = len(body) > max_bytes
    if truncated:
        body = body[:max_bytes]
    text = body.decode(response.encoding or "utf-8", errors="replace")
    return SourceResult(
        value=text,
        provenance=Provenance.create(
            source,
            source_type,
            url=url,
            truncated=truncated,
            notes=(f"content_type={ctype}",) if ctype else (),
        ),
    )


async def fetch_json(
    client: ResilientHttpClient,
    url: str,
    *,
    source: str,
    source_type: SourceType,
    headers: Mapping[str, str] | None = None,
    max_bytes: int = MAX_JSON_RESPONSE_BYTES,
    secrets: Iterable[str | None] = (),
    timeout: float | None = None,
) -> SourceResult[Any]:
    """GET and parse JSON. Failure raises; it never degrades to ``{}``.

    An oversized body is refused (``OVERSIZED_RESPONSE``) rather than truncated:
    half a JSON document is not a smaller JSON document.

    Raises:
        ExternalSourceError: for every non-success outcome, classified.
    """
    kwargs: dict[str, Any] = {"headers": dict(headers or {})}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        response = await client.get(url, **kwargs)
    except asyncio.CancelledError:
        raise
    except ExternalSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-typed below, never swallowed
        raise _classify_http_exception(exc, source, url, secrets) from exc

    _raise_for_status(response, source, url, secrets)

    body = response.content
    if len(body) > max_bytes:
        raise ExternalSourceError(
            ExternalErrorKind.OVERSIZED_RESPONSE,
            source,
            f"response is {len(body)} bytes, limit is {max_bytes}",
            url=url,
            status_code=response.status_code,
            secrets=secrets,
        )

    ctype = _content_type(response)
    notes: list[str] = []
    if ctype and not any(token in ctype for token in _JSON_CONTENT_TYPES):
        # Not fatal on its own (wttr.in answers ``text/plain`` for ``?format=j1``),
        # but it is recorded so a provider silently switching to an HTML error
        # page is visible in provenance instead of looking like a parse bug.
        notes.append(f"unexpected_content_type={ctype}")

    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExternalSourceError(
            ExternalErrorKind.MALFORMED_RESPONSE,
            source,
            f"response is not valid JSON ({type(exc).__name__})",
            url=url,
            status_code=response.status_code,
            secrets=secrets,
        ) from exc

    return SourceResult(
        value=payload,
        provenance=Provenance.create(
            source, source_type, url=url, degraded=bool(notes), notes=notes
        ),
    )


def require_mapping(payload: Any, *, source: str, url: str | None = None) -> Mapping[str, Any]:
    """Assert a JSON payload is an object, with a typed error if it is not."""
    if not isinstance(payload, Mapping):
        raise ExternalSourceError(
            ExternalErrorKind.MALFORMED_RESPONSE,
            source,
            f"expected a JSON object, got {type(payload).__name__}",
            url=url,
        )
    return payload


# ── HTML extraction (off the loop, bounded, parser-degradation aware) ──────
_PARSER_PREFERENCE = ("lxml", "html.parser")
_STRIPPED_TAGS = ("script", "style", "noscript", "template", "svg")


def _select_parser() -> tuple[str, bool]:
    """Pick the best available parser; report whether we had to degrade.

    ``lxml`` is **not** a declared dependency of this project — it is only
    present transitively (``duckduckgo-search`` requires it).  Hard-coding
    ``BeautifulSoup(text, "lxml")`` therefore made HTML extraction one
    dependency-resolution change away from raising ``FeatureNotFound`` inside a
    broad ``except`` and silently yielding zero results forever.  The stdlib
    parser is always available, so the path degrades instead of disappearing.
    """
    from bs4 import (
        BeautifulSoup,
        FeatureNotFound,  # local import: bs4 is only needed here
    )

    for name in _PARSER_PREFERENCE:
        try:
            BeautifulSoup("", name)
        except (FeatureNotFound, Exception):  # noqa: B014 — bs4 raises broadly here
            continue
        return name, name != _PARSER_PREFERENCE[0]
    return "html.parser", True


def _extract_sync(html: str, max_chars: int) -> tuple[str, str, bool, bool]:
    """Blocking body of :func:`extract_text`. Returns (text, parser, degraded, truncated)."""
    from bs4 import BeautifulSoup

    parser, degraded = _select_parser()
    soup = BeautifulSoup(html, parser)
    for element in soup(list(_STRIPPED_TAGS)):
        element.decompose()
    text = _WHITESPACE_RE.sub(" ", soup.get_text(separator=" ", strip=True)).strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip()
    return text, parser, degraded, truncated


async def extract_text(
    html: str,
    *,
    source: str,
    max_chars: int = MAX_EXTRACTED_TEXT_CHARS,
) -> tuple[str, tuple[str, ...], bool]:
    """Extract visible text from *html* without blocking the event loop.

    Returns:
        ``(text, notes, truncated)`` — ``notes`` records the parser actually
        used and whether it was a degraded choice, so provenance can carry it.

    Raises:
        ExternalSourceError: ``MALFORMED_RESPONSE`` if the document cannot be
            parsed at all, ``DEPENDENCY_MISSING`` if BeautifulSoup is absent.
    """
    try:
        text, parser, degraded, truncated = await run_blocking(
            _extract_sync, html, max_chars, source=source, kind=ExternalErrorKind.MALFORMED_RESPONSE
        )
    except ExternalSourceError as exc:
        if "ModuleNotFoundError" in exc.detail or "ImportError" in exc.detail:
            raise ExternalSourceError(
                ExternalErrorKind.DEPENDENCY_MISSING, source, "beautifulsoup4 is not installed"
            ) from exc
        raise
    notes = (f"parser={parser}",) + (("parser_degraded",) if degraded else ())
    return text, notes, truncated


# ── DuckDuckGo backend (lazy, optional, off-loop, bounded) ─────────────────
class SearchBackend:
    """Interface a search provider must satisfy. Tests inject fakes through it."""

    async def search(
        self, kind: str, query: str, *, max_results: int
    ) -> list[dict[str, Any]]:  # pragma: no cover - interface
        raise NotImplementedError


class DuckDuckGoBackend(SearchBackend):
    """``duckduckgo_search`` behind the typed contract.

    Three things the direct ``DDGS()`` usage got wrong and this fixes:

    1. **Import at module scope.** ``from duckduckgo_search import DDGS`` at the
       top of a module makes the *whole* module unimportable when the package is
       absent.  Here the import is lazy and its absence is a typed
       ``DEPENDENCY_MISSING`` on the one call that needs it.
    2. **Construction in ``__init__``.** Every ``NewsTool()`` built one HTTP
       session; the bot builds a new tool per command.  The client is built once
       per backend, on first use.
    3. **Synchronous calls inside ``async def``.** Every call is offloaded with
       :func:`run_blocking`.
    """

    def __init__(
        self,
        factory: Callable[[], Any] | None = None,
        *,
        module_name: str = "duckduckgo_search",
    ) -> None:
        self._factory = factory
        self._module_name = module_name
        self._client: Any | None = None

    def _client_or_raise(self, source: str) -> Any:
        if self._client is not None:
            return self._client
        factory = self._factory
        if factory is None:
            try:
                module = importlib.import_module(self._module_name)
            except ImportError as exc:
                raise ExternalSourceError(
                    ExternalErrorKind.DEPENDENCY_MISSING,
                    source,
                    f"{self._module_name} is not installed",
                ) from exc
            factory = getattr(module, "DDGS", None)
            if factory is None:
                raise ExternalSourceError(
                    ExternalErrorKind.DEPENDENCY_MISSING,
                    source,
                    f"{self._module_name} exposes no DDGS entry point",
                )
        try:
            self._client = factory()
        except Exception as exc:  # noqa: BLE001 — construction failure is a provider error
            raise ExternalSourceError(
                ExternalErrorKind.PROVIDER_ERROR,
                source,
                f"could not construct the search client: {type(exc).__name__}",
            ) from exc
        return self._client

    async def search(self, kind: str, query: str, *, max_results: int) -> list[dict[str, Any]]:
        """Run ``text``/``news``/``videos`` off the loop and sanitise the rows.

        Raises:
            ExternalSourceError: ``DEPENDENCY_MISSING`` / ``PROVIDER_ERROR`` /
                ``MALFORMED_RESPONSE``.
        """
        source = f"duckduckgo:{kind}"
        client = self._client_or_raise(source)
        method = getattr(client, kind, None)
        if method is None:
            raise ExternalSourceError(
                ExternalErrorKind.PROVIDER_ERROR, source, f"backend has no {kind!r} method"
            )
        rows = await run_blocking(method, query, source=source, max_results=max_results)
        if rows is None:
            raise ExternalSourceError(
                ExternalErrorKind.MALFORMED_RESPONSE, source, "provider returned None"
            )
        if not isinstance(rows, Sequence) or isinstance(rows, str | bytes):
            raise ExternalSourceError(
                ExternalErrorKind.MALFORMED_RESPONSE,
                source,
                f"provider returned {type(rows).__name__}, expected a sequence",
            )
        # ``max_results`` is a *hint* to the provider, not a guarantee: enforce
        # the bound locally so a hostile/buggy provider cannot hand us 500 rows.
        return [dict(row) for row in rows[:max_results] if isinstance(row, Mapping)]


async def search_via(
    backend: SearchBackend,
    kind: str,
    query: str,
    *,
    max_results: int,
    source: str,
) -> list[dict[str, Any]]:
    """Call *backend* and guarantee the typed contract at the seam.

    ``SearchBackend`` is an injection point: a custom or future backend that
    raises a bare ``RuntimeError`` must not be able to punch an untyped
    exception through the zone's "every failure is an ExternalSourceError"
    guarantee.  Cancellation is re-raised unchanged.
    """
    try:
        return await backend.search(kind, query, max_results=max_results)
    except asyncio.CancelledError:
        raise
    except ExternalSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 — re-typed at the contract seam
        raise ExternalSourceError(
            ExternalErrorKind.PROVIDER_ERROR, source, f"{type(exc).__name__}: {exc}"
        ) from exc


_default_backend: DuckDuckGoBackend | None = None


def get_search_backend() -> DuckDuckGoBackend:
    """Process-wide lazy search backend (one client, not one per command)."""
    global _default_backend
    if _default_backend is None:
        _default_backend = DuckDuckGoBackend()
    return _default_backend


def dedupe_by_url(rows: Iterable[Mapping[str, Any]], *, url_key: str) -> list[Mapping[str, Any]]:
    """Drop rows whose URL was already seen (providers do repeat themselves)."""
    seen: set[str] = set()
    unique: list[Mapping[str, Any]] = []
    for row in rows:
        raw = row.get(url_key)
        if not isinstance(raw, str):
            continue
        key = raw.strip().rstrip("/").casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def is_fresh(retrieved_at: datetime, max_age: timedelta) -> bool:
    """Freshness predicate for provenance-aware callers."""
    reference = retrieved_at if retrieved_at.tzinfo else retrieved_at.replace(tzinfo=timezone.utc)
    return utcnow() - reference <= max_age


__all__ = [
    "MAX_EXTRACTED_TEXT_CHARS",
    "MAX_JSON_RESPONSE_BYTES",
    "MAX_QUERY_CHARS",
    "MAX_TEXT_RESPONSE_BYTES",
    "MAX_URL_SEGMENT_CHARS",
    "REDACTED",
    "DuckDuckGoBackend",
    "ExternalErrorKind",
    "ExternalSourceError",
    "Provenance",
    "SearchBackend",
    "SourceResult",
    "SourceType",
    "build_url",
    "cache_key_for",
    "dedupe_by_url",
    "extract_text",
    "fetch_json",
    "fetch_text",
    "get_search_backend",
    "is_fresh",
    "naive_utcnow",
    "normalize_query",
    "redact_headers",
    "redact_text",
    "redact_url",
    "require_mapping",
    "run_blocking",
    "search_via",
    "safe_path_segment",
    "utcnow",
]
