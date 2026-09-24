"""Architecture ratchet: the creative channel must be wired end-to-end.

Four links in the canonical chain (task-166 / P0-B regression):

1. ``bot/app.py::build_application`` registers the creative command handlers
   (P0-B5: they used to never be registered — dead commands);
2. the registered command set is exactly ``{edit, caption, grade}``;
3. ``worker.py::default_job_handlers`` maps ``creative_render`` (P0-B1);
4. the completion notifier has a ``creative_render`` branch so results reach
   the origin chat (measured artifact or translated typed failure).

These are AST facts about the source, not runtime mocks: import graphs and
call sites are the contract.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOT_APP = ROOT / "src/nexus_ai_agent/bot/app.py"
WORKER = ROOT / "src/nexus_ai_agent/worker.py"


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _strings(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_build_application_registers_creative_surface() -> None:
    tree = _tree(BOT_APP)
    names = _names(tree)
    assert "build_creative_handlers" in names, (
        "build_application must call build_creative_handlers — otherwise /edit "
        "/caption /grade answer nothing (P0-B5 regression)"
    )
    assert any(n.endswith("CommandHandler") for n in names), (
        "registered functions must be wrapped in PTB CommandHandlers"
    )


def test_creative_commands_are_exactly_the_three_surface_commands() -> None:
    from nexus_ai_agent.bot.creative_surface import build_creative_handlers

    class _Q:  # noqa: D401 - structural stub, never called
        pass

    handlers = build_creative_handlers(_Q())  # type: ignore[arg-type]
    assert set(handlers) == {"edit", "caption", "grade"}


def test_worker_routes_creative_render() -> None:
    tree = _tree(WORKER)
    assert '"creative_render"' in {repr(s) for s in _strings(tree)} or (
        "creative_render" in _strings(tree)
    )
    assert "creative_render_job" in {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }


def test_composition_root_registers_the_queue_handler() -> None:
    """Bot and worker drain the same queue with the same handler map — a
    resumed job must resolve its handler in either process."""
    from nexus_ai_agent.worker import default_job_handlers

    handlers = default_job_handlers()
    assert "creative_render" in handlers
    assert callable(handlers["creative_render"])


def test_completion_notifier_handles_creative_render() -> None:
    tree = _tree(BOT_APP)
    assert "creative_render" in _strings(tree), (
        "completion notifier must branch on creative_render — else results die silently"
    )
    assert "_notify_creative_completion" in {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
