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
    "FakeChunk",
    "FakeDocInfo",
    "FakeEngine",
    "FakeMessage",
    "FakeUpdate",
    "make_context",
    "make_update",
]


class FakeBot:
    """Minimal stand-in for ``telegram.Bot``."""

    def __init__(self, member_status: str = "member") -> None:
        self.sent: list[tuple[int, str]] = []
        self.member_status = member_status

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        self.sent.append((chat_id, text))

    async def get_chat_member(self, chat_id: Any, user_id: int) -> Any:
        return type("Member", (), {"status": self.member_status})()


class FakeMessage:
    """Records ``reply_text`` calls instead of talking to Telegram."""

    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[str] = []

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
    ) -> None:
        self.message = FakeMessage(text)
        self.edited_message = None
        self.callback_query = None
        self._user_id = user_id
        self._chat_id = chat_id
        self._first_name = first_name

    @property
    def effective_user(self) -> Any | None:
        if self._user_id is None:
            return None
        return type("User", (), {"id": self._user_id, "first_name": self._first_name})()

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
) -> FakeUpdate:
    """Build an update for a command or a free-text message."""
    return FakeUpdate(user_id=user_id, chat_id=chat_id, text=text, first_name=first_name)


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
