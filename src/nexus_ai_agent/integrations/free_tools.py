"""Free external tools (weather, FX, news, video search) on the typed contract.

Every tool exposes two layers:

``*_result()``
    The honest API.  Returns a :class:`~nexus_ai_agent.integrations.external.SourceResult`
    carrying provenance, or raises a typed
    :class:`~nexus_ai_agent.integrations.external.ExternalSourceError`.
    New call sites should use this.

the original method name
    A thin backwards-compatible adapter for the existing out-of-zone callers in
    ``bot/tool_handlers.py`` (``dict | None``, ``float | None``,
    ``list[dict[str, str]]``).  It never fabricates data: a failure becomes
    ``None`` / ``[]`` *after* the typed error has been classified and logged
    with its kind, instead of the failure being invisible.

What changed and why
--------------------
* The NewsAPI key travelled in the URL query string, so it was written to the
  logs by ``core/http_client``'s ``get_json_failed`` handler on every failure.
  It now travels in the ``X-Api-Key`` header, and every log line, exception and
  provenance record goes through redaction.
* ``get_weather`` interpolated the raw city into the URL: ``"x?format=j1&evil=1"``
  rewrote the query and ``"../../../etc/passwd"`` rewrote the path.  All
  components are percent-encoded now.
* ``get_rate`` returned ``0.0`` when the provider quoted no IRR rate — a
  fabricated number presented as a real exchange rate.  A missing rate is now
  ``MALFORMED_RESPONSE``.
* ``DDGS`` is a synchronous SDK and was called directly inside ``async def``:
  measured at **0** event-loop heartbeat ticks during a 0.6 s provider call.
  All provider calls are offloaded.
* A single malformed provider row discarded the whole result set; rows are now
  validated individually and the good ones survive.
"""

from __future__ import annotations

from typing import Any

from nexus_ai_agent.core.http_client import ResilientHttpClient, get_http_client
from nexus_ai_agent.core.instrumentation import instrumented
from nexus_ai_agent.integrations.external import (
    ExternalErrorKind,
    ExternalSourceError,
    Provenance,
    SearchBackend,
    SourceResult,
    SourceType,
    build_url,
    dedupe_by_url,
    fetch_json,
    get_search_backend,
    normalize_query,
    redact_text,
    require_mapping,
    search_via,
)
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

WTTR_HOST = "wttr.in"
EXCHANGERATE_HOST = "api.exchangerate-api.com"
NEWSAPI_HOST = "newsapi.org"

MAX_NEWS_ITEMS = 5
MAX_VIDEO_ITEMS = 5

# Currency codes are ISO-4217: exactly three ASCII letters.  Anything else is
# rejected before it can become part of a URL.
_CURRENCY_LEN = 3


def _log_typed_failure(op: str, exc: ExternalSourceError) -> None:
    """One structured line per failure — kind, source, retryability, no secrets."""
    logger.warning(
        "external_source_failed",
        op=op,
        kind=exc.kind.value,
        source=exc.source,
        retryable=exc.retryable,
        status=exc.status_code,
        detail=exc.detail[:200],
        url=exc.url,
    )


class WeatherTool:
    """Weather from wttr.in, with an encoded URL and a typed failure contract."""

    SOURCE = "wttr.in"

    def __init__(self, client: ResilientHttpClient | None = None) -> None:
        self.client = client or get_http_client()

    @staticmethod
    def build_request_url(city: str) -> str:
        """Compose the wttr.in URL. Every user byte is percent-encoded.

        Exposed as a static method so the security invariant ("no user input can
        alter host, path depth or query") is directly testable without a fetch.
        """
        return build_url(
            host=WTTR_HOST,
            segments=[city],
            params={"format": "j1"},
            source=WeatherTool.SOURCE,
        )

    @instrumented("tools.weather")
    async def get_weather_result(self, city: str) -> SourceResult[dict[str, Any]]:
        """Fetch current conditions.

        Raises:
            ExternalSourceError: ``INVALID_INPUT`` for an unusable city,
                ``NOT_FOUND`` when wttr.in does not know it, ``NETWORK`` /
                ``TIMEOUT`` / ``CIRCUIT_OPEN`` for transport failures, and
                ``MALFORMED_RESPONSE`` when the payload lacks
                ``current_condition`` (the field every caller dereferences).
        """
        normalized = normalize_query(city, source=self.SOURCE, max_chars=128)
        url = self.build_request_url(normalized)
        result = await fetch_json(
            self.client, url, source=self.SOURCE, source_type=SourceType.WEATHER_API
        )
        payload = require_mapping(result.value, source=self.SOURCE, url=url)
        conditions = payload.get("current_condition")
        if not isinstance(conditions, list) or not conditions:
            # Contract check at the boundary: bot/tool_handlers.py indexes
            # data["current_condition"][0] unguarded, so a provider shape change
            # would otherwise surface as an IndexError inside a Telegram handler.
            raise ExternalSourceError(
                ExternalErrorKind.MALFORMED_RESPONSE,
                self.SOURCE,
                "payload has no non-empty 'current_condition'",
                url=url,
            )
        return SourceResult(value=dict(payload), provenance=result.provenance)

    async def get_weather(self, city: str) -> dict[str, Any] | None:
        """Backwards-compatible adapter for ``bot/tool_handlers.py``.

        Returns ``None`` on any failure — but only after the failure has been
        classified and logged with its kind, never as a silent ``{}``.
        """
        try:
            return (await self.get_weather_result(city)).value
        except ExternalSourceError as exc:
            _log_typed_failure("tools.weather", exc)
            return None


class CurrencyTool:
    """Exchange rates, where a missing quote is an error rather than ``0.0``."""

    SOURCE = "exchangerate-api"

    def __init__(self, client: ResilientHttpClient | None = None) -> None:
        self.client = client or get_http_client()

    @classmethod
    def normalize_currency(cls, code: str) -> str:
        """Validate an ISO-4217 code.

        Raises:
            ExternalSourceError: ``INVALID_INPUT`` for anything that is not three
                ASCII letters — this is also what keeps ``base`` out of the URL
                structure.
        """
        candidate = (code or "").strip().upper()
        if len(candidate) != _CURRENCY_LEN or not candidate.isascii() or not candidate.isalpha():
            raise ExternalSourceError(
                ExternalErrorKind.INVALID_INPUT,
                cls.SOURCE,
                f"currency code must be three ASCII letters, got {code!r}",
            )
        return candidate

    @classmethod
    def build_request_url(cls, base: str) -> str:
        return build_url(
            host=EXCHANGERATE_HOST,
            segments=["v4", "latest", cls.normalize_currency(base)],
            source=cls.SOURCE,
        )

    @instrumented("tools.currency")
    async def get_rate_result(self, base: str = "USD", quote: str = "IRR") -> SourceResult[float]:
        """Return the *quote* rate for *base*.

        Raises:
            ExternalSourceError: ``INVALID_INPUT``, transport kinds, or
                ``MALFORMED_RESPONSE`` when the provider does not quote
                *quote* / quotes a non-positive or non-numeric value.  It never
                substitutes ``0`` for "unknown".
        """
        base_code = self.normalize_currency(base)
        quote_code = self.normalize_currency(quote)
        url = self.build_request_url(base_code)
        result = await fetch_json(
            self.client, url, source=self.SOURCE, source_type=SourceType.FX_API
        )
        payload = require_mapping(result.value, source=self.SOURCE, url=url)
        rates = payload.get("rates")
        if not isinstance(rates, dict):
            raise ExternalSourceError(
                ExternalErrorKind.MALFORMED_RESPONSE,
                self.SOURCE,
                "payload has no 'rates' object",
                url=url,
            )
        if quote_code not in rates:
            raise ExternalSourceError(
                ExternalErrorKind.MALFORMED_RESPONSE,
                self.SOURCE,
                f"provider quoted no {quote_code} rate for {base_code}",
                url=url,
            )
        raw = rates[quote_code]
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ExternalSourceError(
                ExternalErrorKind.MALFORMED_RESPONSE,
                self.SOURCE,
                f"{quote_code} rate is not a number ({type(raw).__name__})",
                url=url,
            ) from exc
        if value <= 0 or value != value or value in (float("inf"), float("-inf")):
            raise ExternalSourceError(
                ExternalErrorKind.MALFORMED_RESPONSE,
                self.SOURCE,
                f"{quote_code} rate is not a usable positive number",
                url=url,
            )
        provenance = result.provenance.with_notes(f"pair={base_code}/{quote_code}")
        return SourceResult(value=value, provenance=provenance)

    async def get_rate(self, base: str = "USD") -> float | None:
        """Backwards-compatible adapter for ``bot/tool_handlers.py``.

        ``None`` means "no rate" — previously this could return ``0.0``, which
        the handler rendered as "0 تومان" for a perfectly reachable provider
        that simply did not quote the pair.
        """
        try:
            return (await self.get_rate_result(base)).value
        except ExternalSourceError as exc:
            _log_typed_failure("tools.currency", exc)
            return None


class NewsTool:
    """News via NewsAPI (key in a header) with an explicit DuckDuckGo fallback."""

    SOURCE_API = "newsapi.org"
    SOURCE_SEARCH = "duckduckgo:news"

    def __init__(
        self,
        api_key: str | None = None,
        client: ResilientHttpClient | None = None,
        backend: SearchBackend | None = None,
    ) -> None:
        self.api_key = api_key
        self.client = client or get_http_client()
        self.backend = backend or get_search_backend()

    @classmethod
    def build_request_url(cls, query: str) -> str:
        """Compose the NewsAPI URL.

        The key is **structurally absent** here: it cannot be forgotten from the
        redaction list because it was never in the URL to begin with.
        """
        return build_url(
            host=NEWSAPI_HOST,
            segments=["v2", "everything"],
            params={"q": query, "pageSize": str(MAX_NEWS_ITEMS)},
            source=cls.SOURCE_API,
        )

    def _auth_headers(self) -> dict[str, str]:
        """NewsAPI accepts the key as an ``X-Api-Key`` request header."""
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    async def _news_from_api(self, query: str) -> SourceResult[list[dict[str, str]]]:
        url = self.build_request_url(query)
        result = await fetch_json(
            self.client,
            url,
            source=self.SOURCE_API,
            source_type=SourceType.NEWS_API,
            headers=self._auth_headers(),
            secrets=(self.api_key,),
        )
        payload = require_mapping(result.value, source=self.SOURCE_API, url=url)
        if payload.get("status") == "error":
            raise ExternalSourceError(
                ExternalErrorKind.PROVIDER_ERROR,
                self.SOURCE_API,
                str(payload.get("message", "provider reported an error")),
                url=url,
                secrets=(self.api_key,),
            )
        articles = payload.get("articles")
        if not isinstance(articles, list):
            raise ExternalSourceError(
                ExternalErrorKind.MALFORMED_RESPONSE,
                self.SOURCE_API,
                "payload has no 'articles' list",
                url=url,
                secrets=(self.api_key,),
            )
        items = _coerce_link_rows(articles, title_key="title", url_key="url")
        return SourceResult(value=items[:MAX_NEWS_ITEMS], provenance=result.provenance)

    async def _news_from_search(self, query: str) -> SourceResult[list[dict[str, str]]]:
        rows = await search_via(
            self.backend, "news", query, max_results=MAX_NEWS_ITEMS, source=self.SOURCE_SEARCH
        )
        items = _coerce_link_rows(rows, title_key="title", url_key="url")
        return SourceResult(
            value=items[:MAX_NEWS_ITEMS],
            provenance=Provenance.create(self.SOURCE_SEARCH, SourceType.NEWS_SEARCH),
        )

    @instrumented("tools.news")
    async def get_news_result(self, query: str) -> SourceResult[list[dict[str, str]]]:
        """Fetch headlines, recording which provider actually answered.

        The fallback is *explicit*: when the keyed API fails, the DuckDuckGo
        result is marked ``degraded`` and the API failure is attached as a
        partial failure, so a caller can tell "5 headlines from NewsAPI" from
        "5 headlines because NewsAPI is down".

        Raises:
            ExternalSourceError: when **every** provider failed; the last error
                is raised and the earlier one is preserved via ``__cause__``.
        """
        normalized = normalize_query(query, source=self.SOURCE_API)
        api_failure: ExternalSourceError | None = None
        if self.api_key:
            try:
                return await self._news_from_api(normalized)
            except ExternalSourceError as exc:
                _log_typed_failure("tools.news", exc)
                api_failure = exc

        try:
            fallback = await self._news_from_search(normalized)
        except ExternalSourceError as exc:
            if api_failure is not None:
                raise exc from api_failure
            raise

        if api_failure is None:
            return fallback
        return SourceResult(
            value=fallback.value,
            provenance=Provenance.create(
                fallback.provenance.source,
                fallback.provenance.source_type,
                degraded=True,
                fallback_path=(f"{self.SOURCE_API}:{api_failure.kind.value}", self.SOURCE_SEARCH),
            ),
            partial_failures=(api_failure,),
        )

    async def get_news(self, query: str) -> list[dict[str, str]]:
        """Backwards-compatible adapter for ``bot/tool_handlers.py``."""
        try:
            return (await self.get_news_result(query)).value
        except ExternalSourceError as exc:
            _log_typed_failure("tools.news", exc)
            return []


class YouTubeSearchTool:
    """Video search through the DuckDuckGo backend."""

    SOURCE = "duckduckgo:videos"

    def __init__(self, backend: SearchBackend | None = None) -> None:
        self.backend = backend or get_search_backend()

    @instrumented("tools.youtube")
    async def search_result(self, query: str) -> SourceResult[list[dict[str, str]]]:
        """Search videos.

        Raises:
            ExternalSourceError: ``INVALID_INPUT`` for an unusable query,
                ``DEPENDENCY_MISSING`` when the SDK is absent, or
                ``PROVIDER_ERROR`` when the provider raises.  An authoritative
                empty answer is a **success** with an empty list, which is a
                different outcome from a provider that exploded.
        """
        normalized = normalize_query(query, source=self.SOURCE)
        rows = await search_via(
            self.backend, "videos", normalized, max_results=MAX_VIDEO_ITEMS, source=self.SOURCE
        )
        # DDGS' video rows put the watch URL in "content"; "href" appears on some
        # backends.  Accept either rather than KeyError-ing the whole page away.
        items = _coerce_link_rows(
            rows, title_key="title", url_key="content", alt_url_keys=("href",)
        )
        dropped = len(rows) - len(items)
        provenance = Provenance.create(
            self.SOURCE,
            SourceType.VIDEO_SEARCH,
            degraded=bool(dropped),
            notes=(f"dropped_malformed_rows={dropped}",) if dropped else (),
        )
        return SourceResult(value=items[:MAX_VIDEO_ITEMS], provenance=provenance)

    async def search(self, query: str) -> list[dict[str, str]]:
        """Backwards-compatible adapter for ``bot/tool_handlers.py``."""
        try:
            return (await self.search_result(query)).value
        except ExternalSourceError as exc:
            _log_typed_failure("tools.youtube", exc)
            return []


def _coerce_link_rows(
    rows: Any,
    *,
    title_key: str,
    url_key: str,
    alt_url_keys: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """Turn hostile provider rows into ``{"title", "url"}`` pairs, dropping junk.

    Every element is validated independently.  The previous list comprehension
    (``[{"title": r["title"], "url": r["url"]} for r in rows]``) raised
    ``KeyError`` on the *first* malformed row and a broad ``except`` then threw
    away every good row with it — reproduced: 1 good + 1 bad row yielded ``[]``.
    """
    if not isinstance(rows, list):
        return []
    cleaned: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url_value: Any = row.get(url_key)
        for alt in alt_url_keys:
            if not isinstance(url_value, str) or not url_value.strip():
                url_value = row.get(alt)
        if not isinstance(url_value, str) or not url_value.strip():
            continue
        title_value = row.get(title_key)
        title = title_value.strip() if isinstance(title_value, str) and title_value.strip() else "—"
        cleaned.append(
            {"title": redact_text(title)[:300], "url": redact_text(url_value.strip())[:2000]}
        )
    return [dict(row) for row in dedupe_by_url(cleaned, url_key="url")]


__all__ = ["CurrencyTool", "NewsTool", "WeatherTool", "YouTubeSearchTool"]
