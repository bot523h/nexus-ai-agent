from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory
from nexus_ai_agent.orchestration.router import classify_intent
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.tools.registry import ToolRegistry


async def _router_node(state: NexusState) -> NexusState:
    last = state["messages"][-1]["content"] if state.get("messages") else ""
    state["intent"] = classify_intent(last)
    return state


async def _memory_reader(long_term_memory: LongTermMemory, state: NexusState) -> NexusState:
    last = state["messages"][-1]["content"] if state.get("messages") else ""
    try:
        results = await long_term_memory.search(state["thread_id"], last, top_k=3)
        state["memory_context"] = await long_term_memory.format_context(results)
    except Exception:
        state.setdefault("memory_context", "")
    return state


async def _chat_agent(llm: LLMProvider, state: NexusState) -> NexusState:
    messages = state.get("messages", [])[-10:]
    memory_context = state.get("memory_context", "")
    prompt = "\n".join([f"{m['role']}: {m['content']}" for m in messages])
    system = f"You are NEXUS, a helpful AI assistant.\nContext from memory: {memory_context}"
    state["response"] = await llm.generate(prompt=prompt, system=system)
    return state


async def _planner_agent(llm: LLMProvider, state: NexusState) -> NexusState:
    # Deterministic MVP plan so FakeLLM works in tests.
    user_msg = state.get("messages", [])[-1]["content"] if state.get("messages") else ""
    plan = {
        "goal": user_msg,
        "steps": [
            {
                "id": 1,
                "action": user_msg or "No-op",
                "tool": None,
                "status": "pending",
            }
        ],
    }
    state["current_task"] = plan
    state["response"] = f"Plan created with {len(plan['steps'])} step(s): {plan['steps'][0]['action']}"
    return state


async def _executor_agent(state: NexusState) -> NexusState:
    task = state.get("current_task") or {}
    steps = task.get("steps", [])
    first_pending = next((s for s in steps if s.get("status") == "pending"), None)
    if not first_pending:
        state["tool_results"] = state.get("tool_results", [])
        return state

    # Tools are wired in later; mark as done for MVP.
    first_pending["status"] = "done"
    state["tool_results"] = state.get("tool_results", []) + [
        {"step_id": first_pending.get("id"), "success": True, "output": "noop"}
    ]
    state["response"] = state.get("response") or "Task executed."
    state["current_task"] = task
    return state


async def _memory_writer(state: NexusState) -> NexusState:
    # Persist short turn-level state updates.
    response = state.get("response", "")
    if response:
        state["messages"] = state.get("messages", []) + [{"role": "assistant", "content": response}]
    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    return state


def compile_graph(
    llm: LLMProvider,
    checkpointer: Any,
    long_term_memory: LongTermMemory,
    tool_registry: ToolRegistry,
):
    _ = tool_registry  # tool wiring is used by executor/planner in later phases
    graph: StateGraph[NexusState] = StateGraph(NexusState)

    async def chat_node(state: NexusState) -> NexusState:
        return await _chat_agent(llm, state)

    async def planner_node(state: NexusState) -> NexusState:
        return await _planner_agent(llm, state)

    async def memory_reader_node(state: NexusState) -> NexusState:
        return await _memory_reader(long_term_memory, state)

    graph.add_node("router", _router_node)
    graph.add_node("memory_reader", memory_reader_node)
    graph.add_node("chat_agent", chat_node)
    graph.add_node("planner_agent", planner_node)
    graph.add_node("executor_agent", _executor_agent)
    graph.add_node("memory_writer", _memory_writer)

    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        lambda s: s["intent"],
        {
            "chat": "chat_agent",
            "task": "memory_reader",
            "memory": "memory_reader",
        },
    )
    graph.add_conditional_edges(
        "memory_reader",
        lambda s: s["intent"],
        {
            "task": "planner_agent",
            "memory": "chat_agent",
            "chat": "chat_agent",
        },
    )

    graph.add_edge("chat_agent", "memory_writer")
    graph.add_edge("planner_agent", "executor_agent")
    graph.add_edge("executor_agent", "memory_writer")
    graph.add_edge("memory_writer", END)

    return graph.compile(checkpointer=checkpointer)
