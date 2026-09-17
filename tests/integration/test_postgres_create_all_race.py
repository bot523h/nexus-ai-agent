"""PostgreSQL regression test for concurrent create_all startup."""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

import nexus_ai_agent.storage.models  # noqa: F401
from nexus_ai_agent.storage.db import create_all_metadata


@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("NEXUS_DATABASE_URL"), reason="requires PostgreSQL")
def test_eight_postgres_creators_all_succeed() -> None:
    """The IntegrityError loser retry must make every simultaneous boot succeed."""

    async def run() -> None:
        url = os.environ["NEXUS_DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
        engine = create_async_engine(url)
        try:
            results = await asyncio.gather(
                *(create_all_metadata(engine, SQLModel.metadata) for _ in range(8)),
                return_exceptions=True,
            )
            assert all(result is None for result in results), results
        finally:
            await engine.dispose()

    asyncio.run(run())
