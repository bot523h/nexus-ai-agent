"""Adopt a pre-Alembic PostgreSQL database safely (D10).

C1 bootstrapped hosted PostgreSQL (Neon) with ``SQLModel.metadata.create_all``;
D7 retired that stopgap and made Alembic the single source of schema truth.
A database created in the C1 window therefore has tables but no
``alembic_version`` marker.  Replaying the initial revision against it would
crash on ``relation ... already exists`` with a raw DBAPI error.

This module closes that last gap the same safe way D6 did for SQLite:

* *Auto-adopt* — when the existing tables exactly match the application
  metadata (zero drift), stamp the database at ``head``.  No data is touched
  and no migration is replayed.
* *Fail-fast* — when drift exists (missing or unknown tables), raise a clear,
  actionable error instead of a confusing DBAPI crash.

URLs are normalized (:func:`normalize_database_url`) and inspected over the
asyncpg transport so introspection uses the same wire protocol the runtime
does.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from nexus_ai_agent.observability.logging import get_logger
from nexus_ai_agent.storage.db import normalize_database_url, to_asyncpg_url
from nexus_ai_agent.storage.migration_metadata import get_target_metadata

log = get_logger(__name__)

#: Marker table Alembic writes once a database is versioned.
ALEMBIC_VERSION_TABLE = "alembic_version"

#: Actions ``prepare_postgres`` / ``adopt_postgres`` can take.
ACTION_MANAGED = "alembic_managed"  # stamped → upgrade head
ACTION_MIGRATE = "migrate"  # empty → build via upgrade head
ACTION_ADOPT = "adopt"  # zero-drift legacy → stamp head
ACTION_FAIL = "fail_fast"  # drift → raise RuntimeError


@dataclass
class PostgresAdoptionReport:
    """Outcome of inspecting/preparing a PostgreSQL database for Alembic."""

    database: str  # redacted URL (host only) for traceability
    action: str  # one of the ACTION_* constants
    alembic_stamped: bool
    table_count: int  # user tables (alembic_version excluded)
    missing_tables: list[str] = field(default_factory=list)
    extra_tables: list[str] = field(default_factory=list)
    reason: str = ""

    def is_drift(self) -> bool:
        """True when the DB diverges from the application metadata."""
        return bool(self.missing_tables or self.extra_tables)


def _redacted_url(url: str) -> str:
    """Return a URL safe to log: scheme + host only, never credentials/path."""
    normalized = normalize_database_url(url)
    scheme, _, rest = normalized.partition("://")
    host = rest.split("/", 1)[0].split("@", 1)[-1]
    return f"{scheme}://{host}"


def _snapshot(sync_conn: Connection, expected: set[str], redacted: str) -> PostgresAdoptionReport:
    inspector = inspect(sync_conn)
    tables = set(inspector.get_table_names())
    user_tables = {t for t in tables if t != ALEMBIC_VERSION_TABLE}
    stamped = ALEMBIC_VERSION_TABLE in tables
    return PostgresAdoptionReport(
        database=redacted,
        action=ACTION_MANAGED if stamped else ACTION_MIGRATE,
        alembic_stamped=stamped,
        table_count=len(user_tables),
        missing_tables=sorted(expected - user_tables),
        extra_tables=sorted(user_tables - expected),
        reason="stamped" if stamped else "",
    )


async def _inspect(url: str) -> PostgresAdoptionReport:
    """Introspect the database over asyncpg; never mutates it."""
    normalized = normalize_database_url(url)
    expected = set(get_target_metadata().tables.keys())
    engine = create_async_engine(to_asyncpg_url(normalized), echo=False)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync_conn: _snapshot(sync_conn, expected, _redacted_url(normalized))
            )
    finally:
        await engine.dispose()


def inspect_postgres(url: str) -> PostgresAdoptionReport:
    """Synchronous inspection wrapper for CLI/scripts (mounts its own loop)."""
    return asyncio.run(_inspect(url))


def decide(report: PostgresAdoptionReport) -> str:
    """Decide the single next action from an inspection report.

    Pure function — the one place the D10 decision matrix lives:

    * stamped → ``alembic_managed`` (upgrade head)
    * empty   → ``migrate`` (build via upgrade head)
    * drift   → ``fail_fast``
    * else    → ``adopt`` (stamp head)
    """
    if report.alembic_stamped:
        return ACTION_MANAGED
    if report.table_count == 0:
        return ACTION_MIGRATE
    if report.is_drift():
        return ACTION_FAIL
    return ACTION_ADOPT


def drift_error(report: PostgresAdoptionReport) -> RuntimeError:
    return RuntimeError(
        "Detected an un-stamped PostgreSQL database with schema drift "
        f"(missing={report.missing_tables}, extra={report.extra_tables}). "
        "Run `nexus adopt-pg --dry-run` for details, then reconcile the "
        "schema before stamping. Data is left untouched."
    )


def stamp_head() -> None:
    """Stamp the database at ``head`` without replaying any migration.

    Synchronous: Alembic drives its own event loop in ``migrations/env.py``, so
    callers that already own a loop must off-load this to a worker thread (see
    :func:`prepare_postgres`).  Sync callers such as ``run_migrations`` can call
    it directly.
    """
    from alembic import command

    from nexus_ai_agent.storage.migrations import build_alembic_config

    command.stamp(build_alembic_config(), "head")


async def prepare_postgres(url: str) -> PostgresAdoptionReport:
    """Bring a PostgreSQL database to head on first use (called from get_session).

    Runs inside an existing event loop: inspection is awaited inline, while
    Alembic operations (which drive their own loop in ``env.py``) run in a
    worker thread.

    Raises:
        RuntimeError: On schema drift — with an actionable message.
    """
    report = await _inspect(url)
    action = decide(report)
    if action == ACTION_FAIL:
        raise drift_error(report)
    if action == ACTION_ADOPT:
        await asyncio.to_thread(stamp_head)
        report.action = ACTION_ADOPT
        log.info("Adopted PostgreSQL database at head (%s tables, zero drift)", report.table_count)
        return report

    from nexus_ai_agent.storage.migrations import run_migrations

    await asyncio.to_thread(run_migrations)
    report.action = ACTION_MANAGED if report.alembic_stamped else ACTION_MIGRATE
    log.info("PostgreSQL at head (%s, %s tables)", report.action, report.table_count)
    return report


def adopt_postgres(url: str) -> PostgresAdoptionReport:
    """CLI entry point: dry-run inspect or stamp an adoptable database.

    Raises:
        RuntimeError: On schema drift — with an actionable message.
    """
    report = inspect_postgres(url)
    action = decide(report)
    if action == ACTION_FAIL:
        raise drift_error(report)
    if action == ACTION_ADOPT:
        stamp_head()
        report.action = ACTION_ADOPT
        log.info("Adopted PostgreSQL database at head (%s tables, zero drift)", report.table_count)
        return report
    report.action = action
    return report
