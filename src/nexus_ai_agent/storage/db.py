from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from nexus_ai_agent.storage import models as _models  # noqa: F401

log = logging.getLogger(__name__)

_engine: Any | None = None
_engine_path: str | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_initialized_paths: set[str] = set()
# Engines retired when the SQLite path changes.  Disposing an engine requires
# an event loop, while _get_engine is synchronous, so retired engines are
# closed by the next async database operation (see _dispose_replaced_engines).
_replaced_engines: list[Any] = []

# Postgres (NEXUS_DATABASE_URL) state, keyed by the normalized URL.
_pg_engines: dict[str, Any] = {}
_pg_session_factories: dict[str, async_sessionmaker[AsyncSession]] = {}
# URLs whose schema has already been brought to head via Alembic.  Prepared
# = Alembic migrated (the stopgap create_all was retired in D7); the guard is
# per-URL so two different Neon databases in one process stay independent.
_pg_prepared_urls: set[str] = set()

# The only URL schemes accepted for NEXUS_DATABASE_URL.
_SUPPORTED_URL_SCHEMES = ("postgresql", "postgres", "postgresql+asyncpg")


def normalize_database_url(url: str) -> str:
    """Normalize a PostgreSQL URL to the canonical ``postgresql://`` form.

    Accepts the ``postgresql://``, ``postgres://`` and ``postgresql+asyncpg://``
    schemes (all normalized to ``postgresql://``).  Anything else — including
    SQLite file URLs — is rejected with a clear error.
    """
    if not isinstance(url, str):
        raise ValueError(f"database_url must be a string, got {type(url).__name__}")
    candidate = url.strip()
    if not candidate:
        raise ValueError(
            "database_url is empty; set NEXUS_DATABASE_URL to a PostgreSQL URL "
            "such as postgresql://user:password@host:port/database"
        )
    parts = urlsplit(candidate)
    scheme = parts.scheme.lower()
    if scheme not in _SUPPORTED_URL_SCHEMES:
        raise ValueError(
            f"Unsupported database URL scheme {parts.scheme!r}: only postgresql://, "
            "postgres:// and postgresql+asyncpg:// are supported. "
            "For SQLite files use NEXUS_DB_PATH instead."
        )
    if not parts.netloc:
        raise ValueError(
            "Invalid PostgreSQL URL: missing host. Expected "
            f"postgresql://user:password@host:port/database, got {candidate!r}"
        )
    return urlunsplit(("postgresql", parts.netloc, parts.path, parts.query, parts.fragment))


def to_asyncpg_url(url: str) -> str:
    """Return the SQLAlchemy asyncpg form (``postgresql+asyncpg://``) of a URL."""
    normalized = normalize_database_url(url)
    parts = urlsplit(normalized)
    return urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, parts.query, parts.fragment))


def resolve_migration_url() -> str:
    """Resolve the URL Alembic should migrate (D3).

    Mirrors the runtime backend selection with the exact same priority as
    :func:`resolve_database_url`: ``NEXUS_DATABASE_URL`` (PostgreSQL/Neon,
    returned in asyncpg form via :func:`to_asyncpg_url`) wins; otherwise the
    configured SQLite path is returned as a ``sqlite+aiosqlite://`` URL.  The
    C1 helpers are reused as-is — no URL logic is duplicated here.
    """
    database_url = resolve_database_url()
    if database_url is not None:
        return to_asyncpg_url(database_url)

    from nexus_ai_agent.config.settings import get_settings

    db_path = Path(get_settings().db_path).expanduser()
    return f"sqlite+aiosqlite:///{db_path}"


def resolve_database_url() -> str | None:
    """Return the PostgreSQL URL configured via the environment, or ``None``.

    Reads ``NEXUS_DATABASE_URL`` (with ``DATABASE_URL`` as the legacy alias)
    through the cached settings.  A blank value (for example the untouched
    ``.env.example`` entry) means "not configured".
    """
    from nexus_ai_agent.config.settings import get_settings

    value = (get_settings().database_url or "").strip()
    if not value:
        return None
    return normalize_database_url(value)


# ── D5: Alembic-first bootstrap decision for SQLite ───────────────────


class _SqliteBootstrapMode(Enum):
    """Which of the two schema sources (Alembic / create_all) should run.

    Both sources end in an equivalent schema; the decision only guards the
    bootstrap *path*, so existing SQLite users are never forced to migrate.
    """

    ALEMBIC = "alembic"
    CREATE_ALL = "create_all"
    NOTHING = "nothing"


def _sqlite_has_tables(db_path: str) -> bool:
    """True when the SQLite file has any user table (sqlite_* excluded)."""
    from sqlalchemy import create_engine as sync_create_engine

    engine = sync_create_engine(f"sqlite:///{Path(db_path).expanduser()}")
    try:
        inspector = inspect(engine)
        tables = {t for t in inspector.get_table_names() if not t.startswith("sqlite_")}
        return bool(tables)
    finally:
        engine.dispose()


def _sqlite_is_alembic_stamped(db_path: str) -> bool:
    """True when the SQLite file carries an ``alembic_version`` table."""
    from sqlalchemy import create_engine as sync_create_engine

    engine = sync_create_engine(f"sqlite:///{Path(db_path).expanduser()}")
    try:
        inspector = inspect(engine)
        return "alembic_version" in inspector.get_table_names()
    finally:
        engine.dispose()


def decide_sqlite_bootstrap(db_path: str | None = None) -> str:
    """Decide the SQLite bootstrap path (D5), without mutating anything.

    Priority:

    1. ``alembic``  — the file is already Alembic-managed (``alembic_version``
       exists: a developer already ran ``nexus migrate`` / an earlier Alembic
       release, or an empty file is a brand-new install where Alembic is the
       single source of truth from day one).  ``alembic upgrade head`` brings
       it to head.
    2. ``create_all`` — the file already has tables but is NOT Alembic-stamped:
       a pre-Alembic install.  ``SQLModel.metadata.create_all`` is idempotent,
       so this preserves the existing data and tables without a migration.
    3. ``nothing`` — the database is ready (already created in this process);
       the cached ``_initialized_paths`` guard handles this case upstream.

    The enum value is returned as a string for a tiny, stable public API.
    """
    from nexus_ai_agent.config.settings import get_settings

    normalized = str(Path(db_path or get_settings().db_path).expanduser())
    if normalized in _initialized_paths:
        return _SqliteBootstrapMode.NOTHING.value
    if not Path(normalized).exists():
        # Brand-new install: Alembic owns the schema from the first byte.
        return _SqliteBootstrapMode.ALEMBIC.value
    if _sqlite_is_alembic_stamped(normalized):
        return _SqliteBootstrapMode.ALEMBIC.value
    if _sqlite_has_tables(normalized):
        return _SqliteBootstrapMode.CREATE_ALL.value
    # Empty-but-existing (created with no table): treat as a new install.
    return _SqliteBootstrapMode.ALEMBIC.value


def _get_engine(db_path: str) -> Any:
    """Return an engine bound to ``db_path``, recreating it when the path changes."""
    global _engine, _engine_path, _session_factory

    normalized_path = str(Path(db_path).expanduser())
    if _engine is None or _engine_path != normalized_path:
        Path(normalized_path).parent.mkdir(parents=True, exist_ok=True)
        if _engine is not None:
            # Retire the old engine: its pooled aiosqlite connections (and
            # worker threads) are closed by _dispose_replaced_engines at the
            # next async database operation, so they never outlive the loop
            # that created them.
            _replaced_engines.append(_engine)
            _engine = None
        _engine = create_async_engine(f"sqlite+aiosqlite:///{normalized_path}", echo=False)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
        _engine_path = normalized_path
    return _engine


async def _dispose_replaced_engines() -> None:
    """Close engines retired by a previous path switch, if any."""
    while _replaced_engines:
        engine = _replaced_engines.pop()
        try:
            await engine.dispose()
        except Exception:
            # A broken pool must never block database access.
            pass


#: How many times :func:`create_all_metadata` retries after losing a race.
_CREATE_ALL_ATTEMPTS = 6


def _is_concurrent_create_conflict(exc: Exception) -> bool:
    """Whether ``exc`` is another process having just created the same object.

    ``MetaData.create_all`` runs with ``checkfirst=True``, so it asks whether a
    table exists and *then* emits ``CREATE TABLE``.  Two processes booting
    together both see "does not exist", both emit the DDL, and the loser gets
    ``table … already exists``.  That is a benign race, not a broken schema —
    the winner did exactly the work the loser was about to do.
    """
    message = str(getattr(exc, "orig", exc)).lower()
    return "already exists" in message


async def create_all_metadata(engine: Any, metadata: MetaData) -> None:
    """Idempotently create ``metadata``'s tables, tolerating a concurrent creator.

    :func:`migrations.migration_lock` serialises migrators *on one host*, via a
    local ``fcntl.flock`` file.  It cannot see a second host — two Koyeb
    replicas booting against the same volume, or an operator running
    ``nexus migrate`` from a laptop while the bot restarts elsewhere.  This
    retry is the complement: a create-conflict is absorbed and retried, so a
    second attempt re-runs ``checkfirst`` and sees what the winner built.
    Anything that is not a create-conflict propagates on the first attempt.
    """
    for attempt in range(1, _CREATE_ALL_ATTEMPTS + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
            return
        except (OperationalError, ProgrammingError) as exc:
            if attempt >= _CREATE_ALL_ATTEMPTS or not _is_concurrent_create_conflict(exc):
                raise
            log.warning(
                "concurrent schema creation detected (%s); retry %d/%d",
                str(getattr(exc, "orig", exc)).splitlines()[0],
                attempt,
                _CREATE_ALL_ATTEMPTS - 1,
            )
            await asyncio.sleep(0.05 * attempt)
    raise RuntimeError("unreachable: create_all_metadata retry loop exhausted")  # pragma: no cover


async def create_all_tables(db_path: str = "data/app.sqlite") -> None:
    """Create all SQLModel tables for the selected database, exactly once per path."""
    await _dispose_replaced_engines()
    normalized_path = str(Path(db_path).expanduser())
    engine = _get_engine(normalized_path)
    if normalized_path in _initialized_paths:
        return

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
    await create_all_metadata(engine, SQLModel.metadata)
    _initialized_paths.add(normalized_path)


def _get_pg_engine(url: str) -> Any:
    """Return the cached async engine for ``url`` (created on first use).

    ``pool_pre_ping`` re-validates pooled connections before use, which is
    important for serverless Postgres hosts (Neon) that drop idle connections.
    """
    normalized = normalize_database_url(url)
    engine = _pg_engines.get(normalized)
    if engine is None:
        engine = create_async_engine(to_asyncpg_url(normalized), echo=False, pool_pre_ping=True)
        _pg_engines[normalized] = engine
    return engine


def _get_pg_session_factory(url: str) -> async_sessionmaker[AsyncSession]:
    """Return the cached async session factory for ``url`` (created on first use)."""
    normalized = normalize_database_url(url)
    factory = _pg_session_factories.get(normalized)
    if factory is None:
        factory = async_sessionmaker(_get_pg_engine(normalized), expire_on_commit=False)
        _pg_session_factories[normalized] = factory
    return factory


async def _ensure_pg_schema(url: str) -> None:
    """Bring a Postgres database to head via Alembic, exactly once per URL.

    D7 retired the ``create_all`` stopgap and made Alembic the single source
    of schema truth for PostgreSQL.  D10 adds the adoption seam: a pre-Alembic
    database (C1-era, tables but no ``alembic_version``) is either stamped at
    head when its schema matches the metadata exactly, or rejected with a
    clear RuntimeError.  Fresh databases are built via ``upgrade head``.

    Alembic's ``env.py`` drives its own event loop (``asyncio.run``), so the
    upgrade is executed in a worker thread from this running loop.
    """
    normalized = normalize_database_url(url)
    if normalized in _pg_prepared_urls:
        return
    from nexus_ai_agent.storage.adopt_pg import prepare_postgres

    await prepare_postgres(normalized)
    _pg_prepared_urls.add(normalized)


@asynccontextmanager
async def get_session(db_path: str | None = None) -> AsyncIterator[AsyncSession]:
    """Yield an initialized async session for the configured backend.

    - ``db_path`` given  → SQLite backend, exactly as before (unchanged).
    - ``db_path`` is ``None`` → the backend comes from the environment:
      ``NEXUS_DATABASE_URL`` set → PostgreSQL, prepared lazily via Alembic
      (D7: the ``create_all`` stopgap was retired); otherwise the default
      SQLite path.
    """
    await _dispose_replaced_engines()
    if db_path is None:
        database_url = resolve_database_url()
        if database_url is not None:
            await _ensure_pg_schema(database_url)
            factory = _get_pg_session_factory(database_url)
            async with factory() as session:
                yield session
            return
        db_path = "data/app.sqlite"

    await create_all_tables(db_path)
    if _session_factory is None:
        raise RuntimeError("Database session factory is not initialized")
    async with _session_factory() as session:
        yield session
