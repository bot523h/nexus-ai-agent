"""Wave 8: BM25+/RRF hybrid retrieval and split-conformal abstention."""

from __future__ import annotations

import math

import pytest

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

DOCS = (
    Document("d1", "apple banana apple", (1.0, 0.0, 0.0)),
    Document("d2", "apple cherry", (0.9, 0.1, 0.0)),
    Document("d3", "delta echo", (0.0, 1.0, 0.0)),
)


def test_tokenize() -> None:
    assert tokenize("Apple, banana-42!") == ("apple", "banana", "42")
    assert tokenize("") == ()
    with pytest.raises(TypeError):
        tokenize(1)  # type: ignore[arg-type]


def test_document_validation() -> None:
    Document("d", "text")
    with pytest.raises(RetrievalError):
        Document("", "text")
    with pytest.raises(RetrievalError):
        Document("d", "")
    with pytest.raises(RetrievalError):
        Document("d", "text", ())
    with pytest.raises(RetrievalError):
        Document("d", "text", (0.0, 0.0))
    with pytest.raises(RetrievalError):
        Document("d", "text", (math.nan, 1.0))
    with pytest.raises(RetrievalError):
        Document("d", "text", (True, 1.0))  # type: ignore[arg-type]


def test_bm25_scores_and_validation() -> None:
    index = BM25Plus(DOCS)
    scores = index.scores("apple")
    assert scores["d1"] > scores["d2"] > 0.0
    assert scores["d3"] == 0.0
    assert index.documents == DOCS
    assert BM25Plus(DOCS, k1=1.5, b=0.5, delta=0.5).scores("apple")["d1"] > 0
    with pytest.raises(RetrievalError):
        BM25Plus(())
    with pytest.raises(RetrievalError):
        BM25Plus((DOCS[0], DOCS[0]))
    with pytest.raises(TypeError):
        BM25Plus(("x",))  # type: ignore[arg-type]
    with pytest.raises(RetrievalError):
        BM25Plus(DOCS, k1=0.0)
    with pytest.raises(RetrievalError):
        BM25Plus(DOCS, b=1.5)
    with pytest.raises(RetrievalError):
        BM25Plus(DOCS, delta=-1.0)
    with pytest.raises(RetrievalError):
        index.scores("!!!")


def test_dense_scores() -> None:
    retriever = HybridRetriever(DOCS)
    scores = retriever.dense_scores((1.0, 0.0, 0.0))
    assert scores["d1"] == pytest.approx(1.0)
    assert scores["d2"] > scores["d3"]
    orthogonal = retriever.dense_scores((0.0, 0.0, 1.0))
    assert orthogonal["d1"] == pytest.approx(0.5)
    with pytest.raises(RetrievalError):
        retriever.dense_scores(())
    with pytest.raises(RetrievalError):
        retriever.dense_scores((math.nan, 0.0, 0.0))
    with pytest.raises(RetrievalError):
        retriever.dense_scores((1.0, 0.0))
    with pytest.raises(RetrievalError):
        retriever.dense_scores((0.0, 0.0, 0.0))


def test_conformal_quantile_and_prediction_set() -> None:
    calibrator = ConformalCalibrator((0.4, 0.1, 0.3, 0.2), alpha=0.25)
    assert calibrator.quantile == pytest.approx(0.4)
    allowed = calibrator.prediction_set({"a": 0.0, "b": 0.4, "c": 0.5})
    assert allowed == frozenset({"a", "b"})
    infinite = ConformalCalibrator((0.0, 0.1), alpha=0.1)
    assert infinite.quantile == math.inf
    assert infinite.prediction_set({"a": 10.0}) == frozenset({"a"})
    with pytest.raises(RetrievalError):
        ConformalCalibrator(())
    with pytest.raises(RetrievalError):
        ConformalCalibrator((math.nan,))
    with pytest.raises(RetrievalError):
        ConformalCalibrator((0.1,), alpha=0.0)
    with pytest.raises(RetrievalError):
        ConformalCalibrator((0.1,), alpha=1.0)
    with pytest.raises(RetrievalError):
        calibrator.prediction_set({"": 0.0})
    with pytest.raises(RetrievalError):
        calibrator.prediction_set({"a": math.nan})


def test_hybrid_search_and_rrf_ordering() -> None:
    retriever = HybridRetriever(DOCS)
    result = retriever.search("apple", (1.0, 0.0, 0.0), limit=3)
    assert result.status is EpistemicRetrievalStatus.OK
    assert result.abstained is False
    assert [document.doc_id for document in result.documents] == ["d1", "d2", "d3"]
    top = result.documents[0]
    assert top.lexical_rank == 1 and top.dense_rank == 1
    assert top.nonconformity == pytest.approx(0.0)
    assert result.conformal_quantile is None
    assert retriever.documents == DOCS
    assert retriever.lexical_scores("apple")["d1"] > 0


def test_mmr_prefers_diverse_documents() -> None:
    documents = (
        Document("a", "apple banana", (1.0, 0.0, 0.0)),
        Document("b", "apple banana", (1.0, 0.0, 0.0)),
        Document("c", "delta echo", (0.0, 1.0, 0.0)),
    )
    retriever = HybridRetriever(documents)
    result = retriever.search("apple", (1.0, 0.0, 0.0), limit=2, mmr_lambda=0.1)
    assert result.documents[0].doc_id in {"a", "b"}
    assert result.documents[1].doc_id == "c"


def test_conformal_gate_filters_and_abstains() -> None:
    retriever = HybridRetriever(DOCS)
    calibrator = ConformalCalibrator((0.0, 0.05), alpha=0.5)
    result = retriever.search("apple", (1.0, 0.0, 0.0), calibration=calibrator, limit=3)
    assert result.status is EpistemicRetrievalStatus.OK
    assert {document.doc_id for document in result.documents} == {"d1", "d2"}
    assert result.conformal_quantile == pytest.approx(0.05)

    strict = ConformalCalibrator((0.0, 0.0), alpha=0.5)
    abstained = retriever.search("apple", (1.0, 1.0, 0.0), calibration=strict, limit=3)
    assert abstained.status is EpistemicRetrievalStatus.ABSTAIN_INSUFFICIENT_EVIDENCE
    assert abstained.documents == ()
    assert abstained.abstained is True


def test_search_validation_and_empty_mmr() -> None:
    retriever = HybridRetriever(DOCS)
    with pytest.raises(RetrievalError):
        retriever.search("apple", (1.0, 0.0, 0.0), limit=0)
    with pytest.raises(RetrievalError):
        retriever.search("apple", (1.0, 0.0, 0.0), limit=True)  # type: ignore[arg-type]
    with pytest.raises(RetrievalError):
        retriever.search("apple", (1.0, 0.0, 0.0), rrf_k=-1)
    with pytest.raises(RetrievalError):
        retriever.search("apple", (1.0, 0.0, 0.0), rrf_k=True)  # type: ignore[arg-type]
    with pytest.raises(RetrievalError):
        retriever.search("apple", (1.0, 0.0, 0.0), lexical_weight=-1.0)
    with pytest.raises(RetrievalError):
        retriever.search("apple", (1.0, 0.0, 0.0), dense_weight=-1.0)
    with pytest.raises(RetrievalError):
        retriever.search("apple", (1.0, 0.0, 0.0), mmr_lambda=1.1)
    assert retriever._mmr({}, 5, 0.5) == ()
    without_embedding = HybridRetriever(
        (Document("plain", "apple"), Document("dense", "apple", (1.0, 0.0)))
    )
    assert set(without_embedding.dense_scores((1.0, 0.0))) == {"dense"}
    lexical_only = without_embedding.search("apple", (1.0, 0.0), limit=5)
    assert {document.doc_id for document in lexical_only.documents} == {"plain", "dense"}


def test_result_validation() -> None:
    document = RetrievedDocument("d1", 0.5, 1, 1, 0.0, 0.5)
    assert document.doc_id == "d1"
    with pytest.raises(RetrievalError):
        RetrievedDocument("", 0.5, 1, 1, 0.0, 0.5)
    with pytest.raises(RetrievalError):
        RetrievedDocument("d1", math.nan, 1, 1, 0.0, 0.5)
    with pytest.raises(RetrievalError):
        RetrievedDocument("d1", 0.5, 0, 1, 0.0, 0.5)
    with pytest.raises(RetrievalError):
        RetrievedDocument("d1", -0.5, 1, 1, 0.0, 0.5)
    with pytest.raises(RetrievalError):
        RetrievedDocument("d1", 0.5, 1, True, 0.0, 0.5)  # type: ignore[arg-type]
    RetrievalResult(EpistemicRetrievalStatus.OK, (document,), None)
    RetrievalResult(EpistemicRetrievalStatus.ABSTAIN_INSUFFICIENT_EVIDENCE, (), math.inf)
    with pytest.raises(RetrievalError):
        RetrievalResult("OK", (document,))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RetrievalResult(EpistemicRetrievalStatus.OK, ("x",))  # type: ignore[arg-type]
    with pytest.raises(RetrievalError):
        RetrievalResult(EpistemicRetrievalStatus.ABSTAIN_INSUFFICIENT_EVIDENCE, (document,))
    with pytest.raises(RetrievalError):
        RetrievalResult(EpistemicRetrievalStatus.OK, ())
    with pytest.raises(RetrievalError):
        RetrievalResult(EpistemicRetrievalStatus.OK, (document,), math.nan)
