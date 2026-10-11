"""SQLModel-backed metadata repository for the Asset Library.

Uses the repository's existing session/engine conventions
(:func:`nexus_ai_agent.storage.db.get_session`) — there is no second database
engine and no parallel persistence framework.  The tables are declared once, in
:mod:`nexus_ai_agent.storage.models`, and this module only maps rows to the
immutable domain contracts.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import Table, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets.models import (
    AssetIdentity,
    AssetVersion,
    IngestRecord,
    IngestState,
    MediaKind,
    ProjectAssetLink,
)
from nexus_ai_agent.storage import models as orm

# Typed table accessors (SQLAlchemy 2.0 style) so column expressions stay typed
# without importing SQLModel into this module — the architecture ratchet forbids
# new ``sqlmodel`` imports outside the frozen baseline.
_INGEST: Table = cast(Table, orm.AssetIngest.__table__)  # type: ignore[attr-defined]
_LINK: Table = cast(Table, orm.ProjectAssetLink.__table__)  # type: ignore[attr-defined]


def _as_version(row: orm.AssetVersion) -> AssetVersion:
    return AssetVersion(
        version_id=row.version_id,
        asset_id=row.asset_id,
        content_sha256=row.content_sha256,
        size_bytes=row.size_bytes,
        media_kind=MediaKind(row.media_kind),
        media_type=row.media_type,
        blob_key=row.blob_key,
        original_filename=row.original_filename,
        created_at=row.created_at,
    )


def _as_identity(row: orm.Asset) -> AssetIdentity:
    return AssetIdentity(
        asset_id=row.asset_id,
        media_kind=MediaKind(row.media_kind),
        created_at=row.created_at,
    )


def _as_link(row: orm.ProjectAssetLink) -> ProjectAssetLink:
    return ProjectAssetLink(
        link_id=row.link_id,
        project_id=row.project_id,
        asset_id=row.asset_id,
        version_id=row.version_id,
        created_at=row.created_at,
    )


def _as_ingest(row: orm.AssetIngest) -> IngestRecord:
    return IngestRecord(
        ingest_id=row.ingest_id,
        idempotency_key=row.idempotency_key,
        project_id=row.project_id,
        request_fingerprint=row.request_fingerprint,
        state=IngestState(row.state),
        media_kind=MediaKind(row.media_kind) if row.media_kind else None,
        media_type=row.media_type,
        original_filename=row.original_filename,
        content_sha256=row.content_sha256,
        size_bytes=row.size_bytes,
        blob_key=row.blob_key,
        staging_key=row.staging_key,
        asset_id=row.asset_id,
        version_id=row.version_id,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAssetRepository:
    """Durable metadata operations over an injected :class:`AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- ingest --------------------------------------------------------- #
    async def create_ingest(
        self,
        *,
        ingest_id: str,
        idempotency_key: str,
        project_id: str,
        request_fingerprint: str,
        staging_key: str,
        original_filename: str = "",
    ) -> IngestRecord:
        row = orm.AssetIngest(
            ingest_id=ingest_id,
            idempotency_key=idempotency_key,
            project_id=project_id,
            request_fingerprint=request_fingerprint,
            state=IngestState.STAGING.value,
            original_filename=original_filename,
            # Recorded before the bytes are written, so a crash mid-write still
            # leaves a recoverable staging key.
            staging_key=staging_key,
        )
        self._session.add(row)
        await self._session.flush()
        return _as_ingest(row)

    async def get_ingest(self, ingest_id: str) -> IngestRecord | None:
        row = await self._session.get(orm.AssetIngest, ingest_id)
        return _as_ingest(row) if row else None

    async def find_ingest_by_key(
        self, project_id: str, idempotency_key: str
    ) -> IngestRecord | None:
        result = await self._session.execute(
            select(orm.AssetIngest)
            .where(
                _INGEST.c.project_id == project_id,
                _INGEST.c.idempotency_key == idempotency_key,
            )
            .order_by(_INGEST.c.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return _as_ingest(row) if row else None

    async def list_pending_ingests(self) -> list[IngestRecord]:
        result = await self._session.execute(
            select(orm.AssetIngest)
            .where(_INGEST.c.state == IngestState.STAGING.value)
            .order_by(_INGEST.c.created_at.asc())
        )
        return [_as_ingest(row) for row in result.scalars().all()]

    async def mark_staged(
        self,
        ingest_id: str,
        *,
        staging_key: str,
        content_sha256: str,
        size_bytes: int,
        media_kind: MediaKind,
        media_type: str,
    ) -> None:
        row = await self._session.get(orm.AssetIngest, ingest_id)
        if row is None:
            raise LookupError(ingest_id)
        row.staging_key = staging_key
        row.content_sha256 = content_sha256
        row.size_bytes = size_bytes
        row.media_kind = media_kind.value
        row.media_type = media_type
        row.updated_at = datetime.utcnow()
        await self._session.flush()

    async def mark_failed(self, ingest_id: str, *, error_code: str) -> None:
        row = await self._session.get(orm.AssetIngest, ingest_id)
        if row is None:
            return
        row.state = IngestState.FAILED.value
        row.error_code = error_code
        row.staging_key = None
        row.updated_at = datetime.utcnow()
        await self._session.flush()

    async def complete_ingest(
        self,
        ingest_id: str,
        *,
        asset_id: str,
        version_id: str,
        blob_key: str,
        media_kind: MediaKind,
        media_type: str,
    ) -> None:
        """The single terminal transition that makes an ingest READY."""
        row = await self._session.get(orm.AssetIngest, ingest_id)
        if row is None:
            raise LookupError(ingest_id)
        row.state = IngestState.READY.value
        row.asset_id = asset_id
        row.version_id = version_id
        row.blob_key = blob_key
        # Recovery finalises an ingest whose Tx2 never committed, so the
        # classification is (re)written here too — never left NULL on a READY row.
        row.media_kind = media_kind.value
        row.media_type = media_type
        row.staging_key = None
        row.error_code = None
        row.updated_at = datetime.utcnow()
        await self._session.flush()

    # -- asset / version / link ----------------------------------------- #
    async def add_asset(self, *, asset_id: str, media_kind: MediaKind) -> AssetIdentity:
        row = orm.Asset(asset_id=asset_id, media_kind=media_kind.value)
        self._session.add(row)
        await self._session.flush()
        return _as_identity(row)

    async def get_asset(self, asset_id: str) -> AssetIdentity | None:
        row = await self._session.get(orm.Asset, asset_id)
        return _as_identity(row) if row else None

    async def add_version(
        self,
        *,
        version_id: str,
        asset_id: str,
        content_sha256: str,
        size_bytes: int,
        media_kind: MediaKind,
        media_type: str,
        blob_key: str,
        original_filename: str = "",
    ) -> AssetVersion:
        row = orm.AssetVersion(
            version_id=version_id,
            asset_id=asset_id,
            content_sha256=content_sha256,
            size_bytes=size_bytes,
            media_kind=media_kind.value,
            media_type=media_type,
            blob_key=blob_key,
            original_filename=original_filename,
        )
        self._session.add(row)
        await self._session.flush()
        return _as_version(row)

    async def get_version(self, version_id: str) -> AssetVersion | None:
        row = await self._session.get(orm.AssetVersion, version_id)
        return _as_version(row) if row else None

    async def add_link(
        self, *, link_id: str, project_id: str, asset_id: str, version_id: str
    ) -> ProjectAssetLink:
        row = orm.ProjectAssetLink(
            link_id=link_id,
            project_id=project_id,
            asset_id=asset_id,
            version_id=version_id,
        )
        self._session.add(row)
        await self._session.flush()
        return _as_link(row)

    async def get_link_for_project(self, project_id: str, asset_id: str) -> ProjectAssetLink | None:
        result = await self._session.execute(
            select(orm.ProjectAssetLink)
            .where(
                _LINK.c.project_id == project_id,
                _LINK.c.asset_id == asset_id,
            )
            .order_by(_LINK.c.created_at.asc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return _as_link(row) if row else None

    async def list_project_assets(
        self, project_id: str, *, limit: int, offset: int
    ) -> list[ProjectAssetLink]:
        result = await self._session.execute(
            select(orm.ProjectAssetLink)
            .where(_LINK.c.project_id == project_id)
            .order_by(_LINK.c.created_at.asc(), _LINK.c.link_id.asc())
            .limit(limit)
            .offset(offset)
        )
        return [_as_link(row) for row in result.scalars().all()]

    async def count_project_assets(self, project_id: str) -> int:
        result = await self._session.execute(
            select(func.count(literal_column("link_id")))
            .select_from(orm.ProjectAssetLink)
            .where(_LINK.c.project_id == project_id)
        )
        return int(result.scalar_one())
