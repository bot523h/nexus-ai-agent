from __future__ import annotations

import asyncio
from pathlib import Path

from llama_cpp import Llama

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

        self._model = Llama(
            model_path=str(model_file),
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
        )

    async def generate(self, prompt: str, system: str = "") -> str:
        formatted_prompt = f"<|system|>{system}<|user|>{prompt}<|assistant|>"

        def _run() -> str:
            result = self._model(
                formatted_prompt,
                max_tokens=512,
            )
            return (result["choices"][0]["text"] or "").strip()

        return await asyncio.to_thread(_run)

    async def embed(self, text: str) -> list[float]:
        if not hasattr(self, "_st"):
            from sentence_transformers import SentenceTransformer

            self._st = SentenceTransformer("all-MiniLM-L6-v2")

        def _run() -> list[float]:
            return self._st.encode(text).tolist()

        return await asyncio.to_thread(_run)
