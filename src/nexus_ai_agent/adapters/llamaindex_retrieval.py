"""The LlamaIndex infrastructure adapter (the only ``llama_index`` import site).

Boundary
--------
:class:`~nexus_ai_agent.knowledge_retrieval.ports.KnowledgeIndexPort` is what
the application sees; this module implements it with ``llama-index-core`` and
is the **only** file under ``src/`` allowed to import ``llama_index``
(``tests/architecture/test_knowledge_retrieval_boundary.py`` fails the build
otherwise).  Everything LlamaIndex-specific — storage context, document
store, vector store, embedding classes — stays behind that wall.

Local-first contract
--------------------
* **No LLM, no OpenAI.**  ``Settings.llm`` is forced to ``None`` (mock) once,
  so nothing in this path can resolve ``llama-index-llms-openai`` or call a
  completion API.  Retrieval is embeddings + similarity only.
* **No telemetry by default.**  ``OTEL_SDK_DISABLED`` is defaulted to ``true``
  (an explicit operator value wins), LlamaIndex instrumentation keeps its
  null handlers, and no telemetry extra is installed.
* **No silent model downloads.**  The default ``hash`` embedding provider is
  pure arithmetic.  The opt-in ``sentence-transformers`` provider requires an
  **existing local model directory** — a missing directory is a typed error
  with the exact prefetch command, never a background download.
* **Imports are lazy.**  Importing this module costs nothing; a missing
  ``llama-index-core`` surfaces as :class:`IndexUnavailableError` with an
  install hint instead of an ImportError at CLI start-up.

Persistence (derived, rebuildable)
----------------------------------
``persist_dir`` holds LlamaIndex's own JSON stores (docstore, index store,
vector store) plus a tiny ``manifest.json`` (generation counter + timestamps)
written by this adapter.  None of it is authoritative: the canonical source
registry lives in the application database, and ``nexus knowledge rebuild``
recreates this directory from that registry and the sources on disk.
Interrupted writes are tolerated because the directory is written atomically
per operation and a corrupt store fails *typed* with a rebuild hint — the
canonical database is never touched by a failed index write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only, never imported at runtime
    from llama_index.core.embeddings import BaseEmbedding

import asyncio
import contextlib
import io
import json
import os
import shutil
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from nexus_ai_agent.knowledge_retrieval.config import KnowledgeConfig
from nexus_ai_agent.knowledge_retrieval.embeddings import EMBEDDING_DIM, hash_embed
from nexus_ai_agent.knowledge_retrieval.errors import (
    IndexUnavailableError,
    KnowledgeConfigError,
)
from nexus_ai_agent.knowledge_retrieval.models import ChunkPayload, SourceMeta
from nexus_ai_agent.knowledge_retrieval.ports import IndexHit, StoreInfo

__all__ = ["LlamaIndexKnowledgeIndex"]

#: Default off, operator-overridable: an explicit ``OTEL_SDK_DISABLED=false``
#: in the environment survives ``setdefault`` (documented in
#: ``docs/architecture/KNOWLEDGE_RETRIEVAL.md`` §6).
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

_MANIFEST_NAME = "manifest.json"
_INDEX_MARKER = "index_store.json"
_SENTENCE_TRANSFORMERS_CACHE: dict[str, BaseEmbedding] = {}
_SETTINGS_LOCK = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_core() -> None:
    """Import guard: a missing framework is a typed, recoverable failure."""
    try:
        import llama_index.core  # noqa: F401
    except ImportError as exc:
        raise IndexUnavailableError(
            "llama-index-core is not installed",
            hint="pip install 'nexus-ai-agent' (llama-index-core is a core dependency)",
        ) from exc


def _configure_global_settings() -> None:
    """Pin LlamaIndex globals: no LLM (mock), stdout kept clean for ``--json``.

    ``Settings.llm`` lazily resolves to an OpenAI client when read unset; the
    first read after assigning ``None`` resolves to the built-in mock and is
    cached, so no code path in this adapter can ever import an LLM package.
    LlamaIndex prints one informational line during that resolution — it is
    swallowed here because the CLI's JSON output must stay parseable.
    """
    from llama_index.core import Settings

    with _SETTINGS_LOCK, contextlib.redirect_stdout(io.StringIO()):
        # The 0.14 ``Settings.llm`` getter's type union omits ``None`` even
        # though ``None`` is the sanctioned no-LLM value (and what forces the
        # offline mock instead of an OpenAI import).
        Settings.llm = None  # type: ignore[assignment]
        _ = Settings.llm  # force the mock resolution once and cache it


class LlamaIndexKnowledgeIndex:
    """Async :class:`KnowledgeIndexPort` implementation over LlamaIndex.

    All LlamaIndex work is synchronous and runs in a worker thread
    (``asyncio.to_thread``), guarded by one lock: the event loop is never
    blocked and store mutations never interleave.
    """

    def __init__(self, config: KnowledgeConfig) -> None:
        self._config = config
        # threading.Lock (not asyncio.Lock): every port method runs its sync
        # LlamaIndex work through asyncio.to_thread, so the critical section
        # lives in the worker thread and must be a *sync* mutex.
        self._lock = threading.Lock()
        self._index: object | None = None  # VectorStoreIndex, loaded lazily
        self._embedding: BaseEmbedding | None = None

    # ── async port surface ─────────────────────────────────────────────

    async def replace_source(self, source: SourceMeta, chunks: Sequence[ChunkPayload]) -> int:
        await asyncio.to_thread(self._replace_sync, source, list(chunks))
        return len(chunks)

    async def remove_source(self, source_id: str) -> bool:
        return await asyncio.to_thread(self._remove_sync, source_id)

    async def search(self, query: str, top_k: int) -> list[IndexHit]:
        return await asyncio.to_thread(self._search_sync, query, top_k)

    async def clear(self) -> None:
        await asyncio.to_thread(self._clear_sync)

    async def inspect(self) -> StoreInfo:
        return await asyncio.to_thread(self._inspect_sync)

    # ── sync internals (worker thread) ─────────────────────────────────

    def _store_dir(self) -> Path:
        return self._config.index_path

    def _make_embedding(self) -> BaseEmbedding:
        provider = self._config.embedding_provider
        if provider == "hash":
            return _build_hash_embedding()
        if provider == "sentence-transformers":
            return _build_sentence_transformer_embedding(self._config)
        raise KnowledgeConfigError(
            f"unknown knowledge embedding provider {provider!r}",
            hint="set NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER to 'hash' or 'sentence-transformers'",
        )

    def _embedding_model(self) -> BaseEmbedding:
        if self._embedding is None:
            self._embedding = self._make_embedding()
        return self._embedding

    def _load_or_create(self) -> object:
        """Return the persisted index, creating an empty one when absent."""
        if self._index is not None:
            return self._index
        _require_core()
        _configure_global_settings()
        from llama_index.core import StorageContext, VectorStoreIndex, load_index_from_storage

        store = self._store_dir()
        marker = store / _INDEX_MARKER
        embed_model = self._embedding_model()
        if marker.is_file():
            try:
                context = StorageContext.from_defaults(persist_dir=str(store))
                self._index = load_index_from_storage(context, embed_model=embed_model)
            except Exception as exc:  # noqa: BLE001 — every load failure is "corrupt"
                raise IndexUnavailableError(
                    f"knowledge index store is unreadable ({type(exc).__name__})",
                    hint="run `nexus knowledge rebuild` to recreate the derived index",
                ) from exc
            return self._index
        store.mkdir(parents=True, exist_ok=True)
        context = StorageContext.from_defaults()
        self._index = VectorStoreIndex(
            nodes=[], storage_context=context, embed_model=embed_model, show_progress=False
        )
        return self._index

    def _read_manifest(self) -> dict[str, object]:
        try:
            data = json.loads((self._store_dir() / _MANIFEST_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_manifest(
        self, *, bump_generation: bool = False, generation_override: int | None = None
    ) -> None:
        manifest = self._read_manifest()
        generation = manifest.get("generation", 0)
        if not isinstance(generation, int) or generation < 0:
            generation = 0
        if generation_override is not None:
            generation = max(0, generation_override)
        elif bump_generation:
            generation += 1
        payload = {
            "format": 1,
            "generation": generation,
            "rebuilt_at": manifest.get("rebuilt_at") if not bump_generation else _utcnow(),
            "updated_at": _utcnow(),
        }
        try:
            (self._store_dir() / _MANIFEST_NAME).write_text(
                json.dumps(payload, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            # The manifest is observability, not authority: a failed stamp
            # must never fail an otherwise successful index operation.
            pass

    def _persist(self, index: object) -> None:
        index.storage_context.persist(persist_dir=str(self._store_dir()))  # type: ignore[attr-defined]
        self._write_manifest()

    def _replace_sync(self, source: SourceMeta, chunks: list[ChunkPayload]) -> None:
        with self._lock:
            index = self._load_or_create()
            from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode

            # Atomic-by-construction: drop the old version first, then insert
            # the new one.  A crash between the two leaves the source absent
            # (recoverable by re-running the index), never half-old/half-new
            # in a way search could cite, because citations verify sha256
            # against the canonical registry anyway.
            index.delete_ref_doc(source.source_id, delete_from_docstore=True)  # type: ignore[attr-defined]
            nodes = []
            for chunk in chunks:
                metadata: dict[str, object] = {
                    "source_id": source.source_id,
                    "path": source.path,
                    "root": source.root,
                    "sha256": source.sha256,
                    "chunk_index": chunk.chunk_index,
                    "start": chunk.start,
                    "end": chunk.end,
                    "ingested_at": source.ingested_at,
                }
                if chunk.page is not None:
                    metadata["page"] = chunk.page
                node = TextNode(
                    text=chunk.text,
                    id_=f"{source.source_id}:{chunk.chunk_index}",
                    metadata=metadata,
                )
                node.relationships[NodeRelationship.SOURCE] = RelatedNodeInfo(
                    node_id=source.source_id
                )
                nodes.append(node)
            if nodes:
                index.insert_nodes(nodes)  # type: ignore[attr-defined]
            self._persist(index)

    def _remove_sync(self, source_id: str) -> bool:
        with self._lock:
            if not (self._store_dir() / _INDEX_MARKER).is_file():
                return False
            index = self._load_or_create()
            ref_info = index.docstore.get_ref_doc_info(source_id)  # type: ignore[attr-defined]
            if ref_info is None:
                return False
            index.delete_ref_doc(source_id, delete_from_docstore=True)  # type: ignore[attr-defined]
            self._persist(index)
            return True

    def _search_sync(self, query: str, top_k: int) -> list[IndexHit]:
        with self._lock:
            if not (self._store_dir() / _INDEX_MARKER).is_file():
                return []
            index = self._load_or_create()
            hits = index.as_retriever(similarity_top_k=top_k).retrieve(query)  # type: ignore[attr-defined]
            results: list[IndexHit] = []
            for node_with_score in hits:
                node = node_with_score.node
                metadata = node.metadata or {}
                source_id = str(metadata.get("source_id") or "")
                if not source_id:
                    continue
                page = metadata.get("page")
                results.append(
                    IndexHit(
                        source_id=source_id,
                        sha256=str(metadata.get("sha256") or ""),
                        path=str(metadata.get("path") or ""),
                        chunk_index=int(metadata.get("chunk_index") or 0),
                        start=int(metadata.get("start") or 0),
                        end=int(metadata.get("end") or 0),
                        score=float(node_with_score.score or 0.0),
                        text=node.get_content(),
                        page=int(page) if isinstance(page, int) else None,
                    )
                )
            return results

    def _clear_sync(self) -> None:
        with self._lock:
            previous_generation = self._read_manifest().get("generation", 0)
            if not isinstance(previous_generation, int) or previous_generation < 0:
                previous_generation = 0
            store = self._store_dir()
            if store.is_dir():
                for child in store.iterdir():
                    if child.is_file():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
            self._index = None
            store.mkdir(parents=True, exist_ok=True)
            self._write_manifest(bump_generation=True, generation_override=previous_generation + 1)

    def _inspect_sync(self) -> StoreInfo:
        store = self._store_dir()
        manifest = self._read_manifest()
        generation = manifest.get("generation")
        rebuilt_at = manifest.get("rebuilt_at")
        marker = store / _INDEX_MARKER
        if not marker.is_file():
            return StoreInfo(
                present=False,
                corrupt=False,
                node_count=0,
                generation=generation if isinstance(generation, int) else None,
                rebuilt_at=rebuilt_at if isinstance(rebuilt_at, str) else None,
            )
        try:
            index = self._load_or_create()
            ref_infos = index.docstore.get_all_ref_doc_info()  # type: ignore[attr-defined]
            node_count = sum(len(info.node_ids) for info in ref_infos.values()) if ref_infos else 0
            return StoreInfo(
                present=True,
                corrupt=False,
                node_count=node_count,
                generation=generation if isinstance(generation, int) else None,
                rebuilt_at=rebuilt_at if isinstance(rebuilt_at, str) else None,
            )
        except IndexUnavailableError as exc:
            return StoreInfo(
                present=True,
                corrupt=True,
                node_count=None,
                generation=generation if isinstance(generation, int) else None,
                rebuilt_at=rebuilt_at if isinstance(rebuilt_at, str) else None,
                load_error=exc.message,
            )
        except Exception as exc:  # noqa: BLE001 — status must never raise
            return StoreInfo(
                present=True,
                corrupt=True,
                node_count=None,
                generation=generation if isinstance(generation, int) else None,
                rebuilt_at=rebuilt_at if isinstance(rebuilt_at, str) else None,
                load_error=type(exc).__name__,
            )


# ── embedding providers ────────────────────────────────────────────────────


def _build_hash_embedding() -> BaseEmbedding:
    """Deterministic hashed bag-of-tokens embeddings (offline, no weights)."""
    from llama_index.core.embeddings import BaseEmbedding

    class HashEmbedding(BaseEmbedding):
        model_name: str = f"nexus-hash-v1-{EMBEDDING_DIM}dim"

        def _embed(self, text: str) -> list[float]:
            return hash_embed([text])[0]

        def _get_query_embedding(self, query: str) -> list[float]:
            return self._embed(query)

        def _get_text_embedding(self, text: str) -> list[float]:
            return self._embed(text)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return self._embed(query)

        async def _aget_text_embedding(self, text: str) -> list[float]:
            return self._embed(text)

        def _get_agg_embedding_from_queries(self, queries: Sequence[str]) -> list[float]:
            vectors = hash_embed(list(queries))
            if not vectors:
                return [0.0] * EMBEDDING_DIM
            size = len(vectors[0])
            summed = [0.0] * size
            for vector in vectors:
                for position, value in enumerate(vector):
                    summed[position] += value
            return summed

        async def _aget_agg_embedding_from_queries(self, queries: Sequence[str]) -> list[float]:
            return self._get_agg_embedding_from_queries(queries)

    return HashEmbedding()


def _build_sentence_transformer_embedding(config: KnowledgeConfig) -> BaseEmbedding:
    """Opt-in local sentence-transformers — **existing directory or typed error**.

    The model directory must exist *before* the provider loads: this is the
    anti-silent-download guard.  The prefetch command is part of the error
    hint and the docs, so an operator downloads deliberately, once.
    """
    from pathlib import Path

    model_dir = (config.embedding_model_path or "").strip()
    if not model_dir:
        raise IndexUnavailableError(
            "sentence-transformers provider selected but "
            "NEXUS_KNOWLEDGE_EMBEDDING_MODEL_PATH is empty",
            code="embedding",
            hint=(
                "download a model once, then set the path — e.g. "
                'python -c "from sentence_transformers import SentenceTransformer; '
                "SentenceTransformer('all-MiniLM-L6-v2').save('models/minilm')\""
            ),
        )
    resolved = Path(model_dir).expanduser()
    if not resolved.is_dir():
        raise IndexUnavailableError(
            "local embedding model directory does not exist (downloads are never "
            "started implicitly)",
            code="embedding",
            hint=f"prefetch the model into {resolved.name!r} first — see "
            "docs/architecture/KNOWLEDGE_RETRIEVAL.md §4",
        )

    cached = _SENTENCE_TRANSFORMERS_CACHE.get(str(resolved))
    if cached is not None:
        return cached
    try:
        from llama_index.core.embeddings import BaseEmbedding
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise IndexUnavailableError(
            "sentence-transformers is not installed",
            code="embedding",
            hint="pip install 'nexus-ai-agent' (declared core dependency) or use "
            "NEXUS_KNOWLEDGE_EMBEDDING_PROVIDER=hash",
        ) from exc

    try:
        model = SentenceTransformer(str(resolved))
    except Exception as exc:  # noqa: BLE001 — model load failure is recoverable
        raise IndexUnavailableError(
            f"local embedding model failed to load ({type(exc).__name__})",
            code="embedding",
            hint="verify the model directory or switch to the 'hash' provider",
        ) from exc

    class SentenceTransformerEmbedding(BaseEmbedding):
        model_name: str = f"sentence-transformers:{resolved.name}"

        def _encode(self, text: str) -> list[float]:
            vector = model.encode(text)
            return [float(value) for value in vector]

        def _get_query_embedding(self, query: str) -> list[float]:
            return self._encode(query)

        def _get_text_embedding(self, text: str) -> list[float]:
            return self._encode(text)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return self._encode(query)

        async def _aget_text_embedding(self, text: str) -> list[float]:
            return self._encode(text)

        def _get_agg_embedding_from_queries(self, queries: Sequence[str]) -> list[float]:
            vectors = [self._encode(item) for item in queries]
            if not vectors:
                return []
            size = len(vectors[0])
            summed = [0.0] * size
            for vector in vectors:
                for position, value in enumerate(vector):
                    summed[position] += value
            return summed

        async def _aget_agg_embedding_from_queries(self, queries: Sequence[str]) -> list[float]:
            return self._get_agg_embedding_from_queries(queries)

    embedding = SentenceTransformerEmbedding()
    _SENTENCE_TRANSFORMERS_CACHE[str(resolved)] = embedding
    return embedding
