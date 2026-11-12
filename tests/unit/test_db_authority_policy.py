"""F-005: one database authority — no ``REQUIRE_POSTGRES=true → SQLite`` bypass.

Arena finding F-005: several composition paths decided their backend from the
raw ``NEXUS_DATABASE_URL`` instead of the canonical policy, so a production
deployment that *required* PostgreSQL but had a blank URL could still end up on
a local SQLite file:

* ``sync_db_path_for`` returned ``settings.db_path`` (a SQLite path) whenever
  ``settings.database_url`` was blank, ignoring ``NEXUS_REQUIRE_POSTGRES``.
* ``get_checkpointer`` and ``create_backup`` consulted ``resolve_database_url``
  (which returns ``None`` for a blank URL) rather than the enforcing policy.

Every path must now go through ``enforce_postgres_policy`` and fail closed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import BackendPolicyError, sync_db_path_for


@pytest.fixture()
def _env(monkeypatch):
    def _apply(**env: str | None) -> None:
        keys = ("NEXUS_DATABASE_URL", "DATABASE_URL", "NEXUS_REQUIRE_POSTGRES", "NEXUS_DB_PATH")
        for key in keys:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            if value is not None:
                monkeypatch.setenv(key, value)
        from nexus_ai_agent.config.settings import get_settings

        get_settings.cache_clear()

    yield _apply
    from nexus_ai_agent.config.settings import get_settings

    get_settings.cache_clear()


def test_sync_db_path_for_fails_closed_when_postgres_is_required(_env) -> None:
    """The named bypass: require-postgres + blank URL must NOT yield SQLite."""
    _env(NEXUS_REQUIRE_POSTGRES="true")
    settings = SimpleNamespace(database_url=None, db_path="/tmp/should-not-be-used.sqlite3")
    with pytest.raises(BackendPolicyError):
        sync_db_path_for(settings)


def test_sync_db_path_for_follows_postgres_when_configured(_env) -> None:
    _env(NEXUS_REQUIRE_POSTGRES="true", NEXUS_DATABASE_URL="postgresql://u:p@h:5432/db")
    settings = SimpleNamespace(database_url="postgresql://u:p@h:5432/db", db_path="/tmp/x.sqlite3")
    assert sync_db_path_for(settings) is None


def test_sync_db_path_for_keeps_sqlite_for_development(_env) -> None:
    _env()  # no policy, no URL → development SQLite is allowed
    settings = SimpleNamespace(database_url=None, db_path="/tmp/dev.sqlite3")
    assert sync_db_path_for(settings) == "/tmp/dev.sqlite3"


def test_sync_database_url_fails_closed_when_postgres_is_required(_env) -> None:
    _env(NEXUS_REQUIRE_POSTGRES="true")
    with pytest.raises(BackendPolicyError):
        db_module.sync_database_url()


def test_create_backup_fails_closed_when_postgres_is_required(_env) -> None:
    """A backup must never silently take a local SQLite copy in production."""
    _env(NEXUS_REQUIRE_POSTGRES="true")
    from nexus_ai_agent.config.settings import get_settings
    from nexus_ai_agent.maintenance.backup import create_backup

    with pytest.raises(BackendPolicyError):
        create_backup(settings=get_settings(), dry_run=True)


def test_selected_backend_reports_postgresql_when_required_but_unconfigured(_env) -> None:
    _env(NEXUS_REQUIRE_POSTGRES="true")
    assert db_module.selected_backend() == "postgresql"


def test_get_checkpointer_fails_closed_when_postgres_is_required(_env) -> None:
    """F-005B: the checkpointer must not bypass the canonical policy.

    With ``NEXUS_REQUIRE_POSTGRES=true`` and no URL, ``get_checkpointer`` must
    raise rather than hand back a local SQLite saver — the exact
    ``REQUIRE_POSTGRES=true → SQLite`` bypass this finding names.
    """
    _env(NEXUS_REQUIRE_POSTGRES="true")
    from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer

    with pytest.raises(BackendPolicyError):
        get_checkpointer("/tmp/should-not-be-used.sqlite")


def test_get_checkpointer_uses_sqlite_for_development(_env, tmp_path) -> None:
    """The policy must not break the ordinary development path."""
    _env()
    from nexus_ai_agent.storage.langgraph_checkpoint import get_checkpointer

    saver = get_checkpointer(str(tmp_path / "dev.sqlite"))
    assert saver is not None
