"""Alembic migration environment for NEXUS AI Agent (async, run_sync pattern).

SQLAlchemy 2.0 / Alembic 1.20 async template, wired to the application's own
URL + metadata resolution:

- The database URL comes from ``resolve_migration_url()`` (D3) — the same
  ``NEXUS_DATABASE_URL`` → asyncpg, else ``sqlite+aiosqlite:///{NEXUS_DB_PATH}``
  priority used by the runtime (never from ``alembic.ini``, which keeps an
  empty ``sqlalchemy.url``).
- The target metadata comes from ``get_target_metadata()`` (D2), the V1 hook
  that merges ``SQLModel.metadata`` with any future metadata owners.
- ``render_as_batch=True`` is enabled only for SQLite, which needs batch mode
  for ALTER statements; PostgreSQL does not need it (and it is slower there).

The application package must be importable (it is installed as part of the
project), the same assumption the stock Alembic template makes about the app
it migrates.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from nexus_ai_agent.storage.db import resolve_migration_url
from nexus_ai_agent.storage.migration_metadata import get_target_metadata

# The Alembic Config object, providing access to the values within the .ini
# file in use.
config = context.config

# Interpret the config file for Python logging.
# ``disable_existing_loggers=False`` is mandatory: the default would silently
# disable every application logger that already exists (e.g. the bot's
# structured loggers) for the rest of the process after a migration runs.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Target metadata for 'autogenerate' support (D2).
target_metadata = get_target_metadata()


def is_sqlite_url(url: str) -> bool:
    """Whether ``url`` targets the SQLite backend (needs batch mode)."""
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine, so no DBAPI is
    required.  Calls to ``context.execute()`` emit SQL to the script output.
    """
    url = resolve_migration_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=is_sqlite_url(url),
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=is_sqlite_url(resolve_migration_url()),
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations through ``run_sync``."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = resolve_migration_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
