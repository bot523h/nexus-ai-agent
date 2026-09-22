"""AST reachability for channel commands. Does not import production modules."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
REQUIRED = {"post", "schedule", "ban", "unban", "stats", "welcome", "pin"}


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_channel_commands_and_new_members_are_in_the_prepend() -> None:
    text = _source("src/nexus_ai_agent/bot/feature_handlers.py")
    tree = ast.parse(text)
    commands: set[str] = set()
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "OPS_COMMANDS":
                value = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "OPS_COMMANDS":
                    value = node.value
        if isinstance(value, ast.Tuple):
            commands = {
                elt.value
                for elt in value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
    assert REQUIRED <= commands
    assert "NEW_CHAT_MEMBERS" in text
    assert "register_ops_handlers" in text


def test_composition_root_registers_ops_before_build_handlers() -> None:
    tree = ast.parse(_source("src/nexus_ai_agent/bot/app.py"))
    build = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_application"
    )
    register_line = None
    loop_line = None
    for node in build.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id == "register_ops_handlers":
                register_line = node.lineno
        if isinstance(node, ast.For):
            iter_node = node.iter
            if (
                isinstance(iter_node, ast.Call)
                and isinstance(iter_node.func, ast.Name)
                and iter_node.func.id == "build_handlers"
            ):
                loop_line = node.lineno
    assert register_line is not None and loop_line is not None
    assert register_line < loop_line


def test_leased_stubs_remain_until_the_fence_is_released() -> None:
    """handlers.py is fenced. The stubs staying is the fence, not the feature."""
    handlers = _source("src/nexus_ai_agent/bot/handlers.py")
    assert "Channel post simulated" in handlers or "simulated" in handlers
    assert "def build_handlers" in handlers
