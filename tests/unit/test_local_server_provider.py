"""Unit tests for the llama.cpp server provider (task-115).

All HTTP runs against an in-process stub ``llama-server`` — no network, no
model weights, no native binding. ``asyncio_mode = auto`` picks up the
coroutine tests.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.llm.litellm_provider import build_llm_provider
from nexus_ai_agent.llm.local_server_provider import (
    LlamaServerError,
    LocalLlamaServerProvider,
)


class _StubLlamaServer(BaseHTTPRequestHandler):
    mode: str = "healthy"
    requests: list[dict[str, Any]] = []

    def log_message(self, *_args: object) -> None:  # silence test output
        return

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self) -> None:
        if self.path == "/health":
            if _StubLlamaServer.mode == "healthy":
                self._send(200, {"status": "ok"})
            else:
                self._send(503, {"status": "loading model"})
        else:
            self._send(404, {})

    def do_POST(self) -> None:
        payload = self._read_json()
        _StubLlamaServer.requests.append({"path": self.path, "body": payload})
        mode = _StubLlamaServer.mode
        if self.path == "/v1/chat/completions":
            if mode == "error":
                self._send(500, {"error": "kv cache full"})
            elif mode == "malformed":
                self._send(200, {"unexpected": "shape"})
            else:
                self._send(
                    200,
                    {"choices": [{"message": {"role": "assistant", "content": "  stub reply  "}}]},
                )
        elif self.path == "/v1/embeddings":
            if mode == "error":
                self._send(500, {"error": "embeddings disabled"})
            elif mode == "malformed":
                self._send(200, {"data": [{"embedding": ["not", "numbers"]}]})
            else:
                self._send(200, {"data": [{"embedding": [0.1, 0.2, 0.3]}]})
        else:
            self._send(404, {})


@pytest.fixture()
def server_url() -> str:
    _StubLlamaServer.mode = "healthy"
    _StubLlamaServer.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubLlamaServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def provider(server_url: str) -> LocalLlamaServerProvider:
    client = LocalLlamaServerProvider(server_url, model="stub-model", timeout=5.0)
    yield client


async def test_generate_returns_stripped_content(provider: LocalLlamaServerProvider) -> None:
    assert await provider.generate("hello") == "stub reply"
    await provider.aclose()


async def test_generate_sends_openai_chat_payload(
    provider: LocalLlamaServerProvider,
) -> None:
    await provider.generate("hi", system="be brief")
    (request,) = _StubLlamaServer.requests
    assert request["path"] == "/v1/chat/completions"
    body = request["body"]
    assert body["model"] == "stub-model"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    await provider.aclose()


async def test_generate_omits_empty_system_message(
    provider: LocalLlamaServerProvider,
) -> None:
    await provider.generate("hi")
    (request,) = _StubLlamaServer.requests
    assert request["body"]["messages"] == [{"role": "user", "content": "hi"}]
    await provider.aclose()


async def test_generate_server_error_carries_status(
    provider: LocalLlamaServerProvider,
) -> None:
    _StubLlamaServer.mode = "error"
    with pytest.raises(LlamaServerError, match="500"):
        await provider.generate("hi")
    await provider.aclose()


async def test_generate_malformed_payload_raises(
    provider: LocalLlamaServerProvider,
) -> None:
    _StubLlamaServer.mode = "malformed"
    with pytest.raises(LlamaServerError, match="unexpected chat payload"):
        await provider.generate("hi")
    await provider.aclose()


async def test_generate_unreachable_server_hints_start_command() -> None:
    provider = LocalLlamaServerProvider("http://127.0.0.1:1", timeout=1.0)
    with pytest.raises(LlamaServerError, match="llama-server -m"):
        await provider.generate("hi")
    await provider.aclose()


async def test_embed_returns_float_vector(provider: LocalLlamaServerProvider) -> None:
    vector = await provider.embed("hello")
    assert vector == [0.1, 0.2, 0.3]
    assert all(isinstance(x, float) for x in vector)
    await provider.aclose()


async def test_embed_error_points_at_embedding_flag(
    provider: LocalLlamaServerProvider,
) -> None:
    _StubLlamaServer.mode = "error"
    with pytest.raises(LlamaServerError, match="--embedding"):
        await provider.embed("hi")
    await provider.aclose()


async def test_embed_rejects_non_numeric_vector(
    provider: LocalLlamaServerProvider,
) -> None:
    _StubLlamaServer.mode = "malformed"
    with pytest.raises(LlamaServerError, match="non-numeric"):
        await provider.embed("hi")
    await provider.aclose()


async def test_health_reports_readiness(provider: LocalLlamaServerProvider) -> None:
    assert await provider.health() is True
    _StubLlamaServer.mode = "loading"
    assert await provider.health() is False
    await provider.aclose()


async def test_health_false_when_unreachable() -> None:
    provider = LocalLlamaServerProvider("http://127.0.0.1:1", timeout=1.0)
    assert await provider.health() is False
    await provider.aclose()


def test_constructor_validates_arguments() -> None:
    with pytest.raises(ValueError, match="base_url"):
        LocalLlamaServerProvider("  ")
    with pytest.raises(ValueError, match="timeout"):
        LocalLlamaServerProvider("http://x", timeout=0)
    with pytest.raises(ValueError, match="max_tokens"):
        LocalLlamaServerProvider("http://x", max_tokens=0)
    assert LocalLlamaServerProvider("http://x:8080/").base_url == "http://x:8080"


def test_importing_provider_pulls_no_heavy_bindings() -> None:
    assert "llama_cpp" not in sys.modules
    assert "sentence_transformers" not in sys.modules


def test_factory_selects_llama_server_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir("/")  # keep away from any repo-level model files
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        TELEGRAM_BOT_TOKEN="test-token",
        NEXUS_LLM_ROUTING_ENABLED="false",
        NEXUS_MODEL_PATH="/nonexistent/model.gguf",
        NEXUS_LLAMA_SERVER_BASE_URL="http://127.0.0.1:8080",
        NEXUS_LLAMA_SERVER_MODEL="qwen-test",
    )
    llm, label = build_llm_provider(settings)
    assert isinstance(llm, LocalLlamaServerProvider)
    assert "llama.cpp server" in label
    assert "127.0.0.1:8080" in label


def test_factory_ignores_llama_server_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir("/")
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        TELEGRAM_BOT_TOKEN="test-token",
        NEXUS_LLM_ROUTING_ENABLED="false",
        NEXUS_MODEL_PATH="/nonexistent/model.gguf",
    )
    llm, label = build_llm_provider(settings)
    assert not isinstance(llm, LocalLlamaServerProvider)
    assert "FakeLLM" in label
