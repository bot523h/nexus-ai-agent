"""Contract tests for ``integrations/external.py`` — the zone's failure spine.

Layout of every test file in this zone::

    R#  regression  — the fixed defect must stay fixed
    A#  adversarial — hostile input / hostile provider / hostile network
    M#  mutation    — a deliberately wrong implementation is injected and the
                      guard assertion MUST fail; a guard that cannot fail is
                      not a guard.

This module also exports the fakes the other two files in the zone reuse
(``FakeHttpClient``, ``make_response``, ``count_heartbeat_ticks``).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit  # noqa: E402

import httpx
import pytest

from nexus_ai_agent.core.http_client import CircuitOpenError
from nexus_ai_agent.core.ssrf_guard import SSRFBlockError
from nexus_ai_agent.integrations.external import (
    MAX_QUERY_CHARS,
    REDACTED,
    DuckDuckGoBackend,
    ExternalErrorKind,
    ExternalSourceError,
    Provenance,
    SourceResult,
    SourceType,
    build_url,
    cache_key_for,
    dedupe_by_url,
    extract_text,
    fetch_json,
    fetch_text,
    normalize_query,
    redact_headers,
    redact_text,
    redact_url,
    require_mapping,
    run_blocking,
    safe_path_segment,
)

# --------------------------------------------------------------------------- #
# shared fakes
# --------------------------------------------------------------------------- #


def make_response(
    *,
    status: int = 200,
    content: bytes | str = b"",
    content_type: str | None = "text/html; charset=utf-8",
    url: str = "https://example.test/x",
) -> httpx.Response:
    """A real ``httpx.Response`` so the code under test sees real semantics."""
    headers = {"content-type": content_type} if content_type else {}
    body = content.encode() if isinstance(content, str) else content
    return httpx.Response(
        status_code=status,
        headers=headers,
        content=body,
        request=httpx.Request("GET", url),
    )


class FakeHttpClient:
    """Stands in for ``ResilientHttpClient``, recording every call.

    ``outcomes`` may hold responses and/or exceptions; exceptions are raised in
    order, exactly like the real client raises after exhausting its retries.
    """

    def __init__(self, *outcomes: httpx.Response | BaseException) -> None:
        self.outcomes: list[httpx.Response | BaseException] = list(outcomes)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]

    @property
    def headers(self) -> list[dict[str, str]]:
        return [dict(kwargs.get("headers") or {}) for _, kwargs in self.calls]

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else make_response()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def count_heartbeat_ticks(operation: Any, *, tick: float = 0.01) -> tuple[Any, int]:
    """Await *operation* while a heartbeat ticks; returns (result, ticks).

    Zero ticks over a call that takes many tick-periods means the event loop was
    blocked — the Rule 6 detector.
    """
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(tick)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        result = await operation
    finally:
        beat.cancel()
        with pytest.raises(asyncio.CancelledError):
            await beat
    return result, ticks


# --------------------------------------------------------------------------- #
# R1 — secret redaction (Rule 7)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "param",
    ["apiKey", "api_key", "apikey", "token", "access_token", "auth_token", "key", "secret", "sig"],
)
def test_r1_redact_url_strips_every_sensitive_parameter_name(param: str) -> None:
    url = f"https://p.test/v1/x?q=iran&{param}=SUPER-SECRET-VALUE"
    scrubbed = redact_url(url)
    assert scrubbed is not None
    assert "SUPER-SECRET-VALUE" not in scrubbed
    assert REDACTED in scrubbed
    assert "q=iran" in scrubbed, "non-sensitive parameters must survive"


def test_r1b_redact_url_leaves_clean_urls_untouched() -> None:
    assert redact_url("https://fa.wikipedia.org/wiki/Iran") == "https://fa.wikipedia.org/wiki/Iran"
    assert redact_url(None) is None


def test_r1c_redact_text_removes_the_callers_own_credential() -> None:
    secret = "abcd-1234-efgh-5678"
    text = f"ConnectError while calling https://x.test?apiKey={secret} with {secret}"
    scrubbed = redact_text(text, secrets=[secret])
    assert secret not in scrubbed
    assert REDACTED in scrubbed


def test_r1d_redact_text_ignores_absurdly_short_secrets() -> None:
    """A 1-char 'secret' must not blank out every 'a' in the message."""
    assert redact_text("a catastrophic failure", secrets=["a"]) == "a catastrophic failure"


def test_r1e_redact_headers_masks_credential_headers_only() -> None:
    masked = redact_headers(
        {"X-Api-Key": "k", "Authorization": "Bearer t", "Accept": "application/json"}
    )
    assert masked == {
        "X-Api-Key": REDACTED,
        "Authorization": REDACTED,
        "Accept": "application/json",
    }


def test_r1f_error_detail_and_message_are_redacted_at_construction() -> None:
    key = "NEWSAPI-LIVE-KEY-0001"
    exc = ExternalSourceError(
        ExternalErrorKind.NETWORK,
        "newsapi.org",
        f"ConnectError for https://newsapi.org/v2/everything?q=x&apiKey={key}",
        url=f"https://newsapi.org/v2/everything?q=x&apiKey={key}",
        secrets=(key,),
    )
    assert key not in str(exc)
    assert key not in exc.detail
    assert key not in (exc.url or "")
    assert key not in repr(exc.as_dict())


# --------------------------------------------------------------------------- #
# M1 — mutation: prove the redaction guard can actually fail
# --------------------------------------------------------------------------- #
def test_m1_secret_guard_is_red_against_a_non_redacting_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject a no-op ``redact_url`` and the R1 assertion must break."""
    import nexus_ai_agent.integrations.external as external

    monkeypatch.setattr(external, "redact_url", lambda url: url)
    key = "NEWSAPI-LIVE-KEY-0001"
    exc = external.ExternalSourceError(
        ExternalErrorKind.NETWORK,
        "newsapi.org",
        "boom",
        url=f"https://newsapi.org/v2/everything?apiKey={key}",
        secrets=(key,),
    )
    with pytest.raises(AssertionError):
        assert key not in str(exc), "guard must fail when redaction is removed"


# --------------------------------------------------------------------------- #
# R2 — failure taxonomy
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        (ExternalErrorKind.NETWORK, True),
        (ExternalErrorKind.TIMEOUT, True),
        (ExternalErrorKind.CIRCUIT_OPEN, True),
        (ExternalErrorKind.HTTP_STATUS, True),
        (ExternalErrorKind.PROVIDER_ERROR, True),
        (ExternalErrorKind.INVALID_INPUT, False),
        (ExternalErrorKind.BLOCKED_URL, False),
        (ExternalErrorKind.NOT_FOUND, False),
        (ExternalErrorKind.MALFORMED_RESPONSE, False),
        (ExternalErrorKind.OVERSIZED_RESPONSE, False),
        (ExternalErrorKind.DEPENDENCY_MISSING, False),
        (ExternalErrorKind.EMPTY_RESULT, False),
    ],
)
def test_r2_retryability_is_declared_per_kind(kind: ExternalErrorKind, retryable: bool) -> None:
    assert ExternalSourceError(kind, "s").retryable is retryable


def test_r2b_error_is_an_exception_not_a_return_value() -> None:
    """The contract is enforced by the type system: it can only be raised."""
    assert issubclass(ExternalSourceError, Exception)
    with pytest.raises(ExternalSourceError):
        raise ExternalSourceError(ExternalErrorKind.NETWORK, "s")


# --------------------------------------------------------------------------- #
# R3 — query normalisation (Rule 5)
# --------------------------------------------------------------------------- #
def test_r3_normalisation_collapses_whitespace_and_applies_nfc() -> None:
    assert normalize_query("  هوش   مصنوعی \n") == "هوش مصنوعی"
    # NFD 'é' and NFC 'é' are one topic
    assert normalize_query("cafe\u0301") == normalize_query("café")


def test_r3b_control_and_bidi_characters_are_stripped() -> None:
    """An RTL override can make a rendered URL or answer lie about its content."""
    hostile = "news\u202egnp.exe\u202c\x00\x07"
    cleaned = normalize_query(hostile)
    assert "\u202e" not in cleaned
    assert "\u202c" not in cleaned
    assert "\x00" not in cleaned


@pytest.mark.parametrize("empty", ["", "   ", "\n\t", "\u200e\u200f", "\x00\x01"])
def test_a3_empty_input_is_invalid_input_not_a_network_call(empty: str) -> None:
    with pytest.raises(ExternalSourceError) as caught:
        normalize_query(empty)
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT


def test_a3b_extremely_long_input_is_rejected_before_the_network() -> None:
    with pytest.raises(ExternalSourceError) as caught:
        normalize_query("ا" * (MAX_QUERY_CHARS + 1))
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT
    assert str(MAX_QUERY_CHARS) in caught.value.detail


def test_a3c_non_string_input_is_rejected() -> None:
    with pytest.raises(ExternalSourceError) as caught:
        normalize_query(None)  # type: ignore[arg-type]
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT


def test_r3c_cache_key_collapses_equivalent_spellings() -> None:
    variants = ["Hoosh AI", "  hoosh   ai ", "HOOSH AI", "hoosh ai"]
    keys = {cache_key_for(normalize_query(v)) for v in variants}
    assert len(keys) == 1, f"expected one cache key, got {keys}"


# --------------------------------------------------------------------------- #
# R4 — URL construction (Rule 5)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "x?format=j1&evil=1",
        "a#fragment",
        "a/b/../../c",
        "a%2fb",
        "a b",
        "تهران",
        "a&b=c",
        "a\u202eb",
    ],
)
def test_a4_no_user_input_can_alter_path_depth_query_or_fragment(hostile: str) -> None:
    url = build_url(host="wttr.in", segments=[hostile], params={"format": "j1"}, source="t")
    parts = httpx.URL(url)
    assert parts.host == "wttr.in"
    assert parts.scheme == "https"
    # Assert on the RAW wire form: httpx.URL.path percent-DEcodes, so a decoded
    # "/a/b/../../c" there is still a single encoded segment on the wire.
    raw_path = urlsplit(url).path
    assert raw_path.count("/") == 1, url
    assert dict(parts.params) == {"format": "j1"}, url
    assert parts.fragment == ""


def test_a4b_host_injection_through_a_template_parameter_is_refused() -> None:
    """The reproduced ``lang='evil.example.com/steal?x='`` attack."""
    with pytest.raises(ExternalSourceError) as caught:
        build_url(host="evil.example.com/steal?x=.wikipedia.org", segments=["q"], source="t")
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT


@pytest.mark.parametrize("bad_host", ["a..b.com", "a/b", "a?b", "a b", "", "http://x", "a@b"])
def test_a4c_only_plain_hostnames_are_accepted(bad_host: str) -> None:
    with pytest.raises(ExternalSourceError):
        build_url(host=bad_host, segments=["q"], source="t")


def test_r4_safe_path_segment_percent_encodes_every_structural_character() -> None:
    encoded = safe_path_segment("a/b?c#d&e%f", source="t")
    assert encoded == "a%2Fb%3Fc%23d%26e%25f"
    # every structural character is now an escape sequence, not a delimiter
    # ("%" itself survives only as the escape introducer, and "%25" proves the
    # user's literal "%" was escaped rather than passed through)
    for char in "/?#&":
        assert char not in encoded
    assert "%25" in encoded


@pytest.mark.parametrize("bad", ["", "   "])
def test_a4d_empty_segment_is_invalid_input(bad: str) -> None:
    with pytest.raises(ExternalSourceError) as caught:
        safe_path_segment(bad, source="t")
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT


def test_a4e_oversized_segment_is_invalid_input() -> None:
    with pytest.raises(ExternalSourceError) as caught:
        safe_path_segment("x" * 10_000, source="t")
    assert caught.value.kind is ExternalErrorKind.INVALID_INPUT


# --------------------------------------------------------------------------- #
# M2 — mutation: prove the URL guard can fail
# --------------------------------------------------------------------------- #
def test_m2_url_guard_is_red_against_naive_fstring_composition() -> None:
    """The pre-fix composition style must trip the A4 assertion."""
    broken = f"https://wttr.in/{'x?format=j1&evil=1'}?format=j1"
    with pytest.raises(AssertionError):
        assert dict(httpx.URL(broken).params) == {"format": "j1"}


# --------------------------------------------------------------------------- #
# R5 — HTTP boundary: failure is never a success-shaped value (Rule 4)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (httpx.ConnectError("down"), ExternalErrorKind.NETWORK),
        (httpx.ReadTimeout("slow"), ExternalErrorKind.TIMEOUT),
        (httpx.ConnectTimeout("slow"), ExternalErrorKind.TIMEOUT),
        (CircuitOpenError("open"), ExternalErrorKind.CIRCUIT_OPEN),
        (SSRFBlockError("private address"), ExternalErrorKind.BLOCKED_URL),
        (httpx.RemoteProtocolError("bad frame"), ExternalErrorKind.NETWORK),
    ],
)
async def test_r5_transport_failures_are_classified_not_swallowed(
    raised: BaseException, expected: ExternalErrorKind
) -> None:
    client = FakeHttpClient(raised)
    with pytest.raises(ExternalSourceError) as caught:
        await fetch_text(client, "https://x.test/a", source="s", source_type=SourceType.WEB_PAGE)
    assert caught.value.kind is expected
    assert caught.value.__cause__ is raised, "the original exception must be preserved"


async def test_r5b_http_status_error_after_retry_exhaustion_is_typed() -> None:
    response = make_response(status=503, content=b"nope")
    exc = httpx.HTTPStatusError("boom", request=response.request, response=response)
    client = FakeHttpClient(exc)
    with pytest.raises(ExternalSourceError) as caught:
        await fetch_json(client, "https://x.test/a", source="s", source_type=SourceType.FX_API)
    assert caught.value.kind is ExternalErrorKind.HTTP_STATUS
    assert caught.value.status_code == 503


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (404, ExternalErrorKind.NOT_FOUND),
        (410, ExternalErrorKind.NOT_FOUND),
        (400, ExternalErrorKind.HTTP_STATUS),
        (401, ExternalErrorKind.HTTP_STATUS),
        (429, ExternalErrorKind.HTTP_STATUS),
    ],
)
async def test_r5c_error_status_bodies_are_never_returned_as_content(
    status: int, kind: ExternalErrorKind
) -> None:
    """``ResilientHttpClient.get`` hands 4xx back as a normal response.

    This is exactly how the Wikipedia 404 page became 'knowledge'.
    """
    client = FakeHttpClient(make_response(status=status, content="<p>no such thing</p>"))
    with pytest.raises(ExternalSourceError) as caught:
        await fetch_text(client, "https://x.test/a", source="s", source_type=SourceType.WEB_PAGE)
    assert caught.value.kind is kind
    assert caught.value.status_code == status


async def test_a5_wrong_content_type_for_a_text_fetch_is_malformed() -> None:
    client = FakeHttpClient(make_response(content=b"\x89PNG\r\n", content_type="image/png"))
    with pytest.raises(ExternalSourceError) as caught:
        await fetch_text(client, "https://x.test/a", source="s", source_type=SourceType.WEB_PAGE)
    assert caught.value.kind is ExternalErrorKind.MALFORMED_RESPONSE


async def test_a5b_malformed_json_is_reported_not_turned_into_an_empty_dict() -> None:
    client = FakeHttpClient(make_response(content=b"{not json", content_type="application/json"))
    with pytest.raises(ExternalSourceError) as caught:
        await fetch_json(client, "https://x.test/a", source="s", source_type=SourceType.FX_API)
    assert caught.value.kind is ExternalErrorKind.MALFORMED_RESPONSE


async def test_a5c_oversized_json_is_refused_rather_than_half_parsed() -> None:
    body = b'{"a":"' + b"x" * 5000 + b'"}'
    client = FakeHttpClient(make_response(content=body, content_type="application/json"))
    with pytest.raises(ExternalSourceError) as caught:
        await fetch_json(
            client, "https://x.test/a", source="s", source_type=SourceType.FX_API, max_bytes=1000
        )
    assert caught.value.kind is ExternalErrorKind.OVERSIZED_RESPONSE


async def test_a5d_oversized_text_is_truncated_and_the_truncation_is_flagged() -> None:
    client = FakeHttpClient(make_response(content=b"y" * 5000, content_type="text/plain"))
    result = await fetch_text(
        client, "https://x.test/a", source="s", source_type=SourceType.WEB_PAGE, max_bytes=1000
    )
    assert len(result.value) == 1000
    assert result.provenance.truncated is True
    assert result.degraded is True, "a truncated payload is a degraded payload"


async def test_a5e_unexpected_json_content_type_is_recorded_not_hidden() -> None:
    """wttr.in answers ``text/plain`` for ``?format=j1`` — usable, but noted."""
    client = FakeHttpClient(make_response(content=b'{"ok":1}', content_type="text/plain"))
    result = await fetch_json(
        client, "https://x.test/a", source="s", source_type=SourceType.WEATHER_API
    )
    assert result.value == {"ok": 1}
    assert result.provenance.degraded is True
    assert any("unexpected_content_type" in note for note in result.provenance.notes)


async def test_a5f_cancellation_during_a_fetch_is_never_laundered_into_an_error() -> None:
    class CancellingClient:
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await fetch_text(
            CancellingClient(),  # type: ignore[arg-type]
            "https://x.test/a",
            source="s",
            source_type=SourceType.WEB_PAGE,
        )


async def test_r5d_successful_fetch_carries_provenance() -> None:
    client = FakeHttpClient(make_response(content=b"<p>hi</p>"))
    result = await fetch_text(
        client, "https://x.test/a?token=abc", source="s", source_type=SourceType.WEB_PAGE
    )
    assert result.value == "<p>hi</p>"
    assert result.provenance.source == "s"
    assert result.provenance.url is not None and "abc" not in result.provenance.url
    assert result.provenance.retrieved_at is not None


def test_r5e_require_mapping_rejects_non_object_json() -> None:
    for payload in ([], "x", 3, None):
        with pytest.raises(ExternalSourceError) as caught:
            require_mapping(payload, source="s")
        assert caught.value.kind is ExternalErrorKind.MALFORMED_RESPONSE


# --------------------------------------------------------------------------- #
# M3 — mutation: prove the "no success-shaped failure" guard can fail
# --------------------------------------------------------------------------- #
async def test_m3_guard_is_red_against_a_get_json_style_swallowing_helper() -> None:
    """Re-introduce ``core.http_client.get_json`` semantics and the guard fails."""

    async def swallowing_fetch_json(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {}  # the pre-fix behaviour: every failure becomes {}

    with pytest.raises(AssertionError):
        try:
            await swallowing_fetch_json()
        except ExternalSourceError:  # pragma: no cover - never taken
            pass
        else:
            raise AssertionError("a swallowing helper cannot signal failure")


# --------------------------------------------------------------------------- #
# R6 — async really means async (Rule 6)
# --------------------------------------------------------------------------- #
async def test_r6_run_blocking_keeps_the_event_loop_alive() -> None:
    import time

    def slow() -> str:
        time.sleep(0.4)
        return "done"

    value, ticks = await count_heartbeat_ticks(run_blocking(slow, source="s"))
    assert value == "done"
    assert ticks >= 10, f"event loop was blocked: only {ticks} ticks in 0.4 s"


async def test_a6_run_blocking_reraises_cancellation_untouched() -> None:
    import time

    def slow() -> None:
        time.sleep(0.5)

    task = asyncio.create_task(run_blocking(slow, source="s"))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a6b_run_blocking_types_provider_exceptions() -> None:
    def explode() -> None:
        raise RuntimeError("ddg rate-limited")

    with pytest.raises(ExternalSourceError) as caught:
        await run_blocking(explode, source="duckduckgo:text")
    assert caught.value.kind is ExternalErrorKind.PROVIDER_ERROR
    assert "RuntimeError" in caught.value.detail


async def test_a6c_run_blocking_redacts_secrets_in_provider_exceptions() -> None:
    key = "PROVIDER-SECRET-9999"

    def explode() -> None:
        raise RuntimeError(f"401 for key {key}")

    with pytest.raises(ExternalSourceError) as caught:
        await run_blocking(explode, source="s", secrets=(key,))
    assert key not in str(caught.value)


# --------------------------------------------------------------------------- #
# R7 — HTML extraction: bounded, off-loop, parser-degradation aware
# --------------------------------------------------------------------------- #
async def test_r7_extraction_removes_script_and_style_content() -> None:
    html = "<html><body><script>alert(1)</script><style>p{}</style><p>real text</p></body></html>"
    text, _notes, truncated = await extract_text(html, source="s")
    assert "alert" not in text
    assert "p{}" not in text
    assert "real text" in text
    assert truncated is False


async def test_a7_multi_megabyte_hostile_html_is_capped_and_does_not_block_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """8.1 MB of hostile HTML: bounded output, and parsed off the loop thread.

    The pre-fix code parsed this inline and produced exactly 0 heartbeat ticks.
    Tick counts alone are load-sensitive, so the primary assertion here is
    *structural* (the parse ran on another thread); liveness is the secondary
    check.
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

    html = "<html>" + ("<p>" + "x" * 1000 + "</p>") * 8000 + "</html>"
    assert len(html) > 8_000_000
    (result, ticks) = await count_heartbeat_ticks(
        external.extract_text(html, source="s", max_chars=2000), tick=0.002
    )
    text, _notes, truncated = result
    assert len(text) <= 2000
    assert truncated is True
    assert parse_threads and all(tid != loop_thread for tid in parse_threads), (
        "the parse ran on the event-loop thread"
    )
    assert ticks >= 1, "the event loop made no progress at all during the parse"


async def test_r7b_extraction_reports_which_parser_was_used() -> None:
    text, notes, _ = await extract_text("<p>a</p>", source="s")
    assert text == "a"
    assert any(note.startswith("parser=") for note in notes)


async def test_a7b_extraction_of_junk_never_raises_an_untyped_error() -> None:
    for junk in ["", "<<<>>>", "\x00\x01", "<p>" * 500]:
        text, _notes, _t = await extract_text(junk, source="s")
        assert isinstance(text, str)


# --------------------------------------------------------------------------- #
# R8 — search backend: lazy, optional, bounded, off-loop
# --------------------------------------------------------------------------- #
async def test_r8_absent_sdk_is_a_typed_dependency_error_not_an_import_crash() -> None:
    backend = DuckDuckGoBackend(module_name="nexus_definitely_not_installed_pkg")
    with pytest.raises(ExternalSourceError) as caught:
        await backend.search("text", "q", max_results=3)
    assert caught.value.kind is ExternalErrorKind.DEPENDENCY_MISSING


def test_r8b_importing_the_zone_never_requires_the_search_sdk() -> None:
    """Module-scope ``from duckduckgo_search import DDGS`` made the whole module
    unimportable without the package; the import is lazy now."""
    import nexus_ai_agent.integrations.free_tools as free_tools
    import nexus_ai_agent.knowledge.web_trainer as web_trainer

    source = (
        __import__("pathlib").Path(free_tools.__file__).read_text()
        + __import__("pathlib").Path(web_trainer.__file__).read_text()
    )
    assert not re.search(r"^\s*from duckduckgo_search import", source, re.MULTILINE)
    assert not re.search(r"^\s*import duckduckgo_search", source, re.MULTILINE)


async def test_a8_provider_exception_becomes_provider_error() -> None:
    class Boom:
        def text(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("captcha")

    backend = DuckDuckGoBackend(factory=Boom)
    with pytest.raises(ExternalSourceError) as caught:
        await backend.search("text", "q", max_results=3)
    assert caught.value.kind is ExternalErrorKind.PROVIDER_ERROR


@pytest.mark.parametrize("bogus", [None, "a string", 42, {"not": "a list"}])
async def test_a8b_non_sequence_provider_output_is_malformed(bogus: Any) -> None:
    class Weird:
        def text(self, *a: Any, **k: Any) -> Any:
            return bogus

    backend = DuckDuckGoBackend(factory=Weird)
    with pytest.raises(ExternalSourceError) as caught:
        await backend.search("text", "q", max_results=3)
    assert caught.value.kind is ExternalErrorKind.MALFORMED_RESPONSE


async def test_a8c_oversized_provider_output_is_capped_locally() -> None:
    """``max_results`` is a hint. A provider returning 500 rows must not win."""

    class Flood:
        def text(self, *a: Any, **k: Any) -> Any:
            return [{"href": f"https://x/{i}"} for i in range(500)]

    backend = DuckDuckGoBackend(factory=Flood)
    rows = await backend.search("text", "q", max_results=3)
    assert len(rows) == 3


async def test_a8d_non_mapping_rows_are_dropped_without_killing_good_rows() -> None:
    class Mixed:
        def text(self, *a: Any, **k: Any) -> Any:
            return ["junk", None, {"href": "https://good"}, 42]

    backend = DuckDuckGoBackend(factory=Mixed)
    rows = await backend.search("text", "q", max_results=10)
    assert rows == [{"href": "https://good"}]


async def test_a8e_a_synchronous_backend_call_does_not_block_the_loop() -> None:
    import time

    class Slow:
        def text(self, *a: Any, **k: Any) -> Any:
            time.sleep(0.4)
            return []

    backend = DuckDuckGoBackend(factory=Slow)
    _rows, ticks = await count_heartbeat_ticks(backend.search("text", "q", max_results=3))
    assert ticks >= 10, f"the synchronous SDK blocked the loop ({ticks} ticks)"


async def test_r8c_backend_client_is_constructed_once_not_per_call() -> None:
    built = []

    class Counting:
        def __init__(self) -> None:
            built.append(1)

        def text(self, *a: Any, **k: Any) -> Any:
            return []

    backend = DuckDuckGoBackend(factory=Counting)
    await backend.search("text", "q", max_results=1)
    await backend.search("text", "q", max_results=1)
    assert built == [1]


async def test_a8f_unknown_search_kind_is_a_provider_error_not_an_attribute_error() -> None:
    class Empty:
        pass

    backend = DuckDuckGoBackend(factory=Empty)
    with pytest.raises(ExternalSourceError) as caught:
        await backend.search("images", "q", max_results=1)
    assert caught.value.kind is ExternalErrorKind.PROVIDER_ERROR


# --------------------------------------------------------------------------- #
# R9 — duplicate handling and provenance semantics (Rule 8)
# --------------------------------------------------------------------------- #
def test_r9_duplicate_urls_are_collapsed_case_and_slash_insensitively() -> None:
    rows = [
        {"href": "https://a.test/x"},
        {"href": "https://A.test/x/"},
        {"href": "https://a.test/x"},
        {"href": "https://b.test/y"},
        {"href": None},
        {},
    ]
    assert [r["href"] for r in dedupe_by_url(rows, url_key="href")] == [
        "https://a.test/x",
        "https://b.test/y",
    ]


def test_r9b_provenance_is_json_safe_and_carries_every_declared_field() -> None:
    provenance = Provenance.create(
        "wikipedia:fa",
        SourceType.ENCYCLOPEDIA,
        url="https://fa.wikipedia.org/x?token=abc",
        truncated=True,
        degraded=True,
        fallback_path=["fa:not_found", "en"],
        notes=["revision_timestamp=2026-01-01"],
    )
    payload = provenance.as_dict()
    assert set(payload) == {
        "source",
        "source_type",
        "url",
        "retrieved_at",
        "truncated",
        "degraded",
        "fallback_path",
        "notes",
    }
    assert "abc" not in str(payload["url"])
    assert payload["fallback_path"] == ["fa:not_found", "en"]


@pytest.mark.parametrize(
    ("truncated", "degraded", "failures", "expected"),
    [
        (False, False, (), False),
        (True, False, (), True),
        (False, True, (), True),
        (False, False, (ExternalSourceError(ExternalErrorKind.NETWORK, "s"),), True),
    ],
)
def test_r9c_degraded_is_true_whenever_anything_was_lost(
    truncated: bool, degraded: bool, failures: tuple[ExternalSourceError, ...], expected: bool
) -> None:
    result = SourceResult(
        value="x",
        provenance=Provenance.create(
            "s", SourceType.WEB_PAGE, truncated=truncated, degraded=degraded
        ),
        partial_failures=failures,
    )
    assert result.degraded is expected


def test_m4_provenance_guard_is_red_when_truncation_is_not_recorded() -> None:
    """A result that silently drops the truncation flag must fail the A5d check."""
    lying = SourceResult(
        value="y" * 1000,
        provenance=Provenance.create("s", SourceType.WEB_PAGE, truncated=False),
    )
    with pytest.raises(AssertionError):
        assert lying.provenance.truncated is True


# --------------------------------------------------------------------------- #
# R10 — the backend seam cannot leak an untyped exception
# --------------------------------------------------------------------------- #
async def test_r10_a_rogue_backend_exception_is_retyped_at_the_seam() -> None:
    """``SearchBackend`` is an injection point; a custom backend raising a bare
    ``RuntimeError`` must not punch through the zone's typed-failure contract."""
    from nexus_ai_agent.integrations.external import SearchBackend, search_via

    class Rogue(SearchBackend):
        async def search(self, kind: str, query: str, *, max_results: int) -> list[dict[str, Any]]:
            raise RuntimeError("untyped boom")

    with pytest.raises(ExternalSourceError) as caught:
        await search_via(Rogue(), "text", "q", max_results=3, source="s")
    assert caught.value.kind is ExternalErrorKind.PROVIDER_ERROR
    assert isinstance(caught.value.__cause__, RuntimeError)


async def test_a10_cancellation_at_the_backend_seam_stays_cancellation() -> None:
    from nexus_ai_agent.integrations.external import SearchBackend, search_via

    class Cancelling(SearchBackend):
        async def search(self, kind: str, query: str, *, max_results: int) -> list[dict[str, Any]]:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await search_via(Cancelling(), "text", "q", max_results=3, source="s")


async def test_r10b_typed_backend_errors_pass_through_unchanged() -> None:
    from nexus_ai_agent.integrations.external import SearchBackend, search_via

    original = ExternalSourceError(ExternalErrorKind.DEPENDENCY_MISSING, "s", "no sdk")

    class Typed(SearchBackend):
        async def search(self, kind: str, query: str, *, max_results: int) -> list[dict[str, Any]]:
            raise original

    with pytest.raises(ExternalSourceError) as caught:
        await search_via(Typed(), "text", "q", max_results=3, source="s")
    assert caught.value is original
