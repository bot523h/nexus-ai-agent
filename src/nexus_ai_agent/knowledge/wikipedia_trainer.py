"""Wikipedia retrieval that cannot present a missing article as knowledge.

The defect this module was rewritten to remove
----------------------------------------------
The previous implementation fetched ``https://{lang}.wikipedia.org/wiki/{query}``
through ``ResilientHttpClient.get_text`` and scraped ``<p>`` tags out of whatever
came back.  ``get_text`` returns the body of a **404** exactly like the body of a
**200**, so for a topic Wikipedia does not have, the bot answered its user with:

    "Wikipedia does not have an article with this exact name. Please search
     for Zzzqqq in Wikipedia to check for alternative titles."

presented as the encyclopaedic summary of "Zzzqqq".  A source failure had become
a user-visible success — the exact inversion Rule 4 forbids.  Three further
defects lived in the same eight lines:

* ``lang`` was interpolated straight into the authority, so ``lang`` =
  ``"evil.example.com/steal?x="`` produced a request to ``evil.example.com``
  (reproduced);
* ``query`` was interpolated straight into the path, so ``"x?action=raw"``
  rewrote the query string and ``"a/b/../../c"`` rewrote the path depth;
* the 1000-character cap was evaluated *after* appending a paragraph, so one
  900 000-character ``<p>`` returned 900 000 characters (reproduced).

The fix, at the root
--------------------
HTML gives no machine-readable answer to "does this article exist?".  The REST
summary endpoint does: it answers **404** for a missing title and tags
disambiguation pages with ``type`` — and it returns a bounded JSON extract plus
a revision timestamp, which is real freshness provenance.  So the source of
truth moved from scraping to ``/api/rest_v1/page/summary/{title}``, the URL is
composed component-by-component, and every failure is typed.
"""

from __future__ import annotations

import re
from typing import Any

from nexus_ai_agent.core.http_client import ResilientHttpClient, get_http_client
from nexus_ai_agent.core.instrumentation import instrumented
from nexus_ai_agent.integrations.external import (
    ExternalErrorKind,
    ExternalSourceError,
    Provenance,
    SourceResult,
    SourceType,
    build_url,
    fetch_json,
    normalize_query,
    require_mapping,
)
from nexus_ai_agent.observability.logging import get_logger

logger = get_logger(__name__)

WIKIPEDIA_HOST_TEMPLATE = "{lang}.wikipedia.org"
SUMMARY_SEGMENTS = ("api", "rest_v1", "page", "summary")

DEFAULT_LANG = "fa"
FALLBACK_LANG = "en"

MAX_SUMMARY_CHARS = 1000
"""Hard cap applied *before* returning, not after appending."""

# A Wikipedia language code is a BCP-47-ish subtag: letters, digits and single
# hyphens ("fa", "en", "zh-yue", "be-tarask").  Anything else — in particular
# anything containing "." or "/" — cannot become part of the host.
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")

# Page types the REST API uses to say "this is not an article".
_NON_ARTICLE_TYPES = frozenset({"disambiguation", "no-extract", "mainpage"})


def validate_lang(lang: str) -> str:
    """Return a safe Wikipedia language subtag.

    Raises:
        ExternalSourceError: ``INVALID_INPUT`` for anything that could alter the
            request authority.  This is the guard that makes
            ``lang="evil.example.com/steal?x="`` impossible.
    """
    candidate = (lang or "").strip().lower()
    if not _LANG_RE.match(candidate):
        raise ExternalSourceError(
            ExternalErrorKind.INVALID_INPUT,
            "wikipedia",
            f"illegal language subtag {lang!r}",
        )
    return candidate


def wikipedia_title(query: str) -> str:
    """Wikipedia's title convention: spaces become underscores.

    Percent-encoding is **not** applied here — :func:`build_url` owns that, and
    doing it twice would double-escape the title.
    """
    return query.replace(" ", "_")


class WikipediaTrainer:
    """Fetches article summaries with typed failures and real provenance."""

    SOURCE_PREFIX = "wikipedia"

    def __init__(self, client: ResilientHttpClient | None = None) -> None:
        self.client = client or get_http_client()

    @classmethod
    def build_summary_url(cls, query: str, lang: str) -> str:
        """Compose the REST summary URL with a validated host and encoded title."""
        safe_lang = validate_lang(lang)
        host = WIKIPEDIA_HOST_TEMPLATE.format(lang=safe_lang)
        return build_url(
            host=host,
            segments=[*SUMMARY_SEGMENTS, wikipedia_title(query)],
            source=f"{cls.SOURCE_PREFIX}:{safe_lang}",
        )

    async def _fetch_one(self, query: str, lang: str) -> SourceResult[str]:
        """Fetch exactly one language edition. Raises on every failure."""
        safe_lang = validate_lang(lang)
        source = f"{self.SOURCE_PREFIX}:{safe_lang}"
        url = self.build_summary_url(query, safe_lang)
        result = await fetch_json(
            self.client, url, source=source, source_type=SourceType.ENCYCLOPEDIA
        )
        payload = require_mapping(result.value, source=source, url=url)

        page_type = str(payload.get("type", "")).lower()
        # "standard" is the only type that is an article; a disambiguation page
        # is a list of other titles, not an answer, and must not be summarised
        # as though it were one.
        if any(marker in page_type for marker in _NON_ARTICLE_TYPES):
            raise ExternalSourceError(
                ExternalErrorKind.NOT_FOUND,
                source,
                f"page is not an article (type={page_type or 'unknown'})",
                url=url,
            )

        extract = payload.get("extract")
        if not isinstance(extract, str) or not extract.strip():
            raise ExternalSourceError(
                ExternalErrorKind.EMPTY_RESULT,
                source,
                "article exists but has no extract",
                url=url,
            )

        text = extract.strip()
        truncated = len(text) > MAX_SUMMARY_CHARS
        if truncated:
            text = text[:MAX_SUMMARY_CHARS].rstrip()

        notes: list[str] = []
        title = payload.get("title")
        if isinstance(title, str) and title.strip():
            notes.append(f"title={title.strip()[:120]}")
        revision = payload.get("timestamp")
        if isinstance(revision, str) and revision.strip():
            # real freshness: when the article itself was last edited
            notes.append(f"revision_timestamp={revision.strip()[:40]}")

        return SourceResult(
            value=text,
            provenance=Provenance.create(
                source,
                SourceType.ENCYCLOPEDIA,
                url=url,
                truncated=truncated,
                notes=notes,
            ),
        )

    @instrumented("knowledge.wikipedia.fetch")
    async def fetch_summary_result(self, query: str, lang: str = DEFAULT_LANG) -> SourceResult[str]:
        """Fetch a summary, falling back to English **only** for a missing article.

        The fallback condition is the important part.  Previously *any* falsy
        body (including a dead network) triggered the ``fa`` → ``en`` retry, so a
        network outage silently doubled the request count and then reported
        "not found".  Here only ``NOT_FOUND``/``EMPTY_RESULT`` — the source
        authoritatively saying "no such article" — is worth asking another
        edition; a timeout is propagated as a timeout.

        Raises:
            ExternalSourceError: typed, with ``fallback_path`` recorded in the
                provenance of a successful degraded result.
        """
        normalized = normalize_query(query, source=self.SOURCE_PREFIX)
        primary_lang = validate_lang(lang)
        try:
            return await self._fetch_one(normalized, primary_lang)
        except ExternalSourceError as primary_exc:
            fallback_worthwhile = primary_exc.kind in (
                ExternalErrorKind.NOT_FOUND,
                ExternalErrorKind.EMPTY_RESULT,
            )
            if not fallback_worthwhile or primary_lang == FALLBACK_LANG:
                raise
            logger.info(
                "wikipedia_language_fallback",
                from_lang=primary_lang,
                to_lang=FALLBACK_LANG,
                kind=primary_exc.kind.value,
            )
            fallback = await self._fetch_one(normalized, FALLBACK_LANG)
            return SourceResult(
                value=fallback.value,
                provenance=Provenance(
                    source=fallback.provenance.source,
                    source_type=fallback.provenance.source_type,
                    url=fallback.provenance.url,
                    retrieved_at=fallback.provenance.retrieved_at,
                    truncated=fallback.provenance.truncated,
                    degraded=True,
                    fallback_path=(
                        f"{self.SOURCE_PREFIX}:{primary_lang}:{primary_exc.kind.value}",
                        f"{self.SOURCE_PREFIX}:{FALLBACK_LANG}",
                    ),
                    notes=fallback.provenance.notes,
                ),
                partial_failures=(primary_exc,),
            )

    async def fetch_summary(self, query: str, lang: str = DEFAULT_LANG) -> str | None:
        """Backwards-compatible adapter for ``bot/knowledge_handlers.py``.

        ``None`` still means "nothing to show", but the reason is now classified
        and logged instead of being indistinguishable from a network outage.
        """
        try:
            return (await self.fetch_summary_result(query, lang)).value
        except ExternalSourceError as exc:
            logger.warning(
                "wikipedia_fetch_failed",
                kind=exc.kind.value,
                source=exc.source,
                retryable=exc.retryable,
                status=exc.status_code,
                detail=exc.detail[:200],
            )
            return None

    async def close(self) -> None:
        """No-op: the HTTP client is a process-wide singleton owned by core/."""
        return None


def summary_payload_is_article(payload: Any) -> bool:
    """Public predicate used by tests and callers to reason about REST payloads."""
    if not isinstance(payload, dict):
        return False
    page_type = str(payload.get("type", "")).lower()
    if any(marker in page_type for marker in _NON_ARTICLE_TYPES):
        return False
    extract = payload.get("extract")
    return isinstance(extract, str) and bool(extract.strip())


__all__ = [
    "DEFAULT_LANG",
    "FALLBACK_LANG",
    "MAX_SUMMARY_CHARS",
    "WikipediaTrainer",
    "summary_payload_is_article",
    "validate_lang",
    "wikipedia_title",
]
