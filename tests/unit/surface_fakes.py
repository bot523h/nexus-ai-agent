"""Duck-typed stand-ins for PTB ``update`` / ``context`` objects.

The ``bot/surface`` handlers only touch those objects through ``getattr``
and ``await msg.reply_text(...)``, so these tiny fakes drive them end-to-end
without python-telegram-bot.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class FakeMessage:
    def __init__(self, text: str = "", document: Any = None) -> None:
        self.text = text
        self.document = document
        self.replies: list[tuple[str, dict[str, Any]]] = []
        self.polls: list[dict[str, Any]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> Any:
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.replies), text=text)

    async def reply_poll(self, **kwargs: Any) -> Any:
        self.polls.append(kwargs)
        return SimpleNamespace(poll=SimpleNamespace(id=f"native-{len(self.polls)}"))

    @property
    def last(self) -> str:
        return self.replies[-1][0] if self.replies else ""


class FakeCallbackQuery:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[Any] = []
        self.edits: list[str] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)


class FakeBot:
    """Records outgoing messages; ``members`` drives ``get_chat_member``."""

    def __init__(self, members: dict[tuple[str, int], str] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.members = members or {}
        self.fail_for: set[int] = set()

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        if chat_id in self.fail_for:
            raise RuntimeError("blocked")
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=len(self.sent))

    async def get_chat_member(self, chat_id: str, user_id: int) -> Any:
        status = self.members.get((str(chat_id), int(user_id)), "left")
        return SimpleNamespace(status=status)


def make_update(
    user_id: int = 100,
    chat_id: int | None = None,
    text: str = "",
    *,
    callback: FakeCallbackQuery | None = None,
    poll_answer: Any = None,
    message: bool = True,
) -> SimpleNamespace:
    cid = user_id if chat_id is None else chat_id
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, first_name="Tester"),
        effective_chat=SimpleNamespace(id=cid),
        message=FakeMessage(text) if message else None,
        edited_message=None,
        callback_query=callback,
        poll_answer=poll_answer,
    )


def make_context(
    args: list[str] | None = None,
    bot: FakeBot | None = None,
    bot_data: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        args=list(args or []),
        bot=bot,
        application=SimpleNamespace(bot_data=bot_data if bot_data is not None else {}),
    )
