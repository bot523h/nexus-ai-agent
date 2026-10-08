"""Unit tests for the pure retrieval core (:mod:`nexus_ai_agent.features.rag_core`).

Everything here is stdlib-only: no ``chromadb``, no torch, no network, no
Telegram. That is the point of the split — the algorithms that decide *what
the model gets to see* must be testable on a bare interpreter.

Covered: Unicode/Persian normalisation, lossless recursive chunking, Okapi
BM25 ranking, weighted reciprocal-rank fusion, cosine similarity, the hybrid
retriever (with an injected deterministic embedder), and the recall harness
arithmetic.
"""

from __future__ import annotations

import pytest

from nexus_ai_agent.features.rag_core import (
    BM25,
    EvalCase,
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

# ── normalisation & tokenisation ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("كتاب", "کتاب"),  # Arabic kaf → Persian keheh
        ("کتاب", "کتاب"),  # already canonical
        ("علي", "علی"),  # alef maksura → yeh
        ("مدرسه", "مدرسه"),  # teh marbuta → heh
        ("الْحَمْدُ", "الحمد"),  # harakat stripped
        ("۴۲", "42"),  # Extended Arabic-Indic digits
        ("٤٢", "42"),  # Arabic-Indic digits
        ("سلامـــ", "سلام"),  # tatweel dropped
        ("نیم‌فاصله", "نیم فاصله"),  # ZWNJ → space
        ("", ""),
    ],
)
def test_normalize_text_folds_persian_and_arabic_variants(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_tokenize_is_case_and_script_insensitive() -> None:
    assert tokenize("Hello, WORLD!") == ["hello", "world"]
    assert tokenize("کتاب و کتاب") == ["کتاب", "و", "کتاب"]
    assert tokenize("") == []
    assert tokenize("!!!") == []


def test_estimate_tokens_is_monotonic_and_bounded() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 4) == 1
    assert estimate_tokens("a" * 5) == 2
    assert estimate_tokens("a" * 100) == 25


# ── chunking ──────────────────────────────────────────────────────────────

PARAGRAPHS = "\n\n".join(f"Paragraph {i}. " + ("word " * 300) for i in range(1, 6))
PERSIAN = "\n\n".join(
    "این یک پاراگراف آزمایشی دربارهٔ بازیابی اطلاعات است. " + ("کلمه " * 200) for _ in range(4)
)
NO_SEPARATORS = "x" * 40_000


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("paragraphs", PARAGRAPHS),
        ("long_words", "supercalifragilistic " * 400),
        ("no_separators", NO_SEPARATORS),
        ("persian", PERSIAN),
        ("mixed_scripts", "سلام دنیا hello world. " * 300),
    ],
)
def test_chunking_is_lossless_and_bounded(label: str, text: str) -> None:
    """Every non-whitespace character lands in a chunk, and none is oversized."""
    chunks = chunk_text(text)
    assert chunks, f"{label}: no chunks produced"

    covered: set[int] = set()
    for chunk in chunks:
        covered.update(range(chunk.start, chunk.end))

    non_space = [index for index, char in enumerate(text) if not char.isspace()]
    assert not [index for index in non_space if index not in covered], (
        f"{label}: characters lost while chunking"
    )
    # A window never exceeds the target + its overlap.
    assert max(chunk.token_estimate for chunk in chunks) <= 512, f"{label}: oversized chunk"
    assert all(chunk.text.strip() for chunk in chunks), f"{label}: empty chunk emitted"


def test_chunks_are_addressable_in_source_coordinates() -> None:
    chunks = chunk_text(PARAGRAPHS)
    assert all(PARAGRAPHS[chunk.start : chunk.end] == chunk.text for chunk in chunks)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_chunking_is_deterministic() -> None:
    assert chunk_text(PERSIAN) == chunk_text(PERSIAN)


def test_consecutive_chunks_overlap() -> None:
    """An answer that straddles a boundary must stay retrievable."""
    chunks = chunk_text(PARAGRAPHS, chunk_tokens=64)
    assert len(chunks) > 3
    assert all(chunks[index].end - chunks[index + 1].start > 0 for index in range(len(chunks) - 1))


def test_overlap_ratio_is_configurable() -> None:
    tight = chunk_text(PARAGRAPHS, chunk_tokens=64, overlap_ratio=0.0)
    loose = chunk_text(PARAGRAPHS, chunk_tokens=64, overlap_ratio=0.4)
    assert len(loose) == len(tight)
    assert sum(chunk.end - chunk.start for chunk in loose) > sum(
        chunk.end - chunk.start for chunk in tight
    )


def test_short_text_stays_one_chunk() -> None:
    chunks = chunk_text("سلام دنیا")
    assert len(chunks) == 1
    assert chunks[0].text == "سلام دنیا"


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t"])
def test_blank_text_yields_no_chunks(text: str) -> None:
    assert chunk_text(text) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"chunk_tokens": 0}, "chunk_tokens must be >= 1"),
        ({"overlap_ratio": 1.0}, "overlap_ratio must be in"),
        ({"overlap_ratio": -0.1}, "overlap_ratio must be in"),
    ],
)
def test_chunking_rejects_nonsense(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        chunk_text("some text", **kwargs)  # type: ignore[arg-type]


def test_boundary_prefers_paragraphs_over_raw_characters() -> None:
    """With a big enough window, paragraph breaks become chunk breaks."""
    chunks = chunk_text(PARAGRAPHS, chunk_tokens=64)
    paragraph_starts = {0}
    cursor = 0
    while True:
        found = PARAGRAPHS.find("\n\n", cursor)
        if found == -1:
            break
        paragraph_starts.add(found + 2)
        cursor = found + 2
    assert any(chunk.start in paragraph_starts for chunk in chunks)


# ── BM25 ──────────────────────────────────────────────────────────────────


def _bm25_fixture() -> BM25:
    index = BM25()
    index.add("d1", "postgres index tuning and query planner")
    index.add("d2", "neural networks and machine learning")
    index.add("d3", "docker compose for service deployment")
    return index


def test_bm25_ranks_the_relevant_document_first() -> None:
    index = _bm25_fixture()
    hits = index.search("postgres index", k=3)
    assert [hit.doc_id for hit in hits][0] == "d1"
    assert all(hit.score > 0 for hit in hits)


def test_bm25_rewards_rare_terms_over_common_ones() -> None:
    index = BM25()
    index.add("common", "the index the index the index docker")
    index.add("rare", "postgres query planner")
    hits = index.search("postgres", k=2)
    assert hits[0].doc_id == "rare"


def test_bm25_matches_across_persian_and_arabic_spellings() -> None:
    index = BM25()
    index.add("fa", "کتابخانه مرکزی دانشگاه")
    index.add("ar", "مكتبة الحرم")
    assert [hit.doc_id for hit in index.search("كتابخانه", k=2)][0] == "fa"
    assert [hit.doc_id for hit in index.search("کتابخانه", k=2)][0] == "fa"


def test_bm25_matches_across_digit_scripts() -> None:
    index = BM25()
    index.add("doc", "نسخه ۴۲ منتشر شد")
    assert index.search("42", k=1)[0].doc_id == "doc"


def test_bm25_remove_and_clear() -> None:
    index = _bm25_fixture()
    assert index.remove("d1") is True
    assert index.remove("d1") is False
    assert len(index) == 2
    assert [hit.doc_id for hit in index.search("postgres", k=3)] == []
    index.clear()
    assert len(index) == 0
    assert index.search("docker", k=3) == []


def test_bm25_add_replaces_the_previous_text() -> None:
    index = BM25()
    index.add("d1", "postgres tuning")
    index.add("d1", "kubernetes helm charts")
    assert len(index) == 1
    assert index.search("postgres", k=1) == []
    assert index.search("helm", k=1)[0].doc_id == "d1"


def test_bm25_empty_and_unknown_queries() -> None:
    index = _bm25_fixture()
    assert index.search("", k=3) == []
    assert index.search("   ", k=3) == []
    assert index.search("zzzz not-present", k=3) == []
    assert index.average_length > 0


# ── fusion ────────────────────────────────────────────────────────────────


def test_reciprocal_rank_fusion_promotes_documents_present_in_both_lanes() -> None:
    lexical = [
        ScoredDoc("only_lexical", 0.9),
        ScoredDoc("both_high", 0.8),
        ScoredDoc("both_low", 0.1),
    ]
    dense = [ScoredDoc("both_high", 0.95), ScoredDoc("both_low", 0.4)]
    fused = reciprocal_rank_fusion([lexical, dense])

    # both_high: 1/62 + 1/61 · both_low: 1/63 + 1/62 · only_lexical: 1/61
    assert [item.doc_id for item in fused] == ["both_high", "both_low", "only_lexical"]
    assert fused[0].score > fused[1].score > fused[2].score


def test_reciprocal_rank_fusion_respects_weights() -> None:
    lexical = [ScoredDoc("lex", 1.0)]
    dense = [ScoredDoc("vec", 1.0)]
    biased = reciprocal_rank_fusion([lexical, dense], weights=[10.0, 1.0])
    assert biased[0].doc_id == "lex"


def test_reciprocal_rank_fusion_edge_cases() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[]]) == []
    with pytest.raises(ValueError, match="weights must match"):
        reciprocal_rank_fusion([[ScoredDoc("a", 1.0)]], weights=[1.0, 2.0])
    assert len(reciprocal_rank_fusion([[ScoredDoc(f"d{i}", 1.0) for i in range(10)]], limit=3)) == 3


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ([1.0, 0.0], [1.0, 0.0], 1.0),
        ([1.0, 0.0], [0.0, 1.0], 0.0),
        ([1.0, 1.0], [1.0, 1.0], 1.0),
        ([], [], 0.0),
        ([1.0], [0.0, 0.0], 0.0),
        ([0.0], [1.0], 0.0),
    ],
)
def test_cosine_similarity(left: list[float], right: list[float], expected: float) -> None:
    assert cosine_similarity(left, right) == pytest.approx(expected)


# ── hybrid retriever ──────────────────────────────────────────────────────


class BucketEmbedder:
    """Deterministic bag-of-synonyms embedder (no model, no network).

    Every synonym bucket is one dimension, so two texts that share a bucket
    (``car`` / ``automobile``, ``llm`` / ``language model``) have cosine 1.0
    even though they share **zero** tokens — which is exactly the case a
    lexical-only retriever cannot solve.
    """

    def __init__(self, buckets: dict[str, tuple[str, ...]]) -> None:
        self.buckets = buckets
        self._names = tuple(sorted(buckets))

    def __call__(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            tokens = set(tokenize(text))
            vectors.append(
                [1.0 if tokens & set(self.buckets[name]) else 0.0 for name in self._names]
            )
        return vectors


VEHICLE_BUCKETS = {
    "vehicle": ("car", "automobile", "vehicle", "خودرو", "ماشین"),
    "fruit": ("apple", "banana", "fruit", "سیب"),
    "weather": ("rain", "weather", "باران"),
}


def test_hybrid_retriever_finds_a_paraphrase_bm25_cannot() -> None:
    corpus = {
        "d1": "the automobile needed a new battery",
        "d2": "apple pie recipe with cinnamon",
        "d3": "rain was forecast for the whole week",
    }
    lexical_only = HybridRetriever(embedder=None)
    hybrid = HybridRetriever(embedder=BucketEmbedder(VEHICLE_BUCKETS), vector_weight=2.0)
    for doc_id, text in corpus.items():
        lexical_only.index(doc_id, text)
        hybrid.index(doc_id, text)

    assert lexical_only.search("car", k=3) == []  # no shared token → lexical miss
    assert hybrid.search("car", k=3)[0].doc_id == "d1"


def test_hybrid_retriever_keeps_lexical_order_when_dense_is_absent() -> None:
    retriever = HybridRetriever(embedder=None)
    retriever.index("d1", "postgres index tuning")
    retriever.index("d2", "kubernetes helm")
    assert retriever.search("postgres", k=2)[0].doc_id == "d1"
    assert retriever.search("unknown term", k=2) == []
    assert len(retriever) == 2
    assert retriever.text_of("d1") == "postgres index tuning"
    assert retriever.text_of("missing") == ""


def test_hybrid_retriever_remove_and_clear() -> None:
    retriever = HybridRetriever(embedder=BucketEmbedder(VEHICLE_BUCKETS))
    retriever.index("d1", "the automobile")
    retriever.index("d2", "apple")
    assert retriever.remove("d1") is True
    assert retriever.remove("d1") is False
    assert [hit.doc_id for hit in retriever.search("car", k=2)] == []
    retriever.clear()
    assert len(retriever) == 0
    assert retriever.search("apple", k=2) == []


# ── evaluation harness arithmetic ─────────────────────────────────────────


def test_evaluate_retrieval_computes_recall_mrr_and_hit_rate() -> None:
    cases = [
        EvalCase(query="a", relevant=("d1",), name="first"),
        EvalCase(query="b", relevant=("d2",), name="second"),
    ]
    answers = {"a": ["d1"], "b": ["d3", "d2"]}

    report = evaluate_retrieval(cases, lambda query, k: answers[query][:k], k=3)

    assert report.cases == 2
    assert report.recall_at_k == 1.0  # both relevant docs found
    assert report.mrr_at_k == pytest.approx(0.75)  # 1/1 and 1/2
    assert report.hit_rate == 1.0
    assert report.misses == ()


def test_evaluate_retrieval_counts_partial_recall() -> None:
    cases = [EvalCase(query="q", relevant=("d1", "d2", "d3"))]
    report = evaluate_retrieval(cases, lambda *_: ["d1"], k=5)
    assert report.recall_at_k == pytest.approx(1 / 3)
    assert report.hit_rate == 1.0


def test_evaluate_retrieval_records_misses() -> None:
    cases = [EvalCase(query="q", relevant=("d1",), name="named")]
    report = evaluate_retrieval(cases, lambda *_: ["d9"], k=5)
    assert report.recall_at_k == 0.0
    assert report.mrr_at_k == 0.0
    assert report.hit_rate == 0.0
    assert report.misses == ("named",)


def test_evaluate_retrieval_edge_cases() -> None:
    empty = evaluate_retrieval([], lambda *_: [], k=5)
    assert (empty.cases, empty.recall_at_k, empty.hit_rate) == (0, 0.0, 0.0)
    with pytest.raises(ValueError, match="k must be >= 1"):
        evaluate_retrieval([EvalCase(query="q", relevant=("d1",))], lambda *_: [], k=0)


def test_evaluate_retrieval_report_is_serialisable() -> None:
    cases = [EvalCase(query="q", relevant=("d1",))]
    payload = evaluate_retrieval(cases, lambda *_: ["d1"], k=5).as_dict()
    assert set(payload) == {"k", "cases", "recall_at_k", "mrr_at_k", "hit_rate", "misses"}
    assert payload["recall_at_k"] == 1.0
