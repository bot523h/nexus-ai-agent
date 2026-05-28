from __future__ import annotations


TASK_KEYWORDS = [
    "create",
    "make",
    "build",
    "plan",
    "schedule",
    "write",
    "delete",
    "run",
    "execute",
    "save",
]

MEMORY_KEYWORDS = [
    "remember",
    "recall",
    "what did",
    "last time",
]


def classify_intent(text: str) -> str:
    t = text.lower().strip()

    for kw in MEMORY_KEYWORDS:
        if kw in t:
            return "memory"

    for kw in TASK_KEYWORDS:
        if kw in t:
            return "task"

    return "chat"

