"""Nagar Wave 1 isolation gates.

The green cockpit core must stay a typed, UI-free, dependency-free skeleton:

* no React/DOM/Canvas (input is JSON, output is state);
* no heavy ML/CV dependencies (torch, transformers, ...);
* no imports from ``storage/``, ``llm/`` or any other package outside
  ``nexus_ai_agent.creative.studio``;
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
        assert imports <= ALLOWED_TOP_LEVEL, (
            f"{path.relative_to(ROOT)} imports outside the Wave 1 allowlist: {sorted(imports)}"
        )
        heavy = imports & FORBIDDEN_IMPORTS
        assert not heavy, f"{path.relative_to(ROOT)} imports heavy/UI dependencies: {sorted(heavy)}"


def test_studio_only_imports_its_own_package() -> None:
    for path in sorted(STUDIO.rglob("*.py")):
        for module in _nexus_ai_agent_modules(path):
            assert module.startswith("nexus_ai_agent.creative.studio"), (
                f"{path.relative_to(ROOT)} crosses the package boundary via {module!r} "
                "(storage/ and llm/ imports are forbidden in Wave 1)"
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
