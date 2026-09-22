"""Duck-typed accessors over the python-telegram-bot ``update`` / ``context`` objects.

The surface modules in this package deliberately **never import ``telegram``**:

* the frozen architecture test (``tests/architecture/test_import_boundaries.py``)
  only tolerates that import in grandfathered files, and ``bot/surface/`` is
  not one of them;
* keeping the command logic framework-free makes every handler unit-testable
  with a handful of ``SimpleNamespace`` fakes — no PTB install, no event loop,
  no network.

Registration is unaffected: PTB only ever awaits ``handler(update, context)``,
so a plain coroutine taking two parameters is a valid ``CommandHandler``,
``CallbackQueryHandler`` or ``MessageHandler`` target.

Every accessor tolerates a malformed update (missing message, no
``effective_user``, no ``args``…) and degrades to an empty value or a silent
no-op, because an exception raised at dispatcher level would drop the update
instead of answering it.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "args",
    "bot_data",
    "bot_of",
    "callback_data",
    "chat_id",
    "message_of",
    "message_text",
    "reply",
    "user_id",
    "user_name",
]


def _attr(source: Any, name: str) -> Any:
    return getattr(source, name, None)


def user_id(update: Any) -> int | None:
    """Telegram user id of the caller, from an update or an inline callback."""
    user = _attr(update, "effective_user")
    if user is None:
        query = _attr(update, "callback_query")
        user = _attr(query, "from_user")
    identifier = _attr(user, "id")
    try:
        return int(identifier) if identifier is not None else None
    except (TypeError, ValueError):
        return None


def user_name(update: Any) -> str:
    """Best-effort display name (first name, username, then the numeric id)."""
    user = _attr(update, "effective_user")
    if user is None:
        query = _attr(update, "callback_query")
        user = _attr(query, "from_user")
    for attribute in ("first_name", "username", "full_name"):
        value = _attr(user, attribute)
        if isinstance(value, str) and value.strip():
            return value.strip()
    identifier = user_id(update)
    return f"کاربر {identifier}" if identifier is not None else "کاربر"


def chat_id(update: Any) -> int | None:
    """Telegram chat id the update came from."""
    chat = _attr(update, "effective_chat")
    identifier = _attr(chat, "id")
    try:
        return int(identifier) if identifier is not None else None
    except (TypeError, ValueError):
        return None


def message_of(update: Any) -> Any | None:
    """The message to answer (edited messages count as messages)."""
    return _attr(update, "message") or _attr(update, "edited_message")


def message_text(update: Any) -> str:
    """Text of the triggering message; ``""`` when there is none."""
    text = _attr(message_of(update), "text")
    return text if isinstance(text, str) else ""


def args(context: Any) -> list[str]:
    """Command arguments as strings (``/cmd a b`` → ``["a", "b"]``)."""
    raw = _attr(context, "args")
    if not raw:
        return []
    return [str(item) for item in raw]


def bot_of(context: Any) -> Any | None:
    """The runtime bot, when the context carries one."""
    return _attr(context, "bot")


def bot_data(context: Any) -> dict[str, Any]:
    """``application.bot_data`` (falling back to ``context.bot_data``)."""
    application = _attr(context, "application")
    data = _attr(application, "bot_data")
    if data is None:
        data = _attr(context, "bot_data")
    return data if isinstance(data, dict) else {}


def callback_data(update: Any) -> str:
    """Payload of an inline-keyboard callback; ``""`` for plain updates."""
    data = _attr(_attr(update, "callback_query"), "data")
    return data if isinstance(data, str) else ""


async def reply(update: Any, text: str, **kwargs: Any) -> Any | None:
    """Reply to the triggering message; returns the sent message (or ``None``)."""
    message = message_of(update)
    if message is None:
        return None
    send = _attr(message, "reply_text")
    if send is None:
        return None
    return await send(text, **kwargs)
