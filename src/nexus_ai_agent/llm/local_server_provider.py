"""Local LLM provider speaking to a ``llama.cpp`` server (task-115).

The `llama-server` binary exposes an OpenAI-compatible HTTP API; this
provider needs no heavy native binding and no extra install — plain ``httpx``
(core dependency) against ``/v1/chat/completions``, ``/v1/embeddings`` and
``/health``.

Start a server (example)::

    llama-server -m models/model.gguf --port 8080 --ctx-size 4096

then point the engine at it::

    export NEXUS_LLAMA_SERVER_BASE_URL=http://127.0.0.1:8080

Unlike the legacy in-process ``LocalLlamaCppProvider`` (GGUF file +
``llama-cpp-python``), the server process owns the model weights, so the bot
stays light and the model can be shared with other tools.
"""

from __future__ import annotations

from typing import Any

import httpx

from nexus_ai_agent.llm.provider import LLMProvider

START_HINT = "start one with: llama-server -m <model.gguf> --port 8080"


class LlamaServerError(RuntimeError):
    """The llama.cpp server could not serve the request (with why + fix)."""


class LocalLlamaServerProvider(LLMProvider):
    """``LLMProvider`` backed by a locally running ``llama-server``."""

    def __init__(
        self,
        base_url: str,
        model: str = "local-model",
        timeout: float = 120.0,
        max_tokens: int = 512,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        """Configured server base URL (no trailing slash)."""
        return self._base_url

    async def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _connection_error(self, path: str, exc: Exception) -> LlamaServerError:
        return LlamaServerError(
            f"llama.cpp server unreachable at {self._base_url}{path}: {exc} ({START_HINT})"
        )

    async def generate(self, prompt: str, system: str = "") -> str:
        """Complete ``prompt`` via ``/v1/chat/completions`` (non-streaming)."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "stream": False,
        }
        client = await self._client_or_create()
        try:
            response = await client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.ConnectError as exc:
            raise self._connection_error("/v1/chat/completions", exc) from exc
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "")[:300]
            raise LlamaServerError(
                f"llama.cpp server error {exc.response.status_code} on /v1/chat/completions: {body}"
            ) from exc
        data = response.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlamaServerError(
                f"llama.cpp server returned an unexpected chat payload: {str(data)[:300]}"
            ) from exc
        return (text or "").strip()

    async def embed(self, text: str) -> list[float]:
        """Embed ``text`` via ``/v1/embeddings`` (needs ``--embedding``)."""
        client = await self._client_or_create()
        try:
            response = await client.post(
                "/v1/embeddings", json={"model": self._model, "input": text}
            )
            response.raise_for_status()
        except httpx.ConnectError as exc:
            raise self._connection_error("/v1/embeddings", exc) from exc
        except httpx.HTTPStatusError as exc:
            body = (exc.response.text or "")[:300]
            raise LlamaServerError(
                f"llama.cpp server error {exc.response.status_code} "
                f"on /v1/embeddings: {body} "
                "(embeddings need `llama-server --embedding`)"
            ) from exc
        data = response.json()
        try:
            vector = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlamaServerError(
                f"llama.cpp server returned an unexpected embeddings payload: {str(data)[:300]}"
            ) from exc
        if not isinstance(vector, list) or not all(isinstance(x, (int, float)) for x in vector):
            raise LlamaServerError("llama.cpp server returned a non-numeric embedding vector")
        return [float(x) for x in vector]

    async def health(self) -> bool:
        """Readiness probe: ``True`` only when ``GET /health`` answers 200."""
        client = await self._client_or_create()
        try:
            response = await client.get("/health")
        except httpx.HTTPError:
            return False
        return response.status_code == 200
