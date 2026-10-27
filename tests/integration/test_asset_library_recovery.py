"""Integration tests: durable ingest recovery across a simulated crash.

Crashes are *injected* at exact boundaries through the service's fault hook,
never faked with a comment.  Each test then rebuilds the service (a restart)
over the same database file and blob root and asserts the recovery outcome.

Invariant under test: filesystem existence is **not** authoritative completion,
and recovery never invents success.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets.authz import GrantTableAuthorizer, StudioAuthorizationSeam
from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import IngestNotFound
from nexus_ai_agent.creative.assets.ingestion import RecoveryOutcome
from nexus_ai_agent.creative.assets.models import IngestRequest, IngestState
from nexus_ai_agent.creative.assets.service import AssetLibraryService
from nexus_ai_agent.storage import models as orm
from nexus_ai_agent.storage.db import get_session

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(96))
PROJECT = "project-alpha"
ACTOR = "user-1"


def _authorizer() -> StudioAuthorizationSeam:
    return StudioAuthorizationSeam(
        GrantTableAuthorizer({(ACTOR, PROJECT): frozenset({"project:read", "project:write"})})
    )


def _session_factory(db_path: Path):
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with get_session(str(db_path)) as session:
            yield session

    return factory


def _service(tmp_path: Path, *, fault: str | None = None) -> AssetLibraryService:
    def hook(point: str) -> None:
        if fault is not None and point == fault:
            raise RuntimeError(f"injected crash at {point}")

    return AssetLibraryService(
        blob_store=LocalContentAddressedBlobStore(tmp_path / "blobs"),
        authorizer=_authorizer(),
        session_factory=_session_factory(tmp_path / "library.sqlite"),
        fault_hook=hook,
    )


async def _only_ingest(tmp_path: Path) -> orm.AssetIngest:
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        row = (
            (await session.execute(select(orm.AssetIngest).order_by(orm.AssetIngest.created_at)))
            .scalars()
            .one()
        )
        return row


async def _counts(tmp_path: Path) -> tuple[int, int, int]:
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        assets = (await session.execute(select(func.count()).select_from(orm.Asset))).scalar_one()
        versions = (
            await session.execute(select(func.count()).select_from(orm.AssetVersion))
        ).scalar_one()
        links = (
            await session.execute(select(func.count()).select_from(orm.ProjectAssetLink))
        ).scalar_one()
        return assets, versions, links


def _ingest(service: AssetLibraryService, key: str = "k"):
    return service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT,
        request=IngestRequest(idempotency_key=key, original_filename="pic.png"),
        stream=io.BytesIO(PNG),
    )


@pytest.mark.asyncio
async def test_crash_after_ingest_row_recovers_to_failed_not_success(tmp_path: Path) -> None:
    """C1/C2: a durable row exists but no bytes were staged — never a false READY."""
    service = _service(tmp_path, fault="after_ingest_row")
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(service)

    row = await _only_ingest(tmp_path)
    assert row.state == IngestState.STAGING.value
    assert row.staging_key  # recorded before the crash

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(row.ingest_id)
    assert report.state is IngestState.FAILED
    assert report.outcome is RecoveryOutcome.FAILED
    assert await _counts(tmp_path) == (0, 0, 0)  # no fabricated asset


@pytest.mark.asyncio
async def test_crash_after_stage_recovers_to_ready(tmp_path: Path) -> None:
    """C2: staged bytes are recoverable — the single completion path finalises them."""
    service = _service(tmp_path, fault="after_stage")
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(service)

    row = await _only_ingest(tmp_path)
    assert row.state == IngestState.STAGING.value
    assert row.content_sha256 is None  # Tx2 had not committed yet

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(row.ingest_id)
    assert report.state is IngestState.READY
    assert report.outcome is RecoveryOutcome.COMPLETED
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_crash_after_publish_before_commit_recovers_to_ready(tmp_path: Path) -> None:
    """C4: the physical artifact exists but is not authoritative until the commit."""
    service = _service(tmp_path, fault="after_publish_before_commit")
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(service)

    # The blob is on disk, but the DB has no asset yet: existence != authority.
    assert await _counts(tmp_path) == (0, 0, 0)
    row = await _only_ingest(tmp_path)
    assert row.state == IngestState.STAGING.value

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(row.ingest_id)
    assert report.state is IngestState.READY
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_crash_after_commit_is_already_ready_and_not_re_executed(tmp_path: Path) -> None:
    """C5: a completed ingest is observed COMPLETED and never duplicated."""
    service = _service(tmp_path)
    result = await _ingest(service)
    assert result.state is IngestState.READY

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(result.ingest_id)
    assert report.outcome is RecoveryOutcome.ALREADY_READY
    assert await _counts(tmp_path) == (1, 1, 1)

    # A retry with the same key is served from the durable result.
    again = await _ingest(restarted)
    assert again.asset_id == result.asset_id
    assert again.reused_result is True
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_ready_row_with_missing_blob_is_downgraded_to_failed(tmp_path: Path) -> None:
    """A READY row whose bytes vanished must never stay falsely READY."""
    service = _service(tmp_path)
    result = await _ingest(service)

    # Remove the physical blob behind the metadata's back.
    blob_dir = tmp_path / "blobs" / "blobs"
    for f in blob_dir.rglob("*"):
        if f.is_file():
            f.unlink()

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(result.ingest_id)
    assert report.state is IngestState.FAILED
    assert report.outcome is RecoveryOutcome.MISSING_BLOB


@pytest.mark.asyncio
async def test_unknown_ingest_is_not_fabricated(tmp_path: Path) -> None:
    """C8: recovery of an unknown identity refuses rather than inventing success."""
    service = _service(tmp_path)
    with pytest.raises(IngestNotFound):
        await service.recover_ingest("does-not-exist")


@pytest.mark.asyncio
async def test_recover_all_only_touches_pending_rows(tmp_path: Path) -> None:
    service = _service(tmp_path, fault="after_stage")
    with pytest.raises(RuntimeError):
        await _ingest(service, key="pending")

    # A second, fully-completed ingest must be left untouched by recover_all.
    good = _service(tmp_path)
    done = await _ingest(good, key="done")

    restarted = _service(tmp_path)
    reports = await restarted.recover_all()
    assert [r.outcome for r in reports] == [RecoveryOutcome.COMPLETED]
    assert await _counts(tmp_path) == (2, 2, 2)
    # The previously completed ingest is still exactly one asset.
    identity, _ = await restarted.get_asset(
        actor_kind="user", actor_id=ACTOR, project_id=PROJECT, asset_id=done.asset_id
    )
    assert identity.asset_id == done.asset_id


@pytest.mark.asyncio
async def test_retry_of_interrupted_ingest_completes_it(tmp_path: Path) -> None:
    """A retry with the same key recovers the interrupted ingest rather than duplicating."""
    service = _service(tmp_path, fault="after_stage")
    with pytest.raises(RuntimeError):
        await _ingest(service, key="retry")

    restarted = _service(tmp_path)
    result = await _ingest(restarted, key="retry")
    assert result.state is IngestState.READY
    assert result.reused_result is True
    assert await _counts(tmp_path) == (1, 1, 1)
