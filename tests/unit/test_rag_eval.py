"""Retrieval-quality tests: a fixed corpus, labelled cases, measured recall.

Nothing here touches ``chromadb``, torch or the network: the vector store is a
:class:`FakeClient` injected into :class:`AdvancedRAGEngine`, and the dense
lane is a deterministic bucket embedder. What *is* real is everything that
decides what the model sees — chunking, indexing, fusion, ranking.

The point of the file is comparative: a lexical-only retriever cannot answer a
paraphrased query, and the numbers below prove the hybrid lane fixes that
without regressing the exact-match queries.
"""

from __future__ import annotations

import inspect
import sys
from typing import Any

import pytest

from nexus_ai_agent.features.rag import (
    AdvancedRAGEngine,
    DocumentInfo,
    RAGUnavailable,
    RetrievedChunk,
)
from nexus_ai_agent.features.rag_core import (
    BM25,
    EvalCase,
    HybridRetriever,
    ScoredDoc,
    cosine_similarity,
    evaluate_retrieval,
    tokenize,
)

# ── the fixed corpus ──────────────────────────────────────────────────────
# Eight short documents, deliberately bilingual, plus six labelled probes.
# Frozen on purpose: the thresholds below are only meaningful while the corpus
# is, so changing it means re-baselining the numbers in the assertions.

CORPUS: dict[str, str] = {
    "crypto": (
        "رمزنگاری کلید عمومی با الگوریتم RSA بر پایهٔ سختی تجزیه اعداد بزرگ است. "
        "امضای دیجیتال ed25519 برای مانیفست تحویل استفاده می‌شود."
    ),
    "ml": (
        "یادگیری ماشین و شبکه‌های عصبی کانولوشن برای دسته‌بندی تصویر. "
        "آموزش مدل با نرخ یادگیری کوچک پایدارتر است."
    ),
    "db": (
        "تنظیمات پایگاه داده پستگرس: ایندکس B-tree برای ستون‌های پرتکرار، "
        "تحلیلگر پرسش و vacuum خودکار."
    ),
    "sales": "گزارش فروش سه‌ماهه: رشد ۱۲ درصدی درآمد و کاهش ۳ درصدی هزینه جذب مشتری.",
    "deploy": "docker compose برای استقرار سرویس‌ها و اجرای مهاجرت پایگاه داده به‌کار می‌رود.",
    "auth": "امنیت: احراز هویت دو مرحله‌ای، مدیریت نشست و چرخش کلیدهای امضا.",
    "tax": "قوانین مالیاتی سالانه برای شرکت‌های کوچک و نحوهٔ ثبت اظهارنامه.",
    # Deliberately avoids the words in the "offline language model" probe:
    # the dense lane must earn this one, the lexical lane cannot.
    "local-llm": "Running llama.cpp on a laptop keeps every weight on disk, no API round-trip.",
}

CASES: tuple[EvalCase, ...] = (
    EvalCase("ایندکس پایگاه داده پستگرس", ("db",), "db-index"),
    EvalCase("شبکه عصبی کانولوشن", ("ml",), "ml-cnn"),
    EvalCase("استقرار سرویس با docker compose", ("deploy",), "deploy-compose"),
    EvalCase("احراز هویت دو مرحله‌ای", ("auth",), "auth-2fa"),
    EvalCase("offline language model", ("local-llm",), "llm-synonym"),
    EvalCase("گزارش فروش درآمد", ("sales",), "sales-report"),
)

#: Bucket → surface forms. Two texts sharing a bucket have cosine 1.0 even with
#: zero shared tokens, which is exactly the paraphrase case BM25 cannot solve.
BUCKETS: dict[str, tuple[str, ...]] = {
    "db": ("postgres", "database", "پایگاه", "داده", "ایندکس", "index", "sql"),
    "ml": ("neural", "network", "learning", "شبکه", "عصبی", "یادگیری", "model"),
    "deploy": ("docker", "compose", "deploy", "استقرار", "service", "سرویس"),
    "auth": ("auth", "authentication", "session", "احراز", "هویت", "نشست", "mfa"),
    "llm": ("language", "model", "llm", "llama", "offline", "local", "server"),
    "sales": ("sales", "revenue", "فروش", "درآمد"),
}


class BucketEmbedder:
    """Deterministic bag-of-synonyms embedder (see :data:`BUCKETS`)."""

    def __init__(self, buckets: dict[str, tuple[str, ...]] = BUCKETS) -> None:
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


# ── a hermetic vector store ───────────────────────────────────────────────


class FakeCollection:
    """In-memory stand-in for a Chroma collection (add/get/query/delete)."""

    def __init__(self, name: str, embedding_function: Any = None) -> None:
        self.name = name
        self._embed = embedding_function
        self._rows: dict[str, tuple[str, dict[str, Any]]] = {}
        self._vectors: dict[str, list[float]] = {}

    def add(self, documents: list[str], metadatas: list[dict[str, Any]], ids: list[str]) -> None:
        for doc_id, text, metadata in zip(ids, documents, metadatas, strict=True):
            self._rows[doc_id] = (text, dict(metadata or {}))
            if self._embed is not None:
                self._vectors[doc_id] = list(self._embed([text])[0])

    def delete(self, ids: list[str] | None = None, where: dict[str, Any] | None = None) -> None:
        if ids:
            for doc_id in list(ids):
                self._rows.pop(doc_id, None)
                self._vectors.pop(doc_id, None)
            return
        if where:
            for doc_id in [key for key in self._rows if self._matches(self._rows[key][1], where)]:
                self._rows.pop(doc_id, None)
                self._vectors.pop(doc_id, None)

    @staticmethod
    def _matches(metadata: dict[str, Any], where: dict[str, Any]) -> bool:
        return all(metadata.get(key) == value for key, value in where.items())

    def get(self) -> dict[str, Any]:
        ids = list(self._rows)
        return {
            "ids": ids,
            "documents": [self._rows[key][0] for key in ids],
            "metadatas": [self._rows[key][1] for key in ids],
        }

    def query(self, query_texts: list[str], n_results: int = 10) -> dict[str, Any]:
        if self._embed is None or not self._vectors:
            return {"ids": [[]], "distances": [[]]}
        query_vector = self._embed([query_texts[0]])[0]
        scored = [
            (doc_id, 1.0 - cosine_similarity(query_vector, vector))
            for doc_id, vector in self._vectors.items()
        ]
        scored.sort(key=lambda pair: (pair[1], pair[0]))
        top = scored[:n_results]
        return {
            "ids": [[doc_id for doc_id, _ in top]],
            "distances": [[distance for _, distance in top]],
        }


class FakeClient:
    """In-memory stand-in for ``chromadb.PersistentClient``."""

    def __init__(self, embedding_function: Any = None) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.deleted: list[str] = []
        self._embedding_function = embedding_function

    def get_or_create_collection(self, name: str, embedding_function: Any = None) -> FakeCollection:
        collection = self.collections.get(name)
        if collection is None:
            collection = FakeCollection(name, embedding_function or self._embedding_function)
            self.collections[name] = collection
        return collection

    def delete_collection(self, name: str) -> None:
        self.collections.pop(name, None)
        self.deleted.append(name)


# ── fixtures ──────────────────────────────────────────────────────────────

USER_ID = 4242


def _index(*, dense: bool = True, client: FakeClient | None = None) -> AdvancedRAGEngine:
    """An engine wired to fakes; ``dense=False`` disables the vector lane."""
    return AdvancedRAGEngine(
        client=client or FakeClient(),
        embedding_fn=BucketEmbedder() if dense else None,
        rerank=False,
        chunk_tokens=64,
    )


async def _ingest(engine: AdvancedRAGEngine) -> None:
    for file_id, text in CORPUS.items():
        await engine.add_document(
            USER_ID, text, {"file_id": file_id, "file_name": f"{file_id}.txt"}
        )


def _lexical_baseline() -> HybridRetriever:
    retriever = HybridRetriever(embedder=None)
    for file_id, text in CORPUS.items():
        retriever.index(file_id, text)
    return retriever


def _bm25_report(k: int = 3) -> Any:
    retriever = _lexical_baseline()
    return evaluate_retrieval(
        CASES, lambda query, limit: [h.doc_id for h in retriever.search(query, limit)], k=k
    )


# ── corpus-level quality ──────────────────────────────────────────────────


def test_lexical_only_misses_the_paraphrase_query() -> None:
    """Baseline: BM25 answers the exact-match probes and fails the synonym one."""
    report = _bm25_report(k=3)
    assert "llm-synonym" in report.misses  # "offline language model" has no shared token
    assert report.recall_at_k == pytest.approx(5 / 6)


def test_hybrid_retrieval_solves_the_paraphrase_without_losing_exact_matches() -> None:
    retriever = HybridRetriever(embedder=BucketEmbedder(), vector_weight=1.5)
    for file_id, text in CORPUS.items():
        retriever.index(file_id, text)

    report = evaluate_retrieval(
        CASES, lambda query, limit: [h.doc_id for h in retriever.search(query, limit)], k=3
    )

    assert report.recall_at_k == 1.0, report.as_dict()
    assert report.hit_rate == 1.0
    assert report.mrr_at_k == 1.0
    assert report.misses == ()


def test_hybrid_strictly_beats_lexical_on_this_corpus() -> None:
    lexical = _bm25_report(k=3)
    retriever = HybridRetriever(embedder=BucketEmbedder(), vector_weight=1.5)
    for file_id, text in CORPUS.items():
        retriever.index(file_id, text)
    hybrid = evaluate_retrieval(
        CASES, lambda query, limit: [h.doc_id for h in retriever.search(query, limit)], k=3
    )
    assert hybrid.recall_at_k > lexical.recall_at_k
    assert hybrid.hit_rate > lexical.hit_rate


def test_metrics_are_stable_across_repeats() -> None:
    first = _bm25_report(k=3).as_dict()
    second = _bm25_report(k=3).as_dict()
    assert first == second  # deterministic: no hashing order, no randomness


# ── engine-level (chunking + store + fusion, no chromadb) ─────────────────


async def test_engine_ingests_lists_and_retrieves() -> None:
    client = FakeClient()
    engine = _index(client=client)

    await _ingest(engine)

    documents = await engine.list_documents(USER_ID)
    assert len(documents) == len(CORPUS)
    assert all(isinstance(item, DocumentInfo) for item in documents)
    assert {item.file_id for item in documents} == set(CORPUS)
    assert all(item.chunks >= 1 for item in documents)

    hits = await engine.retrieve(USER_ID, "ایندکس پایگاه داده پستگرس", top_k=3)
    assert hits and all(isinstance(hit, RetrievedChunk) for hit in hits)
    assert hits[0].file_id == "db"


async def test_engine_recall_report_beats_the_lexical_baseline() -> None:
    engine = _index()
    await _ingest(engine)

    report = await engine.recall_report(USER_ID, list(CASES), k=3)
    lexical = _bm25_report(k=3).recall_at_k

    assert report["recall_at_k"] >= 1.0, report
    assert report["hit_rate"] == 1.0
    assert report["recall_at_k"] > lexical


async def test_engine_query_returns_the_joined_context() -> None:
    engine = _index()
    await _ingest(engine)

    answer = await engine.query(USER_ID, "گزارش فروش درآمد", top_k=2)

    assert isinstance(answer, str)
    assert answer != "No relevant documents found."
    assert "فروش" in answer
    assert "\n---\n" in answer or "\n" not in answer  # joined or a single chunk


async def test_retrieval_is_empty_for_unknown_users_and_blank_queries() -> None:
    engine = _index()
    await _ingest(engine)

    assert await engine.retrieve(USER_ID + 1, "پایگاه داده") == []
    assert await engine.retrieve(USER_ID, "") == []
    assert await engine.retrieve(USER_ID, "   ") == []
    assert await engine.query(USER_ID + 1, "پایگاه داده") == "No relevant documents found."


async def test_re_ingesting_the_same_file_replaces_its_chunks() -> None:
    client = FakeClient()
    engine = _index(client=client)
    await engine.add_document(USER_ID, CORPUS["db"], {"file_id": "db", "file_name": "db.txt"})
    before = len(client.collections[f"user_docs_{USER_ID}"].get()["ids"])

    await engine.add_document(USER_ID, CORPUS["db"], {"file_id": "db", "file_name": "db.txt"})
    after = len(client.collections[f"user_docs_{USER_ID}"].get()["ids"])

    assert after == before > 0  # no duplication
    assert len(await engine.list_documents(USER_ID)) == 1


async def test_long_documents_are_chunked() -> None:
    engine = _index()
    long_text = "\n\n".join(CORPUS.values()) * 12

    ids = await engine.add_document(USER_ID, long_text, {"file_id": "big", "file_name": "big.txt"})

    assert len(ids) > 1
    assert len(set(ids)) == len(ids)


async def test_delete_document_removes_it_from_both_lanes() -> None:
    client = FakeClient()
    engine = _index(client=client)
    await _ingest(engine)

    assert await engine.delete_document(USER_ID, "db") is True
    assert {item.file_id for item in await engine.list_documents(USER_ID)} == set(CORPUS) - {"db"}
    assert await engine.delete_document(USER_ID, "db") is False

    hits = await engine.retrieve(USER_ID, "ایندکس پایگاه داده پستگرس", top_k=3)
    assert all(hit.file_id != "db" for hit in hits)


async def test_clear_memory_wipes_the_user_index() -> None:
    client = FakeClient()
    engine = _index(client=client)
    await _ingest(engine)

    await engine.clear_memory(USER_ID)

    assert client.deleted == [f"user_docs_{USER_ID}"]
    assert await engine.list_documents(USER_ID) == []
    assert await engine.retrieve(USER_ID, "پایگاه داده") == []


async def test_ingestion_rejects_empty_and_oversized_documents() -> None:
    engine = _index()
    assert await engine.add_document(USER_ID, "   ", {"file_id": "blank"}) == []
    with pytest.raises(ValueError, match="document too large"):
        await engine.add_document(USER_ID, "x" * 1_000_001, {"file_id": "huge"})


def test_missing_vector_stack_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """No chromadb → a precise, actionable error, never a silent no-op.

    ``chromadb`` is a **core** dependency (``pyproject.toml``), so it is
    installed in CI and in a correct ``pip install -e ".[dev]"`` tree: the
    absence this test is about cannot be observed there, it has to be
    *simulated*.  ``sys.modules[name] = None`` is the documented way to make
    ``import name`` raise ``ImportError`` — exactly the failure mode of a tree
    without the vector stack — so the assertion holds whichever way the
    developer's venv happens to be built.  (As written before, the test only
    passed where chromadb was missing, which is precisely where CI is not.)
    """
    monkeypatch.setitem(sys.modules, "chromadb", None)
    engine = AdvancedRAGEngine()

    with pytest.raises(RAGUnavailable) as excinfo:
        engine.client()

    message = str(excinfo.value)
    assert "chromadb is not installed" in message
    # …and it stays actionable: the user is told how to repair the tree.
    assert "pip install" in message


def test_worker_contract_is_preserved() -> None:
    """``worker.process_pdf_task`` calls ``AdvancedRAGEngine().add_document``."""
    signature = inspect.signature(AdvancedRAGEngine.add_document)
    assert list(signature.parameters) == ["self", "user_id", "text", "metadata"]
    assert inspect.iscoroutinefunction(AdvancedRAGEngine.add_document)
    assert inspect.iscoroutinefunction(AdvancedRAGEngine.query)


def test_core_and_engine_share_one_chunker() -> None:
    """The engine chunks with the same code the chunking tests validate."""
    from nexus_ai_agent.features.rag import chunk_text as engine_chunk_text
    from nexus_ai_agent.features.rag_core import chunk_text as core_chunk_text

    assert engine_chunk_text is core_chunk_text


def test_bm25_index_built_from_the_corpus_orders_the_expected_doc_first() -> None:
    """Sanity anchor for the baseline numbers used above."""
    index = BM25()
    for file_id, text in CORPUS.items():
        index.add(file_id, text)
    top = index.search("docker compose استقرار", k=1)
    assert top and top[0].doc_id == "deploy"
    assert isinstance(top[0], ScoredDoc)
