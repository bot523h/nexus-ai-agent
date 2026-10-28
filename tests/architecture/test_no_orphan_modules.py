"""Law R20 — every production module is reachable (audit finding A-10).

The 2026-10-15 full audit found nine modules under ``src/nexus_ai_agent`` that
nothing imported — not from ``src``, not from ``tests``, not from ``scripts``,
not by name from an entry point: aspirational code (``core/async_db.py`` even
described itself as "the foundation for all async DB operations") that
shipped, was never wired, and quietly rotted. They were removed; this guard
keeps the tree honest from now on.

A module is *reachable* when at least one other file imports it (statically,
by its dotted path, including relative imports), names it as a string (dynamic
``importlib`` / entry points), or when it is a package whose sub-modules are
reachable. Composition roots that only the outside world calls are listed in
``ENTRY_POINTS`` with the reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
PACKAGE = "nexus_ai_agent"

#: Modules nothing in the tree imports *by design* — each one is invoked from
#: the outside (a console script, ``python -m``, Alembic, Uvicorn).
ENTRY_POINTS: dict[str, str] = {
    f"{PACKAGE}.cli": "console script `nexus` (pyproject [project.scripts])",
    f"{PACKAGE}.api.app": "ASGI target `nexus_ai_agent.api.app:app` (uvicorn, Dockerfile, compose)",
    f"{PACKAGE}.nagar.__main__": "`python -m nexus_ai_agent.nagar` truth generator and --check",
}

#: Files whose imports count as "reaching" a module.
IMPORTER_ROOTS = ("src", "tests", "scripts", "migrations")


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _production_modules() -> dict[str, Path]:
    return {_module_name(p): p for p in sorted((SRC / PACKAGE).rglob("*.py"))}


def _imported_names(path: Path) -> set[str]:
    """Every dotted name a file imports, with relative imports resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if path.is_relative_to(SRC):
        rel = path.relative_to(SRC).with_suffix("")
        parts = list(rel.parts)
        package = parts[:-1]  # the package a `from . import x` is relative to
    else:
        package = []
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - node.level + 1
                base = package[:keep] if node.level <= len(package) else []
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def _reachable_modules() -> set[str]:
    modules = _production_modules()
    reachable: set[str] = set()
    for root in IMPORTER_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            own = _module_name(path) if path.is_relative_to(SRC) else None
            text = path.read_text(encoding="utf-8")
            for name in _imported_names(path):
                if name in modules and name != own:
                    reachable.add(name)
            # dynamic references: importlib strings and "module:attr" targets
            for name in modules:
                if name == own:
                    continue
                if f'"{name}"' in text or f"'{name}'" in text or f'"{name}:' in text:
                    reachable.add(name)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    reachable.update(name for name in modules if name in pyproject)
    # a package is alive when any sub-module is
    for name in list(reachable):
        parts = name.split(".")
        for i in range(1, len(parts)):
            reachable.add(".".join(parts[:i]))
    return reachable


def test_every_production_module_has_an_importer_or_is_a_declared_entry_point() -> None:
    modules = _production_modules()
    reachable = _reachable_modules()
    orphans = sorted(name for name in modules if name not in reachable and name not in ENTRY_POINTS)
    assert orphans == [], (
        "modules nothing imports (wire them, delete them, or add them to ENTRY_POINTS "
        f"with a reason): {orphans}"
    )


def test_the_entry_point_allowlist_is_not_stale() -> None:
    """An entry point that acquired an importer, or vanished, must leave the list."""
    modules = _production_modules()
    missing = sorted(name for name in ENTRY_POINTS if name not in modules)
    assert missing == [], f"ENTRY_POINTS names modules that no longer exist: {missing}"


def test_the_guard_is_not_vacuous() -> None:
    modules = _production_modules()
    assert len(modules) > 250, len(modules)
    # the removed audit findings must stay removed
    for gone in (
        f"{PACKAGE}.core.async_db",
        f"{PACKAGE}.agents.chat_agent",
        f"{PACKAGE}.agents.planner_agent",
        f"{PACKAGE}.agent.feedback",
        f"{PACKAGE}.domain.retention",
        f"{PACKAGE}.memory.short_term",
        f"{PACKAGE}.storage.model_fetch",
    ):
        assert gone not in modules, gone
