"""Backup verifiability (task-167, P0-C).

Reproduced on main: ``create_backup()`` declared success the moment the
upload returned, with no integrity evidence at all — a truncated, corrupt
or wrong-key artifact reported ``uploaded: True`` identically to a healthy
one, and nothing recorded *when* the successful run happened (so "nightly
backup" status was a silent-failure blindspot: the scheduled workflow could
fail 3/3 runs while the docs claimed nightly backups exist).

These tests pin the release-level contract:

* artifact exists AND non-empty AND sha256-measured,
* integrity verified *by round-trip*: re-download into an isolated temp
  SQLite DB and run ``PRAGMATIC integrity_check`` + table inventory
  (empty/trivial dumps can never pass),
* failure ⇒ non-zero and truthfully flagged (never mislabeled success),
* a SUCCESS summary carries a timestamp.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.maintenance.backup import create_backup


@pytest.fixture(autouse=True)
def _uncached_settings():
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


class _FakeR2:
    """In-memory R2 provider: stores objects, can be told to corrupt them."""

    name = "r2"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.configured = True
        self.corrupt_on_upload: bytes | None = None

    def is_configured(self) -> bool:
        return self.configured

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        data = local_path.read_bytes()
        if self.corrupt_on_upload is not None:
            data = self.corrupt_on_upload
        self.objects[remote_key] = data

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        local_path.write_bytes(self.objects[remote_key])


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_rows: int = 3) -> Any:
    from nexus_ai_agent.config.settings import Settings

    db = tmp_path / "app.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, payload TEXT)")
        for i in range(db_rows):
            conn.execute("INSERT INTO jobs VALUES (?, ?)", (f"job-{i}", "x" * 128))
        conn.commit()
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    return Settings(db_path=str(db))


def _provider(settings: Any, fake: _FakeR2, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._build_provider", lambda settings: fake)


# ── the core contract: success must be verified, not asserted ────────────────


def test_backup_summary_is_verified_by_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch)
    fake = _FakeR2()
    _provider(settings, fake, monkeypatch)

    summary = create_backup(settings=settings)

    assert summary["uploaded"] is True
    assert summary["verified"] is True, "success without verification is the main bug"
    assert summary["size_bytes"] > 0, "empty artifact must never read as success"
    assert (
        summary["sha256"]
        == hashlib.sha256(
            fake.objects[summary["key"]].read_bytes()
            if isinstance(fake.objects[summary["key"]], Path)
            else fake.objects[summary["key"]]
        ).hexdigest()
    )
    assert summary.get("timestamp"), "success must be timestamped"
    assert summary["verification"]["integrity"] == "ok"
    assert summary["verification"]["tables"]["jobs"] == 3
    assert summary["verification"]["roundtrip"] == "byte-identical"


def test_backup_fails_loudly_when_artifact_is_corrupt_in_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent-success elimination: bytes that come back wrong must hard-fail."""
    settings = _settings(tmp_path, monkeypatch)
    fake = _FakeR2()
    fake.corrupt_on_upload = b"truncated-garbage"
    _provider(settings, fake, monkeypatch)

    with pytest.raises(RuntimeError):
        create_backup(settings=settings)


def test_backup_refuses_official_success_on_empty_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, monkeypatch, db_rows=0)
    # no tables at all → a zero-content backup is never "success"-grade
    db = Path(settings.db_path)
    db.write_bytes(b"")
    fake = _FakeR2()
    _provider(settings, fake, monkeypatch)

    with pytest.raises(RuntimeError):
        create_backup(settings=settings)


def test_unconfigured_provider_still_fails_closed_with_precise_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.storage.providers.base import ProviderUnavailable

    settings = _settings(tmp_path, monkeypatch)
    fake = _FakeR2()
    fake.configured = False
    assert not fake.is_configured()
    _provider(settings, fake, monkeypatch)

    with pytest.raises(ProviderUnavailable, match="R2"):
        create_backup(settings=settings)


def test_postgres_dump_verifies_via_sha256_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pg_dump route: no temp-DB restore, so verification = byte-identical
    round-trip + non-empty + dump-footer marker. No new tooling is imposed —
    the check uses the download primitive the provider already exposes."""
    from nexus_ai_agent.config.settings import Settings

    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@h/db")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    settings = Settings(db_path=str(tmp_path / "unused.sqlite3"))
    fake = _FakeR2()
    _provider(settings, fake, monkeypatch)

    def _fake_pg_dump(url: str, dest: Path) -> None:
        dest.write_bytes(
            b"-- fake pg dump\nCREATE TABLE t(x);\n-- PostgreSQL database dump complete\n"
        )

    monkeypatch.setattr("nexus_ai_agent.maintenance.backup._dump_postgres", _fake_pg_dump)

    summary = create_backup(settings=settings)
    assert summary["verified"] is True
    assert summary["sha256"] == hashlib.sha256(fake.objects[summary["key"]]).hexdigest()
    assert summary["verification"]["roundtrip"] == "byte-identical"


def test_postgres_dump_rejects_truncated_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.config.settings import Settings

    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://u:p@h/db")
    from nexus_ai_agent.config import settings as settings_module

    settings_module.get_settings.cache_clear()
    settings = Settings(db_path=str(tmp_path / "unused.sqlite3"))
    fake = _FakeR2()
    fake.corrupt_on_upload = b"-- fake pg dump\ntruncated"  # no completion marker
    _provider(settings, fake, monkeypatch)

    monkeypatch.setattr(
        "nexus_ai_agent.maintenance.backup._dump_postgres",
        lambda url, dest: dest.write_bytes(
            b"-- fake pg dump\n-- PostgreSQL database dump complete\n"
        ),
    )
    with pytest.raises(RuntimeError):
        create_backup(settings=settings)


def test_postgres_dump_accepts_pg17_restrict_wrapper(tmp_path: Path) -> None:
    """PostgreSQL 17 wraps the dump in ``\\restrict``/``\\unrestrict`` lines.

    The completion footer is therefore a *line* in the file, not its final
    bytes — a real PG17 dump must still verify. (The old ``endswith()`` check
    rejected every PG17 dump as "truncated".)
    """
    from nexus_ai_agent.maintenance.backup import _verify_postgres_dump

    token = "AbCdEf0123456789"
    dump = (
        "--\n-- PostgreSQL database dump\n--\n\n"
        f"\\restrict {token}\n\n"
        "CREATE TABLE t (id int);\n"
        "-- PostgreSQL database dump complete\n--\n\n"
        f"\\unrestrict {token}\n"
    )
    artifact = tmp_path / "pg17.sql"
    artifact.write_text(dump, encoding="utf-8")

    assert _verify_postgres_dump(artifact) == {"engine": "postgres", "footer": "present"}


def test_postgres_dump_without_footer_still_rejected(tmp_path: Path) -> None:
    from nexus_ai_agent.maintenance.backup import _verify_postgres_dump

    artifact = tmp_path / "truncated.sql"
    artifact.write_text("--\n-- PostgreSQL database dump\n--\nCREATE TABLE t (id int);\n")
    with pytest.raises(RuntimeError, match="last meaningful line is not the footer"):
        _verify_postgres_dump(artifact)


def test_postgres_dump_with_footer_in_data_is_still_rejected(tmp_path: Path) -> None:
    """A truncated dump whose *data* contains the footer literal must fail.

    A plain containment check is a false positive: a row value can legitimately
    hold the footer text. Only the footer as the LAST meaningful line proves a
    complete dump.
    """
    from nexus_ai_agent.maintenance.backup import _PG_DUMP_FOOTER, _verify_postgres_dump

    artifact = tmp_path / "poisoned.sql"
    artifact.write_text(
        "-- PostgreSQL database dump\n"
        "\\restrict abc\n"
        "CREATE TABLE t(v text);\n"
        f"INSERT INTO t VALUES ('{_PG_DUMP_FOOTER}');\n"
        "-- truncated before the real completion footer\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="last meaningful line is not the footer"):
        _verify_postgres_dump(artifact)


def test_pg_dump_is_self_contained_for_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dump must re-apply over an existing schema: ``--clean --if-exists``."""
    from nexus_ai_agent.maintenance import backup as backup_module

    captured: dict[str, list[str]] = {}

    class _Done:
        returncode = 0
        stderr = ""

    def _fake_run(argv: list[str], **_kwargs: Any) -> _Done:
        captured["argv"] = list(argv)
        Path(argv[argv.index("--file") + 1]).write_text("-- dump\n", encoding="utf-8")
        return _Done()

    monkeypatch.setattr(backup_module.subprocess, "run", _fake_run)
    monkeypatch.setattr(backup_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    backup_module._dump_postgres("postgresql://u:p@h/db", tmp_path / "out.sql")
    assert "--clean" in captured["argv"]
    assert "--if-exists" in captured["argv"]
