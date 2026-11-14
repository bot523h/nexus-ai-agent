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
#: such module; the persistence/durability module (mission P0) is the third.
PG_WRITING_MODULES = (
    INTEGRATION_MODULE,
    REPO_ROOT / "tests" / "integration" / "test_artifact_binding_postgres_concurrency.py",
    REPO_ROOT / "tests" / "integration" / "test_postgres_persistence.py",
)

#: Modules that exercise PostgreSQL against a **disposable target they create and
#: drop themselves** (a per-run database or schema) rather than writing rows to
#: ``NEXUS_DATABASE_URL``.  They carry the same destructive opt-in, but their
#: safety property is "the configured database is only read, never mutated",
#: so the nonce-scoped-``DELETE`` contract above does not apply.  They still must
#: be *run* by CI (a registered-but-uninvoked proof is a silent hole) and they
#: still may not write to the configured database — both enforced below.
PG_DISPOSABLE_TARGET_MODULES = (
    REPO_ROOT / "tests" / "integration" / "test_postgres_restore_atomicity.py",
)

#: F-011.6: modules that write rows to ``NEXUS_DATABASE_URL`` **through the ORM**
#: rather than through ``text()`` SQL.  They carry the same destructive opt-in
#: and the same per-run-nonce scoping obligation, but their writes are not
#: ``DELETE`` literals, so the text()-based scoped-``DELETE`` guard cannot see
#: them — hence a distinct category with its own guard below.
PG_ORM_WRITING_MODULES = (
    REPO_ROOT / "tests" / "integration" / "test_knowledge_retrieval_postgres.py",
)

#: F-011.6: hybrid modules whose PG leg writes to ``NEXUS_DATABASE_URL`` but whose
#: SQLite leg must keep running everywhere (including the default ``test`` job,
#: which has no database).  They therefore may NOT use a module-level ``skipif``:
#: the guard has to sit on the PG leg itself (a ``pytest.skip`` inside the fixture
#: or a per-test ``skipif``), so that ``NEXUS_DATABASE_URL`` alone never arms a
#: write while the SQLite contract still executes without a database.  Guarded by
#: ``test_hybrid_pg_modules_guard_the_pg_leg_not_the_module`` below.
PG_HYBRID_MODULES = (
    REPO_ROOT / "tests" / "integration" / "test_checkpoint_adapter_contract.py",
    REPO_ROOT / "tests" / "integration" / "test_lifecycle_store_contract.py",
    REPO_ROOT / "tests" / "integration" / "test_checkpoint_composition.py",
)
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: F-011.6: the writer inventory above is hand-maintained, so a new module that
#: writes to ``NEXUS_DATABASE_URL`` — through ``text()`` SQL *or* through the ORM
#: (``.delete()`` / ``.insert()`` / ``.commit()``) — could silently escape every
#: guard.  This scan discovers candidates mechanically and fails when one is not
#: registered in ``PG_WRITING_MODULES`` or ``PG_DISPOSABLE_TARGET_MODULES``.
_INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"
_SQL_WRITE = re.compile(r"\b(DELETE|INSERT|UPDATE|DROP|CREATE|TRUNCATE|ALTER)\b", re.IGNORECASE)
_ORM_WRITE = re.compile(
    r"\.(delete|insert|update|upsert|upsert_ingested|add|add_all|commit|flush)\s*\("
)

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
        # The exact form that reddened CI on PR #325: an ``IN`` list of two
        # projects is not a scoped equality, so it is refused (it names a scoped
        # column yet can widen past this run's rows).
        "DELETE FROM projectartifactbinding WHERE project_id IN (:a, :b)",
        "DELETE FROM artifactcontentowner WHERE artifact_id = :aid",  # no nonce column
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


#: Every table the fence's cleanup touches, with the scoped column it must use.
#: The survival baseline below is checked for ALL of them, not just one: a single
#: table left with an unscoped DELETE is enough to destroy another run's rows.
_FENCE_CLEANUP_TABLES = (
    "projectartifactbinding",
    "artifactcontentowner",
    "projectrevisionhead",
    "projectrevision",
    "appproject",
)


def test_every_fence_cleanup_delete_survives_another_projects_rows() -> None:
    """A survival baseline for EVERY table in the fence cleanup, not just one.

    The 2026-11-11 CI failure reddened on exactly one module and named three
    tables; a guard that only pinned one table would have missed the other two.
    Each DELETE in the fence module must be scoped so that a *different* project's
    row (a fresh nonce, never bound by this run) is provably outside its predicate
    — i.e. the predicate is ``<scoped-column> = :bound`` and nothing wider.
    """
    source = (
        REPO_ROOT / "tests" / "integration" / "test_artifact_binding_postgres_concurrency.py"
    ).read_text(encoding="utf-8")
    deletes = [sql for sql in _sql_literals(source) if re.search(r"\bDELETE\b", sql, re.I)]
    # The module must still clean up every table the fence writes to.
    touched = {
        match.group(1)
        for sql in deletes
        if (match := re.search(r"\bFROM\s+(\w+)", sql, re.IGNORECASE))
    }
    missing = [table for table in _FENCE_CLEANUP_TABLES if table not in touched]
    assert missing == [], f"the fence cleanup no longer deletes from: {missing}"
    # And every one of those DELETEs must be nonce-scoped (a survival baseline).
    offenders = [
        f"{_unscoped_delete_reason(sql)}: {sql!r}"
        for sql in deletes
        if _unscoped_delete_reason(sql)
    ]
    assert offenders == [], (
        "an unscoped fence cleanup DELETE can destroy another run:\n" + "\n".join(offenders)
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


def _writes_to_the_configured_database(source: str) -> bool:
    """True when *source* both reads ``NEXUS_DATABASE_URL`` and mutates rows.

    Reads must be *direct* (``os.environ[...]``/``os.getenv``) rather than merely
    mentioning the variable in prose, so a module that only documents the DSN is
    not misclassified.  A mutation is either a ``text()`` DDL/DML literal or an
    ORM write call.
    """
    reads_dsn = 'os.environ["NEXUS_DATABASE_URL"]' in source or (
        "os.environ['NEXUS_DATABASE_URL']" in source or 'os.getenv("NEXUS_DATABASE_URL"' in source
    )
    if not reads_dsn:
        return False
    if any(_SQL_WRITE.search(sql) for sql in _sql_literals(source)):
        return True
    return bool(_ORM_WRITE.search(source))


def test_every_module_writing_the_configured_database_is_registered() -> None:
    """F-011.6: mechanical discovery of writer modules, not a hand-kept list.

    ``PG_WRITING_MODULES`` is maintained by hand. A new integration module that
    writes to ``NEXUS_DATABASE_URL`` would therefore escape the scoped-DELETE,
    no-DDL, and destructive-opt-in guards without anything going red. This scan
    reads every integration module and fails if a writer is not registered.
    """
    registered = {
        m.name
        for m in PG_WRITING_MODULES
        + PG_DISPOSABLE_TARGET_MODULES
        + PG_ORM_WRITING_MODULES
        + PG_HYBRID_MODULES
    }
    discovered = {
        path.name
        for path in sorted(_INTEGRATION_DIR.glob("test_*.py"))
        if _writes_to_the_configured_database(path.read_text(encoding="utf-8"))
    }
    unregistered = sorted(discovered - registered)
    assert unregistered == [], (
        "these integration modules write to NEXUS_DATABASE_URL but are not in "
        "PG_WRITING_MODULES / PG_DISPOSABLE_TARGET_MODULES / PG_ORM_WRITING_MODULES, "
        f"so no guard covers them: {unregistered}"
    )
    # The scan must not go blind: every registered *direct* writer must still be
    # found.  ``PG_DISPOSABLE_TARGET_MODULES`` is deliberately excluded — those
    # modules create and drop their own throwaway database, so they never write
    # to ``NEXUS_DATABASE_URL`` and the scan is *right* not to match them.
    direct_writers = {
        m.name for m in PG_WRITING_MODULES + PG_ORM_WRITING_MODULES + PG_HYBRID_MODULES
    }
    assert direct_writers <= discovered, (
        "the discovery scan no longer finds every registered direct writer — it "
        f"has gone blind and would miss a new one: {sorted(direct_writers - discovered)}"
    )


def _skip_fired(module: ModuleType) -> bool:
    """True iff at least one module-level ``skipif`` condition is currently true."""
    return any(
        bool(mark.args[0])
        for mark in getattr(module, "pytestmark", [])
        if getattr(mark, "name", "") == "skipif" and mark.args
    )


def _pg_leg_guard_present(module_path: Path) -> bool:
    """True iff ``PG_ENABLED`` is literally ``DSN and opt-in == "1"``.

    A hybrid module keeps its SQLite leg running everywhere, so its opt-in
    cannot be a module-level ``skipif``.  The guard must instead be a conjunction
    that the PG leg keys off — ``PG_ENABLED = bool(PG_URL) and os.getenv(
    "NEXUS_PG_TEST_DESTRUCTIVE") == "1"``.  Requiring the *shape* (an ``and``
    whose operands mention both the DSN and the opt-in) is what makes this
    mutation-sensitive: weakening it to ``bool(PG_URL)`` alone must fail here,
    not merely drop a string.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "PG_ENABLED" for t in node.targets):
            continue
        value = node.value
        if not isinstance(value, ast.BoolOp) or not isinstance(value.op, ast.And):
            return False
        rendered = ast.unparse(value)
        return "PG_URL" in rendered and "DESTRUCTIVE_OPT_IN" in rendered
    return False


def test_hybrid_pg_modules_guard_the_pg_leg_not_the_module() -> None:
    """F-011.6: a hybrid module's PG leg must require the explicit opt-in.

    ``test_checkpoint_adapter_contract``, ``test_lifecycle_store_contract`` and
    ``test_checkpoint_composition`` each write to ``NEXUS_DATABASE_URL`` on
    their PG leg while their SQLite leg runs with no database at all.  They
    cannot use a module-level ``skipif`` (that would disable the SQLite
    contract), so the guard has to sit on the PG leg — and nothing forced it
    to.  Before this test, ``NEXUS_DATABASE_URL`` alone ran their writes, which
    is the exact hazard D-0028 forbids.
    """
    for module_path in PG_HYBRID_MODULES:
        assert _pg_leg_guard_present(module_path), (
            f"{module_path.name} writes to NEXUS_DATABASE_URL on its PG leg but "
            "does not gate that leg on NEXUS_PG_TEST_DESTRUCTIVE=1"
        )
        # …and the SQLite leg must NOT be gated by a module-level skipif, or the
        # default no-database `test` job would silently lose that coverage.
        module = _load_module(module_path)
        assert not _skip_fired(module), (
            f"{module_path.name} carries a module-level skipif; its SQLite leg "
            "would stop running in the database-less `test` job"
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
    registered = PG_WRITING_MODULES + PG_DISPOSABLE_TARGET_MODULES + PG_ORM_WRITING_MODULES
    missing = [module.name for module in registered if module.name not in runs]
    assert missing == [], (
        "these PostgreSQL modules are never run by the migrate-postgres "
        f"job, so their live proof is silently absent: {missing}"
    )


def test_disposable_target_modules_never_write_to_the_configured_database() -> None:
    """A disposable-target module must derive its target, not write to the DSN.

    These modules are allowed to run destructive SQL, but only against a
    database they created and drop themselves.  They must therefore derive that
    target (``make_url(...).set(database=...)``) rather than mutate whatever
    ``NEXUS_DATABASE_URL`` names — otherwise the destructive opt-in would
    authorise damage to a real database.
    """
    problems: list[str] = []
    for module in PG_DISPOSABLE_TARGET_MODULES:
        source = module.read_text(encoding="utf-8")
        if "make_url(" not in source or "set(database=" not in source:
            problems.append(
                f"{module.name}: does not derive a disposable database via "
                "make_url(...).set(database=...)"
            )
        if "NEXUS_DATABASE_URL" not in source:
            problems.append(f"{module.name}: never reads NEXUS_DATABASE_URL (unexpected)")
    assert not problems, "\n".join(problems)


def test_the_ci_postgres_job_still_uses_a_disposable_service_container() -> None:
    job = _migrate_postgres_job()
    assert "postgres" in (job.get("services") or {}), (
        "the job no longer runs a disposable PostgreSQL service container"
    )
    assert "NEXUS_DATABASE_URL" in (job.get("env") or {})
