"""Unit tests for the duck-typed PTB accessors (``bot/surface/_ptb.py``).

The accessors are the reason the surface modules can stay framework-free, so
their tolerance for malformed updates is a contract, not an accident: a
handler must degrade to silence rather than raise inside the dispatcher.
"""

from __future__ import annotations

from typing import Any

import pytest
from surface_fakes import FakeMessage, make_context, make_update

from nexus_ai_agent.bot.surface import _ptb


def test_user_id_prefers_the_update_then_the_callback() -> None:
    assert _ptb.user_id(make_update(user_id=7)) == 7

    callback = type(
        "Update",
        (),
        {
            "effective_user": None,
            "callback_query": type("Q", (), {"from_user": type("U", (), {"id": 9})()})(),
        },
    )()
    assert _ptb.user_id(callback) == 9


def test_user_id_tolerates_missing_users_and_junk_ids() -> None:
    assert _ptb.user_id(make_update(user_id=None)) is None
    assert _ptb.user_id(type("U", (), {})()) is None
    assert (
        _ptb.user_id(type("U", (), {"effective_user": type("E", (), {"id": "nope"})()})()) is None
    )


def test_chat_id_tolerates_missing_chats() -> None:
    assert _ptb.chat_id(make_update(chat_id=42)) == 42
    assert _ptb.chat_id(make_update(chat_id=None)) is None
    assert _ptb.chat_id(type("U", (), {})()) is None


def test_user_name_falls_back_through_the_name_fields() -> None:
    assert _ptb.user_name(make_update(first_name="علی")) == "علی"
    assert (
        _ptb.user_name(type("U", (), {"effective_user": type("E", (), {"username": "ali"})()})())
        == "ali"
    )
    assert _ptb.user_name(make_update(user_id=5, first_name=None)) == "کاربر 5"


def test_message_text_and_edited_messages() -> None:
    assert _ptb.message_text(make_update(text="سلام")) == "سلام"
    assert _ptb.message_text(make_update(text=None)) == ""
    edited = type(
        "U", (), {"message": None, "edited_message": type("M", (), {"text": "ویرایش"})()}
    )()
    assert _ptb.message_text(edited) == "ویرایش"


def test_args_are_stringified_and_absent_args_are_empty() -> None:
    assert _ptb.args(make_context(["1", "x"])) == ["1", "x"]
    assert _ptb.args(make_context()) == []
    assert _ptb.args(type("C", (), {})()) == []


def test_bot_data_prefers_the_application_dict() -> None:
    assert _ptb.bot_data(make_context(engines={"a": 1})) == {"engines": {"a": 1}}
    assert _ptb.bot_data(type("C", (), {"bot_data": {"b": 2}})()) == {"b": 2}
    assert _ptb.bot_data(type("C", (), {})()) == {}


def test_callback_data_is_empty_for_plain_updates() -> None:
    assert _ptb.callback_data(make_update()) == ""
    query = type("U", (), {"callback_query": type("Q", (), {"data": "pollvote_1"})()})()
    assert _ptb.callback_data(query) == "pollvote_1"


async def test_reply_is_a_no_op_without_a_message() -> None:
    """A handler on a channel post (no message object) must not explode."""
    assert (
        await _ptb.reply(type("U", (), {"message": None, "edited_message": None})(), "hi") is None
    )


async def test_reply_records_the_text_on_the_message() -> None:
    update = make_update()
    sent = await _ptb.reply(update, "سلام")
    assert update.replies == ["سلام"]
    assert sent is update.message


async def test_reply_tolerates_a_message_without_reply_text() -> None:
    message = type("M", (), {})()
    assert (
        await _ptb.reply(type("U", (), {"message": message, "edited_message": None})(), "hi")
        is None
    )


def test_the_surface_package_imports_no_telegram() -> None:
    """Frozen-boundary guard, asserted locally as well as in CI."""
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[2] / "src" / "nexus_ai_agent" / "bot" / "surface"
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & {"telegram", "sqlmodel", "langgraph"}, f"{path.name}: {imported}"


@pytest.mark.parametrize(
    "attribute",
    [
        "user_id",
        "chat_id",
        "user_name",
        "message_text",
        "callback_data",
        "reply_to_message_id",
        "reply_to_user_id",
        "user_language_code",
    ],
)
def test_accessors_never_raise_on_a_foreign_object(attribute: str) -> None:
    accessor: Any = getattr(_ptb, attribute)
    accessor(object())


# ── the quoted-message and language accessors (added with the dead-engine batch) ──


def test_reply_to_message_id_reads_the_quoted_message() -> None:
    quoted = FakeMessage("x", message_id=421)
    update = make_update(reply_to=quoted)
    assert _ptb.reply_to_message_id(update) == 421


@pytest.mark.parametrize("payload", [make_update(), make_update(text=None), object()])
def test_reply_to_message_id_is_none_without_a_quote(payload: Any) -> None:
    assert _ptb.reply_to_message_id(payload) is None


def test_reply_to_message_id_tolerates_a_junk_id() -> None:
    broken = make_update(reply_to=FakeMessage("x", message_id="not-a-number"))
    assert _ptb.reply_to_message_id(broken) is None


def test_reply_to_user_id_reads_the_quoted_author() -> None:
    quoted = FakeMessage("spam", author_id=777)
    assert _ptb.reply_to_user_id(make_update(reply_to=quoted)) == 777
    assert _ptb.reply_to_user_id(make_update()) is None


def test_user_language_code_prefers_the_effective_user() -> None:
    assert _ptb.user_language_code(make_update(language_code="fa-IR")) == "fa-IR"
    assert _ptb.user_language_code(make_update(language_code=None)) is None
    assert _ptb.user_language_code(object()) is None


def test_user_language_code_falls_back_to_the_callback_sender() -> None:
    callback_only = type(
        "Update",
        (),
        {
            "effective_user": None,
            "callback_query": type(
                "Q", (), {"from_user": type("U", (), {"language_code": " de "})(), "data": "x"}
            )(),
        },
    )()
    assert _ptb.user_language_code(callback_only) == "de"
