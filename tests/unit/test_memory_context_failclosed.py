"""PHASE 1 + 2.2 — ``memory_context`` fails closed and never crosses audiences.

Regression suite for two interlocking defects (PR #198 final-mission audit,
CodeRabbit comment 4236380409 and its deeper design gap):

1. ``_memory_reader``'s failure path used ``state.setdefault("memory_context",
   "")`` — a checkpoint-hydrated value survived the failure and rode into the
   next persona prompt (stale cross-request context).
2. The chat-intent path never re-reads memory at all: it used whatever
   ``memory_context`` the caller/checkpoint supplied (injection / replay).
3. Personal memory was prompt-visible in group audiences (every group member
   is a reader of any prompt text the model sees).

Contract under test (prompt level — what the provider actually receives):

- every failure branch of the memory read (missing/invalid identity, empty
  retrieval, store failure) leaves ``memory_context`` empty — never stale;
- a fresh turn always re-derives ``memory_context`` (router clears it, the
  reader overwrites it); nothing a caller or a checkpoint supplies survives;
- personal memory is disclosed only to a provable private audience
  (``chat_id == user_id``); the most restrictive safe default applies to
  groups, anonymous turns and legacy shared-key rows.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory, memory_scope_id
from nexus_ai_agent.orchestration.graph import _memory_reader, compile_graph
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
from nexus_ai_agent.tools.registry import ToolRegistry

# Sentinel phrases: none of these may ever appear in a prompt unless the test
# explicitly asserts their presence.
A_SECRET = "ALPHA-SECRET-42"
B_SECRET = "BETA-SECRET-9"
STALE = "STALE-CONTEXT-LEAK"
INJECTED = "INJECTED-CONTEXT-LEAK"
LEGACY = "LEGACY-THREAD-SECRET"

USER_A = 4242
USER_B = 9876
GROUP_CHAT = -100777


class PromptSpy(FakeLLMProvider):
    """FakeLLM that records every (prompt, system) pair the provider sees."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    async def generate(self, prompt: str, system: str = "", **kwargs: Any) -> str:
        self.calls.append((prompt, system))
        return await super().generate(prompt, system, **kwargs)

    def seen_text(self) -> str:
        return "\n".join(p + "\n" + s for p, s in self.calls)


def _state(
    *,
    thread_id: str,
    user_id: int | None,
    chat_id: int | None,
    content: str,
    memory_context: str | None,
    intent: str = "memory",
) -> dict[str, Any]:
    st: dict[str, Any] = {
        "thread_id": thread_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "correlation_id": "cid-failclosed",
        "messages": [{"role": "user", "content": content}],
        "intent": intent,
        "active_persona": "gemma",
        "current_task": None,
        "tool_results": [],
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }
    if memory_context is not None:
        # ``None`` omits the key entirely — the checkpointer's value (if any)
        # hydrates the turn, exactly like a resume/replay in production.
        st["memory_context"] = memory_context
    return st


async def _turn(
    graph: Any,
    *,
    thread_id: str,
    user_id: int | None,
    chat_id: int | None,
    content: str = "what do you remember?",
    memory_context: str | None = None,
    intent: str = "memory",
) -> NexusState:
    st = _state(
        thread_id=thread_id,
        user_id=user_id,
        chat_id=chat_id,
        content=content,
        memory_context=memory_context,
        intent=intent,
    )
    with (
        patch("nexus_ai_agent.orchestration.graph.classify_intent", lambda _t: intent),
        patch("nexus_ai_agent.orchestration.graph.select_persona", lambda _t: "gemma"),
    ):
        return cast(
            NexusState,
            await graph.ainvoke(
                cast(NexusState, st), config={"configurable": {"thread_id": thread_id}}
            ),
        )


def _build(llm: PromptSpy) -> tuple[Any, LongTermMemory]:
    mem = LongTermMemory(":memory:", llm)
    registry = ToolRegistry(enable_shell=False, workspace_root=".")
    graph = compile_graph(llm, get_checkpointer(":memory:"), mem, registry)
    return graph, mem


# ---------------------------------------------------------------------------
# PHASE 1 — every failure branch clears; nothing stale survives.
# ---------------------------------------------------------------------------


async def test_reader_node_direct_failure_branches_fail_closed() -> None:
    """The reader node itself (no router in front) must never keep a stale value.

    Direct unit seam over ``_memory_reader``: covers the exact CodeRabbit
    4236380409 line — restoring ``state.setdefault(...)`` in the except path
    (or any early-return that preserves the incoming value) must fail here.
    """
    llm = PromptSpy()
    _graph, mem = _build(llm)

    async def _boom(*_a: Any, **_k: Any) -> list[str]:
        raise RuntimeError("memory store down")

    mem.search = _boom  # type: ignore[method-assign]

    for bad_user in (0, None):
        st = _state(
            thread_id="t",
            user_id=cast(int | None, bad_user),
            chat_id=USER_A,
            content="q",
            memory_context=STALE,
        )
        out = await _memory_reader(mem, cast(NexusState, st))
        assert out["memory_context"] == "", (
            f"reader kept stale context for user_id={bad_user!r}: {out['memory_context']!r}"
        )

    st = _state(thread_id="t", user_id=USER_A, chat_id=USER_A, content="q", memory_context=STALE)
    out = await _memory_reader(mem, cast(NexusState, st))
    assert out["memory_context"] == "", (
        f"reader failure branch kept stale context: {out['memory_context']!r}"
    )

    # Audience gate at the reader itself: a group chat is never authorized,
    # even with a valid identity and a live store.
    llm2 = PromptSpy()
    _graph2, mem2 = _build(llm2)
    await mem2.store(cast(str, memory_scope_id(USER_A)), A_SECRET)
    st = _state(
        thread_id="t", user_id=USER_A, chat_id=GROUP_CHAT, content="q", memory_context=STALE
    )
    out = await _memory_reader(mem2, cast(NexusState, st))
    assert out["memory_context"] == "", (
        f"reader disclosed memory to a group audience: {out['memory_context']!r}"
    )


@pytest.mark.asyncio
async def test_read_failure_clears_stale_context() -> None:
    """Store/search failure ⇒ empty context (not the checkpoint's stale value)."""
    llm = PromptSpy()
    graph, mem = _build(llm)

    async def _boom(*_a: Any, **_k: Any) -> list[str]:
        raise RuntimeError("memory store down")

    mem.search = _boom  # type: ignore[method-assign]
    result = await _turn(
        graph,
        thread_id="tg:4242",
        user_id=USER_A,
        chat_id=USER_A,
        memory_context=STALE,
    )
    assert result["memory_context"] == "", (
        f"retrieval failure must fail closed to empty context; kept {result['memory_context']!r}"
    )
    assert STALE not in llm.seen_text(), "stale context leaked into the provider prompt"


@pytest.mark.asyncio
async def test_invalid_identity_clears_stale_context() -> None:
    """Missing/invalid identity ⇒ empty context; anonymous sees no prior user's text."""
    for bad_user in (0, None):
        llm = PromptSpy()
        graph, _mem = _build(llm)
        result = await _turn(
            graph,
            thread_id=f"tg:anon-{bad_user}",
            user_id=cast(int | None, bad_user),
            chat_id=USER_A,
            memory_context=STALE,
        )
        assert result["memory_context"] == "", (
            f"user_id={bad_user!r} must fail closed; got {result['memory_context']!r}"
        )
        assert STALE not in llm.seen_text(), f"anonymous user_id={bad_user!r} saw stale context"


@pytest.mark.asyncio
async def test_empty_retrieval_clears_stale_context() -> None:
    """Valid identity but zero rows ⇒ empty context, not the previous value."""
    llm = PromptSpy()
    graph, _mem = _build(llm)
    result = await _turn(
        graph,
        thread_id="tg:4242",
        user_id=USER_A,
        chat_id=USER_A,
        memory_context=STALE,
    )
    assert result["memory_context"] == ""
    assert STALE not in llm.seen_text()


@pytest.mark.asyncio
async def test_chat_intent_cannot_inject_context() -> None:
    """A chat turn never renders caller/checkpoint-supplied memory_context."""
    llm = PromptSpy()
    graph, _mem = _build(llm)
    result = await _turn(
        graph,
        thread_id="tg:4242",
        user_id=USER_A,
        chat_id=USER_A,
        content="hello there",
        memory_context=INJECTED,
        intent="chat",
    )
    assert result["memory_context"] == "", (
        f"chat intent must re-derive (here: clear) memory_context, got {result['memory_context']!r}"
    )
    assert INJECTED not in llm.seen_text(), "caller-supplied context reached the provider prompt"


@pytest.mark.asyncio
async def test_task_intent_failure_branches_fail_closed() -> None:
    """The task flow's reader failure leaves the graph output with empty context."""
    llm = PromptSpy()
    graph, mem = _build(llm)

    async def _boom(*_a: Any, **_k: Any) -> list[str]:
        raise RuntimeError("memory store down")

    mem.search = _boom  # type: ignore[method-assign]
    result = await _turn(
        graph,
        thread_id="tg:4242",
        user_id=USER_A,
        chat_id=USER_A,
        content="do the thing",
        memory_context=STALE,
        intent="task",
    )
    assert result["memory_context"] == ""
    assert STALE not in llm.seen_text()


# ---------------------------------------------------------------------------
# PHASE 2.2 — audience matrix: A↛B, B↛A, A→A, anonymous ⊘, group non-disclosure,
# shared thread ≠ shared memory, no stale context, legacy rows never served.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_audience_disclosure_matrix() -> None:
    llm = PromptSpy()
    graph, mem = _build(llm)
    await mem.store(cast(str, memory_scope_id(USER_A)), A_SECRET)
    await mem.store(cast(str, memory_scope_id(USER_B)), B_SECRET)
    await mem.store("tg:-100777", LEGACY)  # legacy shared-key row: kept, never served

    # A→A works (private audience, own scope).
    r = await _turn(graph, thread_id="tg:4242", user_id=USER_A, chat_id=USER_A)
    assert A_SECRET in llm.seen_text(), "A must read A's memory in A's private chat"
    assert B_SECRET not in llm.seen_text()
    assert LEGACY not in llm.seen_text()

    # A↛B: A's private turn must not surface B's memory (and vice versa).
    llm.calls.clear()
    r = await _turn(graph, thread_id="tg:9876", user_id=USER_B, chat_id=USER_B)
    assert B_SECRET in llm.seen_text(), "B must read B's memory in B's private chat"
    assert A_SECRET not in llm.seen_text(), "B must not see A's memory"
    assert r["memory_context"] != "" and A_SECRET not in r["memory_context"]

    llm.calls.clear()
    await _turn(graph, thread_id="tg:4242-b", user_id=USER_A, chat_id=USER_A)
    assert A_SECRET in llm.seen_text()
    assert B_SECRET not in llm.seen_text(), "A must not see B's memory"

    # Group non-disclosure: personal memory is NOT prompt-visible in a group —
    # even to its owner.  Most restrictive safe default (documented policy).
    llm.calls.clear()
    await _turn(graph, thread_id="tg:g", user_id=USER_A, chat_id=GROUP_CHAT)
    assert A_SECRET not in llm.seen_text(), "owner's memory leaked into group-visible prompt"
    assert B_SECRET not in llm.seen_text()
    assert LEGACY not in llm.seen_text(), "legacy shared-key row served to a group"

    llm.calls.clear()
    await _turn(graph, thread_id="tg:g", user_id=USER_B, chat_id=GROUP_CHAT)
    assert B_SECRET not in llm.seen_text(), "owner's memory leaked into group-visible prompt"

    # Anonymous reads nothing.
    llm.calls.clear()
    await _turn(graph, thread_id="tg:g", user_id=None, chat_id=GROUP_CHAT)
    assert A_SECRET not in llm.seen_text() and B_SECRET not in llm.seen_text()
    llm.calls.clear()
    await _turn(graph, thread_id="tg:anon", user_id=0, chat_id=GROUP_CHAT)
    assert A_SECRET not in llm.seen_text() and B_SECRET not in llm.seen_text()


@pytest.mark.asyncio
async def test_shared_thread_is_not_shared_memory() -> None:
    """Two users replaying one thread_id still get disjoint personal contexts."""
    llm = PromptSpy()
    graph, mem = _build(llm)
    await mem.store(cast(str, memory_scope_id(USER_A)), A_SECRET)
    await mem.store(cast(str, memory_scope_id(USER_B)), B_SECRET)

    # Same thread id, two private chats (A first, then B) — checkpoint-shared,
    # memory-disjoint.
    await _turn(graph, thread_id="shared-room", user_id=USER_A, chat_id=USER_A)
    assert A_SECRET in llm.seen_text()
    llm.calls.clear()
    await _turn(graph, thread_id="shared-room", user_id=USER_B, chat_id=USER_B)
    assert B_SECRET in llm.seen_text()
    assert A_SECRET not in llm.seen_text(), "A's checkpointed context leaked into B's turn"


@pytest.mark.asyncio
async def test_checkpoint_context_never_crosses_users_or_audiences() -> None:
    """Turn-2+ must not replay turn-1's memory_context to a different user/audience.

    This is the exact CodeRabbit 4236380409 scenario: the checkpointer hydrates
    turn 1's populated ``memory_context`` into turn 2's state.
    """
    llm = PromptSpy()
    graph, mem = _build(llm)
    await mem.store(cast(str, memory_scope_id(USER_A)), A_SECRET)
    await mem.store(cast(str, memory_scope_id(USER_B)), B_SECRET)

    # Turn 1 — A, private, populated context (A→A works).
    await _turn(graph, thread_id="tg:leak", user_id=USER_A, chat_id=USER_A)
    assert A_SECRET in llm.seen_text()

    # Turn 2 — B in a group, same thread: neither the checkpointed ALPHA (A's)
    # nor B's own memory may appear (group non-disclosure).
    llm.calls.clear()
    await _turn(graph, thread_id="tg:leak", user_id=USER_B, chat_id=GROUP_CHAT)
    assert A_SECRET not in llm.seen_text(), "checkpointed A-context leaked to B (group)"
    assert B_SECRET not in llm.seen_text(), "B's memory disclosed in a group audience"

    # Turn 3 — anonymous in the same thread: sees nothing.
    llm.calls.clear()
    await _turn(graph, thread_id="tg:leak", user_id=0, chat_id=GROUP_CHAT)
    assert A_SECRET not in llm.seen_text(), "anonymous turn replayed A's context"

    # Turn 4 — A in the group: even the owner gets no disclosure in a group.
    llm.calls.clear()
    await _turn(graph, thread_id="tg:leak", user_id=USER_A, chat_id=GROUP_CHAT)
    assert A_SECRET not in llm.seen_text(), "owner's context disclosed to the group audience"

    # Turn 5 — back to A private (no caller-supplied context): re-derived live.
    llm.calls.clear()
    result = await _turn(graph, thread_id="tg:leak", user_id=USER_A, chat_id=USER_A)
    assert A_SECRET in llm.seen_text(), "A→A must keep working after blocked turns"
    assert B_SECRET not in result["memory_context"]
