"""Hybrid retrieval: BM25+, dense geodesic ranks and conformal abstention."""

from nexus_ai_agent.retrieval.conformal_hybrid import (
    BM25Plus,
    ConformalCalibrator,
    Document,
    EpistemicRetrievalStatus,
    HybridRetriever,
    RetrievalError,
    RetrievalResult,
    RetrievedDocument,
    tokenize,
)

__all__ = [
    "BM25Plus",
    "ConformalCalibrator",
    "Document",
    "EpistemicRetrievalStatus",
    "HybridRetriever",
    "RetrievalError",
    "RetrievalResult",
    "RetrievedDocument",
    "tokenize",
]
