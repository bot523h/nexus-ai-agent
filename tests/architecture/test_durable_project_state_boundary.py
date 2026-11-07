"""Boundary laws for the durable project/revision lifecycle (task-282).

The lifecycle must reuse the repository's existing authorities instead of
minting new ones.  These fitness tests fail if the package:

* imports a web framework, Telegram, an LLM/provider SDK or a heavy ML library;
* reaches into the job queue, jobs, provenance, passport, verifier, worker or
  llm trees (there is exactly one queue, one ledger, one verifier, one passport);
* declares database tables anywhere other than the single storage schema;
* maps the storage schema from any module other than the one store adapter;
* exposes an ``execute``-style combined orchestration method.

Every check is structural (AST), so it cannot be satisfied by prose.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
PROJECT_STATE = ROOT / "src" / "nexus_ai_agent" / "creative" / "project_state"

ALLOWED_TOP_LEVEL = {
    "__future__",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "hashlib",
    "json",
    "pathlib",
    "re",
    "typing",
    "pydantic",
    "sqlalchemy",
    "nexus_ai_agent",
}

FORBIDDEN_TOP_LEVEL = {
    "telegram",
    "fastapi",
    "starlette",
    "flask",
    "openai",
    "anthropic",
    "google",
    "boto3",
    "botocore",
    "httpx",
    "requests",
    "torch",
    "transformers",
    "cv2",
    "opencv",
    "onnxruntime",
    "trigger",
    "hatchet",
    "inngest",
    "temporal",
    "celery",
    "redis",
    "kafka",
}

FORBIDDEN_NEXUS_PREFIXES = (
    "nexus_ai_agent.adapters.in_process_job_queue",
    "nexus_ai_agent.application.ports.job_queue",
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.provenance",
    "nexus_ai_agent.worker",
    "nexus_ai_agent.llm",
    "nexus_ai_agent.bot",
    "nexus_ai_agent.api",
)


def _files() -> list[Path]:
    return sorted(PROJECT_STATE.rglob("*.py"))


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


def test_no_dynamic_imports() -> None:
    # A dynamic import (__import__("torch") / importlib.import_module("httpx"))
    # would load a forbidden dependency while the static allowlist check passes.
    # The package has no legitimate reason to import dynamically, so refuse it.
    for path in _files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "__import__", (
                    f"{path.name} uses __import__ (dynamic imports evade the boundary check)"
                )
            if isinstance(node, ast.Attribute) and node.attr == "import_module":
                assert not (isinstance(node.value, ast.Name) and node.value.id == "importlib"), (
                    f"{path.name} uses importlib.import_module (dynamic import)"
                )


def test_package_exists() -> None:
    assert _files(), "expected the durable project_state package to exist"


def test_imports_stay_within_the_allowlist() -> None:
    for path in _files():
        imports = _top_level_imports(path)
        assert imports <= ALLOWED_TOP_LEVEL, (
            f"{path.relative_to(ROOT)} imports outside the allowlist: {sorted(imports)}"
        )
        heavy = imports & FORBIDDEN_TOP_LEVEL
        assert not heavy, f"{path.relative_to(ROOT)} imports heavy/provider deps: {sorted(heavy)}"


def test_imports_no_competing_authority() -> None:
    for path in _files():
        for module in _nexus_imports(path):
            assert not module.startswith(FORBIDDEN_NEXUS_PREFIXES), (
                f"{path.name} reaches into a competing authority: {module}"
            )


def test_no_module_declares_database_tables() -> None:
    # The schema is declared exactly once, in storage.models.  A table=True
    # model here would be a second schema authority.
    for path in _files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    assert not (keyword.arg == "table" and keyword.value is not None), (
                        f"{path.name} declares a database table outside storage.models"
                    )


def test_only_the_store_maps_the_storage_schema() -> None:
    # storage.db (the shared engine authority) may be used by the store; the
    # storage.models schema must be mapped by exactly one module.
    persistence = {
        path.name
        for path in _files()
        if any(
            m == "nexus_ai_agent.storage" or m.startswith("nexus_ai_agent.storage.models")
            for m in _nexus_imports(path)
        )
    }
    assert persistence == {"store.py"}, (
        f"exactly one module may map the storage schema, found {sorted(persistence)}"
    )


def test_no_combined_execute_style_method() -> None:
    # The lifecycle persists what the CommandBus already accepted; it must not
    # grow a combined "execute" that bypasses the bus.
    for path in _files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert node.name != "execute", f"{path.name} exposes a combined execute() method"
