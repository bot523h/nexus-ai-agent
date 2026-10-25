"""Deterministic contract tests for the ingest compare-and-set transitions.

The concurrent-replay tests prove the race is closed under real interleaving but
depend on timing.  These pin each conditional transition *directly*, so a future
change that turns an ``UPDATE ... WHERE state='STAGING'`` back into an
unconditional write fails deterministically, not once in a hundred runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets.models import IngestState, MediaKind
from nexus_ai_agent.creative.assets.repository import SqlAssetRepository
from nexus_ai_agent.storage.db import get_session

PROJECT = "project-alpha"


@asynccontextmanager
async def _session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with get_session(str(tmp_path / "cas.sqlite")) as session:
        yield session


async def _staging_row(repo: SqlAssetRepository, session: AsyncSession, key: str = "k") -> str:
    ingest_id = f"ing-{key}"
    await repo.create_ingest(
        ingest_id=ingest_id,
        idempotency_key=key,
        project_id=PROJECT,
        request_fingerprint="fp",
        staging_key="staging/original",
        original_filename="a.png",
    )
    await session.commit()
    return ingest_id


async def _complete(repo: SqlAssetRepository, ingest_id: str) -> bool:
    return await repo.complete_ingest(
        ingest_id,
        asset_id="asset-1",
        version_id="version-1",
        blob_key="blobs/xx",
        content_sha256="ab" * 32,
        size_bytes=104,
        media_kind=MediaKind.IMAGE,
        media_type="image/png",
    )


@pytest.mark.asyncio
async def test_complete_ingest_wins_exactly_once(tmp_path: Path) -> None:
    async with _session(tmp_path) as session:
        repo = SqlAssetRepository(session)
        ingest_id = await _staging_row(repo, session)
        assert await _complete(repo, ingest_id) is True
        assert await _complete(repo, ingest_id) is False  # the loser is told, not silent


@pytest.mark.asyncio
async def test_mark_failed_cannot_downgrade_a_ready_ingest(tmp_path: Path) -> None:
    async with _session(tmp_path) as session:
        repo = SqlAssetRepository(session)
        ingest_id = await _staging_row(repo, session)
        assert await _complete(repo, ingest_id) is True
        await session.commit()

        assert await repo.mark_failed(ingest_id, error_code="staging_lost") is False

        row = await repo.get_ingest_fresh(ingest_id)
        assert row is not None and row.state is IngestState.READY
        assert row.error_code is None


@pytest.mark.asyncio
async def test_mark_failed_applies_to_a_staging_ingest_and_clears_the_staging_key(
    tmp_path: Path,
) -> None:
    async with _session(tmp_path) as session:
        repo = SqlAssetRepository(session)
        ingest_id = await _staging_row(repo, session)
        assert await repo.mark_failed(ingest_id, error_code="empty_content") is True
        row = await repo.get_ingest_fresh(ingest_id)
        assert row is not None and row.state is IngestState.FAILED
        assert row.error_code == "empty_content" and row.staging_key is None


@pytest.mark.asyncio
async def test_set_staging_key_refuses_a_row_that_left_staging(tmp_path: Path) -> None:
    async with _session(tmp_path) as session:
        repo = SqlAssetRepository(session)
        ingest_id = await _staging_row(repo, session)
        assert await repo.set_staging_key(ingest_id, "staging/replay") is True
        assert await _complete(repo, ingest_id) is True
        await session.commit()

        assert await repo.set_staging_key(ingest_id, "staging/late") is False
        row = await repo.get_ingest_fresh(ingest_id)
        assert row is not None and row.staging_key is None  # no dangling reference


@pytest.mark.asyncio
async def test_downgrade_applies_only_to_a_ready_ingest(tmp_path: Path) -> None:
    async with _session(tmp_path) as session:
        repo = SqlAssetRepository(session)
        staging_id = await _staging_row(repo, session, "s")
        assert await repo.downgrade_ready_to_failed(staging_id, error_code="blob_missing") is False
        row = await repo.get_ingest_fresh(staging_id)
        assert row is not None and row.state is IngestState.STAGING  # untouched

        ready_id = await _staging_row(repo, session, "r")
        assert await _complete(repo, ready_id) is True
        await session.commit()
        assert await repo.downgrade_ready_to_failed(ready_id, error_code="blob_missing") is True
        row = await repo.get_ingest_fresh(ready_id)
        assert row is not None and row.state is IngestState.FAILED


@pytest.mark.asyncio
async def test_get_ingest_fresh_reports_the_committed_state(tmp_path: Path) -> None:
    """``get_ingest_fresh`` reads the database row, not a cached object.

    (Whether plain ``get_ingest`` can be stale is NOT claimed here: with the
    default expire-on-commit session it reloads.  The fresh read exists so that
    decisions taken *because a row may have changed* never depend on that.)
    """
    async with _session(tmp_path) as reader, _session(tmp_path) as writer:
        reader_repo, writer_repo = SqlAssetRepository(reader), SqlAssetRepository(writer)
        ingest_id = await _staging_row(writer_repo, writer)
        seen = await reader_repo.get_ingest(ingest_id)
        assert seen is not None and seen.state is IngestState.STAGING

        assert await _complete(writer_repo, ingest_id) is True
        await writer.commit()

        fresh = await reader_repo.get_ingest_fresh(ingest_id)
        assert fresh is not None and fresh.state is IngestState.READY
