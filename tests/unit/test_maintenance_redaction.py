"""F-003: credentials must never leak, and scratch dirs must not survive.

Arena finding F-003: the restore path stored the raw ``target_url`` (password
included) in ``DrillReport.restored["target"]``, logged ``target_url`` verbatim,
put the DSN on ``psql`` argv (world-readable via ``ps``), and created
``nexus_restore_fetch_*`` / ``nexus_restore_stage_*`` directories it never
removed.

This module pins the fix:

* a synthetic poison password appears in **no** evidence — report JSON, log
  lines, or subprocess diagnostics;
* the ``PGPASSFILE`` (mode 0600) is what carries the password, not argv;
* the scratch directory is gone after both a successful and a failing drill.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from nexus_ai_agent.maintenance import restore as restore_module
from nexus_ai_agent.maintenance.redact import (
    redact_dsn,
    redact_text,
    split_dsn_password,
    target_identity,
)

POISON = "NEXUS_SECRET_POISON_92831"


def test_redact_dsn_covers_url_and_keyword_forms() -> None:
    url = f"postgresql://nexus:{POISON}@ep-x.neon.tech/nexusdb?sslmode=require"
    assert POISON not in redact_dsn(url)
    assert "ep-x.neon.tech" in redact_dsn(url)
    assert POISON not in redact_dsn(f"host=h dbname=d password={POISON}")
    assert POISON not in redact_text(f"psql: could not connect to {url}")


def test_target_identity_never_carries_a_credential() -> None:
    url = f"postgresql://nexus:{POISON}@ep-x.neon.tech:5432/nexusdb?sslmode=require"
    identity = target_identity(url)
    assert identity == "ep-x.neon.tech:5432/nexusdb"
    assert POISON not in identity


def test_split_dsn_password_strips_argv_credential() -> None:
    url = f"postgresql://nexus:{POISON}@h:5432/db?sslmode=require"
    safe, password = split_dsn_password(url)
    assert password == POISON
    assert POISON not in safe
    assert "sslmode=require" in safe


def test_passfile_is_mode_0600_and_carries_the_password(tmp_path: Path) -> None:
    url = f"postgresql://nexus:{POISON}@h:5432/db"
    safe, env = restore_module._pg_psql_env(url, tmp_path)
    assert POISON not in safe
    assert POISON not in " ".join(env.get("PGPASSFILE", "").split())
    passfile = Path(env["PGPASSFILE"])
    assert passfile.is_file()
    assert stat.S_IMODE(passfile.stat().st_mode) == 0o600
    assert POISON in passfile.read_text()


def test_drill_report_never_contains_the_poison(tmp_path: Path, monkeypatch) -> None:
    """The report's ``restored`` block must carry the redacted identity only."""
    artifact = tmp_path / "backup.sql"
    artifact.write_text(
        "-- PostgreSQL database dump\nCREATE TABLE public.t (id int);\n"
        "-- PostgreSQL database dump complete\n"
    )

    class _Done:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str = "") -> None:
            self.stdout = stdout

    def _fake_run(argv, **_kwargs):
        assert POISON not in " ".join(argv), "the password reached argv"
        return _Done(stdout="t\n")

    monkeypatch.setattr(restore_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(restore_module.subprocess, "run", _fake_run)

    report = restore_module.DrillReport(mode="apply")
    url = f"postgresql://nexus:{POISON}@h:5432/db"
    restore_module._apply_postgres_restore(artifact, url, report, force=True, scratch=tmp_path)

    blob = json.dumps(report.as_dict())
    assert POISON not in blob, "the raw DSN leaked into DrillReport"
    assert report.restored["target"] == "h:5432/db"


def test_scratch_dir_is_removed_after_a_failing_drill(tmp_path: Path, monkeypatch) -> None:
    """``_ScratchDir`` must clean up when the body raises."""
    created: list[Path] = []
    real_mkdtemp = restore_module.tempfile.mkdtemp

    def _tracking_mkdtemp(*, prefix: str) -> str:
        path = real_mkdtemp(prefix=prefix)
        created.append(Path(path))
        return path

    monkeypatch.setattr(restore_module.tempfile, "mkdtemp", _tracking_mkdtemp)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("stage failure")

    monkeypatch.setattr(restore_module, "_run_drill", _boom)
    monkeypatch.setattr(restore_module, "_guard_target_identity", lambda _url: None)

    with pytest.raises(RuntimeError):
        restore_module.restore_drill(
            settings=None,  # type: ignore[arg-type]
            from_local=tmp_path / "x.sql",
            from_r2_key=None,
            latest=False,
            target=None,
            target_url=None,
            expected_sha256=None,
            apply=False,
            force=False,
            expect_app_schema=False,
        )

    assert created, "the scratch dir was never created"
    assert all(not path.exists() for path in created), (
        f"scratch dirs leaked after a failing drill: {[p for p in created if p.exists()]}"
    )
