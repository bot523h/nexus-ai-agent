"""Integration tests: durable persistence, authorization and idempotency.

Every test drives the real service against real SQLite rows and real bytes on a
real temporary filesystem.  "Restart" means brand-new service/repository/session
instances over the same database file and blob root — never a reused in-memory
object.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets.authz import GrantTableAuthorizer, StudioAuthorizationSeam
from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import (
    AssetNotFound,
    AuthorizationFailure,
    ContentTooLarge,
    IdempotencyConflict,
    RecoveryError,
)
from nexus_ai_agent.creative.assets.models import IngestRequest, IngestState
from nexus_ai_agent.creative.assets.service import AssetLibraryService
from nexus_ai_agent.storage import models as orm
from nexus_ai_agent.storage.db import get_session

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(64))
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(range(64))

PROJECT_A = "project-alpha"
PROJECT_B = "project-beta"
ACTOR = "user-1"


def _grants() -> StudioAuthorizationSeam:
    return StudioAuthorizationSeam(
        GrantTableAuthorizer(
            {
                (ACTOR, PROJECT_A): frozenset({"project:read", "project:write"}),
                (ACTOR, PROJECT_B): frozenset({"project:read", "project:write"}),
            }
        )
    )


def _session_factory(db_path: Path):
    """A fresh per-call session factory over an isolated SQLite engine."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with get_session(str(db_path)) as session:
            yield session

    return factory


def _new_service(tmp_path: Path) -> AssetLibraryService:
    """A brand-new service over the same db file and blob root (a restart)."""
    return AssetLibraryService(
        blob_store=LocalContentAddressedBlobStore(tmp_path / "blobs"),
        authorizer=_grants(),
        session_factory=_session_factory(tmp_path / "library.sqlite"),
    )


def _oversize_service(tmp_path: Path) -> AssetLibraryService:
    """A service whose content limit is one byte below the PNG fixture."""
    return AssetLibraryService(
        blob_store=LocalContentAddressedBlobStore(tmp_path / "blobs"),
        authorizer=_grants(),
        session_factory=_session_factory(tmp_path / "library.sqlite"),
        max_bytes=len(PNG) - 1,
    )


@pytest.mark.asyncio
async def test_ingest_persists_identity_version_and_project_link(tmp_path: Path) -> None:
    service = _new_service(tmp_path)
    result = await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT_A,
        request=IngestRequest(idempotency_key="k1", original_filename="pic.png"),
        stream=io.BytesIO(PNG),
    )
    assert result.state is IngestState.READY
    assert result.size_bytes == len(PNG)
    assert len(result.content_sha256) == 64

    identity, version = await service.get_asset(
        actor_kind="user", actor_id=ACTOR, project_id=PROJECT_A, asset_id=result.asset_id
    )
    assert identity.asset_id == result.asset_id
    assert version.content_sha256 == result.content_sha256
    assert version.size_bytes == len(PNG)
    assert version.original_filename == "pic.png"
    # blob_key is content-derived, never the caller filename.
    assert version.blob_key.startswith("blobs/")
    assert "pic.png" not in version.blob_key


@pytest.mark.asyncio
async def test_restart_preserves_asset_version_link_and_hash(tmp_path: Path) -> None:
    service = _new_service(tmp_path)
    result = await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT_A,
        request=IngestRequest(idempotency_key="k-restart", original_filename="pic.png"),
        stream=io.BytesIO(PNG),
    )

    # Restart: brand-new service + repository + session instances.
    restarted = _new_service(tmp_path)
    identity, version = await restarted.get_asset(
        actor_kind="user", actor_id=ACTOR, project_id=PROJECT_A, asset_id=result.asset_id
    )
    assert identity.asset_id == result.asset_id
    assert version.content_sha256 == result.content_sha256
    assert version.size_bytes == len(PNG)
    assert (
        await restarted.read_content(
            actor_kind="user", actor_id=ACTOR, project_id=PROJECT_A, asset_id=result.asset_id
        )
        == PNG
    )


@pytest.mark.asyncio
async def test_cross_project_access_is_refused(tmp_path: Path) -> None:
    service = _new_service(tmp_path)
    result = await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT_A,
        request=IngestRequest(idempotency_key="k-x", original_filename="pic.png"),
        stream=io.BytesIO(PNG),
    )
    # The same actor may not read project-alpha's asset via project-beta scope.
    with pytest.raises(AssetNotFound):
        await service.get_asset(
            actor_kind="user",
            actor_id=ACTOR,
            project_id=PROJECT_B,
            asset_id=result.asset_id,
        )


@pytest.mark.asyncio
async def test_unauthorized_actor_creates_no_side_effect(tmp_path: Path) -> None:
    service = _new_service(tmp_path)
    with pytest.raises(AuthorizationFailure):
        await service.ingest(
            actor_kind="user",
            actor_id="intruder",
            project_id=PROJECT_A,
            request=IngestRequest(idempotency_key="k-intrude", original_filename="pic.png"),
            stream=io.BytesIO(PNG),
        )
    # No blob, no metadata row: authorization precedes every write-side effect.
    blobs_dir = tmp_path / "blobs" / "blobs"
    assert not blobs_dir.exists() or not any(p.is_file() for p in blobs_dir.rglob("*"))
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        from sqlalchemy import func, select

        count = (
            await session.execute(select(func.count()).select_from(orm.AssetIngest))
        ).scalar_one()
        assert count == 0


@pytest.mark.asyncio
async def test_oversize_upload_fails_the_ingest_row_and_leaves_no_residue(
    tmp_path: Path,
) -> None:
    """An upload above the limit is refused *and* durably recorded as FAILED.

    Before the fix the Tx1 row stayed STAGING after ContentTooLarge, so a later
    recovery pass reconciled an already-refused request and mislabelled it
    ``staging_lost``.  The row must fail now, with the precise error code.
    """
    service = _oversize_service(tmp_path)
    with pytest.raises(ContentTooLarge):
        await service.ingest(
            actor_kind="user",
            actor_id=ACTOR,
            project_id=PROJECT_A,
            request=IngestRequest(idempotency_key="k-too-big", original_filename="pic.png"),
            stream=io.BytesIO(PNG),
        )

    async with get_session(str(tmp_path / "library.sqlite")) as session:
        from sqlalchemy import select

        rows = (await session.execute(select(orm.AssetIngest))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.state == IngestState.FAILED.value
        assert row.error_code == "content_too_large"
        assert row.staging_key is None
        assert row.asset_id is None and row.version_id is None

        # No half-published artifact may be recorded as success.
        assert (await session.execute(select(orm.Asset))).scalars().first() is None
        assert (await session.execute(select(orm.AssetVersion))).scalars().first() is None
        assert (await session.execute(select(orm.ProjectAssetLink))).scalars().first() is None

    # No staging residue and no published blob survive the refusal.
    staging_dir = tmp_path / "blobs" / "staging"
    assert not any(p.is_file() for p in staging_dir.rglob("*"))
    blobs_dir = tmp_path / "blobs" / "blobs"
    assert not blobs_dir.exists() or not any(p.is_file() for p in blobs_dir.rglob("*"))

    # The listing surface never shows the refused upload.
    listed = await _new_service(tmp_path).list_project_assets(
        actor_kind="user", actor_id=ACTOR, project_id=PROJECT_A
    )
    assert listed == []


@pytest.mark.asyncio
async def test_replay_of_an_oversize_failed_key_is_a_clean_refusal(tmp_path: Path) -> None:
    """The burned key stays terminal: a replay is refused outright instead of
    reconciling (or succeeding against) the already-refused request."""
    service = _oversize_service(tmp_path)
    request = IngestRequest(idempotency_key="k-too-big-replay", original_filename="pic.png")
    with pytest.raises(ContentTooLarge):
        await service.ingest(
            actor_kind="user",
            actor_id=ACTOR,
            project_id=PROJECT_A,
            request=request,
            stream=io.BytesIO(PNG),
        )
    # Even a *small* replay under the failed key must not resurrect the ingest.
    restarted = _new_service(tmp_path)  # limit restored; replay is within it
    with pytest.raises(RecoveryError):
        await restarted.ingest(
            actor_kind="user",
            actor_id=ACTOR,
            project_id=PROJECT_A,
            request=request,
            stream=io.BytesIO(PNG),
        )


@pytest.mark.asyncio
async def test_repeated_key_same_payload_is_one_result(tmp_path: Path) -> None:
    service = _new_service(tmp_path)
    request = IngestRequest(idempotency_key="dup", original_filename="pic.png")
    first = await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT_A,
        request=request,
        stream=io.BytesIO(PNG),
    )
    second = await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT_A,
        request=request,
        stream=io.BytesIO(PNG),
    )
    assert first.asset_id == second.asset_id
    assert first.version_id == second.version_id
    assert second.reused_result is True
    # Exactly one logical asset and one link exist.
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        from sqlalchemy import func, select

        assets = (await session.execute(select(func.count()).select_from(orm.Asset))).scalar_one()
        links = (
            await session.execute(select(func.count()).select_from(orm.ProjectAssetLink))
        ).scalar_one()
        assert (assets, links) == (1, 1)


@pytest.mark.asyncio
async def test_reused_key_with_different_payload_is_a_conflict(tmp_path: Path) -> None:
    service = _new_service(tmp_path)
    await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT_A,
        request=IngestRequest(idempotency_key="conf", original_filename="pic.png"),
        stream=io.BytesIO(PNG),
    )
    with pytest.raises(IdempotencyConflict):
        await service.ingest(
            actor_kind="user",
            actor_id=ACTOR,
            project_id=PROJECT_A,
            request=IngestRequest(idempotency_key="conf", original_filename="other.jpg"),
            stream=io.BytesIO(JPEG),
        )


@pytest.mark.asyncio
async def test_reused_key_different_bytes_same_filename_is_a_conflict(tmp_path: Path) -> None:
    """The idempotency fingerprint includes the measured content digest.

    Same filename and declared type, but different bytes: the replay must be a
    typed conflict rather than a silent return of the earlier asset.
    """
    service = _new_service(tmp_path)
    request = IngestRequest(idempotency_key="bytes", original_filename="pic.png")
    await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT_A,
        request=request,
        stream=io.BytesIO(PNG),
    )
    with pytest.raises(IdempotencyConflict):
        await service.ingest(
            actor_kind="user",
            actor_id=ACTOR,
            project_id=PROJECT_A,
            request=request,
            stream=io.BytesIO(PNG + b"different"),
        )


@pytest.mark.asyncio
async def test_same_key_second_insert_hits_the_unique_constraint(tmp_path: Path) -> None:
    """A database UNIQUE(project_id, idempotency_key) backs scoped idempotency.

    Two inserts in distinct sessions over the same file; the second must fail at
    commit, so concurrent same-key ingests cannot create two asset rows.
    """
    from sqlalchemy import func, select
    from sqlalchemy.exc import IntegrityError

    from nexus_ai_agent.creative.assets.repository import SqlAssetRepository

    db = str(tmp_path / "library.sqlite")
    async with get_session(db) as session:
        repo = SqlAssetRepository(session)
        await repo.create_ingest(
            ingest_id="ing-a",
            idempotency_key="race",
            project_id=PROJECT_A,
            request_fingerprint="fp",
            staging_key="staging/a",
        )
        await session.commit()

    with pytest.raises(IntegrityError):
        async with get_session(db) as session:
            repo = SqlAssetRepository(session)
            await repo.create_ingest(
                ingest_id="ing-b",
                idempotency_key="race",
                project_id=PROJECT_A,
                request_fingerprint="fp",
                staging_key="staging/b",
            )
            await session.commit()

    async with get_session(db) as session:
        count = (
            await session.execute(select(func.count()).select_from(orm.AssetIngest))
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_listing_is_project_scoped_ordered_and_bounded(tmp_path: Path) -> None:
    service = _new_service(tmp_path)
    ids = []
    for i, blob in enumerate([PNG, JPEG, WAV]):
        r = await service.ingest(
            actor_kind="user",
            actor_id=ACTOR,
            project_id=PROJECT_A,
            request=IngestRequest(idempotency_key=f"l{i}", original_filename=f"f{i}"),
            stream=io.BytesIO(blob + bytes([i])),
        )
        ids.append(r.asset_id)
    listed = await service.list_project_assets(
        actor_kind="user", actor_id=ACTOR, project_id=PROJECT_A, limit=2, offset=0
    )
    assert len(listed) == 2
    assert [i.asset_id for i, _ in listed] == ids[:2]
    page2 = await service.list_project_assets(
        actor_kind="user", actor_id=ACTOR, project_id=PROJECT_A, limit=2, offset=2
    )
    assert [i.asset_id for i, _ in page2] == ids[2:]


@pytest.mark.asyncio
async def test_same_bytes_across_projects_deduplicate_the_blob_but_not_access(
    tmp_path: Path,
) -> None:
    service = _new_service(tmp_path)
    a = await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT_A,
        request=IngestRequest(idempotency_key="a", original_filename="a.png"),
        stream=io.BytesIO(PNG),
    )
    b = await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT_B,
        request=IngestRequest(idempotency_key="b", original_filename="b.png"),
        stream=io.BytesIO(PNG),
    )
    # Identical bytes: the second ingest reports blob deduplication...
    assert b.blob_deduplicated is True
    assert a.asset_id != b.asset_id
    # ...but sharing bytes does not merge project ownership.
    with pytest.raises(AssetNotFound):
        await service.get_asset(
            actor_kind="user", actor_id=ACTOR, project_id=PROJECT_A, asset_id=b.asset_id
        )
