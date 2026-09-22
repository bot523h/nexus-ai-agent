"""AST reachability for onboarding. Does not import production modules."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_start_calls_onboarding_and_callback_is_registered() -> None:
    text = _source("src/nexus_ai_agent/bot/feature_handlers.py")
    tree = ast.parse(text)
    start = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "start_cmd"
    )
    calls = [
        node.func.id
        for node in ast.walk(start)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "maybe_onboard" in calls
    assert 'pattern=r"^onboarding_"' in text or "onboarding_" in text
    assert "onboarding_callback" in text


def test_first_time_query_supports_execute_and_fails_closed() -> None:
    text = _source("src/nexus_ai_agent/features/onboarding.py")
    function = text.split("async def is_first_time_user", 1)[1].split("\nasync def", 1)[0]
    assert "session.exec" in function
    assert "session.execute" in function
    assert "return False" in function
    assert "return True" not in function.split("except", 1)[1]
