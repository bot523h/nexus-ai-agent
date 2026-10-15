from __future__ import annotations

from typing import Any, Literal, TypedDict


class NexusState(TypedDict):
    thread_id: str
    chat_id: int
    user_id: int
    correlation_id: str
    messages: list[dict[str, Any]]  # {"role": "user"|"assistant", "content": str}
    intent: Literal["chat", "task", "memory", "unknown"]
    active_persona: str
    current_task: dict[str, Any] | None
    tool_results: list[dict[str, Any]]
    memory_context: str
    response: str
    error: str | None
    turn_count: int
    moderation_passed: bool
