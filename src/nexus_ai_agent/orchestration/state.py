from __future__ import annotations

from typing import Literal, TypedDict

from typing_extensions import NotRequired


class NexusState(TypedDict):
    thread_id: str
    chat_id: int
    user_id: int
    correlation_id: str
    messages: list[dict]  # {"role": "user"|"assistant", "content": str}
    intent: Literal["chat", "task", "memory", "unknown"]
    active_persona: str
    current_task: dict | None
    tool_results: list[dict]
    memory_context: str
    response: str
    error: str | None
    turn_count: int
    moderation_passed: bool
    # Host-injected, trusted upload metadata; never populated by the LLM.
    creative_asset: NotRequired[dict[str, object] | None]
    # Structured request/plan/job/artifact/verification trace for the agent path.
    agent_intelligence: NotRequired[dict[str, object]]
