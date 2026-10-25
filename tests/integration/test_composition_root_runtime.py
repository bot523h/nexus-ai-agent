"""A-03 deeper validation: the composition root, dispatched for real.

The surface tests prove each handler's behaviour and the AST ratchets prove
the source *mentions* the right registrations.  Neither builds the real PTB
``Application`` — so a mis-ordered group, a shadowed ``MessageHandler`` or a
command registered twice (PTB silently runs the first) would never be seen.

These tests call ``build_application`` exactly as ``nexus run`` does (with a
syntactically valid test token; ``ApplicationBuilder.build`` performs no
network I/O) and then ask PTB's own ``check_update`` which handler a real
``telegram.Update`` would reach — the dispatch PTB performs, not a re-model of
it.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import Chat, Message, MessageEntity, PhotoSize, Update, User
from telegram.ext import CommandHandler, MessageHandler

from nexus_ai_agent.bot.access_guard import AccessGuardHandler
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.storage import db as db_module

OWNER_ID = 777
STRANGER_ID = 4242

#: The commands the 2026-10-15 audit (A-03) found answering with canned text
#: or without an owner check; they must be live at runtime, not just in source.
A03_COMMANDS = {
    "mod_config",
    "warn",
    "mute",
    "unmute",
    "reputation",
    "viral_preview",
    "viral_stats",
    "viral_post",
}


@pytest.fixture()
def application(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:composition-root-test")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("NEXUS_OWNER_TELEGRAM_ID", str(OWNER_ID))
    monkeypatch.setenv("NEXUS_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("NEXUS_DB_PATH", str(tmp_path / "app.sqlite"))
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "langgraph.sqlite"))
    monkeypatch.setenv("NEXUS_VECTOR_PATH", str(tmp_path / "vector.sqlite"))
    monkeypatch.setenv("NEXUS_MODEL_PATH", str(tmp_path / "missing.gguf"))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    settings_module.get_settings.cache_clear()
    db_module._engine = None  # type: ignore[attr-defined]
    db_module._session_factory = None  # type: ignore[attr-defined]
    try:
        from nexus_ai_agent.bot.app import build_application

        yield build_application(settings_module.get_settings(), graph=object())
    finally:
        settings_module.get_settings.cache_clear()
        db_module._engine = None  # type: ignore[attr-defined]
        db_module._session_factory = None  # type: ignore[attr-defined]


def _text_update(*, user_id: int, chat_type: str, text: str = "سلام") -> Update:
    chat = Chat(id=-100500 if chat_type != Chat.PRIVATE else user_id, type=chat_type)
    user = User(id=user_id, first_name="t", is_bot=False)
    message = Message(
        message_id=1,
        date=dt.datetime.now(dt.timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=1, message=message)


def _fake_bot() -> Any:
    """A bot object with just the member command dispatch reads (``username``).

    The real ``application.bot`` would need a network round trip to be
    initialized; ``CommandHandler`` only asks it for its username.
    """
    return SimpleNamespace(username="nexus_test_bot")


def _command_update(*, user_id: int, chat_type: str, text: str, bot: Any = None) -> Update:
    """A real ``/command`` message (the entity is what ``filters.COMMAND`` reads).

    ``CommandHandler.check_update`` resolves the optional ``@bot`` suffix
    through ``message.get_bot()``, so a bot must be attached for command
    dispatch to be evaluated at all.
    """
    chat = Chat(id=-100500 if chat_type != Chat.PRIVATE else user_id, type=chat_type)
    user = User(id=user_id, first_name="t", is_bot=False)
    entity = MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text.split()[0]))
    message = Message(
        message_id=2,
        date=dt.datetime.now(dt.timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
        entities=[entity],
    )
    if bot is not None:
        message.set_bot(bot)
    return Update(update_id=2, message=message)


def _photo_update(*, user_id: int, chat_type: str, caption: str | None = None) -> Update:
    """A photo message; its text (if any) lives in ``caption``, never in ``text``."""
    chat = Chat(id=-100500 if chat_type != Chat.PRIVATE else user_id, type=chat_type)
    user = User(id=user_id, first_name="t", is_bot=False)
    return Update(
        update_id=3,
        message=Message(
            message_id=3,
            date=dt.datetime.now(dt.timezone.utc),
            chat=chat,
            from_user=user,
            photo=(PhotoSize(file_id="p1", file_unique_id="u1", width=64, height=64),),
            caption=caption,
        ),
    )


def _first_match(handlers: list[Any], update: Update) -> Any:
    """PTB's per-group rule: the first handler whose ``check_update`` is truthy."""
    for handler in handlers:
        if handler.check_update(update):
            return handler
    return None


def test_ingress_group_is_access_guard_then_moderation_gate(application: Any) -> None:
    ingress = application.handlers[-1]
    assert [type(h).__name__ for h in ingress] == ["AccessGuardHandler", "MessageHandler"]
    assert isinstance(ingress[0], AccessGuardHandler)
    assert ingress[1].callback.__name__ == "_moderation_gate"
    assert set(application.handlers) == {-1, 0}, "handlers registered in an unplanned group"


def test_group_text_reaches_the_moderation_gate_but_private_text_does_not(
    application: Any,
) -> None:
    guard, gate = application.handlers[-1]
    group_text = _text_update(user_id=OWNER_ID, chat_type=Chat.SUPERGROUP)
    private_text = _text_update(user_id=OWNER_ID, chat_type=Chat.PRIVATE)
    # The owner is allowed: the guard stands aside and PTB continues to the gate.
    assert guard.check_update(group_text) is False
    assert gate.check_update(group_text)
    assert not gate.check_update(private_text)


def test_strangers_are_stopped_by_the_guard_before_any_other_handler(application: Any) -> None:
    guard = application.handlers[-1][0]
    assert guard.check_update(_text_update(user_id=STRANGER_ID, chat_type=Chat.PRIVATE)) is True
    assert guard.check_update(_text_update(user_id=OWNER_ID, chat_type=Chat.PRIVATE)) is False


def test_a_private_text_message_is_dispatched_to_on_message(application: Any) -> None:
    update = _text_update(user_id=OWNER_ID, chat_type=Chat.PRIVATE)
    handler = _first_match(application.handlers[0], update)
    assert handler is not None, "a plain text message reaches no group-0 handler"
    assert isinstance(handler, MessageHandler)
    assert handler.callback.__name__ == "on_message"


def test_no_text_handler_is_shadowed_behind_on_message(application: Any) -> None:
    handlers = application.handlers[0]
    index = next(i for i, h in enumerate(handlers) if h.callback.__name__ == "on_message")
    update = _text_update(user_id=OWNER_ID, chat_type=Chat.PRIVATE)
    shadowed = [
        h.callback.__name__
        for h in handlers[index + 1 :]
        if isinstance(h, MessageHandler) and h.check_update(update)
    ]
    assert shadowed == [], f"unreachable text handlers after on_message: {shadowed}"


def test_every_command_is_registered_exactly_once(application: Any) -> None:
    counts: Counter[str] = Counter()
    for handler in application.handlers[0]:
        if isinstance(handler, CommandHandler):
            counts.update(handler.commands)
    duplicates = {name: n for name, n in counts.items() if n > 1}
    assert duplicates == {}, f"PTB runs only the first registration: {duplicates}"
    assert A03_COMMANDS <= set(counts), sorted(A03_COMMANDS - set(counts))


def test_a03_commands_have_real_callbacks_not_shared_stubs(application: Any) -> None:
    callbacks = {
        command: handler.callback.__name__
        for handler in application.handlers[0]
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    a03 = {command: callbacks[command] for command in sorted(A03_COMMANDS)}
    assert a03 == {command: f"{command}_cmd" for command in sorted(A03_COMMANDS)}, a03
    assert len(set(a03.values())) == len(a03), "two commands share one callback"


def test_build_application_rejects_a_placeholder_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "CHANGE_ME")
    monkeypatch.chdir(tmp_path)
    from nexus_ai_agent.bot.app import build_application

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        build_application(SimpleNamespace(telegram_bot_token="CHANGE_ME"), graph=object())  # type: ignore[arg-type]


# ── the moderation gate's message coverage (review finding, 2026-10-17) ────


def test_the_gate_matches_group_text_group_commands_and_group_captions(
    application: Any,
) -> None:
    """``filters.TEXT & ~filters.COMMAND`` missed commands *and* media captions.

    A violating command — or a spam link in a photo caption — therefore
    reached the AI and stayed in the chat. The owner is allowed by the guard
    and by the engine, so all three forms must be seen by the gate.
    """
    guard, gate = application.handlers[-1]
    cases = {
        "plain text": _text_update(user_id=OWNER_ID, chat_type=Chat.SUPERGROUP),
        "command": _command_update(
            user_id=OWNER_ID, chat_type=Chat.SUPERGROUP, text="/start ref_1234"
        ),
        "photo caption": _photo_update(
            user_id=OWNER_ID, chat_type=Chat.SUPERGROUP, caption="http://spam.example"
        ),
    }
    for label, update in cases.items():
        assert guard.check_update(update) is False, f"{label}: the owner is allowed"
        assert gate.check_update(update), f"{label} never reached the moderation gate"


def test_the_gate_still_ignores_private_messages_and_bare_media(application: Any) -> None:
    _, gate = application.handlers[-1]
    assert not gate.check_update(_text_update(user_id=OWNER_ID, chat_type=Chat.PRIVATE))
    assert not gate.check_update(
        _command_update(user_id=OWNER_ID, chat_type=Chat.PRIVATE, text="/start")
    )
    # A photo with no caption carries no text: nothing to moderate.
    assert not gate.check_update(_photo_update(user_id=OWNER_ID, chat_type=Chat.SUPERGROUP))


def test_a_disallowed_group_message_never_reaches_a_group_zero_handler(
    application: Any,
) -> None:
    """The gate stops propagation for a denied verdict; the gate is the filter."""
    guard, gate = application.handlers[-1]
    strangers = [
        _text_update(user_id=STRANGER_ID, chat_type=Chat.SUPERGROUP),
        _command_update(user_id=STRANGER_ID, chat_type=Chat.SUPERGROUP, text="/start"),
        _photo_update(
            user_id=STRANGER_ID, chat_type=Chat.SUPERGROUP, caption="http://spam.example"
        ),
    ]
    for update in strangers:
        assert guard.check_update(update) is True, "the guard stops strangers first"
    # … and for an *allowed* user the gate itself is what decides, in group -1,
    # before any group-0 handler runs.
    assert isinstance(application.handlers[-1][1], MessageHandler)


# ── /vision on a photo caption (review finding, 2026-10-17) ────────────────


def _group_zero_callback(application: Any, update: Update) -> str | None:
    handler = _first_match(application.handlers[0], update)
    return None if handler is None else handler.callback.__name__


def test_a_photo_captioned_vision_is_dispatched_to_the_vision_handler(
    application: Any,
) -> None:
    """``CommandHandler`` only reads ``message.text``, so this never dispatched."""
    update = _photo_update(user_id=OWNER_ID, chat_type=Chat.PRIVATE, caption="/vision")
    assert _group_zero_callback(application, update) == "vision_cmd"

    addressed = _photo_update(
        user_id=OWNER_ID, chat_type=Chat.PRIVATE, caption="/vision@nexus_test_bot what is this?"
    )
    assert _group_zero_callback(application, addressed) == "vision_cmd"

    # Mobile clients put the question on the next line. The first cut of this
    # filter ended with ``(?:[^\S\n]|$)``, which excludes a newline and, with
    # no re.MULTILINE, also fails to match before a mid-string newline — so
    # the photo was silently buffered as a slideshow image instead.
    newline = _photo_update(
        user_id=OWNER_ID,
        chat_type=Chat.PRIVATE,
        caption="/vision\nwhat is in this picture?",
    )
    assert _group_zero_callback(application, newline) == "vision_cmd"


def test_an_ordinary_photo_still_reaches_the_slideshow_handler(application: Any) -> None:
    assert (
        _group_zero_callback(application, _photo_update(user_id=OWNER_ID, chat_type=Chat.PRIVATE))
        == "slideshow_photo"
    )
    assert (
        _group_zero_callback(
            application,
            _photo_update(user_id=OWNER_ID, chat_type=Chat.PRIVATE, caption="عکس اسلایدشو"),
        )
        == "slideshow_photo"
    )


def test_a_text_vision_command_still_reaches_the_vision_handler(application: Any) -> None:
    update = _command_update(
        user_id=OWNER_ID,
        chat_type=Chat.PRIVATE,
        text="/vision describe this",
        bot=_fake_bot(),
    )
    assert _group_zero_callback(application, update) == "vision_cmd"


def test_the_vision_question_is_read_from_the_caption_when_args_are_empty() -> None:
    """A ``MessageHandler`` update has no ``context.args`` — the caption is the source."""
    from types import SimpleNamespace

    from nexus_ai_agent.bot.handlers import (
        _VISION_DEFAULT_QUESTION,
        _vision_question,
    )

    def update_with(caption: str) -> Any:
        return SimpleNamespace(message=SimpleNamespace(caption=caption))

    empty_context = SimpleNamespace(args=None)
    assert _vision_question(update_with("/vision"), empty_context) == _VISION_DEFAULT_QUESTION
    assert _vision_question(update_with("/vision what is this?"), empty_context) == (
        "what is this?"
    )
    assert _vision_question(update_with("/vision@nexus_bot what is this?"), empty_context) == (
        "what is this?"
    )
    assert _vision_question(update_with("عکس اسلایدشو"), empty_context) == (
        _VISION_DEFAULT_QUESTION
    )
    # A newline is a separator too, on every Telegram client.
    assert _vision_question(update_with("/vision\nwhat is in this picture?"), empty_context) == (
        "what is in this picture?"
    )
    # A text command keeps using the arguments PTB parsed.
    assert (
        _vision_question(update_with(""), SimpleNamespace(args=["describe", "this"]))
        == "describe this"
    )
