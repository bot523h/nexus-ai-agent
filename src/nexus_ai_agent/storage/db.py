from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from nexus_ai_agent.storage import models as _models  # noqa: F401

_engine = None
_session_factory = None


def _get_engine(db_path: str):
    global _engine, _session_factory
    if _engine is None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{db_path}"
        _engine = create_async_engine(url, echo=False)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def create_all_tables(db_path: str = "data/app.sqlite") -> None:
    engine = _get_engine(db_path)
    async with engine.begin() as conn:
        await conn.execute(__import__("sqlalchemy").text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(SQLModel.metadata.create_all)


@asynccontextmanager
async def get_session(db_path: str = "data/app.sqlite"):
    _get_engine(db_path)
    async with _session_factory() as session:
        yield session
