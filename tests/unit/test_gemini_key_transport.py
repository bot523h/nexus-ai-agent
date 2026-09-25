"""S4: Gemini API-key transport — header, never the URL.

Inventory of Gemini call sites in ``src/`` (kept complete by the source
scan below):

1. ``features/ai_chat.py``    — ``GeminiEngine._call_gemini``
2. ``features/summarizer.py`` — ``SummarizerEngine.summarize_text``
3. ``creative/video_director.py`` — ``analyze_video_with_gemini``
4. ``creative/slideshow/analysis.py`` — ``analyze_with_gemini``
5. ``creative/image_gen/gemini_adapter.py`` — already header-based
   (the reference implementation this migration converges on)

Every site must send the key as the ``x-goog-api-key`` header. A
``?key=<secret>`` query parameter leaks into httpx INFO log lines
(``HTTP Request: POST …?key=…``), proxy/access logs and exception
reprs — the redaction layer is defence-in-depth, not the control.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus_ai_agent.features.ai_chat import GeminiEngine
from nexus_ai_agent.features.summarizer import SummarizerEngine


def _json_dumps(payload: Any) -> str:
    import json

    return json.dumps(payload)


SRC = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent"

#: Gemini call-site modules — the complete inventory.
GEMINI_SITE_FILES = [
    "features/ai_chat.py",
    "features/summarizer.py",
    "creative/video_director.py",
    "creative/slideshow/analysis.py",
    "creative/image_gen/gemini_adapter.py",
]

RESPONSE_PAYLOAD = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}


def test_no_gemini_call_site_uses_query_param_auth() -> None:
    """Source scan: no ``?key=`` in URLs and no ``params={"key": …}``
    anywhere in the Gemini call-site files (or anywhere else in src,
    apart from the redaction rules' own docstrings)."""
    pattern = re.compile(r'generateContent\?key=|params=\{\s*"key"|\?key=\{')
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert not offenders, "Gemini API key in URL query parameters:\n" + "\n".join(offenders)


async def test_ai_chat_gemini_sends_key_in_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(
        self: httpx.AsyncClient,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = headers or {}
        return httpx.Response(200, json=RESPONSE_PAYLOAD)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    engine = GeminiEngine(api_key="gemini-secret-key", model="gemini-2.0-flash")
    await engine._call_gemini([{"role": "user", "parts": [{"text": "hi"}]}])
    assert captured["headers"].get("x-goog-api-key") == "gemini-secret-key"
    assert "gemini-secret-key" not in captured["url"]
    assert "?key=" not in captured["url"]


async def test_summarizer_sends_key_in_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(
        self: httpx.AsyncClient,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = headers or {}
        return httpx.Response(200, json=RESPONSE_PAYLOAD, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    engine = SummarizerEngine(gemini_api_key="gemini-secret-key")
    try:
        result = await engine.summarize_text("some long text to summarize", mode="brief")
        assert result.error is None
    finally:
        await engine.close()
    assert captured["headers"].get("x-goog-api-key") == "gemini-secret-key"
    assert "gemini-secret-key" not in captured["url"]
    assert "?key=" not in captured["url"]


def test_slideshow_analysis_sends_key_in_header(
    slideshow_media: Any,  # provided by tests/unit/conftest.py
) -> None:
    from nexus_ai_agent.creative.slideshow.analysis import GeminiConfig, analyze_with_gemini
    from nexus_ai_agent.creative.slideshow.probe import probe_image

    requests_seen: list[httpx.Request] = []
    evidence = (probe_image(slideshow_media.crisp_image),)
    score_sheet = {
        "images": [
            {
                "evidence_id": evidence[0].evidence_id,
                "labels": ["portrait"],
                "sharpness": 0.6,
                "aesthetic": 0.5,
                "subject": "person",
                "suggested_role": "body",
            }
        ],
        "recommended_template_id": "cinematic_epic",
        "reasoning": "wide golden-hour frames",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": _json_dumps(score_sheet)}]}}]},
        )

    analysis = analyze_with_gemini(
        evidence,
        config=GeminiConfig(
            api_key="gemini-secret-key",
            model="gemini-2.0-flash",
            transport=httpx.MockTransport(handler),
        ),
        allow_upload=True,
    )
    assert analysis.source == "gemini"
    assert len(requests_seen) == 1
    sent = requests_seen[0]
    assert sent.headers.get("x-goog-api-key") == "gemini-secret-key"
    assert "key" not in sent.url.params
    assert "gemini-secret-key" not in str(sent.url)


def test_video_director_sends_key_in_header() -> None:
    """Covered end-to-end in test_creative_studio.py; asserted here as
    part of the complete inventory with the same guard shape.

    Integration note (release 2026-09-25): this was a cross-module
    ``from tests.unit.test_creative_studio import ...`` re-export guard.
    That shape breaks under CI's bare-``pytest`` entry point (no ``tests``
    package on ``sys.path``) and is banned by the task-183 suite-hygiene
    guard, so the guard now pins the sibling test's *existence* with a
    self-contained AST scan instead of importing it. Same intent: this
    test fails if the end-to-end coverage is renamed or removed.
    """
    import ast

    sibling = Path(__file__).with_name("test_creative_studio.py")
    tree = ast.parse(sibling.read_text(encoding="utf-8"), filename=str(sibling))
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "test_video_director_calls_gemini" in names
