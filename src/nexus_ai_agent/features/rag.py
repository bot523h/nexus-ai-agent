"""Per-user document RAG on ChromaDB (local embeddings + optional FlashRank).

Design notes (B8):

* Heavy dependencies (``chromadb``, ``sentence-transformers``/torch,
  ``flashrank``) are imported **lazily** inside the engine so importing this
  module — e.g. from the bot surface — stays cheap; the model is loaded only
  when an engine is actually built.
* Every collaborator is injectable (``client``, ``embedding_fn``,
  ``ranker``) so the engine can be unit-tested with an ephemeral client and a
  deterministic embedding function.
* Blocking Chroma/model calls run in a worker thread so the bot's event loop
  is never stalled by a query or an upload.
* Documents are keyed by ``file_id``; re-adding the same ``file_id``
  replaces the old chunks instead of duplicating them.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from nexus_ai_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "AdvancedRAGEngine",
    "DocumentInfo",
    "RetrievedChunk",
    "chunk_text",
]

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100
#: Upper bound for one document (≈ a few hundred PDF pages of text).
MAX_DOCUMENT_CHARS = 1_000_000
#: Upper bound of distinct documents kept per user.
MAX_DOCUMENTS_PER_USER = 20
#: Chunks handed to the LLM after (re-)ranking.
TOP_CONTEXT = 3
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
RERANK_MODEL = "ms-marco-MiniLM-L-12-v2"


@dataclass(frozen=True)
class DocumentInfo:
    """One uploaded document as seen by ``/docs``."""

    file_id: str
    file_name: str
    chunks: int
    added_at: str  # ISO-8601 UTC, "" when unknown (legacy rows)


@dataclass(frozen=True)
class RetrievedChunk:
    text: str
    file_id: str
    file_name: str
    score: float | None = None


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Fixed-size character windows with a small overlap; blank chunks are dropped."""
    if size <= 0:
        raise ValueError("size must be positive")
    overlap = max(0, min(overlap, size - 1))
    text = text.strip()
    if not text:
        return []
    step = size - overlap
    chunks: list[str] = []
    start = 0
    while True:
        end = start + size
        piece = text[start:end]
        if piece.strip():
            chunks.append(piece)
        if end >= len(text):
            break
        start += step
    return chunks


def _collection_name(user_id: int) -> str:
    return f"user_docs_{int(user_id)}"


def _clean_metadata(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
    """Chroma only stores scalar metadata values."""
    out: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if isinstance(value, bool | int | float):
            out[str(key)] = value
        elif value is not None:
            out[str(key)] = str(value)[:500]
    return out


def _default_client() -> Any:
    import chromadb

    settings = get_settings()
    os.makedirs(settings.chroma_db_path, exist_ok=True)
    return chromadb.PersistentClient(path=settings.chroma_db_path)


def _default_embedding_fn() -> Any:
    from chromadb.utils import embedding_functions

    # Local, small, free; the model is downloaded on first use.
    return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)


def _default_ranker() -> Any | None:
    try:
        from flashrank import Ranker

        return Ranker(model_name=RERANK_MODEL, cache_dir="data/flashrank")
    except Exception as e:  # noqa: BLE001 - optional accelerator
        logger.warning("FlashRank init failed, falling back to basic retrieval: %s", e)
        return None


class AdvancedRAGEngine:
    """Advanced RAG using ChromaDB, Sentence-Transformers, and FlashRank."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        embedding_fn: Any | None = None,
        ranker: Any | None | bool = None,
    ) -> None:
        """Build the engine.

        Pass ``ranker=False`` to disable re-ranking explicitly (``None`` means
        "try to load FlashRank").
        """
        self.client: Any = client if client is not None else _default_client()
        self.embedding_fn: Any = (
            embedding_fn if embedding_fn is not None else _default_embedding_fn()
        )
        self.ranker: Any | None
        if ranker is False:
            self.ranker = None
        elif ranker is None:
            self.ranker = _default_ranker()
        else:
            self.ranker = ranker

    # ── internals ─────────────────────────────────────────────────

    def _get_collection(self, user_id: int) -> Any:
        """Get or create a unique collection for each user."""
        return self.client.get_or_create_collection(
            name=_collection_name(user_id),
            embedding_function=self.embedding_fn,
        )

    def _list_sync(self, user_id: int) -> list[DocumentInfo]:
        collection = self._get_collection(user_id)
        result = collection.get(include=["metadatas"])
        grouped: dict[str, dict[str, Any]] = {}
        for meta in result.get("metadatas") or []:
            meta = meta or {}
            file_id = str(meta.get("file_id") or "doc")
            entry = grouped.setdefault(
                file_id,
                {
                    "file_name": str(meta.get("file_name") or file_id),
                    "chunks": 0,
                    "added_at": str(meta.get("added_at") or ""),
                },
            )
            entry["chunks"] += 1
        docs = [
            DocumentInfo(
                file_id=fid,
                file_name=e["file_name"],
                chunks=e["chunks"],
                added_at=e["added_at"],
            )
            for fid, e in grouped.items()
        ]
        return sorted(docs, key=lambda d: d.added_at)  # stable: insertion order on ties

    def _delete_sync(self, user_id: int, file_id: str) -> int:
        collection = self._get_collection(user_id)
        existing = collection.get(where={"file_id": file_id}, include=[])
        count = len(existing.get("ids") or [])
        if count:
            collection.delete(where={"file_id": file_id})
        return count

    def _add_sync(self, user_id: int, text: str, metadata: dict[str, Any]) -> int:
        chunks = chunk_text(text)
        if not chunks:
            return 0
        file_id = str(metadata.get("file_id") or "doc")
        collection = self._get_collection(user_id)

        # Replace, never duplicate, a re-uploaded document.
        existing = collection.get(where={"file_id": file_id}, include=[])
        if existing.get("ids"):
            collection.delete(where={"file_id": file_id})
        else:
            docs = self._list_sync(user_id)
            if len(docs) >= MAX_DOCUMENTS_PER_USER:
                raise ValueError(
                    f"document limit reached ({MAX_DOCUMENTS_PER_USER}); "
                    "delete one with /doc_delete"
                )

        base_meta = _clean_metadata(metadata)
        base_meta.setdefault("file_id", file_id)
        base_meta.setdefault("file_name", file_id)
        base_meta.setdefault("added_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        ids = [f"chunk_{file_id}_{i}" for i in range(len(chunks))]
        metadatas = [{**base_meta, "chunk_index": i} for i in range(len(chunks))]
        collection.add(documents=chunks, metadatas=metadatas, ids=ids)
        return len(chunks)

    def _retrieve_sync(
        self, user_id: int, question: str, top_k: int, file_id: str | None
    ) -> list[RetrievedChunk]:
        collection = self._get_collection(user_id)
        if collection.count() == 0:
            return []
        kwargs: dict[str, Any] = {"query_texts": [question], "n_results": max(1, top_k)}
        if file_id:
            kwargs["where"] = {"file_id": file_id}
        results = collection.query(**kwargs)
        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        if not documents:
            return []

        ordered: list[tuple[str, dict[str, Any], float | None]] = [
            (doc, metadatas[i] or {}, None) for i, doc in enumerate(documents)
        ]
        if self.ranker is not None:
            try:
                from flashrank import RerankRequest

                passages = [
                    {"id": i, "text": doc, "meta": meta} for i, (doc, meta, _) in enumerate(ordered)
                ]
                reranked = self.ranker.rerank(RerankRequest(query=question, passages=passages))
                ordered = [
                    (str(r["text"]), dict(r.get("meta") or {}), float(r.get("score") or 0.0))
                    for r in reranked
                ]
            except Exception as e:  # noqa: BLE001 - re-ranking is best effort
                logger.warning("FlashRank rerank failed, using vector order: %s", e)

        return [
            RetrievedChunk(
                text=doc,
                file_id=str(meta.get("file_id") or ""),
                file_name=str(meta.get("file_name") or meta.get("file_id") or ""),
                score=score,
            )
            for doc, meta, score in ordered[:TOP_CONTEXT]
        ]

    # ── public API (async, thread-offloaded) ───────────────────────

    async def add_document(self, user_id: int, text: str, metadata: dict[str, Any]) -> int:
        """Chunk and add a document; returns the number of chunks stored."""
        if len(text) > MAX_DOCUMENT_CHARS:
            raise ValueError(f"document too large ({len(text)} chars > {MAX_DOCUMENT_CHARS})")
        count = await asyncio.to_thread(self._add_sync, user_id, text, metadata)
        logger.info("Added %d chunks to collection for user %s", count, user_id)
        return count

    async def list_documents(self, user_id: int) -> list[DocumentInfo]:
        """Documents currently indexed for *user_id*."""
        return await asyncio.to_thread(self._list_sync, user_id)

    async def has_documents(self, user_id: int) -> bool:
        return bool(await self.list_documents(user_id))

    async def delete_document(self, user_id: int, file_id: str) -> int:
        """Remove one document; returns the number of chunks removed (0 = unknown id)."""
        return await asyncio.to_thread(self._delete_sync, user_id, file_id)

    async def retrieve(
        self, user_id: int, question: str, top_k: int = 10, *, file_id: str | None = None
    ) -> list[RetrievedChunk]:
        """Vector search + optional re-rank; the best :data:`TOP_CONTEXT` chunks."""
        return await asyncio.to_thread(self._retrieve_sync, user_id, question, top_k, file_id)

    async def query(self, user_id: int, question: str, top_k: int = 10) -> str:
        """Query, re-rank, and return the most relevant context (``""`` when nothing matches)."""
        chunks = await self.retrieve(user_id, question, top_k)
        return "\n---\n".join(c.text for c in chunks)

    async def clear_memory(self, user_id: int) -> None:
        """Delete user's collection."""
        try:
            await asyncio.to_thread(self.client.delete_collection, _collection_name(user_id))
        except Exception:  # noqa: BLE001 - missing collection is fine
            pass
