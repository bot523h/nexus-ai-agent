"""Dependency-free retrieval core for :mod:`nexus_ai_agent.features.rag`.

This module is the **pure** half of the RAG engine: recursive chunking, an
Okapi BM25 index, reciprocal-rank fusion of lexical and vector rankings, and a
``recall@k`` evaluation harness. It imports **nothing** outside the standard
library on purpose:

* the engine in :mod:`nexus_ai_agent.features.rag` pulls in ``chromadb``,
  ``sentence-transformers`` (→ torch) and ``flashrank`` **lazily**; keeping the
  algorithms here means those heavy stacks are never required to unit-test
  (or to run) the retrieval logic;
* the frozen architecture tests forbid new ``telegram``/``sqlmodel`` imports
  under ``src/`` — a stdlib-only core is immune by construction.

Design rules
------------
* **Deterministic.** No randomness, no hashing-order dependence: the same
  input always yields the same chunks and the same ranking, which is what makes
  the ``recall@k`` numbers in the test suite meaningful instead of noisy.
* **Lossless chunking.** ``chunk_text`` returns spans over the *original*
  string; the spans cover it fully and every chunk satisfies
  ``text[chunk.start:chunk.end] == chunk.text``. Overlap is expressed as a
  ratio of the target window, never as a mutation of the source.
* **Unicode-first.** Persian/Arabic normalisation (yeh, kaf, heh, hamza,
  tatweel, harakat, Arabic-Indic and Extended-Arabic-Indic digits) runs before
  tokenisation, so ``کتاب`` / ``كتاب`` and ``۴۲`` / ``42`` retrieve together.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

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

# ── token / size model ────────────────────────────────────────────────────
# A byte-pair tokenizer costs a heavy dependency; the standard proxy
# (≈ 4 characters per token for Latin, ≈ 2 for Persian) is deterministic and
# good enough to *bound* a window. Callers treat these as "about" — the engine
# never hands a chunk larger than the target to an embedding model.
_CHARS_PER_TOKEN = 4

#: Target window (tokens) — inside the 256–512 band from task-127.
DEFAULT_CHUNK_TOKENS = 384
#: Overlap between consecutive chunks, as a fraction of the target window.
DEFAULT_OVERLAP_RATIO = 0.15
#: Never emit a chunk smaller than this fraction of the target (avoid dust).
_MIN_CHUNK_RATIO = 0.15
#: Hard ceiling so a pathological blob can never produce one giant chunk.
MAX_CHUNK_TOKENS = 1024

#: Separator ladder, coarse → fine. The chunker recurses down it.
DEFAULT_SEPARATORS: tuple[str, ...] = (
    "\n\n",  # blank line / paragraph
    "\n",  # line
    "۔",  # Persian full stop
    ".",  # Latin full stop
    "!",  # exclamation (Latin + Persian share "!")
    "؟",  # Persian question mark
    "?",  # Latin question mark
    "؛",  # Persian semicolon
    ";",  # Latin semicolon
    "،",  # Persian comma
    ",",  # Latin comma
    " ",  # word
)

# ── Unicode normalisation ─────────────────────────────────────────────────

#: Arabic → Persian letter forms and other high-frequency equivalences.
_CHAR_MAP: dict[int, str] = {
    0x0649: "ی",  # alef maksura  → yeh
    0x064A: "ی",  # yeh           → Persian yeh (U+06CC is already correct)
    0x06CD: "ی",  # yeh with tail → yeh
    0x0643: "ک",  # Arabic kaf    → keheh
    0x06C0: "ه",  # heh + hamza   → heh
    0x0629: "ه",  # teh marbuta   → heh
    0x0622: "آ",  # alef + madda  → alef with madda above
    0x0623: "ا",  # alef + hamza  → alef
    0x0625: "ا",  # alef + hamza below → alef
    0x0627: "ا",  # alef
    0x0640: "",  # tatweel (keshide) → drop
    0x200C: " ",  # ZWNJ → space (a Persian word boundary)
    0x200D: " ",  # ZWJ  → space
}

#: Ranges to delete outright: Arabic harakat + Quranic marks + tatweel leftovers.
_STRIP_RANGES: tuple[tuple[int, int], ...] = (
    (0x064B, 0x065F),  # harakat (fathatan … shadda … sukun)
    (0x0670, 0x0670),  # dagger alef
    (0x06D6, 0x06ED),  # Quranic annotation marks
    (0xFE70, 0xFEFF),  # presentation forms (isolated/medial/final Arabic)
    (0x0610, 0x061A),  # additional Quranic marks
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _normalize_char(char: str) -> str:
    code = ord(char)
    if 0x0660 <= code <= 0x0669:  # Arabic-Indic digits ٠-٩
        return str(code - 0x0660)
    if 0x06F0 <= code <= 0x06F9:  # Extended Arabic-Indic digits ۰-۹
        return str(code - 0x06F0)
    for low, high in _STRIP_RANGES:
        if low <= code <= high:
            return ""
    return _CHAR_MAP.get(code, char)


def normalize_text(text: str) -> str:
    """Fold a string into a canonical retrieval form.

    ``NFKC`` first (compatibility forms, ligatures, full-width), then the
    Persian/Arabic equivalences, then digit folding. Case folding happens in
    :func:`tokenize` so the original casing survives for display.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(_normalize_char(char) for char in folded)


def tokenize(text: str) -> list[str]:
    """Split text into lowercase retrieval tokens (Latin + Persian + Arabic)."""
    if not text:
        return []
    return [token for token in _WORD_RE.findall(normalize_text(text).casefold()) if token]


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (≈ 4 characters per token, min 1)."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


# ── chunking ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrieval window, addressed in the coordinates of the source text."""

    text: str
    start: int
    end: int
    index: int

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.text)


def _target_chars(chunk_tokens: int) -> int:
    return max(1, chunk_tokens) * _CHARS_PER_TOKEN


def _split_with_offsets(text: str, separator: str) -> list[tuple[str, int]]:
    """Split ``text`` keeping the separator attached to the preceding piece.

    Returns ``[(piece, offset)]``; offsets are absolute inside ``text``.
    """
    pieces: list[tuple[str, int]] = []
    cursor = 0
    while True:
        found = text.find(separator, cursor)
        if found == -1:
            break
        end = found + len(separator)
        pieces.append((text[cursor:end], cursor))
        cursor = end
    if cursor < len(text) or not pieces:
        pieces.append((text[cursor:], cursor))
    return pieces


def _hard_spans(text: str, offset: int, target_chars: int) -> list[tuple[int, int]]:
    """Last resort: cut on character boundaries (never loses a character)."""
    if not text:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + target_chars, len(text))
        spans.append((offset + start, offset + end))
        start = end
    return spans


def _merge_units(units: Sequence[tuple[str, int]], target_chars: int) -> list[tuple[int, int]]:
    """Greedily group atomic units into windows of ≈ ``target_chars``."""
    spans: list[tuple[int, int]] = []
    current_start: int | None = None
    current_end = 0
    current_len = 0

    def flush() -> None:
        nonlocal current_start, current_end, current_len
        if current_start is not None and current_end > current_start:
            spans.append((current_start, current_end))
        current_start, current_end, current_len = None, 0, 0

    for piece, offset in units:
        if current_start is None:
            current_start = offset
        current_end = offset + len(piece)
        current_len += len(piece)
        if current_len >= target_chars:
            flush()
    flush()
    return spans


def _recursive_spans(
    text: str, offset: int, separators: Sequence[str], target_chars: int
) -> list[tuple[int, int]]:
    """Recursively split ``text`` down the separator ladder.

    Spans are returned in **absolute** coordinates (``offset`` is where
    ``text`` starts inside the document), so callers can slice the original
    string. Coverage is an invariant: the union of the spans is ``text``
    (whitespace-only regions excepted, which are dropped as retrieval noise).
    """
    if not text:
        return []
    if not separators:
        # Ladder exhausted: cut on character boundaries rather than emitting a
        # window larger than the embedding model can take.
        return _hard_spans(text, offset, target_chars)
    if len(text) <= target_chars:
        return [(offset, offset + len(text))] if text.strip() else []

    separator = separators[0]
    units = _split_with_offsets(text, separator)
    if len(units) <= 1:
        return _recursive_spans(text, offset, separators[1:], target_chars)

    spans: list[tuple[int, int]] = []
    for rel_start, rel_end in _merge_units(units, target_chars):
        piece = text[rel_start:rel_end]
        abs_start, abs_end = offset + rel_start, offset + rel_end
        if len(piece) > target_chars:
            spans.extend(_recursive_spans(piece, abs_start, separators[1:], target_chars))
        elif piece.strip():
            spans.append((abs_start, abs_end))
    return spans


def chunk_text(
    text: str,
    *,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
    separators: Sequence[str] = DEFAULT_SEPARATORS,
) -> list[Chunk]:
    """Split ``text`` into overlapping, offset-addressed retrieval chunks.

    The splitter walks the separator ladder (:data:`DEFAULT_SEPARATORS`) from
    paragraph down to word: a window that is still too large after a separator
    level is recursed into the next one, so boundaries prefer paragraphs, then
    lines, then sentences, then clauses, and only ever fall back to raw
    characters. Consecutive chunks overlap by ``overlap_ratio`` of the target
    window so an answer that straddles a boundary stays retrievable.

    Guarantees: deterministic, lossless (spans cover the source), and no chunk
    is empty or whitespace-only.
    """
    if not text or not text.strip():
        return []
    if chunk_tokens < 1:
        raise ValueError("chunk_tokens must be >= 1")
    if not 0.0 <= overlap_ratio < 1.0:
        raise ValueError("overlap_ratio must be in [0, 1)")

    target_chars = _target_chars(min(chunk_tokens, MAX_CHUNK_TOKENS))
    spans = _recursive_spans(text, 0, separators, target_chars)
    if not spans:
        return []

    # Drop trailing "dust": a final window far below the target is merged into
    # its predecessor instead of becoming a one-word chunk.
    min_chars = max(1, int(target_chars * _MIN_CHUNK_RATIO))
    if len(spans) > 1 and (spans[-1][1] - spans[-1][0]) < min_chars:
        last_start, last_end = spans.pop()
        prev_start, _ = spans[-1]
        if last_end - prev_start <= target_chars * 2:
            spans[-1] = (prev_start, last_end)
        else:
            spans.append((last_start, last_end))

    # Overlap: pull each window's start back by a fraction of the target.
    overlap_chars = int(target_chars * overlap_ratio)
    windowed: list[tuple[int, int]] = []
    for index, (start, end) in enumerate(spans):
        window_start = max(0, start - overlap_chars) if index and overlap_chars else start
        if windowed and window_start < windowed[-1][0]:
            window_start = windowed[-1][0]
        windowed.append((window_start, end))

    chunks: list[Chunk] = []
    for _position, (start, end) in enumerate(windowed):
        piece = text[start:end]
        if not piece.strip():
            continue
        # Trim the leading whitespace an overlap may have introduced.
        stripped_start = start + (len(piece) - len(piece.lstrip()))
        stripped_end = end - (len(piece) - len(piece.rstrip()))
        if stripped_end <= stripped_start:
            continue
        chunks.append(
            Chunk(
                text=text[stripped_start:stripped_end],
                start=stripped_start,
                end=stripped_end,
                index=len(chunks),
            )
        )
    return chunks


# ── lexical retrieval (Okapi BM25) ────────────────────────────────────────

_K1 = 1.5
_B = 0.75


@dataclass(frozen=True, slots=True)
class ScoredDoc:
    """A document id with its retrieval score (higher is better)."""

    doc_id: str
    score: float


class BM25:
    """Incremental Okapi BM25 index over tokenised documents.

    ``add`` is O(tokens of one document), so the engine can index chunks as
    they are ingested without a rebuild. Documents are keyed by an opaque id
    and can be removed/replaced (needed when a file is re-uploaded).
    """

    def __init__(self, *, k1: float = _K1, b: float = _B) -> None:
        self.k1 = k1
        self.b = b
        self._lengths: dict[str, int] = {}
        self._terms: dict[str, dict[str, int]] = {}
        self._total_length = 0

    # ── mutation ──────────────────────────────────────────────────────
    def add(self, doc_id: str, text: str) -> None:
        """Index (or re-index) ``doc_id`` with ``text``."""
        tokens = tokenize(text)
        if doc_id in self._lengths:
            self.remove(doc_id)
        self._lengths[doc_id] = len(tokens)
        self._total_length += len(tokens)
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            self._terms.setdefault(token, {})[doc_id] = count

    def remove(self, doc_id: str) -> bool:
        """Drop ``doc_id``. Returns ``True`` when something was removed."""
        length = self._lengths.pop(doc_id, None)
        if length is None:
            return False
        self._total_length -= length
        empty: list[str] = []
        for token, postings in self._terms.items():
            if postings.pop(doc_id, None) is not None and not postings:
                empty.append(token)
        for token in empty:
            self._terms.pop(token, None)
        return True

    def clear(self) -> None:
        self._lengths.clear()
        self._terms.clear()
        self._total_length = 0

    # ── query ─────────────────────────────────────────────────────────
    def __len__(self) -> int:
        return len(self._lengths)

    @property
    def average_length(self) -> float:
        if not self._lengths:
            return 0.0
        return self._total_length / len(self._lengths)

    def idf(self, token: str) -> float:
        """Robertson/Sparck-Jones IDF, floored at 0 for very common terms."""
        postings = self._terms.get(token)
        if not postings:
            return 0.0
        n = len(self._lengths)
        return max(0.0, math.log(1.0 + ((n - len(postings) + 0.5) / (len(postings) + 0.5))))

    def score(self, doc_id: str, query_tokens: Sequence[str]) -> float:
        """BM25 score of one document against an already tokenised query."""
        length = self._lengths.get(doc_id, 0)
        if not length:
            return 0.0
        total = 0.0
        for token in query_tokens:
            postings = self._terms.get(token)
            if not postings or doc_id not in postings:
                continue
            tf = postings[doc_id]
            denominator = tf + self.k1 * (1.0 - self.b + self.b * (length / self.average_length))
            total += self.idf(token) * ((tf * (self.k1 + 1.0)) / denominator)
        return total

    def search(self, query: str, k: int = 10) -> list[ScoredDoc]:
        """Top-``k`` documents for ``query`` (ties broken by id → stable)."""
        tokens = tokenize(query)
        if not tokens or not self._lengths:
            return []
        scored = [
            ScoredDoc(doc_id=doc_id, score=self.score(doc_id, tokens)) for doc_id in self._lengths
        ]
        scored = [item for item in scored if item.score > 0.0]
        scored.sort(key=lambda item: (-item.score, item.doc_id))
        return scored[:k]


# ── fusion ────────────────────────────────────────────────────────────────

_RRF_CONSTANT = 60.0


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 on degenerate input)."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right, strict=False):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[ScoredDoc]],
    *,
    weights: Sequence[float] | None = None,
    limit: int = 10,
    rrf_constant: float = _RRF_CONSTANT,
) -> list[ScoredDoc]:
    """Fuse ranked lists with weighted reciprocal-rank fusion.

    RRF is score-scale independent, which matters here: BM25 scores are
    unbounded tf-idf-ish values while cosine similarities live in ``[-1, 1]``.
    ``weights`` biases one lane (e.g. lexical for exact-match queries).
    """
    if not rankings:
        return []
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights must match the number of rankings")

    fused: dict[str, float] = {}
    for weight, ranking in zip(weights, rankings, strict=False):
        for rank, item in enumerate(ranking, start=1):
            fused[item.doc_id] = fused.get(item.doc_id, 0.0) + weight / (rrf_constant + rank)
    ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
    return [ScoredDoc(doc_id=doc_id, score=score) for doc_id, score in ordered[:limit]]


# ── hybrid retriever ──────────────────────────────────────────────────────


@dataclass(slots=True)
class HybridRetriever:
    """Lexical + dense retrieval fused with RRF.

    The dense lane is optional: ``embedder`` is any callable
    ``Sequence[str] -> Sequence[Sequence[float]]`` (a protocol, not a
    dependency), so tests inject a deterministic hashing embedder and the
    production engine injects a SentenceTransformer. With no embedder the
    retriever degrades to BM25 instead of failing.
    """

    embedder: Callable[[Sequence[str]], Sequence[Sequence[float]]] | None = None
    lexical_weight: float = 1.0
    vector_weight: float = 1.0
    _texts: dict[str, str] = field(default_factory=dict, repr=False)
    _vectors: dict[str, Sequence[float]] = field(default_factory=dict, repr=False)
    _lexical: BM25 = field(default_factory=BM25, repr=False)

    def index(self, doc_id: str, text: str) -> None:
        """(Re-)index one document in both lanes."""
        self._texts[doc_id] = text
        self._lexical.add(doc_id, text)
        if self.embedder is not None:
            vectors = self.embedder([text])
            if vectors:
                self._vectors[doc_id] = vectors[0]

    def remove(self, doc_id: str) -> bool:
        """Drop one document from both lanes."""
        self._texts.pop(doc_id, None)
        self._vectors.pop(doc_id, None)
        return self._lexical.remove(doc_id)

    def clear(self) -> None:
        self._texts.clear()
        self._vectors.clear()
        self._lexical.clear()

    def __len__(self) -> int:
        return len(self._texts)

    def text_of(self, doc_id: str) -> str:
        """Source text of an indexed document (empty when unknown)."""
        return self._texts.get(doc_id, "")

    def _vector_ranking(self, query: str, k: int) -> list[ScoredDoc]:
        if self.embedder is None or not self._vectors:
            return []
        vectors = self.embedder([query])
        if not vectors:
            return []
        query_vector = vectors[0]
        scored = [
            ScoredDoc(doc_id=doc_id, score=cosine_similarity(query_vector, vector))
            for doc_id, vector in self._vectors.items()
        ]
        scored = [item for item in scored if item.score > 0.0]
        scored.sort(key=lambda item: (-item.score, item.doc_id))
        return scored[:k]

    def search(self, query: str, k: int = 10) -> list[ScoredDoc]:
        """Fused top-``k`` for ``query`` (empty index → empty list)."""
        if not self._texts:
            return []
        lexical = self._lexical.search(query, k)
        vector = self._vector_ranking(query, k)
        rankings = [item for item in (lexical, vector) if item]
        if not rankings:
            return []
        return reciprocal_rank_fusion(
            rankings,
            weights=[self.lexical_weight, self.vector_weight][: len(rankings)],
            limit=k,
        )


# ── evaluation harness ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One labelled probe: a query plus the ids that answer it."""

    query: str
    relevant: tuple[str, ...]
    name: str = ""


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregate retrieval quality for one configuration."""

    k: int
    cases: int
    recall_at_k: float
    mrr_at_k: float
    hit_rate: float
    misses: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """JSON-serialisable view (used by the report printed in CI logs)."""
        return {
            "k": self.k,
            "cases": self.cases,
            "recall_at_k": round(self.recall_at_k, 4),
            "mrr_at_k": round(self.mrr_at_k, 4),
            "hit_rate": round(self.hit_rate, 4),
            "misses": list(self.misses),
        }


def evaluate_retrieval(
    cases: Iterable[EvalCase],
    retrieve: Callable[[str, int], Sequence[str]],
    *,
    k: int = 5,
) -> EvalReport:
    """Score a retriever over labelled cases.

    ``retrieve(query, k)`` must return document ids, best first — any backend
    (BM25-only, dense-only, hybrid, or the production engine) can be measured
    through the same two-line adapter.

    * ``recall@k`` — mean fraction of the relevant documents found in the top k
      (the primary metric: a RAG answer cannot be better than its context).
    * ``mrr@k`` — mean reciprocal rank of the first relevant hit (ranking).
    * ``hit_rate`` — fraction of cases with **at least one** relevant hit.
    """
    case_list = list(cases)
    if not case_list:
        return EvalReport(k=k, cases=0, recall_at_k=0.0, mrr_at_k=0.0, hit_rate=0.0, misses=())
    if k < 1:
        raise ValueError("k must be >= 1")

    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    hits = 0
    misses: list[str] = []

    for case in case_list:
        found = list(retrieve(case.query, k))
        relevant = set(case.relevant)
        matched = [doc_id for doc_id in found if doc_id in relevant]
        recalls.append(len(matched) / len(relevant) if relevant else 0.0)
        if matched:
            hits += 1
            first_rank = found.index(matched[0]) + 1
            reciprocal_ranks.append(1.0 / first_rank)
        else:
            reciprocal_ranks.append(0.0)
            misses.append(case.name or case.query)

    total = len(case_list)
    return EvalReport(
        k=k,
        cases=total,
        recall_at_k=sum(recalls) / total,
        mrr_at_k=sum(reciprocal_ranks) / total,
        hit_rate=hits / total,
        misses=tuple(misses),
    )
