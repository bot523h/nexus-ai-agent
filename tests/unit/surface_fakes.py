"""Test doubles for the framework-free ``bot/surface`` layer.

Because the surface modules never import ``telegram``, a handful of small
classes is enough to drive a whole command: no PTB install, no event loop, no
network. :class:`FakeUpdate` also records every reply, so assertions can read
exactly what the user would have seen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexus_ai_agent.features.rag import RAGUnavailable

__all__ = [
    "FakeBot",
    "FakeCallback",
    "FakeChunk",
    "FakeDocInfo",
    "FakeEngine",
    "FakeMessage",
    "FakeUpdate",
    "make_context",
    "make_update",
]


class FakeBot:
    """Minimal stand-in for ``telegram.Bot``.

    Records every call so a test can assert on what the *engine* asked the
    platform to do, not only on what the handler replied. ``raises`` makes the
    failure paths (insufficient rights, unknown chat) reachable without
    importing ``telegram.error`` — the accessors catch on ``Exception``.
    """

    def __init__(
        self,
        member_status: str = "member",
        *,
        member_count: int = 150,
        raises: Exception | None = None,
    ) -> None:
        self.sent: list[tuple[int, str]] = []
        self.member_status = member_status
        self.member_count = member_count
        self._raises = raises
        self.pinned: list[tuple[int, int]] = []
        self.deleted: list[tuple[int, int]] = []
        self.banned: list[tuple[int, int]] = []
        self.unbanned: list[tuple[int, int]] = []

    def _guard(self) -> None:
        if self._raises is not None:
            raise self._raises

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self._guard()
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat_id: Any, user_id: int) -> Any:
        return type("Member", (), {"status": self.member_status})()

    async def pin_chat_message(self, chat_id: int, message_id: int, **_: Any) -> None:
        self._guard()
        self.pinned.append((chat_id, message_id))

    async def delete_message(self, chat_id: int, message_id: int, **_: Any) -> None:
        self._guard()
        self.deleted.append((chat_id, message_id))

    async def ban_chat_member(self, chat_id: int, user_id: int, **_: Any) -> None:
        self._guard()
        self.banned.append((chat_id, user_id))

    async def unban_chat_member(self, chat_id: int, user_id: int, **_: Any) -> None:
        self._guard()
        self.unbanned.append((chat_id, user_id))

    async def get_chat_member_count(self, chat_id: int, **_: Any) -> int:
        self._guard()
        return self.member_count


class FakeCallback:
    """Stand-in for ``telegram.CallbackQuery``.

    ``answer`` accepts the ``text=`` toast keyword the onboarding surface uses
    for payloads it has no branch for, and ``edit_message_text`` records the new
    body so a test can prove the engine — not a fixed string — wrote it.
    """

    def __init__(self, data: str = "", *, user_id: int | None = None) -> None:
        self.data = data
        self.answers: list[str | None] = []
        self.edits: list[str] = []
        self.from_user = type("User", (), {"id": user_id, "language_code": "en"})()

    async def answer(self, **kwargs: Any) -> None:
        self.answers.append(kwargs.get("text"))

    async def edit_message_text(self, text: str, **_: Any) -> None:
        self.edits.append(text)


class FakeMessage:
    """Records ``reply_text`` calls instead of talking to Telegram."""

    def __init__(
        self,
        text: str | None = None,
        *,
        message_id: int | None = None,
        author_id: int | None = None,
    ) -> None:
        self.text = text
        self.message_id = message_id
        self.replies: list[str] = []
        if author_id is not None:
            self.from_user = type("User", (), {"id": author_id})()

    async def reply_text(self, text: str, **_: Any) -> FakeMessage:
        self.replies.append(text)
        return self

    async def reply_document(self, *_: Any, **__: Any) -> None:  # pragma: no cover
        return None

    async def reply_photo(self, *_: Any, **__: Any) -> None:  # pragma: no cover
        return None


class FakeUpdate:
    """Duck-typed ``telegram.Update`` for command handlers."""

    def __init__(
        self,
        *,
        user_id: int | None = 1,
        chat_id: int | None = 10,
        text: str | None = None,
        first_name: str | None = "تست",
        callback: FakeCallback | None = None,
        language_code: str | None = "en",
        reply_to: FakeMessage | None = None,
    ) -> None:
        self.message = FakeMessage(text)
        self.message.reply_to_message = reply_to
        self.edited_message = None
        self.callback_query = callback
        self._user_id = user_id
        self._chat_id = chat_id
        self._first_name = first_name
        self._language_code = language_code

    @property
    def effective_user(self) -> Any | None:
        if self._user_id is None:
            return None
        return type(
            "User",
            (),
            {
                "id": self._user_id,
                "first_name": self._first_name,
                "language_code": self._language_code,
            },
        )()

    @property
    def effective_chat(self) -> Any | None:
        if self._chat_id is None:
            return None
        return type("Chat", (), {"id": self._chat_id})()

    @property
    def replies(self) -> list[str]:
        """Everything the handler sent back, in order."""
        return self.message.replies

    @property
    def last_reply(self) -> str:
        """The most recent reply; ``""`` when nothing was sent."""
        return self.message.replies[-1] if self.message.replies else ""


def make_update(
    *,
    user_id: int | None = 1,
    chat_id: int | None = 10,
    text: str | None = None,
    first_name: str | None = "تست",
    callback: FakeCallback | None = None,
    language_code: str | None = "en",
    reply_to: FakeMessage | None = None,
) -> FakeUpdate:
    """Build an update for a command, a free-text message, or a callback."""
    return FakeUpdate(
        user_id=user_id,
        chat_id=chat_id,
        text=text,
        first_name=first_name,
        callback=callback,
        language_code=language_code,
        reply_to=reply_to,
    )


def make_context(args: list[str] | None = None, bot: Any = None, **bot_data: Any) -> Any:
    """Build a context carrying ``args``, a bot and ``bot_data``."""
    application = type("Application", (), {"bot_data": dict(bot_data)})()
    return type(
        "Context",
        (),
        {
            "args": list(args or []),
            "bot": bot,
            "bot_data": dict(bot_data),
            "application": application,
        },
    )()


@dataclass(frozen=True, slots=True)
class FakeDocInfo:
    """Stand-in for :class:`~nexus_ai_agent.features.rag.DocumentInfo`."""

    file_id: str
    file_name: str
    chunks: int = 1
    added_at: str = "2026-09-21T00:00:00+00:00"


@dataclass(frozen=True, slots=True)
class FakeChunk:
    """Stand-in for :class:`~nexus_ai_agent.features.rag.RetrievedChunk`."""

    file_id: str
    file_name: str
    text: str
    score: float = 1.0
    chunk_id: str = ""


class FakeEngine:
    """Duck-typed stand-in for :class:`AdvancedRAGEngine`.

    Implements only the three members the docs surface uses; construct it with
    ``unavailable=True`` to exercise the fail-closed path.
    """

    def __init__(
        self,
        documents: list[tuple[str, str, int]] | None = None,
        *,
        unavailable: bool = False,
        answers: dict[str, list[tuple[str, str]]] | None = None,
    ) -> None:
        # ``documents``: (file_id, file_name, chunks)
        self.documents = [FakeDocInfo(fid, name, chunks) for fid, name, chunks in documents or []]
        self.unavailable = unavailable
        self.answers = answers or {}
        self.deleted: list[str] = []
        self.queries: list[str] = []

    def _guard(self) -> None:
        if self.unavailable:
            raise RAGUnavailable("chromadb is not installed (fake engine)")

    async def list_documents(self, user_id: int) -> list[FakeDocInfo]:
        self._guard()
        return list(self.documents)

    async def delete_document(self, user_id: int, file_id: str) -> bool:
        self._guard()
        remaining = [item for item in self.documents if item.file_id != file_id]
        if len(remaining) == len(self.documents):
            return False
        self.documents = remaining
        self.deleted.append(file_id)
        return True

    async def retrieve(self, user_id: int, question: str, top_k: int = 3) -> list[FakeChunk]:
        self._guard()
        self.queries.append(question)
        return [
            FakeChunk(file_id=fid, file_name=fid, text=text, chunk_id=f"{fid}:0")
            for fid, text in self.answers.get(question, [])[:top_k]
        ]
