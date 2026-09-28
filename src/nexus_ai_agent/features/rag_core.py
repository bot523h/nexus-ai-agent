"""Re-export shim — the retrieval kernel moved to :mod:`nexus_ai_agent.retrieval`.

Historically this module *was* the pure retrieval core (chunking, BM25, RRF,
hybrid retriever, ``recall@k`` harness) and lived under ``features/`` because
its first consumer was :mod:`nexus_ai_agent.features.rag`.

It moved to the leaf package :mod:`nexus_ai_agent.retrieval` for one concrete
reason, not for tidiness: **``memory/`` is fenced as a leaf**
(``tests/architecture/test_memory_boundaries.py`` forbids importing ``features``
or ``bot``), and long-term memory needed the *same* BM25 and the *same* fusion
rule the RAG engine already uses. With the kernel under ``features/`` the only
ways to share it were to break that fence or to grow a second, silently
divergent retriever inside ``memory/``. Relocating the kernel removes the
choice.

This file stays so that every pre-existing import path keeps working:

    from nexus_ai_agent.features.rag_core import BM25, chunk_text   # unchanged

New code should import from :mod:`nexus_ai_agent.retrieval` directly.
"""

from __future__ import annotations

from nexus_ai_agent.retrieval.core import (
    BM25,
    MAX_CHUNK_TOKENS,
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
from nexus_ai_agent.retrieval.core import (
    DEFAULT_CHUNK_TOKENS as DEFAULT_CHUNK_TOKENS,
)
from nexus_ai_agent.retrieval.core import (
    DEFAULT_OVERLAP_RATIO as DEFAULT_OVERLAP_RATIO,
)

__all__ = [
    "BM25",
    "Chunk",
    "DEFAULT_CHUNK_TOKENS",
    "DEFAULT_OVERLAP_RATIO",
    "EvalCase",
    "EvalReport",
    "HybridRetriever",
    "MAX_CHUNK_TOKENS",
    "ScoredDoc",
    "chunk_text",
    "cosine_similarity",
    "estimate_tokens",
    "evaluate_retrieval",
    "normalize_text",
    "reciprocal_rank_fusion",
    "tokenize",
]
