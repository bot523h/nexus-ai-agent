"""Immutable domain contracts for the Asset Library.

These models are pure pydantic: no database, web, Telegram or provider types
cross this boundary.  They encode the core distinction the library exists to
protect:

* :class:`AssetIdentity` — the stable *logical* identity of an asset;
* :class:`AssetVersion` — an immutable content version with its own measured
  digest and size;
* :class:`ProjectAssetLink` — a durable, project-scoped association;
* :class:`IngestRecord` — the recoverable state of one ingest attempt.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class MediaKind(str, Enum):
    """The coarse media family an asset belongs to."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class IngestState(str, Enum):
    """Explicit ingest lifecycle: ``STAGING -> READY`` or ``FAILED``.

    ``READY`` is a behavioural guarantee: the referenced bytes exist, their
    measured size and digest agree with the version record, and the metadata
    plus project link are durably committed.  ``FAILED`` is terminal: safe
    recovery is no longer possible.
    """

    STAGING = "STAGING"
    READY = "READY"
    FAILED = "FAILED"


#: The explicit, closed allowlist of media types V1 accepts.  The value is the
#: coarse kind; the key is the canonical type produced by content inspection
#: (never by the caller's filename or claimed MIME type).
SUPPORTED_MEDIA_TYPES: dict[str, MediaKind] = {
    "image/png": MediaKind.IMAGE,
    "image/jpeg": MediaKind.IMAGE,
    "image/webp": MediaKind.IMAGE,
    "audio/wav": MediaKind.AUDIO,
    "audio/mpeg": MediaKind.AUDIO,
    "video/mp4": MediaKind.VIDEO,
    "video/webm": MediaKind.VIDEO,
}


class AssetIdentity(BaseModel):
    """Stable logical identity of an asset, independent of any version."""

    model_config = ConfigDict(frozen=True)

    asset_id: str = Field(min_length=1)
    media_kind: MediaKind
    created_at: datetime


class AssetVersion(BaseModel):
    """One immutable content version of an asset.

    ``content_sha256`` and ``size_bytes`` are *measured* from the stored bytes,
    never supplied by a caller.  ``blob_key`` is an internal, content-derived
    storage detail — it is never exposed as the asset's public identity.
    """

    model_config = ConfigDict(frozen=True)

    version_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    content_sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=1)
    media_kind: MediaKind
    media_type: str
    blob_key: str
    original_filename: str = ""
    created_at: datetime


class ProjectAssetLink(BaseModel):
    """Durable association of one asset version with one authorized project."""

    model_config = ConfigDict(frozen=True)

    link_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    created_at: datetime


class IngestRequest(BaseModel):
    """A caller's ingest intent.

    ``declared_media_type`` and ``original_filename`` are untrusted display
    metadata only: they never influence the measured digest or the accepted
    media classification, and the filename is never used as a storage path.
    """

    model_config = ConfigDict(frozen=True)

    idempotency_key: str = Field(min_length=1, max_length=200)
    original_filename: str = Field(default="", max_length=512)
    declared_media_type: str | None = None


class IngestResult(BaseModel):
    """The successful outcome of an ingest.

    Only returned once the content and durable metadata have been verified.
    """

    model_config = ConfigDict(frozen=True)

    ingest_id: str
    asset_id: str
    version_id: str
    content_sha256: str
    size_bytes: int
    media_kind: MediaKind
    media_type: str
    state: IngestState
    blob_deduplicated: bool = False
    reused_result: bool = False


class IngestRecord(BaseModel):
    """A snapshot of one ingest attempt's recoverable state."""

    model_config = ConfigDict(frozen=True)

    ingest_id: str
    idempotency_key: str
    project_id: str
    request_fingerprint: str
    state: IngestState
    media_kind: MediaKind | None = None
    media_type: str | None = None
    original_filename: str = ""
    content_sha256: str | None = None
    size_bytes: int | None = None
    blob_key: str | None = None
    staging_key: str | None = None
    asset_id: str | None = None
    version_id: str | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime
