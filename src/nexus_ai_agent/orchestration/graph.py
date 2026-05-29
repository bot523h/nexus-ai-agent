from __future__ import annotations

from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph

from nexus_ai_agent.agents.gemma_agent import GemmaAgent
from nexus_ai_agent.agents.phi_agent import PhiAgent
from nexus_ai_agent.agents.qwen_agent import QwenAgent
from nexus_ai_agent.llm.provider import LLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory
from nexus_ai_agent.orchestration.router import classify_intent, select_persona
from nexus_ai_agent.orchestration.state import NexusState
from nexus_ai_agent.tools.registry import ToolRegistry


async def _router_node(state: NexusState) -> NexusState:
    last_user = state["messages"][-1]["content"] if state.get("messages") else ""
    intent = cast(Literal["chat", "task", "memory", "unknown"], classify_intent(last_user))
    persona = select_persona(last_user)
    return {
        **state,
        "intent": intent,
        "active_persona": persona,
        "turn_count": int(state.get("turn_count", 0)) + 1,
    }


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
    action = user_msg or "No-op"
    steps: list[dict[str, Any]] = [
        {
            "id": 1,
            "action": action,
            "tool": None,
            "status": "pending",
        }
    ]
    plan: dict[str, Any] = {
        "goal": user_msg,
        "steps": steps,
    }
    state["current_task"] = plan
    state["response"] = f"Plan created with {len(steps)} step(s): {action}"
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
    return state


def compile_graph(
    llm: LLMProvider,
    checkpointer: Any,
    long_term_memory: LongTermMemory,
    tool_registry: ToolRegistry,
) -> Any:
    _ = tool_registry  # tool wiring is used by executor/planner in later phases

    # Persona cores
    phi = PhiAgent(llm)
    qwen = QwenAgent(llm)
    gemma = GemmaAgent(llm)

    graph: StateGraph[NexusState] = StateGraph(NexusState)

    async def planner_node(state: NexusState) -> NexusState:
        return await _planner_agent(llm, state)

    async def memory_reader_task_node(state: NexusState) -> NexusState:
        return await _memory_reader(long_term_memory, state)

    async def memory_reader_chat_node(state: NexusState) -> NexusState:
        return await _memory_reader(long_term_memory, state)

    async def phi_node(state: NexusState) -> NexusState:
        return await phi.run(state)

    async def qwen_node(state: NexusState) -> NexusState:
        return await qwen.run(state)

    async def gemma_node(state: NexusState) -> NexusState:
        return await gemma.run(state)

    async def moderation_node(state: NexusState) -> NexusState:
        resp = state.get("response", "")
        if not resp:
            return {**state, "moderation_passed": True}
        result = await phi.moderate(resp)
        if not result.get("safe", True):
            return {**state, "response": "I cannot respond to that.", "moderation_passed": False}
        return {**state, "moderation_passed": True}

    def route_intent(state: NexusState) -> str:
        intent = state.get("intent", "chat")
        if intent == "task":
            return "memory_reader_task"
        if intent == "memory":
            return "memory_reader_chat"
        return "route_persona"

    def route_persona(state: NexusState) -> str:
        p = state.get("active_persona", "gemma")
        if p == "qwen":
            return "qwen_agent"
        if p == "phi":
            return "phi_agent"
        return "gemma_agent"

    graph.add_node("router", _router_node)
    graph.add_node("memory_reader_task", memory_reader_task_node)
    graph.add_node("memory_reader_chat", memory_reader_chat_node)
    graph.add_node("planner_agent", planner_node)
    graph.add_node("executor_agent", _executor_agent)
    graph.add_node("route_persona", lambda s: s)
    graph.add_node("phi_agent", phi_node)
    graph.add_node("qwen_agent", qwen_node)
    graph.add_node("gemma_agent", gemma_node)
    graph.add_node("moderation", moderation_node)
    graph.add_node("memory_writer", _memory_writer)

    graph.add_edge(START, "router")
    graph.add_conditional_edges(
        "router",
        route_intent,
        {
            "memory_reader_task": "memory_reader_task",
            "memory_reader_chat": "memory_reader_chat",
            "route_persona": "route_persona",
        },
    )

    graph.add_edge("memory_reader_chat", "route_persona")
    graph.add_conditional_edges(
        "route_persona",
        route_persona,
        {
            "qwen_agent": "qwen_agent",
            "phi_agent": "phi_agent",
            "gemma_agent": "gemma_agent",
        },
    )

    graph.add_edge("phi_agent", "moderation")
    graph.add_edge("qwen_agent", "moderation")
    graph.add_edge("gemma_agent", "moderation")

    graph.add_edge("moderation", "memory_writer")

    graph.add_edge("memory_reader_task", "planner_agent")
    graph.add_edge("planner_agent", "executor_agent")
    graph.add_edge("executor_agent", "moderation")
    graph.add_edge("memory_writer", END)

    return graph.compile(checkpointer=checkpointer)
