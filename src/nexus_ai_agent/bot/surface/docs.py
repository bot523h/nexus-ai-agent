"""Telegram surface for the document workspace: ``/docs``, ``/doc_delete``, ``/chat_with_doc``.

Replaces three more fixed-string stubs in ``bot/handlers.py``::

    async def docs_list_cmd(...)     -> "📚 لیست اسناد شما خالی است (نسخه دمو)."
    async def doc_delete_cmd(...)    -> "🗑️ سند حذف شد."
    async def chat_with_doc_cmd(...) -> "🔍 حالت چت با سند فعال شد. سوال خود را بپرسید."

The real backend is :class:`~nexus_ai_agent.features.rag.AdvancedRAGEngine`, the
hybrid retriever delivered in the same batch, so ``/chat_with_doc`` answers
from the user's uploaded PDFs instead of pretending to.

Design
------
* **Framework-free** (see :mod:`._ptb`): no ``telegram`` import, so the whole
  flow is testable with fakes and stays inside the frozen import boundary.
* **Fail closed, in Persian.** A missing vector stack raises
  :class:`~nexus_ai_agent.features.rag.RAGUnavailable`; the user gets an
  actionable sentence, never a stack trace, and never a silent pretend-success.
* **Bounded session state.** "Chat with a document" mode is kept in a dict
  keyed by ``(user_id, chat_id)`` with a TTL and a hard cap, because an
  unbounded per-user dict in a long-lived bot process is a memory leak.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from nexus_ai_agent.features.rag import (
    RAGUnavailable,
    RetrievedChunk,
)

from ._ptb import args, chat_id, message_text, reply, user_id

logger = logging.getLogger(__name__)

__all__ = [
    "DocSession",
    "chat_with_doc_cmd",
    "clear_session",
    "doc_delete_cmd",
    "docs_list_cmd",
    "format_answer",
    "format_docs",
    "get_engine",
    "reset_engine",
    "route_doc_text",
    "session_for",
    "set_engine",
]

#: Sessions older than this are dropped on the next access.
SESSION_TTL_SECONDS = 30 * 60
#: Hard cap on concurrent doc-chat sessions (memory guard).
MAX_SESSIONS = 2_000

_UNAVAILABLE = (
    "📚 موتور جستجوی اسناد روی این سرور فعال نیست "
    "(بستهٔ chromadb نصب نشده). با مدیر ربات تماس بگیرید."
)
_EMPTY_DOCS = (
    "📚 هنوز سندی ندارید. یک فایل PDF برای ربات بفرستید تا آن را ایندکس کنم "
    "و بعد با /chat_with_doc از آن بپرسید."
)


@dataclass(slots=True)
class DocSession:
    """ "Chat with my documents" mode for one ``(user, chat)`` pair."""

    file_id: str | None = None  # None → every document of the user
    updated_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.updated_at = time.monotonic()

    def is_expired(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        return (moment - self.updated_at) > SESSION_TTL_SECONDS


_SESSIONS: dict[tuple[int, int], DocSession] = {}
_ENGINE: Any | None = None


def get_engine() -> Any:
    """The process-wide RAG engine (constructed on first use)."""
    global _ENGINE
    if _ENGINE is None:
        from nexus_ai_agent.features.rag import AdvancedRAGEngine

        _ENGINE = AdvancedRAGEngine()
    return _ENGINE


def set_engine(engine: Any) -> None:
    """Inject an engine (tests, or a shared instance from the composition root)."""
    global _ENGINE
    _ENGINE = engine


def reset_engine() -> None:
    """Drop the cached engine (tests only)."""
    global _ENGINE
    _ENGINE = None
    _SESSIONS.clear()


def session_for(user: int, chat: int) -> DocSession | None:
    """The live session for ``(user, chat)``, purging expired entries."""
    _purge()
    return _SESSIONS.get((user, chat))


def clear_session(user: int, chat: int) -> bool:
    """End doc-chat mode. Returns ``True`` when a session was active."""
    return _SESSIONS.pop((user, chat), None) is not None


def _purge() -> None:
    now = time.monotonic()
    expired = [key for key, session in _SESSIONS.items() if session.is_expired(now)]
    for key in expired:
        _SESSIONS.pop(key, None)


def _remember(user: int, chat: int, file_id: str | None) -> DocSession:
    session = _SESSIONS.get((user, chat))
    if session is None:
        if len(_SESSIONS) >= MAX_SESSIONS:
            _purge()
            oldest = min(_SESSIONS, key=lambda key: _SESSIONS[key].updated_at, default=None)
            if oldest is not None:
                _SESSIONS.pop(oldest, None)
        session = DocSession()
        _SESSIONS[(user, chat)] = session
    session.file_id = file_id
    session.touch()
    return session


# ── pure rendering ─────────────────────────────────────────────────────────


def format_docs(documents: list[Any]) -> str:
    """Render the document list (numbered, so ``/doc_delete 2`` is unambiguous)."""
    if not documents:
        return _EMPTY_DOCS
    lines = [f"📚 اسناد شما ({len(documents)})", "━━━━━━━━━━━━━━━━"]
    for index, document in enumerate(documents, start=1):
        name = getattr(document, "file_name", "") or getattr(document, "file_id", "?")
        chunks = getattr(document, "chunks", 0)
        lines.append(f"  {index}. {name} ({chunks} بخش)")
    lines.append("")
    lines.append("پرسش: /chat_with_doc <شماره> · حذف: /doc_delete <شماره>")
    return "\n".join(lines)


def format_answer(question: str, chunks: list[RetrievedChunk]) -> str:
    """Render retrieval results as a compact, quotable answer."""
    if not chunks:
        return "🔍 پاسخی در اسناد شما پیدا نشد. سوال را ساده‌تر یا با واژه‌های دیگر بپرسید."

    parts = [f"🔍 «{question.strip()}»"]
    for index, chunk in enumerate(chunks, start=1):
        source = chunk.file_name or chunk.file_id
        parts.append(f"\n{index}. از {source}:\n{chunk.text.strip()}")
    return "\n".join(parts)


# ── commands ──────────────────────────────────────────────────────────────


async def docs_list_cmd(update: Any, context: Any) -> None:
    """``/docs`` — list the documents indexed for this user."""
    uid = user_id(update)
    if uid is None:
        return
    try:
        documents = await get_engine().list_documents(uid)
    except RAGUnavailable:
        await reply(update, _UNAVAILABLE)
        return
    except Exception:  # noqa: BLE001 - a command must never raise into PTB
        logger.exception("docs_list_failed", extra={"user_id": uid})
        await reply(update, "❌ خطایی در خواندن فهرست اسناد رخ داد؛ دوباره تلاش کنید.")
        return
    await reply(update, format_docs(list(documents)))


async def doc_delete_cmd(update: Any, context: Any) -> None:
    """``/doc_delete <شماره|file_id>`` — remove one document and its chunks."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None:
        return
    raw = args(context)
    if not raw:
        await reply(update, "🗑️ استفاده: /doc_delete <شماره سند> — فهرست با /docs")
        return

    try:
        documents = await get_engine().list_documents(uid)
        target = _resolve_document(documents, raw[0])
        if target is None:
            await reply(update, f"❌ سند «{raw[0]}» پیدا نشد. فهرست: /docs")
            return
        deleted = await get_engine().delete_document(uid, target.file_id)
    except RAGUnavailable:
        await reply(update, _UNAVAILABLE)
        return
    except Exception:  # noqa: BLE001 - a command must never raise into PTB
        logger.exception("doc_delete_failed", extra={"user_id": uid})
        await reply(update, "❌ حذف سند با خطا مواجه شد؛ دوباره تلاش کنید.")
        return

    if not deleted:
        await reply(update, f"❌ سند «{raw[0]}» پیدا نشد. فهرست: /docs")
        return
    if cid is not None:
        clear_session(uid, cid)  # a deleted document cannot be chatted with
    await reply(update, f"🗑️ سند «{_document_name(target)}» و همهٔ بخش‌های آن حذف شد.")


async def chat_with_doc_cmd(update: Any, context: Any) -> None:
    """``/chat_with_doc [شماره|همه]`` — enter (or leave) document-chat mode."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None or cid is None:
        return
    raw = args(context)

    if raw and raw[0].lower() in {"stop", "end", "exit", "توقف", "پایان", "خروج"}:
        clear_session(uid, cid)
        await reply(update, "🔚 حالت چت با سند پایان یافت.")
        return

    try:
        documents = await get_engine().list_documents(uid)
        if not documents:
            await reply(update, _EMPTY_DOCS)
            return

        if not raw:
            await reply(
                update,
                f"{format_docs(list(documents))}\n\n"
                "برای شروع: /chat_with_doc <شماره> (یا /chat_with_doc همه)",
            )
            return

        if raw[0] in {"all", "همه", "*"}:
            _remember(uid, cid, None)
            await reply(
                update,
                "🔍 حالت چت با سند فعال شد (همهٔ اسناد). سوال خود را بپرسید؛ "
                "برای خروج: /chat_with_doc stop",
            )
            return

        target = _resolve_document(documents, raw[0])
        if target is None:
            await reply(update, f"❌ سند «{raw[0]}» پیدا نشد. فهرست: /docs")
            return
        _remember(uid, cid, target.file_id)
        await reply(
            update,
            f"🔍 حالت چت با «{_document_name(target)}» فعال شد. سوال خود را بپرسید؛ "
            "برای خروج: /chat_with_doc stop",
        )
    except RAGUnavailable:
        await reply(update, _UNAVAILABLE)
        return
    except Exception:  # noqa: BLE001 - a command must never raise into PTB
        logger.exception("chat_with_doc_failed", extra={"user_id": uid})
        await reply(update, "❌ فعال‌سازی حالت چت با سند با خطا مواجه شد.")


async def route_doc_text(update: Any, context: Any) -> bool:
    """Answer free text while doc-chat mode is on. ``True`` when handled."""
    uid, cid = user_id(update), chat_id(update)
    if uid is None or cid is None:
        return False
    session = session_for(uid, cid)
    if session is None:
        return False

    question = message_text(update).strip()
    if not question:
        return False
    if question.startswith("/"):
        return False  # a command is never a question

    try:
        chunks = await get_engine().retrieve(uid, question, top_k=3)
        if session.file_id:
            chunks = [chunk for chunk in chunks if chunk.file_id == session.file_id]
    except RAGUnavailable:
        await reply(update, _UNAVAILABLE)
        return True
    except Exception:  # noqa: BLE001 - a message handler must never raise
        logger.exception("doc_chat_failed", extra={"user_id": uid})
        await reply(update, "❌ جستجو در اسناد با خطا مواجه شد؛ دوباره تلاش کنید.")
        return True

    session.touch()
    await reply(update, format_answer(question, list(chunks)))
    return True


# ── helpers ───────────────────────────────────────────────────────────────


def _document_name(document: Any) -> str:
    name = getattr(document, "file_name", "") or getattr(document, "file_id", "")
    return str(name or "سند")


def _resolve_document(documents: list[Any], raw: str) -> Any | None:
    """Resolve ``"2"`` (1-based index from ``/docs``) or a raw ``file_id``."""
    text = raw.strip()
    for index, document in enumerate(documents, start=1):
        if text == str(index):
            return document
    for document in documents:
        if text == str(getattr(document, "file_id", "")):
            return document
    return None
