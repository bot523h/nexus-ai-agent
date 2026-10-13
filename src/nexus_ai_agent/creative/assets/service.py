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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import (
    AssetNotFound,
    BlobStoreError,
    ContentIntegrityError,
    ContentTooLarge,
    EmptyContent,
    IdempotencyConflict,
    IngestAlreadyResolved,
    IngestNotFound,
    RecoveryError,
    UnsupportedMediaType,
)
from nexus_ai_agent.creative.assets.ingestion import (
    IngestRecoveryReport,
    RecoveryOutcome,
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
                return await self._resolve_existing(repo, session, existing, fingerprint, stream)

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
            try:
                await session.commit()  # Tx1: durable STAGING row before any fs effect
            except IntegrityError:
                # A concurrent same-key ingest won the UNIQUE(project_id,
                # idempotency_key) race.  Drop our row and resolve against theirs
                # instead of creating a second asset.
                await session.rollback()
                existing = await repo.find_ingest_by_key(project_id, request.idempotency_key)
                if existing is None:
                    raise
                return await self._resolve_existing(repo, session, existing, fingerprint, stream)
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
        stream: BinaryIO,
    ) -> IngestResult:
        """Resolve a replayed idempotency key against the existing ingest.

        The replay is only honoured when the incoming bytes are the *same
        content*: the stream is staged (or, for a READY record, hashed without a
        filesystem effect) and its measured digest is compared with the stored
        digest.  A reused key with different bytes is a typed conflict, never a
        silent successful return of the earlier result.
        """
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflict("idempotency key was reused with a different request payload")

        if existing.state is IngestState.READY:
            observed = self._blob_store.hash_stream(stream, max_bytes=self._max_bytes)
            if observed != (existing.content_sha256, existing.size_bytes):
                raise IdempotencyConflict("idempotency key was reused with different content")
            return _result_from(existing, reused=True)

        if existing.state is IngestState.STAGING:
            return await self._resolve_interrupted(repo, session, existing, stream)
        raise RecoveryError("this idempotency key belongs to a failed ingest; use a new key")

    async def _resolve_interrupted(
        self,
        repo: SqlAssetRepository,
        session: AsyncSession,
        existing: IngestRecord,
        stream: BinaryIO,
    ) -> IngestResult:
        """Resolve a replay against an ingest interrupted while STAGING.

        The original staged file is the *only* recoverable copy of the caller's
        upload, so a replay is **never** staged onto ``existing.staging_key``:
        doing so would overwrite those bytes before they could be compared (an
        empty or different replay would silently replace or wipe the original).

        The replay is staged under a brand-new key (never the original's) and
        compared with the original's identity, taken from, in order:

        1. the digest/size recorded by Tx2, when the crash happened after it;
        2. a fresh measurement of the original staged file, when it is intact;
        3. nothing, when the original is gone - only then is the replay adopted
           as the staged original.

        Any mismatch, including an empty replay, is an ``IdempotencyConflict``;
        the original is untouched and still recoverable, and the replay's own
        staging file is removed on every exit path.
        """
        replay_key = self._blob_store.new_staging_key()
        adopted = False
        try:
            try:
                replay_sha, replay_size = self._blob_store.stage_to(
                    replay_key, stream, max_bytes=self._max_bytes
                )
            except ContentTooLarge:
                raise IdempotencyConflict(
                    "idempotency key was reused with content larger than the stored attempt"
                ) from None

            original: tuple[str, int] | None
            if existing.content_sha256 is not None and existing.size_bytes is not None:
                original = (existing.content_sha256, existing.size_bytes)
            elif existing.staging_key is not None:
                try:
                    original = self._blob_store.stage_identity(existing.staging_key)
                except BlobStoreError:
                    original = None  # the original staged file is gone
            else:
                original = None

            if original is not None:
                if (replay_sha, replay_size) != original:
                    raise IdempotencyConflict("idempotency key was reused with different content")
            elif replay_size == 0:
                raise IdempotencyConflict("idempotency key was reused with an empty body")
            else:
                # ``existing`` is a snapshot: a concurrent replay may have completed
                # the ingest and consumed the original since it was read.  Only a row
                # that is *still* STAGING may adopt this replay as its staged original.
                adopted = await repo.set_staging_key(existing.ingest_id, replay_key)
        finally:
            if not adopted:
                self._blob_store.remove_staging(replay_key)

        refreshed = await repo.get_ingest_fresh(existing.ingest_id)
        assert refreshed is not None
        report = await self._reconcile_committing(
            session, repo, refreshed, owned_staging_key=replay_key if adopted else None
        )
        if report.state is IngestState.READY:
            final = await repo.get_ingest_fresh(existing.ingest_id)
            assert final is not None
            # Serve the ingest only to a caller whose bytes ARE the published bytes.
            # A replay with different content that merely lost the race to a matching
            # replay must be refused, never told its upload succeeded.
            if (replay_sha, replay_size) != (final.content_sha256, final.size_bytes):
                raise IdempotencyConflict("idempotency key was reused with different content")
            return _result_from(final, reused=True)
        raise RecoveryError(f"interrupted ingest could not be completed: {report.outcome}")

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
            return await self._reconcile_committing(session, repo, record)

    async def _reconcile_committing(
        self,
        session: AsyncSession,
        repo: SqlAssetRepository,
        record: IngestRecord,
        *,
        owned_staging_key: str | None = None,
    ) -> IngestRecoveryReport:
        """Reconcile one ingest and commit; if a concurrent actor won, report theirs.

        A lost compare-and-set means another session already moved the row out of
        STAGING.  This session's uncommitted asset/version/link rows are discarded
        by rollback, and the report is derived from the winner's committed row, so
        a loser can neither duplicate the asset nor downgrade a READY ingest.
        """
        try:
            report = await reconcile_ingest(repo, self._blob_store, record)
            await session.commit()
            return report
        except IngestAlreadyResolved:
            await session.rollback()
            if owned_staging_key is not None:
                # This call adopted its own replay as the staged original, then lost
                # the race: that file is referenced by no row any more.
                self._blob_store.remove_staging(owned_staging_key)
            winner = await repo.get_ingest_fresh(record.ingest_id)
            assert winner is not None
            if winner.state is IngestState.READY:
                return IngestRecoveryReport(
                    winner.ingest_id, IngestState.READY, RecoveryOutcome.ALREADY_READY
                )
            return IngestRecoveryReport(
                winner.ingest_id, winner.state, RecoveryOutcome.FAILED, "resolved concurrently"
            )

    async def recover_all(self) -> list[IngestRecoveryReport]:
        """Reconcile every durable STAGING ingest; never invents success."""
        async with self._session_factory() as session:
            repo = SqlAssetRepository(session)
            pending = await repo.list_pending_ingests()
            reports: list[IngestRecoveryReport] = []
            for record in pending:
                # One commit per ingest: a lost race on one record must not roll
                # back the progress already made on the others.
                reports.append(await self._reconcile_committing(session, repo, record))
            return reports


__all__ = [
    "AssetLibraryService",
    "LocalContentAddressedBlobStore",
    "PERMISSION_READ",
    "PERMISSION_WRITE",
]
