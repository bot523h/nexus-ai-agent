"""PostgreSQL regression test for independent concurrent startup connections."""

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
def test_eight_independent_postgres_creators_all_succeed() -> None:
    """Eight independent engines model eight replicas booting simultaneously."""

    async def run() -> None:
        raw_url = os.environ["NEXUS_DATABASE_URL"]
        url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        engines = [create_async_engine(url, pool_size=1, max_overflow=0) for _ in range(8)]
        try:
            results = await asyncio.gather(
                *(create_all_metadata(engine, SQLModel.metadata) for engine in engines),
                return_exceptions=True,
            )
            assert all(result is None for result in results), results
        finally:
            await asyncio.gather(*(engine.dispose() for engine in engines))

    asyncio.run(run())
