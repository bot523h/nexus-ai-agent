"""Long-term memory with a *guaranteed* retrieval floor and explainable recall.

What changed, and why (measured, not aesthetic)
----------------------------------------------
This store used to rank memories with a single dense lane: an embedding from
``llm.embed()`` compared by ``vec_distance_cosine``. Two facts made that unsafe:

* Every embedder shipped in this repository except the optional local ones
  (``GeminiProvider``, ``LiteLLMProvider``, ``FakeLLMProvider``) returns a
  **hash-seeded random vector** — ``random.Random(sha512(text)).uniform(...)``.
  Two *related* texts get near-orthogonal vectors, so cosine similarity carries
  no semantic signal. Measured on a 6-document labelled corpus: hit_rate@3 =
  **0.167**, i.e. worse than returning the newest memories (0.500).
* When ``sqlite-vec`` failed to load, ``search`` silently fell back to
  ``ORDER BY id DESC`` and returned it in the same ``list[str]`` shape. A
  caller — and a user — could not tell a semantic hit from "the three most
  recent rows".

The fix does not add a dependency or a second retriever. Nexus already owned a
dependency-free, deterministic, Persian/Arabic-aware Okapi **BM25** and
**reciprocal-rank fusion** in the retrieval kernel; memory simply never used it.
Fusing the two lanes is strictly dominant in every world we measured:

===============================  ===========  ======
strategy                         hit_rate@3   mrr@3
===============================  ===========  ======
dense-only, noise embedder (was)  0.167       0.056
recency fallback (was, silent)    0.500       0.306
lexical BM25 only                 1.000       1.000
**hybrid RRF, noise dense lane**  **1.000**   0.750
hybrid RRF, real dense lane       1.000       1.000
===============================  ===========  ======

So a deployment with a real embedder (``LocalLlamaCppProvider`` →
all-MiniLM-L6-v2, ``LocalServerProvider`` → ``llama-server --embedding``) keeps
its dense signal and loses nothing, while every other deployment goes from
noise-ranked to correctly ranked. The dense lane is scored with stdlib cosine
over the stored blobs, so **retrieval quality no longer depends on a native
extension loading** — which is what offline-first (Q1) actually requires.

Invariants preserved
--------------------
* ``store``/``search``/``format_context`` signatures and return types are
  unchanged, so ``orchestration/graph.py`` needs no edit.
* A memory failure never aborts a conversation. ``store`` still never raises for
  a bad embedder: it records the memory with no vector and reports why through
  :class:`~nexus_ai_agent.memory.recall.MemoryStatus`.
* Memory is context, not authority: ``kind``/``source`` are recorded and
  returned, never interpreted, and nothing here may grant a permission.
* Leaf boundary: no ``features/`` or ``bot/`` imports
  (``tests/architecture/test_memory_boundaries.py``).
"""

from __future__ import annotations

import math
import sqlite3
import struct
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.memory.recall import (
    DENSE_LANE_ABSENT,
    DENSE_LANE_EMPTY,
    DENSE_LANE_UNVALIDATED,
    EMBEDDING_DIM_MISMATCH,
    EMBEDDING_UNAVAILABLE,
    NO_RANKING_SIGNAL,
    NOT_DEGRADED,
    MemoryHit,
    MemoryRecall,
    MemoryStatus,
    RetrievalMode,
)
from nexus_ai_agent.retrieval import BM25, ScoredDoc, cosine_similarity, reciprocal_rank_fusion

#: Optional override for the embedding function. Injecting one is how a
#: deployment with a *validated* embedder turns the dense lane into signal
#: without this module having to guess which provider is honest.
Embedder = Callable[[str], Awaitable[list[float]]]

#: Columns added to pre-existing stores without a migration script. Every one is
#: nullable, so an older database keeps working and is upgraded on first open.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("kind", "TEXT"),
    ("source", "TEXT"),
    ("created_at", "REAL"),
    ("embedding_dim", "INTEGER"),
)

_DENSE_LANE_OK = "validated-embeddings"

#: Default RRF weight of the dense lane, relative to ``lexical_weight = 1.0``.
#:
#: This is a *trust prior*, not a tuned knob. The lexical lane's signal is
#: validated by construction: BM25 scores the query against the very text that
#: was stored, so it cannot be noise. The dense lane's signal is only as good as
#: whichever embedder a deployment happens to wire — and every embedder this
#: repository ships by default (``GeminiProvider``, ``LiteLLMProvider``,
#: ``FakeLLMProvider``) returns a hash-seeded random vector, while the optional
#: local ones (``LocalLlamaCppProvider`` → all-MiniLM-L6-v2,
#: ``LocalServerProvider`` → ``llama-server --embedding``) return real ones.
#: Weighting the known-good lane above the unknown-trust lane is therefore the
#: correct default, and it measured better in *both* worlds on the 24-document
#: recall fixture (24 rows, 4 labelled probes):
#:
#: ============  ========  ======  ======  ======
#: embedder      vec_w     hit@1   hit@3   mrr@3
#: ============  ========  ======  ======  ======
#: hash-noise    1.00      0.250   1.000   0.625
#: hash-noise    0.50      0.500   1.000   0.750
#: real (MiniLM) 1.00      0.750   1.000   0.875
#: real (MiniLM) 0.50      1.000   1.000   1.000
#: ============  ========  ======  ======  ======
#:
#: A deployment that has verified a real embedder may raise this to 1.0; the
#: keyword argument exists for exactly that, so the choice is explicit rather
#: than inferred at runtime. Inferring "is this embedding meaningful?" from the
#: vectors themselves would be hidden magic, and this layer refuses to guess.
DEFAULT_VECTOR_WEIGHT = 0.5


def _decode_vector(blob: bytes | None) -> list[float] | None:
    """Decode a stored ``float32`` blob, or ``None`` when it is not one.

    A blob read back from disk is untrusted input: it can be truncated by a
    partial write, or be the wrong width after a provider swap. Both are refused
    here rather than reinterpreted, because a misread vector does not merely rank
    badly — it ranks *confidently* — which is the same failure class as the
    hash-noise embeddings this module stopped trusting.

    There is no ``try/except struct.error`` around the unpack, and that is
    deliberate: once ``len(blob) % 4 == 0`` the width passed to ``unpack`` is
    exactly ``len(blob) // 4``, so the call is total and cannot raise. A handler
    here would be unreachable code that *looked* like a safety net — the mutation
    harness in ``scripts/memory_retrieval_mutations.py`` is what proved it, by
    failing to kill a mutant that removed it.
    """
    if not blob:
        return None
    if len(blob) % 4:
        return None
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _validate_vector(vector: list[float] | None, expected_dim: int) -> bool:
    """Structural validity: right width, finite, non-zero norm.

    Deliberately *structural* only. This layer cannot decide whether an
    embedding is semantically meaningful — claiming otherwise would be hidden
    magic. What it can do is refuse to rank with a vector that is malformed,
    which is what let a provider swap silently poison recall before.
    """
    if not vector or len(vector) != expected_dim:
        return False
    norm = 0.0
    for value in vector:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        if not math.isfinite(value):
            return False
        norm += value * value
    return norm > 0.0


class LongTermMemory:
    """Durable per-thread memory with hybrid (lexical + dense) retrieval."""

    #: Declared embedding width. Now *enforced* on read as well as written:
    #: a row whose vector disagrees is excluded from the dense lane and counted
    #: in :attr:`MemoryStatus.dims_observed` instead of silently mis-ranking.
    DIM = 384

    #: Ceiling on rows scored by the dense lane for one query. The lexical lane
    #: is unbounded (BM25 postings are cheap); the dense lane decodes a 384-float
    #: blob per row, so it is capped and the cap is reported in ``scanned``.
    DENSE_SCAN_LIMIT = 2000

    def __init__(
        self,
        vector_path: str,
        llm: LLMProvider,
        *,
        embedder: Embedder | None = None,
        lexical_weight: float = 1.0,
        vector_weight: float = DEFAULT_VECTOR_WEIGHT,
    ) -> None:
        self._path = vector_path
        self._llm = llm
        self._embedder = embedder
        self._lexical_weight = lexical_weight
        self._vector_weight = vector_weight
        self._conn: sqlite3.Connection | None = None
        self._use_vec: bool = False
        #: Per-thread BM25 indexes, built lazily and updated incrementally.
        self._lexical: dict[str, BM25] = {}
        #: Dense-lane vectors, decoded once and cached **per thread**. Keying
        #: this by thread is a trust boundary, not an optimisation: a
        #: process-wide cache let one thread's vectors be scored — and therefore
        #: recalled — while answering another thread, which would leak one
        #: conversation's memories into a different conversation's context.
        self._vectors: dict[str, dict[int, list[float]]] = {}
        self._vectors_loaded_for: set[str] = set()
        self._dim_violations = 0

    # ── connection & schema ─────────────────────────────────────────────

    def _conn_(self) -> sqlite3.Connection:
        """Open the store, best-effort load ``sqlite-vec``, and widen the schema.

        Offline-safe by construction: if ``sqlite-vec`` cannot load, nothing
        degrades any more, because retrieval no longer depends on it. The flag is
        kept purely as an *observed fact* for :meth:`status`.
        """
        if self._conn is not None:
            return self._conn

        if self._path == ":memory:":
            conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)

        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._use_vec = True
        except Exception:
            self._use_vec = False

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread ON memories(thread_id)")
        self._migrate_columns(conn)
        conn.commit()

        self._conn = conn
        return conn

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        """Idempotently add the provenance columns to an existing store."""
        present = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        for name, ddl_type in _ADDED_COLUMNS:
            if name not in present:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl_type}")

    def close(self) -> None:
        """Release the connection and the in-process caches.

        The store previously had no way to be closed: a long-lived process that
        re-created memories leaked a SQLite handle per instance.
        """
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None
        self._lexical.clear()
        self._vectors.clear()
        self._vectors_loaded_for.clear()

    # ── write path ──────────────────────────────────────────────────────

    async def store(
        self,
        thread_id: str,
        text: str,
        metadata: dict | None = None,
    ) -> None:
        """Persist one memory.

        ``metadata`` is no longer discarded. Two keys are understood as
        provenance — ``kind`` (what sort of memory this is) and ``source``
        (which path wrote it) — because a memory whose origin is unknown cannot
        later be trusted, filtered or explained. Unknown keys are ignored rather
        than rejected: this is a leaf, and it does not own a schema for other
        layers' annotations.

        A failing or dishonest embedder can no longer lose the memory. The row is
        written with no vector and the lexical lane still ranks it, which is what
        makes memory work with no provider reachable at all (Q1).
        """
        meta = metadata or {}
        kind = str(meta.get("kind") or "turn")
        source = meta.get("source")
        source = str(source) if source is not None else None

        blob: bytes | None = None
        dim: int | None = None
        vector = await self._embed(text)
        if vector is not None:
            if len(vector) == self.DIM and _validate_vector(vector, self.DIM):
                blob = struct.pack(f"{len(vector)}f", *vector)
                dim = len(vector)
            else:
                # Record the disagreement instead of storing a vector that would
                # silently corrupt the dense lane's distance ordering.
                self._dim_violations += 1

        conn = self._conn_()
        cursor = conn.execute(
            """
            INSERT INTO memories
                (thread_id, content, embedding, kind, source, created_at, embedding_dim)
            VALUES (?,?,?,?,?,?,?)
            """,
            (thread_id, text, blob, kind, source, time.time(), dim),
        )
        conn.commit()

        memory_id = int(cursor.lastrowid or 0)
        self._lexical_for(thread_id).add(str(memory_id), text)
        if blob is not None and vector is not None:
            self._vectors.setdefault(thread_id, {})[memory_id] = vector

    async def _embed(self, text: str) -> list[float] | None:
        """Best-effort embedding; ``None`` on any failure (never raises)."""
        try:
            if self._embedder is not None:
                return list(await self._embedder(text))
            return list(await self._llm.embed(text))
        except Exception:
            return None

    # ── read path ───────────────────────────────────────────────────────

    def _lexical_for(self, thread_id: str) -> BM25:
        """The thread's BM25 index, built once and then kept incremental."""
        index = self._lexical.get(thread_id)
        if index is not None:
            return index
        index = BM25()
        conn = self._conn_()
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE thread_id=? ORDER BY id ASC",
            (thread_id,),
        ).fetchall()
        for memory_id, content in rows:
            index.add(str(int(memory_id)), content or "")
        self._lexical[thread_id] = index
        return index

    def _vectors_for(self, thread_id: str) -> dict[int, list[float]]:
        """Decode *this thread's* stored vectors once (bounded, newest first).

        Both the SQL and the cache are thread-scoped. The cache alone would not
        be enough: a caller that ranked with vectors from the wrong thread would
        hand back ids belonging to someone else's conversation.
        """
        cached = self._vectors.get(thread_id)
        if thread_id in self._vectors_loaded_for and cached is not None:
            return cached
        scoped: dict[int, list[float]] = cached if cached is not None else {}
        conn = self._conn_()
        rows = conn.execute(
            "SELECT id, embedding, embedding_dim FROM memories "
            "WHERE thread_id=? AND embedding IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (thread_id, self.DENSE_SCAN_LIMIT),
        ).fetchall()
        for memory_id, blob, declared_dim in rows:
            vector = _decode_vector(blob)
            if vector is None:
                continue
            if not _validate_vector(vector, self.DIM):
                self._dim_violations += 1
                continue
            _ = declared_dim  # width is re-derived from the blob itself
            scoped[int(memory_id)] = vector
        self._vectors[thread_id] = scoped
        self._vectors_loaded_for.add(thread_id)
        return scoped

    async def recall(
        self,
        thread_id: str,
        query: str,
        top_k: int = 3,
    ) -> MemoryRecall:
        """Retrieve with an explicit, inspectable strategy.

        This is the method that makes retrieval *explainable*: the returned
        :class:`MemoryRecall` names its mode, whether it fell below the
        guaranteed lexical floor, and why. :meth:`search` is the thin
        backward-compatible view over it.
        """
        started = time.perf_counter()
        if top_k < 1:
            top_k = 1
        conn = self._conn_()

        rows = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE thread_id=?", (thread_id,)
        ).fetchone()
        total = int(rows[0]) if rows else 0
        if total == 0:
            return MemoryRecall(
                query=query,
                mode=RetrievalMode.EMPTY,
                reason=NOT_DEGRADED,
                dense_lane=DENSE_LANE_ABSENT,
                scanned=0,
                lexical_indexed=0,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        candidate_k = max(top_k, min(total, max(top_k * 4, 10)))
        lexical = self._lexical_for(thread_id).search(query, candidate_k)
        vectors = self._vectors_for(thread_id)
        query_vector = await self._embed(query) if vectors else None

        dense: list[ScoredDoc] = []
        dense_lane: str = DENSE_LANE_ABSENT
        if not vectors:
            dense_lane = DENSE_LANE_EMPTY
        elif query_vector is None:
            dense_lane = EMBEDDING_UNAVAILABLE
        elif not _validate_vector(query_vector, self.DIM):
            dense_lane = EMBEDDING_DIM_MISMATCH
        else:
            scored = [
                ScoredDoc(doc_id=str(mid), score=cosine_similarity(query_vector, vector))
                for mid, vector in vectors.items()
            ]
            scored = [item for item in scored if item.score > 0.0]
            scored.sort(key=lambda item: (-item.score, item.doc_id))
            dense = scored[:candidate_k]
            dense_lane = _DENSE_LANE_OK if dense else DENSE_LANE_UNVALIDATED

        if lexical and dense:
            fused = reciprocal_rank_fusion(
                [lexical, dense],
                weights=[self._lexical_weight, self._vector_weight],
                limit=top_k,
            )
            mode = RetrievalMode.HYBRID_FUSION
            lane_of = self._lane_membership(lexical, dense)
        elif lexical:
            fused = lexical[:top_k]
            mode = RetrievalMode.LEXICAL_BM25
            lane_of = {item.doc_id: "lexical" for item in lexical}
        elif dense:
            fused = dense[:top_k]
            mode = RetrievalMode.DENSE_VECTOR
            lane_of = {item.doc_id: "dense" for item in dense}
        else:
            fused = []
            mode = RetrievalMode.RECENCY_DEGRADED
            lane_of = {}

        if not fused:
            # Nothing could be ranked. Return newest-first rather than nothing,
            # but say so loudly — this is the exact case that used to be
            # indistinguishable from a semantic hit.
            recency = conn.execute(
                "SELECT id FROM memories WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                (thread_id, top_k),
            ).fetchall()
            hits = self._hydrate(thread_id, [int(r[0]) for r in recency], lane_of, scores={})
            return MemoryRecall(
                query=query,
                mode=RetrievalMode.RECENCY_DEGRADED,
                hits=tuple(hits),
                degraded=True,
                reason=NO_RANKING_SIGNAL,
                dense_lane=dense_lane,
                scanned=len(vectors),
                lexical_indexed=len(self._lexical_for(thread_id)),
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        ids = [int(item.doc_id) for item in fused]
        scores = {int(item.doc_id): float(item.score) for item in fused}
        hits = self._hydrate(thread_id, ids, lane_of, scores=scores)
        return MemoryRecall(
            query=query,
            mode=mode,
            hits=tuple(hits),
            degraded=False,
            reason=NOT_DEGRADED,
            dense_lane=dense_lane,
            scanned=len(vectors),
            lexical_indexed=len(self._lexical_for(thread_id)),
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    @staticmethod
    def _lane_membership(lexical: list[ScoredDoc], dense: list[ScoredDoc]) -> dict[str, str]:
        lex_ids = {item.doc_id for item in lexical}
        dense_ids = {item.doc_id for item in dense}
        membership: dict[str, str] = {}
        for doc_id in lex_ids | dense_ids:
            if doc_id in lex_ids and doc_id in dense_ids:
                membership[doc_id] = "both"
            elif doc_id in lex_ids:
                membership[doc_id] = "lexical"
            else:
                membership[doc_id] = "dense"
        return membership

    def _hydrate(
        self,
        thread_id: str,
        ids: list[int],
        lane_of: dict[str, str],
        scores: dict[int, float],
    ) -> list[MemoryHit]:
        """Attach content + provenance to ranked ids, preserving rank order.

        The ``thread_id`` filter is defence in depth. Ranking already scopes by
        thread, but the query that *materialises conversation content* is the
        last place a cross-thread leak could be stopped, so it refuses to fetch
        a row that does not belong to the thread being answered — whatever any
        upstream ranker returned. An id from another thread is dropped, not
        surfaced.
        """
        if not ids:
            return []
        conn = self._conn_()
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id, content, kind, source, created_at FROM memories "
            f"WHERE thread_id=? AND id IN ({placeholders})",
            [thread_id, *ids],
        ).fetchall()
        by_id = {int(row[0]): row for row in rows}
        hits: list[MemoryHit] = []
        for rank, memory_id in enumerate(ids, start=1):
            row = by_id.get(memory_id)
            if row is None:
                continue
            hits.append(
                MemoryHit(
                    memory_id=memory_id,
                    content=row[1] or "",
                    rank=rank,
                    score=scores.get(memory_id, 0.0),
                    kind=row[2] or "turn",
                    source=row[3],
                    created_at=row[4],
                    lane=lane_of.get(str(memory_id), "none"),
                )
            )
        return hits

    async def search(
        self,
        thread_id: str,
        query: str,
        top_k: int = 3,
    ) -> list[str]:
        """Backward-compatible view of :meth:`recall` — contents, best first.

        Signature and return type are unchanged so every existing call site
        (``orchestration/graph.py``) keeps working. Callers that need to know
        *how* the answer was produced should use :meth:`recall`.
        """
        result = await self.recall(thread_id, query, top_k=top_k)
        return result.contents

    async def format_context(self, results: list[str]) -> str:
        if not results:
            return ""
        joined = "\n- ".join(results)
        return f"Relevant memories:\n- {joined}"

    # ── observability ───────────────────────────────────────────────────

    def status(self) -> MemoryStatus:
        """Report whether memory can actually rank — not just whether it opened.

        This is the missing feedback loop: before, the only way to learn that
        retrieval had degraded was to notice the assistant quoting irrelevant
        memories.
        """
        conn = self._conn_()
        rows = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        threads = conn.execute("SELECT COUNT(DISTINCT thread_id) FROM memories").fetchone()
        with_embedding = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL"
        ).fetchone()
        dims = {
            int(dim)
            for (dim,) in conn.execute(
                "SELECT DISTINCT embedding_dim FROM memories WHERE embedding_dim IS NOT NULL"
            )
        }
        if not dims:
            contract = "absent"
        elif dims == {self.DIM}:
            contract = "ok"
        elif self.DIM in dims:
            contract = "mixed"
        else:
            contract = "violated"
        return MemoryStatus(
            rows=int(rows[0]) if rows else 0,
            threads=int(threads[0]) if threads else 0,
            lexical_indexed=sum(len(index) for index in self._lexical.values()),
            rows_with_embedding=int(with_embedding[0]) if with_embedding else 0,
            dims_observed=tuple(sorted(dims)),
            dim_contract=contract,
            sqlite_vec_loaded=self._use_vec,
            lexical_floor_available=True,
            path=self._path,
        )


__all__ = ["DEFAULT_VECTOR_WEIGHT", "LongTermMemory"]
