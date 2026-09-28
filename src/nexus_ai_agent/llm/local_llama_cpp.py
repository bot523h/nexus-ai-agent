from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from nexus_ai_agent.llm.provider import LLMProvider


class LocalLlamaCppProvider(LLMProvider):
    def __init__(self, model_path: str, n_ctx: int = 2048, n_gpu_layers: int = 0):
        self._native_lock = threading.Lock()
        self.model_path = model_path
        model_file = Path(model_path)
        if not model_file.exists():
            raise FileNotFoundError(
                f"GGUF model file not found at '{model_path}'. "
                "Set NEXUS_MODEL_PATH or place a model at models/model.gguf."
            )

        from llama_cpp import Llama

        self._model: Any = Llama(
            model_path=str(model_file),
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
        )

    async def generate(self, prompt: str, system: str = "") -> str:
        formatted_prompt = f"<|system|>{system}<|user|>{prompt}<|assistant|>"

        def _run() -> str:
            # This mutex protects the native model, not request admission.
            # The gateway bounds the number of already-admitted worker calls.
            with self._native_lock:
                result = self._model(
                    formatted_prompt,
                    max_tokens=512,
                )
                return (result["choices"][0]["text"] or "").strip()

        return await asyncio.to_thread(_run)

    async def embed(self, text: str) -> list[float]:
        def _embed() -> list[float]:
            # Import/download/model construction can block as well as encode.
            # Keep the entire cold path off the authority event loop, and
            # initialize the cached model once under concurrent first use.
            with self._native_lock:
                if not hasattr(self, "_st"):
                    try:
                        from sentence_transformers import SentenceTransformer
                    except ImportError as exc:
                        raise ImportError(
                            "LocalLlamaCppProvider.embed needs the "
                            "'sentence-transformers' package "
                            "(pip install sentence-transformers), or use "
                            "LocalLlamaServerProvider with "
                            "`llama-server --embedding` instead."
                        ) from exc

                    self._st = SentenceTransformer("all-MiniLM-L6-v2")
                return self._st.encode(text).tolist()

        return await asyncio.to_thread(_embed)
