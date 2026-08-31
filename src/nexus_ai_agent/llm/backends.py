from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

import httpx

from nexus_ai_agent.llm.schemas import GenerateRequest


class Backend(Protocol):
    """Provider-neutral contract implemented by each local inference backend."""

    name: str

    async def generate(self, request: GenerateRequest) -> str: ...

    def stream(self, request: GenerateRequest) -> AsyncIterator[str]: ...


class _LlamaModel(Protocol):
    def create_chat_completion(self, **kwargs: object) -> object: ...


class LlamaCppBackend:
    """Optional llama.cpp backend loading a local GGUF model lazily."""

    name = "llama.cpp"

    def __init__(self, model_path: str, n_ctx: int = 4096, n_gpu_layers: int = 0) -> None:
        path = Path(model_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"GGUF model file not found: {path}")
        from llama_cpp import Llama

        self._model: _LlamaModel = cast(
            _LlamaModel,
            Llama(model_path=str(path), n_ctx=n_ctx, n_gpu_layers=n_gpu_layers, verbose=False),
        )

    def _completion_kwargs(self, request: GenerateRequest, stream: bool) -> dict[str, object]:
        return {
            "messages": [message.model_dump() for message in request.messages],
            "tools": [tool.model_dump() for tool in request.tools] or None,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": stream,
        }

    async def generate(self, request: GenerateRequest) -> str:
        def run() -> str:
            result = self._model.create_chat_completion(**self._completion_kwargs(request, False))
            payload = cast(Mapping[str, object], result)
            choices = cast(Sequence[Mapping[str, object]], payload["choices"])
            message = cast(Mapping[str, object], choices[0]["message"])
            return str(message.get("content") or "").strip()

        return await asyncio.to_thread(run)

    async def _next_chunk(
        self, iterator: Iterator[Mapping[str, object]]
    ) -> tuple[bool, Mapping[str, object] | None]:
        def get_next() -> tuple[bool, Mapping[str, object] | None]:
            try:
                chunk = next(iterator)
            except StopIteration:
                return False, None
            return True, chunk

        return await asyncio.to_thread(get_next)

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        raw = await asyncio.to_thread(
            lambda: self._model.create_chat_completion(**self._completion_kwargs(request, True))
        )
        iterator = iter(cast(Sequence[Mapping[str, object]], raw))
        while True:
            has_chunk, chunk = await self._next_chunk(iterator)
            if not has_chunk or chunk is None:
                return
            choices = cast(Sequence[Mapping[str, object]], chunk.get("choices", []))
            if choices:
                delta = cast(Mapping[str, object], choices[0].get("delta", {}))
                text = str(delta.get("content") or "")
                if text:
                    yield text


class OllamaBackend:
    """Ollama HTTP adapter; the Ollama daemon remains a local dependency."""

    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = httpx.Timeout(timeout_seconds)

    @staticmethod
    def _messages(request: GenerateRequest) -> list[dict[str, object]]:
        return [message.model_dump() for message in request.messages]

    @staticmethod
    def _normalize_http_error(exc: httpx.HTTPError) -> ConnectionError | RuntimeError:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            return RuntimeError(f"Ollama rejected request with HTTP {exc.response.status_code}")
        return ConnectionError(f"Ollama unavailable: {exc}")

    async def generate(self, request: GenerateRequest) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": self._messages(request),
            "stream": False,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        if request.tools:
            payload["tools"] = [tool.model_dump() for tool in request.tools]
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._normalize_http_error(exc) from exc
        body = cast(Mapping[str, object], response.json())
        message = cast(Mapping[str, object], body.get("message", {}))
        return str(message.get("content") or "").strip()

    async def stream(self, request: GenerateRequest) -> AsyncIterator[str]:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": self._messages(request),
            "stream": True,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/api/chat", json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        body = cast(Mapping[str, object], json.loads(line))
                        message = cast(Mapping[str, object], body.get("message", {}))
                        text = str(message.get("content") or "")
                        if text:
                            yield text
                        if body.get("done") is True:
                            return
        except httpx.HTTPError as exc:
            raise self._normalize_http_error(exc) from exc
