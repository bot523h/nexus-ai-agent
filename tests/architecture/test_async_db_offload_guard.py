"""Architecture gates: synchronous DB engine calls must stay off the event loop.

``src/nexus_ai_agent/bot/`` is the asyncio layer of the Telegram bot. The
feature engines (``ModerationEngine``, ``ViralEngine``, ``QuizGame``,
``ReferralEngine``, ``ForceJoinManager``) perform *synchronous* SQLAlchemy
work: every public method builds its own engine via ``_sync_engine()`` and
opens a ``Session``. Calling such a method directly inside a handler blocks
the whole event loop for the DB round-trip and is the first step towards
sharing loop-bound objects across threads.

The single approved pattern is::

    result = await asyncio.to_thread(ModerationEngine.check_message, user_id, chat_id, text)

Gate 1 (mutation G, FORENSIC_REPORT_task122) parses every bot-layer module
with :mod:`ast` — deterministic, stdlib-only, nothing executed — and fails
the moment a sync-engine call is *not* a direct argument of an
``asyncio.to_thread(...)`` call. Deleting the offload wrapper turns the
suite red here, with the violating file/line in the failure message.

Gate 2 pins the matching producer-side invariant in
``features/moderation.py``: engine/session objects are created inside each
call and never accepted from, or cached for, a caller (which is what makes
the consumer-side offload safe — no Session crosses a thread boundary).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
BOT = ROOT / "src" / "nexus_ai_agent" / "bot"
MODERATION = ROOT / "src" / "nexus_ai_agent" / "features" / "moderation.py"

#: Engine classes whose public methods do synchronous SQLAlchemy work.
SYNC_DB_ENGINES = {
    "ModerationEngine",
    "ViralEngine",
    "QuizGame",
    "ReferralEngine",
    "ForceJoinManager",
}

#: ``(Engine, method)`` pairs that are proven pure (no engine/session opened)
#: and may therefore be called directly on the loop.  Everything else must go
#: through ``asyncio.to_thread``.  Extend this list only with a genuinely pure
#: function — never to silence a real offload regression.
PURE_ENGINE_METHODS = {
    ("ViralEngine", "calculate_viral_score"),  # pure heuristic over a str
    ("ForceJoinManager", "get_join_keyboard"),  # builds an InlineKeyboardMarkup
}


def _engine_aliases(tree: ast.AST) -> dict[str, str]:
    """Map local names to sync-engine classes (``import X as Y`` aware)."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".")
            absolute = parts[:2] == ["nexus_ai_agent", "features"]
            relative = node.level > 0 and parts[:1] == ["features"]
            if absolute or relative:
                for alias in node.names:
                    if alias.name in SYNC_DB_ENGINES:
                        aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if (
                    parts[:2] == ["nexus_ai_agent", "features"]
                    and len(parts) == 3
                    and parts[-1] in SYNC_DB_ENGINES
                ):
                    aliases[alias.asname or alias.name] = parts[-1]
    return aliases


def _to_thread_wrapped_calls(tree: ast.AST) -> set[int]:
    """IDs of ``Call`` nodes passed as direct arguments of ``asyncio.to_thread``."""
    wrapped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_to_thread = (
                isinstance(func, ast.Attribute)
                and func.attr == "to_thread"
                and isinstance(func.value, ast.Name)
                and func.value.id == "asyncio"
            )
            if is_to_thread:
                wrapped.update(id(arg) for arg in node.args if isinstance(arg, ast.Call))
    return wrapped


def _direct_sync_db_calls(tree: ast.AST, path: Path) -> list[str]:
    """Violations: sync-engine calls that are neither to_thread-wrapped nor pure."""
    aliases = _engine_aliases(tree)
    wrapped = _to_thread_wrapped_calls(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        base = node.func.value
        if not isinstance(base, ast.Name):
            continue
        engine = aliases.get(base.id)
        if engine is None or engine not in SYNC_DB_ENGINES:
            continue
        if id(node) in wrapped:
            continue
        method = node.func.attr
        if (engine, method) in PURE_ENGINE_METHODS:
            continue
        rel = path.relative_to(ROOT)
        violations.append(
            f"{rel}:{node.lineno}: {base.id}.{method}() runs on the event loop — "
            f"wrap it: await asyncio.to_thread({base.id}.{method}, ...)"
        )
    return violations


def test_bot_layer_offloads_sync_db_engines() -> None:
    """No bot-layer module may call a sync DB engine outside asyncio.to_thread."""
    violations: list[str] = []
    for path in sorted(BOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_direct_sync_db_calls(tree, path))
    assert not violations, (
        "Sync DB engine call(s) found outside asyncio.to_thread in the bot layer.\n"
        f"Allowed engines: {sorted(SYNC_DB_ENGINES)}\n"
        f"Proven-pure exceptions: {sorted(PURE_ENGINE_METHODS)}\n"
        "Every other call opens a synchronous SQLAlchemy Session and blocks the\n"
        "loop; the approved pattern is\n"
        "    result = await asyncio.to_thread(Engine.method, ...)\n"
        "Violations:\n" + "\n".join(violations)
    )


def _is_call_to(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def test_moderation_creates_db_objects_per_call_only() -> None:
    """``ModerationEngine`` must not cache or accept engine/session objects.

    This is the producer-side half of the thread-offload contract: every
    ``Session`` use is paired with a locally-created ``_sync_engine()`` in the
    same function, no module/class-level SQLAlchemy state exists, and no
    method takes a ``Session``/``Engine`` parameter — so no DB object can
    cross a thread boundary when handlers offload via ``asyncio.to_thread``.
    """
    tree = ast.parse(MODERATION.read_text(encoding="utf-8"), filename=str(MODERATION))
    problems: list[str] = []

    # 1) Module-level assignments must not create/hold engine or Session state.
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is not None and (
                _is_call_to(value, "create_engine") or _is_call_to(value, "Session")
            ):
                problems.append(f"moderation.py:{node.lineno}: module-level engine/Session state")

    # 2) Every Session( use must be paired with _sync_engine() in the same function.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = list(ast.walk(node))
        uses_session = any(_is_call_to(n, "Session") for n in body)
        builds_engine = any(_is_call_to(n, "_sync_engine") for n in body)
        if uses_session and not builds_engine:
            problems.append(
                f"moderation.py:{node.lineno}: {node.name}() opens a Session without a "
                "locally-created _sync_engine() — a foreign Session would cross threads"
            )
        # 3) No public method may accept a shared Session/Engine parameter.
        for arg in [*node.args.args, *node.args.kwonlyargs]:
            if arg.annotation is None:
                continue
            ann = ast.unparse(arg.annotation)
            parts = {p for p in ann.replace("[", ".").replace("]", ".").split(".") if p}
            if parts & {"Session", "Engine"}:
                problems.append(
                    f"moderation.py:{node.lineno}: {node.name}(... {arg.arg}: {ann}) accepts "
                    "a shared DB object — build one via _sync_engine() inside the call"
                )

    assert not problems, (
        "ModerationEngine thread-offload invariant violated.\n"
        "Each call must build its own engine/session (via _sync_engine()) and never\n"
        "accept or cache one — that is what keeps asyncio.to_thread offload safe.\n"
        "Problems:\n" + "\n".join(problems)
    )
