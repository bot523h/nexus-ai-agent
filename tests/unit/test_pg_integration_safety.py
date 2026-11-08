"""The PostgreSQL integration module must be safe to aim at a real database.

The 2026-10-17 review of PR #219 found ``tests/integration/
test_postgres_single_engine.py`` opening with an unscoped

    DELETE FROM forcejoinconfig WHERE enabled IS true

— no nonce filter, running against whatever ``NEXUS_DATABASE_URL`` names,
which is the same variable an operator points at the real (Neon) database.
CI happens to aim it at a disposable service container; a developer's machine
does not, and the old module docstring offered "a local server" as a
supported target.

These tests need no database: they read the integration module and the CI
workflow, so they run in the default suite and fail the build the moment the
guarantee is weakened.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_MODULE = REPO_ROOT / "tests" / "integration" / "test_postgres_single_engine.py"
#: Every module that writes to the ``NEXUS_DATABASE_URL`` database must obey the
#: same data-safety contract: an explicit destructive opt-in and only
#: nonce-scoped writes.  The fence module (task-282 slice-2 F1) is the second
#: such module.
PG_WRITING_MODULES = (
    INTEGRATION_MODULE,
    REPO_ROOT / "tests" / "integration" / "test_artifact_binding_postgres_concurrency.py",
)
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Columns every scoped ``DELETE`` must filter on: the per-run nonce ids a
#: module itself generated (a chat id, the nonce used as a ``telegram_id``, or
#: the project id a durable-state module scopes every row to).
SCOPED_COLUMNS = ("chat_id", "telegram_id", "project_id")


def _sql_literals(source: str) -> list[str]:
    """Every string literal passed to SQLAlchemy's ``text(...)`` in *source*.

    An AST walk — not a regex over the whole file — so the module's docstring
    may quote the offending statement without tripping the guard.
    """
    literals: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name != "text":
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                literals.append(argument.value)
    return literals


def test_every_delete_in_the_integration_module_is_scoped_to_its_own_rows() -> None:
    for module in PG_WRITING_MODULES:
        deletes = [
            sql
            for sql in _sql_literals(module.read_text(encoding="utf-8"))
            if re.search(r"\bDELETE\b", sql, re.IGNORECASE)
        ]
        assert deletes, (
            f"{module.name}: the guard found no DELETE at all — it is no longer testing anything"
        )
        offenders: list[str] = []
        for sql in deletes:
            if not re.search(r"\bWHERE\b", sql, re.IGNORECASE):
                offenders.append(f"no WHERE clause: {sql!r}")
            elif not any(column in sql for column in SCOPED_COLUMNS):
                offenders.append(f"WHERE does not name a per-run nonce column: {sql!r}")
        assert not offenders, (
            f"an unscoped delete in {module.name} can destroy another row's data:\n"
            + "\n".join(offenders)
        )


def test_the_integration_module_issues_no_ddl_or_table_wide_statements() -> None:
    for module in PG_WRITING_MODULES:
        source = module.read_text(encoding="utf-8")
        offenders = [
            sql
            for sql in _sql_literals(source)
            if re.search(r"\b(DROP|TRUNCATE|ALTER|UPDATE)\b", sql, re.IGNORECASE)
        ]
        assert offenders == [], f"{module.name}:\n" + "\n".join(offenders)


def test_every_pg_writing_module_requires_the_destructive_opt_in() -> None:
    """Every module that writes to the URL database arms the same opt-in."""
    for module in PG_WRITING_MODULES:
        loaded = _load_module(module)
        assert getattr(loaded, "DESTRUCTIVE_OPT_IN", None) == "NEXUS_PG_TEST_DESTRUCTIVE", (
            f"{module.name} must name the destructive opt-in variable"
        )
        guards = _module_level_skip_guards(module)
        assert guards, f"{module.name} carries no module-level skip guards"
        assert any("DESTRUCTIVE_OPT_IN" in guard for guard in guards), (
            f"{module.name}: NEXUS_DATABASE_URL alone must not enable the module"
        )


def _module_level_skip_guards(module: Path = INTEGRATION_MODULE) -> list[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return [
        ast.unparse(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(child, ast.Call) and getattr(child.func, "attr", "") == "skipif"
            for child in ast.walk(node.value)
        )
    ]


def _load_module(module_path: Path = INTEGRATION_MODULE) -> ModuleType:
    """Import the integration module fresh, so ``pytestmark`` is re-evaluated."""
    name = "_nexus_pg_probe_" + module_path.stem
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_the_integration_module_is_skipped_without_the_destructive_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is behavioural, not decorative: prove it disarms the module.

    ``NEXUS_DATABASE_URL`` alone points the *application* at a database; it
    must not by itself arm a module that writes to it.
    """
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@db.example.invalid:5432/nexus")
    monkeypatch.delenv("NEXUS_PG_TEST_DESTRUCTIVE", raising=False)

    module = _load_module()
    fired = [
        bool(mark.args[0])
        for mark in module.pytestmark
        if getattr(mark, "name", "") == "skipif" and mark.args
    ]
    assert any(fired), "no skip guard fired with the destructive opt-in unset"


def test_the_opt_in_armed_without_a_database_is_a_red_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An armed opt-in must never degrade into a silent skip.

    If ``NEXUS_PG_TEST_DESTRUCTIVE=1`` is set but no database is configured,
    every test in the module would collect as "skipped": the build reports
    green while the live PostgreSQL proof — the reason the module exists —
    goes unverified. That is the exact failure mode the opt-in was added to
    prevent, so a misconfigured run has to be *red*.
    """
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUS_PG_TEST_DESTRUCTIVE", "1")

    # ``pytest.fail`` raises ``Failed``, which derives from BaseException.
    with pytest.raises(BaseException) as excinfo:
        _load_module()
    assert "NEXUS_DATABASE_URL" in str(excinfo.value), (
        "the module must say which variable is missing instead of skipping quietly"
    )


def test_the_module_runs_when_both_variables_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """… and the same guard must not have disabled it permanently."""
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@db.example.invalid:5432/nexus")
    monkeypatch.setenv("NEXUS_PG_TEST_DESTRUCTIVE", "1")

    module = _load_module()
    fired = [
        bool(mark.args[0])
        for mark in module.pytestmark
        if getattr(mark, "name", "") == "skipif" and mark.args
    ]
    assert not any(fired), "the module skips even with both variables set"


def _migrate_postgres_job() -> dict[str, Any]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["migrate-postgres"]
    assert isinstance(job, dict)
    return job


def test_the_ci_postgres_job_sets_the_destructive_opt_in() -> None:
    """A silent skip is not coverage: CI must actually run the module."""
    env = _migrate_postgres_job().get("env") or {}
    assert env.get("NEXUS_PG_TEST_DESTRUCTIVE") == "1", (
        "the migrate-postgres job runs the module without the opt-in, so it "
        "would skip and the live PostgreSQL proof would silently disappear"
    )


def test_every_pg_writing_module_is_actually_run_by_ci() -> None:
    """A module registered as a writer but never invoked is a silent hole."""
    runs = " ".join(str(step.get("run", "")) for step in _migrate_postgres_job().get("steps", []))
    missing = [module.name for module in PG_WRITING_MODULES if module.name not in runs]
    assert missing == [], (
        "these PostgreSQL-writing modules are never run by the migrate-postgres "
        f"job, so their live proof is silently absent: {missing}"
    )


def test_the_ci_postgres_job_still_uses_a_disposable_service_container() -> None:
    job = _migrate_postgres_job()
    assert "postgres" in (job.get("services") or {}), (
        "the job no longer runs a disposable PostgreSQL service container"
    )
    assert "NEXUS_DATABASE_URL" in (job.get("env") or {})
