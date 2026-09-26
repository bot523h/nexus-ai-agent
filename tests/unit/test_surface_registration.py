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
    # dead engines batch (see docs/DECISION_LOG.md D-0009): the advertisement
    # engine and the channel manager were never imported anywhere in ``src/``.
    "ad_create": "ad_create_cmd",
    "ad_list": "ad_list_cmd",
    "ad_pause": "ad_pause_cmd",
    "ad_resume": "ad_resume_cmd",
    "ad_delete": "ad_delete_cmd",
    "ad_stats": "ad_stats_cmd",
    "post": "post_cmd",
    "schedule": "schedule_cmd",
    "pin": "pin_cmd",
    "ban": "ban_cmd",
    "unban": "unban_cmd",
    "stats": "stats_cmd",
    "welcome": "welcome_cmd",
    # Phase 13: the moderation engine (add_warning/mute_user/unmute_user/
    # get_reputation) had no importer beyond a single set_config call, so every
    # warning and every mute was discarded. Registered under the short names.
    "mod_config": "mod_config_cmd",
    "warn": "mod_warn_cmd",
    "mute": "mod_mute_cmd",
    "unmute": "mod_unmute_cmd",
    "reputation": "mod_reputation_cmd",
    # Phase 13: /viral_now called the real engine while its three siblings
    # answered with constants — including a likes counter no column can produce.
    "viral_preview": "viral_preview_cmd",
    "viral_stats": "viral_stats_cmd",
    "viral_post": "viral_post_cmd",
    # Phase 13: /storage and /model were registered with a body of ``pass``
    # (answering with silence); /story_style offered three styles the renderer
    # discards at ``_ = style``.
    "model": "model_cmd",
    "storage": "storage_cmd",
    "story_style": "story_style_cmd",
}

#: callback pattern → the surface symbol that must handle it. Kept separate from
#: ``EXPECTED`` because these are ``CallbackQueryHandler`` registrations.
EXPECTED_CALLBACKS: dict[str, str] = {
    "^onboarding_": "onboarding_callback_cmd",
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
    # Phase 12: the advertisement engine had no importer at all.
    "📢 Ad campaign created successfully.",
    "📢 Active Ads: 2, Paused: 1.",
    "⏸️ Ad paused.",
    "▶️ Ad resumed.",
    "🗑️ Ad deleted.",
    "📊 Ad Stats: 5k impressions, 200 clicks.",
    # Phase 1: "simulated" replies — the operator was told the action ran.
    "✅ Post sent to channel (simulated).",
    "📅 Post scheduled (simulated).",
    "🚫 User banned (simulated).",
    "✅ User unbanned (simulated).",
    "📊 Group stats: 150 members, 1.2k messages/day.",
    "👋 Welcome message updated.",
    "📌 Message pinned.",
    "✅ Onboarding step completed!",
    # Phase 13: moderation — counters that never moved because nothing wrote.
    "🛡️ Moderation rules updated.",
    "⚠️ User warned (1/3).",
    "🔇 User muted for 10 minutes.",
    "🔊 User unmuted.",
    "👤 User Reputation: 85/100 (Good).",
    # Phase 13: viral — "450 likes" cannot be produced by any column in the
    # ViralPost table (chat_id, text, viral_score, status, posted_at).
    "🔥 Preview: Top AI trends of the week...",
    "🔥 Viral Engine: 12 posts sent, 450 likes total.",
    "📋 Pending viral posts: 3 in queue.",
    # Phase 13: /story_style advertised styles create_story() throws away.
    "🎨 استایل فعلی: Motivational\nگزینه‌ها: Motivational | Romantic | Success",
    # Phase 13: /vision answered this without downloading the photo.
    "I see a beautiful landscape in this image.",
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


def test_the_surface_handlers_are_imported_from_the_surface_package(tree: ast.Module) -> None:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nexus_ai_agent.bot.surface":
            imported.update(alias.asname or alias.name for alias in node.names)
    assert set(EXPECTED.values()) <= imported, f"missing surface imports: {imported}"


def test_every_command_is_registered_once_with_the_surface_handler(tree: ast.Module) -> None:
    registrations = _command_registrations(tree)
    for command, handler in EXPECTED.items():
        assert registrations.get(command) == handler, f"/{command} → {registrations.get(command)!r}"


def _local_defs(tree: ast.Module) -> set[str]:
    """Every function name defined anywhere in the module (incl. nested ones)."""
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _callback_registrations(tree: ast.Module) -> dict[str, str]:
    """Map ``CallbackQueryHandler(symbol, pattern="…")`` → pattern to symbol."""
    registrations: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "CallbackQueryHandler"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        keyword = next((k for k in node.keywords if k.arg == "pattern"), None)
        if keyword is None or not isinstance(keyword.value, ast.Constant):
            continue
        registrations[str(keyword.value.value)] = node.args[0].id
    return registrations


def test_the_onboarding_callback_is_owned_by_the_surface(tree: ast.Module) -> None:
    """``^onboarding_`` must route to the surface, not to a local stub.

    The handler this replaced answered every callback id under that pattern
    with a success sentence and edited the message; this test is what keeps a
    stub from creeping back into either side of the registration.
    """
    registrations = _callback_registrations(tree)
    for pattern, symbol in EXPECTED_CALLBACKS.items():
        assert registrations.get(pattern) == symbol, f"{pattern} → {registrations.get(pattern)!r}"

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nexus_ai_agent.bot.surface":
            imported.update(alias.asname or alias.name for alias in node.names)
    assert set(EXPECTED_CALLBACKS.values()) <= imported, (
        "callback handler not imported from surface"
    )

    shadowed = _local_defs(tree) & set(EXPECTED_CALLBACKS.values())
    assert shadowed == set(), f"local definitions shadow the surface callback: {sorted(shadowed)}"


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


def test_no_registered_command_answers_with_silence(tree: ast.Module) -> None:
    """A registered command whose body is ``pass`` is worse than a stub.

    ``/storage`` and ``/model`` were registered — so Telegram advertised them
    in the command list — with a body of exactly ``pass``. The user got no
    reply at all, which is indistinguishable from the bot being down: no
    error, no log line, nothing to debug against. This test fails if any
    locally-defined handler reachable from a ``CommandHandler`` registration
    degenerates to a docstring plus ``pass``.
    """
    registered = set(_command_registrations(tree).values())
    empty: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name not in registered:
            continue
        body = [
            statement
            for statement in node.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        if not body or all(isinstance(statement, ast.Pass) for statement in body):
            empty.append(f"{node.name}:{node.lineno}")
    assert empty == [], f"registered commands that reply with silence: {empty}"


def test_moderation_and_viral_surfaces_never_import_telegram() -> None:
    """The surface layer talks to PTB through duck-typing only (see ._ptb).

    ``tests/architecture/test_import_boundaries.py`` enforces this globally
    against a frozen baseline; this is the local, fast assertion for the two
    modules added in this phase, so the failure names the right file.
    """
    surface = ROOT / "src" / "nexus_ai_agent" / "bot" / "surface"
    for name in ("moderation.py", "viral.py", "status.py"):
        path = surface / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not a.name.startswith("telegram") for a in node.names), name
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("telegram"), name


def test_every_surface_module_documents_the_stub_it_replaced() -> None:
    """Each surface module keeps the exact sentences it removed, verbatim.

    Those tuples are the evidence trail: ``STUB_STRINGS`` in this file is the
    union, and a module that stops declaring its own would let a future rebase
    quietly reintroduce the literal it was written to delete.
    """
    import importlib

    union: set[str] = set()
    modules = (
        "ads",
        "channel_management",
        "docs",
        "onboarding",
        "moderation",
        "viral",
        "status",
    )
    for name in modules:
        module = importlib.import_module(f"nexus_ai_agent.bot.surface.{name}")
        declared = getattr(module, "STUB_STRINGS", None)
        assert isinstance(declared, tuple) and declared, f"{name} declares no STUB_STRINGS"
        union.update(declared)

    # Every sentence a surface module claims to have removed must also be in
    # this file's guard list, or the global "never reply with it" test above
    # would not actually cover it.
    uncovered = sorted(stub for stub in union if stub not in STUB_STRINGS)
    assert uncovered == [], f"surface stubs not guarded by STUB_STRINGS: {uncovered}"
