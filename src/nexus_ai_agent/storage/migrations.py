"""Alembic dispatch — the canonical schema-upgrade entry point (D5/D6).

Everything that brings a database to the current schema goes through here, so
local SQLite and hosted PostgreSQL/Neon share one code path:

* :func:`run_migrations` — ``alembic upgrade head`` against the URL resolved
  by :func:`nexus_ai_agent.storage.db.resolve_migration_url` (the
  ``NEXUS_DATABASE_URL`` → asyncpg, else SQLite priority from D3).  This is
  what the ``nexus migrate`` CLI command calls, and it works for both backends.
* :func:`ensure_startup_schema` — the startup decision for SQLite:

  - brand-new or Alembic-managed file → Alembic;
  - pre-Alembic file (tables exist, no ``alembic_version``) → **adopted**:
    the idempotent ``create_all_tables`` brings it to the current schema and
    ``alembic stamp head`` records it as versioned, so existing installs are
    never forced to migrate and never lose data.

  PostgreSQL is prepared lazily via Alembic too (D7 retired the C1
  ``create_all`` stopgap).  ``nexus migrate`` upgrades Neon explicitly;
  ``get_session`` prepares it on first use through the same code path.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig

from nexus_ai_agent.storage.db import (
    create_all_tables,
    decide_sqlite_bootstrap,
    resolve_database_url,
    resolve_migration_url,
)

log = logging.getLogger(__name__)

# Repository root: src/nexus_ai_agent/storage/migrations.py → 4 levels up.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def build_alembic_config() -> AlembicConfig:
    """Build an Alembic config that discovers this repo's scripts by path."""
    config = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", resolve_migration_url())
    return config


def _adopt_legacy_sqlite(db_path: str) -> None:
    """Adopt a pre-Alembic SQLite file safely.

    ``create_all_tables`` (idempotent) first brings the schema up to the
    current ``SQLModel.metadata`` — repairing any tables/indexes introduced
    since the file was last written — and then ``alembic stamp head`` records
    the file as versioned *without* replaying the initial revision (which
    would crash on the pre-existing tables).  Existing data is preserved.
    """
    asyncio.run(create_all_tables(db_path))
    command.stamp(build_alembic_config(), "head")
    log.info("adopted legacy SQLite database and stamped it at head: %s", db_path)


def run_migrations() -> None:
    """Apply all pending Alembic revisions (``upgrade head``).

    Backend-agnostic: the URL comes from :func:`resolve_migration_url`, so the
    same call upgrades a local SQLite file or a Neon Postgres database.

    A pre-Alembic SQLite file is adopted first (create_all + stamp) instead of
    being overwritten by the initial revision, which keeps legacy installs and
    their data intact.
    """
    if resolve_database_url() is None:
        from nexus_ai_agent.config.settings import get_settings

        db_path = str(Path(get_settings().db_path).expanduser())
        if decide_sqlite_bootstrap(db_path) == "create_all":
            _adopt_legacy_sqlite(db_path)
            return
    command.upgrade(build_alembic_config(), "head")


def ensure_startup_schema() -> dict[str, Any]:
    """Prepare the *startup* schema following the Alembic-first order.

    Returns a small report describing what happened::

        {"backend": "sqlite" | "postgresql",
         "source": "alembic" | "legacy_adopted" | "none" | "deferred"}

    ``"deferred"`` means PostgreSQL: ``ensure_startup_schema`` never blocks a
    serverless Postgres host (Neon) that may be idle/scaled-to-zero at bot
    startup.  The schema is prepared lazily by ``get_session`` via the same
    ``run_migrations`` path (D7).

    Note: call this from a *sync* context (the CLI command body).  The legacy
    SQLite adoption opens its own private event loop; do not call it from
    inside a running loop.
    """
    if resolve_database_url() is not None:
        return {"backend": "postgresql", "source": "deferred"}

    from nexus_ai_agent.config.settings import get_settings

    db_path = str(Path(get_settings().db_path).expanduser())
    decision = decide_sqlite_bootstrap(db_path)
    if decision == "nothing":
        return {"backend": "sqlite", "source": "none"}
    if decision == "create_all":
        _adopt_legacy_sqlite(db_path)
        return {"backend": "sqlite", "source": "legacy_adopted"}
    run_migrations()
    return {"backend": "sqlite", "source": "alembic"}
