"""Retrieval — the dependency-free ranking kernel shared by RAG and memory.

A *leaf* package: it imports nothing from ``nexus_ai_agent`` except itself, and
nothing outside the standard library. That is what allows both a product engine
(``features/rag.py``) and a fenced leaf (``memory/``) to depend on it without
creating a cycle or violating ``tests/architecture/test_memory_boundaries.py``.

Public surface (all implemented in :mod:`nexus_ai_agent.retrieval.core`):

* :func:`normalize_text` / :func:`tokenize` — Unicode-first, Persian/Arabic
  aware folding (yeh, kaf, heh, hamza, tatweel, harakat, Arabic-Indic digits,
  ZWNJ) so ``کتاب`` / ``كتاب`` and ``۴۲`` / ``42`` retrieve together.
* :func:`chunk_text` — recursive, lossless, offset-addressed windowing.
* :class:`BM25` — incremental Okapi BM25 over tokenised documents.
* :func:`cosine_similarity` / :func:`reciprocal_rank_fusion` — lane scoring and
  score-scale-independent fusion.
* :class:`HybridRetriever` — lexical + optional dense lanes fused with RRF;
  with no embedder it degrades to BM25 **instead of failing**.
* :class:`EvalCase` / :class:`EvalReport` / :func:`evaluate_retrieval` — the
  ``recall@k`` / ``mrr@k`` / ``hit_rate`` harness.
"""

from __future__ import annotations

from nexus_ai_agent.retrieval.core import (
    BM25,
    Chunk,
    EvalCase,
    EvalReport,
    HybridRetriever,
    ScoredDoc,
    chunk_text,
    cosine_similarity,
    estimate_tokens,
    evaluate_retrieval,
    normalize_text,
    reciprocal_rank_fusion,
    tokenize,
)

__all__ = [
    "BM25",
    "Chunk",
    "EvalCase",
    "EvalReport",
    "HybridRetriever",
    "ScoredDoc",
    "chunk_text",
    "cosine_similarity",
    "estimate_tokens",
    "evaluate_retrieval",
    "normalize_text",
    "reciprocal_rank_fusion",
    "tokenize",
]
