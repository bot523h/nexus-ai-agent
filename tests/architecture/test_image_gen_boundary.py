"""Image generation must not depend on bot/storage, even through local helpers.

Resolve relative imports and `from package import module`, inspect imports in
functions/TYPE_CHECKING blocks, and prohibit dynamic import escape hatches in
the pack. No production modules are imported to perform this check.
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2] / "src"
PACK = ROOT / "nexus_ai_agent" / "creative" / "image_gen"
FORBIDDEN = ("nexus_ai_agent.bot", "nexus_ai_agent.storage", "bot", "storage")


def _imports(source: str, package: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, package)
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def _forbidden(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN)


def _local_files(name: str) -> set[Path]:
    path = ROOT.joinpath(*name.split("."))
    candidates = {path.with_suffix(".py"), path / "__init__.py"}
    # Package initializers execute too, including for `import package.child`.
    candidates.update(parent / "__init__.py" for parent in path.parents if ROOT in parent.parents)
    return {candidate for candidate in candidates if candidate.is_file()}


def test_image_gen_has_no_direct_or_transitive_bot_storage_dependency() -> None:
    pending = list(PACK.rglob("*.py"))
    assert pending, "the image_gen pack must exist, not vacuously pass this gate"
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        package = ".".join(path.relative_to(ROOT).parent.parts)
        imports = _imports(path.read_text(encoding="utf-8"), package)
        offenders = sorted(name for name in imports if _forbidden(name))
        assert not offenders, f"image_gen dependency {path.relative_to(ROOT)} imports {offenders}"
        for name in imports:
            if name.startswith("nexus_ai_agent"):
                pending.extend(_local_files(name) - visited)


def test_image_gen_cannot_bypass_boundaries_with_dynamic_imports() -> None:
    for path in PACK.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        package = ".".join(path.relative_to(ROOT).parent.parts)
        imports = _imports(source, package)
        assert not any(name.split(".")[0] in {"importlib", "builtins"} for name in imports), path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                assert node.id not in {"__import__", "exec", "eval"}, path
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"__import__", "import_module", "exec", "eval"}, path


@pytest.mark.parametrize(
    "source",
    [
        "import nexus_ai_agent.bot.handlers as h",
        "import nexus_ai_agent.storage.db",
        "from nexus_ai_agent import bot",
        "from nexus_ai_agent.storage import db",
        "from ...bot import handlers",
        "from ... import storage",
        "if TYPE_CHECKING:\n    from ...storage import db",
        "def delayed():\n    from nexus_ai_agent.bot import handlers",
        "import bot.handlers",
    ],
)
def test_boundary_detector_recognizes_absolute_relative_and_deferred_imports(source: str) -> None:
    assert any(_forbidden(name) for name in _imports(source, "nexus_ai_agent.creative.image_gen"))


def test_boundary_detector_does_not_reject_local_provider_imports() -> None:
    assert not any(
        _forbidden(name)
        for name in _imports(
            "from .provider import ImageGenProvider", "nexus_ai_agent.creative.image_gen"
        )
    )
