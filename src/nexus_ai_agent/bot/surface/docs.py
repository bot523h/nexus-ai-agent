"""Telegram surface for the document RAG: ``/docs``, ``/doc_delete``, ``/chat_with_doc``.

Replaces the demo stubs ("لیست اسناد شما خالی است (نسخه دمو)") with real
calls into :class:`~nexus_ai_agent.features.rag.AdvancedRAGEngine`.

* ``/docs``                      — list indexed documents (id, name, chunks)
* ``/doc_delete <id|#n>``        — delete one document (by id or list index)
* ``/chat_with_doc <question>``  — answer from the documents via Gemini
* ``/chat_with_doc``             — toggle "doc mode": subsequent plain
  messages are routed through :func:`route_doc_chat` (once the catch-all
  handler calls it); ``/chat_with_doc off`` leaves the mode.

The engine (and its embedding model) is built lazily in a worker thread on
first use, so importing this module never pulls torch into the bot process.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus_ai_agent.features.rag import AdvancedRAGEngine, DocumentInfo, RetrievedChunk
from nexus_ai_agent.observability.logging import get_logger

from ._ptb import args, bot_data, message_text, reply, user_id

__all__ = [
    "chat_with_doc_cmd",
    "doc_delete_cmd",
    "docs_list_cmd",
    "format_docs",
    "get_rag_engine",
    "in_doc_mode",
    "reset_docs",
    "resolve_doc_ref",
    "route_doc_chat",
]

logger = get_logger(__name__)

MAX_QUESTION_CHARS = 1000
MAX_CONTEXT_CHARS = 6000
_OFF_WORDS = frozenset({"off", "stop", "exit", "خروج", "خاموش", "پایان"})

_engine: AdvancedRAGEngine | None = None
_engine_factory: Any = AdvancedRAGEngine
_engine_lock = asyncio.Lock()
_doc_mode: set[int] = set()


async def get_rag_engine() -> AdvancedRAGEngine:
    """Shared engine, constructed once (off the event loop — model loading is slow)."""
    global _engine
    if _engine is not None:
        return _engine
    async with _engine_lock:
        if _engine is None:
            built: AdvancedRAGEngine = await asyncio.to_thread(_engine_factory)
            _engine = built
        return _engine


def reset_docs(engine: AdvancedRAGEngine | None = None, factory: Any = None) -> None:
    """Swap the engine/factory (tests) and clear doc-mode state."""
    global _engine, _engine_factory
    _engine = engine
    _engine_factory = factory or AdvancedRAGEngine
    _doc_mode.clear()


def in_doc_mode(uid: int) -> bool:
    return uid in _doc_mode


# ── formatting / parsing helpers (pure) ──────────────────────────────


def format_docs(docs: list[DocumentInfo]) -> str:
    if not docs:
        return (
            "📚 هنوز سندی ندارید.\n"
            "یک فایل PDF بفرستید تا نمایه شود؛ سپس با /chat_with_doc از آن بپرسید."
        )
    lines = [f"📚 اسناد شما ({len(docs)}):"]
    for i, d in enumerate(docs, 1):
        when = f" — {d.added_at[:10]}" if d.added_at else ""
        lines.append(f"{i}. {d.file_name}  ({d.chunks} بخش){when}\n   🆔 `{d.file_id}`")
    lines.append("\nحذف: `/doc_delete <شماره یا شناسه>`")
    return "\n".join(lines)


def resolve_doc_ref(ref: str, docs: list[DocumentInfo]) -> DocumentInfo | None:
    """Map ``#2`` / ``2`` / ``<file_id>`` / ``<file_name>`` to a document."""
    ref = ref.strip().lstrip("#")
    if not ref:
        return None
    if ref.isdigit():
        idx = int(ref)
        if 1 <= idx <= len(docs):
            return docs[idx - 1]
    for d in docs:
        if d.file_id == ref:
            return d
    for d in docs:
        if d.file_name == ref:
            return d
    return None


def _build_prompt(question: str, chunks: list[RetrievedChunk]) -> str:
    context_parts: list[str] = []
    used = 0
    for c in chunks:
        piece = f"[{c.file_name or c.file_id}]\n{c.text}"
        if used + len(piece) > MAX_CONTEXT_CHARS:
            piece = piece[: max(0, MAX_CONTEXT_CHARS - used)]
        if not piece:
            break
        context_parts.append(piece)
        used += len(piece)
    context = "\n---\n".join(context_parts)
    return (
        "You answer strictly from the document excerpts below. If the answer is not "
        "contained in them, say so briefly in the user's language. Reply in the language "
        "of the question.\n\n"
        f"=== EXCERPTS ===\n{context}\n=== END ===\n\n"
        f"Question: {question}"
    )


# ── command handlers ─────────────────────────────────────────────────


async def docs_list_cmd(update: Any, context: Any) -> None:
    uid = user_id(update)
    if uid is None:
        return
    try:
        docs = await (await get_rag_engine()).list_documents(uid)
    except Exception:  # noqa: BLE001 - optional heavy stack may be missing
        logger.exception("docs_list_failed", user_id=uid)
        await reply(update, "❌ سامانه اسناد در دسترس نیست (وابستگی‌های RAG نصب نشده‌اند؟).")
        return
    await reply(update, format_docs(docs), parse_mode="Markdown")


async def doc_delete_cmd(update: Any, context: Any) -> None:
    uid = user_id(update)
    if uid is None:
        return
    a = args(context)
    if not a:
        await reply(update, "❌ استفاده: /doc_delete <شماره یا شناسه سند>\nفهرست: /docs")
        return
    try:
        engine = await get_rag_engine()
        docs = await engine.list_documents(uid)
        target = resolve_doc_ref(" ".join(a), docs)
        if target is None:
            await reply(update, "⚠️ سندی با این شماره/شناسه پیدا نشد. فهرست: /docs")
            return
        removed = await engine.delete_document(uid, target.file_id)
    except Exception:  # noqa: BLE001
        logger.exception("doc_delete_failed", user_id=uid)
        await reply(update, "❌ حذف سند ناموفق بود.")
        return
    await reply(update, f"🗑️ سند «{target.file_name}» حذف شد ({removed} بخش).")


async def _answer(update: Any, context: Any, uid: int, question: str) -> None:
    question = question.strip()[:MAX_QUESTION_CHARS]
    try:
        engine = await get_rag_engine()
        chunks = await engine.retrieve(uid, question)
    except Exception:  # noqa: BLE001
        logger.exception("doc_retrieve_failed", user_id=uid)
        await reply(update, "❌ جست‌وجو در اسناد ناموفق بود.")
        return
    if not chunks:
        await reply(update, "📭 بخشی مرتبط با سؤال شما در اسنادتان پیدا نشد. (فهرست: /docs)")
        return

    gemini = bot_data(context).get("gemini_engine")
    if gemini is None or not hasattr(gemini, "ask"):
        preview = "\n---\n".join(c.text[:700] for c in chunks)
        await reply(update, f"📄 بخش‌های مرتبط (مدل زبانی پیکربندی نشده):\n\n{preview}"[:4000])
        return
    try:
        answer = await gemini.ask(_build_prompt(question, chunks), user_id=uid)
    except Exception:  # noqa: BLE001
        logger.exception("doc_chat_llm_failed", user_id=uid)
        await reply(update, "❌ پاسخ‌دهی مدل زبانی ناموفق بود؛ کمی بعد دوباره تلاش کنید.")
        return
    sources = sorted({c.file_name or c.file_id for c in chunks if (c.file_name or c.file_id)})
    footer = f"\n\n📎 منبع: {', '.join(sources)}" if sources else ""
    await reply(update, f"{answer}{footer}"[:4000])


async def chat_with_doc_cmd(update: Any, context: Any) -> None:
    uid = user_id(update)
    if uid is None:
        return
    a = args(context)
    if a and a[0].lower() in _OFF_WORDS:
        _doc_mode.discard(uid)
        await reply(update, "🔚 حالت چت با سند خاموش شد.")
        return
    if a:
        await _answer(update, context, uid, " ".join(a))
        return

    try:
        has_docs = await (await get_rag_engine()).has_documents(uid)
    except Exception:  # noqa: BLE001
        logger.exception("doc_mode_failed", user_id=uid)
        await reply(update, "❌ سامانه اسناد در دسترس نیست.")
        return
    if not has_docs:
        await reply(update, "📭 هنوز سندی ندارید؛ ابتدا یک فایل PDF بفرستید.")
        return
    _doc_mode.add(uid)
    await reply(
        update,
        "🔍 حالت چت با سند فعال شد. سؤال خود را بپرسید یا مستقیم بنویسید:\n"
        "`/chat_with_doc <سؤال>`\nخروج: `/chat_with_doc off`",
        parse_mode="Markdown",
    )


async def route_doc_chat(update: Any, context: Any) -> bool:
    """Answer a plain message from the documents when the user is in doc mode.

    Returns ``True`` when the message was consumed.
    """
    uid = user_id(update)
    text = message_text(update)
    if uid is None or not text or text.startswith("/") or uid not in _doc_mode:
        return False
    await _answer(update, context, uid, text)
    return True
