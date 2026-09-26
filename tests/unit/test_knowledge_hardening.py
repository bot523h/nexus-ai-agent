"""Hardening tests for ``knowledge/`` — Wikipedia, web retrieval, orchestration.

Pre-fix behaviour each test locks out (all reproduced before the rewrite):

======  ===================================================================
F18     ``https://{lang}.wikipedia.org/wiki/{query}`` built by f-string:
        ``lang="evil.example.com/steal?x="`` produced a request to
        ``evil.example.com``; ``query="x?action=raw"`` rewrote the query
F21     a dead network and a missing article both returned ``None``
F23     the 1000-char cap was checked *after* appending, so one 900 000-char
        paragraph returned 900 000 characters
F24     a 404 "Wikipedia does not have an article with this exact name" page
        was scraped and served to the user as the article summary
F13     8.1 MB of attacker-controlled HTML was parsed on the event loop
        (0 heartbeat ticks) with no download cap
F15     search failure, zero results, and "all three pages failed" were all
        the same ``[]``
F11     a provider row without ``href`` was passed to the HTTP client as
        ``None`` and surfaced as a swallowed ``TypeError``
F25     with both sources dead, ``learn()`` still asked the LLM for a summary
        of "Not found / Not found" and cached the invention for 24 h
F26     the cache key was the raw query: 4 spellings = 4 rows + 4 LLM calls
F28/29  no stampede guard, so 5 concurrent learns wrote 5 rows, after which
        ``scalar_one_or_none()`` raised ``MultipleResultsFound`` forever
F33     an empty LLM answer was cached, so the cache could never hit
======  ===================================================================
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_external_contracts import FakeHttpClient, count_heartbeat_ticks, make_response

from nexus_ai_agent.integrations.external import (
    ExternalErrorKind,
    ExternalSourceError,
    SearchBackend,
)
from nexus_ai_agent.knowledge.knowledge_manager import (
    KnowledgeManager,
    KnowledgeReport,
    format_provenance_footer,
)
from nexus_ai_agent.knowledge.web_trainer import MAX_PAGE_CHARS, MAX_SEARCH_RESULTS, WebTrainer
from nexus_ai_agent.knowledge.wikipedia_trainer import (
    MAX_SUMMARY_CHARS,
    WikipediaTrainer,
    validate_lang,
)


def json_response(payload: Any, *, status: int = 200) -> httpx.Response:
    return make_response(
        status=status, content=json.dumps(payload).encode(), content_type="application/json"
    )


def article(extract: str, *, title: str = "Iran", page_type: str = "standard") -> dict[str, Any]:
    return {
        "type": page_type,
        "title": title,
        "extract": extract,
        "timestamp": "2026-05-01T10:00:00Z",
    }


class FakeBackend(SearchBackend):
    def __init__(self, rows: Any = None, error: BaseException | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[tuple[str, str, int]] = []

    async def search(self, kind: str, query: str, *, max_results: int) -> list[dict[str, Any]]:
        self.calls.append((kind, query, max_results))
        if self.error is not None:
            raise self.error
        return list(self.rows)


# ==========================================================================
# WikipediaTrainer — URL safety (F18)
# ==========================================================================
@pytest.mark.parametrize(
    "hostile_lang",
    [
        "evil.example.com/steal?x=",
        "fa.evil.com",
        "../en",
        "fa/x",
        "fa?x=1",
        "",
        "f",
        "toolongsubtag",
        "fa.",
    ],
)
def test_a_f18_language_subtag_cannot_rewrite_the_request_authority(hostile_lang: str) -> None:
    with pytest.raises(ExternalSourceError) as caught:
        WikipediaTrainer.build_summary_url("Iran", hostile_lang)
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT


@pytest.mark.parametrize("good", ["fa", "en", "de", "zh-yue", "be-tarask", "nds-nl"])
def test_r_f18_legitimate_language_subtags_are_accepted(good: str) -> None:
    assert validate_lang(good) == good
    assert (
        httpx.URL(WikipediaTrainer.build_summary_url("Iran", good)).host == f"{good}.wikipedia.org"
    )


@pytest.mark.parametrize(
    "hostile_query",
    ["x?action=raw", "a/b/../../c", "a#frag", "Iran&x=1", "a%2fb", "هوش مصنوعی", "a\u202eb"],
)
def test_a_f18b_the_query_cannot_rewrite_the_path_or_the_query_string(hostile_query: str) -> None:
    url = WikipediaTrainer.build_summary_url(hostile_query, "fa")
    parsed = httpx.URL(url)
    assert parsed.host == "fa.wikipedia.org"
    assert parsed.query == b""
    assert parsed.fragment == ""
    # /api/rest_v1/page/summary/<one encoded segment>
    from urllib.parse import urlsplit

    assert urlsplit(url).path.count("/") == 5, url


def test_r_f18c_spaces_become_underscores_per_wikipedia_convention() -> None:
    url = WikipediaTrainer.build_summary_url("هوش مصنوعی", "fa")
    assert url.endswith("/%D9%87%D9%88%D8%B4_%D9%85%D8%B5%D9%86%D9%88%D8%B9%DB%8C")


# ==========================================================================
# WikipediaTrainer — a missing article is not knowledge (F24, F21)
# ==========================================================================
async def test_r_f24_a_404_page_is_never_presented_as_an_article() -> None:
    not_found_page = (
        "<html><body><p>Wikipedia does not have an article with this exact name.</p></body></html>"
    )
    client = FakeHttpClient(make_response(status=404, content=not_found_page))
    trainer = WikipediaTrainer(client=client)
    with pytest.raises(ExternalSourceError) as caught:
        await trainer.fetch_summary_result("Zzzqqq", "en")
    assert caught.value.kind is ExternalErrorKind.NOT_FOUND
    assert "does not have an article" not in str(caught.value)


async def test_r_f24b_legacy_adapter_returns_none_for_a_missing_article() -> None:
    client = FakeHttpClient(
        make_response(status=404, content=b"nope"), make_response(status=404, content=b"nope")
    )
    assert await WikipediaTrainer(client=client).fetch_summary("Zzzqqq") is None


@pytest.mark.parametrize("page_type", ["disambiguation", "no-extract", "mainpage"])
async def test_a_f24c_a_disambiguation_page_is_not_an_article(page_type: str) -> None:
    payload = article("Iran may refer to: ...", page_type=page_type)
    client = FakeHttpClient(json_response(payload))
    with pytest.raises(ExternalSourceError) as caught:
        await WikipediaTrainer(client=client).fetch_summary_result("Iran", "en")
    assert caught.value.kind is ExternalErrorKind.NOT_FOUND


async def test_a_f24d_an_article_with_an_empty_extract_is_empty_not_a_summary() -> None:
    for extract in ["", "   ", None, 42]:
        client = FakeHttpClient(json_response(article(extract)))  # type: ignore[arg-type]
        with pytest.raises(ExternalSourceError) as caught:
            await WikipediaTrainer(client=client).fetch_summary_result("Iran", "en")
        assert caught.value.kind is ExternalErrorKind.EMPTY_RESULT


async def test_r_f21_network_failure_and_missing_article_are_distinguishable() -> None:
    dead = WikipediaTrainer(client=FakeHttpClient(httpx.ConnectError("down")))
    with pytest.raises(ExternalSourceError) as network:
        await dead.fetch_summary_result("Iran", "en")

    missing = WikipediaTrainer(client=FakeHttpClient(make_response(status=404)))
    with pytest.raises(ExternalSourceError) as absent:
        await missing.fetch_summary_result("Iran", "en")

    assert network.value.kind is ExternalErrorKind.NETWORK
    assert absent.value.kind is ExternalErrorKind.NOT_FOUND
    assert network.value.retryable is True
    assert absent.value.retryable is False


async def test_r_f21b_a_timeout_does_not_trigger_the_english_fallback() -> None:
    """Pre-fix any falsy body triggered the fa->en retry, so an outage silently
    doubled the request count and then reported 'not found'."""
    client = FakeHttpClient(httpx.ReadTimeout("slow"))
    with pytest.raises(ExternalSourceError) as caught:
        await WikipediaTrainer(client=client).fetch_summary_result("Iran", "fa")
    assert caught.value.kind is ExternalErrorKind.TIMEOUT
    assert len(client.calls) == 1, "a transport failure must not fan out to another edition"


async def test_r_fallback_to_english_happens_only_for_a_missing_article() -> None:
    client = FakeHttpClient(make_response(status=404), json_response(article("Iran is a country.")))
    trainer = WikipediaTrainer(client=client)
    result = await trainer.fetch_summary_result("Iran", "fa")
    assert result.value == "Iran is a country."
    assert result.provenance.degraded is True
    assert result.provenance.fallback_path == ("wikipedia:fa:not_found", "wikipedia:en")
    assert len(client.calls) == 2
    assert "fa.wikipedia.org" in client.urls[0] and "en.wikipedia.org" in client.urls[1]


async def test_a_english_missing_too_raises_instead_of_recursing() -> None:
    client = FakeHttpClient(make_response(status=404), make_response(status=404))
    with pytest.raises(ExternalSourceError) as caught:
        await WikipediaTrainer(client=client).fetch_summary_result("Zzzqqq", "fa")
    assert caught.value.kind is ExternalErrorKind.NOT_FOUND
    assert len(client.calls) == 2, "the fallback must not recurse further"


# ==========================================================================
# WikipediaTrainer — the cap (F23) and provenance
# ==========================================================================
async def test_r_f23_the_summary_cap_is_enforced_before_returning() -> None:
    client = FakeHttpClient(json_response(article("y" * 900_000)))
    result = await WikipediaTrainer(client=client).fetch_summary_result("Iran", "en")
    assert len(result.value) <= MAX_SUMMARY_CHARS
    assert result.provenance.truncated is True
    assert result.degraded is True


async def test_r_short_summaries_are_not_marked_truncated() -> None:
    client = FakeHttpClient(json_response(article("Iran is a country.")))
    result = await WikipediaTrainer(client=client).fetch_summary_result("Iran", "en")
    assert result.provenance.truncated is False
    assert result.degraded is False


async def test_r_provenance_records_the_article_revision_timestamp() -> None:
    """Rule 8 freshness: the REST payload knows when the article was last edited."""
    client = FakeHttpClient(json_response(article("Iran is a country.")))
    result = await WikipediaTrainer(client=client).fetch_summary_result("Iran", "en")
    assert any(note.startswith("revision_timestamp=") for note in result.provenance.notes)
    assert any(note.startswith("title=") for note in result.provenance.notes)
    assert result.provenance.source == "wikipedia:en"


@pytest.mark.parametrize("junk", [b"not json", b"[1,2,3]", b'"a string"', b"null"])
async def test_a_malformed_wikipedia_payloads_are_typed(junk: bytes) -> None:
    client = FakeHttpClient(make_response(content=junk, content_type="application/json"))
    with pytest.raises(ExternalSourceError) as caught:
        await WikipediaTrainer(client=client).fetch_summary_result("Iran", "en")
    assert caught.value.kind is ExternalErrorKind.MALFORMED_RESPONSE


async def test_a_empty_query_never_reaches_wikipedia() -> None:
    client = FakeHttpClient()
    with pytest.raises(ExternalSourceError) as caught:
        await WikipediaTrainer(client=client).fetch_summary_result("   ")
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT
    assert client.calls == []


# --- M: mutation probe for the "404 is not content" invariant --------------
def test_m_f24_guard_is_red_when_error_statuses_are_not_checked() -> None:
    """``ResilientHttpClient.get_text`` semantics: body regardless of status."""
    response = make_response(status=404, content="Wikipedia does not have an article")
    broken_text = response.text  # what get_text() would have returned
    with pytest.raises(AssertionError):
        assert "does not have an article" not in broken_text


# ==========================================================================
# WebTrainer — partial failure is visible (F15, F11, F13)
# ==========================================================================
async def test_r_f15_search_failure_raises_rather_than_returning_empty() -> None:
    trainer = WebTrainer(client=FakeHttpClient(), backend=FakeBackend(error=RuntimeError("boom")))
    with pytest.raises(ExternalSourceError) as caught:
        await trainer.search_and_summarize_result("q")
    assert caught.value.kind is ExternalErrorKind.PROVIDER_ERROR


async def test_r_f15b_zero_results_is_a_clean_success_not_a_degradation() -> None:
    trainer = WebTrainer(client=FakeHttpClient(), backend=FakeBackend(rows=[]))
    result = await trainer.search_and_summarize_result("q")
    assert result.value == []
    assert result.partial_failures == ()
    assert result.degraded is False


async def test_r_f15c_every_page_failing_is_reported_not_silently_empty() -> None:
    rows = [{"href": f"https://a{i}.test", "title": f"t{i}"} for i in range(3)]
    client = FakeHttpClient(
        httpx.ConnectError("down"), httpx.ReadTimeout("slow"), make_response(status=404)
    )
    trainer = WebTrainer(client=client, backend=FakeBackend(rows=rows))
    result = await trainer.search_and_summarize_result("q")
    assert result.value == []
    assert len(result.partial_failures) == 3
    assert {f.kind for f in result.partial_failures} == {
        ExternalErrorKind.NETWORK,
        ExternalErrorKind.TIMEOUT,
        ExternalErrorKind.NOT_FOUND,
    }
    assert result.degraded is True


async def test_r_partial_failure_keeps_the_pages_that_worked() -> None:
    rows = [{"href": f"https://a{i}.test", "title": f"t{i}"} for i in range(3)]
    client = FakeHttpClient(
        make_response(content="<html><p>alpha</p></html>"),
        httpx.ConnectError("down"),
        make_response(content="<html><p>gamma</p></html>"),
    )
    trainer = WebTrainer(client=client, backend=FakeBackend(rows=rows))
    result = await trainer.search_and_summarize_result("q")
    assert [page["content"] for page in result.value] == ["alpha", "gamma"]
    assert len(result.partial_failures) == 1
    assert result.degraded is True
    assert any("fetched=2" in note for note in result.provenance.notes)
    assert any("failed=1" in note for note in result.provenance.notes)


@pytest.mark.parametrize(
    "bad_row", [{"title": "no href"}, {"href": None}, {"href": ""}, {"href": "   "}, {"href": 42}]
)
async def test_a_f11_a_row_without_a_usable_url_is_rejected_before_the_network(
    bad_row: dict[str, Any],
) -> None:
    client = FakeHttpClient()
    trainer = WebTrainer(client=client, backend=FakeBackend(rows=[bad_row]))
    result = await trainer.search_and_summarize_result("q")
    assert result.value == []
    assert client.calls == [], "a malformed row must never reach the HTTP client"
    assert any("malformed_or_duplicate_rows=" in note for note in result.provenance.notes)


async def test_a_duplicate_search_hits_count_as_one_source() -> None:
    rows = [
        {"href": "https://a.test/x", "title": "a"},
        {"href": "https://A.test/x/", "title": "a again"},
        {"href": "https://b.test/y", "title": "b"},
    ]
    client = FakeHttpClient(
        make_response(content="<p>one</p>"), make_response(content="<p>two</p>")
    )
    trainer = WebTrainer(client=client, backend=FakeBackend(rows=rows))
    result = await trainer.search_and_summarize_result("q")
    assert len(result.value) == 2
    assert len(client.calls) == 2


async def test_a_f7_provider_flooding_is_capped_at_the_declared_budget() -> None:
    rows = [{"href": f"https://a{i}.test", "title": "t"} for i in range(200)]
    client = FakeHttpClient(*[make_response(content="<p>x</p>") for _ in range(200)])
    trainer = WebTrainer(client=client, backend=FakeBackend(rows=rows))
    result = await trainer.search_and_summarize_result("q")
    assert len(result.value) <= MAX_SEARCH_RESULTS
    assert len(client.calls) <= MAX_SEARCH_RESULTS


async def test_a_f13_hostile_multi_megabyte_html_is_bounded_and_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduced pre-fix: 8.1 MB parsed inline, 0 heartbeat ticks, no cap.

    Structural assertion (parse thread != loop thread) is the load-independent
    one; the tick count is a secondary liveness signal.
    """
    import threading

    import nexus_ai_agent.integrations.external as external

    loop_thread = threading.get_ident()
    parse_threads: list[int] = []
    original = external._extract_sync

    def recording(html_text: str, max_chars: int) -> Any:
        parse_threads.append(threading.get_ident())
        return original(html_text, max_chars)

    monkeypatch.setattr(external, "_extract_sync", recording)

    huge = "<html>" + ("<p>" + "x" * 1000 + "</p>") * 8000 + "</html>"
    assert len(huge) > 8_000_000
    client = FakeHttpClient(make_response(content=huge))
    trainer = WebTrainer(
        client=client, backend=FakeBackend(rows=[{"href": "https://a.test", "title": "t"}])
    )
    result, ticks = await count_heartbeat_ticks(
        trainer.search_and_summarize_result("q"), tick=0.002
    )
    assert len(result.value[0]["content"]) <= MAX_PAGE_CHARS
    assert result.value[0]["truncated"] is True
    assert parse_threads and all(tid != loop_thread for tid in parse_threads), (
        "the parse ran on the event-loop thread"
    )
    assert ticks >= 1, "the event loop made no progress at all during the parse"


async def test_a_a_binary_page_is_a_typed_failure_not_mojibake() -> None:
    client = FakeHttpClient(make_response(content=b"\x89PNG\r\n\x1a\n", content_type="image/png"))
    trainer = WebTrainer(
        client=client, backend=FakeBackend(rows=[{"href": "https://a.test", "title": "t"}])
    )
    result = await trainer.search_and_summarize_result("q")
    assert result.value == []
    assert result.partial_failures[0].kind is ExternalErrorKind.MALFORMED_RESPONSE


async def test_a_a_page_with_no_extractable_text_is_empty_result() -> None:
    client = FakeHttpClient(make_response(content="<html><script>x=1</script></html>"))
    trainer = WebTrainer(
        client=client, backend=FakeBackend(rows=[{"href": "https://a.test", "title": "t"}])
    )
    result = await trainer.search_and_summarize_result("q")
    assert result.partial_failures[0].kind is ExternalErrorKind.EMPTY_RESULT


async def test_r_page_provenance_is_attached_to_every_row() -> None:
    client = FakeHttpClient(make_response(content="<p>hello world</p>"))
    trainer = WebTrainer(
        client=client, backend=FakeBackend(rows=[{"href": "https://a.test", "title": "t"}])
    )
    result = await trainer.search_and_summarize_result("q")
    provenance = result.value[0]["provenance"]
    assert provenance["source_type"] == "web_page"
    assert provenance["url"] == "https://a.test"
    assert "retrieved_at" in provenance


async def test_r_legacy_web_adapter_keeps_the_handler_shape() -> None:
    client = FakeHttpClient(make_response(content="<p>hello</p>"))
    trainer = WebTrainer(
        client=client, backend=FakeBackend(rows=[{"href": "https://a.test", "title": "t"}])
    )
    rows = await trainer.search_and_summarize("q")
    assert rows == [{"title": "t", "url": "https://a.test", "content": "hello"}]


async def test_r_legacy_web_adapter_returns_empty_on_search_failure() -> None:
    trainer = WebTrainer(client=FakeHttpClient(), backend=FakeBackend(error=RuntimeError("x")))
    assert await trainer.search_and_summarize("q") == []


async def test_a_cancelling_a_web_search_propagates_cancellation() -> None:
    class HangingClient:
        calls = 0

        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            await asyncio.sleep(5)
            raise AssertionError("unreachable")

    trainer = WebTrainer(
        client=HangingClient(),  # type: ignore[arg-type]
        backend=FakeBackend(rows=[{"href": "https://a.test", "title": "t"}]),
    )
    task = asyncio.create_task(trainer.search_and_summarize_result("q"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ==========================================================================
# KnowledgeManager — never invent, never poison the cache
# ==========================================================================
@pytest.fixture()
def knowledge_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Give each test its own knowledge-cache database.

    CROSS-ZONE NOTE: ``storage/db.get_session(None)`` hard-codes
    ``"data/app.sqlite"`` and never consults ``settings.db_path`` /
    ``NEXUS_DB_PATH``, so setting the env var is not enough — the path has to be
    injected into ``KnowledgeManager``.  Discovered while writing these tests:
    without the injection they silently shared one repository-local database.
    """
    from nexus_ai_agent.config import settings as settings_module
    from nexus_ai_agent.storage import db as db_module

    db_path = tmp_path / "knowledge.sqlite"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_path))
    settings_module.get_settings.cache_clear()
    db_module._engine = None
    db_module._engine_path = None
    db_module._session_factory = None
    _ACTIVE_DB["path"] = str(db_path)
    yield db_path
    _ACTIVE_DB.pop("path", None)


_ACTIVE_DB: dict[str, str] = {}


class RecordingLLM:
    """Summariser stub.

    ``delay`` matters: a summariser that returns without ever suspending lets
    the first ``learn()`` run to completion before its rivals are scheduled, so
    a stampede test built on it would pass even with the lock removed (verified
    with the MUT-7 mutant).  Concurrency tests must pass a non-zero delay to
    open a real race window.
    """

    def __init__(self, answer: str = "خلاصه‌ای دقیق بر پایهٔ منابع.", *, delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay
        self.prompts: list[str] = []

    async def generate(self, prompt: str, system: str = "") -> str:
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.answer


class DeadWiki:
    def __init__(self, kind: ExternalErrorKind = ExternalErrorKind.NETWORK) -> None:
        self.kind = kind

    async def fetch_summary_result(self, query: str, lang: str = "fa") -> Any:
        raise ExternalSourceError(self.kind, "wikipedia:fa", "source is down")

    async def close(self) -> None:
        return None


class DeadWeb:
    async def search_and_summarize_result(self, query: str) -> Any:
        raise ExternalSourceError(ExternalErrorKind.PROVIDER_ERROR, "duckduckgo:text", "captcha")

    async def close(self) -> None:
        return None


def working_wiki(text: str = "ایران کشوری در غرب آسیا است.") -> WikipediaTrainer:
    return WikipediaTrainer(client=FakeHttpClient(json_response(article(text))))


def working_web(pages: int = 1) -> WebTrainer:
    return WebTrainer(
        client=FakeHttpClient(*[make_response(content=f"<p>page {i}</p>") for i in range(pages)]),
        backend=FakeBackend(
            rows=[{"href": f"https://s{i}.test", "title": f"t{i}"} for i in range(pages)]
        ),
    )


def manager(wiki: Any, web: Any, llm: Any) -> KnowledgeManager:
    return KnowledgeManager(gemini_provider=llm, wiki=wiki, web=web, db_path=_ACTIVE_DB.get("path"))


async def cache_rows(query_key: str | None = None) -> list[Any]:
    from sqlmodel import col, select

    from nexus_ai_agent.storage.db import get_session
    from nexus_ai_agent.storage.models import KnowledgeCache

    async with get_session(_ACTIVE_DB.get("path")) as session:
        statement = select(KnowledgeCache)
        if query_key is not None:
            statement = statement.where(col(KnowledgeCache.query) == query_key)
        return list((await session.execute(statement)).scalars().all())


async def test_r_f25_zero_working_sources_never_produce_a_summary(knowledge_db: Path) -> None:
    """THE invariant: no source, no answer, no cache entry, no LLM call."""
    llm = RecordingLLM()
    km = manager(DeadWiki(), DeadWeb(), llm)
    with pytest.raises(ExternalSourceError) as caught:
        await km.learn("موضوع کاملا ناشناخته")
    assert caught.value.source == "knowledge.learn"
    assert llm.prompts == [], "the summariser must not be called with zero material"
    assert await cache_rows() == [], "nothing may be cached when nothing was retrieved"


async def test_r_f25b_the_failure_kind_mirrors_the_underlying_source_failure(
    knowledge_db: Path,
) -> None:
    km = manager(DeadWiki(ExternalErrorKind.TIMEOUT), DeadWeb(), RecordingLLM())
    with pytest.raises(ExternalSourceError) as caught:
        await km.learn("x")
    assert caught.value.kind is ExternalErrorKind.TIMEOUT
    assert caught.value.retryable is True


async def test_r_f25c_one_surviving_source_is_enough_and_is_marked_degraded(
    knowledge_db: Path,
) -> None:
    llm = RecordingLLM()
    km = manager(working_wiki(), DeadWeb(), llm)
    report = await km.learn_report("ایران")
    assert "خلاصه" in report.summary
    assert report.degraded is True
    assert len(report.failures) == 1
    assert [p.source for p in report.sources] == ["wikipedia:fa"]
    assert "wikipedia" in report.source_label()


async def test_r_the_prompt_contains_only_real_source_material(knowledge_db: Path) -> None:
    """Pre-fix the prompt literally said ``Wikipedia Content: Not found``."""
    llm = RecordingLLM()
    km = manager(working_wiki("ایران کشوری است."), DeadWeb(), llm)
    await km.learn("ایران")
    prompt = llm.prompts[0]
    assert "Not found" not in prompt
    assert "ایران کشوری است." in prompt
    assert "ONLY material" in prompt


async def test_a_the_prompt_is_length_bounded(knowledge_db: Path) -> None:
    from nexus_ai_agent.knowledge.knowledge_manager import MAX_PROMPT_SOURCE_CHARS

    llm = RecordingLLM()
    wiki = WikipediaTrainer(client=FakeHttpClient(json_response(article("ا" * 900_000))))
    km = manager(wiki, working_web(3), llm)
    await km.learn("ایران")
    assert len(llm.prompts[0]) < MAX_PROMPT_SOURCE_CHARS + 2000


async def test_r_f33_an_empty_model_answer_is_a_failure_and_is_not_cached(
    knowledge_db: Path,
) -> None:
    km = manager(working_wiki(), DeadWeb(), RecordingLLM(answer="   "))
    with pytest.raises(ExternalSourceError) as caught:
        await km.learn("ایران")
    assert caught.value.kind is ExternalErrorKind.EMPTY_RESULT
    assert await cache_rows() == []


async def test_a_a_summariser_exception_is_typed_not_leaked(knowledge_db: Path) -> None:
    class ExplodingLLM:
        async def generate(self, prompt: str, system: str = "") -> str:
            raise RuntimeError("quota exceeded")

    km = manager(working_wiki(), DeadWeb(), ExplodingLLM())
    with pytest.raises(ExternalSourceError) as caught:
        await km.learn("ایران")
    assert caught.value.kind is ExternalErrorKind.PROVIDER_ERROR
    assert caught.value.source == "llm:summarizer"
    assert await cache_rows() == []


# --- cache correctness -----------------------------------------------------
async def test_r_f26_equivalent_spellings_share_one_cache_entry(knowledge_db: Path) -> None:
    """Pre-fix: 4 spellings => 4 rows and 4 paid LLM calls."""
    llm = RecordingLLM()
    for spelling in ["هوش مصنوعی", " هوش مصنوعی ", "هوش  مصنوعی", "هوش مصنوعی\n"]:
        km = manager(working_wiki(), DeadWeb(), llm)
        await km.learn(spelling)
    assert len(llm.prompts) == 1, "only the first spelling may reach the summariser"
    assert len(await cache_rows()) == 1


class GatedLLM:
    """Summariser that parks inside ``generate`` until the test releases it.

    This makes the stampede window deterministic instead of scheduling-
    dependent: whoever wins the lock is held hostage while every rival caller
    gets unlimited loop time to reach the summariser.  With the per-key lock
    only one prompt can exist; without it (mutant MUT-7) all five arrive.
    """

    def __init__(self, answer: str = "answer") -> None:
        self.answer = answer
        self.prompts: list[str] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, prompt: str, system: str = "") -> str:
        self.prompts.append(prompt)
        self.entered.set()
        await self.release.wait()
        return self.answer


async def test_r_f28_concurrent_identical_learns_collapse_into_one(knowledge_db: Path) -> None:
    """Pre-fix: 5 concurrent learns => 5 LLM calls and 5 duplicate rows."""
    llm = GatedLLM()

    def fresh() -> KnowledgeManager:
        return manager(working_wiki(), DeadWeb(), llm)

    tasks = [
        asyncio.create_task(fresh().learn("stampede", include_sources=False)) for _ in range(5)
    ]
    await asyncio.wait_for(llm.entered.wait(), timeout=5)
    # The winner is parked; the loop is otherwise idle, so every rival has had
    # ample opportunity to reach the summariser by now.
    for _ in range(50):
        await asyncio.sleep(0.005)
    assert len(llm.prompts) == 1, f"cache stampede: {len(llm.prompts)} summariser calls"

    llm.release.set()
    results = await asyncio.gather(*tasks)
    assert len(set(results)) == 1, "all callers must observe the same answer body"
    assert len(await cache_rows()) == 1

    # The rendered footer legitimately differs: the race winner reports the live
    # attempt (including the dead web source), the queued callers report a cache
    # read.  Both are true; what must never differ is the answer itself.
    reports = await asyncio.gather(*[fresh().learn_report("stampede") for _ in range(3)])
    assert {r.summary for r in reports} == set(results)
    assert all(r.from_cache for r in reports)


async def test_r_f29_duplicate_cache_rows_no_longer_detonate_the_reader(
    knowledge_db: Path,
) -> None:
    """The exact pre-fix outage: ``scalar_one_or_none`` -> MultipleResultsFound.

    Duplicates are seeded directly, so the reader is proven robust even against
    rows written by an older build of this code.
    """
    from datetime import timedelta

    from nexus_ai_agent.integrations.external import cache_key_for, naive_utcnow
    from nexus_ai_agent.storage.db import get_session
    from nexus_ai_agent.storage.models import KnowledgeCache

    key = cache_key_for("stampede")
    async with get_session(_ACTIVE_DB.get("path")) as session:
        for index in range(5):
            session.add(
                KnowledgeCache(
                    query=key,
                    source="combined",
                    content=f"answer-{index}",
                    expires_at=naive_utcnow() + timedelta(hours=index + 1),
                )
            )
        await session.commit()

    km = manager(DeadWiki(), DeadWeb(), RecordingLLM())
    assert await km.get_cached_knowledge("stampede") == "answer-4", "newest live row wins"
    assert await km.learn("STAMPEDE  ", include_sources=False) == "answer-4", (
        "and the read path is reachable again"
    )


async def test_r_a_write_replaces_the_keys_rows_instead_of_appending(
    knowledge_db: Path,
) -> None:
    llm = RecordingLLM()
    await manager(working_wiki(), DeadWeb(), llm).learn("ایران")
    rows_first = await cache_rows()
    assert len(rows_first) == 1

    # expire it, then learn again: still exactly one row for the key
    from sqlmodel import col, update

    from nexus_ai_agent.integrations.external import naive_utcnow
    from nexus_ai_agent.storage.db import get_session
    from nexus_ai_agent.storage.models import KnowledgeCache

    async with get_session(_ACTIVE_DB.get("path")) as session:
        await session.execute(
            update(KnowledgeCache)
            .where(col(KnowledgeCache.query) == rows_first[0].query)
            .values(expires_at=naive_utcnow())
        )
        await session.commit()

    await manager(working_wiki(), DeadWeb(), RecordingLLM("پاسخ تازه")).learn("ایران")
    assert len(await cache_rows()) == 1


async def test_r_the_cached_source_label_is_honest(knowledge_db: Path) -> None:
    """Pre-fix every row said ``source="combined"`` even with zero sources."""
    await manager(working_wiki(), DeadWeb(), RecordingLLM()).learn("ایران")
    rows = await cache_rows()
    assert rows[0].source == "wikipedia:fa"
    assert rows[0].source != "combined"


async def test_r_a_cache_hit_skips_every_source_and_the_llm(knowledge_db: Path) -> None:
    llm = RecordingLLM()
    await manager(working_wiki(), DeadWeb(), llm).learn("ایران")

    second = manager(DeadWiki(), DeadWeb(), llm)
    report = await second.learn_report("ایران")
    assert report.from_cache is True
    assert len(llm.prompts) == 1


@pytest.mark.parametrize("bad", ["", "    ", "\u200e\u200f", "ا" * 5000])
async def test_a_hostile_queries_are_rejected_before_any_source_is_touched(
    knowledge_db: Path, bad: str
) -> None:
    llm = RecordingLLM()
    km = manager(DeadWiki(), DeadWeb(), llm)
    with pytest.raises(ExternalSourceError) as caught:
        await km.learn(bad)
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT
    assert llm.prompts == []
    assert await cache_rows() == []


async def test_r_the_answer_carries_a_provenance_footer(knowledge_db: Path) -> None:
    km = manager(working_wiki(), DeadWeb(), RecordingLLM())
    summary = await km.learn("ایران")
    assert "— منابع —" in summary
    assert "wikipedia:fa" in summary
    assert "منابع ناموفق" in summary, "a dead source must be visible to the reader"


def test_r_footer_marks_truncation_and_fallback() -> None:
    from nexus_ai_agent.integrations.external import Provenance, SourceType

    report = KnowledgeReport(
        summary="x",
        sources=(
            Provenance.create(
                "wikipedia:en", SourceType.ENCYCLOPEDIA, truncated=True, degraded=True
            ),
        ),
        failures=(ExternalSourceError(ExternalErrorKind.TIMEOUT, "duckduckgo:text"),),
    )
    footer = format_provenance_footer(report)
    assert "کوتاه‌شده" in footer
    assert "جایگزین" in footer
    assert "timeout" in footer


async def test_r_learn_result_exposes_the_typed_envelope(knowledge_db: Path) -> None:
    km = manager(working_wiki(), DeadWeb(), RecordingLLM())
    result = await km.learn_result("ایران")
    assert result.degraded is True
    assert result.provenance.source == "wikipedia:fa"
    assert [f.kind for f in result.partial_failures] == [ExternalErrorKind.PROVIDER_ERROR]


async def test_a_sources_are_fetched_concurrently_not_serially(knowledge_db: Path) -> None:
    """A dead Wikipedia must not serialise in front of a healthy web search."""
    order: list[str] = []

    class SlowWiki:
        async def fetch_summary_result(self, query: str, lang: str = "fa") -> Any:
            await asyncio.sleep(0.15)
            order.append("wiki")
            raise ExternalSourceError(ExternalErrorKind.TIMEOUT, "wikipedia:fa", "slow")

        async def close(self) -> None:
            return None

    class FastWeb:
        async def search_and_summarize_result(self, query: str) -> Any:
            order.append("web")
            return await working_web(1).search_and_summarize_result(query)

        async def close(self) -> None:
            return None

    km = manager(SlowWiki(), FastWeb(), RecordingLLM())
    await km.learn("ایران")
    assert order == ["web", "wiki"], "the fast source must not wait for the slow one"


# --- M: mutation probes for the KnowledgeManager invariants ----------------
async def test_m_f25_guard_is_red_when_the_zero_source_check_is_removed(
    knowledge_db: Path,
) -> None:
    """Restore the pre-fix behaviour and the F25 assertion must break."""
    llm = RecordingLLM()

    class FabricatingManager(KnowledgeManager):
        async def _learn_uncached(self, normalized: str, key: str) -> Any:
            # the pre-fix code path: summarise "Not found" and cache it
            summary = await self._summarize(normalized, [("Wikipedia", "Not found")])
            await self._store(key, summary, "combined")
            return KnowledgeReport(summary=summary, sources=(), cache_key=key)

    km = FabricatingManager(
        gemini_provider=llm, wiki=DeadWiki(), web=DeadWeb(), db_path=_ACTIVE_DB.get("path")
    )
    answer = await km.learn("موضوع ناشناخته")
    assert answer, "the mutated manager fabricates an answer (this is the defect)"
    with pytest.raises(AssertionError):
        assert llm.prompts == [], "guard must fail when the zero-source check is removed"
    with pytest.raises(AssertionError):
        assert await cache_rows() == [], "guard must fail when the invention is cached"


async def test_m_f29_guard_is_red_against_scalar_one_or_none(knowledge_db: Path) -> None:
    """Re-introduce ``scalar_one_or_none()`` on duplicate rows: it must detonate."""
    from datetime import timedelta

    from sqlalchemy.exc import MultipleResultsFound
    from sqlmodel import col, select

    from nexus_ai_agent.integrations.external import cache_key_for, naive_utcnow
    from nexus_ai_agent.storage.db import get_session
    from nexus_ai_agent.storage.models import KnowledgeCache

    key = cache_key_for("stampede")
    async with get_session(_ACTIVE_DB.get("path")) as session:
        for index in range(3):
            session.add(
                KnowledgeCache(
                    query=key,
                    source="combined",
                    content=f"a{index}",
                    expires_at=naive_utcnow() + timedelta(hours=1),
                )
            )
        await session.commit()

    async with get_session(_ACTIVE_DB.get("path")) as session:
        statement = select(KnowledgeCache).where(col(KnowledgeCache.query) == key)
        with pytest.raises(MultipleResultsFound):
            (await session.execute(statement)).scalar_one_or_none()

    # the shipped reader survives the identical state
    km = manager(DeadWiki(), DeadWeb(), RecordingLLM())
    assert await km.get_cached_knowledge("stampede") is not None


async def test_m_f26_guard_is_red_when_the_cache_key_is_not_normalised() -> None:
    from nexus_ai_agent.integrations.external import cache_key_for

    raw_keys = {" هوش مصنوعی ", "هوش مصنوعی"}
    assert len(raw_keys) == 2, "the raw spellings really are different strings"
    with pytest.raises(AssertionError):
        assert len(raw_keys) == 1
    assert len({cache_key_for(k) for k in raw_keys}) == 1


# ==========================================================================
# Out-of-zone caller compatibility
# ==========================================================================
def test_r_legacy_public_surface_for_bot_knowledge_handlers() -> None:
    import inspect

    assert inspect.iscoroutinefunction(KnowledgeManager.learn)
    assert inspect.iscoroutinefunction(KnowledgeManager.close)
    assert inspect.iscoroutinefunction(WikipediaTrainer.fetch_summary)
    assert inspect.iscoroutinefunction(WebTrainer.search_and_summarize)
    # handlers construct KnowledgeManager() with no arguments
    params = inspect.signature(KnowledgeManager.__init__).parameters
    assert all(p.default is not inspect.Parameter.empty for n, p in params.items() if n != "self")


async def test_r_cache_stores_the_bare_summary_not_the_rendered_footer(
    knowledge_db: Path,
) -> None:
    """Storage is canonical; provenance is presentation.

    Before this split the footer was baked into the cached row, so the value a
    caller got back depended on which caller happened to warm the cache
    (``include_sources=False`` stored a footer-less body that a later
    ``include_sources=True`` caller then received without any provenance).
    """
    km = manager(working_wiki(), DeadWeb(), RecordingLLM())
    rendered = await km.learn("ایران")
    assert "— منابع —" in rendered

    rows = await cache_rows()
    assert len(rows) == 1
    assert "— منابع —" not in rows[0].content, "presentation must not be persisted"
    assert rows[0].content == rendered.split("\n— منابع —")[0]
    assert rows[0].source == "wikipedia:fa"


async def test_r_cache_hit_shape_is_independent_of_the_warming_caller(
    knowledge_db: Path,
) -> None:
    km = manager(working_wiki(), DeadWeb(), RecordingLLM())
    bare = await km.learn("ایران", include_sources=False)
    assert "— منابع —" not in bare

    second = manager(DeadWiki(), DeadWeb(), RecordingLLM())
    rendered = await second.learn("ایران")
    assert rendered.startswith(bare), "the cached body must be reused verbatim"
    assert "— منابع —" in rendered, "a cache hit still tells the reader where it came from"
    assert "wikipedia:fa" in rendered, "the original source label survives the cache round-trip"
    assert len(await cache_rows()) == 1


async def test_r_cached_report_marks_provenance_as_cache_typed(knowledge_db: Path) -> None:
    from nexus_ai_agent.integrations.external import SourceType

    km = manager(working_wiki(), DeadWeb(), RecordingLLM())
    await km.learn("ایران")
    report = await km.learn_report("ایران")
    assert report.from_cache is True
    assert [p.source_type for p in report.sources] == [SourceType.CACHE]
    assert [p.source for p in report.sources] == ["wikipedia:fa"]
    assert report.notes == ("cache_source=wikipedia:fa",)
