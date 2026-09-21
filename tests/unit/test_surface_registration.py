"""Registration contract for the ``bot/surface`` command layer.

``bot/handlers.py`` cannot be imported in a bare environment (it pulls in
``telegram``, ``langgraph`` and friends), so this file verifies the wiring
**statically, from the AST**: which symbols are imported, which commands are
registered to which handler, and that no stub is left behind.

That makes the contract testable in the same cheap job that runs the pure unit
tests — and, more importantly, it fails loudly if a future rebase silently
re-introduces a local stub shadowing the imported handler, which is exactly
how PR#32's value could have been lost again.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HANDLERS = ROOT / "src" / "nexus_ai_agent" / "bot" / "handlers.py"

#: command → the surface symbol it must be registered with
EXPECTED: dict[str, str] = {
    "daily": "daily_cmd",
    "profile": "profile_cmd",
    "achievements": "achievements_cmd",
    "xp_leaderboard": "xp_leaderboard_cmd",
    "docs": "docs_list_cmd",
    "doc_delete": "doc_delete_cmd",
    "chat_with_doc": "chat_with_doc_cmd",
}

#: The complete sentences the stubs answered with — verbatim, punctuation
#: included, so a new message that merely starts the same way (the real
#: "/chat_with_doc" confirmation does) is not a false positive.
STUB_STRINGS = (
    "🎁 Daily reward claimed: +50 XP!",
    "🏆 **XP Leaderboard**\n\n1. UserX: 5000 XP",
    "🏅 **Achievements**\n\n- First Message\n- 7 Day Streak",
    "📚 لیست اسناد شما خالی است (نسخه دمو).",
    "🗑️ سند حذف شد.",
    "🔍 حالت چت با سند فعال شد. سوال خود را بپرسید.",
)


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(HANDLERS.read_text(encoding="utf-8"), filename=str(HANDLERS))


@pytest.fixture(scope="module")
def source() -> str:
    return HANDLERS.read_text(encoding="utf-8")


def _command_registrations(tree: ast.Module) -> dict[str, str]:
    """Map ``CommandHandler("cmd", handler)`` → the handler symbol used."""
    registrations: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "CommandHandler"):
            continue
        if len(node.args) < 2:
            continue
        command, handler = node.args[0], node.args[1]
        if isinstance(command, ast.Constant) and isinstance(handler, ast.Name):
            registrations[str(command.value)] = handler.id
    return registrations


def test_handlers_module_parses(tree: ast.Module) -> None:
    assert tree.body, "handlers.py parsed to an empty module"


def test_the_seven_handlers_come_from_the_surface_package(tree: ast.Module) -> None:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nexus_ai_agent.bot.surface":
            imported.update(alias.asname or alias.name for alias in node.names)
    assert set(EXPECTED.values()) <= imported, f"missing surface imports: {imported}"


def test_every_command_is_registered_once_with_the_surface_handler(tree: ast.Module) -> None:
    registrations = _command_registrations(tree)
    for command, handler in EXPECTED.items():
        assert registrations.get(command) == handler, f"/{command} → {registrations.get(command)!r}"


def test_no_command_is_registered_twice(tree: ast.Module) -> None:
    seen: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "CommandHandler"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            command = str(node.args[0].value)
            seen[command] = seen.get(command, 0) + 1
    duplicated = {command: count for command, count in seen.items() if count > 1}
    assert duplicated == {}, f"double-registered commands: {duplicated}"


def test_no_local_stub_shadows_an_imported_handler(tree: ast.Module) -> None:
    """A nested `async def daily_cmd(...)` would silently shadow the import."""
    defined: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            defined.add(node.name)
    shadowed = defined & set(EXPECTED.values())
    assert shadowed == set(), f"local definitions shadow the surface handlers: {sorted(shadowed)}"


def test_no_stub_string_survives_in_handlers(source: str) -> None:
    """The file that shipped the stubs must not contain a single one."""
    offenders = [stub for stub in STUB_STRINGS if stub in source]
    assert offenders == [], f"stub strings still in handlers.py: {offenders}"


def test_no_stub_is_ever_replied_to_a_user() -> None:
    """No reply anywhere in the bot package may carry a stub literal.

    (The strings survive in ``bot/surface`` **docstrings** on purpose — they
    document what each command used to lie about. Docstrings are not replies.)
    """
    offenders: list[str] = []
    for path in (ROOT / "src" / "nexus_ai_agent" / "bot").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id in {"_reply", "reply"}):
                continue
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    for stub in STUB_STRINGS:
                        if stub in argument.value:
                            offenders.append(f"{path.name}:{node.lineno}: {stub}")
    assert offenders == [], f"stub replies shipped: {offenders}"


def test_free_text_is_routed_to_the_document_retriever(tree: ast.Module) -> None:
    """/chat_with_doc only works if on_message consults it before the LLM."""
    on_message = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_message"
    )

    routed: list[int] = []
    for node in ast.walk(on_message):
        if (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "route_doc_text"
        ):
            routed.append(node.lineno)
    assert routed, "on_message never calls route_doc_text"

    correlation = next(
        node.lineno
        for node in ast.walk(on_message)
        if isinstance(node, ast.Assign)
        for call in [node.value, *ast.walk(node.value)]
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "uuid4"
    )
    assert min(routed) < correlation, "doc-chat routing must precede the LLM path"


def test_the_surface_package_exports_exactly_these_commands() -> None:
    """`COMMAND_HANDLERS` is the contract the composition root reads."""
    from nexus_ai_agent.bot.surface import COMMAND_HANDLERS

    assert set(COMMAND_HANDLERS) == set(EXPECTED)
    assert all(callable(handler) for handler in COMMAND_HANDLERS.values())
