"""Knowledge orchestration: never summarise sources that did not answer.

The two defects this module was rewritten to remove
---------------------------------------------------
**1. Fabrication.**  ``learn()`` fetched Wikipedia and the web, and when *both*
returned nothing it still built a prompt reading ``Wikipedia Content: Not found /
Web Search Content: Not found`` and asked Gemini for "a comprehensive and concise
summary of this topic".  The model obliged.  The invented answer was then written
to ``KnowledgeCache`` with ``source="combined"`` and served to every user for 24
hours.  Reproduced: with both sources dead, ``learn()`` returned a confident
Persian paragraph and cached it.  A total source failure had become a durable,
authoritative-looking user-visible success.

**2. A cache that breaks itself.**  ``get_cached_knowledge`` used
``scalar_one_or_none()``, which raises ``MultipleResultsFound`` when the query
matches more than one row — and ``learn()`` inserted a row per call with no
uniqueness, no stampede guard and no key normalisation.  Reproduced: five
concurrent ``learn("stampede")`` calls produced 5 LLM calls and 5 rows, after
which *every* read of that key raised ``MultipleResultsFound``.  ``/learn`` was
permanently broken for that topic until the entries expired.

The contract now
----------------
* At least one source must really have answered, or nothing is summarised, and
  nothing is cached; the caller gets a typed ``ExternalSourceError``.
* The cache key is normalised (NFC + casefold + whitespace collapse + length
  bound), reads tolerate duplicates, and writes replace the key's rows.
* Concurrent identical learns collapse into one fetch + one LLM call.
* The answer carries its provenance: which sources answered, which failed, and
  whether the result is degraded.
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass, field
from datetime import timedelta

from sqlmodel import col, delete, select

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.core.instrumentation import instrumented
from nexus_ai_agent.integrations.external import (
    ExternalErrorKind,
    ExternalSourceError,
    Provenance,
    SourceResult,
    SourceType,
    cache_key_for,
    naive_utcnow,
    normalize_query,
    utcnow,
)
from nexus_ai_agent.knowledge.web_trainer import WebTrainer
from nexus_ai_agent.knowledge.wikipedia_trainer import WikipediaTrainer
from nexus_ai_agent.llm.gemini_provider import GeminiProvider
from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import KnowledgeCache

logger = get_logger(__name__)

SOURCE = "knowledge.learn"
CACHE_TTL = timedelta(hours=24)
MAX_CACHED_QUERY_CHARS = 256
MAX_PROMPT_SOURCE_CHARS = 6000
"""Upper bound on external text pasted into the LLM prompt."""

_SUMMARY_SYSTEM = (
    "You are an expert knowledge assistant. Summarize information accurately and "
    "professionally in Persian. Use ONLY the supplied source material. If the "
    "sources do not support a claim, do not make it."
)


# ── cache-stampede guard ───────────────────────────────────────────────────
# Keyed per event loop so a lock created in one test's loop is never awaited in
# another's (``asyncio.Lock`` binds to the loop that first acquires it).
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)
MAX_TRACKED_LOCKS = 512


def _lock_for(key: str) -> asyncio.Lock:
    """Return the process-wide in-flight lock for *key* on the current loop."""
    loop = asyncio.get_running_loop()
    registry = _locks.get(loop)
    if registry is None:
        registry = {}
        _locks[loop] = registry
    lock = registry.get(key)
    if lock is None:
        if len(registry) >= MAX_TRACKED_LOCKS:
            # Bounded: drop idle locks rather than growing without limit on a
            # stream of distinct queries.
            for idle_key in [k for k, v in registry.items() if not v.locked()][:64]:
                registry.pop(idle_key, None)
        lock = asyncio.Lock()
        registry[key] = lock
    return lock


def _release_lock(key: str) -> None:
    loop = asyncio.get_running_loop()
    registry = _locks.get(loop)
    if registry is None:
        return
    lock = registry.get(key)
    if lock is not None and not lock.locked():
        registry.pop(key, None)


# ── report ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class KnowledgeReport:
    """Everything a caller needs to judge how much to trust a summary."""

    summary: str
    sources: tuple[Provenance, ...] = ()
    failures: tuple[ExternalSourceError, ...] = ()
    from_cache: bool = False
    cache_key: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def degraded(self) -> bool:
        return bool(self.failures) or any(p.degraded or p.truncated for p in self.sources)

    def source_label(self) -> str:
        """Compact, honest value for ``KnowledgeCache.source``.

        The old code hard-coded ``"combined"`` even when nothing was combined.
        """
        if not self.sources:
            return "none"
        return "+".join(sorted({p.source for p in self.sources}))[:200]


def format_provenance_footer(report: KnowledgeReport) -> str:
    """Render a short Persian provenance block appended to the answer."""
    lines = ["", "— منابع —"]
    if report.sources:
        for provenance in report.sources:
            marks = []
            if provenance.truncated:
                marks.append("کوتاه‌شده")
            if provenance.degraded:
                marks.append("جایگزین")
            suffix = f" ({'، '.join(marks)})" if marks else ""
            lines.append(f"• {provenance.source}{suffix}")
    else:  # pragma: no cover - a report with no sources is never rendered
        lines.append("• هیچ منبعی پاسخ نداد")
    if report.failures:
        kinds = sorted({failure.kind.value for failure in report.failures})
        lines.append(f"⚠️ منابع ناموفق: {len(report.failures)} ({'، '.join(kinds)})")
    return "\n".join(lines)


class KnowledgeManager:
    """Knowledge orchestration layer with typed failures and provenance."""

    def __init__(
        self,
        gemini_provider: GeminiProvider | None = None,
        *,
        wiki: WikipediaTrainer | None = None,
        web: WebTrainer | None = None,
        db_path: str | None = None,
    ) -> None:
        self.wiki = wiki or WikipediaTrainer()
        self.web = web or WebTrainer()
        # ``None`` keeps the repository-wide resolution (PostgreSQL via
        # NEXUS_DATABASE_URL, else the default SQLite file).  An explicit path
        # pins the cache to one database — CROSS-ZONE NOTE: storage/db.py's
        # ``get_session(None)`` hard-codes "data/app.sqlite" and ignores
        # ``settings.db_path``/NEXUS_DB_PATH, so without this seam the knowledge
        # cache is untestable and unconfigurable.  Recorded, not fixed here:
        # storage/ belongs to another zone.
        self._db_path = db_path
        if gemini_provider is not None:
            self.gemini: GeminiProvider = gemini_provider
        else:
            settings = get_settings()
            self.gemini = GeminiProvider(api_key=settings.gemini_api_key or "")

    # ── cache ──────────────────────────────────────────────────────────
    async def get_cached_knowledge(self, query: str) -> str | None:
        """Return a live cache entry for *query*, or ``None`` if there is none.

        ``None`` here means "no cache", which is not a failure.  Duplicate rows
        are tolerated: the newest live one wins.  ``scalar_one_or_none()`` used
        to raise ``MultipleResultsFound`` instead, turning a duplicate — which
        the writer itself created — into a permanent outage for that key.
        """
        key = cache_key_for(query)
        async with get_session(self._db_path) as session:
            statement = (
                select(KnowledgeCache)
                .where(
                    col(KnowledgeCache.query) == key,
                    col(KnowledgeCache.expires_at) > naive_utcnow(),
                )
                .order_by(col(KnowledgeCache.expires_at).desc())
                .limit(1)
            )
            result = await session.execute(statement)
            entry = result.scalars().first()
        if entry is None or not entry.content.strip():
            return None
        return entry.content

    async def _read_cache_entry(self, key: str) -> tuple[str, str] | None:
        """Return ``(content, source_label)`` for a live cache row, or ``None``.

        The stored content is the *bare* summary: presentation (the provenance
        footer) is applied on the way out, so a value warmed by one caller is
        never a different shape from a value warmed by another.
        """
        async with get_session(self._db_path) as session:
            statement = (
                select(KnowledgeCache)
                .where(
                    col(KnowledgeCache.query) == key,
                    col(KnowledgeCache.expires_at) > naive_utcnow(),
                )
                .order_by(col(KnowledgeCache.expires_at).desc())
                .limit(1)
            )
            result = await session.execute(statement)
            entry = result.scalars().first()
        if entry is None or not entry.content.strip():
            return None
        return entry.content, entry.source or "cache"

    def _cached_report(self, key: str, content: str, source_label: str) -> KnowledgeReport:
        """Rebuild a report from a cache row, preserving the recorded sources."""
        sources = tuple(
            Provenance.create(label, SourceType.CACHE)
            for label in (source_label.split("+") if source_label else ["cache"])
            if label
        )
        return KnowledgeReport(
            summary=content,
            sources=sources or (Provenance.create("cache", SourceType.CACHE),),
            from_cache=True,
            cache_key=key,
            notes=(f"cache_source={source_label}",),
        )

    async def _store(self, key: str, content: str, source_label: str) -> None:
        """Replace every row for *key* with exactly one fresh entry.

        Delete-then-insert keeps the table from accumulating a row per call and
        makes the duplicate state that broke reads unreachable.
        """
        async with get_session(self._db_path) as session:
            await session.execute(delete(KnowledgeCache).where(col(KnowledgeCache.query) == key))
            session.add(
                KnowledgeCache(
                    query=key,
                    source=source_label,
                    content=content,
                    expires_at=naive_utcnow() + CACHE_TTL,
                )
            )
            await session.commit()

    # ── source gathering ───────────────────────────────────────────────
    async def _gather_sources(
        self, query: str
    ) -> tuple[list[tuple[str, str]], list[Provenance], list[ExternalSourceError]]:
        """Fetch every source concurrently; return (material, provenance, failures)."""
        # gather() over the awaitables directly: the sources are independent, so
        # a dead Wikipedia must not serialise in front of a healthy web search.
        # return_exceptions keeps one dead source from cancelling the other.
        wiki_outcome, web_outcome = await asyncio.gather(
            self.wiki.fetch_summary_result(query),
            self.web.search_and_summarize_result(query),
            return_exceptions=True,
        )

        material: list[tuple[str, str]] = []
        provenances: list[Provenance] = []
        failures: list[ExternalSourceError] = []

        if isinstance(wiki_outcome, BaseException):
            if isinstance(wiki_outcome, asyncio.CancelledError):
                raise wiki_outcome
            failures.append(_as_external(wiki_outcome, "wikipedia"))
        else:
            material.append(("Wikipedia", wiki_outcome.value))
            provenances.append(wiki_outcome.provenance)
            failures.extend(wiki_outcome.partial_failures)

        if isinstance(web_outcome, BaseException):
            if isinstance(web_outcome, asyncio.CancelledError):
                raise web_outcome
            failures.append(_as_external(web_outcome, "web"))
        else:
            failures.extend(web_outcome.partial_failures)
            for page in web_outcome.value:
                material.append((f"Web: {page['url']}", str(page["content"])))
                provenances.append(
                    Provenance.create(
                        f"web:{page['url']}",
                        SourceType.WEB_PAGE,
                        url=str(page["url"]),
                        truncated=bool(page.get("truncated")),
                    )
                )
        return material, provenances, failures

    # ── the operation ──────────────────────────────────────────────────
    @instrumented("knowledge.learn")
    async def learn_report(self, query: str) -> KnowledgeReport:
        """Learn about *query*, or fail loudly. Never invents an answer.

        Raises:
            ExternalSourceError: ``INVALID_INPUT`` for an unusable query, or a
                classified failure when **no** source produced material.  In the
                latter case the LLM is not called and the cache is not written —
                the whole point of the rewrite.
        """
        normalized = normalize_query(query, source=SOURCE, max_chars=MAX_CACHED_QUERY_CHARS)
        key = cache_key_for(normalized)

        entry = await self._read_cache_entry(key)
        if entry is not None:
            return self._cached_report(key, *entry)

        lock = _lock_for(key)
        async with lock:
            try:
                # Double-checked: whoever held the lock may have just filled the
                # cache, so the queued callers pay nothing.
                entry = await self._read_cache_entry(key)
                if entry is not None:
                    return self._cached_report(key, *entry)
                return await self._learn_uncached(normalized, key)
            finally:
                _release_lock(key)

    async def _learn_uncached(self, normalized: str, key: str) -> KnowledgeReport:
        material, provenances, failures = await self._gather_sources(normalized)

        if not material:
            # THE invariant: zero real sources => no summary, no cache, no lie.
            primary = failures[0] if failures else None
            raise ExternalSourceError(
                primary.kind if primary else ExternalErrorKind.EMPTY_RESULT,
                SOURCE,
                "no source produced material for this topic "
                f"({len(failures)} source failure(s): "
                f"{', '.join(sorted({f.kind.value for f in failures})) or 'none'})",
                provenance=Provenance.create(SOURCE, SourceType.LLM_SYNTHESIS, degraded=True),
            )

        summary = await self._summarize(normalized, material)
        if not summary.strip():
            # An empty model answer used to be cached, so the cache could never
            # hit and the whole pipeline re-ran on every command.
            raise ExternalSourceError(
                ExternalErrorKind.EMPTY_RESULT,
                SOURCE,
                "the summariser returned an empty answer",
            )

        report = KnowledgeReport(
            summary=summary.strip(),
            sources=tuple(provenances),
            failures=tuple(failures),
            from_cache=False,
            cache_key=key,
            notes=(f"sources_used={len(provenances)}", f"sources_failed={len(failures)}"),
        )
        await self._store(key, report.summary, report.source_label())
        logger.info(
            "knowledge_learned",
            sources_used=len(provenances),
            sources_failed=len(failures),
            degraded=report.degraded,
            cached=True,
        )
        return report

    async def _summarize(self, query: str, material: list[tuple[str, str]]) -> str:
        """Build a bounded, source-only prompt and call the summariser."""
        budget = MAX_PROMPT_SOURCE_CHARS
        blocks: list[str] = []
        for label, text in material:
            if budget <= 0:
                break
            chunk = text[:budget]
            budget -= len(chunk)
            blocks.append(f"### {label}\n{chunk}")
        prompt = (
            f"Topic: {query}\n\n"
            "Source material (this is the ONLY material you may use):\n\n"
            + "\n\n".join(blocks)
            + "\n\nProvide a comprehensive and concise summary of this topic in Persian (Farsi), "
            "grounded strictly in the source material above."
        )
        try:
            return await self.gemini.generate(prompt=prompt, system=_SUMMARY_SYSTEM)
        except asyncio.CancelledError:
            raise
        except ExternalSourceError:
            raise
        except Exception as exc:  # noqa: BLE001 — re-typed, never swallowed
            raise ExternalSourceError(
                ExternalErrorKind.PROVIDER_ERROR,
                "llm:summarizer",
                f"{type(exc).__name__}: {exc}",
            ) from exc

    async def learn_result(self, query: str) -> SourceResult[str]:
        """Typed envelope around :meth:`learn_report`."""
        report = await self.learn_report(query)
        return SourceResult(
            value=report.summary,
            provenance=Provenance(
                source=report.source_label(),
                source_type=SourceType.LLM_SYNTHESIS,
                retrieved_at=utcnow(),
                degraded=report.degraded,
                notes=report.notes,
            ),
            partial_failures=report.failures,
        )

    async def learn(self, query: str, *, include_sources: bool = True) -> str:
        """Backwards-compatible entry point for ``bot/knowledge_handlers.py``.

        Still returns ``str``; it now raises :class:`ExternalSourceError` instead
        of returning a fabricated summary when no source answered.  The handler
        already wraps this call in ``try/except Exception`` and shows the message
        to the user, so the failure is surfaced rather than silently believed.

        ``include_sources`` is a *presentation* switch only.  The provenance
        footer is rendered here, never stored, so the cached value has one
        canonical shape regardless of which caller warmed it.
        """
        report = await self.learn_report(query)
        if not include_sources:
            return report.summary
        return report.summary + format_provenance_footer(report)

    async def close(self) -> None:
        """Close underlying trainers."""
        await self.wiki.close()
        await self.web.close()


def _as_external(exc: BaseException, source: str) -> ExternalSourceError:
    """Normalise any source exception into the taxonomy without losing it."""
    if isinstance(exc, ExternalSourceError):
        return exc
    return ExternalSourceError(
        ExternalErrorKind.PROVIDER_ERROR, source, f"{type(exc).__name__}: {exc}"
    )


__all__ = [
    "CACHE_TTL",
    "MAX_PROMPT_SOURCE_CHARS",
    "KnowledgeManager",
    "KnowledgeReport",
    "format_provenance_footer",
]
