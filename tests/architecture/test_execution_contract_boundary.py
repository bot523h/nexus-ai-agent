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
from typing import Any

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
        # The dedup semantics live in ``_insert_or_get`` (the ``ON CONFLICT
        # (idempotency_key) DO NOTHING`` insert), which ``enqueue`` delegates to.
        "nexus_ai_agent.adapters.in_process_job_queue:InProcessJobQueue._insert_or_get",
        "tests/integration/test_execution_native_backend.py::"
        "test_submit_is_idempotent_on_the_nexus_key",
    ),
    "I9": (
        "nexus_ai_agent.adapters.native_local_backend:NativeLocalBackend.__init__",
        "tests/architecture/test_execution_contract_boundary.py::"
        "test_backend_never_constructs_a_queue",
    ),
    "I10": (
        # Ordering ("notification strictly after the durable CAS") is enforced
        # in ``_process_job`` at the ``_mark_completed`` call site — the
        # ``_notify_completion`` method is the callee, not the ordering guard.
        "nexus_ai_agent.adapters.in_process_job_queue:InProcessJobQueue._process_job",
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


# --------------------------------------------------------------------------- #
# Invariant -> negative mutation — the AC-5 closure.
#
# Each invariant must be tied to a *named* mutation in the execution-core
# harness (``scripts/execution_core_mutations.py``) whose mutant is applied to
# that invariant's enforcement symbol and whose targeted tests include the
# invariant's proving test.  A mere GREEN proving test is not enough: the
# invariant's guard must be provably *killable*.
# --------------------------------------------------------------------------- #
#: invariant -> mutation probe name in scripts/execution_core_mutations.py
INVARIANT_MUTATION: dict[str, str] = {
    "I1": "M18 enqueue never arms the authoritative job",
    "I2": "M19 observe presents a provider run id as authority",
    "I3": "M1 final completion CAS ignores the attempt fence",
    "I4": "M1 final completion CAS ignores the attempt fence",
    "I5": "M19 observe presents a provider run id as authority",
    "I6": "M20 execution success bypasses independent verification",
    "I7": "M21 any disposition is a terminal business failure",
    "I8": "M25 the dedup insert ignores the durable idempotency key",
    "I9": "M22 backend fabricates a second authority when none is injected",
    "I10": "M6 success notification before the durable commit",
}


def _load_probes() -> dict[str, Any]:
    harness = ROOT / "scripts/execution_core_mutations.py"
    source = harness.read_text(encoding="utf-8")
    namespace: dict[str, Any] = {"__file__": str(harness)}
    # The harness is a script, not a package; exec it far enough to read PROBES.
    head = source.rsplit("def _run_tests", 1)[0]
    exec(compile(head, "execution_core_mutations", "exec"), namespace)
    return {probe.name: probe for probe in namespace["PROBES"]}


def _enclosing_qualname(source: str, anchor: str) -> str | None:
    """Return ``Class.method`` enclosing ``anchor`` in ``source`` (or ``None``).

    A file-level check is not enough: two guards in the same module (e.g. the
    ``_mark_completed`` call site inside ``_process_job`` vs. the
    ``_notify_completion`` callee) share a file, so the mutation must be proven
    to land *inside the enforcement method itself*.
    """
    tree = ast.parse(source)
    index = source.find(anchor)
    if index < 0:
        return None
    line = source.count("\n", 0, index) + 1
    # Two different classes may define a method with the same name, so resolve
    # the *class* that actually contains the enclosing method — never a global
    # name map (which would map a duplicated name to the wrong class).
    best: tuple[str, str] | None = None
    best_lineno = -1
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (
                node.lineno <= line <= (node.end_lineno or node.lineno)
                and node.lineno >= best_lineno
            ):
                best = (cls.name, node.name)
                best_lineno = node.lineno
    if best is None:
        return None
    return f"{best[0]}.{best[1]}"


def test_every_invariant_is_killable_by_a_named_mutation() -> None:
    probes = _load_probes()
    for invariant, mutation_name in INVARIANT_MUTATION.items():
        probe = next(
            (p for name, p in probes.items() if name.startswith(mutation_name)),
            None,
        )
        assert probe is not None, f"{invariant}: mutation not found: {mutation_name}"
        # The mutation must break the invariant's *enforcement symbol*...
        symbol, proving_test = INVARIANT_ENFORCEMENT[invariant]
        module_name, _, qualname = symbol.partition(":")
        enforcing_file = Path(importlib.import_module(module_name).__file__ or "").resolve()
        assert probe.target.resolve() == enforcing_file, (
            f"{invariant}: mutation {mutation_name} targets {probe.target} "
            f"not the enforcement symbol's file {enforcing_file}"
        )
        # ... *inside the enforcement method* (not merely the same file) ...
        enclosing = _enclosing_qualname(probe.target.read_text(encoding="utf-8"), probe.anchor)
        assert enclosing == qualname, (
            f"{invariant}: mutation {mutation_name} anchors at {enclosing} "
            f"but the enforcement symbol is {qualname}"
        )
        # ... and must run the invariant's proving test.
        assert proving_test in probe.tests, (
            f"{invariant}: mutation {mutation_name} does not run {proving_test}"
        )


def test_invariant_mutation_table_covers_i1_through_i10() -> None:
    assert set(INVARIANT_MUTATION) == {f"I{n}" for n in range(1, 11)}
