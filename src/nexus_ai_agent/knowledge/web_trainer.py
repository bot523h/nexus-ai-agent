"""Web search + page extraction with honest partial-failure reporting.

What this module is defending against
-------------------------------------
``search_and_summarize`` fetches URLs chosen by a **third party** (a search
engine) that were in turn influenced by a **user** query.  Every byte it
handles is hostile input.  The previous implementation:

* called the synchronous ``DDGS`` SDK directly inside ``async def`` — measured
  at 0 event-loop heartbeat ticks during a 0.6 s provider call;
* parsed unbounded attacker-controlled HTML with BeautifulSoup on the loop —
  8.1 MB of HTML froze the loop for 0.34 s, and nothing capped the download;
* returned ``[]`` for "provider exploded", for "no results" and for "all three
  pages failed to download" alike (all three reproduced);
* passed ``res.get("href")`` — possibly ``None`` — straight into the HTTP
  client, so a malformed provider row surfaced as a swallowed ``TypeError``;
* never deduplicated, so one page repeated three times counted as three sources.

Every one of those is fixed below, and the result now says *which* sources
succeeded, *which* failed and *why*.
"""

from __future__ import annotations

import asyncio
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
    dedupe_by_url,
    extract_text,
    fetch_text,
    get_search_backend,
    normalize_query,
    search_via,
)
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

MAX_SEARCH_RESULTS = 3
"""How many search hits are considered. Enforced locally — ``max_results`` is
only a hint to the provider."""

MAX_PAGE_CHARS = 2000
"""Per-page extracted-text budget handed to the summariser."""

MAX_CONCURRENT_FETCHES = 3
"""Pages are fetched concurrently but never unboundedly."""


class WebTrainer:
    """DuckDuckGo search plus bounded, off-loop page extraction."""

    SOURCE_SEARCH = "duckduckgo:text"
    SOURCE_PAGE = "web_page"

    def __init__(
        self,
        client: ResilientHttpClient | None = None,
        backend: SearchBackend | None = None,
    ) -> None:
        self.client = client or get_http_client()
        self.backend = backend or get_search_backend()

    async def _fetch_page(self, url: str, title: str) -> dict[str, Any]:
        """Download and extract one page. Raises a typed error on failure."""
        result = await fetch_text(
            self.client, url, source=self.SOURCE_PAGE, source_type=SourceType.WEB_PAGE
        )
        text, notes, extract_truncated = await extract_text(
            result.value, source=self.SOURCE_PAGE, max_chars=MAX_PAGE_CHARS
        )
        if not text:
            raise ExternalSourceError(
                ExternalErrorKind.EMPTY_RESULT,
                self.SOURCE_PAGE,
                "page contained no extractable text",
                url=url,
            )
        truncated = result.provenance.truncated or extract_truncated
        provenance = Provenance.create(
            self.SOURCE_PAGE,
            SourceType.WEB_PAGE,
            url=url,
            truncated=truncated,
            notes=(*result.provenance.notes, *notes),
        )
        return {
            "title": title,
            "url": url,
            "content": text,
            "truncated": truncated,
            "provenance": provenance.as_dict(),
        }

    @instrumented("knowledge.web.search")
    async def search_and_summarize_result(self, query: str) -> SourceResult[list[dict[str, Any]]]:
        """Search, then fetch each hit, reporting per-source outcomes.

        Success semantics, explicitly:

        * ``value == []`` **and** no partial failures → the search engine
          authoritatively found nothing.  A real, empty, trustworthy answer.
        * ``value == []`` **and** partial failures → every candidate page failed;
          the result is marked degraded and the reasons are attached.
        * a raised :class:`ExternalSourceError` → the *search itself* failed, so
          there is nothing to be partial about.

        Raises:
            ExternalSourceError: ``INVALID_INPUT`` for an unusable query,
                ``DEPENDENCY_MISSING``/``PROVIDER_ERROR``/``MALFORMED_RESPONSE``
                when the search provider fails.
        """
        normalized = normalize_query(query, source=self.SOURCE_SEARCH)
        rows = await search_via(
            self.backend,
            "text",
            normalized,
            max_results=MAX_SEARCH_RESULTS,
            source=self.SOURCE_SEARCH,
        )

        # Validate every row before it can reach the network layer: a row with a
        # missing/None/non-string href used to be handed to the HTTP client and
        # blow up there as a TypeError inside a broad except.
        candidates: list[tuple[str, str]] = []
        malformed = 0
        for row in dedupe_by_url(rows, url_key="href")[:MAX_SEARCH_RESULTS]:
            href = row.get("href")
            if not isinstance(href, str) or not href.strip():
                malformed += 1
                continue
            raw_title = row.get("title")
            title = (
                raw_title.strip()[:300] if isinstance(raw_title, str) and raw_title.strip() else "—"
            )
            candidates.append((href.strip(), title))
        malformed += max(0, len(rows) - len(dedupe_by_url(rows, url_key="href")))

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def guarded(url: str, title: str) -> dict[str, Any]:
            async with semaphore:
                return await self._fetch_page(url, title)

        # return_exceptions=True keeps one dead page from cancelling its
        # siblings; CancelledError is re-raised below so cancellation of the
        # whole operation still propagates instead of looking like a page error.
        gathered = await asyncio.gather(
            *(guarded(url, title) for url, title in candidates), return_exceptions=True
        )

        pages: list[dict[str, Any]] = []
        failures: list[ExternalSourceError] = []
        for (url, _title), outcome in zip(candidates, gathered, strict=True):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if isinstance(outcome, ExternalSourceError):
                    failures.append(outcome)
                else:
                    failures.append(
                        ExternalSourceError(
                            ExternalErrorKind.NETWORK,
                            self.SOURCE_PAGE,
                            f"{type(outcome).__name__}: {outcome}",
                            url=url,
                        )
                    )
                continue
            pages.append(outcome)

        for failure in failures:
            logger.warning(
                "web_page_fetch_failed",
                kind=failure.kind.value,
                url=failure.url,
                retryable=failure.retryable,
                detail=failure.detail[:200],
            )

        notes: list[str] = [
            f"search_hits={len(rows)}",
            f"fetched={len(pages)}",
            f"failed={len(failures)}",
        ]
        if malformed:
            notes.append(f"malformed_or_duplicate_rows={malformed}")

        return SourceResult(
            value=pages,
            provenance=Provenance.create(
                self.SOURCE_SEARCH,
                SourceType.WEB_SEARCH,
                degraded=bool(failures) or bool(malformed),
                notes=notes,
            ),
            partial_failures=tuple(failures),
        )

    async def search_and_summarize(self, query: str) -> list[dict[str, str]]:
        """Backwards-compatible adapter for ``bot/knowledge_handlers.py``.

        Keeps the ``[{"title", "url", "content"}]`` shape the handler renders.
        """
        try:
            result = await self.search_and_summarize_result(query)
        except ExternalSourceError as exc:
            logger.warning(
                "web_search_failed",
                kind=exc.kind.value,
                source=exc.source,
                retryable=exc.retryable,
                detail=exc.detail[:200],
            )
            return []
        return [
            {"title": str(page["title"]), "url": str(page["url"]), "content": str(page["content"])}
            for page in result.value
        ]

    async def close(self) -> None:
        """No-op: the HTTP client is a process-wide singleton owned by core/."""
        return None


__all__ = ["MAX_CONCURRENT_FETCHES", "MAX_PAGE_CHARS", "MAX_SEARCH_RESULTS", "WebTrainer"]
