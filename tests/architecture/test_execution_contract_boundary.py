"""Architecture boundary tests for the NEXUS V1 execution core.

Two laws are enforced here:

1. The Execution Contract is **provider-neutral**.  ``execution/contract.py``
   imports only the standard library plus the two existing Nexus ports it
   projects (the job-status enum and the single business failure taxonomy).  It
   imports no provider SDK, no adapter, no queue, no database, no HTTP client.
2. ``NativeLocalBackend`` creates **no second execution authority**.  It never
   constructs a queue, never opens a database, never creates a table; the one
   queue is injected.

The file also carries the explicit invariant table (I1..I10) mapping each
invariant to the code that enforces it and the test that proves it.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).parents[2]
CONTRACT = ROOT / "src/nexus_ai_agent/execution/contract.py"
STAGING = ROOT / "src/nexus_ai_agent/execution/staging.py"
BACKEND = ROOT / "src/nexus_ai_agent/adapters/native_local_backend.py"

#: Provider SDKs / routing / queue / persistence libraries the pure contract
#: must never depend on (V1 installs none of them for the execution core).
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

#: The only Nexus modules the pure contract may import.
ALLOWED_NEXUS_IMPORTS = frozenset(
    {
        "nexus_ai_agent.application.ports.job_queue",
        "nexus_ai_agent.jobs.failure_semantics",
    }
)


def _module_imports(path: Path) -> tuple[set[str], set[str]]:
    """Return (top-level module roots, full nexus module paths) for a file."""
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


# --------------------------------------------------------------------------- #
# Law 1 — the contract is provider-neutral
# --------------------------------------------------------------------------- #
def test_contract_imports_no_provider_sdk() -> None:
    roots, _ = _module_imports(CONTRACT)
    offending = roots & FORBIDDEN_PROVIDER_SDKS
    assert not offending, f"execution/contract.py imports a forbidden dependency: {offending}"


def test_contract_imports_no_adapter() -> None:
    roots, nexus_paths = _module_imports(CONTRACT)
    assert "adapters" not in roots
    assert not any(path.startswith("nexus_ai_agent.adapters") for path in nexus_paths)


def test_contract_imports_only_sanctioned_nexus_modules() -> None:
    _, nexus_paths = _module_imports(CONTRACT)
    assert nexus_paths <= ALLOWED_NEXUS_IMPORTS, nexus_paths - ALLOWED_NEXUS_IMPORTS


def test_staging_imports_no_provider_sdk() -> None:
    roots, _ = _module_imports(STAGING)
    assert not roots & FORBIDDEN_PROVIDER_SDKS


def test_execution_package_does_not_import_adapters() -> None:
    for path in (ROOT / "src/nexus_ai_agent/execution").rglob("*.py"):
        roots, nexus_paths = _module_imports(path)
        assert "adapters" not in roots, path
        assert not any(p.startswith("nexus_ai_agent.adapters") for p in nexus_paths), path


# --------------------------------------------------------------------------- #
# Law 2 — the backend creates no second authority
# --------------------------------------------------------------------------- #
def test_backend_opens_no_database_and_creates_no_table() -> None:
    roots, _ = _module_imports(BACKEND)
    assert not roots & {"sqlite3", "sqlalchemy", "sqlmodel", "aiosqlite", "asyncpg", "psycopg"}
    source = BACKEND.read_text(encoding="utf-8")
    assert "CREATE TABLE" not in source.upper()


def test_backend_never_constructs_a_queue() -> None:
    source = BACKEND.read_text(encoding="utf-8")
    assert "InProcessJobQueue(" not in source, "the backend must not construct a queue"
    # The queue type is referenced for typing only, never imported at runtime.
    tree = ast.parse(source)
    runtime_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    for node in runtime_imports:
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "in_process_job_queue" not in node.module


def test_backend_does_not_define_a_queue_or_persistence_class() -> None:
    tree = ast.parse(BACKEND.read_text(encoding="utf-8"))
    class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert class_names == {"NativeLocalBackend"}, class_names


# --------------------------------------------------------------------------- #
# Invariant table — each invariant maps to enforcement + a proving test
# --------------------------------------------------------------------------- #
#: invariant -> (enforcing symbol as "module:qualname", proving test node id)
#: Each proof is a *behavioral* test over the real queue/staging, not a mere
#: field/attribute check (the mission's P0-6 requirement).
INVARIANT_ENFORCEMENT: dict[str, tuple[str, str]] = {
    "I1": (
        "nexus_ai_agent.adapters.in_process_job_queue:InProcessJobQueue.enqueue",
        "tests/integration/test_execution_native_backend.py::"
        "test_submit_returns_the_authoritative_identity",
    ),
    "I2": (
        "nexus_ai_agent.adapters.native_local_backend:NativeLocalBackend.observe",
        "tests/integration/test_execution_native_backend.py::"
        "test_provider_retry_never_mints_a_new_nexus_attempt",
    ),
    "I3": (
        "nexus_ai_agent.adapters.in_process_job_queue:InProcessJobQueue._mark_completed",
        "tests/integration/test_execution_native_backend.py::"
        "test_stale_attempt_cannot_complete_after_takeover",
    ),
    "I4": (
        "nexus_ai_agent.adapters.in_process_job_queue:InProcessJobQueue._mark_completed",
        "tests/integration/test_execution_crash_matrix.py::"
        "test_c7_late_stale_worker_is_refused_without_a_success_notice",
    ),
    "I5": (
        "nexus_ai_agent.adapters.native_local_backend:NativeLocalBackend.observe",
        "tests/integration/test_execution_native_backend.py::"
        "test_provider_run_id_never_changes_observed_truth",
    ),
    "I6": (
        "nexus_ai_agent.adapters.in_process_job_queue:InProcessJobQueue._verify_safely",
        "tests/integration/test_execution_native_backend.py::"
        "test_handler_success_without_independent_verification_is_not_job_success",
    ),
    "I7": (
        "nexus_ai_agent.execution.contract:FailureDisposition.is_terminal_business_failure",
        "tests/integration/test_execution_native_backend.py::"
        "test_observe_unknown_job_is_unknown_not_failed",
    ),
    "I8": (
        "nexus_ai_agent.adapters.in_process_job_queue:InProcessJobQueue.enqueue",
        "tests/integration/test_execution_native_backend.py::"
        "test_submit_is_idempotent_on_the_nexus_key",
    ),
    "I9": (
        "nexus_ai_agent.adapters.native_local_backend:NativeLocalBackend.__init__",
        "tests/architecture/test_execution_contract_boundary.py::"
        "test_backend_never_constructs_a_queue",
    ),
    "I10": (
        "nexus_ai_agent.adapters.in_process_job_queue:InProcessJobQueue._notify_completion",
        "tests/integration/test_execution_races.py::"
        "test_success_notification_only_after_the_commit",
    ),
}


def test_every_invariant_maps_to_a_real_enforcement_symbol() -> None:
    for invariant, (symbol, _test) in INVARIANT_ENFORCEMENT.items():
        module_name, _, qualname = symbol.partition(":")
        module = importlib.import_module(module_name)
        target = module
        for attribute in qualname.split("."):
            target = getattr(target, attribute)
        assert target is not None, f"{invariant} enforcement symbol missing: {symbol}"


def test_every_invariant_maps_to_an_existing_test() -> None:
    for invariant, (_symbol, test_id) in INVARIANT_ENFORCEMENT.items():
        relative, _, node = test_id.partition("::")
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert f"def {node}(" in source, f"{invariant} proving test missing: {test_id}"


def test_invariant_table_covers_i1_through_i10() -> None:
    assert set(INVARIANT_ENFORCEMENT) == {f"I{n}" for n in range(1, 11)}
