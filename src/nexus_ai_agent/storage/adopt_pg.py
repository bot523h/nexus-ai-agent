"""Legacy PostgreSQL adoption and the un-stamped fail-fast guard (D10).

D6 closed this hole for SQLite; D10 closes the same hole for PostgreSQL.

The black swan
--------------
``NEXUS_DATABASE_URL`` may point at a PostgreSQL database that already holds
NEXUS tables but has **no** ``alembic_version`` table — for example a database
created by the C1 ``create_all`` stopgap before D7 retired it, or one restored
from a pre-Alembic dump.  Running ``alembic upgrade head`` against it replays
the initial revision and dies inside ``op.create_table`` with::

    psycopg.errors.DuplicateTable: relation "chat" already exists

That message says nothing about the real situation and nothing about the fix.

The two decisions encoded here
------------------------------
1. **Fail-fast, never silent mutation.**  :func:`assert_postgres_ready` raises
   :class:`UnstampedPostgresError` — naming the database, the table count and
   the exact command to run — *before* Alembic touches anything.  Fail-Fast
   wins over Zero-Data-Loss here only in the sense that we refuse to guess:
   stamping a schema we have not verified would be the real data-loss risk,
   because every later revision would then run against a database Alembic
   believes is at head.
2. **Adoption is explicit, inspectable and reversible-by-default.**
   :func:`adopt` refuses a database that is missing tables the current models
   require (:class:`IncompatiblePostgresSchemaError`), supports ``dry_run``,
   and otherwise performs exactly the D6 sequence — idempotent ``create_all``
   followed by ``alembic stamp head`` — so no row is ever dropped and the
   initial revision is never replayed over existing tables.

Testability
-----------
Every I/O function takes an explicit SQLAlchemy URL rather than reading
settings, so the *same* code path can be driven against PostgreSQL in CI and
against a temporary SQLite file in the unit suite (dialect substitution, not a
mock).  :class:`PgSchemaReport` and :attr:`PgSchemaReport.state` are pure, so
the decision table is testable with no database at all.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit, urlunsplit

from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from nexus_ai_agent.storage.migration_metadata import get_target_metadata

log = logging.getLogger(__name__)

#: The bookkeeping table Alembic uses to record the applied revision.
ALEMBIC_VERSION_TABLE = "alembic_version"

#: The revision ``adopt`` stamps an adopted database at (the chain head).
HEAD_REVISION = "head"


# ── Errors ───────────────────────────────────────────────────────────────


class PgAdoptionError(RuntimeError):
    """Base class for every PostgreSQL adoption refusal."""


class PostgresNotConfiguredError(PgAdoptionError):
    """``NEXUS_DATABASE_URL`` is not set, so there is nothing to adopt."""


class UnstampedPostgresError(PgAdoptionError):
    """The database has NEXUS tables but no ``alembic_version`` stamp."""


class IncompatiblePostgresSchemaError(PgAdoptionError):
    """The database is missing tables the current models require."""


# ── Decision model (pure) ────────────────────────────────────────────────


class PgBootstrapState(str, Enum):
    """What an un-migrated PostgreSQL database actually is.

    The four states form a total decision table — :attr:`PgSchemaReport.state`
    maps any report onto exactly one of them, and every consumer branches on
    the enum instead of re-deriving the conditions.
    """

    MANAGED = "managed"
    """``alembic_version`` exists: Alembic already owns this database."""

    FRESH = "fresh"
    """No tables at all: ``alembic upgrade head`` is the correct path."""

    ADOPTABLE = "adoptable"
    """Tables exist, no stamp, and every expected table is present.

    Safe to adopt: ``create_all`` (idempotent) repairs any drift and
    ``stamp head`` records the database as versioned.
    """

    INCOMPATIBLE = "incompatible"
    """Tables exist, no stamp, and expected tables are missing.

    Refusing is the only safe answer: stamping head would assert a schema the
    database does not have, and every later revision would then be applied to
    a database that silently disagrees with it.
    """


def expected_tables() -> frozenset[str]:
    """Return the table names the current models define.

    Reads :func:`~nexus_ai_agent.storage.migration_metadata.get_target_metadata`
    — the exact metadata Alembic autogenerates against — so the adoption check
    and the migration chain can never disagree about what "complete" means.
    """
    return frozenset(get_target_metadata().tables)


@dataclass(frozen=True)
class PgSchemaReport:
    """An immutable snapshot of what a database contains right now.

    ``tables`` is every table the inspector sees (including
    ``alembic_version``); ``expected`` is the model-defined table set.
    """

    url: str
    tables: frozenset[str]
    expected: frozenset[str]

    @property
    def has_alembic_version(self) -> bool:
        """Whether the database carries Alembic's bookkeeping table."""
        return ALEMBIC_VERSION_TABLE in self.tables

    @property
    def user_tables(self) -> frozenset[str]:
        """Every table except Alembic's own bookkeeping table."""
        return self.tables - {ALEMBIC_VERSION_TABLE}

    @property
    def missing(self) -> frozenset[str]:
        """Tables the models require that the database does not have."""
        return self.expected - self.tables

    @property
    def extra(self) -> frozenset[str]:
        """Tables present but not model-defined (never blocks adoption)."""
        return self.user_tables - self.expected

    @property
    def state(self) -> PgBootstrapState:
        """Classify this report — the single decision point for PostgreSQL."""
        if self.has_alembic_version:
            return PgBootstrapState.MANAGED
        if not self.user_tables:
            return PgBootstrapState.FRESH
        if self.missing:
            return PgBootstrapState.INCOMPATIBLE
        return PgBootstrapState.ADOPTABLE


# ── Reporting helpers ────────────────────────────────────────────────────


def redact_url(url: str) -> str:
    """Return ``url`` with the password replaced, safe for error messages.

    ``NEXUS_DATABASE_URL`` carries credentials; an error that quotes it would
    leak them into logs and CI output, undoing the intent of D8.
    """
    parts = urlsplit(url)
    if not parts.password:
        return url
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _format_table_list(names: frozenset[str], limit: int = 10) -> str:
    """Render a table set for a human-readable message (truncated)."""
    ordered = sorted(names)
    shown = ", ".join(ordered[:limit])
    if len(ordered) > limit:
        shown += f", … (+{len(ordered) - limit} more)"
    return shown


def unstamped_message(report: PgSchemaReport) -> str:
    """Build the precise, actionable fail-fast message for ``report``."""
    return (
        f"Detected an un-stamped PostgreSQL database at {redact_url(report.url)}: "
        f"it already contains {len(report.user_tables)} NEXUS table(s) "
        f"(e.g. {_format_table_list(report.user_tables, 5)}) but no "
        f"`{ALEMBIC_VERSION_TABLE}` table, so `alembic upgrade head` would fail "
        f"with a DuplicateTable error (relation already exists).\n\n"
        f"Run:\n"
        f"  nexus adopt-pg --dry-run    # inspect, change nothing\n"
        f"  nexus adopt-pg --yes        # create missing tables + stamp at head\n\n"
        f"No data was modified."
    )


def incompatible_message(report: PgSchemaReport) -> str:
    """Build the refusal message for a schema that cannot be adopted."""
    return (
        f"Refusing to adopt {redact_url(report.url)}: the database is missing "
        f"{len(report.missing)} table(s) the current models require "
        f"({_format_table_list(report.missing)}). Stamping it at head would "
        f"record a schema the database does not have, and every later revision "
        f"would then be applied to a database that silently disagrees with it.\n\n"
        f"This is usually a database that belongs to a different application, or "
        f"a partial restore. Point NEXUS_DATABASE_URL at the right database, or "
        f"create the missing tables by hand, then re-run `nexus adopt-pg --dry-run`."
    )


# ── Introspection + adoption (I/O) ───────────────────────────────────────


def _table_names(connection: Connection) -> list[str]:
    """Return every table name visible to a sync connection (``run_sync`` target)."""
    return list(inspect(connection).get_table_names())


async def introspect(sqlalchemy_url: str) -> PgSchemaReport:
    """Read the live table set of ``sqlalchemy_url`` and classify it.

    Opens a throwaway engine (``NullPool``) so a serverless host such as Neon
    is never left holding a pooled connection from a short-lived CLI process.
    """
    engine = create_async_engine(sqlalchemy_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(_table_names)
    finally:
        await engine.dispose()
    return PgSchemaReport(
        url=sqlalchemy_url,
        tables=frozenset(tables),
        expected=expected_tables(),
    )


async def _create_missing_tables(sqlalchemy_url: str) -> None:
    """Idempotently create every model-defined table on ``sqlalchemy_url``.

    ``create_all`` only issues ``CREATE TABLE`` for tables that do not exist,
    which is exactly what makes adoption non-destructive: pre-existing tables
    and their rows are left untouched.  The shared helper also absorbs the
    ``checkfirst`` race, so two processes adopting the same Neon database at
    once converge instead of one dying on ``relation … already exists``.
    """
    from nexus_ai_agent.storage.db import create_all_metadata

    engine = create_async_engine(sqlalchemy_url, poolclass=NullPool)
    try:
        await create_all_metadata(engine, get_target_metadata())
    finally:
        await engine.dispose()


async def _alembic(verb: str, config: AlembicConfig) -> None:
    """Run an Alembic command without colliding with the caller's event loop.

    ``migrations/env.py`` drives its own loop with ``asyncio.run``, so calling
    ``command.stamp``/``command.upgrade`` directly from this coroutine would
    raise ``RuntimeError: asyncio.run() cannot be called from a running event
    loop``.  Off-loading to a worker thread gives Alembic a loop-free thread to
    own — the same technique ``storage.db._ensure_pg_schema`` already uses.
    """
    from alembic import command

    runner = command.stamp if verb == "stamp" else command.upgrade
    await asyncio.to_thread(runner, config, HEAD_REVISION)


@dataclass(frozen=True)
class AdoptResult:
    """What :func:`adopt` found and what it did (or would do)."""

    url: str
    state: PgBootstrapState
    action: str
    dry_run: bool
    tables_before: frozenset[str]
    missing: frozenset[str]
    extra: frozenset[str]
    stamped_revision: str | None = None

    @property
    def changed(self) -> bool:
        """Whether anything was actually written to the database."""
        return not self.dry_run and self.action != "none"


#: Action names used by :class:`AdoptResult`.
ACTION_NONE = "none"
ACTION_ADOPT = "create_all_and_stamp"
ACTION_UPGRADE = "alembic_upgrade"


async def adopt(sqlalchemy_url: str, *, dry_run: bool = False) -> AdoptResult:
    """Adopt a legacy PostgreSQL database, or report what would happen.

    Decision table (from :attr:`PgSchemaReport.state`):

    ==================  =========================  =========================
    state               ``dry_run=False``          ``dry_run=True``
    ==================  =========================  =========================
    ``MANAGED``         nothing                    nothing
    ``FRESH``           ``alembic upgrade head``   report only
    ``ADOPTABLE``       create_all + stamp head    report only
    ``INCOMPATIBLE``    raise                      raise
    ==================  =========================  =========================

    ``INCOMPATIBLE`` raises in both modes: a dry run must still tell the truth
    about a database that can never be adopted.
    """
    from nexus_ai_agent.storage.migrations import build_alembic_config

    report = await introspect(sqlalchemy_url)
    state = report.state

    def result(action: str, stamped: str | None) -> AdoptResult:
        """Build the result for one branch of the decision table."""
        return AdoptResult(
            url=sqlalchemy_url,
            state=state,
            action=action,
            dry_run=dry_run,
            tables_before=report.tables,
            missing=report.missing,
            extra=report.extra,
            stamped_revision=stamped,
        )

    if state is PgBootstrapState.INCOMPATIBLE:
        raise IncompatiblePostgresSchemaError(incompatible_message(report))

    if state is PgBootstrapState.MANAGED:
        log.info("PostgreSQL database is already Alembic-managed: %s", redact_url(sqlalchemy_url))
        return result(ACTION_NONE, None)

    if state is PgBootstrapState.FRESH:
        if dry_run:
            return result(ACTION_UPGRADE, None)
        await _alembic("upgrade", build_alembic_config(sqlalchemy_url))
        return result(ACTION_UPGRADE, HEAD_REVISION)

    # ADOPTABLE — the D6 sequence, on PostgreSQL.
    if dry_run:
        return result(ACTION_ADOPT, None)

    await _create_missing_tables(sqlalchemy_url)
    await _alembic("stamp", build_alembic_config(sqlalchemy_url))
    log.info(
        "adopted legacy PostgreSQL database and stamped it at head: %s "
        "(%d pre-existing table(s) preserved)",
        redact_url(sqlalchemy_url),
        len(report.user_tables),
    )
    return result(ACTION_ADOPT, HEAD_REVISION)


# ── Sync entry points (CLI + startup guard) ──────────────────────────────


def _require_configured_url() -> str:
    """Return the migration URL for the configured PostgreSQL, or raise."""
    from nexus_ai_agent.storage.db import resolve_database_url, resolve_migration_url

    if resolve_database_url() is None:
        raise PostgresNotConfiguredError(
            "NEXUS_DATABASE_URL is not set, so there is no PostgreSQL database to adopt. "
            "Set it to e.g. postgresql://user:password@host:5432/dbname, or use "
            "`nexus migrate` for the default SQLite backend."
        )
    return resolve_migration_url()


def adopt_postgres(*, dry_run: bool = False) -> AdoptResult:
    """Adopt the PostgreSQL database configured via ``NEXUS_DATABASE_URL``.

    Synchronous wrapper around :func:`adopt` for the CLI: opens a private event
    loop, so it must not be called from inside a running one.
    """
    url = _require_configured_url()
    return asyncio.run(adopt(url, dry_run=dry_run))


def assert_postgres_ready(sqlalchemy_url: str) -> PgSchemaReport:
    """Fail fast when ``sqlalchemy_url`` cannot be upgraded by Alembic.

    Called by :func:`nexus_ai_agent.storage.migrations.run_migrations` before
    ``alembic upgrade head``.  Raises:

    * :class:`UnstampedPostgresError` — tables present, no stamp;
    * :class:`IncompatiblePostgresSchemaError` — tables present, expected
      tables missing (an unstamped database that could not be adopted either).

    Returns the report for ``MANAGED`` and ``FRESH``, which are both safe to
    hand to Alembic.  Synchronous: opens a private event loop.
    """
    report = asyncio.run(introspect(sqlalchemy_url))
    if report.state is PgBootstrapState.INCOMPATIBLE:
        raise IncompatiblePostgresSchemaError(incompatible_message(report))
    if report.state is PgBootstrapState.ADOPTABLE:
        raise UnstampedPostgresError(unstamped_message(report))
    return report


def probe_postgres_state(sqlalchemy_url: str) -> PgSchemaReport | None:
    """Introspect ``sqlalchemy_url``, returning ``None`` if it is unreachable.

    Used by the *startup* path, which must never block a serverless Postgres
    host (Neon) that is idle or scaled to zero when the bot boots.  ``None``
    means "could not tell" — the caller defers schema preparation to the first
    real query, exactly as it did before D10.
    """
    from sqlalchemy.exc import SQLAlchemyError

    try:
        return asyncio.run(introspect(sqlalchemy_url))
    except (SQLAlchemyError, OSError, ModuleNotFoundError) as exc:
        log.debug("postgres startup probe deferred (%s)", exc)
        return None


__all__ = [
    "ACTION_ADOPT",
    "ACTION_NONE",
    "ACTION_UPGRADE",
    "ALEMBIC_VERSION_TABLE",
    "HEAD_REVISION",
    "AdoptResult",
    "IncompatiblePostgresSchemaError",
    "PgAdoptionError",
    "PgBootstrapState",
    "PgSchemaReport",
    "PostgresNotConfiguredError",
    "UnstampedPostgresError",
    "adopt",
    "adopt_postgres",
    "assert_postgres_ready",
    "expected_tables",
    "incompatible_message",
    "introspect",
    "probe_postgres_state",
    "redact_url",
    "unstamped_message",
]
