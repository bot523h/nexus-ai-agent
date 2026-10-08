"""Contract tests for the SQLite adapter behind ``ConversationStorePort`` (task-110).

The port had no implementation before this adapter, so nothing proved that a
store actually matched its shape. These tests pin both halves:

* the adapter is *structurally* compatible with the port (same method names,
  same parameter names — so keyword calls through the port still work — and the
  same annotations);
* the adapter behaves like a durable transcript: order, thread isolation,
  persistence across reconnects, and fail-closed input validation.
"""

from __future__ import annotations

from inspect import signature
from typing import get_type_hints

import pytest

from nexus_ai_agent.adapters.conversation_store_sqlite import (
    ALLOWED_ROLES,
    SqliteConversationStore,
)
from nexus_ai_agent.application.ports.conversation_store import ConversationStorePort

PORT_METHOD_PARAMS: dict[str, tuple[str, ...]] = {
    "append_message": ("thread_id", "role", "content"),
    "list_messages": ("thread_id",),
}


@pytest.mark.parametrize(("method", "params"), sorted(PORT_METHOD_PARAMS.items()))
def test_adapter_matches_the_port_signature(method: str, params: tuple[str, ...]) -> None:
    port_method = getattr(ConversationStorePort, method)
    impl_method = getattr(SqliteConversationStore, method)

    port_names = [n for n in signature(port_method).parameters if n != "self"]
    impl_names = [n for n in signature(impl_method).parameters if n != "self"]
    assert impl_names[: len(port_names)] == port_names, (
        f"{method}: parameter names must match the port so keyword calls through "
        "the port keep working"
    )

    port_hints = get_type_hints(port_method)
    impl_hints = get_type_hints(impl_method)
    for name in port_names:
        assert str(port_hints[name]) == str(impl_hints[name]), f"{method}({name}) type drifted"
    assert str(port_hints["return"]) == str(impl_hints["return"]), (
        f"{method} return type drifted from the port"
    )


async def test_append_then_list_round_trip(tmp_path) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        message_id = await store.append_message("tg:12345", "user", "سلام")
        await store.append_message("tg:12345", "assistant", "در خدمتم")

        history = await store.list_messages("tg:12345")

    assert len(message_id) == 32
    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "سلام"),
        ("assistant", "در خدمتم"),
    ]
    assert {m["message_id"] for m in history} == {message_id, *{m["message_id"] for m in history}}
    assert all(m["created_at"] for m in history)


async def test_threads_are_isolated(tmp_path) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        await store.append_message("tg:1", "user", "one")
        await store.append_message("tg:2", "user", "two")

        assert [m["content"] for m in await store.list_messages("tg:1")] == ["one"]
        assert [m["content"] for m in await store.list_messages("tg:2")] == ["two"]
        assert sorted(await store.threads()) == ["tg:1", "tg:2"]
        assert await store.count("tg:1") == 1


async def test_history_survives_a_reconnect(tmp_path) -> None:
    path = tmp_path / "durable.sqlite"
    first = SqliteConversationStore(path)
    await first.append_message("tg:9", "user", "persist me")
    await first.aclose()

    second = SqliteConversationStore(path)
    history = await second.list_messages("tg:9")
    await second.aclose()

    assert [m["content"] for m in history] == ["persist me"]


async def test_limit_keeps_the_most_recent_messages_in_chronological_order(tmp_path) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        for index in range(5):
            await store.append_message("tg:1", "user", f"m{index}")

        recent = await store.list_messages("tg:1", limit=2)

    assert [m["content"] for m in recent] == ["m3", "m4"]


async def test_role_is_canonicalised_on_write(tmp_path) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        await store.append_message("tg:1", "  User ", "hi")

        history = await store.list_messages("tg:1")

    assert history[0]["role"] == "user"


async def test_unknown_role_is_rejected(tmp_path) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        with pytest.raises(ValueError, match="unknown conversation role"):
            await store.append_message("tg:1", "wizard", "hi")

        assert await store.count("tg:1") == 0


@pytest.mark.parametrize("role", sorted(ALLOWED_ROLES))
async def test_every_allowed_role_round_trips(tmp_path, role: str) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        await store.append_message("tg:1", role, "ok")
        assert (await store.list_messages("tg:1"))[0]["role"] == role


@pytest.mark.parametrize(
    ("thread_id", "role", "content"),
    [
        ("", "user", "hi"),
        ("   ", "user", "hi"),
        ("tg:1", "", "hi"),
        ("tg:1", "user", 123),  # type: ignore[arg-type]
    ],
)
async def test_invalid_input_fails_closed(
    tmp_path, thread_id: str, role: str, content: str
) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        with pytest.raises((ValueError, TypeError)):
            await store.append_message(thread_id, role, content)


async def test_oversized_content_is_rejected(tmp_path) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite", max_content_length=10) as store:
        with pytest.raises(ValueError, match="over the 10 limit"):
            await store.append_message("tg:1", "user", "x" * 11)


async def test_list_messages_rejects_blank_thread_id(tmp_path) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        with pytest.raises(ValueError, match="non-empty string"):
            await store.list_messages("  ")


async def test_clear_thread_removes_only_that_thread(tmp_path) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        await store.append_message("tg:1", "user", "drop me")
        await store.append_message("tg:2", "user", "keep me")

        removed = await store.clear_thread("tg:1")

        assert removed == 1
        assert await store.count("tg:1") == 0
        assert await store.count("tg:2") == 1


async def test_in_memory_database_supported() -> None:
    async with SqliteConversationStore(":memory:") as store:
        await store.append_message("tg:mem", "user", "hi")
        assert len(await store.list_messages("tg:mem")) == 1


async def test_iter_threads_streams_all_thread_ids(tmp_path) -> None:
    async with SqliteConversationStore(tmp_path / "conv.sqlite") as store:
        for thread_id in ("tg:3", "tg:1", "tg:2"):
            await store.append_message(thread_id, "user", "x")

        streamed = [thread_id async for thread_id in store.iter_threads()]

    assert streamed == ["tg:1", "tg:2", "tg:3"]


async def test_aclose_is_idempotent(tmp_path) -> None:
    store = SqliteConversationStore(tmp_path / "conv.sqlite")
    await store.connect()
    await store.aclose()
    await store.aclose()  # must not raise on an already-closed connection
