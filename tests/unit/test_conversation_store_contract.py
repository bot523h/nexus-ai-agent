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
            {"role": "user" if i % 2 == 0 else "model",
             "parts": [{"text": str(i)}]},
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
    store.append("tg:noop", {"role": "user", "parts": [{"text": "pre"}]})
    store.append_batch("tg:noop", [])
    assert len(store.get_history("tg:noop")) == 1


# ---------------------------------------------------------------------------
# 4. clear
# ---------------------------------------------------------------------------

def test_clear_removes_all_messages(store: ConversationStore) -> None:
    store.append("tg:clear", {"role": "user", "parts": [{"text": "x"}]})
    store.clear("tg:clear")
    assert store.get_history("tg:clear") == []


def test_clear_only_affects_target_conv(store: ConversationStore) -> None:
    store.append("tg:ca", {"role": "user", "parts": [{"text": "keep"}]})
    store.append("tg:cb", {"role": "user", "parts": [{"text": "delete"}]})
    store.clear("tg:cb")
    assert len(store.get_history("tg:ca")) == 1
    assert store.get_history("tg:cb") == []


def test_clear_nonexistent_conv_is_noop(store: ConversationStore) -> None:
    store.clear("tg:ghost")
    assert store.get_history("tg:ghost") == []


# ---------------------------------------------------------------------------
# 5. trim_to_limit
# ---------------------------------------------------------------------------

def test_trim_to_limit_removes_oldest(store: ConversationStore) -> None:
    for i in range(10):
        store.append("tg:trim", {"role": "user", "parts": [{"text": str(i)}]})
    store.trim_to_limit("tg:trim", limit=4)
    history = store.get_history("tg:trim", limit=20)
    assert len(history) == 4
    assert [h["parts"][0]["text"] for h in history] == ["6", "7", "8", "9"]


def test_trim_within_limit_is_noop(store: ConversationStore) -> None:
    for i in range(3):
        store.append("tg:short", {"role": "user", "parts": [{"text": str(i)}]})
    store.trim_to_limit("tg:short", limit=10)
    assert len(store.get_history("tg:short", limit=20)) == 3


def test_trim_to_limit_zero_clears_all(store: ConversationStore) -> None:
    for i in range(5):
        store.append("tg:zero", {"role": "user", "parts": [{"text": str(i)}]})
    store.trim_to_limit("tg:zero", limit=0)
    assert store.get_history("tg:zero") == []


# ---------------------------------------------------------------------------
# 6. active_conversations counter
# ---------------------------------------------------------------------------

def test_active_conversations_empty_store(store: ConversationStore) -> None:
    assert store.active_conversations() == 0


def test_active_conversations_counts_distinct_conv_ids(store: ConversationStore) -> None:
    for cid in ("tg:x1", "tg:x2", "tg:x3"):
        store.append(cid, {"role": "user", "parts": [{"text": "hi"}]})
    store.append("tg:x1", {"role": "model", "parts": [{"text": "hello"}]})
    assert store.active_conversations() == 3


def test_active_conversations_decrements_after_clear(store: ConversationStore) -> None:
    store.append("tg:solo", {"role": "user", "parts": [{"text": "hi"}]})
    assert store.active_conversations() == 1
    store.clear("tg:solo")
    assert store.active_conversations() == 0


# ---------------------------------------------------------------------------
# 7. Boundary and edge-case inputs
# ---------------------------------------------------------------------------

def test_message_with_empty_parts_list(store: ConversationStore) -> None:
    store.append("tg:edge", {"role": "user", "parts": []})
    history = store.get_history("tg:edge")
    assert len(history) == 1
    assert history[0]["parts"] == []


def test_message_with_missing_role_defaults_to_user(store: ConversationStore) -> None:
    store.append("tg:norole", {"parts": [{"text": "no role"}]})
    history = store.get_history("tg:norole")
    assert len(history) == 1
    assert history[0]["role"] == "user"


def test_unicode_content_stored_intact(store: ConversationStore) -> None:
    text = "\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627 \U0001f30d"
    store.append("tg:unicode", {"role": "user", "parts": [{"text": text}]})
    assert store.get_history("tg:unicode")[0]["parts"][0]["text"] == text


def test_conv_id_with_special_characters(store: ConversationStore) -> None:
    cid = "tg:123-abc_xyz:extra"
    store.append(cid, {"role": "user", "parts": [{"text": "special"}]})
    assert len(store.get_history(cid)) == 1


def test_very_long_content_stored_without_truncation(store: ConversationStore) -> None:
    big_text = "x" * 102_400
    store.append("tg:big", {"role": "user", "parts": [{"text": big_text}]})
    history = store.get_history("tg:big")
    assert len(history[0]["parts"][0]["text"]) == 102_400


# ---------------------------------------------------------------------------
# 8. Data-format compatibility -- multi-part and nested dicts
# ---------------------------------------------------------------------------

def test_multi_part_message_roundtrip(store: ConversationStore) -> None:
    parts = [
        {"text": "part1"},
        {"text": "part2"},
        {"inline_data": {"mime_type": "image/jpeg"}},
    ]
    store.append("tg:multi", {"role": "user", "parts": parts})
    assert store.get_history("tg:multi")[0]["parts"] == parts


def test_nested_dict_parts_roundtrip(store: ConversationStore) -> None:
    parts = [{"text": "hello", "metadata": {"lang": "fa", "score": 0.9}}]
    store.append("tg:nested", {"role": "model", "parts": parts})
    assert store.get_history("tg:nested")[0]["parts"] == parts


# ---------------------------------------------------------------------------
# 9. Persistence across instances (restart simulation)
# ---------------------------------------------------------------------------

def test_data_persists_across_store_instances(tmp_path: Path) -> None:
    db = str(tmp_path / "persist.sqlite")
    s1 = ConversationStore(db_path=db)
    s1.append("tg:persist", {"role": "user", "parts": [{"text": "survive"}]})
    del s1
    s2 = ConversationStore(db_path=db)
    history = s2.get_history("tg:persist")
    assert len(history) == 1
    assert history[0]["parts"][0]["text"] == "survive"


# ---------------------------------------------------------------------------
# Architecture gap note (informational, not a failing test)
# ---------------------------------------------------------------------------
#
#  ConversationStorePort (ports/conversation_store.py) -- async hexagonal port
#    async def append_message(self, thread_id, role, content) -> str
#    async def list_messages(self, thread_id) -> Sequence[dict[str, str]]
#
#  ConversationStore (features/conversation_store.py) -- live in production
#    SYNC def append(self, conv_id, message: dict) -> None
#    SYNC def get_history(self, conv_id, *, limit=20) -> list[dict]
#    SYNC def append_batch(self, conv_id, messages) -> None
#    SYNC def clear(self, conv_id) -> None
#    SYNC def active_conversations(self) -> int
#    SYNC def trim_to_limit(self, conv_id, limit=20) -> None
#
#  These two interfaces are NOT compatible. No bridge adapter exists in main
#  at SHA df3c9d16.  PR#33 (arena/01a0c3aa-nexus-ai-agent) has
#  adapters/conversation_store_sqlite.py that implements the port but is not
#  yet merged.  test_port_signatures.py enforces the port surface already.
# ---------------------------------------------------------------------------
