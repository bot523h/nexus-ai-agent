from __future__ import annotations

from typing import Literal, TypedDict


class _NexusStateRequired(TypedDict):
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


class NexusState(_NexusStateRequired, total=False):
    """LangGraph state; ``memory_consent`` is optional by contract.

    The effective per-user long-term-memory consent decision travels with the
    run: the Telegram message handler carries what the P0-7 gate produced
    (explicit grant + feature enabled) into this key.  It is declared on a
    ``total=False`` subclass so existing constructors (the CLI ``smoke``
    command, tests, resumed checkpoint states) stay type-valid without the key;
    every consumer must read an absent or malformed value as *denied* —
    fail closed, never a write.
    """

    memory_consent: bool
