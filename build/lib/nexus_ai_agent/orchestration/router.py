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


_STORY = [
    "story",
    "tale",
    "once upon",
    "narrate",
    "fiction",
    "character",
    "adventure",
    "imagine",
    "roleplay",
    "continue",
    "chapter",
    "plot",
    "write a",
]
_LOGIC = [
    "analyze",
    "explain why",
    "how does",
    "compare",
    "reason",
    "calculate",
    "proof",
    "evidence",
    "fact check",
    "moderate",
    "logic",
]
_SOCIAL = [
    "feel",
    "sad",
    "happy",
    "lonely",
    "friend",
    "talk to me",
    "listen",
    "support",
    "advice",
    "how are you",
    "miss you",
    "love",
    "care",
]


def select_persona(text: str) -> str:
    t = text.lower()
    for kw in _STORY:
        if kw in t:
            return "qwen"
    for kw in _LOGIC:
        if kw in t:
            return "phi"
    for kw in _SOCIAL:
        if kw in t:
            return "gemma"
    return "gemma"
