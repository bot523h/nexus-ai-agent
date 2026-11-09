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
            reason = _unscoped_delete_reason(sql)
            if reason:
                offenders.append(f"{reason}: {sql!r}")
        assert not offenders, (
            f"an unscoped delete in {module.name} can destroy another row's data:\n"
            + "\n".join(offenders)
        )


#: A scoped delete is a conjunction of equalities that each bind a per-run nonce
#: column to a bound parameter.  Anything else (a top-level ``OR``, a tautology
#: such as ``TRUE``/``1 = 1``, ``IS NOT NULL``, a constant instead of a bound
#: nonce, a subquery) can widen the delete past this run's rows and is refused.
_SCOPED_CONJUNCT = re.compile(r"^\s*(\w+)\s*=\s*:\w+\s*$")


def _where_clause(sql: str) -> str | None:
    match = re.search(r"\bWHERE\b(.*)$", sql, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _unscoped_delete_reason(sql: str) -> str | None:
    """Return why *sql* is not nonce-scoped, or ``None`` when it is safe.

    A substring check for a scoped-column name is not enough: a predicate such
    as ``WHERE project_id = :pid OR TRUE`` names a scoped column yet deletes
    every row.  So each conjunct of the ``WHERE`` must be exactly
    ``<scoped-column> = :<bound-param>`` — no ``OR``, no tautology, no constant
    in place of the per-run nonce.
    """
    where = _where_clause(sql)
    if where is None:
        return "no WHERE clause"
    if re.search(r"\bOR\b", where, re.IGNORECASE):
        return "a top-level OR can widen the delete past this run's rows"
    if re.search(r"\b(TRUE|FALSE|IS\s+NOT\s+NULL)\b", where, re.IGNORECASE):
        return "a tautology in the WHERE can widen the delete"
    conjuncts = re.split(r"\bAND\b", where, flags=re.IGNORECASE)
    scoped = 0
    for conjunct in conjuncts:
        match = _SCOPED_CONJUNCT.match(conjunct)
        if match is None:
            return f"conjunct {conjunct.strip()!r} is not a scoped equality to a bound nonce"
        if match.group(1) in SCOPED_COLUMNS:
            scoped += 1
    if scoped == 0:
        return "no conjunct binds a per-run nonce column"
    return None


def test_the_delete_scope_guard_rejects_widening_predicates() -> None:
    """The guard must reject the evasions it exists to catch, not just ``WHERE``.

    A substring check would accept ``WHERE project_id = :pid OR TRUE`` (it names
    a scoped column) while deleting every project's rows.  Pin the guard's own
    behaviour so a future weakening is caught here.
    """
    unsafe = [
        "DELETE FROM projectartifactbinding",  # no WHERE
        "DELETE FROM t WHERE TRUE",
        "DELETE FROM t WHERE project_id = :pid OR TRUE",
        "DELETE FROM t WHERE project_id = :pid OR 1 = 1",
        "DELETE FROM t WHERE project_id IS NOT NULL",
        "DELETE FROM t WHERE project_id = 'literal'",  # constant, not a bound nonce
        "DELETE FROM t WHERE other_id = :pid",  # not a per-run nonce column
        "DELETE FROM t WHERE project_id IN (SELECT project_id FROM appproject)",
    ]
    for sql in unsafe:
        assert _unscoped_delete_reason(sql) is not None, f"guard accepted an unsafe delete: {sql!r}"

    safe = [
        "DELETE FROM projectartifactbinding WHERE project_id = :pid",
        'DELETE FROM "user" WHERE telegram_id = :id',
        "DELETE FROM t WHERE project_id = :pid AND artifact_id = :aid",
    ]
    for sql in safe:
        assert _unscoped_delete_reason(sql) is None, f"guard rejected a safe delete: {sql!r}"


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
    must not by itself arm a module that writes to it.  Every registered writer
    is loaded under the environment and its skip guards must actually fire.
    """
    for module_path in PG_WRITING_MODULES:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@db.example.invalid:5432/nexus")
        monkeypatch.delenv("NEXUS_PG_TEST_DESTRUCTIVE", raising=False)
        module = _load_module(module_path)
        assert _skip_fired(module), f"{module_path.name} is not skipped without the opt-in"


def test_a_false_like_opt_in_value_does_not_arm_the_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``0``/``false``/``no``/``off`` are truthy strings — they must NOT arm.

    ``os.getenv(...)`` returns the literal text, so ``bool(os.getenv(...))``
    treats ``"0"`` as enabled and would write to whatever ``NEXUS_DATABASE_URL``
    names.  Only the exact string ``"1"`` may arm a writing module.
    """
    for module_path in PG_WRITING_MODULES:
        for value in ("0", "false", "False", "no", "off", "", "yes", "true", "2"):
            monkeypatch.setenv(
                "NEXUS_DATABASE_URL", "postgresql://u:p@db.example.invalid:5432/nexus"
            )
            monkeypatch.setenv("NEXUS_PG_TEST_DESTRUCTIVE", value)
            module = _load_module(module_path)
            assert _skip_fired(module), (
                f"{module_path.name} armed for the destructive opt-in value {value!r}"
            )


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
    for module_path in PG_WRITING_MODULES:
        monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
        monkeypatch.setenv("NEXUS_PG_TEST_DESTRUCTIVE", "1")

        # ``pytest.fail`` raises ``Failed``, which derives from BaseException.
        with pytest.raises(BaseException) as excinfo:
            _load_module(module_path)
        assert "NEXUS_DATABASE_URL" in str(excinfo.value), (
            "the module must say which variable is missing instead of skipping quietly"
        )


def test_the_module_runs_when_both_variables_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """… and the same guard must not have disabled it permanently."""
    for module_path in PG_WRITING_MODULES:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@db.example.invalid:5432/nexus")
        monkeypatch.setenv("NEXUS_PG_TEST_DESTRUCTIVE", "1")

        module = _load_module(module_path)
        assert not _skip_fired(module), f"{module_path.name} skips even with both variables set"


def _skip_fired(module: ModuleType) -> bool:
    """True iff at least one module-level ``skipif`` condition is currently true."""
    return any(
        bool(mark.args[0])
        for mark in module.pytestmark
        if getattr(mark, "name", "") == "skipif" and mark.args
    )


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
