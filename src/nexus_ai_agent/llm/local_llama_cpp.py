from __future__ import annotations

import asyncio
from pathlib import Path

from llama_cpp import Llama
from sentence_transformers import SentenceTransformer

from nexus_ai_agent.llm.provider import LLMProvider


class LocalLlamaCppProvider(LLMProvider):
    def __init__(self, model_path: str, n_ctx: int = 2048, n_gpu_layers: int = 0):
        self.model_path = model_path
        model_file = Path(model_path)
        if not model_file.exists():
            raise FileNotFoundError(
                f"GGUF model file not found at '{model_path}'. "
                "Set NEXUS_MODEL_PATH or place a model at models/model.gguf."
            )

        self._llm = Llama(
            model_path=str(model_file),
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
        )
        # Keep embedder separate to allow swapping later without affecting generation.
        self._embedder = SentenceTransformer("all-MiniLM-L6-v2")

    async def generate(self, prompt: str, system: str = "") -> str:
        full_prompt = prompt if not system else f"{system.strip()}\n\n{prompt}"

        def _run() -> str:
            result = self._llm(
                full_prompt,
                max_tokens=512,
                temperature=0.2,
                stop=[],
            )
            return (result["choices"][0]["text"] or "").strip()

        return await asyncio.to_thread(_run)

    async def embed(self, text: str) -> list[float]:
        def _run() -> list[float]:
            vec = self._embedder.encode(text, normalize_embeddings=True)
            return [float(x) for x in vec.tolist()]

        return await asyncio.to_thread(_run)

