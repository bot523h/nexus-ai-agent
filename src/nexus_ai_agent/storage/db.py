from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from nexus_ai_agent.storage import models as _models  # noqa: F401

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
_pg_initialized_urls: set[str] = set()

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


async def create_all_tables(db_path: str = "data/app.sqlite") -> None:
    """Create all SQLModel tables for the selected database, exactly once per path."""
    await _dispose_replaced_engines()
    normalized_path = str(Path(db_path).expanduser())
    engine = _get_engine(normalized_path)
    if normalized_path in _initialized_paths:
        return

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(SQLModel.metadata.create_all)
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


async def _ensure_pg_tables(url: str) -> None:
    """Stopgap schema bootstrap for Postgres until Alembic lands in C2."""
    normalized = normalize_database_url(url)
    if normalized in _pg_initialized_urls:
        return
    async with _get_pg_engine(normalized).begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    _pg_initialized_urls.add(normalized)


@asynccontextmanager
async def get_session(db_path: str | None = None) -> AsyncIterator[AsyncSession]:
    """Yield an initialized async session for the configured backend.

    - ``db_path`` given  → SQLite backend, exactly as before (unchanged).
    - ``db_path`` is ``None`` → the backend comes from the environment:
      ``NEXUS_DATABASE_URL`` set → PostgreSQL (with a stopgap ``create_all``
      until Alembic lands in C2); otherwise the default SQLite path.
    """
    await _dispose_replaced_engines()
    if db_path is None:
        database_url = resolve_database_url()
        if database_url is not None:
            await _ensure_pg_tables(database_url)
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
