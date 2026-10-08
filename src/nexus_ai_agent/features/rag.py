"""Per-user document RAG: recursive chunking + hybrid retrieval (task-127).

Architecture
------------
This module is the **adapter** half; the algorithms live in
:mod:`nexus_ai_agent.features.rag_core` (pure stdlib). Splitting them is what
lets the retrieval logic be unit-tested — and reasoned about — without
``chromadb``, ``sentence-transformers`` (→ torch) or ``flashrank`` installed:

```
        ┌──────────────── rag.py (this file) ────────────────┐
        │  AdvancedRAGEngine                                 │
        │    ├── chunking      → rag_core.chunk_text         │
        │    ├── lexical lane  → rag_core.BM25               │
        │    ├── dense lane    → chromadb  (lazy, optional)  │
        │    ├── fusion        → rag_core.rrf                │
        │    └── rerank        → flashrank (lazy, optional)  │
        └────────────────────────────────────────────────────┘
```

Operational rules
-----------------
* **Lazy heavy imports.** Importing this module (the bot surface does) costs
  nothing; ``chromadb``/``flashrank``/the embedding model load on first use.
* **Injectable collaborators.** ``client``, ``embedding_fn`` and ``ranker`` are
  constructor arguments, so tests drive the engine with fakes and production
  uses the defaults.
* **Never block the event loop.** Every Chroma/model call runs in a worker
  thread via :func:`asyncio.to_thread`.
* **Fail loud on a missing stack.** If the vector backend is unavailable the
  engine raises :class:`RAGUnavailable` with an install hint instead of
  silently storing documents nowhere.
* **Idempotent ingestion.** Re-adding the same ``file_id`` replaces its chunks
  instead of duplicating them.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.features.rag_core import (
    BM25,
    EvalCase,
    chunk_text,
    evaluate_retrieval,
    reciprocal_rank_fusion,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AdvancedRAGEngine",
    "DocumentInfo",
    "RAGUnavailable",
    "RetrievedChunk",
    "chunk_text",
]

#: Chunking window handed to the embedding model (tokens, chars ≈ tokens × 4).
CHUNK_TOKENS = 384
#: Overlap between consecutive chunks (10–20 % per the retrieval design).
CHUNK_OVERLAP_RATIO = 0.15
#: Upper bound for one document (≈ a few hundred PDF pages of text).
MAX_DOCUMENT_CHARS = 1_000_000
#: Upper bound of distinct documents kept per user.
MAX_DOCUMENTS_PER_USER = 20
#: Chunks handed to the LLM after (re-)ranking.
TOP_CONTEXT = 3
#: Candidate pool per retrieval lane before fusion.
CANDIDATES_PER_LANE = 20

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
RERANK_MODEL = "ms-marco-MiniLM-L-12-v2"


class RAGUnavailable(RuntimeError):
    """The vector stack is missing or unusable; ingestion/query cannot proceed."""


class Embedder(Protocol):
    """Anything that turns texts into vectors (SentenceTransformer, fakes…)."""

    def __call__(self, texts: list[str]) -> Any: ...  # pragma: no cover - protocol


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    """One uploaded document as seen by ``/docs``."""

    file_id: str
    file_name: str
    chunks: int
    added_at: str  # ISO-8601 UTC, "" when unknown (legacy rows)


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One hit: the text, its provenance, and the fused score."""

    file_id: str
    file_name: str
    text: str
    score: float
    chunk_id: str = ""


def _collection_name(user_id: int) -> str:
    return f"user_docs_{int(user_id)}"


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
    """Chroma only stores scalar metadata values."""
    cleaned: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if isinstance(value, bool | int | float):
            cleaned[str(key)] = value
        elif value is not None:
            cleaned[str(key)] = str(value)[:500]
    return cleaned


def _distance_to_score(distance: Any) -> float:
    """Chroma returns distances (smaller = closer): map to a similarity score."""
    try:
        value = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return 1.0 / (1.0 + max(0.0, value))


class AdvancedRAGEngine:
    """Per-user retrieval over uploaded documents (hybrid lexical + dense).

    Retrieval runs two lanes and fuses them with weighted reciprocal-rank
    fusion: BM25 over the exact words (great for identifiers, Persian
    morphology, rare terms) and a dense lane over embeddings (great for
    paraphrases). When only one lane is available the other is skipped instead
    of failing — a Chroma-less deployment still gets lexical retrieval.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        embedding_fn: Any | None = None,
        ranker: Any | None = None,
        rerank: bool = True,
        chunk_tokens: int = CHUNK_TOKENS,
        overlap_ratio: float = CHUNK_OVERLAP_RATIO,
    ) -> None:
        self._client = client
        self._embedding_fn = embedding_fn
        self._ranker = ranker
        self._rerank = rerank
        self._chunk_tokens = chunk_tokens
        self._overlap_ratio = overlap_ratio
        self._collections: dict[int, Any] = {}
        self._lexical: dict[int, BM25] = {}
        self._texts: dict[int, dict[str, str]] = {}  # user → chunk_id → text
        self._documents: dict[int, dict[str, DocumentInfo]] = {}  # user → file_id → info
        self._hydrated: set[int] = set()

    # ── lazy collaborators ────────────────────────────────────────────

    def _default_client(self) -> Any:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RAGUnavailable(
                "chromadb is not installed; install it with "
                "pip install 'nexus-ai-agent[rag]' to enable document RAG"
            ) from exc
        settings = get_settings()
        os.makedirs(settings.chroma_db_path, exist_ok=True)
        return chromadb.PersistentClient(path=settings.chroma_db_path)

    def _default_embedding_fn(self) -> Any:
        from chromadb.utils import embedding_functions

        return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)

    def _default_ranker(self) -> Any | None:
        try:
            from flashrank import Ranker
        except Exception:  # noqa: BLE001 - a missing reranker is not fatal
            logger.info("flashrank unavailable; re-ranking disabled")
            return None
        try:
            return Ranker(model_name=RERANK_MODEL, cache_dir="data/flashrank")
        except Exception as exc:  # noqa: BLE001 - model download can fail offline
            logger.warning("flashrank init failed (%s); re-ranking disabled", exc)
            return None

    def client(self) -> Any:
        """The (lazily created) vector-store client."""
        if self._client is None:
            self._client = self._default_client()
        return self._client

    def embedding_fn(self) -> Any | None:
        """The embedding function, or ``None`` when the dense lane is disabled."""
        if self._embedding_fn is None:
            try:
                self._embedding_fn = self._default_embedding_fn()
            except Exception as exc:  # noqa: BLE001 - model load can fail offline
                logger.warning("embedding backend unavailable (%s); lexical-only RAG", exc)
                return None
        return self._embedding_fn

    def ranker(self) -> Any | None:
        """The cross-encoder re-ranker, or ``None`` when unavailable/disabled."""
        if not self._rerank:
            return None
        if self._ranker is None:
            self._ranker = self._default_ranker()
        return self._ranker

    # ── store access ──────────────────────────────────────────────────

    def _collection(self, user_id: int) -> Any:
        cached = self._collections.get(user_id)
        if cached is not None:
            return cached
        collection = self.client().get_or_create_collection(
            name=_collection_name(user_id),
            embedding_function=self.embedding_fn(),
        )
        self._collections[user_id] = collection
        return collection

    def _lexical_index(self, user_id: int) -> BM25:
        index = self._lexical.get(user_id)
        if index is None:
            index = BM25()
            self._lexical[user_id] = index
        return index

    def _hydrate(self, user_id: int) -> None:
        """Rebuild the in-process lexical index from the vector store."""
        if user_id in self._hydrated:
            return
        lexical = self._lexical_index(user_id)
        texts = self._texts.setdefault(user_id, {})
        documents = self._documents.setdefault(user_id, {})
        try:
            payload = self._collection(user_id).get()
        except Exception as exc:  # noqa: BLE001 - a cold store is not fatal
            logger.warning("could not hydrate user %s from the vector store: %s", user_id, exc)
            self._hydrated.add(user_id)
            return

        ids = list(payload.get("ids") or [])
        docs = list(payload.get("documents") or [])
        metadatas = list(payload.get("metadatas") or [])
        for chunk_id, text, metadata in zip(ids, docs, metadatas, strict=False):
            if not isinstance(text, str) or not text.strip():
                continue
            texts[str(chunk_id)] = text
            lexical.add(str(chunk_id), text)
            metadata = metadata if isinstance(metadata, dict) else {}
            file_id = str(metadata.get("file_id", "") or "")
            if not file_id:
                continue
            info = documents.get(file_id)
            documents[file_id] = DocumentInfo(
                file_id=file_id,
                file_name=str(metadata.get("file_name") or file_id),
                chunks=(info.chunks if info else 0) + 1,
                added_at=str(metadata.get("added_at") or (info.added_at if info else "")),
            )
        self._hydrated.add(user_id)

    # ── ingestion ─────────────────────────────────────────────────────

    async def add_document(self, user_id: int, text: str, metadata: dict[str, Any]) -> list[str]:
        """Chunk, embed and store one document. Returns the chunk ids."""
        cleaned = (text or "").strip()
        if not cleaned:
            return []
        if len(cleaned) > MAX_DOCUMENT_CHARS:
            raise ValueError(f"document too large: {len(cleaned)} chars > {MAX_DOCUMENT_CHARS}")

        file_id = str(metadata.get("file_id") or "")
        if file_id:
            await self.delete_document(user_id, file_id)
            documents = self._documents.setdefault(user_id, {})
            if file_id not in documents and len(documents) >= MAX_DOCUMENTS_PER_USER:
                raise ValueError(
                    f"too many documents for this user (limit {MAX_DOCUMENTS_PER_USER})"
                )

        chunks = chunk_text(
            cleaned,
            chunk_tokens=self._chunk_tokens,
            overlap_ratio=self._overlap_ratio,
        )
        if not chunks:
            return []

        stamp = datetime.now(timezone.utc).isoformat()
        ids = [f"{file_id or 'doc'}:{chunk.index}" for chunk in chunks]
        metadatas = [
            _clean_metadata(
                {
                    **metadata,
                    "chunk_index": chunk.index,
                    "start": chunk.start,
                    "end": chunk.end,
                    "added_at": stamp,
                }
            )
            for chunk in chunks
        ]

        def _write() -> None:
            self._collection(user_id).add(
                documents=[chunk.text for chunk in chunks],
                metadatas=metadatas,
                ids=ids,
            )

        await asyncio.to_thread(_write)

        lexical = self._lexical_index(user_id)
        texts = self._texts.setdefault(user_id, {})
        for chunk_id, chunk in zip(ids, chunks, strict=False):
            texts[chunk_id] = chunk.text
            lexical.add(chunk_id, chunk.text)
        self._documents.setdefault(user_id, {})[file_id or ids[0]] = DocumentInfo(
            file_id=file_id or ids[0],
            file_name=str(metadata.get("file_name") or file_id or "document"),
            chunks=len(chunks),
            added_at=stamp,
        )
        self._hydrated.add(user_id)
        logger.info("indexed %s chunks for user %s (file %s)", len(chunks), user_id, file_id)
        return ids

    async def delete_document(self, user_id: int, file_id: str) -> bool:
        """Drop every chunk of ``file_id``. Returns ``True`` when it existed."""
        self._hydrate(user_id)
        documents = self._documents.get(user_id, {})
        if file_id not in documents:
            return False

        def _delete() -> None:
            self._collection(user_id).delete(where={"file_id": file_id})

        await asyncio.to_thread(_delete)
        documents.pop(file_id, None)
        self._reindex_lexical(user_id)
        return True

    def _reindex_lexical(self, user_id: int) -> None:
        """Rebuild the lexical index from the store after a deletion."""
        self._lexical[user_id] = BM25()
        self._texts[user_id] = {}
        self._hydrated.discard(user_id)
        self._hydrate(user_id)

    async def list_documents(self, user_id: int) -> list[DocumentInfo]:
        """Every document owned by ``user_id``, newest first."""
        self._hydrate(user_id)
        documents = list(self._documents.get(user_id, {}).values())
        documents.sort(key=lambda item: item.added_at, reverse=True)
        return documents

    # ── retrieval ─────────────────────────────────────────────────────

    async def retrieve(
        self, user_id: int, question: str, top_k: int = TOP_CONTEXT
    ) -> list[RetrievedChunk]:
        """Hybrid retrieval: BM25 ⊕ dense, fused with RRF, optionally re-ranked."""
        query = (question or "").strip()
        if not query:
            return []
        self._hydrate(user_id)
        lexical = self._lexical.get(user_id)
        if lexical is None or len(lexical) == 0:
            return []

        pool = max(top_k * 4, CANDIDATES_PER_LANE)
        lexical_hits = lexical.search(query, pool)
        dense_hits = await self._dense_search(user_id, query, pool)

        rankings = [hits for hits in (lexical_hits, dense_hits) if hits]
        if not rankings:
            return []
        fused = reciprocal_rank_fusion(rankings, limit=max(top_k * 3, top_k))

        texts = self._texts.get(user_id, {})
        documents = self._documents.get(user_id, {})
        chunks: list[RetrievedChunk] = []
        for item in fused:
            text = texts.get(item.doc_id)
            if not text:
                continue
            file_id = item.doc_id.split(":", 1)[0]
            info = documents.get(file_id)
            chunks.append(
                RetrievedChunk(
                    file_id=file_id,
                    file_name=info.file_name if info is not None else file_id,
                    text=text,
                    score=item.score,
                    chunk_id=item.doc_id,
                )
            )

        reranked = await self._rerank_chunks(query, chunks, top_k)
        return reranked[:top_k]

    async def _dense_search(self, user_id: int, query: str, pool: int) -> list[Any]:
        """The vector lane; empty when embeddings are unavailable."""
        if self.embedding_fn() is None:
            return []

        def _query() -> Any:
            return self._collection(user_id).query(query_texts=[query], n_results=pool)

        try:
            payload = await asyncio.to_thread(_query)
        except Exception as exc:  # noqa: BLE001 - a cold/empty collection is normal
            logger.debug("dense lane unavailable for user %s: %s", user_id, exc)
            return []

        ids = (payload or {}).get("ids") or [[]]
        distances = (payload or {}).get("distances") or [[]]
        from nexus_ai_agent.features.rag_core import ScoredDoc

        hits: list[Any] = []
        for doc_id, distance in zip(ids[0], distances[0], strict=False):
            hits.append(ScoredDoc(doc_id=str(doc_id), score=_distance_to_score(distance)))
        hits.sort(key=lambda item: -item.score)
        return hits

    async def _rerank_chunks(
        self, query: str, chunks: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        """Cross-encoder re-ranking; a transparent no-op when unavailable."""
        ranker = self.ranker()
        if ranker is None or len(chunks) <= 1:
            return chunks

        def _rerank() -> list[RetrievedChunk]:
            passages = [
                {"id": index, "text": chunk.text, "meta": {"file_id": chunk.file_id}}
                for index, chunk in enumerate(chunks)
            ]
            try:
                from flashrank import RerankRequest

                results = ranker.rerank(RerankRequest(query=query, passages=passages))
            except Exception as exc:  # noqa: BLE001 - never fail a query for rerank
                logger.warning("re-ranking failed (%s); keeping fused order", exc)
                return chunks
            ordered: list[RetrievedChunk] = []
            for result in results:
                index = int(result.get("id", -1))
                if 0 <= index < len(chunks):
                    ordered.append(chunks[index])
            return ordered or chunks

        return await asyncio.to_thread(_rerank)

    async def query(self, user_id: int, question: str, top_k: int = TOP_CONTEXT) -> str:
        """Telegram-facing query: returns the joined context (legacy contract)."""
        chunks = await self.retrieve(user_id, question, top_k)
        if not chunks:
            return "No relevant documents found."
        return "\n---\n".join(chunk.text for chunk in chunks)

    async def recall_report(
        self,
        user_id: int,
        cases: Sequence[EvalCase | tuple[str, Sequence[str]]],
        *,
        k: int = 5,
    ) -> dict[str, Any]:
        """``recall@k`` of this user's index against labelled cases.

        Thin adapter over :func:`rag_core.evaluate_retrieval`: the queries are
        answered once (asynchronously) and the harness scores the ordering, so
        the production engine is measured with exactly the same code path the
        unit tests use.
        """
        normalised = [
            case
            if isinstance(case, EvalCase)
            else EvalCase(query=str(case[0]), relevant=tuple(case[1]))
            for case in cases
        ]
        answers: dict[str, list[str]] = {}
        for case in normalised:
            chunks = await self.retrieve(user_id, case.query, k)
            # Cases are labelled per *document*, and several chunks of one
            # document may be retrieved: collapse to unique file ids, first
            # (best) rank wins.
            answers[case.query] = list(dict.fromkeys(chunk.file_id for chunk in chunks))

        def _retrieve(query: str, limit: int) -> list[str]:
            return answers.get(query, [])[:limit]

        return evaluate_retrieval(normalised, _retrieve, k=k).as_dict()

    async def clear_memory(self, user_id: int) -> None:
        """Delete the user's whole index (store + in-process caches)."""

        def _drop() -> None:
            try:
                self.client().delete_collection(_collection_name(user_id))
            except Exception:  # noqa: BLE001 - an absent collection is success
                logger.debug("no collection to delete for user %s", user_id)

        await asyncio.to_thread(_drop)
        self._collections.pop(user_id, None)
        self._lexical.pop(user_id, None)
        self._texts.pop(user_id, None)
        self._documents.pop(user_id, None)
        self._hydrated.discard(user_id)
