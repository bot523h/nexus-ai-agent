"""B8 — RAG list/delete/query and the ``/docs`` · ``/doc_delete`` · ``/chat_with_doc`` surface."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest
from surface_fakes import make_context, make_update

chromadb = pytest.importorskip("chromadb")

from nexus_ai_agent.bot.surface import docs as surface  # noqa: E402
from nexus_ai_agent.features.rag import (  # noqa: E402
    MAX_DOCUMENTS_PER_USER,
    AdvancedRAGEngine,
    DocumentInfo,
    chunk_text,
)


class _HashEmbedding(chromadb.EmbeddingFunction):  # type: ignore[misc]
    """Deterministic, dependency-free embedding for tests (bag of char-trigrams)."""

    def __init__(self) -> None:
        pass

    def __call__(self, input: list[str]) -> list[Any]:  # noqa: A002 - chroma protocol
        out = []
        for text in input:
            vec = np.zeros(64, dtype="float32")
            for i in range(max(1, len(text) - 2)):
                h = int(hashlib.md5(text[i : i + 3].encode()).hexdigest(), 16)  # noqa: S324
                vec[h % 64] += 1.0
            norm = float(np.linalg.norm(vec)) or 1.0
            out.append(vec / norm)
        return out

    @staticmethod
    def name() -> str:
        return "test_hash_embedding"

    def get_config(self) -> dict[str, Any]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> _HashEmbedding:
        return _HashEmbedding()


@pytest.fixture()
def engine() -> Any:
    # EphemeralClient is a process-wide singleton in chromadb 1.x: wipe collections per test.
    client = chromadb.EphemeralClient()
    for col in client.list_collections():
        client.delete_collection(col.name)
    yield AdvancedRAGEngine(client=client, embedding_fn=_HashEmbedding(), ranker=False)
    for col in client.list_collections():
        client.delete_collection(col.name)


@pytest.fixture(autouse=True)
def _surface_engine(engine: AdvancedRAGEngine) -> Any:
    surface.reset_docs(engine=engine)
    yield
    surface.reset_docs()


# ── engine ───────────────────────────────────────────────────────────


def test_chunk_text_windows_and_overlap() -> None:
    assert chunk_text("") == []
    assert chunk_text("abc", size=10) == ["abc"]
    chunks = chunk_text("a" * 2500, size=1000, overlap=100)
    assert [len(c) for c in chunks] == [1000, 1000, 700]
    assert chunk_text("x" * 30, size=10, overlap=0) == ["x" * 10] * 3


@pytest.mark.asyncio
async def test_add_list_delete_and_replace(engine: AdvancedRAGEngine) -> None:
    assert await engine.list_documents(1) == []
    assert await engine.has_documents(1) is False

    n = await engine.add_document(1, "hello world " * 200, {"file_id": "A", "file_name": "a.pdf"})
    assert n == 3
    await engine.add_document(1, "short note", {"file_id": "B"})
    docs = await engine.list_documents(1)
    assert [(d.file_id, d.file_name, d.chunks) for d in docs] == [("A", "a.pdf", 3), ("B", "B", 1)]
    assert all(d.added_at for d in docs)

    # re-adding the same file_id replaces instead of duplicating
    await engine.add_document(1, "new content", {"file_id": "A", "file_name": "a2.pdf"})
    docs = await engine.list_documents(1)
    assert [(d.file_id, d.file_name, d.chunks) for d in docs] == [("B", "B", 1), ("A", "a2.pdf", 1)]

    assert await engine.delete_document(1, "nope") == 0
    assert await engine.delete_document(1, "A") == 1
    assert [d.file_id for d in await engine.list_documents(1)] == ["B"]
    # other users are isolated
    assert await engine.list_documents(2) == []


@pytest.mark.asyncio
async def test_retrieve_and_query(engine: AdvancedRAGEngine) -> None:
    assert await engine.retrieve(1, "anything") == []
    assert await engine.query(1, "anything") == ""
    await engine.add_document(
        1, "the capital of france is paris", {"file_id": "F", "file_name": "f"}
    )
    await engine.add_document(
        1, "python is a programming language", {"file_id": "P", "file_name": "p"}
    )
    hits = await engine.retrieve(1, "capital of france paris")
    assert hits and hits[0].file_id == "F"
    only_p = await engine.retrieve(1, "capital of france paris", file_id="P")
    assert [h.file_id for h in only_p] == ["P"]
    assert "paris" in await engine.query(1, "capital of france paris")


@pytest.mark.asyncio
async def test_document_limit_and_size_guard(engine: AdvancedRAGEngine) -> None:
    for i in range(MAX_DOCUMENTS_PER_USER):
        await engine.add_document(3, f"doc number {i}", {"file_id": f"d{i}"})
    with pytest.raises(ValueError):
        await engine.add_document(3, "one too many", {"file_id": "overflow"})
    # replacing an existing one is still allowed at the limit
    await engine.add_document(3, "replaced", {"file_id": "d0"})
    with pytest.raises(ValueError):
        await engine.add_document(4, "x" * 2_000_000, {"file_id": "huge"})


def test_engine_constructor_is_lazy_about_heavy_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injected collaborators mean no chromadb persistence / model download happens."""
    from nexus_ai_agent.features import rag as rag_module

    monkeypatch.setattr(rag_module, "_default_client", lambda: pytest.fail("client built"))
    monkeypatch.setattr(rag_module, "_default_embedding_fn", lambda: pytest.fail("model loaded"))
    monkeypatch.setattr(rag_module, "_default_ranker", lambda: pytest.fail("ranker loaded"))
    eng = AdvancedRAGEngine(client=object(), embedding_fn=object(), ranker=False)
    assert eng.ranker is None


# ── surface helpers ──────────────────────────────────────────────────


def test_format_docs_and_resolve_ref() -> None:
    assert "هنوز سندی" in surface.format_docs([])
    docs = [
        DocumentInfo("id-1", "a.pdf", 3, "2026-09-21T10:00:00+00:00"),
        DocumentInfo("id-2", "b.pdf", 1, ""),
    ]
    text = surface.format_docs(docs)
    assert "1. a.pdf" in text and "id-2" in text and "2026-09-21" in text
    assert surface.resolve_doc_ref("2", docs) == docs[1]
    assert surface.resolve_doc_ref("#1", docs) == docs[0]
    assert surface.resolve_doc_ref("id-2", docs) == docs[1]
    assert surface.resolve_doc_ref("b.pdf", docs) == docs[1]
    assert surface.resolve_doc_ref("9", docs) is None
    assert surface.resolve_doc_ref("", docs) is None


# ── surface commands ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_docs_and_doc_delete_cmds(engine: AdvancedRAGEngine) -> None:
    update = make_update(user_id=1)
    await surface.docs_list_cmd(update, make_context())
    assert "هنوز سندی" in update.message.last

    await engine.add_document(1, "alpha beta gamma", {"file_id": "A", "file_name": "a.pdf"})
    update = make_update(user_id=1)
    await surface.docs_list_cmd(update, make_context())
    assert "a.pdf" in update.message.last and "(نسخه دمو)" not in update.message.last

    update = make_update(user_id=1)
    await surface.doc_delete_cmd(update, make_context([]))
    assert "استفاده" in update.message.last

    update = make_update(user_id=1)
    await surface.doc_delete_cmd(update, make_context(["7"]))
    assert "پیدا نشد" in update.message.last

    update = make_update(user_id=1)
    await surface.doc_delete_cmd(update, make_context(["1"]))
    assert "a.pdf" in update.message.last and "حذف شد" in update.message.last
    assert await engine.list_documents(1) == []


@pytest.mark.asyncio
async def test_chat_with_doc_answers_via_gemini(engine: AdvancedRAGEngine) -> None:
    await engine.add_document(
        1, "the capital of france is paris", {"file_id": "F", "file_name": "geo.pdf"}
    )
    gemini = AsyncMock()
    gemini.ask = AsyncMock(return_value="Paris.")
    ctx = make_context(
        ["what", "is", "the", "capital", "of", "france"], bot_data={"gemini_engine": gemini}
    )

    update = make_update(user_id=1)
    await surface.chat_with_doc_cmd(update, ctx)
    assert update.message.last.startswith("Paris.")
    assert "geo.pdf" in update.message.last
    prompt = gemini.ask.await_args.args[0]
    assert "paris" in prompt and "what is the capital of france" in prompt
    assert gemini.ask.await_args.kwargs == {"user_id": 1}


@pytest.mark.asyncio
async def test_chat_with_doc_without_llm_and_without_hits(engine: AdvancedRAGEngine) -> None:
    update = make_update(user_id=1)
    await surface.chat_with_doc_cmd(update, make_context(["anything"]))
    assert "پیدا نشد" in update.message.last

    await engine.add_document(1, "the capital of france is paris", {"file_id": "F"})
    update = make_update(user_id=1)
    await surface.chat_with_doc_cmd(update, make_context(["capital", "of", "france"]))
    assert "پیکربندی نشده" in update.message.last and "paris" in update.message.last


@pytest.mark.asyncio
async def test_doc_mode_routing(engine: AdvancedRAGEngine) -> None:
    update = make_update(user_id=1)
    await surface.chat_with_doc_cmd(update, make_context())
    assert "هنوز سندی" in update.message.last and not surface.in_doc_mode(1)

    await engine.add_document(1, "the capital of france is paris", {"file_id": "F"})
    update = make_update(user_id=1)
    await surface.chat_with_doc_cmd(update, make_context())
    assert "فعال شد" in update.message.last and surface.in_doc_mode(1)

    gemini = AsyncMock()
    gemini.ask = AsyncMock(return_value="Paris")
    ctx = make_context(bot_data={"gemini_engine": gemini})
    update = make_update(user_id=1, text="capital of france?")
    assert await surface.route_doc_chat(update, ctx) is True
    assert update.message.last.startswith("Paris")
    assert await surface.route_doc_chat(make_update(user_id=2, text="x"), ctx) is False
    assert await surface.route_doc_chat(make_update(user_id=1, text="/docs"), ctx) is False

    update = make_update(user_id=1)
    await surface.chat_with_doc_cmd(update, make_context(["off"]))
    assert not surface.in_doc_mode(1)
