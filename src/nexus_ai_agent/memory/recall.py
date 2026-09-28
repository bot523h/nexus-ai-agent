"""Typed, explainable recall contract for long-term memory.

The defect this module exists to close
--------------------------------------
``LongTermMemory.search`` used to answer with a bare ``list[str]``. That return
type could not distinguish four very different outcomes:

1. a genuine semantic hit,
2. a hit ranked by an *embedding that carries no signal*,
3. a recency fallback taken because a native extension failed to load,
4. nothing stored at all.

All four looked identical to the caller — and to the user, whose assistant would
confidently quote an unrelated memory. The retrieval *mode* was hidden state.

Nexus already answers this problem elsewhere with typed vocabularies:
``jobs/failure_semantics.py`` classifies every failure and fails closed on an
unknown code; ``jobs/verification.py`` reports a typed ``reason_code`` instead of
a bare "failed". This module applies the same discipline to memory, so a recall
is **self-describing**: it names the strategy that produced it, whether that
strategy was below the guaranteed floor, and why.

Invariant (mirrors ``memory ≠ authority`` in the trust model)
------------------------------------------------------------
A :class:`MemoryRecall` is *evidence about retrieval*, never an instruction and
never an authority. It reports what was found and how; it does not decide what
the assistant may do. Nothing here may import ``features/`` or ``bot/``
(``tests/architecture/test_memory_boundaries.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "DENSE_LANE_ABSENT",
    "DENSE_LANE_EMPTY",
    "DENSE_LANE_UNVALIDATED",
    "EMBEDDING_DIM_MISMATCH",
    "EMBEDDING_UNAVAILABLE",
    "LEXICAL_LANE_DEGRADED",
    "NO_RANKING_SIGNAL",
    "NOT_DEGRADED",
    "DegradationReason",
    "MemoryHit",
    "MemoryRecall",
    "MemoryStatus",
    "RetrievalMode",
]


class RetrievalMode(str, Enum):
    """The strategy that actually produced a recall (never inferred by callers)."""

    #: Lexical BM25 **and** a structurally valid dense lane, fused with RRF.
    HYBRID_FUSION = "hybrid_fusion"
    #: Lexical BM25 alone — the guaranteed floor. Needs no embedder, no native
    #: extension and no network, so it is available on every deployment (Q1).
    LEXICAL_BM25 = "lexical_bm25"
    #: Dense lane alone — only when BM25 produced nothing to fuse with.
    DENSE_VECTOR = "dense_vector"
    #: No lane could rank anything, so rows were returned newest-first. This is
    #: the mode that used to masquerade as a semantic hit.
    RECENCY_DEGRADED = "recency_degraded"
    #: The thread has no stored memories at all.
    EMPTY = "empty"


#: Typed degradation vocabulary. Like ``failure_semantics``, an unlisted reason
#: is a contract bug, not a state to invent at a call site.
NOT_DEGRADED = "not_degraded"
NO_RANKING_SIGNAL = "no_ranking_signal"
EMBEDDING_UNAVAILABLE = "embedding_unavailable"
EMBEDDING_DIM_MISMATCH = "embedding_dim_mismatch"
DENSE_LANE_ABSENT = "dense_lane_absent"
DENSE_LANE_EMPTY = "dense_lane_empty"
DENSE_LANE_UNVALIDATED = "dense_lane_unvalidated"
LEXICAL_LANE_DEGRADED = "lexical_lane_degraded"

DegradationReason = str


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """One recalled memory, with its provenance and its score."""

    memory_id: int
    content: str
    rank: int
    score: float
    #: Caller-supplied classification (``"turn"``, ``"preference"``, …). Never
    #: interpreted here — memory is not authority.
    kind: str = "turn"
    #: Where the memory came from (``"graph.turn"``, ``"user.explicit"``, …).
    source: str | None = None
    created_at: float | None = None
    #: Which lane(s) contributed: ``"lexical"``, ``"dense"``, ``"both"``, ``"none"``.
    lane: str = "none"


@dataclass(frozen=True, slots=True)
class MemoryRecall:
    """A self-describing retrieval result.

    ``degraded`` is ``True`` **only** when the outcome is below the guaranteed
    floor (lexical BM25 over stored content). Ranking by an unavailable dense
    lane is *not* degradation, because the floor still holds; returning recency
    because nothing could be ranked *is*.
    """

    query: str
    mode: RetrievalMode
    hits: tuple[MemoryHit, ...] = ()
    degraded: bool = False
    reason: DegradationReason = NOT_DEGRADED
    #: What the dense lane ranked with: ``"validated-embeddings"`` or a typed
    #: reason it did not run. Never ``"sqlite-vec"``/``"none"`` as a quality
    #: claim — the lane is stdlib cosine over stored blobs.
    dense_lane: str = DENSE_LANE_ABSENT
    #: Rows considered for this thread (observability: recall cost is visible).
    scanned: int = 0
    #: Rows currently in the lexical index for this thread.
    lexical_indexed: int = 0
    elapsed_ms: float = 0.0

    @property
    def contents(self) -> list[str]:
        """Backward-compatible plain-text view (best first)."""
        return [hit.content for hit in self.hits]

    def as_dict(self) -> dict[str, object]:
        """JSON-serialisable view for logs/dashboards — content excluded.

        The *content* of a memory is conversation data and must not leak into
        operational records (the same boundary PR #119 holds for memory write
        failures). The shape of the retrieval is not sensitive, so it is safe —
        and useful — to report.
        """
        return {
            "mode": self.mode.value,
            "degraded": self.degraded,
            "reason": self.reason,
            "dense_lane": self.dense_lane,
            "hits": len(self.hits),
            "scanned": self.scanned,
            "lexical_indexed": self.lexical_indexed,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


@dataclass(frozen=True, slots=True)
class MemoryStatus:
    """Store-level health — the answer to "is memory actually working?".

    This is what turns the old *hidden* degradation into an inspectable fact:
    one call reports row counts, the embedding dimension contract, and whether
    any lane is currently below the floor.
    """

    rows: int = 0
    threads: int = 0
    lexical_indexed: int = 0
    rows_with_embedding: int = 0
    #: Distinct embedding widths actually stored. More than one value means the
    #: ``DIM`` contract drifted (a provider swap mid-life) — previously silent.
    dims_observed: tuple[int, ...] = ()
    #: ``"ok"`` · ``"absent"`` · ``"mixed"`` · ``"violated"``
    dim_contract: str = "absent"
    sqlite_vec_loaded: bool = False
    #: True when the store can still rank semantically without any embedder or
    #: native extension — the guarantee this layer now makes.
    lexical_floor_available: bool = True
    path: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "threads": self.threads,
            "lexical_indexed": self.lexical_indexed,
            "rows_with_embedding": self.rows_with_embedding,
            "dims_observed": list(self.dims_observed),
            "dim_contract": self.dim_contract,
            "sqlite_vec_loaded": self.sqlite_vec_loaded,
            "lexical_floor_available": self.lexical_floor_available,
            "path": self.path,
        }
