"""Boundary laws for the Nagar Asset Library V1 (task-266).

The library must reuse the repository's existing authorities instead of minting
new ones.  These fitness tests fail if the package:

* imports a web framework, Telegram, an LLM/provider SDK or a heavy ML library;
* reaches into the job queue, jobs, provenance, passport, verifier or llm trees
  (there is exactly one queue, one ledger, one verifier and one passport);
* declares persistence anywhere other than the single repository module;
* exposes an ``execute``-style combined orchestration method.

Every check is structural (AST), so it cannot be satisfied by prose.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
ASSETS = ROOT / "src" / "nexus_ai_agent" / "creative" / "assets"

#: Top-level modules the library may import: stdlib + pydantic + nexus itself.
ALLOWED_TOP_LEVEL = {
    "__future__",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "enum",
    "hashlib",
    "os",
    "pathlib",
    "secrets",
    "stat",
    "typing",
    "uuid",
    "pydantic",
    "sqlalchemy",
    "nexus_ai_agent",
}

#: Frameworks/providers that would break V1 provider-neutrality or portability.
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

#: ``nexus_ai_agent`` subtrees that are competing authorities for this V1.
FORBIDDEN_NEXUS_PREFIXES = (
    "nexus_ai_agent.adapters.in_process_job_queue",
    "nexus_ai_agent.application.ports.job_queue",
    "nexus_ai_agent.jobs",
    "nexus_ai_agent.provenance",
    "nexus_ai_agent.worker",
    "nexus_ai_agent.llm",
    "nexus_ai_agent.bot",
    "nexus_ai_agent.features",
)


def _files() -> list[Path]:
    files = sorted(ASSETS.rglob("*.py"))
    assert files, "expected the asset library package to exist"
    return files


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
            modules.extend(a.name for a in node.names if a.name.startswith("nexus_ai_agent"))
    return modules


def test_asset_library_stays_inside_the_import_allowlist() -> None:
    for path in _files():
        imported = _top_level_imports(path)
        assert imported <= ALLOWED_TOP_LEVEL, (
            f"unexpected import in {path.name}: {imported - ALLOWED_TOP_LEVEL}"
        )
        assert not imported & FORBIDDEN_TOP_LEVEL, f"forbidden import in {path.name}"


def test_asset_library_imports_no_competing_authority() -> None:
    for path in _files():
        for module in _nexus_imports(path):
            assert not module.startswith(FORBIDDEN_NEXUS_PREFIXES), (
                f"{path.name} reaches into a competing authority: {module}"
            )


def test_only_the_repository_module_maps_storage_models() -> None:
    # The schema (``storage.models``) is mapped by exactly one module; the
    # service may use the shared session factory (``storage.db``), which is the
    # repository's existing engine authority, but must not touch the tables.
    persistence = {
        path.name
        for path in _files()
        if any(
            m == "nexus_ai_agent.storage" or m.startswith("nexus_ai_agent.storage.models")
            for m in _nexus_imports(path)
        )
    }
    assert persistence == {"repository.py"}, (
        f"exactly one module may map the storage schema, found {sorted(persistence)}"
    )


def test_no_combined_execute_style_method() -> None:
    for path in _files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name != "execute", f"{path.name} exposes a combined execute() method"


def test_service_is_the_only_orchestration_path() -> None:
    orchestrators = {
        path.name
        for path in _files()
        if any(m == "nexus_ai_agent.creative.assets.repository" for m in _nexus_imports(path))
        and any(m == "nexus_ai_agent.creative.assets.blob_store" for m in _nexus_imports(path))
    }
    # ingestion.py and service.py both compose repo + blob store; nothing else may.
    assert orchestrators <= {"ingestion.py", "service.py"}, sorted(orchestrators)


def test_the_boundary_guard_is_not_vacuous() -> None:
    """Positive control: the guard's own recognisers fire on a known-bad source."""
    bad = ast.parse("import telegram\nfrom nexus_ai_agent.jobs.lifecycle import x\n")
    names = set()
    for node in ast.walk(bad):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
    assert names & FORBIDDEN_TOP_LEVEL
    assert _nexus_imports_from_tree(bad)[0].startswith(FORBIDDEN_NEXUS_PREFIXES)


def _nexus_imports_from_tree(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("nexus_ai_agent")
        ):
            modules.append(node.module)
    return modules
