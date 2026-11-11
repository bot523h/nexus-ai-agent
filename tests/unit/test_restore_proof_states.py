"""F-007B/C/D/E: the restore proof must mean exactly what it claims.

Arena finding F-007 named four more holes beyond table detection:

* **B** ``--expect-app-schema`` was silently ignored on the PostgreSQL path —
  the flag only worked for SQLite, so a PG drill returned green without ever
  checking the application schema.
* **C** an artifact that creates *no* tables passed application-schema
  validation (the check iterated the empty intersection).
* **D** a sidecar containing ``{}`` produced ``CHECKSUM_VERIFIED``: nothing was
  compared, yet the artifact was labelled checksum-verified.
* **E** the state machine conflated "artifact verified" with "checksum matched"
  and reported ``RESTORE_PROVEN`` after proving only the *schema*, never the
  data.

Each test below fails on the pre-fix code and passes after it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_ai_agent.maintenance import restore as restore_module
from nexus_ai_agent.maintenance.backup import _sha256
from nexus_ai_agent.maintenance.dump_sql import copy_row_counts
from nexus_ai_agent.maintenance.restore import DrillReport, RestoreVerificationError


def _artifact(tmp_path: Path, body: str = "CREATE TABLE public.t (id int);\n") -> Path:
    path = tmp_path / "backup.sql"
    path.write_text(
        "-- PostgreSQL database dump\n" + body + "-- PostgreSQL database dump complete\n"
    )
    return path


def _manifest(artifact: Path, **overrides) -> dict:
    base = {
        "kind": "nexus.db-backup.meta/v1",
        "key": f"backups/db/20250101-000000/{artifact.name}",
        "sha256": _sha256(artifact),
        "size_bytes": artifact.stat().st_size,
        "timestamp": "20250101-000000",
        "source": "postgresql (pg_dump)",
        "verification": {"tables": {"t": 1}},
    }
    base.update(overrides)
    return base


# ── D: sidecar must be a usable manifest ────────────────────────────────


def test_empty_sidecar_object_is_not_checksum_verified(tmp_path: Path) -> None:
    """``{}`` compares nothing — it must never yield CHECKSUM_VERIFIED."""
    artifact = _artifact(tmp_path)
    sidecar = tmp_path / "backup.sql.meta.json"
    sidecar.write_text("{}")
    report = DrillReport(mode="drill")
    with pytest.raises(RestoreVerificationError, match="not a usable checksum record"):
        restore_module._verify_checksum(artifact, report, expected_sha256=None, sidecar=sidecar)
    assert all(s["state"] != "CHECKSUM_VERIFIED" for s in report.chain)


@pytest.mark.parametrize(
    "override",
    [
        {"sha256": "not-a-hash"},
        {"sha256": "F" * 64},  # uppercase is not the format backup writes
        {"sha256": "f" * 63},  # short
        {"size_bytes": 0},
        {"size_bytes": "1234"},
        {"size_bytes": True},
        {"key": ""},
        {"timestamp": ""},
        {"source": ""},
        {"kind": "something-else"},
    ],
)
def test_malformed_sidecar_fields_are_rejected(tmp_path: Path, override: dict) -> None:
    artifact = _artifact(tmp_path)
    sidecar = tmp_path / "backup.sql.meta.json"
    sidecar.write_text(json.dumps(_manifest(artifact, **override)))
    report = DrillReport(mode="drill")
    with pytest.raises(RestoreVerificationError, match="not a usable checksum record"):
        restore_module._verify_checksum(artifact, report, expected_sha256=None, sidecar=sidecar)


def test_well_formed_sidecar_still_verifies(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    sidecar = tmp_path / "backup.sql.meta.json"
    sidecar.write_text(json.dumps(_manifest(artifact)))
    report = DrillReport(mode="drill")
    restore_module._verify_checksum(artifact, report, expected_sha256=None, sidecar=sidecar)
    assert report.chain[0]["state"] == "CHECKSUM_VERIFIED"


def test_sidecar_sha_mismatch_is_still_corruption(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    sidecar = tmp_path / "backup.sql.meta.json"
    sidecar.write_text(json.dumps(_manifest(artifact, sha256="a" * 64)))
    report = DrillReport(mode="drill")
    with pytest.raises(RestoreVerificationError, match="MISMATCH"):
        restore_module._verify_checksum(artifact, report, expected_sha256=None, sidecar=sidecar)


# ── B / C: expect_app_schema on the PostgreSQL path ─────────────────────


class _Done:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _fake_psql(monkeypatch, *, tables: str, counts: str = "") -> None:
    def _run(argv, **_kwargs):
        if "-tAc" in argv:
            return _Done(stdout=tables)
        if "-tAF" in argv:
            return _Done(stdout=counts)
        return _Done()

    monkeypatch.setattr(restore_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(restore_module.subprocess, "run", _run)


def test_pg_expect_app_schema_missing_app_table_is_refused(tmp_path, monkeypatch) -> None:
    """B: the flag must bite on PostgreSQL, not only SQLite."""
    artifact = _artifact(tmp_path)
    # The target has the dump's table but is missing the application's tables.
    _fake_psql(monkeypatch, tables="t\n")
    report = DrillReport(mode="apply")
    with pytest.raises(RestoreVerificationError, match="missing application tables"):
        restore_module._apply_postgres_restore(
            artifact,
            "postgresql://u:p@h/db",
            report,
            force=True,
            scratch=tmp_path,
            expect_app_schema=True,
        )


def test_pg_without_expect_app_schema_does_not_require_app_tables(tmp_path, monkeypatch) -> None:
    artifact = _artifact(tmp_path)
    _fake_psql(monkeypatch, tables="t\n")
    report = DrillReport(mode="apply")
    restore_module._apply_postgres_restore(
        artifact, "postgresql://u:p@h/db", report, force=True, scratch=tmp_path
    )
    assert all(s["state"] != "APP_SCHEMA_READABLE" for s in report.chain)


def test_pg_empty_schema_cannot_pass_app_schema_validation(tmp_path, monkeypatch) -> None:
    """C: an artifact that creates no tables must not pass app-schema."""
    artifact = tmp_path / "backup.sql"
    artifact.write_text("-- PostgreSQL database dump\n-- PostgreSQL database dump complete\n")
    _fake_psql(monkeypatch, tables="")
    report = DrillReport(mode="apply")
    with pytest.raises(RestoreVerificationError, match="creates no tables"):
        restore_module._apply_postgres_restore(
            artifact,
            "postgresql://u:p@h/db",
            report,
            force=True,
            scratch=tmp_path,
            expect_app_schema=True,
        )


# ── E: data, not only schema ────────────────────────────────────────────


def test_copy_row_counts_reads_per_table(tmp_path: Path) -> None:
    artifact = tmp_path / "dump.sql"
    artifact.write_text(
        "COPY public.jobs (id) FROM stdin;\n1\n2\n3\n\\.\n"
        "COPY public.users (id) FROM stdin;\n1\n\\.\n"
        "COPY public.empty (id) FROM stdin;\n\\.\n"
    )
    counts, truncated = copy_row_counts(artifact)
    assert counts == {"jobs": 3, "users": 1, "empty": 0}
    assert truncated is False


def test_pg_data_row_mismatch_is_a_verification_error(tmp_path, monkeypatch) -> None:
    """A schema that landed with no data must not be RESTORE_PROVEN."""
    artifact = tmp_path / "backup.sql"
    artifact.write_text(
        "-- PostgreSQL database dump\n"
        "CREATE TABLE public.t (id int);\n"
        "COPY public.t (id) FROM stdin;\n1\n2\n\\.\n"
        "-- PostgreSQL database dump complete\n"
    )
    # Target reports the table but only one row instead of two.
    _fake_psql(monkeypatch, tables="t\n", counts="t\t1\n")
    report = DrillReport(mode="apply")
    with pytest.raises(RestoreVerificationError, match="row counts do not match"):
        restore_module._apply_postgres_restore(
            artifact, "postgresql://u:p@h/db", report, force=True, scratch=tmp_path
        )


def test_pg_matching_row_counts_record_post_restore_data(tmp_path, monkeypatch) -> None:
    artifact = tmp_path / "backup.sql"
    artifact.write_text(
        "-- PostgreSQL database dump\n"
        "CREATE TABLE public.t (id int);\n"
        "COPY public.t (id) FROM stdin;\n1\n2\n\\.\n"
        "-- PostgreSQL database dump complete\n"
    )
    _fake_psql(monkeypatch, tables="t\n", counts="t\t2\n")
    report = DrillReport(mode="apply")
    restore_module._apply_postgres_restore(
        artifact, "postgresql://u:p@h/db", report, force=True, scratch=tmp_path
    )
    data_step = next(s for s in report.chain if s["state"] == "POST_RESTORE_DATA")
    assert data_step["ok"] is True


def test_row_count_reader_quotes_identifiers_against_injection(tmp_path, monkeypatch) -> None:
    """A crafted table name must not break out of the count query."""
    captured: dict[str, list[str]] = {}

    def _run(argv, **_kwargs):
        captured["argv"] = list(argv)
        return _Done(stdout='evil"; DROP TABLE x; --\t0\n')

    monkeypatch.setattr(restore_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(restore_module.subprocess, "run", _run)
    counts = restore_module._list_postgres_row_counts(
        "/usr/bin/psql", "postgresql://u:p@h/db", ['evil"; DROP TABLE x; --']
    )
    query = captured["argv"][captured["argv"].index("-c") + 1]
    # The identifier is doubled-quoted, so the embedded quote is escaped.
    assert '""' in query
    assert counts == {'evil"; DROP TABLE x; --': 0}


def test_artifact_verified_state_is_distinct(tmp_path: Path, monkeypatch) -> None:
    """E: ARTIFACT_VERIFIED and POST_RESTORE_DATA are real, recorded states."""
    from nexus_ai_agent.maintenance.restore import restore_drill
    from tests.unit.test_restore_drill import _settings, _sqlite_artifact

    artifact = _sqlite_artifact(tmp_path / "backup.sqlite3")
    report = restore_drill(settings=_settings(tmp_path, monkeypatch), from_local=artifact)
    states = [s["state"] for s in report.chain]
    assert "ARTIFACT_VERIFIED" in states
    assert states.index("ARTIFACT_VERIFIED") > states.index("INTEGRITY_VERIFIED")
    assert states[-1] == "RESTORE_PROVEN"
