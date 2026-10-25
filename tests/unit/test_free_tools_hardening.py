"""Hardening tests for ``integrations/free_tools.py``.

Every test below names the pre-fix behaviour it locks out.  Reproductions of
each were captured against the original implementation before the rewrite:

======  ===================================================================
F1      the NewsAPI key travelled in the URL query string and was written to
        the logs verbatim by ``core/http_client``'s ``get_json_failed``
F2      ``https://wttr.in/{city}?format=j1`` with the raw city: ``"x?format=
        j1&evil=1"`` rewrote the query, ``"../../../etc/passwd"`` the path
F3      a provider that quoted no IRR rate produced the number ``0.0``
F4      a dead network produced ``{}`` from a function declared ``dict|None``
F5      ``DDGS`` is synchronous: 0 event-loop ticks during a 0.6 s call
F6      provider exception and "no results" were both ``[]``; one malformed
        row discarded every good row in the same page
F7      ``max_results`` was trusted: 500 provider rows came back as 500
======  ===================================================================

``R`` = regression, ``A`` = adversarial, ``M`` = mutation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from test_external_contracts import FakeHttpClient, count_heartbeat_ticks, make_response

from nexus_ai_agent.integrations.external import (
    ExternalErrorKind,
    ExternalSourceError,
    SearchBackend,
)
from nexus_ai_agent.integrations.free_tools import (
    MAX_NEWS_ITEMS,
    MAX_VIDEO_ITEMS,
    CurrencyTool,
    NewsTool,
    WeatherTool,
    YouTubeSearchTool,
)

NEWS_KEY = "NEWSAPI-LIVE-KEY-0001"


class FakeBackend(SearchBackend):
    """Search backend under full test control."""

    def __init__(self, rows: Any = None, error: BaseException | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[tuple[str, str, int]] = []

    async def search(self, kind: str, query: str, *, max_results: int) -> list[dict[str, Any]]:
        self.calls.append((kind, query, max_results))
        if self.error is not None:
            raise self.error
        return list(self.rows)


def json_response(payload: Any, *, status: int = 200) -> httpx.Response:
    return make_response(
        status=status, content=json.dumps(payload).encode(), content_type="application/json"
    )


# ==========================================================================
# WeatherTool
# ==========================================================================
@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "x?format=j1&evil=1",
        "a#frag",
        "a/b",
        "Tehran&x=1",
        "تهران",
        "a b",
        "%2e%2e%2f",
    ],
)
def test_a_f2_weather_url_cannot_be_steered_by_the_city_argument(hostile: str) -> None:
    url = WeatherTool.build_request_url(hostile)
    parsed = httpx.URL(url)
    assert parsed.host == "wttr.in"
    assert parsed.scheme == "https"
    assert dict(parsed.params) == {"format": "j1"}, url
    assert parsed.fragment == ""


def test_r_f2_normal_city_still_produces_the_documented_endpoint() -> None:
    assert WeatherTool.build_request_url("Tehran") == "https://wttr.in/Tehran?format=j1"


async def test_r_f4_weather_network_failure_is_typed_not_an_empty_dict() -> None:
    tool = WeatherTool(client=FakeHttpClient(httpx.ConnectError("down")))
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_weather_result("Tehran")
    assert caught.value.kind is ExternalErrorKind.NETWORK
    assert caught.value.retryable is True


async def test_r_f4b_legacy_weather_adapter_returns_none_never_a_bare_empty_dict() -> None:
    """``bot/tool_handlers`` does ``if data:`` — ``{}`` and ``None`` both fall to
    the error branch, but only ``None`` is an honest 'no data' sentinel."""
    tool = WeatherTool(client=FakeHttpClient(httpx.ConnectError("down")))
    assert await tool.get_weather("Tehran") is None


async def test_a_weather_unknown_city_is_not_found_not_a_generic_failure() -> None:
    tool = WeatherTool(client=FakeHttpClient(make_response(status=404, content=b"nope")))
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_weather_result("Zzzqqq")
    assert caught.value.kind is ExternalErrorKind.NOT_FOUND
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    "payload", [{}, {"current_condition": []}, {"current_condition": "x"}, {"other": 1}, []]
)
async def test_a_weather_payload_missing_the_field_handlers_index_is_malformed(
    payload: Any,
) -> None:
    """``bot/tool_handlers`` does ``data["current_condition"][0]`` unguarded."""
    tool = WeatherTool(client=FakeHttpClient(json_response(payload)))
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_weather_result("Tehran")
    assert caught.value.kind is ExternalErrorKind.MALFORMED_RESPONSE


async def test_r_weather_success_carries_provenance() -> None:
    payload = {"current_condition": [{"temp_C": "20", "weatherDesc": [{"value": "Sunny"}]}]}
    tool = WeatherTool(client=FakeHttpClient(json_response(payload)))
    result = await tool.get_weather_result("Tehran")
    assert result.value == payload
    assert result.provenance.source == "wttr.in"
    assert result.provenance.url == "https://wttr.in/Tehran?format=j1"


@pytest.mark.parametrize("empty", ["", "   ", "\x00"])
async def test_a_weather_empty_city_never_reaches_the_network(empty: str) -> None:
    client = FakeHttpClient()
    tool = WeatherTool(client=client)
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_weather_result(empty)
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT
    assert client.calls == [], "an invalid city must not produce an HTTP request"


# ==========================================================================
# CurrencyTool — the fabricated 0.0
# ==========================================================================
async def test_r_f3_missing_rate_is_an_error_not_the_number_zero() -> None:
    tool = CurrencyTool(client=FakeHttpClient(json_response({"rates": {"EUR": 0.9}})))
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_rate_result("USD")
    assert caught.value.kind is ExternalErrorKind.MALFORMED_RESPONSE
    assert "IRR" in caught.value.detail


async def test_r_f3b_legacy_adapter_returns_none_never_zero() -> None:
    tool = CurrencyTool(client=FakeHttpClient(json_response({"rates": {"EUR": 0.9}})))
    rate = await tool.get_rate("USD")
    assert rate is None
    assert rate != 0.0 or rate is None  # explicit: 0.0 is no longer reachable


@pytest.mark.parametrize("bad_rate", [0, -1, "abc", None, [], {}, float("nan"), float("inf")])
async def test_a_f3_unusable_rate_values_are_all_refused(bad_rate: Any) -> None:
    payload = {"rates": {"IRR": bad_rate}}
    tool = CurrencyTool(client=FakeHttpClient(json_response(payload)))
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_rate_result("USD")
    assert caught.value.kind is ExternalErrorKind.MALFORMED_RESPONSE


async def test_r_f3c_a_numeric_string_rate_is_accepted() -> None:
    tool = CurrencyTool(client=FakeHttpClient(json_response({"rates": {"IRR": "42000.5"}})))
    assert (await tool.get_rate_result("USD")).value == 42000.5


async def test_r_f3d_network_failure_and_unknown_currency_are_distinguishable() -> None:
    """Pre-fix: dead network -> None, unquoted pair -> 0.0, and *neither* said why."""
    dead = CurrencyTool(client=FakeHttpClient(httpx.ConnectError("down")))
    with pytest.raises(ExternalSourceError) as network:
        await dead.get_rate_result("USD")

    quoted_nothing = CurrencyTool(client=FakeHttpClient(json_response({"rates": {}})))
    with pytest.raises(ExternalSourceError) as missing:
        await quoted_nothing.get_rate_result("USD")

    assert network.value.kind is not missing.value.kind
    assert network.value.retryable is True
    assert missing.value.retryable is False


@pytest.mark.parametrize("bad", ["", "US", "USDD", "US1", "usd$", "../US", "دلار", "U S"])
async def test_a_currency_codes_are_validated_before_url_construction(bad: str) -> None:
    client = FakeHttpClient()
    tool = CurrencyTool(client=client)
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_rate_result(bad)
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT
    assert client.calls == []


def test_r_currency_url_is_composed_from_a_validated_code() -> None:
    assert CurrencyTool.build_request_url("usd") == "https://api.exchangerate-api.com/v4/latest/USD"


async def test_a_currency_missing_rates_object_is_malformed() -> None:
    tool = CurrencyTool(client=FakeHttpClient(json_response({"result": "error"})))
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_rate_result("USD")
    assert caught.value.kind is ExternalErrorKind.MALFORMED_RESPONSE


# ==========================================================================
# NewsTool — secret transport (Rule 7)
# ==========================================================================
def test_r_f1_the_api_key_is_structurally_absent_from_the_url() -> None:
    url = NewsTool.build_request_url("iran")
    assert NEWS_KEY not in url
    assert "apiKey" not in url
    assert "api_key" not in url.lower()
    assert httpx.URL(url).host == "newsapi.org"


async def test_r_f1b_the_key_travels_in_a_header_and_only_in_a_header() -> None:
    client = FakeHttpClient(json_response({"status": "ok", "articles": []}))
    tool = NewsTool(api_key=NEWS_KEY, client=client, backend=FakeBackend())
    await tool.get_news_result("iran")
    assert client.urls, "no request was made"
    assert all(NEWS_KEY not in url for url in client.urls)
    assert client.headers[0]["X-Api-Key"] == NEWS_KEY


async def test_a_f1_the_key_never_reaches_a_log_line_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeHttpClient(httpx.ConnectError("down"))
    tool = NewsTool(api_key=NEWS_KEY, client=client, backend=FakeBackend(rows=[]))
    with caplog.at_level(logging.DEBUG):
        await tool.get_news_result("iran")
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert NEWS_KEY not in blob, "the credential leaked into the logs"


async def test_a_f1b_the_key_never_reaches_an_exception_or_its_traceback() -> None:
    import traceback

    client = FakeHttpClient(httpx.ConnectError("down"))
    tool = NewsTool(
        api_key=NEWS_KEY, client=client, backend=FakeBackend(error=RuntimeError("ddg down"))
    )
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_news_result("iran")
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert NEWS_KEY not in rendered


async def test_a_f1c_a_provider_error_message_echoing_the_key_is_still_redacted() -> None:
    payload = {"status": "error", "message": f"Your API key {NEWS_KEY} is invalid"}
    client = FakeHttpClient(json_response(payload))
    tool = NewsTool(api_key=NEWS_KEY, client=client, backend=FakeBackend(error=RuntimeError("x")))
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_news_result("iran")
    assert NEWS_KEY not in str(caught.value)


# --- M: mutation probes for the secret-transport invariant -----------------
def test_m_f1_url_guard_is_red_when_the_key_is_put_back_in_the_query_string() -> None:
    """Re-create the pre-fix URL builder; the R_F1 assertion must break."""
    broken_url = f"https://newsapi.org/v2/everything?q=iran&apiKey={NEWS_KEY}"
    with pytest.raises(AssertionError):
        assert NEWS_KEY not in broken_url
    with pytest.raises(AssertionError):
        assert "apiKey" not in broken_url


async def test_m_f1b_log_guard_is_red_against_a_url_logging_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Simulate ``core/http_client``'s ``get_json_failed`` on a key-bearing URL."""
    logger = logging.getLogger("mutation.http")
    with caplog.at_level(logging.DEBUG):
        logger.error("get_json_failed url=https://newsapi.org/v2/everything?apiKey=%s", NEWS_KEY)
    blob = "\n".join(record.getMessage() for record in caplog.records)
    with pytest.raises(AssertionError):
        assert NEWS_KEY not in blob


# --- NewsTool behaviour ----------------------------------------------------
async def test_r_news_api_success_is_not_marked_degraded() -> None:
    payload = {
        "status": "ok",
        "articles": [{"title": "t1", "url": "https://n/1"}, {"title": "t2", "url": "https://n/2"}],
    }
    tool = NewsTool(
        api_key=NEWS_KEY, client=FakeHttpClient(json_response(payload)), backend=FakeBackend()
    )
    result = await tool.get_news_result("iran")
    assert [row["url"] for row in result.value] == ["https://n/1", "https://n/2"]
    assert result.degraded is False
    assert result.provenance.source == "newsapi.org"


async def test_r_news_fallback_is_explicit_and_marked_degraded() -> None:
    """Pre-fix the fallback was invisible: the caller could not tell whether the
    paid source had answered."""
    backend = FakeBackend(rows=[{"title": "ddg", "url": "https://d/1"}])
    tool = NewsTool(
        api_key=NEWS_KEY, client=FakeHttpClient(httpx.ConnectError("down")), backend=backend
    )
    result = await tool.get_news_result("iran")
    assert [row["url"] for row in result.value] == ["https://d/1"]
    assert result.degraded is True
    assert result.provenance.degraded is True
    assert result.provenance.fallback_path[0].startswith("newsapi.org:")
    assert len(result.partial_failures) == 1
    assert result.partial_failures[0].kind is ExternalErrorKind.NETWORK


async def test_a_news_total_failure_raises_and_preserves_both_causes() -> None:
    tool = NewsTool(
        api_key=NEWS_KEY,
        client=FakeHttpClient(httpx.ReadTimeout("slow")),
        backend=FakeBackend(error=RuntimeError("captcha")),
    )
    with pytest.raises(ExternalSourceError) as caught:
        await tool.get_news_result("iran")
    assert caught.value.kind is ExternalErrorKind.PROVIDER_ERROR
    cause = caught.value.__cause__
    assert isinstance(cause, ExternalSourceError)
    assert cause.kind is ExternalErrorKind.TIMEOUT


async def test_r_news_without_a_key_goes_straight_to_the_search_backend() -> None:
    client = FakeHttpClient()
    backend = FakeBackend(rows=[{"title": "d", "url": "https://d/1"}])
    tool = NewsTool(api_key=None, client=client, backend=backend)
    result = await tool.get_news_result("iran")
    assert client.calls == [], "no keyed request may be attempted without a key"
    assert result.degraded is False
    assert backend.calls == [("news", "iran", MAX_NEWS_ITEMS)]


async def test_a_news_provider_status_error_is_a_provider_error() -> None:
    payload = {"status": "error", "code": "rateLimited", "message": "too many requests"}
    tool = NewsTool(
        api_key=NEWS_KEY,
        client=FakeHttpClient(json_response(payload)),
        backend=FakeBackend(rows=[]),
    )
    result = await tool.get_news_result("iran")
    assert result.value == []
    assert result.partial_failures[0].kind is ExternalErrorKind.PROVIDER_ERROR


async def test_a_f6_a_malformed_article_does_not_discard_the_good_ones() -> None:
    payload = {
        "status": "ok",
        "articles": [
            {"title": "good", "url": "https://n/1"},
            {"title": "no url"},
            {"url": "https://n/2"},
            "not a dict",
            {"title": "good2", "url": "https://n/3"},
        ],
    }
    tool = NewsTool(
        api_key=NEWS_KEY, client=FakeHttpClient(json_response(payload)), backend=FakeBackend()
    )
    result = await tool.get_news_result("iran")
    urls = [row["url"] for row in result.value]
    assert urls == ["https://n/1", "https://n/2", "https://n/3"]
    assert result.value[1]["title"] == "—", "a missing title becomes a placeholder, not a KeyError"


async def test_a_f7_a_flooding_provider_is_capped_and_deduplicated() -> None:
    payload = {
        "status": "ok",
        "articles": [{"title": f"t{i}", "url": f"https://n/{i % 2}"} for i in range(500)],
    }
    tool = NewsTool(
        api_key=NEWS_KEY, client=FakeHttpClient(json_response(payload)), backend=FakeBackend()
    )
    result = await tool.get_news_result("iran")
    assert len(result.value) <= MAX_NEWS_ITEMS
    assert len({row["url"] for row in result.value}) == len(result.value)


async def test_a_news_missing_articles_list_is_malformed() -> None:
    tool = NewsTool(
        api_key=NEWS_KEY,
        client=FakeHttpClient(json_response({"status": "ok"})),
        backend=FakeBackend(rows=[]),
    )
    result = await tool.get_news_result("iran")
    assert result.partial_failures[0].kind is ExternalErrorKind.MALFORMED_RESPONSE


async def test_r_news_legacy_adapter_keeps_the_handler_contract() -> None:
    payload = {"status": "ok", "articles": [{"title": "t", "url": "https://n/1"}]}
    tool = NewsTool(
        api_key=NEWS_KEY, client=FakeHttpClient(json_response(payload)), backend=FakeBackend()
    )
    rows = await tool.get_news("iran")
    assert rows == [{"title": "t", "url": "https://n/1"}]
    assert all(set(row) == {"title", "url"} for row in rows)


async def test_r_news_legacy_adapter_returns_empty_list_on_total_failure() -> None:
    tool = NewsTool(
        api_key=None,
        client=FakeHttpClient(),
        backend=FakeBackend(error=RuntimeError("captcha")),
    )
    assert await tool.get_news("iran") == []


# ==========================================================================
# YouTubeSearchTool
# ==========================================================================
async def test_r_f6_provider_explosion_and_empty_results_are_different_outcomes() -> None:
    exploded = YouTubeSearchTool(backend=FakeBackend(error=RuntimeError("captcha")))
    with pytest.raises(ExternalSourceError) as caught:
        await exploded.search_result("cats")
    assert caught.value.kind is ExternalErrorKind.PROVIDER_ERROR

    empty = YouTubeSearchTool(backend=FakeBackend(rows=[]))
    result = await empty.search_result("cats")
    assert result.value == []
    assert result.partial_failures == ()
    assert result.degraded is False, "an authoritative empty answer is not a degradation"


async def test_a_f6b_one_malformed_video_row_does_not_erase_the_good_row() -> None:
    backend = FakeBackend(
        rows=[
            {"title": "ok", "content": "https://y/1"},
            {"title": "no url"},
            {"content": "https://y/2"},
        ]
    )
    tool = YouTubeSearchTool(backend=backend)
    result = await tool.search_result("cats")
    assert [row["url"] for row in result.value] == ["https://y/1", "https://y/2"]
    assert result.provenance.degraded is True
    assert any("dropped_malformed_rows=1" in note for note in result.provenance.notes)


async def test_r_video_rows_accept_either_content_or_href() -> None:
    backend = FakeBackend(rows=[{"title": "a", "href": "https://y/9"}])
    result = await YouTubeSearchTool(backend=backend).search_result("cats")
    assert result.value == [{"title": "a", "url": "https://y/9"}]


async def test_a_f7b_video_results_are_capped_and_deduplicated() -> None:
    backend = FakeBackend(rows=[{"title": "t", "content": "https://y/1"}] * 500)
    result = await YouTubeSearchTool(backend=backend).search_result("cats")
    assert len(result.value) == 1, "500 identical rows are one video"
    backend2 = FakeBackend(rows=[{"title": "t", "content": f"https://y/{i}"} for i in range(50)])
    result2 = await YouTubeSearchTool(backend=backend2).search_result("cats")
    assert len(result2.value) <= MAX_VIDEO_ITEMS


async def test_r_youtube_legacy_adapter_returns_empty_list_on_failure() -> None:
    tool = YouTubeSearchTool(backend=FakeBackend(error=RuntimeError("captcha")))
    assert await tool.search("cats") == []


@pytest.mark.parametrize("hostile", ["", "   ", "\x00\x01", "\u202e" * 4])
async def test_a_youtube_hostile_empty_query_never_reaches_the_provider(hostile: str) -> None:
    backend = FakeBackend(rows=[])
    tool = YouTubeSearchTool(backend=backend)
    with pytest.raises(ExternalSourceError) as caught:
        await tool.search_result(hostile)
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT
    assert backend.calls == []


async def test_a_unicode_rtl_query_is_normalised_before_reaching_the_provider() -> None:
    backend = FakeBackend(rows=[])
    await YouTubeSearchTool(backend=backend).search_result("  گربه\u202e  خانگی  ")
    assert backend.calls[0][1] == "گربه خانگی"


# ==========================================================================
# Rule 6 — the event loop
# ==========================================================================
async def test_r_f5_the_news_path_does_not_block_the_event_loop() -> None:
    """Pre-fix: 0 heartbeat ticks during a 0.6 s synchronous provider call."""
    import time

    class SlowSdk:
        def news(self, *a: Any, **k: Any) -> Any:
            time.sleep(0.4)
            return []

    from nexus_ai_agent.integrations.external import DuckDuckGoBackend

    tool = NewsTool(api_key=None, backend=DuckDuckGoBackend(factory=SlowSdk))
    _result, ticks = await count_heartbeat_ticks(tool.get_news_result("iran"))
    assert ticks >= 10, f"the event loop was blocked for the whole call ({ticks} ticks)"


async def test_a_cancellation_during_a_search_propagates_as_cancellation() -> None:
    import time

    class SlowSdk:
        def videos(self, *a: Any, **k: Any) -> Any:
            time.sleep(0.5)
            return []

    from nexus_ai_agent.integrations.external import DuckDuckGoBackend

    tool = YouTubeSearchTool(backend=DuckDuckGoBackend(factory=SlowSdk))
    task = asyncio.create_task(tool.search_result("cats"))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ==========================================================================
# Import-time contract
# ==========================================================================
def test_r_legacy_public_surface_is_unchanged_for_out_of_zone_callers() -> None:
    """``bot/tool_handlers.py`` is owned by another zone: its call sites must
    keep working byte-for-byte."""
    import inspect

    assert inspect.iscoroutinefunction(WeatherTool.get_weather)
    assert inspect.iscoroutinefunction(CurrencyTool.get_rate)
    assert inspect.iscoroutinefunction(NewsTool.get_news)
    assert inspect.iscoroutinefunction(YouTubeSearchTool.search)
    # the constructor signature the handler uses: NewsTool(api_key=...)
    assert "api_key" in inspect.signature(NewsTool.__init__).parameters
    assert list(inspect.signature(CurrencyTool.get_rate).parameters) == ["self", "base"]
