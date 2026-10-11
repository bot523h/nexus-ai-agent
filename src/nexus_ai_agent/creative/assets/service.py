"""Application service for the Asset Library.

The service is the only orchestration path for ingest, retrieval and recovery.
It composes three collaborators injected at construction:

* a **session factory** yielding the existing async SQLModel session;
* a **content-addressed blob store** (the local adapter by default);
* a trusted **authorization seam** (reusing the Studio ``ProjectAuthorizer``
  shape via an adapter).

Transaction boundaries are explicit because a filesystem publish and a database
commit are *not* one transaction:

    Tx1  create the ingest row            (durable: STAGING, no key yet)
    --   stage bytes on disk              (filesystem side effect)
    Tx2  record staged identity           (durable: STAGING + staging key)
    --   publish blob + flush metadata graph
    Tx3  commit                           (durable: READY)

A crash between any two steps leaves a durable STAGING row that
:func:`~nexus_ai_agent.creative.assets.ingestion.reconcile_ingest` can complete
or explicitly fail.  ``READY`` is returned only after Tx3 succeeds.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import BinaryIO

from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import (
    AssetNotFound,
    ContentIntegrityError,
    EmptyContent,
    IdempotencyConflict,
    IngestNotFound,
    RecoveryError,
    UnsupportedMediaType,
)
from nexus_ai_agent.creative.assets.ingestion import (
    IngestRecoveryReport,
    publish_and_commit,
    reconcile_ingest,
)
from nexus_ai_agent.creative.assets.mime import HEADER_BYTES, detect_media_type
from nexus_ai_agent.creative.assets.models import (
    SUPPORTED_MEDIA_TYPES,
    AssetIdentity,
    AssetVersion,
    IngestRecord,
    IngestRequest,
    IngestResult,
    IngestState,
)
from nexus_ai_agent.creative.assets.ports import AuthorizationSeam, BlobStore
from nexus_ai_agent.creative.assets.repository import SqlAssetRepository
from nexus_ai_agent.storage.db import get_session

#: Permissions checked through the authorization seam.
PERMISSION_WRITE = "project:write"
PERMISSION_READ = "project:read"

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
FaultHook = Callable[[str], None]


def _request_fingerprint(request: IngestRequest) -> str:
    """A stable fingerprint of the *request* (never of the untrusted content)."""
    payload = f"{request.original_filename}\x00{request.declared_media_type or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _result_from(record: IngestRecord, *, reused: bool) -> IngestResult:
    # Only ever called for a READY record, where classification is durably set.
    if record.media_kind is None or record.media_type is None:
        raise RecoveryError("a READY ingest is missing its media classification")
    return IngestResult(
        ingest_id=record.ingest_id,
        asset_id=record.asset_id or "",
        version_id=record.version_id or "",
        content_sha256=record.content_sha256 or "",
        size_bytes=record.size_bytes or 0,
        media_kind=record.media_kind,
        media_type=record.media_type,
        state=record.state,
        reused_result=reused,
    )


class AssetLibraryService:
    """Secure ingest, durable retrieval and explicit recovery for assets."""

    def __init__(
        self,
        *,
        blob_store: BlobStore,
        authorizer: AuthorizationSeam,
        session_factory: SessionFactory | None = None,
        max_bytes: int = 64 * 1024 * 1024,
        fault_hook: FaultHook | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._blob_store = blob_store
        self._authorizer = authorizer
        self._session_factory: SessionFactory = session_factory or (lambda: get_session())
        self._max_bytes = max_bytes
        self._fault_hook = fault_hook

    def _fault(self, point: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point)

    # -- ingest --------------------------------------------------------- #
    async def ingest(
        self,
        *,
        actor_kind: str,
        actor_id: str,
        project_id: str,
        request: IngestRequest,
        stream: BinaryIO,
    ) -> IngestResult:
        """Authorize, then stage, publish and commit one asset ingest."""
        self._authorizer.require(actor_kind, actor_id, project_id, PERMISSION_WRITE)
        fingerprint = _request_fingerprint(request)

        async with self._session_factory() as session:
            repo = SqlAssetRepository(session)

            existing = await repo.find_ingest_by_key(project_id, request.idempotency_key)
            if existing is not None:
                return await self._resolve_existing(repo, session, existing, fingerprint)

            ingest_id = uuid.uuid4().hex
            staging_key = self._blob_store.new_staging_key()
            await repo.create_ingest(
                ingest_id=ingest_id,
                idempotency_key=request.idempotency_key,
                project_id=project_id,
                request_fingerprint=fingerprint,
                staging_key=staging_key,
                original_filename=request.original_filename,
            )
            await session.commit()  # Tx1: durable STAGING row before any fs effect
            self._fault("after_ingest_row")

            content_sha256, size_bytes = self._blob_store.stage_to(
                staging_key, stream, max_bytes=self._max_bytes
            )
            self._fault("after_stage")

            if size_bytes == 0:
                self._blob_store.remove_staging(staging_key)
                await repo.mark_failed(ingest_id, error_code="empty_content")
                await session.commit()
                raise EmptyContent("the stream contained no bytes")

            header = self._blob_store.read_staging_header(staging_key, HEADER_BYTES)
            detected = detect_media_type(header)
            if detected is None or detected not in SUPPORTED_MEDIA_TYPES:
                self._blob_store.remove_staging(staging_key)
                await repo.mark_failed(ingest_id, error_code="unsupported_media_type")
                await session.commit()
                raise UnsupportedMediaType("content is not an allowed media type")

            media_kind = SUPPORTED_MEDIA_TYPES[detected]
            await repo.mark_staged(
                ingest_id,
                staging_key=staging_key,
                content_sha256=content_sha256,
                size_bytes=size_bytes,
                media_kind=media_kind,
                media_type=detected,
            )
            await session.commit()  # Tx2: durable staged identity for recovery

            try:
                asset_id, version_id, deduplicated = await publish_and_commit(
                    repo,
                    self._blob_store,
                    ingest_id=ingest_id,
                    project_id=project_id,
                    staging_key=staging_key,
                    content_sha256=content_sha256,
                    size_bytes=size_bytes,
                    media_kind=media_kind,
                    media_type=detected,
                    original_filename=request.original_filename,
                )
                self._fault("after_publish_before_commit")
                await session.commit()  # Tx3: the single authoritative commit
            except BaseException:
                await session.rollback()
                raise

            return IngestResult(
                ingest_id=ingest_id,
                asset_id=asset_id,
                version_id=version_id,
                content_sha256=content_sha256,
                size_bytes=size_bytes,
                media_kind=media_kind,
                media_type=detected,
                state=IngestState.READY,
                blob_deduplicated=deduplicated,
            )

    async def _resolve_existing(
        self,
        repo: SqlAssetRepository,
        session: AsyncSession,
        existing: IngestRecord,
        fingerprint: str,
    ) -> IngestResult:
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflict("idempotency key was reused with a different request payload")
        if existing.state is IngestState.READY:
            return _result_from(existing, reused=True)
        if existing.state is IngestState.STAGING:
            report = await reconcile_ingest(repo, self._blob_store, existing)
            await session.commit()
            if report.state is IngestState.READY:
                refreshed = await repo.get_ingest(existing.ingest_id)
                assert refreshed is not None
                return _result_from(refreshed, reused=True)
            raise RecoveryError(f"interrupted ingest could not be completed: {report.outcome}")
        raise RecoveryError("this idempotency key belongs to a failed ingest; use a new key")

    # -- retrieval ------------------------------------------------------ #
    async def get_asset(
        self, *, actor_kind: str, actor_id: str, project_id: str, asset_id: str
    ) -> tuple[AssetIdentity, AssetVersion]:
        self._authorizer.require(actor_kind, actor_id, project_id, PERMISSION_READ)
        async with self._session_factory() as session:
            repo = SqlAssetRepository(session)
            link = await repo.get_link_for_project(project_id, asset_id)
            if link is None:
                raise AssetNotFound("no such asset is visible for this project")
            identity = await repo.get_asset(link.asset_id)
            version = await repo.get_version(link.version_id)
            if identity is None or version is None:
                raise AssetNotFound("asset metadata is incomplete")
            return identity, version

    async def list_project_assets(
        self,
        *,
        actor_kind: str,
        actor_id: str,
        project_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[tuple[AssetIdentity, AssetVersion]]:
        self._authorizer.require(actor_kind, actor_id, project_id, PERMISSION_READ)
        if limit < 1 or limit > 500 or offset < 0:
            raise ValueError("invalid pagination window")
        async with self._session_factory() as session:
            repo = SqlAssetRepository(session)
            links = await repo.list_project_assets(project_id, limit=limit, offset=offset)
            out: list[tuple[AssetIdentity, AssetVersion]] = []
            for link in links:
                identity = await repo.get_asset(link.asset_id)
                version = await repo.get_version(link.version_id)
                if identity is not None and version is not None:
                    out.append((identity, version))
            return out

    async def read_content(
        self, *, actor_kind: str, actor_id: str, project_id: str, asset_id: str
    ) -> bytes:
        """Return the stored bytes after re-verifying them against the record."""
        _, version = await self.get_asset(
            actor_kind=actor_kind, actor_id=actor_id, project_id=project_id, asset_id=asset_id
        )
        if not self._blob_store.verify(
            version.blob_key,
            sha256=version.content_sha256,
            size_bytes=version.size_bytes,
        ):
            raise ContentIntegrityError("stored content does not match the asset version")
        return self._blob_store.open(version.blob_key)

    # -- recovery ------------------------------------------------------- #
    async def recover_ingest(self, ingest_id: str) -> IngestRecoveryReport:
        async with self._session_factory() as session:
            repo = SqlAssetRepository(session)
            record = await repo.get_ingest(ingest_id)
            if record is None:
                raise IngestNotFound(ingest_id)
            report = await reconcile_ingest(repo, self._blob_store, record)
            await session.commit()
            return report

    async def recover_all(self) -> list[IngestRecoveryReport]:
        """Reconcile every durable STAGING ingest; never invents success."""
        async with self._session_factory() as session:
            repo = SqlAssetRepository(session)
            pending = await repo.list_pending_ingests()
            reports: list[IngestRecoveryReport] = []
            for record in pending:
                reports.append(await reconcile_ingest(repo, self._blob_store, record))
            await session.commit()
            return reports


__all__ = [
    "AssetLibraryService",
    "LocalContentAddressedBlobStore",
    "PERMISSION_READ",
    "PERMISSION_WRITE",
]
