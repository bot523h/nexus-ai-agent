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


#: Main option carrying an explicit target URL for this run (see
#: :func:`build_alembic_config`).  ``migrations/env.py`` prefers it over
#: :func:`resolve_migration_url`; it is deliberately *not* ``sqlalchemy.url``,
#: so the documented invariant "the URL never comes from alembic.ini" holds.
URL_OVERRIDE_OPTION = "nexus.url_override"


def build_alembic_config(url_override: str | None = None) -> AlembicConfig:
    """Build an Alembic config that discovers this repo's scripts by path.

    ``url_override`` targets an explicit SQLAlchemy URL instead of the one
    resolved from settings.  D10 needs this so adoption stamps the very
    database it just introspected, and so the unit suite can drive the
    identical code path against a temporary SQLite file (dialect substitution)
    without pretending to be a PostgreSQL host.  When ``None``, the URL comes
    from :func:`~nexus_ai_agent.storage.db.resolve_migration_url` as before.
    """
    config = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url_override or resolve_migration_url())
    if url_override is not None:
        config.set_main_option(URL_OVERRIDE_OPTION, url_override)
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


def _postgres_startup_report() -> dict[str, Any]:
    """Startup decision for PostgreSQL (D10).

    Two properties have to hold at the same time:

    * **Never block startup on a serverless host.**  Neon may be idle or scaled
      to zero when the bot boots, so a probe that cannot connect still returns
      ``"deferred"`` and the schema is prepared lazily by ``get_session`` —
      exactly the pre-D10 behaviour.
    * **Never walk into a known crash.**  When the probe *does* succeed and
      shows an un-stamped database, startup fails here with the actionable D10
      error instead of dying later inside ``op.create_table`` with an opaque
      ``DuplicateTable``.

    The returned ``source`` stays ``"deferred"`` for a healthy database:
    ``ensure_startup_schema`` still does not migrate PostgreSQL at startup, it
    only verifies that migration is safe.
    """
    from nexus_ai_agent.storage.adopt_pg import (
        IncompatiblePostgresSchemaError,
        PgBootstrapState,
        UnstampedPostgresError,
        incompatible_message,
        probe_postgres_state,
        unstamped_message,
    )

    report = probe_postgres_state(resolve_migration_url())
    if report is None:
        return {"backend": "postgresql", "source": "deferred"}
    if report.state is PgBootstrapState.INCOMPATIBLE:
        raise IncompatiblePostgresSchemaError(incompatible_message(report))
    if report.state is PgBootstrapState.ADOPTABLE:
        raise UnstampedPostgresError(unstamped_message(report))
    return {"backend": "postgresql", "source": "deferred"}


def run_migrations() -> None:
    """Apply all pending Alembic revisions (``upgrade head``).

    Backend-agnostic: the URL comes from :func:`resolve_migration_url`, so the
    same call upgrades a local SQLite file or a Neon Postgres database.

    A pre-Alembic database is adopted first (create_all + stamp) instead of
    being overwritten by the initial revision, which keeps legacy installs and
    their data intact.  For SQLite that adoption is automatic (D6); for
    PostgreSQL it fails fast and points at ``nexus adopt-pg`` (D10), because
    silently mutating a shared hosted database is not an acceptable default.
    """
    if resolve_database_url() is not None:
        from nexus_ai_agent.storage.adopt_pg import assert_postgres_ready

        assert_postgres_ready(resolve_migration_url())
        command.upgrade(build_alembic_config(), "head")
        return

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

    ``"deferred"`` means PostgreSQL: ``ensure_startup_schema`` never migrates a
    serverless Postgres host (Neon) at bot startup, because the host may be
    idle/scaled-to-zero.  The schema is prepared lazily by ``get_session`` via
    the same ``run_migrations`` path (D7).  Since D10 the call also *probes*
    PostgreSQL when it is reachable and fails fast on an un-stamped database
    instead of letting ``alembic upgrade head`` crash later.

    Note: call this from a *sync* context (the CLI command body).  The legacy
    SQLite adoption opens its own private event loop; do not call it from
    inside a running loop.
    """
    if resolve_database_url() is not None:
        return _postgres_startup_report()

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
