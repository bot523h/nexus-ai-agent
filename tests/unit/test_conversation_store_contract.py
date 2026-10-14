"""ConversationStore contract tests.
Nails down the behavioural contract of the real
``ConversationStore`` (``features/conversation_store.py``).
Structured as a reusable suite so that any future adapter
sharing the same storage semantics can run the same assertions.

Current implementation under test
----------------------------------
Synchronous SQLite class in
``nexus_ai_agent.features.conversation_store.ConversationStore``.

Architecture gap
----------------
``ConversationStorePort`` (``application/ports/conversation_store.py``)
is the *async* hexagonal port with ``append_message`` / ``list_messages``.
``ConversationStore`` (``features/conversation_store.py``) is the LIVE
implementation wired to GeminiEngine; its API is NOT the port.

PR#33 (arena/01a0c3aa-nexus-ai-agent) contains
``adapters/conversation_store_sqlite.py`` that implements the port but
has NOT been merged into main at SHA df3c9d16.

This gap is documented at the bottom of this module and in
docs/architecture/PORTS.md.

Running
-------
pytest -q tests/unit/test_conversation_store_contract.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nexus_ai_agent.features.conversation_store import ConversationStore


@pytest.fixture()
def store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(db_path=str(tmp_path / "test_conv.sqlite"))


# ---------------------------------------------------------------------------
# 1. Basic append and retrieval -- ordering guarantee
# ---------------------------------------------------------------------------


def test_empty_history_returns_empty_list(store: ConversationStore) -> None:
    assert store.get_history("tg:unknown") == []


def test_append_single_message_is_returned(store: ConversationStore) -> None:
    msg = {"role": "user", "parts": [{"text": "Hello"}]}
    store.append("tg:1", msg)
    history = store.get_history("tg:1")
    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert history[0]["parts"] == [{"text": "Hello"}]


def test_messages_returned_in_chronological_order(store: ConversationStore) -> None:
    for i in range(5):
        store.append(
            "tg:2",
            {"role": "user" if i % 2 == 0 else "model", "parts": [{"text": str(i)}]},
        )
    texts = [h["parts"][0]["text"] for h in store.get_history("tg:2", limit=5)]
    assert texts == ["0", "1", "2", "3", "4"]


def test_limit_returns_most_recent(store: ConversationStore) -> None:
    for i in range(10):
        store.append("tg:3", {"role": "user", "parts": [{"text": str(i)}]})
    history = store.get_history("tg:3", limit=3)
    assert len(history) == 3
    assert [h["parts"][0]["text"] for h in history] == ["7", "8", "9"]


# ---------------------------------------------------------------------------
# 2. conv_id isolation
# ---------------------------------------------------------------------------


def test_separate_conv_ids_are_isolated(store: ConversationStore) -> None:
    store.append("tg:A", {"role": "user", "parts": [{"text": "from A"}]})
    store.append("tg:B", {"role": "user", "parts": [{"text": "from B"}]})
    assert store.get_history("tg:A")[0]["parts"][0]["text"] == "from A"
    assert store.get_history("tg:B")[0]["parts"][0]["text"] == "from B"


# ---------------------------------------------------------------------------
# 3. append_batch
# ---------------------------------------------------------------------------


def test_append_batch_inserts_all_messages(store: ConversationStore) -> None:
    msgs = [
        {"role": "user", "parts": [{"text": "a"}]},
        {"role": "model", "parts": [{"text": "b"}]},
        {"role": "user", "parts": [{"text": "c"}]},
    ]
    store.append_batch("tg:batch", msgs)
    assert len(store.get_history("tg:batch", limit=10)) == 3


def test_append_batch_preserves_insertion_order(store: ConversationStore) -> None:
    msgs = [{"role": "user", "parts": [{"text": str(i)}]} for i in range(5)]
    store.append_batch("tg:order", msgs)
    texts = [h["parts"][0]["text"] for h in store.get_history("tg:order", limit=10)]
    assert texts == ["0", "1", "2", "3", "4"]


def test_append_batch_empty_list_is_noop(store: ConversationStore) -> None:
    store.append_batch("tg:noop", [])
    assert store.get_history("tg:noop") == []


# ---------------------------------------------------------------------------
# 4. clear
# ---------------------------------------------------------------------------


def test_clear_removes_all_messages(store: ConversationStore) -> None:
    for i in range(3):
        store.append("tg:clr", {"role": "user", "parts": [{"text": str(i)}]})
    store.clear("tg:clr")
    assert store.get_history("tg:clr") == []


def test_clear_only_targets_specified_conv_id(store: ConversationStore) -> None:
    store.append("tg:keep", {"role": "user", "parts": [{"text": "stay"}]})
    store.append("tg:clr2", {"role": "user", "parts": [{"text": "go"}]})
    store.clear("tg:clr2")
    assert store.get_history("tg:keep")[0]["parts"][0]["text"] == "stay"
    assert store.get_history("tg:clr2") == []


def test_clear_nonexistent_conv_id_is_noop(store: ConversationStore) -> None:
    store.clear("tg:ghost")  # must not raise
    assert store.get_history("tg:ghost") == []


# ---------------------------------------------------------------------------
# 5. trim_to_limit
# ---------------------------------------------------------------------------


def test_trim_to_limit_removes_oldest(store: ConversationStore) -> None:
    for i in range(6):
        store.append("tg:trim", {"role": "user", "parts": [{"text": str(i)}]})
    store.trim_to_limit("tg:trim", 3)
    texts = [h["parts"][0]["text"] for h in store.get_history("tg:trim", limit=10)]
    assert texts == ["3", "4", "5"]


def test_trim_to_limit_within_limit_is_noop(store: ConversationStore) -> None:
    for i in range(3):
        store.append("tg:trim2", {"role": "user", "parts": [{"text": str(i)}]})
    store.trim_to_limit("tg:trim2", 10)
    assert len(store.get_history("tg:trim2", limit=20)) == 3


def test_trim_to_limit_zero_clears_all(store: ConversationStore) -> None:
    for i in range(3):
        store.append("tg:trim3", {"role": "user", "parts": [{"text": str(i)}]})
    store.trim_to_limit("tg:trim3", 0)
    assert store.get_history("tg:trim3") == []


# ---------------------------------------------------------------------------
# 6. active_conversations
# ---------------------------------------------------------------------------


def test_active_conversations_empty_store(store: ConversationStore) -> None:
    assert store.active_conversations() == []


def test_active_conversations_returns_distinct_ids(store: ConversationStore) -> None:
    for cid in ("tg:x", "tg:y", "tg:z"):
        store.append(cid, {"role": "user", "parts": [{"text": "hi"}]})
    active = store.active_conversations()
    assert set(active) == {"tg:x", "tg:y", "tg:z"}


def test_active_conversations_after_clear_decrements(store: ConversationStore) -> None:
    for cid in ("tg:p", "tg:q"):
        store.append(cid, {"role": "user", "parts": [{"text": "hi"}]})
    store.clear("tg:p")
    active = store.active_conversations()
    assert "tg:p" not in active
    assert "tg:q" in active


# ---------------------------------------------------------------------------
# 7. Boundary inputs
# ---------------------------------------------------------------------------


def test_message_with_empty_parts_list(store: ConversationStore) -> None:
    msg = {"role": "user", "parts": []}
    store.append("tg:ep", msg)
    result = store.get_history("tg:ep")
    assert result[0]["parts"] == []


def test_message_missing_role_defaults_gracefully(store: ConversationStore) -> None:
    msg = {"parts": [{"text": "no role"}]}
    store.append("tg:nr", msg)
    result = store.get_history("tg:nr")
    assert result[0]["parts"] == [{"text": "no role"}]


def test_unicode_content_roundtrips(store: ConversationStore) -> None:
    text = "مرحبا 🌍 \u4e2d\u6587"
    store.append("tg:uni", {"role": "user", "parts": [{"text": text}]})
    assert store.get_history("tg:uni")[0]["parts"][0]["text"] == text


def test_special_characters_in_content(store: ConversationStore) -> None:
    text = "line1\nline2\ttab\"quote'apos\\back"
    store.append("tg:sc", {"role": "user", "parts": [{"text": text}]})
    assert store.get_history("tg:sc")[0]["parts"][0]["text"] == text


def test_large_content_roundtrips(store: ConversationStore) -> None:
    text = "x" * 100_000
    store.append("tg:big", {"role": "user", "parts": [{"text": text}]})
    assert store.get_history("tg:big")[0]["parts"][0]["text"] == text


# ---------------------------------------------------------------------------
# 8. Data-format compatibility
# ---------------------------------------------------------------------------


def test_multi_part_message_roundtrip(store: ConversationStore) -> None:
    msg = {
        "role": "user",
        "parts": [{"text": "hello"}, {"image_url": "http://img"}, {"text": "world"}],
    }
    store.append("tg:mp", msg)
    result = store.get_history("tg:mp")[0]
    assert result["parts"] == msg["parts"]


def test_nested_dict_values_roundtrip(store: ConversationStore) -> None:
    msg = {"role": "model", "parts": [{"tool_use": {"name": "calc", "input": {"x": 1}}}]}
    store.append("tg:nd", msg)
    result = store.get_history("tg:nd")[0]
    assert result["parts"][0]["tool_use"]["input"]["x"] == 1


# ---------------------------------------------------------------------------
# 9. Persistence across instances
# ---------------------------------------------------------------------------


def test_data_persists_across_store_instances(tmp_path: Path) -> None:
    db = str(tmp_path / "persist.sqlite")
    s1 = ConversationStore(db_path=db)
    s1.append("tg:ps", {"role": "user", "parts": [{"text": "persist me"}]})
    del s1
    s2 = ConversationStore(db_path=db)
    result = s2.get_history("tg:ps")
    assert result[0]["parts"][0]["text"] == "persist me"


# ---------------------------------------------------------------------------
# Architecture gap — documented for future adapter work
# ---------------------------------------------------------------------------
# ConversationStorePort  (application/ports/conversation_store.py)
#   async append_message(thread_id: str, role: str, content: str) -> str
#   async list_messages(thread_id: str) -> Sequence[dict]
#
# ConversationStore  (features/conversation_store.py)  — THIS module tests
#   sync  append(conv_id, dict) -> None
#   sync  append_batch(conv_id, list[dict]) -> None
#   sync  get_history(conv_id, limit) -> list[dict]
#   sync  clear(conv_id) -> None
#   sync  trim_to_limit(conv_id, n) -> None
#   sync  active_conversations() -> list[str]
#
# These two interfaces are NOT compatible.  An async adapter bridge is
# tracked in PR#33 (adapters/conversation_store_sqlite.py) but has not
# been merged into main at the base SHA of this PR (df3c9d16).
