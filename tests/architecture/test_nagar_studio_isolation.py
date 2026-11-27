"""Nagar Wave 1 isolation gates.

The green cockpit core must stay a typed, UI-free, dependency-free skeleton:

* no React/DOM/Canvas (input is JSON, output is state);
* no heavy ML/CV dependencies (torch, transformers, ...);
* no imports from ``storage/``, ``llm/`` or any other package outside
  ``nexus_ai_agent.creative.studio`` — with one reviewed exception: the durable
  state modules (``durable_state_modules``) persist pins and disaster-recovery
  bundles, so they may import the repository's single canonicalization authority
  (``nexus_ai_agent.provenance.canonicalization``) and nothing else;
* the default registry exposes exactly the Wave 1 catalog.
"""

from __future__ import annotations

import ast
from pathlib import Path

from nexus_ai_agent.creative.studio import PermissionLevel, build_wave1_registry

ROOT = Path(__file__).parents[2]
STUDIO = ROOT / "src" / "nexus_ai_agent" / "creative" / "studio"

#: Top-level modules the studio core may import (stdlib + pydantic + itself).
ALLOWED_TOP_LEVEL = {
    "__future__",
    "collections",
    "dataclasses",
    "enum",
    "hashlib",
    "json",
    "re",
    "threading",
    "typing",
    "uuid",
    "pydantic",
    "nexus_ai_agent",
}

#: Modules that own durable state (SQLite pinning, DR bundles, exact-rational
#: readback) rather than pure in-memory state.  They may reach the *single*
#: canonicalization authority; no other cross-package import is allowed.
DURABLE_STATE_MODULES = frozenset(
    {"revision_pinning.py", "preview_readback.py", "production_dr.py"}
)

#: The only ``nexus_ai_agent`` package a durable-state module may import.
CANONICALIZATION_AUTHORITY = "nexus_ai_agent.provenance.canonicalization"

#: What a durable-state module still may never import (pixels/audio/network).
DURABLE_STATE_STILL_FORBIDDEN = frozenset({"PIL", "httpx", "numpy", "socket", "subprocess", "wave"})

#: Extra stdlib roots a durable-state module may import beyond the core list.
DURABLE_STATE_TOP_LEVEL = frozenset(
    {
        "sqlite3",
        "os",
        "pathlib",
        "fcntl",
        "fractions",
        "math",
        "hmac",
        "datetime",
        "contextlib",
        "stat",
        "asyncio",
        "shutil",
        "tempfile",
        "time",
        "collections",
        # ``production_dr`` runs admitted blocking work in a bounded thread pool;
        # ``concurrent.futures`` is the stdlib primitive, not a new dependency.
        "concurrent",
    }
)

#: Imports that would break the Wave 1 "no heavy dependencies" rule.
FORBIDDEN_IMPORTS = {
    "torch",
    "transformers",
    "cv2",
    "opencv",
    "onnxruntime",
    "websockets",
    "fastapi",
}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _top_level_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _nexus_ai_agent_modules(path: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("nexus_ai_agent"):
                modules.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("nexus_ai_agent"):
                    modules.append(alias.name)
    return modules


def test_studio_modules_stay_inside_the_allowlist() -> None:
    files = sorted(STUDIO.rglob("*.py"))
    assert files, "expected the Nagar studio package to exist"
    for path in files:
        imports = _top_level_imports(path)
        # The durable-state stdlib surface (SQLite, POSIX locks, paths, exact
        # rationals) applies to its own reviewed modules only; every other studio
        # module keeps the Wave 1 allowlist unchanged.
        allowed = (
            ALLOWED_TOP_LEVEL | DURABLE_STATE_TOP_LEVEL
            if path.name in DURABLE_STATE_MODULES
            else ALLOWED_TOP_LEVEL
        )
        outside = sorted(imports - allowed)
        assert imports <= allowed, (
            f"{path.relative_to(ROOT)} imports outside the Wave 1 allowlist: {outside}"
        )
        heavy = imports & FORBIDDEN_IMPORTS
        assert not heavy, f"{path.relative_to(ROOT)} imports heavy/UI dependencies: {sorted(heavy)}"
        if path.name in DURABLE_STATE_MODULES:
            assert not (imports & DURABLE_STATE_STILL_FORBIDDEN), (
                f"{path.relative_to(ROOT)} (durable state) imports media/network primitives: "
                f"{sorted(imports & DURABLE_STATE_STILL_FORBIDDEN)}"
            )


def test_studio_only_imports_its_own_package() -> None:
    for path in sorted(STUDIO.rglob("*.py")):
        allowed_extra = (
            {CANONICALIZATION_AUTHORITY} if path.name in DURABLE_STATE_MODULES else set()
        )
        for module in _nexus_ai_agent_modules(path):
            assert module.startswith("nexus_ai_agent.creative.studio") or module in allowed_extra, (
                f"{path.relative_to(ROOT)} crosses the package boundary via {module!r} "
                "(storage/ and llm/ imports are forbidden; durable-state modules may only "
                f"import {CANONICALIZATION_AUTHORITY!r})"
            )


def test_default_registry_exposes_exactly_the_wave1_catalog() -> None:
    registry = build_wave1_registry()
    expected = {
        "media.play": PermissionLevel.IMMEDIATE,
        "media.pause": PermissionLevel.IMMEDIATE,
        "timeline.mark": PermissionLevel.REVERSIBLE,
        "timeline.split_at_playhead": PermissionLevel.REVERSIBLE,
        "system.undo": PermissionLevel.IMMEDIATE,
    }
    assert set(registry.list_operations()) == set(expected)
    for operation_id, level in expected.items():
        assert registry.get_spec(operation_id).permission_level is level, operation_id
