"""Architecture gate: no raw-model decision path under ``agents/**``.

The mission's closing invariant is:

    every operational execution decision passes through typed cognition,
    deterministic routing, registry/policy authorization, the authorizer, the
    CommandBus and verification.  Legacy raw-model execution cannot cross it.

This gate makes that invariant *mechanically enforceable* rather than a matter
of review.  It scans the legacy agent tree structurally (AST, not grep) and
asserts:

A. no module under ``agents/**`` both consumes model text (``await *.generate``)
   and calls an execution sink (dispatch / tool run / shell / eval) — so a
   model response can never flow straight into an executable action;
B. no module under ``agents/**`` imports the canonical command bus or the
   cognition bridge — legacy agents cannot hand-build the executable path, and
   cannot bypass the boundary by constructing commands directly;
C. the set of model-consuming agent modules is pinned, so a *new* agent that
   starts calling a model must be consciously added here (and therefore
   reviewed against the boundary) instead of landing silently.

``agents/executor_agent.py`` legitimately calls ``registry.run`` but never calls
a model, so rule A does not fire on it: it executes a *deterministic plan step*,
not raw model text.  That distinction is exactly what rule A encodes.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
AGENTS = ROOT / "src" / "nexus_ai_agent" / "agents"

#: Calls that dispatch or realise an executable effect.
_EXECUTION_CALLS = {
    "dispatch",
    "run",
    "execute",
    "system",
    "popen",
    "spawn",
    "check_output",
    "check_call",
    "eval",
    "exec",
}

#: Modules a legacy agent must never import directly (the executable boundary).
_FORBIDDEN_IMPORTS = {
    "nexus_ai_agent.creative.studio.bus",
    "nexus_ai_agent.nagar.cognition.bridge",
}

#: Model-consuming modules that exist today.  Pinned deliberately: adding a
#: model call to a new agent must update this list, which forces a review of
#: whether that agent needs the cognition boundary.
_KNOWN_MODEL_CONSUMERS = {
    "src/nexus_ai_agent/agents/chat_agent.py",
    "src/nexus_ai_agent/agents/qwen_agent.py",
    "src/nexus_ai_agent/agents/gemma_agent.py",
    "src/nexus_ai_agent/agents/phi_agent.py",
    "src/nexus_ai_agent/agents/store/base_agent.py",
}


def _files() -> list[Path]:
    return sorted(AGENTS.rglob("*.py"))


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _calls_generate(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "generate":
                return True
    return False


def _execution_calls(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _EXECUTION_CALLS:
                found.append(f"{name}()@{node.lineno}")
    return found


def _imported_modules(tree: ast.AST) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
        elif isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
    return mods


def test_agents_tree_exists() -> None:
    assert _files(), "expected the legacy agents package to exist"


def test_no_agent_module_both_calls_a_model_and_executes() -> None:
    violations: list[str] = []
    for path in _files():
        tree = _tree(path)
        if _calls_generate(tree):
            sinks = _execution_calls(tree)
            if sinks:
                violations.append(f"{path.relative_to(ROOT)}: generate() + {sinks}")
    assert not violations, (
        "a legacy agent consumes model text AND calls an execution sink — "
        "raw-model execution is forbidden:\n" + "\n".join(violations)
    )


def test_no_agent_module_imports_the_executable_boundary() -> None:
    violations: list[str] = []
    for path in _files():
        forbidden = _imported_modules(_tree(path)) & _FORBIDDEN_IMPORTS
        if forbidden:
            violations.append(f"{path.relative_to(ROOT)}: {sorted(forbidden)}")
    assert not violations, "a legacy agent imports the executable boundary directly:\n" + "\n".join(
        violations
    )


def test_model_consuming_agents_are_pinned() -> None:
    actual = {str(path.relative_to(ROOT)) for path in _files() if _calls_generate(_tree(path))}
    assert actual == _KNOWN_MODEL_CONSUMERS, (
        "the set of model-consuming agent modules changed; update _KNOWN_MODEL_CONSUMERS "
        "and review each new agent against the cognition boundary.\n"
        f"added: {sorted(actual - _KNOWN_MODEL_CONSUMERS)}\n"
        f"removed: {sorted(_KNOWN_MODEL_CONSUMERS - actual)}"
    )
