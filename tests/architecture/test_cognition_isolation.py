"""Architecture gate for the Nagar cognition boundary.

The boundary is only trustworthy if it stays *authority-free* and
*dependency-light*.  These fitness tests make the laws executable:

* ``nagar.cognition`` may import ``creative.studio`` models (one direction)
  and ``pydantic`` plus a small stdlib set — nothing heavy, nothing that could
  execute;
* ``creative.studio`` must NOT import ``nagar`` (the boundary is upstream;
  the execution core must not depend on cognition);
* no module in the cognition package may reference an execution primitive
  (subprocess, os.system, eval/exec, socket, file writes) — a proposal
  producer can never run anything.  ``asyncio`` is allowed because the
  ``CognitionPort`` contract is async (a real provider adapter awaits the
  provider); it is not an execution primitive here.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
COGNITION = ROOT / "src" / "nexus_ai_agent" / "nagar" / "cognition"
STUDIO = ROOT / "src" / "nexus_ai_agent" / "creative" / "studio"

#: Top-level imports the cognition boundary may use (stdlib + pydantic + self).
ALLOWED_TOP_LEVEL = {
    "__future__",
    "asyncio",  # the CognitionPort contract is async; awaiting a provider is not execution
    "json",
    "time",
    "enum",
    "typing",
    "uuid",
    "pydantic",
    "nexus_ai_agent",
}

#: Execution / authority primitives a *proposal* producer must never touch.
FORBIDDEN_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "system",
    "popen",
    "spawn",
    "run",
    "call",
    "check_output",
    "check_call",
}
FORBIDDEN_IMPORTS = {
    "subprocess",
    "os",
    "sys",
    "socket",
    "shutil",
    "ctypes",
    "importlib",
    "pickle",
    "marshal",
    "multiprocessing",
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


def test_cognition_package_exists() -> None:
    files = sorted(COGNITION.rglob("*.py"))
    assert files, "expected the nagar cognition package to exist"


def test_cognition_modules_stay_inside_the_allowlist() -> None:
    for path in sorted(COGNITION.rglob("*.py")):
        imports = _top_level_imports(path)
        assert imports <= ALLOWED_TOP_LEVEL, (
            f"{path.relative_to(ROOT)} imports outside the cognition allowlist: "
            f"{sorted(imports - ALLOWED_TOP_LEVEL)}"
        )
        forbidden = imports & FORBIDDEN_IMPORTS
        assert not forbidden, f"{path.relative_to(ROOT)} imports execution primitives: {forbidden}"


def test_cognition_does_not_call_execution_primitives() -> None:
    violations: list[str] = []
    for path in sorted(COGNITION.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in FORBIDDEN_CALLS:
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} calls {name}()")
    assert not violations, "cognition boundary calls execution primitives:\n" + "\n".join(
        violations
    )


def test_studio_core_does_not_import_cognition() -> None:
    # The dependency must point one way: cognition -> studio.  If the execution
    # core ever imported cognition, the boundary would no longer be optional.
    violations: list[str] = []
    for path in sorted(STUDIO.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                module = ",".join(a.name for a in node.names)
            if module and "nagar" in module:
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {module}")
    assert not violations, "studio core imports the cognition boundary:\n" + "\n".join(violations)


def test_cognition_only_imports_its_own_package_and_studio_models() -> None:
    allowed_prefixes = (
        "nexus_ai_agent.nagar",
        "nexus_ai_agent.creative.studio",
    )
    violations: list[str] = []
    for path in sorted(COGNITION.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            module = None
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("nexus_ai_agent"):
                        module = alias.name
            if module and module.startswith("nexus_ai_agent"):
                if not module.startswith(allowed_prefixes):
                    violations.append(f"{path.relative_to(ROOT)} imports {module}")
    assert not violations, "cognition imports outside its allowed surface:\n" + "\n".join(
        violations
    )
