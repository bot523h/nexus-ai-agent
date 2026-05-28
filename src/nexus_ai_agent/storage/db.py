from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from nexus_ai_agent.config.settings import get_settings

_engine: AsyncEngine | None = None
_session_factory: sessionmaker | None = None


def _sqlite_url(db_path: str) -> str:
    # aiosqlite is required for true async usage; it is expected to be available via deps.
    return f"sqlite+aiosqlite:///{db_path}"


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is not None:
        return _engine

    settings = get_settings()
    db_path = settings.db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        _sqlite_url(db_path),
        echo=False,
        future=True,
        pool_pre_ping=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
        # Enable WAL for better concurrent reads/writes; enable FK constraints.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    _engine = engine
    return engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is not None:
        return _session_factory

    engine = get_engine()
    _session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return _session_factory


async def create_all_tables() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        # Ensure pragmas are applied at least once even if engine pooling changes.
        await conn.execute(text("PRAGMA journal_mode=WAL;"))
        await conn.execute(text("PRAGMA foreign_keys=ON;"))
        await conn.run_sync(SQLModel.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    SessionLocal = get_session_factory()
    async with SessionLocal() as session:
        yield session

