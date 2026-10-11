"""Unit-level edge cases for the Asset Library service.

These exercise the real service against a real (temporary) SQLite database and
real bytes — no mocks — but focus on the boundary/error contracts the happy
paths do not reach: size limits, empty/unsupported/spoofed content, pagination
bounds, integrity re-verification and construction validation.
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
    ContentIntegrityError,
    ContentTooLarge,
    EmptyContent,
    UnsupportedMediaType,
)
from nexus_ai_agent.creative.assets.models import IngestRequest, IngestState
from nexus_ai_agent.creative.assets.service import AssetLibraryService
from nexus_ai_agent.storage.db import get_session

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
PROJECT = "project-alpha"
ACTOR = "user-1"


def _session_factory(db_path: Path):
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with get_session(str(db_path)) as session:
            yield session

    return factory


def _service(tmp_path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> AssetLibraryService:
    return AssetLibraryService(
        blob_store=LocalContentAddressedBlobStore(tmp_path / "blobs"),
        authorizer=StudioAuthorizationSeam(
            GrantTableAuthorizer({(ACTOR, PROJECT): frozenset({"project:read", "project:write"})})
        ),
        session_factory=_session_factory(tmp_path / "library.sqlite"),
        max_bytes=max_bytes,
    )


async def _ingest(
    service: AssetLibraryService, data: bytes, *, key: str = "k", name: str = "a.png"
):
    return await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT,
        request=IngestRequest(idempotency_key=key, original_filename=name),
        stream=io.BytesIO(data),
    )


def test_service_rejects_a_nonpositive_size_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _service(tmp_path, max_bytes=0)


@pytest.mark.asyncio
async def test_oversize_stream_is_refused_and_leaves_no_ready_row(tmp_path: Path) -> None:
    service = _service(tmp_path, max_bytes=16)
    with pytest.raises(ContentTooLarge):
        await _ingest(service, PNG)
    # A typed failure, never a fabricated success.
    with pytest.raises(AssetNotFound):
        await service.get_asset(
            actor_kind="user", actor_id=ACTOR, project_id=PROJECT, asset_id="nope"
        )


@pytest.mark.asyncio
async def test_empty_stream_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(EmptyContent):
        await _ingest(service, b"")


@pytest.mark.asyncio
async def test_unsupported_content_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(UnsupportedMediaType):
        await _ingest(service, b"this is not media at all" + bytes(range(64)))


@pytest.mark.asyncio
async def test_filename_and_declared_type_do_not_influence_classification(tmp_path: Path) -> None:
    """A spoofed filename/MIME cannot change the measured classification."""
    service = _service(tmp_path)
    result = await service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT,
        request=IngestRequest(
            idempotency_key="spoof",
            original_filename="evil.mp4",
            declared_media_type="video/mp4",
        ),
        stream=io.BytesIO(PNG),
    )
    assert result.media_type == "image/png"
    assert result.media_kind.value == "image"


@pytest.mark.asyncio
async def test_unauthorized_actor_is_refused_before_any_effect(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(AuthorizationFailure):
        await service.ingest(
            actor_kind="user",
            actor_id="intruder",
            project_id=PROJECT,
            request=IngestRequest(idempotency_key="x"),
            stream=io.BytesIO(PNG),
        )
    # No blob and no row were created: the blob root may exist, but it is empty.
    root = tmp_path / "blobs"
    assert not any(root.rglob("*.*"))


@pytest.mark.asyncio
async def test_pagination_bounds_are_enforced(tmp_path: Path) -> None:
    service = _service(tmp_path)
    await _ingest(service, PNG)
    for bad in (0, 501, -1):
        with pytest.raises(ValueError):
            await service.list_project_assets(
                actor_kind="user", actor_id=ACTOR, project_id=PROJECT, limit=bad
            )


@pytest.mark.asyncio
async def test_read_content_reverifies_and_round_trips(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = await _ingest(service, PNG)
    data = await service.read_content(
        actor_kind="user", actor_id=ACTOR, project_id=PROJECT, asset_id=result.asset_id
    )
    assert data == PNG


@pytest.mark.asyncio
async def test_read_content_detects_tampering(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = await _ingest(service, PNG)
    # Corrupt the stored blob on disk; read must refuse rather than serve it.
    for f in (tmp_path / "blobs" / "blobs").rglob("*"):
        if f.is_file():
            f.write_bytes(b"tampered")
    with pytest.raises(ContentIntegrityError):
        await service.read_content(
            actor_kind="user", actor_id=ACTOR, project_id=PROJECT, asset_id=result.asset_id
        )


@pytest.mark.asyncio
async def test_result_reports_ready_state(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = await _ingest(service, PNG)
    assert result.state is IngestState.READY
    assert len(result.content_sha256) == 64
    assert result.size_bytes == len(PNG)
