"""Behavioural tests for the document workspace surface (``bot/surface/docs.py``).

``bot/handlers.py`` answers ``/docs`` with ``"📚 لیست اسناد شما خالی است (نسخه
دمو)."`` and ``/doc_delete`` with a lie (``"🗑️ سند حذف شد."`` — nothing is ever
deleted). These tests drive the real surface against a duck-typed engine and
assert the difference: lists come from the store, deletion really deletes, and
``/chat_with_doc`` answers from the retriever.

The engine is injected (``set_engine``), so nothing here needs ``chromadb``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from surface_fakes import FakeChunk, FakeDocInfo, FakeEngine, make_context, make_update

from nexus_ai_agent.bot.surface import docs as docs_surface

DOCS = [("d1", "قرارداد.pdf", 4), ("d2", "گزارش.pdf", 2)]


@pytest.fixture()
def engine() -> Any:
    """A fake engine with two documents; yields it and resets the surface."""
    fake = FakeEngine(DOCS, answers={"سوال": [("d1", "پاسخ قرارداد"), ("d2", "پاسخ گزارش")]})
    docs_surface.set_engine(fake)
    yield fake
    docs_surface.reset_engine()


# ── /docs ─────────────────────────────────────────────────────────────────


async def test_docs_lists_the_real_documents(engine: FakeEngine) -> None:
    update = make_update(user_id=1, chat_id=10)

    await docs_surface.docs_list_cmd(update, make_context())

    text = update.last_reply
    assert "📚 اسناد شما (2)" in text
    assert "1. قرارداد.pdf (4 بخش)" in text
    assert "2. گزارش.pdf (2 بخش)" in text
    assert "/doc_delete" in text  # how to act on the list


async def test_docs_empty_state_tells_the_user_what_to_do() -> None:
    docs_surface.set_engine(FakeEngine([]))
    update = make_update()

    await docs_surface.docs_list_cmd(update, make_context())

    assert "نسخه دمو" not in update.last_reply  # the stub wording is gone
    assert "PDF" in update.last_reply
    docs_surface.reset_engine()


# ── /doc_delete ───────────────────────────────────────────────────────────


async def test_doc_delete_accepts_the_list_index(engine: FakeEngine) -> None:
    update = make_update()

    await docs_surface.doc_delete_cmd(update, make_context(["1"]))

    assert engine.deleted == ["d1"]
    assert "قرارداد.pdf" in update.last_reply
    assert "🗑️" in update.last_reply


async def test_doc_delete_accepts_a_raw_file_id(engine: FakeEngine) -> None:
    update = make_update()

    await docs_surface.doc_delete_cmd(update, make_context(["d2"]))

    assert engine.deleted == ["d2"]


async def test_doc_delete_without_arguments_shows_usage(engine: FakeEngine) -> None:
    update = make_update()

    await docs_surface.doc_delete_cmd(update, make_context())

    assert engine.deleted == []
    assert "/doc_delete <شماره سند>" in update.last_reply


async def test_doc_delete_reports_unknown_targets(engine: FakeEngine) -> None:
    update = make_update()

    await docs_surface.doc_delete_cmd(update, make_context(["99"]))

    assert engine.deleted == []
    assert "پیدا نشد" in update.last_reply


async def test_doc_delete_ends_an_active_doc_chat(engine: FakeEngine) -> None:
    await docs_surface.chat_with_doc_cmd(make_update(), make_context(["1"]))
    assert docs_surface.session_for(1, 10) is not None

    await docs_surface.doc_delete_cmd(make_update(), make_context(["1"]))

    assert docs_surface.session_for(1, 10) is None


# ── /chat_with_doc ────────────────────────────────────────────────────────


async def test_chat_with_doc_without_arguments_shows_the_menu(engine: FakeEngine) -> None:
    update = make_update()

    await docs_surface.chat_with_doc_cmd(update, make_context())

    assert "1. قرارداد.pdf" in update.last_reply
    assert "/chat_with_doc <شماره>" in update.last_reply
    assert docs_surface.session_for(1, 10) is None  # not activated yet


async def test_chat_with_doc_activates_one_document(engine: FakeEngine) -> None:
    update = make_update()

    await docs_surface.chat_with_doc_cmd(update, make_context(["1"]))

    session = docs_surface.session_for(1, 10)
    assert session is not None and session.file_id == "d1"
    assert "قرارداد.pdf" in update.last_reply
    assert "/chat_with_doc stop" in update.last_reply


async def test_chat_with_doc_can_cover_every_document(engine: FakeEngine) -> None:
    update = make_update()

    await docs_surface.chat_with_doc_cmd(update, make_context(["همه"]))

    assert docs_surface.session_for(1, 10) is not None


async def test_chat_with_doc_with_no_uploads_explains_itself() -> None:
    docs_surface.set_engine(FakeEngine([]))
    update = make_update()

    await docs_surface.chat_with_doc_cmd(update, make_context())

    assert "PDF" in update.last_reply
    assert docs_surface.session_for(1, 10) is None
    docs_surface.reset_engine()


async def test_chat_with_doc_accepts_several_stop_words(engine: FakeEngine) -> None:
    await docs_surface.chat_with_doc_cmd(make_update(), make_context(["1"]))
    for word in ("stop", "exit", "پایان", "توقف"):
        await docs_surface.chat_with_doc_cmd(make_update(), make_context(["1"]))
        update = make_update()
        await docs_surface.chat_with_doc_cmd(update, make_context([word]))
        assert "🔚" in update.last_reply
        assert docs_surface.session_for(1, 10) is None


async def test_chat_with_doc_rejects_an_unknown_index(engine: FakeEngine) -> None:
    update = make_update()

    await docs_surface.chat_with_doc_cmd(update, make_context(["7"]))

    assert "پیدا نشد" in update.last_reply
    assert docs_surface.session_for(1, 10) is None


# ── free-text routing ─────────────────────────────────────────────────────


async def test_route_doc_text_answers_while_the_session_is_active(engine: FakeEngine) -> None:
    await docs_surface.chat_with_doc_cmd(make_update(), make_context(["1"]))
    update = make_update(text="سوال")

    handled = await docs_surface.route_doc_text(update, make_context())

    assert handled is True
    assert "🔍 «سوال»" in update.last_reply
    assert "پاسخ قرارداد" in update.last_reply
    assert engine.queries == ["سوال"]


async def test_route_doc_text_scopes_answers_to_the_selected_document(engine: FakeEngine) -> None:
    """Only the chosen document's chunks may reach the user."""
    await docs_surface.chat_with_doc_cmd(make_update(), make_context(["2"]))
    update = make_update(text="سوال")

    await docs_surface.route_doc_text(update, make_context())

    assert "پاسخ گزارش" in update.last_reply
    assert "پاسخ قرارداد" not in update.last_reply


async def test_route_doc_text_is_inert_without_a_session(engine: FakeEngine) -> None:
    update = make_update(text="سوال")

    assert await docs_surface.route_doc_text(update, make_context()) is False
    assert update.replies == []


async def test_route_doc_text_ignores_blank_text_and_commands(engine: FakeEngine) -> None:
    await docs_surface.chat_with_doc_cmd(make_update(), make_context(["همه"]))

    assert await docs_surface.route_doc_text(make_update(text=""), make_context()) is False
    assert await docs_surface.route_doc_text(make_update(text="  "), make_context()) is False
    assert await docs_surface.route_doc_text(make_update(text="/docs"), make_context()) is False


async def test_sessions_are_scoped_per_user_and_chat(engine: FakeEngine) -> None:
    await docs_surface.chat_with_doc_cmd(make_update(user_id=1, chat_id=10), make_context(["1"]))

    assert docs_surface.session_for(1, 10) is not None
    assert docs_surface.session_for(2, 10) is None
    assert docs_surface.session_for(1, 11) is None


async def test_expired_sessions_are_dropped(engine: FakeEngine) -> None:
    await docs_surface.chat_with_doc_cmd(make_update(), make_context(["1"]))
    session = docs_surface.session_for(1, 10)
    assert session is not None
    session.updated_at = time.monotonic() - (docs_surface.SESSION_TTL_SECONDS + 1)

    assert docs_surface.session_for(1, 10) is None
    assert await docs_surface.route_doc_text(make_update(text="سوال"), make_context()) is False


async def test_session_count_is_bounded(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unbounded per-user dict is a memory leak: the cap must evict."""
    monkeypatch.setattr(docs_surface, "MAX_SESSIONS", 2)
    await docs_surface.chat_with_doc_cmd(make_update(user_id=1, chat_id=1), make_context(["همه"]))
    await docs_surface.chat_with_doc_cmd(make_update(user_id=2, chat_id=2), make_context(["همه"]))
    await docs_surface.chat_with_doc_cmd(make_update(user_id=3, chat_id=3), make_context(["همه"]))

    assert docs_surface.session_for(1, 1) is None  # oldest evicted
    assert docs_surface.session_for(2, 2) is not None
    assert docs_surface.session_for(3, 3) is not None


# ── failure modes ─────────────────────────────────────────────────────────


async def test_missing_vector_stack_fails_closed_in_persian() -> None:
    docs_surface.set_engine(FakeEngine(DOCS, unavailable=True))

    update = make_update()
    await docs_surface.docs_list_cmd(update, make_context())

    assert "chromadb" in update.last_reply  # actionable, not a stack trace
    assert "📚" in update.last_reply
    docs_surface.reset_engine()


async def test_unexpected_engine_failures_are_contained(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command must never raise into the PTB dispatcher."""

    async def boom(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("secret stack trace")

    monkeypatch.setattr(engine, "list_documents", boom)
    update = make_update()

    await docs_surface.docs_list_cmd(update, make_context())

    assert "secret stack trace" not in update.last_reply
    assert "❌" in update.last_reply


async def test_commands_ignore_updates_without_a_user(engine: FakeEngine) -> None:
    update = make_update(user_id=None)

    await docs_surface.docs_list_cmd(update, make_context())
    await docs_surface.doc_delete_cmd(update, make_context(["1"]))
    await docs_surface.chat_with_doc_cmd(update, make_context(["1"]))

    assert update.replies == []
    assert await docs_surface.route_doc_text(update, make_context()) is False


# ── pure renderers ────────────────────────────────────────────────────────


def test_format_docs_empty_state() -> None:
    assert "PDF" in docs_surface.format_docs([])


def test_format_docs_falls_back_to_the_file_id() -> None:
    text = docs_surface.format_docs([FakeDocInfo("f1", "", 3)])
    assert "f1 (3 بخش)" in text


def test_format_answer_without_hits() -> None:
    text = docs_surface.format_answer("چرا؟", [])
    assert "پیدا نشد" in text
    assert "چرا؟" not in text


def test_format_answer_quotes_the_question_and_the_source() -> None:
    hits = [FakeChunk("d1", "قرارداد.pdf", "متن قرارداد", chunk_id="d1:0")]
    text = docs_surface.format_answer("  مهلت؟  ", hits)
    assert "«مهلت؟»" in text  # trimmed
    assert "از قرارداد.pdf" in text
    assert "متن قرارداد" in text


def test_format_answer_numbers_several_hits() -> None:
    hits = [
        FakeChunk("d1", "a.pdf", "اول"),
        FakeChunk("d2", "b.pdf", "دوم"),
    ]
    text = docs_surface.format_answer("چی؟", hits)
    assert "1. از a.pdf" in text
    assert "2. از b.pdf" in text
