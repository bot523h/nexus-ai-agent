"""Duck-typed accessors over the PTB ``update`` / ``context`` objects.

The surface modules in this package deliberately do **not** import
``telegram``: the frozen import-boundary baseline
(``tests/architecture/legacy_baseline.json``) only tolerates that import in
grandfathered files, and — exactly like ``bot/slideshow.py`` — keeping the
command logic framework-free makes it unit-testable with plain fakes.  The
handlers are still directly registrable with ``CommandHandler`` because PTB
only awaits ``handler(update, context)``; the type of both arguments is
irrelevant at runtime.

Every accessor tolerates missing attributes (``None`` message, no
``effective_user``...) so a malformed update degrades to a silent no-op
instead of an exception in the dispatcher.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "args",
    "bot_of",
    "bot_data",
    "chat_id",
    "message_of",
    "message_text",
    "reply",
    "user_id",
]


def user_id(update: Any) -> int | None:
    user = getattr(update, "effective_user", None)
    if user is None:
        query = getattr(update, "callback_query", None)
        user = getattr(query, "from_user", None)
    ident = getattr(user, "id", None)
    return int(ident) if ident is not None else None


def chat_id(update: Any) -> int | None:
    chat = getattr(update, "effective_chat", None)
    ident = getattr(chat, "id", None)
    return int(ident) if ident is not None else None


def message_of(update: Any) -> Any | None:
    return getattr(update, "message", None) or getattr(update, "edited_message", None)


def message_text(update: Any) -> str:
    msg = message_of(update)
    text = getattr(msg, "text", None)
    return text if isinstance(text, str) else ""


def args(context: Any) -> list[str]:
    raw = getattr(context, "args", None)
    return [str(a) for a in raw] if raw else []


def bot_of(context: Any) -> Any | None:
    return getattr(context, "bot", None)


def bot_data(context: Any) -> dict[str, Any]:
    application = getattr(context, "application", None)
    data = getattr(application, "bot_data", None)
    if data is None:
        data = getattr(context, "bot_data", None)
    return data if isinstance(data, dict) else {}


async def reply(update: Any, text: str, **kwargs: Any) -> Any | None:
    """Reply to the triggering message; returns the sent message (or ``None``)."""
    msg = message_of(update)
    if msg is None:
        return None
    return await msg.reply_text(text, **kwargs)
