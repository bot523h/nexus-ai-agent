"""Architecture gates for the Creative Intelligence Plane.

The plane owns ``INTENT -> UNDERSTANDING -> STRATEGY -> TYPED CREATIVE IR ->
COMPILATION -> EXECUTABLE PLAN``. It does **not** own the last two boxes of the
full chain. These gates make that a build failure rather than an intention:

1. **the plane never executes** -- no process spawning, no file or network I/O,
   no database, no async runtime. A package that cannot touch the world cannot
   become a second execution authority by accident;
2. **the plane never trespasses** -- it imports nothing from
   ``creative/studio`` (the Canonical Creative Execution Substrate),
   ``creative/spine``, ``creative/rendering``, the packs, or ``storage``.
   Coupling to any of them would make the IR a view onto someone else's state
   instead of an independent contract, and would put this package inside another
   agent's ownership zone;
3. **the dependency surface is closed** -- stdlib plus pydantic and the plane's
   own modules. No ML, no media, no HTTP client: understanding and compilation
   are pure transformations, and anything that needs a provider sits behind a
   port that a *later* slice declares;
4. **the shared time base really is shared** -- ``MICROSECONDS_PER_SECOND`` is
   redeclared rather than imported (rule 2 forbids the import), so a ratchet
   here fails the build if the execution substrate's constant ever moves;
5. **the public surface is the declared surface** -- ``__all__`` and the actual
   exported names must agree, so the plane's contract is legible to the agents
   that consume it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
PLANE = REPO_ROOT / "src" / "nexus_ai_agent" / "creative" / "intelligence"

#: Everything the plane is allowed to import. Deliberately tiny: this package is
#: a pure transformation layer and each addition here is an architecture change.
ALLOWED_TOP_LEVEL = {
    "__future__",
    "enum",
    "hashlib",
    "json",
    "typing",
    "pydantic",
    "nexus_ai_agent",
}

#: The only ``nexus_ai_agent`` subtree the plane may reach into: itself.
ALLOWED_NEXUS_PREFIXES = ("nexus_ai_agent.creative.intelligence",)

#: Modules whose presence would mean the plane had started executing, storing or
#: calling out. Each one names a boundary the plane must not cross.
FORBIDDEN_EXECUTION_MODULES = {
    "asyncio",  # the plane is synchronous and total; no event loop, no worker
    "httpx",
    "os",
    "requests",
    "shutil",
    "socket",
    "sqlite3",
    "subprocess",
    "tempfile",
    "urllib",
}

#: Heavy or provider-specific modules: understanding may need a model one day,
#: but it will need it behind a port, not imported by the IR.
FORBIDDEN_HEAVY_MODULES = {
    "chromadb",
    "cv2",
    "ffmpeg",
    "litellm",
    "moviepy",
    "numpy",
    "onnxruntime",
    "sentence_transformers",
    "torch",
}

#: Ownership zones belonging to other agents. Importing any of these would put
#: the plane inside someone else's exclusive paths and couple the IR to state
#: the plane does not own.
FORBIDDEN_NEXUS_PREFIXES = (
    "nexus_ai_agent.creative.studio",
    "nexus_ai_agent.creative.spine",
    "nexus_ai_agent.creative.rendering",
    "nexus_ai_agent.creative.packs",
    "nexus_ai_agent.creative.slideshow",
    "nexus_ai_agent.storage",
    "nexus_ai_agent.llm",
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.adapters",
    "nexus_ai_agent.bot",
    "nexus_ai_agent.api",
    "nexus_ai_agent.features",
    "nexus_ai_agent.application",
    "nexus_ai_agent.worker",
)

#: Vocabulary of the execution authority. The plane produces plans for the bus to
#: run; it must never name the machinery that runs them.
FORBIDDEN_EXECUTION_NAMES = {
    "CommandBus",
    "PlanTransaction",
    "EditTransaction",
    "TypedCommand",
    "execute",
    "dispatch",
    "undo",
}


def _plane_files() -> list[Path]:
    files = sorted(p for p in PLANE.rglob("*.py") if p.name != "__pycache__")
    assert files, "expected the Creative Intelligence Plane package to exist"
    return files


def _imports(path: Path) -> set[str]:
    """Top-level module names a file imports (stdlib-style boundary reading)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _full_imports(path: Path) -> set[str]:
    """Fully-qualified imported module names, for prefix checks."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_the_plane_package_exists_and_is_not_empty() -> None:
    assert (PLANE / "__init__.py").is_file()
    assert (PLANE / "ir.py").is_file()
    assert (PLANE / "identity.py").is_file()
    assert (PLANE / "errors.py").is_file()


@pytest.mark.parametrize("path", _plane_files(), ids=lambda p: p.name)
def test_no_module_imports_an_execution_or_heavy_dependency(path: Path) -> None:
    imported = _imports(path)
    execution = imported & FORBIDDEN_EXECUTION_MODULES
    heavy = imported & FORBIDDEN_HEAVY_MODULES
    assert not execution, f"{path.name} imports execution machinery: {sorted(execution)}"
    assert not heavy, f"{path.name} imports a heavy dependency: {sorted(heavy)}"


@pytest.mark.parametrize("path", _plane_files(), ids=lambda p: p.name)
def test_the_dependency_surface_is_closed(path: Path) -> None:
    """Stdlib + pydantic + the plane itself, and nothing else."""
    unexpected = _imports(path) - ALLOWED_TOP_LEVEL
    assert not unexpected, f"{path.name} imports {sorted(unexpected)}"


@pytest.mark.parametrize("path", _plane_files(), ids=lambda p: p.name)
def test_the_plane_never_reaches_into_another_ownership_zone(path: Path) -> None:
    """Rule 2: no coupling to the execution substrate, the spine, or storage."""
    offenders = [name for name in _full_imports(path) if name.startswith(FORBIDDEN_NEXUS_PREFIXES)]
    assert not offenders, f"{path.name} imports {sorted(offenders)}"


@pytest.mark.parametrize("path", _plane_files(), ids=lambda p: p.name)
def test_the_plane_only_imports_itself(path: Path) -> None:
    """Any ``nexus_ai_agent`` import must be inside the plane."""
    offenders = [
        name
        for name in _full_imports(path)
        if name.startswith("nexus_ai_agent") and not name.startswith(ALLOWED_NEXUS_PREFIXES)
    ]
    assert not offenders, f"{path.name} imports {sorted(offenders)}"


@pytest.mark.parametrize("path", _plane_files(), ids=lambda p: p.name)
def test_the_plane_never_names_the_execution_authority(path: Path) -> None:
    """No CommandBus, no transaction, no undo, no dispatch -- not even a symbol."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            named.add(node.id)
        elif isinstance(node, ast.Attribute):
            named.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            named.add(node.name)
    hit = named & FORBIDDEN_EXECUTION_NAMES
    assert not hit, f"{path.name} references execution vocabulary: {sorted(hit)}"


def test_no_file_performs_io() -> None:
    """``open``, ``Path.read_text``/``write_text`` and friends are absent."""
    banned_calls = {"open", "read_text", "write_text", "read_bytes", "write_bytes", "mkdir"}
    offenders: list[str] = []
    for path in _plane_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            target = node.func if isinstance(node, ast.Call) else None
            name = None
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
            if name in banned_calls:
                offenders.append(f"{path.name}:{node.lineno}: {name}()")
    assert offenders == [], offenders


def test_the_time_base_matches_the_execution_substrate() -> None:
    """Ratchet across an ownership boundary.

    The plane redeclares ``MICROSECONDS_PER_SECOND`` because rule 2 forbids
    importing ``creative.studio``. A redeclared constant is a promise, so this
    test is what keeps it: if the Canonical Creative Execution Substrate ever
    changes its time base, this fails here instead of the plane silently
    compiling plans in the wrong unit.
    """
    from nexus_ai_agent.creative.intelligence.identity import MICROSECONDS_PER_SECOND
    from nexus_ai_agent.creative.studio.models import (
        MICROSECONDS_PER_SECOND as STUDIO_MICROSECONDS_PER_SECOND,
    )

    assert MICROSECONDS_PER_SECOND == STUDIO_MICROSECONDS_PER_SECOND == 1_000_000


def test_the_ir_schema_identifier_is_versioned() -> None:
    """The version is part of the root identity, so a v2 cannot read as a v1."""
    from nexus_ai_agent.creative.intelligence.identity import IR_VERSION

    assert IR_VERSION == "nexus.creative-ir.v1"


def test_the_public_surface_matches_the_declared_surface() -> None:
    """``__all__`` is the plane's contract with the agents that consume it."""
    import nexus_ai_agent.creative.intelligence as plane

    declared = set(plane.__all__)
    assert declared, "the plane must declare a public surface"
    missing = [name for name in declared if not hasattr(plane, name)]
    assert not missing, f"__all__ names that do not exist: {sorted(missing)}"

    exported = {
        name for name in vars(plane) if not name.startswith("_") and name not in {"annotations"}
    }
    # modules are implementation detail; only re-exported symbols are contract
    exported -= {"errors", "identity", "ir"}
    assert exported <= declared, f"exported but undeclared: {sorted(exported - declared)}"


def test_every_typed_error_is_a_plane_error() -> None:
    """One catchable root: a caller can never miss a plane failure."""
    import nexus_ai_agent.creative.intelligence.errors as errors

    for name in dir(errors):
        obj = getattr(errors, name)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            assert issubclass(obj, errors.CreativeIRError), name


def test_the_plane_produces_no_plan_or_command_type_yet() -> None:
    """Honest-scope gate: this slice is the IR, not the compiler.

    Recording the absence in a test is what stops the plane from growing an
    execution path quietly. When the compiler slice lands it must update this
    test deliberately -- and a reviewer will see that it did.
    """
    import nexus_ai_agent.creative.intelligence as plane

    for name in ("CompiledPlan", "PlanStep", "CreativeCompiler", "ExecutionPlan"):
        assert not hasattr(plane, name), f"{name} appeared without its own slice"
