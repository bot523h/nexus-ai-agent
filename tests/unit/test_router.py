from __future__ import annotations

from nexus_ai_agent.orchestration.router import classify_intent


def test_chat_intent():
    assert classify_intent("How are you?") == "chat"


def test_task_intent():
    assert classify_intent("Create a file for me") == "task"


def test_memory_intent():
    assert classify_intent("Remember what I said") == "memory"
