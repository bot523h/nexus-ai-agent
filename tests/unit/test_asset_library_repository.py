"""Focused unit tests for the durable metadata repository read paths.

The service exercises ``count_project_assets``, ``get_link_for_project`` and
``list_pending_ingests`` only indirectly (through ``get_asset`` / ``recover_all``),
so their scoping and ordering contracts were previously unpinned.  These tests
drive the real repository against real SQLite rows and assert the properties a
future query rewrite could silently break:

* project scope — a project never counts or resolves another project's rows;
* determinism — the link lookup and pending list have a defined order;
* lifecycle filter — "pending" means STAGING only, oldest first.

No mocks: the real ``SqlAssetRepository`` runs over a real temporary database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nexus_ai_agent.creative.assets.models import MediaKind
from nexus_ai_agent.creative.assets.repository import SqlAssetRepository
from nexus_ai_agent.storage import models as orm
from nexus_ai_agent.storage.db import get_session

SHA_A = "a" * 64
SHA_B = "b" * 64
PROJECT_A = "project-alpha"
PROJECT_B = "project-beta"
_BASE = datetime(2026, 1, 1, 12, 0, 0)


@asynccontextmanager
async def _repo(tmp_path: Path) -> AsyncIterator[SqlAssetRepository]:
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        yield SqlAssetRepository(session)


async def _seed_asset_version(
    repo: SqlAssetRepository, *, asset_id: str, version_id: str, sha: str
) -> None:
    await repo.add_asset(asset_id=asset_id, media_kind=MediaKind.IMAGE)
    await repo.add_version(
        version_id=version_id,
        asset_id=asset_id,
        content_sha256=sha,
        size_bytes=10,
        media_kind=MediaKind.IMAGE,
        media_type="image/png",
        blob_key=f"blobs/{sha}",
    )


async def test_count_project_assets_counts_only_that_project(tmp_path: Path) -> None:
    async with _repo(tmp_path) as repo:
        await _seed_asset_version(repo, asset_id="a1", version_id="v1", sha=SHA_A)
        await _seed_asset_version(repo, asset_id="a2", version_id="v2", sha=SHA_B)
        await repo.add_link(link_id="l1", project_id=PROJECT_A, asset_id="a1", version_id="v1")
        await repo.add_link(link_id="l2", project_id=PROJECT_A, asset_id="a2", version_id="v2")
        await repo.add_link(link_id="l3", project_id=PROJECT_B, asset_id="a1", version_id="v1")

        assert await repo.count_project_assets(PROJECT_A) == 2
        assert await repo.count_project_assets(PROJECT_B) == 1


async def test_count_project_assets_is_zero_for_an_unknown_project(tmp_path: Path) -> None:
    async with _repo(tmp_path) as repo:
        await _seed_asset_version(repo, asset_id="a1", version_id="v1", sha=SHA_A)
        await repo.add_link(link_id="l1", project_id=PROJECT_A, asset_id="a1", version_id="v1")

        assert await repo.count_project_assets("project-nobody") == 0


async def test_get_link_for_project_returns_none_when_absent(tmp_path: Path) -> None:
    async with _repo(tmp_path) as repo:
        await _seed_asset_version(repo, asset_id="a1", version_id="v1", sha=SHA_A)

        assert await repo.get_link_for_project(PROJECT_A, "a1") is None


async def test_get_link_for_project_is_project_scoped(tmp_path: Path) -> None:
    """The same asset linked to two projects resolves to each project's own link."""
    async with _repo(tmp_path) as repo:
        await _seed_asset_version(repo, asset_id="a1", version_id="v1", sha=SHA_A)
        await repo.add_link(link_id="l-a", project_id=PROJECT_A, asset_id="a1", version_id="v1")
        await repo.add_link(link_id="l-b", project_id=PROJECT_B, asset_id="a1", version_id="v1")

        link_a = await repo.get_link_for_project(PROJECT_A, "a1")
        link_b = await repo.get_link_for_project(PROJECT_B, "a1")

        assert link_a is not None and link_a.link_id == "l-a"
        assert link_b is not None and link_b.link_id == "l-b"


async def test_get_link_for_project_returns_the_earliest_link(tmp_path: Path) -> None:
    """Deterministic tie-break: ``created_at`` ascending picks the first link."""
    async with _repo(tmp_path) as repo:
        await repo.add_asset(asset_id="a1", media_kind=MediaKind.IMAGE)
        await repo.add_version(
            version_id="v1",
            asset_id="a1",
            content_sha256=SHA_A,
            size_bytes=10,
            media_kind=MediaKind.IMAGE,
            media_type="image/png",
            blob_key=f"blobs/{SHA_A}",
        )
        await repo.add_version(
            version_id="v2",
            asset_id="a1",
            content_sha256=SHA_B,
            size_bytes=10,
            media_kind=MediaKind.IMAGE,
            media_type="image/png",
            blob_key=f"blobs/{SHA_B}",
        )
        session = repo._session  # the repository's own session, for a controlled clock
        session.add_all(
            [
                orm.ProjectAssetLink(
                    link_id="l-late",
                    project_id=PROJECT_A,
                    asset_id="a1",
                    version_id="v2",
                    created_at=_BASE + timedelta(seconds=5),
                ),
                orm.ProjectAssetLink(
                    link_id="l-early",
                    project_id=PROJECT_A,
                    asset_id="a1",
                    version_id="v1",
                    created_at=_BASE,
                ),
            ]
        )
        await session.flush()

        link = await repo.get_link_for_project(PROJECT_A, "a1")

        assert link is not None and link.link_id == "l-early"


async def test_list_pending_ingests_returns_only_staging_oldest_first(tmp_path: Path) -> None:
    async with _repo(tmp_path) as repo:
        session = repo._session
        session.add_all(
            [
                orm.AssetIngest(
                    ingest_id="i-new",
                    idempotency_key="k-new",
                    project_id=PROJECT_A,
                    request_fingerprint="fp-new",
                    state="STAGING",
                    staging_key="s-new",
                    created_at=_BASE + timedelta(seconds=2),
                    updated_at=_BASE + timedelta(seconds=2),
                ),
                orm.AssetIngest(
                    ingest_id="i-old",
                    idempotency_key="k-old",
                    project_id=PROJECT_A,
                    request_fingerprint="fp-old",
                    state="STAGING",
                    staging_key="s-old",
                    created_at=_BASE,
                    updated_at=_BASE,
                ),
                orm.AssetIngest(
                    ingest_id="i-ready",
                    idempotency_key="k-ready",
                    project_id=PROJECT_A,
                    request_fingerprint="fp-ready",
                    state="READY",
                    created_at=_BASE + timedelta(seconds=1),
                    updated_at=_BASE + timedelta(seconds=1),
                ),
                orm.AssetIngest(
                    ingest_id="i-failed",
                    idempotency_key="k-failed",
                    project_id=PROJECT_A,
                    request_fingerprint="fp-failed",
                    state="FAILED",
                    created_at=_BASE + timedelta(seconds=3),
                    updated_at=_BASE + timedelta(seconds=3),
                ),
            ]
        )
        await session.flush()

        pending = await repo.list_pending_ingests()

        assert [record.ingest_id for record in pending] == ["i-old", "i-new"]


async def test_set_staging_key_raises_for_an_unknown_ingest(tmp_path: Path) -> None:
    """A missing row is a programming error, not a silent no-op."""
    async with _repo(tmp_path) as repo:
        with pytest.raises(LookupError):
            await repo.set_staging_key("does-not-exist", "s")


async def test_mark_failed_is_a_no_op_for_an_unknown_ingest(tmp_path: Path) -> None:
    """``mark_failed`` tolerates a vanished row (idempotent recovery path)."""
    async with _repo(tmp_path) as repo:
        await repo.mark_failed("does-not-exist", error_code="empty_content")
