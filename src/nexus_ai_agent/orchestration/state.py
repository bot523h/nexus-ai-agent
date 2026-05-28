from __future__ import annotations

from typing import Literal, TypedDict


class NexusState(TypedDict):
    thread_id: str
    chat_id: int
    user_id: int
    correlation_id: str
    messages: list[dict]  # {"role": "user"|"assistant", "content": str}
    intent: Literal["chat", "task", "memory", "unknown"]
    current_task: dict | None
    tool_results: list[dict]
    memory_context: str
    response: str
    error: str | None
    turn_count: int

