from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from nexus_ai_agent.storage import models as _models  # noqa: F401

_engine: Any | None = None
_engine_path: str | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_initialized_paths: set[str] = set()


def _get_engine(db_path: str) -> Any:
    """Return an engine bound to ``db_path``, recreating it when the path changes."""
    global _engine, _engine_path, _session_factory

    normalized_path = str(Path(db_path).expanduser())
    if _engine is None or _engine_path != normalized_path:
        Path(normalized_path).parent.mkdir(parents=True, exist_ok=True)
        if _engine is not None:
            # The old engine is intentionally replaced here.  SQLAlchemy will
            # close its pooled connections when the process shuts down.
            _engine = None
        _engine = create_async_engine(f"sqlite+aiosqlite:///{normalized_path}", echo=False)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
        _engine_path = normalized_path
    return _engine


async def create_all_tables(db_path: str = "data/app.sqlite") -> None:
    """Create all SQLModel tables for the selected database, exactly once per path."""
    normalized_path = str(Path(db_path).expanduser())
    engine = _get_engine(normalized_path)
    if normalized_path in _initialized_paths:
        return

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(SQLModel.metadata.create_all)
    _initialized_paths.add(normalized_path)


@asynccontextmanager
async def get_session(db_path: str = "data/app.sqlite") -> AsyncIterator[AsyncSession]:
    """Yield an initialized async session for ``db_path``.

    Lazy initialization keeps CLI commands and library consumers safe even when
    migrations have not been run explicitly, while preserving the explicit
    ``create_all_tables`` entry point for deployments.
    """
    await create_all_tables(db_path)
    if _session_factory is None:
        raise RuntimeError("Database session factory is not initialized")
    async with _session_factory() as session:
        yield session
