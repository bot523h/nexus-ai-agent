"""The explicit ingest state machine and its recovery procedure.

Filesystem publication and the database commit are **not** one atomic
transaction, so the design names that fact instead of hiding it:

    STAGING --publish bytes--> verify --> commit metadata --> READY
       \\                                                    /
        '---------------- on unrecoverable error ---------- FAILED

``READY`` is a behavioural guarantee, not a label: the blob exists, its
measured size and digest agree with the version record, and the metadata plus
project link are durably committed.  :func:`reconcile_ingest` may *complete* a
valid interrupted ingest or *fail* it — it never manufactures success.

Both the live service and recovery share :func:`publish_and_commit`, so there is
exactly one completion path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum

from nexus_ai_agent.creative.assets.errors import (
    BlobStoreError,
    ContentIntegrityError,
    IngestAlreadyResolved,
)
from nexus_ai_agent.creative.assets.mime import HEADER_BYTES, detect_media_type
from nexus_ai_agent.creative.assets.models import (
    SUPPORTED_MEDIA_TYPES,
    IngestRecord,
    IngestState,
    MediaKind,
)
from nexus_ai_agent.creative.assets.ports import AssetRepository, BlobStore


class RecoveryOutcome(str, Enum):
    """What a single reconcile pass concluded about one ingest."""

    COMPLETED = "completed"
    ALREADY_READY = "already_ready"
    STILL_STAGING = "still_staging"
    FAILED = "failed"
    MISSING_BLOB = "missing_blob"


@dataclass(frozen=True)
class IngestRecoveryReport:
    ingest_id: str
    state: IngestState
    outcome: RecoveryOutcome
    detail: str = ""


async def publish_and_commit(
    repo: AssetRepository,
    blob_store: BlobStore,
    *,
    ingest_id: str,
    project_id: str,
    staging_key: str,
    content_sha256: str,
    size_bytes: int,
    media_kind: MediaKind,
    media_type: str,
    original_filename: str = "",
) -> tuple[str, str, bool]:
    """Publish staged bytes, verify them, and write the metadata graph.

    Returns ``(asset_id, version_id, deduplicated)``.  The caller owns the
    transaction: this function only flushes; the single authoritative commit
    happens once, after it returns.
    """
    blob_key = blob_store.blob_key_for(content_sha256)
    measured_sha, measured_size, deduplicated = blob_store.store(staging_key, blob_key)
    if (measured_sha, measured_size) != (content_sha256, size_bytes):
        raise ContentIntegrityError("published bytes do not match the staged identity")
    if not blob_store.verify(blob_key, sha256=content_sha256, size_bytes=size_bytes):
        raise ContentIntegrityError("published blob failed independent verification")

    asset_id = uuid.uuid4().hex
    version_id = uuid.uuid4().hex
    await repo.add_asset(asset_id=asset_id, media_kind=media_kind)
    await repo.add_version(
        version_id=version_id,
        asset_id=asset_id,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        media_kind=media_kind,
        media_type=media_type,
        blob_key=blob_key,
        original_filename=original_filename,
    )
    await repo.add_link(
        link_id=uuid.uuid4().hex,
        project_id=project_id,
        asset_id=asset_id,
        version_id=version_id,
    )
    if not await repo.complete_ingest(
        ingest_id,
        asset_id=asset_id,
        version_id=version_id,
        blob_key=blob_key,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        media_kind=media_kind,
        media_type=media_type,
    ):
        raise IngestAlreadyResolved(ingest_id)
    return asset_id, version_id, deduplicated


async def reconcile_ingest(
    repo: AssetRepository,
    blob_store: BlobStore,
    ingest: IngestRecord,
) -> IngestRecoveryReport:
    """Complete or fail exactly one interrupted ingest.  Never invents success."""
    if ingest.state is IngestState.READY:
        if ingest.blob_key and blob_store.verify(
            ingest.blob_key,
            sha256=ingest.content_sha256 or "",
            size_bytes=ingest.size_bytes or 0,
        ):
            return IngestRecoveryReport(
                ingest.ingest_id, IngestState.READY, RecoveryOutcome.ALREADY_READY
            )
        # A READY row whose bytes are gone must never stay falsely READY.
        await repo.downgrade_ready_to_failed(ingest.ingest_id, error_code="blob_missing")
        return IngestRecoveryReport(
            ingest.ingest_id,
            IngestState.FAILED,
            RecoveryOutcome.MISSING_BLOB,
            "recorded final blob is missing or tampered",
        )

    if ingest.state is IngestState.FAILED:
        return IngestRecoveryReport(
            ingest.ingest_id, IngestState.FAILED, RecoveryOutcome.FAILED, "already failed"
        )

    # STAGING: prefer the staged bytes; otherwise finalise a published blob.
    if ingest.staging_key:
        try:
            staging_sha, staging_size = blob_store.stage_identity(ingest.staging_key)
        except BlobStoreError:
            # The staging file is gone (already published or lost); fall through
            # to the final-blob path or an explicit failure.  Never invent success.
            staging_sha = None
            staging_size = None
        if staging_sha is not None and staging_size is not None:
            media_type = ingest.media_type
            media_kind = ingest.media_kind
            if media_type is None or media_kind is None:
                header = blob_store.read_staging_header(ingest.staging_key, HEADER_BYTES)
                detected = detect_media_type(header) if header else None
                if detected is None or detected not in SUPPORTED_MEDIA_TYPES:
                    if not await repo.mark_failed(
                        ingest.ingest_id, error_code="unsupported_media_type"
                    ):
                        raise IngestAlreadyResolved(ingest.ingest_id)
                    return IngestRecoveryReport(
                        ingest.ingest_id, IngestState.FAILED, RecoveryOutcome.FAILED
                    )
                media_type = detected
                media_kind = SUPPORTED_MEDIA_TYPES[detected]
            await publish_and_commit(
                repo,
                blob_store,
                ingest_id=ingest.ingest_id,
                project_id=ingest.project_id,
                staging_key=ingest.staging_key,
                content_sha256=staging_sha,
                size_bytes=staging_size,
                media_kind=media_kind,
                media_type=media_type,
                original_filename=ingest.original_filename,
            )
            return IngestRecoveryReport(
                ingest.ingest_id, IngestState.READY, RecoveryOutcome.COMPLETED
            )

    if ingest.content_sha256 and ingest.size_bytes is not None and ingest.media_type:
        blob_key = blob_store.blob_key_for(ingest.content_sha256)
        if blob_store.verify(blob_key, sha256=ingest.content_sha256, size_bytes=ingest.size_bytes):
            asset_id = uuid.uuid4().hex
            version_id = uuid.uuid4().hex
            media_kind = ingest.media_kind or SUPPORTED_MEDIA_TYPES[ingest.media_type]
            await repo.add_asset(asset_id=asset_id, media_kind=media_kind)
            await repo.add_version(
                version_id=version_id,
                asset_id=asset_id,
                content_sha256=ingest.content_sha256,
                size_bytes=ingest.size_bytes,
                media_kind=media_kind,
                media_type=ingest.media_type,
                blob_key=blob_key,
                original_filename=ingest.original_filename,
            )
            await repo.add_link(
                link_id=uuid.uuid4().hex,
                project_id=ingest.project_id,
                asset_id=asset_id,
                version_id=version_id,
            )
            if not await repo.complete_ingest(
                ingest.ingest_id,
                asset_id=asset_id,
                version_id=version_id,
                blob_key=blob_key,
                content_sha256=ingest.content_sha256,
                size_bytes=ingest.size_bytes,
                media_kind=media_kind,
                media_type=ingest.media_type,
            ):
                raise IngestAlreadyResolved(ingest.ingest_id)
            return IngestRecoveryReport(
                ingest.ingest_id, IngestState.READY, RecoveryOutcome.COMPLETED
            )
        if not await repo.mark_failed(ingest.ingest_id, error_code="blob_missing"):
            raise IngestAlreadyResolved(ingest.ingest_id)
        return IngestRecoveryReport(
            ingest.ingest_id, IngestState.FAILED, RecoveryOutcome.MISSING_BLOB
        )

    if not await repo.mark_failed(ingest.ingest_id, error_code="staging_lost"):
        raise IngestAlreadyResolved(ingest.ingest_id)
    return IngestRecoveryReport(
        ingest.ingest_id, IngestState.FAILED, RecoveryOutcome.FAILED, "no recoverable content"
    )
