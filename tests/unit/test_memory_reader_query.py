"""task-211 — _memory_reader must query the USER's words, not the assistant's.

THE DEFECT, REPRODUCED BEFORE FIXED
-----------------------------------
``graph.py::_memory_reader`` built its query from
``state['messages'][-1]['content']`` with no role filter, while its sibling
``_memory_writer`` walks backwards for the last **user** message::

    # writer (correct)
    last_user = next((m["content"] for m in reversed(messages)
                      if m.get("role") == "user"), "")
    # reader (wrong)  <-- whatever happens to be last
    last = state["messages"][-1]["content"]

Driving the real compiled graph with a real checkpointer, on 2026-09-29, gave:

  case A  on_message's own flow (a fresh one-message state)
         -> queried the user's words.  OK
  case B  the caller resumes by replaying the previous RESULT state
         -> queried ``'ASSISTANT-ANSWER'``.  DEFECT
  case C  a state whose last message is the assistant's
         -> queried ``'Noted, Sara.'``.  DEFECT
  case D  no messages at all
         -> queried ``''``.  No crash, no recall.

Case A is the only one the production entry point currently exercises, which is
why this was latent rather than loud.  But case B is the *graph's own output
state* — the most natural thing for any caller to hand back — and the reader is
a public helper whose contract is "read memory for this turn", not "echo the
last array element".

WHY IT MATTERS FOR RECALL
-------------------------
The store is queried with text that the user never typed.  Embedding the
assistant's own prose retrieves turns whose *answer* resembles it, not turns the
user *said* it.  A user asking "what did I tell you about my daughter?" gets a
query vector built from the model's previous reply, so the right memory is not
merely missed - it is missed in a way that looks like a weak index.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory
from nexus_ai_agent.orchestration.graph import compile_graph
from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer
from nexus_ai_agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.anyio

FACT = "Sara"
BORN = "2019"
TURN1 = f"My daughter is called {FACT} and she was born in {BORN}"


class SpyMemory(LongTermMemory):
    """A real sqlite store that records the exact query it was handed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.reads: list[str] = []

    async def search(  # type: ignore[override]
        self, thread_id: str, query: str, top_k: int = 3
    ) -> list[str]:
        self.reads.append(query)
        return await super().search(thread_id, query, top_k=top_k)


class EchoLLM(FakeLLMProvider):
    """A real provider whose replies are unmistakably not the user's words."""

    async def generate(self, prompt: str, system: str = "") -> str:  # type: ignore[override]
        return "ASSISTANT-ANSWER"


def _state(text: str | None, thread: str = "tg:-500") -> dict[str, Any]:
    return {
        "thread_id": thread,
        "chat_id": -500,
        "user_id": 7,
        "correlation_id": "task-211",
        "messages": [] if text is None else [{"role": "user", "content": text}],
        "intent": "unknown",
        "active_persona": "",
        "current_task": None,
        "tool_results": [],
        "memory_context": "",
        "response": "",
        "error": None,
        "turn_count": 0,
        "moderation_passed": True,
    }


@pytest.fixture
def rig():
    llm = EchoLLM()
    memory = SpyMemory(":memory:", llm)
    graph = compile_graph(
        llm,
        get_checkpointer(":memory:"),
        memory,
        ToolRegistry(enable_shell=False, workspace_root="."),
    )
    return {
        "graph": graph,
        "memory": memory,
        "cfg": {"configurable": {"thread_id": "tg:-500"}},
    }


# ── the reproductions, now as guards ──────────────────────────────────────── #
async def test_a_resumed_conversation_queries_the_user_not_the_assistant(rig) -> None:
    """CASE B — the caller replays the previous result state.

    This is the graph's own output handed back, which is what a resumed
    conversation looks like. Before the fix the store was queried with
    ``'ASSISTANT-ANSWER'`` and the user's question was never searched for.
    """
    first = await rig["graph"].ainvoke(_state(TURN1), config=rig["cfg"])
    assert first["messages"][-1]["role"] == "assistant", (
        "the writer no longer appends the assistant turn; this reproduction is stale"
    )

    rig["memory"].reads.clear()
    await rig["graph"].ainvoke(first, config=rig["cfg"])

    queried = rig["memory"].reads[-1]
    assert "ASSISTANT" not in queried, (
        f"the memory store was queried with the assistant's own reply: {queried!r}"
    )


async def test_a_state_ending_on_the_assistant_queries_the_last_user_message(
    rig,
) -> None:
    """CASE C — the assistant's text sits at [-1] with no trailing user message."""
    state = _state(None)
    state["messages"] = [
        {"role": "user", "content": TURN1},
        {"role": "assistant", "content": "Noted, Sara."},
    ]
    await rig["graph"].ainvoke(state, config=rig["cfg"])

    queried = rig["memory"].reads[-1]
    assert "Noted" not in queried, (
        f"the memory store was queried with the assistant's reply: {queried!r}"
    )
    assert FACT in queried and BORN in queried, (
        f"expected the last USER message to be the query, got {queried!r}"
    )


async def test_the_standard_on_message_flow_still_queries_the_user(rig) -> None:
    """CASE A must keep working — the fix must not regress the normal path."""
    await rig["graph"].ainvoke(_state(TURN1), config=rig["cfg"])
    assert rig["memory"].reads[-1].strip() == TURN1


async def test_a_multi_turn_recall_actually_finds_the_stored_fact(rig) -> None:
    """The point of the fix: the right memory comes back, not merely a clean query.

    A query shaped like the user's own words is the precondition for recall; this
    asserts the consequence, so the guard cannot be satisfied by a cosmetic edit.
    """
    await rig["memory"].store(
        "tg:-500", f"User: my daughter {FACT} was born in {BORN}\nAssistant: noted"
    )
    first = await rig["graph"].ainvoke(_state(TURN1), config=rig["cfg"])
    rig["memory"].reads.clear()

    await rig["graph"].ainvoke(first, config=rig["cfg"])

    assert rig["memory"].reads[-1] == TURN1
    hits = await rig["memory"].search("tg:-500", TURN1, top_k=3)
    assert any(FACT in h for h in hits), f"the stored fact was not retrievable: {hits!r}"


async def test_no_messages_still_queries_empty_and_does_not_raise(rig) -> None:
    """CASE D — the degenerate case must stay a no-op, not a crash."""
    await rig["graph"].ainvoke(_state(None), config=rig["cfg"])
    assert rig["memory"].reads == [""]


async def test_the_query_ignores_a_trailing_tool_or_system_message(rig) -> None:
    """Only ``role == "user"`` counts; a trailing system turn is not the question."""
    state = _state(None)
    state["messages"] = [
        {"role": "system", "content": "SESSION RESET"},
        {"role": "user", "content": TURN1},
        {"role": "assistant", "content": "Noted."},
        {"role": "system", "content": "COMPACTED"},
    ]
    await rig["graph"].ainvoke(state, config=rig["cfg"])

    queried = rig["memory"].reads[-1]
    assert "COMPACTED" not in queried and "SESSION RESET" not in queried
    assert FACT in queried


async def test_a_history_with_several_user_turns_queries_the_MOST_RECENT_one(
    rig,
) -> None:
    """The mutant that separates "last" from "first".

    ``scripts/memory_reader_query_mutations.py::M2`` rewrites the fix to take the
    FIRST user message instead of the last.  Every other test in this file uses a
    state holding exactly one user message, so M2 passed the whole suite: with
    one user turn, first and last are the same string and the bug is invisible.

    This is the guard that makes the direction of the fix load-bearing.  A
    reviewer who "fixed" task-211 by reading the first user message would ship a
    query that gets staler every turn, and on a long conversation it degrades
    into a fixed opening question.
    """
    state = _state(None)
    state["messages"] = [
        {"role": "user", "content": "My daughter is called Sara and she was born in 2019"},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "Actually I meant my son Noah, born 2021"},
        {"role": "assistant", "content": "Updated."},
        {"role": "user", "content": "What are both my children's names?"},
    ]
    await rig["graph"].ainvoke(state, config=rig["cfg"])

    queried = rig["memory"].reads[-1]
    # The last user turn is the question being asked, NOT the turn that happens
    # to mention a fact.  M2 would answer with the opening turn instead.
    assert queried.strip() == "What are both my children's names?", (
        f"the query was not the most recent user turn: {queried!r}"
    )
    assert "Sara" not in queried, (
        f"the query used a STALE earlier user turn instead of the latest: {queried!r}"
    )
