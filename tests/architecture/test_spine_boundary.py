"""Boundary gates for the creative execution spine (D-0024).

The spine is an upstream *planner* that compiles an intent into commands. It
must stay a pure, dependency-light core and must never become a second write
path:

* stdlib + pydantic + ``creative.studio`` only -- no shell/media/ML imports
  (the studio isolation gate's rule, applied to the spine);
* the spine never imports a media/executor module directly;
* the only edge that reaches a registered operation handler is still
  ``studio/bus.py`` -- the spine dispatches through the bus and nothing else.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
SPINE = ROOT / "src/nexus_ai_agent/creative/spine"
CREATIVE = ROOT / "src/nexus_ai_agent/creative"
BUS = CREATIVE / "studio" / "bus.py"

#: stdlib + pydantic + itself (mirrors ``test_nagar_studio_isolation``).
ALLOWED_TOP_LEVEL = {
    "__future__",
    "dataclasses",
    "hashlib",
    "json",
    "typing",
    "uuid",
    "pydantic",
    "nexus_ai_agent",
}

FORBIDDEN_IMPORTS = {
    "subprocess",
    "socket",
    "shutil",
    "os",
    "ctypes",
    "torch",
    "transformers",
    "cv2",
    "opencv",
    "numpy",
    "moviepy",
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


def _nexus_imports(path: Path) -> list[str]:
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


def test_spine_exists_and_stays_inside_the_allowlist() -> None:
    files = sorted(SPINE.rglob("*.py"))
    assert files, "expected the creative spine package to exist"
    for path in files:
        imports = _top_level_imports(path)
        assert imports <= ALLOWED_TOP_LEVEL, (
            f"{path.relative_to(ROOT)} imports outside the spine allowlist: "
            f"{sorted(imports - ALLOWED_TOP_LEVEL)}"
        )
        forbidden = imports & FORBIDDEN_IMPORTS
        assert not forbidden, f"{path.relative_to(ROOT)} imports {sorted(forbidden)}"


def test_spine_only_imports_the_studio_core() -> None:
    allowed = ("nexus_ai_agent.creative.studio", "nexus_ai_agent.creative.spine")
    for path in sorted(SPINE.rglob("*.py")):
        for module in _nexus_imports(path):
            assert module.startswith(allowed), (
                f"{path.relative_to(ROOT)} crosses into {module!r}; the spine may only "
                "depend on creative.studio (and itself) — never a pack, executor or "
                "render lane"
            )


def test_only_the_bus_invokes_an_operation_handler() -> None:
    """The spine is not a second write path: it must not call ``.handler``."""
    direct: list[str] = []
    for path in sorted(CREATIVE.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "handler"
            ):
                direct.append(str(path.relative_to(ROOT)))
    assert direct == [str(BUS.relative_to(ROOT))], (
        f"only studio/bus.py may invoke a registered operation handler; found: {direct}"
    )
