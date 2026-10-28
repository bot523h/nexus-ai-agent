"""Mechanical enforcement of the NEXUS V1 execution-core boundaries.

Complements ``test_execution_contract_boundary.py`` with the static + runtime
checks the mission requires:

* the whole ``execution`` package is provider-neutral (no provider SDK, no
  adapter, no queue, no database, no HTTP client);
* ``staging.py`` reuses the ONE filesystem security boundary
  (``WorkspaceFilesystem``) instead of defining a second, competing one;
* ``NativeLocalBackend`` owns no persistence, no verifier, no passport, no
  commit authority — it only delegates to the injected queue (I9);
* the backend contract exposes exactly the four verbs and never ``execute``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.adapters.native_local_backend import NativeLocalBackend
from nexus_ai_agent.execution import contract as contract_module
from nexus_ai_agent.execution import staging as staging_module

ROOT = Path(__file__).parents[2]
EXECUTION = ROOT / "src/nexus_ai_agent/execution"
BACKEND = ROOT / "src/nexus_ai_agent/adapters/native_local_backend.py"
STAGING = ROOT / "src/nexus_ai_agent/execution/staging.py"

FORBIDDEN_PROVIDER_SDKS = frozenset(
    {
        "litellm",
        "openai",
        "anthropic",
        "google",
        "cohere",
        "mistralai",
        "boto3",
        "botocore",
        "httpx",
        "requests",
        "aiohttp",
        "telegram",
        "langgraph",
        "langchain",
        "langchain_community",
        "sqlalchemy",
        "sqlmodel",
        "aiosqlite",
        "sqlite3",
        "chromadb",
        "sentence_transformers",
        "llama_cpp",
        "temporalio",
        "hatchet",
        "inngest",
        "windmill",
        "trigger",
        "redis",
        "kafka",
        "celery",
        "asyncpg",
        "psycopg",
    }
)


def _imports(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    nexus_paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
                if alias.name.startswith("nexus_ai_agent"):
                    nexus_paths.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
            if node.module.startswith("nexus_ai_agent"):
                nexus_paths.add(node.module)
    return roots, nexus_paths


def _imported_names(path: Path, module: str) -> set[str]:
    """List imports from one module; ``*`` records a direct module import."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            if any(
                alias.name == module or alias.name.startswith(f"{module}.") for alias in node.names
            ):
                names.add("*")
    return names


# --------------------------------------------------------------------------- #
# Provider neutrality of the whole execution package
# --------------------------------------------------------------------------- #
def test_whole_execution_package_is_provider_neutral() -> None:
    for path in EXECUTION.rglob("*.py"):
        roots, _ = _imports(path)
        offending = roots & FORBIDDEN_PROVIDER_SDKS
        assert not offending, f"{path.name} imports a forbidden dependency: {offending}"


def test_execution_package_imports_no_adapter() -> None:
    for path in EXECUTION.rglob("*.py"):
        roots, nexus_paths = _imports(path)
        assert "adapters" not in roots, path
        assert not any(p.startswith("nexus_ai_agent.adapters") for p in nexus_paths), path


# --------------------------------------------------------------------------- #
# ONE filesystem security boundary
# --------------------------------------------------------------------------- #
def test_staging_reuses_the_workspace_filesystem_boundary() -> None:
    _, nexus_paths = _imports(STAGING)
    assert "nexus_ai_agent.tools.filesystem_policy" in nexus_paths


def test_staging_defines_no_second_filesystem_security_layer() -> None:
    """No raw shutil tree removal and no module-level symlink shim of its own."""
    source = STAGING.read_text(encoding="utf-8")
    assert "import shutil" not in source, "staging must reuse WorkspaceFilesystem.remove_tree"
    assert "shutil.rmtree" not in source
    roots, _ = _imports(STAGING)
    assert "shutil" not in roots


# --------------------------------------------------------------------------- #
# The backend owns no second authority (I9)
# --------------------------------------------------------------------------- #
def test_backend_owns_no_persistence_verifier_or_passport() -> None:
    source = BACKEND.read_text(encoding="utf-8")
    for forbidden in (
        "CREATE TABLE",
        "sqlite3.connect",
        "InProcessJobQueue(",
        "reverify_stored_passport",
        "archive_trim_evidence",
    ):
        assert forbidden not in source, f"backend must not own: {forbidden}"
    _, nexus_paths = _imports(BACKEND)
    # The backend may import the two identity helpers, but the module path is
    # not proof of authority: inspect names so moving the helpers is harmless
    # and a future passport/verifier import fails closed.
    assert not any("verification" in p for p in nexus_paths)
    passport_imports = _imported_names(BACKEND, "nexus_ai_agent.jobs.creative_passport")
    assert passport_imports <= {"attempt_id", "request_identity"}, passport_imports


def test_backend_never_constructs_a_queue_at_runtime() -> None:
    """Runtime construction check: the backend keeps the injected queue only.

    With no queue injected it fails closed rather than manufacturing a second
    authority: a missing queue is a refusal, never a fresh ``InProcessJobQueue``.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        queue = InProcessJobQueue(Path(tmp) / "jobs.sqlite3", artifact_verifiers={})
        backend = NativeLocalBackend(queue)
        assert backend._queue is queue, "the backend must delegate to the injected queue"

    with pytest.raises(ValueError):
        NativeLocalBackend(None)  # type: ignore[arg-type]


def test_backend_defines_only_the_backend_class() -> None:
    tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
    classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert classes == {"NativeLocalBackend"}, classes


# --------------------------------------------------------------------------- #
# Four verbs, never execute
# --------------------------------------------------------------------------- #
def test_contract_protocol_declares_exactly_four_verbs() -> None:
    tree = ast.parse((EXECUTION / "contract.py").read_text(encoding="utf-8"))
    protocol = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ExecutionBackend"
    )
    verbs = {
        node.name
        for node in protocol.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert verbs == {"submit", "observe", "cancel", "reconcile"}, verbs


def test_contract_has_no_execute_verb() -> None:
    assert not hasattr(contract_module.ExecutionBackend, "execute")
    assert not hasattr(staging_module.AttemptStaging, "execute")


def test_native_local_backend_has_no_execute_verb() -> None:
    for name in dir(NativeLocalBackend):
        assert name != "execute"
