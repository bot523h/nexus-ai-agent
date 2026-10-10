"""P1-A — personal long-term memory is scoped per user, not per group chat.

Regression for the overnight audit finding: ``thread_id = tg:{chat_id}`` keyed
personal memory for every member of a group, so user B's memory reader returned
user A's stored turns.  The contract asserted here:

* a real graph round-trip (write → read) with two identities in one chat is
  isolated: B never retrieves A's turn, A still retrieves their own;
* the personal scope is deterministic and stable across requests;
* missing identity (0/None) fails closed — no personal read, no personal write,
  no fallback to a shared scope;
* legacy rows stored under the old shared key are never served to a personal
  read (no silent migration, no deletion, no copy-to-all);
* conversation/checkpoint identity (``thread_id``) is untouched: the group chat
  history and checkpointer semantics keep working.

All tests run through the real graph nodes (``compile_graph`` with a fake LLM)
or the real storage; nothing touches the network.
"""

from __future__ import annotations

import uuid

from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory, memory_scope_id
from nexus_ai_agent.orchestration.graph import compile_graph
from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
from nexus_ai_agent.tools.registry import ToolRegistry

SECRET = "My secret code is ALPHA-42"


def _build_graph():
    llm = FakeLLMProvider()
    checkpointer = get_checkpointer(":memory:")
    long_term = LongTermMemory(":memory:", llm)
    registry = ToolRegistry(enable_shell=False, workspace_root=".")
    graph = compile_graph(llm, checkpointer, long_term, registry)
    return graph, long_term


def _state(user_id: int | None, chat_id: int, text: str) -> dict:
    return {
        "thread_id": f"tg:{chat_id}",
        "chat_id": chat_id,
        "user_id": user_id,
        "correlation_id": f"corr-{uuid.uuid4().hex[:8]}",
        "messages": [{"role": "user", "content": text}],
        "intent": "chat",
        "active_persona": "gemma",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }


# ── the typed scope helper ────────────────────────────────────────────


def test_memory_scope_is_deterministic_and_personal() -> None:
    a1 = memory_scope_id(111)
    a2 = memory_scope_id(111)
    b = memory_scope_id(222)
    assert a1 == a2, "the scope for one user must be stable across requests"
    assert a1 != b, "two users must never share a personal scope"
    assert a1 not in (None, "", "tg:0"), f"scope must not be a shared fallback: {a1!r}"


def test_memory_scope_fails_closed_on_missing_identity() -> None:
    assert memory_scope_id(None) is None
    assert memory_scope_id(0) is None


# ── behavioural isolation through the real graph ──────────────────────


async def test_group_member_cannot_read_another_members_memory(settings_override) -> None:
    graph, _long_term = _build_graph()
    chat = -100123

    state_a = _state(111, chat, SECRET)
    result_a = await graph.ainvoke(
        state_a, config={"configurable": {"thread_id": state_a["thread_id"]}}
    )
    assert result_a.get("response"), "user A turn produced no response"

    state_b = _state(222, chat, "What do you remember?")
    result_b = await graph.ainvoke(
        state_b, config={"configurable": {"thread_id": state_b["thread_id"]}}
    )
    ctx_b = result_b.get("memory_context") or ""
    assert "ALPHA-42" not in ctx_b, (
        "cross-user memory leak in group chat: user 222's memory_context "
        f"contains user 111's stored turn -> {ctx_b!r}"
    )


async def test_user_still_reads_their_own_memory_later(settings_override) -> None:
    graph, _long_term = _build_graph()
    chat = -100123

    state_a = _state(111, chat, SECRET)
    await graph.ainvoke(state_a, config={"configurable": {"thread_id": f"tg:{chat}"}})

    # Same user, a later request (same chat): their own memory must be there.
    later = _state(111, chat, "What did I tell you?")
    result = await graph.ainvoke(later, config={"configurable": {"thread_id": later["thread_id"]}})
    ctx = result.get("memory_context") or ""
    assert "ALPHA-42" in ctx, f"user 111 lost access to their own personal memory: context={ctx!r}"


async def test_personal_write_round_trip_is_per_user(settings_override) -> None:
    """Storage-level round-trip with two identities under one conversation key."""
    llm = FakeLLMProvider()
    ltm = LongTermMemory(":memory:", llm)
    scope_a = memory_scope_id(111)
    scope_b = memory_scope_id(222)
    assert scope_a is not None and scope_b is not None
    await ltm.store(scope_a, f"User: 111\nAssistant: {SECRET}")
    hits_a = await ltm.search(scope_a, "secret code", top_k=3)
    hits_b = await ltm.search(scope_b, "secret code", top_k=3)
    assert any("ALPHA-42" in h for h in hits_a), "owner must read their own row"
    assert hits_b == [], f"scope B must see nothing of A: {hits_b!r}"


# ── fail closed on missing identity ───────────────────────────────────


async def test_missing_identity_fails_closed(settings_override) -> None:
    graph, long_term = _build_graph()
    chat = -100123

    for anon in (0, None):
        state = _state(anon, chat, SECRET)
        result = await graph.ainvoke(state, config={"configurable": {"thread_id": f"tg:{chat}"}})
        assert result.get("response"), "the chat reply itself may continue"
        # No personal read: the memory context must stay empty.
        assert not (result.get("memory_context") or ""), (
            f"user_id={anon!r} must not read any personal memory"
        )

    # No personal write either: nothing addressable by a shared/zero scope.
    for bad_scope in ("tg:0", "tg:-100123", "mem:u:0"):
        hits = await long_term.search(bad_scope, "ALPHA-42", top_k=5)
        assert hits == [], (
            f"user_id=0/None leaked a personal write into shared scope {bad_scope!r}: {hits!r}"
        )


# ── legacy data: preserved, never served, never copied ────────────────


async def test_legacy_shared_rows_are_never_served_to_personal_reads(
    settings_override,
) -> None:
    graph, long_term = _build_graph()
    chat = -100777

    # Simulate a pre-migration row written under the old shared key.
    await long_term.store(f"tg:{chat}", f"User: 111\nAssistant: {SECRET}")

    state_b = _state(222, chat, "What do you remember?")
    result_b = await graph.ainvoke(state_b, config={"configurable": {"thread_id": f"tg:{chat}"}})
    assert "ALPHA-42" not in (result_b.get("memory_context") or ""), (
        "legacy shared-key row must never surface in a personal read"
    )

    state_a = _state(111, chat, "What do you remember?")
    result_a = await graph.ainvoke(state_a, config={"configurable": {"thread_id": f"tg:{chat}"}})
    assert "ALPHA-42" not in (result_a.get("memory_context") or ""), (
        "legacy shared-key row must not be silently migrated into a personal scope"
    )
    # The legacy row itself is still stored untouched (no deletion).
    preserved = await long_term.search(f"tg:{chat}", "ALPHA-42", top_k=5)
    assert any("ALPHA-42" in h for h in preserved), (
        "the legacy row must remain stored (non-destructive migration policy)"
    )


# ── conversation identity stays shared in groups (no regression) ──────


async def test_group_conversation_identity_is_unchanged(settings_override) -> None:
    graph, _long_term = _build_graph()
    chat = -100555

    first = _state(111, chat, "hello there")
    result1 = await graph.ainvoke(first, config={"configurable": {"thread_id": f"tg:{chat}"}})
    assert result1["turn_count"] == 1

    # A different member of the SAME group resumes the SAME conversation
    # thread (checkpointer identity is the chat, not the user).
    result2 = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "hello back"}]},
        config={"configurable": {"thread_id": f"tg:{chat}"}},
    )
    assert result2["turn_count"] >= 2, (
        "group conversation/checkpoint identity must keep working: "
        f"turn_count={result2.get('turn_count')}"
    )
