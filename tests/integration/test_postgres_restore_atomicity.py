"""F-002: a failed PostgreSQL restore must not destroy pre-existing target data.

Arena finding F-002: ``pg_dump --clean --if-exists`` emits destructive DDL
(``DROP TABLE``) and the restore applied it with ``psql -v ON_ERROR_STOP=1``
but *no* transaction.  A failure part-way through therefore left the target
with some tables dropped and others untouched — a partially destroyed
database, which is worse than no restore at all.

This module proves the fix against a real PostgreSQL cluster:

* a destructive artifact whose failure is injected immediately after the first
  ``DROP TABLE`` leaves the sentinel row **intact** (the ``--single-transaction``
  rollback), and
* the full generated NEXUS dump is compatible with ``--single-transaction``
  (PG17 ``\\restrict``/``\\unrestrict`` trailers included).

Removing ``--single-transaction`` from ``restore._apply_postgres_restore`` turns
the first test RED — it is the regression that binds the guarantee.

Gating matches the other PostgreSQL-writing modules (``PG_WRITING_MODULES`` in
``tests/unit/test_pg_integration_safety.py``): it needs ``NEXUS_DATABASE_URL``
and the explicit ``NEXUS_PG_TEST_DESTRUCTIVE=1`` opt-in, and every object it
creates is a per-run-nonce-named disposable database it drops afterwards.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from nexus_ai_agent.maintenance import restore as restore_module

DESTRUCTIVE_OPT_IN = "NEXUS_PG_TEST_DESTRUCTIVE"

pytestmark = [
    pytest.mark.skipif(
        not os.getenv("NEXUS_DATABASE_URL"),
        reason="requires PostgreSQL (NEXUS_DATABASE_URL)",
    ),
    pytest.mark.skipif(
        os.getenv(DESTRUCTIVE_OPT_IN) != "1",
        reason=f"refuses to write to NEXUS_DATABASE_URL without {DESTRUCTIVE_OPT_IN}=1",
    ),
]

if os.getenv(DESTRUCTIVE_OPT_IN) == "1" and not os.getenv("NEXUS_DATABASE_URL"):
    pytest.fail(
        f"{DESTRUCTIVE_OPT_IN}=1 is set but NEXUS_DATABASE_URL is not: this module "
        "would collect as skipped, so the live restore-atomicity proof it exists "
        "to provide would silently disappear — a misconfigured run must be red, "
        "never a green skip"
    )


def _psql() -> str:
    psql = shutil.which("psql")
    if psql is None:
        pytest.skip("psql not on PATH (install postgresql-client)")
    return psql


def _run(psql: str, url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test-controlled argv
        [psql, "-v", "ON_ERROR_STOP=1", "--dbname", url, *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture()
def disposable_target() -> str:
    """A per-run disposable database on the configured cluster.

    The admin URL is derived from ``NEXUS_DATABASE_URL`` by swapping the path for
    ``postgres``; the disposable database is named with a fresh nonce and dropped
    in teardown, so nothing outside this run is touched.
    """
    from sqlalchemy.engine import make_url

    psql = _psql()
    admin = make_url(os.environ["NEXUS_DATABASE_URL"]).set(database="postgres")
    admin_url = admin.render_as_string(hide_password=False)
    name = f"nexus_f002_{secrets.token_hex(6)}"
    target = admin.set(database=name).render_as_string(hide_password=False)

    created = _run(psql, admin_url, "-c", f'CREATE DATABASE "{name}"')
    if created.returncode != 0:
        pytest.skip(f"cannot create disposable database: {created.stderr.strip()[:200]}")
    try:
        yield target
    finally:
        _run(psql, admin_url, "-c", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_failed_restore_leaves_target_intact(disposable_target: str) -> None:
    """The sentinel row survives a restore that fails mid-apply.

    Without ``--single-transaction`` the first ``DROP TABLE`` commits and the
    sentinel is gone; with it, the failure rolls the whole artifact back.
    """
    psql = _psql()
    # Seed a sentinel table that the artifact's first statement drops.
    seed = _run(
        psql,
        disposable_target,
        "-c",
        "CREATE TABLE sentinel_tbl(id int primary key, title text)",
        "-c",
        "INSERT INTO sentinel_tbl VALUES (999,'SENTINEL-MUST-SURVIVE')",
    )
    assert seed.returncode == 0, seed.stderr

    artifact = Path(tempfile.mkdtemp(prefix="nexus_f002_artifact_")) / "min_destructive.sql"
    artifact.write_text(
        "DROP TABLE IF EXISTS sentinel_tbl;\n"
        "CREATE TABLE sentinel_tbl(id int primary key, title text);\n"
        "INSERT INTO sentinel_tbl VALUES (1,'restored');\n"
        "INSERT INTO __definitely_missing_table__ VALUES (1);\n"
    )

    report = restore_module.DrillReport(mode="apply")
    scratch = Path(tempfile.mkdtemp(prefix="nexus_f002_scratch_"))
    with pytest.raises(restore_module.RestoreVerificationError):
        restore_module._apply_postgres_restore(
            artifact, disposable_target, report, force=True, scratch=scratch
        )

    # The apply failed — RESTORE_PROVEN must never have been recorded.
    assert all(step["state"] != "RESTORE_PROVEN" for step in report.chain)

    # The pre-existing data is still there: the transaction rolled back.
    after = _run(psql, disposable_target, "-tAc", "SELECT title FROM sentinel_tbl WHERE id=999")
    assert after.returncode == 0, after.stderr
    assert after.stdout.strip() == "SENTINEL-MUST-SURVIVE", (
        "the failed restore destroyed pre-existing target data — "
        "--single-transaction is not in force"
    )


def test_full_nexus_dump_is_transaction_compatible(disposable_target: str) -> None:
    """A real ``pg_dump`` of the live database applies under ``-1``.

    This is the F-002 requirement to *prove* the complete generated NEXUS dump
    (PG17 trailers included) is safe under ``--single-transaction`` rather than
    assuming it.
    """
    psql = _psql()
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        pytest.skip("pg_dump not on PATH (install postgresql-client)")

    dump_dir = Path(tempfile.mkdtemp(prefix="nexus_f002_dump_"))
    dump = dump_dir / "nexus.sql"
    proc = subprocess.run(  # noqa: S603 - test-controlled argv
        [
            pg_dump,
            "--no-owner",
            "--clean",
            "--if-exists",
            "--file",
            str(dump),
            os.environ["NEXUS_DATABASE_URL"],
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[:400]

    applied = subprocess.run(  # noqa: S603 - test-controlled argv
        [
            psql,
            "-v",
            "ON_ERROR_STOP=1",
            "--single-transaction",
            "--dbname",
            disposable_target,
            "--file",
            str(dump),
        ],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert applied.returncode == 0, (
        "the full NEXUS dump is not compatible with --single-transaction: "
        + applied.stderr.strip()[:400]
    )
