from __future__ import annotations

import logging
import os
from typing import Any

import chromadb
from chromadb.utils import embedding_functions
from flashrank import Ranker, RerankRequest

from nexus_ai_agent.config.settings import get_settings

logger = logging.getLogger(__name__)


class AdvancedRAGEngine:
    """Advanced RAG using ChromaDB, Sentence-Transformers, and FlashRank."""

    def __init__(self) -> None:
        settings = get_settings()
        os.makedirs(settings.chroma_db_path, exist_ok=True)

        # 1. Initialize ChromaDB with local persistence
        self.client = chromadb.PersistentClient(path=settings.chroma_db_path)

        # 2. Local Embeddings (Sentence-Transformers)
        # Model 'all-MiniLM-L6-v2' is small, fast, and free.
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )

        # 3. FlashRank Re-ranker (Free & Fast)
        try:
            self.ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2", cache_dir="data/flashrank")
        except Exception as e:
            logger.warning(f"FlashRank init failed, falling back to basic retrieval: {e}")
            self.ranker = None

    def _get_collection(self, user_id: int) -> Any:
        """Get or create a unique collection for each user."""
        collection_name = f"user_docs_{user_id}"
        return self.client.get_or_create_collection(
            name=collection_name, embedding_function=self.embedding_fn
        )

    async def add_document(self, user_id: int, text: str, metadata: dict[str, Any]) -> None:
        """Chunk and add document to the vector database."""
        collection = self._get_collection(user_id)

        # Simple chunking (can be improved with semantic chunking)
        chunk_size = 1000
        chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

        ids = [f"chunk_{metadata.get('file_id', 'doc')}_{i}" for i in range(len(chunks))]
        metadatas = [metadata for _ in chunks]

        collection.add(documents=chunks, metadatas=metadatas, ids=ids)
        logger.info(f"Added {len(chunks)} chunks to collection for user {user_id}")

    async def query(self, user_id: int, question: str, top_k: int = 10) -> str:
        """Query, re-rank, and return the most relevant context."""
        collection = self._get_collection(user_id)

        # Initial retrieval
        results = collection.query(query_texts=[question], n_results=top_k)

        documents = results.get("documents", [[]])[0]
        if not documents:
            return "No relevant documents found."

        # Re-ranking with FlashRank
        if self.ranker:
            passages = [
                {"id": i, "text": doc, "meta": results["metadatas"][0][i]}
                for i, doc in enumerate(documents)
            ]
            rerank_request = RerankRequest(query=question, passages=passages)
            reranked_results = self.ranker.rerank(rerank_request)

            # Take top 3 after re-ranking
            final_context = "\n---\n".join([r["text"] for r in reranked_results[:3]])
        else:
            # Fallback to top 3 from initial retrieval
            final_context = "\n---\n".join(documents[:3])

        return final_context

    async def clear_memory(self, user_id: int) -> None:
        """Delete user's collection."""
        try:
            self.client.delete_collection(f"user_docs_{user_id}")
        except Exception:
            pass
