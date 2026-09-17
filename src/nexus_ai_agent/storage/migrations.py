"""Alembic dispatch — the canonical schema-upgrade entry point (D5).

Everything that brings a database to the current schema goes through here, so
local SQLite and hosted PostgreSQL/Neon share one code path:

* :func:`run_migrations` — ``alembic upgrade head`` against the URL resolved
  by :func:`nexus_ai_agent.storage.db.resolve_migration_url` (the
  ``NEXUS_DATABASE_URL`` → asyncpg, else SQLite priority from D3).  This is
  what the ``nexus migrate`` CLI command calls, and it works for both backends.
* :func:`ensure_startup_schema` — the *local* startup decision for SQLite:

  - brand-new file or Alembic-managed file → Alembic;
  - pre-Alembic file (tables exist, no ``alembic_version``) → the legacy
    ``create_all_tables`` fallback, so existing users are never forced to
    migrate.

  PostgreSQL stays lazy at startup (the C1 ``_ensure_pg_tables`` stopgap still
  bootstraps it on first use); ``nexus migrate`` is the explicit upgrade path
  for Neon until the stopgap is retired (D7).
"""

from __future__ import annotations

import asyncio
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

# Repository root: src/nexus_ai_agent/storage/migrations.py → 4 levels up.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def build_alembic_config() -> AlembicConfig:
    """Build an Alembic config that discovers this repo's scripts by path."""
    config = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", resolve_migration_url())
    return config


def run_migrations() -> None:
    """Apply all pending Alembic revisions (``upgrade head``).

    Backend-agnostic: the URL comes from :func:`resolve_migration_url`, so the
    same call upgrades a local SQLite file or a Neon Postgres database.
    """
    command.upgrade(build_alembic_config(), "head")


def ensure_startup_schema() -> dict[str, Any]:
    """Prepare the *startup* schema following the Alembic-first order.

    Returns a small report describing what happened::

        {"backend": "sqlite" | "postgresql",
         "source": "alembic" | "create_all" | "none" | "deferred"}

    ``"deferred"`` means PostgreSQL: its schema is bootstrapped lazily by the
    unchanged C1 stopgap; ``nexus migrate`` upgrades it explicitly.

    Note: call this from a *sync* context (the CLI command body).  The legacy
    SQLite fallback opens its own private event loop; do not call it from
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
        asyncio.run(create_all_tables(db_path))
        return {"backend": "sqlite", "source": "create_all"}
    run_migrations()
    return {"backend": "sqlite", "source": "alembic"}
