"""Hybrid lexical-dense retrieval with split-conformal abstention.

BM25+ provides sublinear lexical scoring, the Riemannian fallback provides
compensated angular similarity, weighted reciprocal-rank fusion combines them,
and Jensen-Shannon MMR removes redundant chunks.  A split-conformal quantile
then gates the prediction set: when no document clears the calibrated
threshold the retriever abstains instead of returning low-relevance noise.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

from nexus_ai_agent.creative.perception.radiometric_scene_cut import (
    js_distance,
)
from nexus_ai_agent.memory.riemannian_vector_fallback import (
    compensated_l2_norm,
    cosine_similarity,
)

__all__ = [
    "BM25Plus",
    "ConformalCalibrator",
    "ConformalHybridRetriever",
    "Document",
    "EpistemicRetrievalStatus",
    "HybridRetriever",
    "RetrievalError",
    "RetrievalResult",
    "RetrievalValidationError",
    "RetrievedDocument",
    "tokenize",
]

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_DEFAULT_K1: Final[float] = 1.2
_DEFAULT_B: Final[float] = 0.75
_DEFAULT_DELTA: Final[float] = 1.0


class RetrievalError(ValueError):
    """Raised when retrieval input violates a scoring or calibration invariant."""


class RetrievalValidationError(RetrievalError):
    """Raised when a document or query has no lexical evidence to score."""


def tokenize(text: str) -> tuple[str, ...]:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return tuple(_TOKEN_PATTERN.findall(text.lower()))


def _finite(value: float, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise RetrievalError(f"{label} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    text: str
    embedding: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.doc_id, str) or not self.doc_id:
            raise RetrievalError("doc_id must be a non-empty string")
        if not isinstance(self.text, str) or not self.text.strip():
            raise RetrievalValidationError("text must be a non-empty string")
        if not tokenize(self.text):
            raise RetrievalValidationError(
                "document text must contain at least one alphanumeric token"
            )
        if self.embedding is not None:
            vector = tuple(self.embedding)
            if not vector:
                raise RetrievalError("embedding must not be empty")
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise RetrievalError("embedding entries must be real numbers")
                if not math.isfinite(float(value)):
                    raise RetrievalError("embedding entries must be finite")
            if compensated_l2_norm(vector) < 1e-12:
                raise RetrievalError("embedding must have non-zero norm")
            object.__setattr__(self, "embedding", tuple(float(value) for value in vector))


class BM25Plus:
    """Okapi BM25+ with Robertson-Sparck Jones IDF.

    The lower-bounded TF term is ``tf*(k1+1)/(tf+k1*(1-b+b*|D|/avgdl)) + delta``.
    This implementation adds ``delta`` only when the query term occurs in the
    document, preserving the historical zero score for absent terms.
    """

    def __init__(
        self,
        documents: Sequence[Document],
        *,
        k1: float = _DEFAULT_K1,
        b: float = _DEFAULT_B,
        delta: float = _DEFAULT_DELTA,
    ) -> None:
        if not documents:
            raise RetrievalError("at least one document is required")
        if any(not isinstance(document, Document) for document in documents):
            raise TypeError("documents must contain Document values")
        if len({document.doc_id for document in documents}) != len(documents):
            raise RetrievalError("document ids must be unique")
        if not math.isfinite(k1) or k1 <= 0.0:
            raise RetrievalError("k1 must be finite and positive")
        if not math.isfinite(b) or not 0.0 <= b <= 1.0:
            raise RetrievalError("b must lie inside [0, 1]")
        if not math.isfinite(delta) or delta < 0.0:
            raise RetrievalError("delta must be finite and non-negative")
        self._documents = tuple(documents)
        self._k1 = k1
        self._b = b
        self._delta = delta
        self._tokens = {document.doc_id: tokenize(document.text) for document in self._documents}
        self._lengths = {doc_id: len(tokens) for doc_id, tokens in self._tokens.items()}
        self._average_length = sum(self._lengths.values()) / len(self._lengths)
        frequencies: dict[str, int] = {}
        for tokens in self._tokens.values():
            for term in set(tokens):
                frequencies[term] = frequencies.get(term, 0) + 1
        self._document_frequency = frequencies

    @property
    def documents(self) -> tuple[Document, ...]:
        return self._documents

    def _idf(self, term: str) -> float:
        count = len(self._documents)
        frequency = self._document_frequency.get(term, 0)
        return math.log(1.0 + (count - frequency + 0.5) / (frequency + 0.5))

    def scores(self, query: str) -> Mapping[str, float]:
        query_terms = tokenize(query)
        if not query_terms:
            raise RetrievalError("query must contain at least one token")
        results: dict[str, float] = {}
        for document in self._documents:
            tokens = self._tokens[document.doc_id]
            length = self._lengths[document.doc_id]
            total = 0.0
            for term in query_terms:
                term_frequency = tokens.count(term)
                if term_frequency == 0:
                    continue
                idf = self._idf(term)
                denominator = term_frequency + self._k1 * (
                    1.0 - self._b + self._b * length / self._average_length
                )
                total += idf * (term_frequency * (self._k1 + 1.0) / denominator + self._delta)
            results[document.doc_id] = total
        return results


def _angular_similarity(first: Sequence[float], second: Sequence[float]) -> float:
    if len(first) != len(second):
        raise RetrievalError("embedding dimensions must match")
    if compensated_l2_norm(first) < 1e-12 or compensated_l2_norm(second) < 1e-12:
        raise RetrievalError("embeddings must have non-zero norm")
    cosine = cosine_similarity(first, second)
    return 1.0 - math.acos(cosine) / math.pi


@dataclass(frozen=True, slots=True)
class ConformalCalibrator:
    """Split-conformal calibration of dense non-conformity scores."""

    scores: tuple[float, ...]
    alpha: float = 0.1

    def __post_init__(self) -> None:
        scores = tuple(self.scores)
        if not scores:
            raise RetrievalError("calibration scores must not be empty")
        for score in scores:
            _finite(score, "calibration score")
        if not math.isfinite(self.alpha) or not 0.0 < self.alpha < 1.0:
            raise RetrievalError("alpha must lie inside (0, 1)")
        object.__setattr__(self, "scores", tuple(float(score) for score in scores))

    @property
    def quantile(self) -> float:
        """Finite-sample quantile ``s_(ceil((n+1)(1-alpha)))``; ``inf`` if out of range."""

        count = len(self.scores)
        rank = math.ceil((count + 1) * (1.0 - self.alpha))
        if rank > count:
            return math.inf
        if rank < 1:  # pragma: no cover - alpha < 1 guarantees rank >= 1
            rank = 1
        return sorted(self.scores)[rank - 1]

    def prediction_set(self, nonconformities: Mapping[str, float]) -> frozenset[str]:
        threshold = self.quantile
        allowed = []
        for doc_id, score in nonconformities.items():
            if not isinstance(doc_id, str) or not doc_id:
                raise RetrievalError("doc_id must be a non-empty string")
            value = _finite(score, "nonconformity")
            if value <= threshold:
                allowed.append(doc_id)
        return frozenset(allowed)


class EpistemicRetrievalStatus(Enum):
    OK = "OK"
    ABSTAIN_INSUFFICIENT_EVIDENCE = "ABSTAIN_INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class RetrievedDocument:
    doc_id: str
    rrf_score: float
    lexical_rank: int | None
    dense_rank: int | None
    nonconformity: float
    mmr_score: float

    def __post_init__(self) -> None:
        if not isinstance(self.doc_id, str) or not self.doc_id:
            raise RetrievalError("doc_id must be a non-empty string")
        for name in ("rrf_score", "nonconformity", "mmr_score"):
            if not math.isfinite(getattr(self, name)):
                raise RetrievalError(f"{name} must be finite")
        if self.nonconformity < 0.0 or self.rrf_score < 0.0:
            raise RetrievalError("scores must be non-negative")
        for name in ("lexical_rank", "dense_rank"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise RetrievalError(f"{name} must be a positive int or None")


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    status: EpistemicRetrievalStatus
    documents: tuple[RetrievedDocument, ...]
    conformal_quantile: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, EpistemicRetrievalStatus):
            raise RetrievalError("status must be an EpistemicRetrievalStatus")
        documents = tuple(self.documents)
        if any(not isinstance(document, RetrievedDocument) for document in documents):
            raise TypeError("documents must contain RetrievedDocument values")
        if self.status is EpistemicRetrievalStatus.ABSTAIN_INSUFFICIENT_EVIDENCE:
            if documents:
                raise RetrievalError("abstention must not carry documents")
        elif not documents:
            raise RetrievalError("OK results must carry at least one document")
        if self.conformal_quantile is not None and math.isnan(self.conformal_quantile):
            raise RetrievalError("conformal_quantile must be finite or +infinity")
        object.__setattr__(self, "documents", documents)

    @property
    def abstained(self) -> bool:
        return self.status is EpistemicRetrievalStatus.ABSTAIN_INSUFFICIENT_EVIDENCE


class HybridRetriever:
    """Weighted RRF over BM25+ and Riemannian dense ranks, then JS-MMR."""

    def __init__(self, documents: Sequence[Document]) -> None:
        self._documents = tuple(documents)
        self._lexical = BM25Plus(self._documents)
        self._vocabulary = tuple(
            sorted({term for document in self._documents for term in tokenize(document.text)})
        )
        self._index = {document.doc_id: document for document in self._documents}

    @property
    def documents(self) -> tuple[Document, ...]:
        return self._documents

    def lexical_scores(self, query: str) -> Mapping[str, float]:
        return self._lexical.scores(query)

    def dense_scores(self, query_embedding: Sequence[float]) -> Mapping[str, float]:
        query = tuple(float(value) for value in query_embedding)
        if not query:
            raise RetrievalError("query embedding must not be empty")
        for value in query:
            if not math.isfinite(value):
                raise RetrievalError("query embedding entries must be finite")
        results: dict[str, float] = {}
        for document in self._documents:
            if document.embedding is None:
                continue
            results[document.doc_id] = _angular_similarity(query, document.embedding)
        return results

    def _term_distribution(self, doc_id: str) -> tuple[float, ...]:
        tokens = tokenize(self._index[doc_id].text)
        if not self._vocabulary or not tokens:
            raise RetrievalValidationError("cannot compute term distribution for tokenless text")
        counts = {term: 0 for term in self._vocabulary}
        for term in tokens:
            counts[term] += 1
        total = float(len(tokens))
        return tuple(counts[term] / total for term in self._vocabulary)

    def _mmr(
        self,
        candidates: Mapping[str, float],
        limit: int,
        lambda_: float,
    ) -> tuple[tuple[str, float], ...]:
        if not candidates:
            return ()
        maximum = max(candidates.values())
        relevance = {
            doc_id: (value / maximum if maximum > 0.0 else 0.0)
            for doc_id, value in candidates.items()
        }
        distributions = {doc_id: self._term_distribution(doc_id) for doc_id in candidates}
        selected: list[tuple[str, float]] = []
        remaining = set(candidates)
        while remaining and len(selected) < limit:
            best_id: str | None = None
            best_value = -math.inf
            for doc_id in sorted(remaining):
                if selected:
                    diversity = max(
                        js_distance(distributions[doc_id], distributions[chosen])
                        for chosen, _score in selected
                    )
                else:
                    diversity = 0.0
                value = lambda_ * relevance[doc_id] + (1.0 - lambda_) * diversity
                if value > best_value:
                    best_value = value
                    best_id = doc_id
            if best_id is None:  # pragma: no cover - remaining is non-empty
                break
            selected.append((best_id, best_value))
            remaining.remove(best_id)
        return tuple(selected)

    def search(
        self,
        query: str,
        query_embedding: Sequence[float],
        *,
        calibration: ConformalCalibrator | None = None,
        limit: int = 5,
        rrf_k: int = 60,
        lexical_weight: float = 1.0,
        dense_weight: float = 1.0,
        mmr_lambda: float = 0.7,
    ) -> RetrievalResult:
        if not tokenize(query):
            raise RetrievalValidationError(
                "query_text must contain at least one alphanumeric token"
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise RetrievalError("limit must be a positive int")
        if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k < 0:
            raise RetrievalError("rrf_k must be a non-negative int")
        if not math.isfinite(lexical_weight) or lexical_weight < 0.0:
            raise RetrievalError("lexical_weight must be finite and non-negative")
        if not math.isfinite(dense_weight) or dense_weight < 0.0:
            raise RetrievalError("dense_weight must be finite and non-negative")
        if not math.isfinite(mmr_lambda) or not 0.0 <= mmr_lambda <= 1.0:
            raise RetrievalError("mmr_lambda must lie inside [0, 1]")

        lexical = self._lexical.scores(query)
        dense = self.dense_scores(query_embedding)
        nonconformities = {doc_id: 1.0 - similarity for doc_id, similarity in dense.items()}
        quantile: float | None = None
        if calibration is not None:
            allowed = calibration.prediction_set(nonconformities)
            quantile = calibration.quantile
        else:
            allowed = frozenset(self._index)

        lexical_ranking = sorted(
            (doc_id for doc_id, score in lexical.items() if score > 0.0),
            key=lambda doc_id: (-lexical[doc_id], doc_id),
        )
        dense_ranking = sorted(dense, key=lambda doc_id: (-dense[doc_id], doc_id))
        lexical_rank = {doc_id: rank for rank, doc_id in enumerate(lexical_ranking, 1)}
        dense_rank = {doc_id: rank for rank, doc_id in enumerate(dense_ranking, 1)}
        candidates: dict[str, float] = {}
        for doc_id in (lexical_rank.keys() | dense_rank.keys()) & allowed:
            fused = 0.0
            if doc_id in lexical_rank:
                fused += lexical_weight / (rrf_k + lexical_rank[doc_id])
            if doc_id in dense_rank:
                fused += dense_weight / (rrf_k + dense_rank[doc_id])
            candidates[doc_id] = fused
        if not candidates:
            return RetrievalResult(
                status=EpistemicRetrievalStatus.ABSTAIN_INSUFFICIENT_EVIDENCE,
                documents=(),
                conformal_quantile=quantile,
            )
        ranked = self._mmr(candidates, limit, mmr_lambda)
        documents = tuple(
            RetrievedDocument(
                doc_id=doc_id,
                rrf_score=candidates[doc_id],
                lexical_rank=lexical_rank.get(doc_id),
                dense_rank=dense_rank.get(doc_id),
                nonconformity=nonconformities.get(doc_id, 1.0),
                mmr_score=score,
            )
            for doc_id, score in ranked
        )
        return RetrievalResult(
            status=EpistemicRetrievalStatus.OK,
            documents=documents,
            conformal_quantile=quantile,
        )


ConformalHybridRetriever = HybridRetriever
