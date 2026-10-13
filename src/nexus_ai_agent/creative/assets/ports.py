"""Ports (interfaces) for the Asset Library.

Only the seams the V1 use cases actually need: a content-addressed blob store,
a metadata repository, and an authorization seam.  A port is not proof of an
implementation — the real local adapters are exercised in integration tests.

No web framework, Telegram, LLM provider or cloud SDK type appears here.
"""

from __future__ import annotations

from typing import BinaryIO, Protocol

from nexus_ai_agent.creative.assets.models import (
    AssetIdentity,
    AssetVersion,
    IngestRecord,
    MediaKind,
    ProjectAssetLink,
)


class BlobStore(Protocol):
    """Content-addressed storage of immutable bytes (synchronous, local I/O)."""

    def new_staging_key(self) -> str:
        """Allocate a fresh, unguessable staging key."""
        ...

    def stage_to(self, staging_key: str, source: BinaryIO, *, max_bytes: int) -> tuple[str, int]:
        """Stream ``source`` into ``staging_key``; return ``(sha256, size)``."""
        ...

    def blob_key_for(self, sha256: str) -> str:
        """Return the deterministic content key for a 64-hex digest."""
        ...

    def hash_stream(self, source: BinaryIO, *, max_bytes: int) -> tuple[str, int]:
        """Measure ``(sha256, size)`` of a stream with no filesystem effect."""
        ...

    def store(self, staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        """Atomically publish staged bytes; return ``(sha256, size, deduplicated)``."""
        ...

    def verify(self, blob_key: str, *, sha256: str, size_bytes: int) -> bool:
        """True iff the stored blob exists and matches the recorded identity."""
        ...

    def open(self, blob_key: str) -> bytes:
        """Return the stored bytes, or raise on missing/tampered content."""
        ...

    def stage_identity(self, staging_key: str) -> tuple[str, int]:
        """Measure ``(sha256, size)`` of a staged file (recovery path)."""
        ...

    def read_staging_header(self, staging_key: str, size: int) -> bytes:
        """Read only the leading bytes of a staged file for classification."""
        ...

    def remove_staging(self, staging_key: str) -> None:
        """Best-effort removal of a staging file."""
        ...


class AssetRepository(Protocol):
    """Durable metadata operations (async; no second database)."""

    async def get_asset(self, asset_id: str) -> AssetIdentity | None: ...

    async def get_version(self, version_id: str) -> AssetVersion | None: ...

    async def get_link_for_project(
        self, project_id: str, asset_id: str
    ) -> ProjectAssetLink | None: ...

    async def list_project_assets(
        self, project_id: str, *, limit: int, offset: int
    ) -> list[ProjectAssetLink]: ...

    async def find_ingest_by_key(
        self, project_id: str, idempotency_key: str
    ) -> IngestRecord | None: ...

    async def get_ingest(self, ingest_id: str) -> IngestRecord | None: ...

    async def get_ingest_fresh(self, ingest_id: str) -> IngestRecord | None: ...

    async def list_pending_ingests(self) -> list[IngestRecord]: ...

    async def mark_failed(self, ingest_id: str, *, error_code: str) -> bool: ...

    async def set_staging_key(self, ingest_id: str, staging_key: str) -> bool: ...

    async def downgrade_ready_to_failed(self, ingest_id: str, *, error_code: str) -> bool: ...

    async def complete_ingest(
        self,
        ingest_id: str,
        *,
        asset_id: str,
        version_id: str,
        blob_key: str,
        media_kind: MediaKind,
        media_type: str,
    ) -> bool: ...

    async def add_asset(self, *, asset_id: str, media_kind: MediaKind) -> AssetIdentity: ...

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
    ) -> AssetVersion: ...

    async def add_link(
        self, *, link_id: str, project_id: str, asset_id: str, version_id: str
    ) -> ProjectAssetLink: ...


class AuthorizationSeam(Protocol):
    """The trusted, project-scoped authorization boundary.

    The caller authenticates before invoking this; the seam independently binds
    the real actor to the project and refuses anything else.  An Asset Library
    operation must never treat ``project_id`` as proof of access.
    """

    def require(self, actor_kind: str, actor_id: str, project_id: str, permission: str) -> None:
        """Raise :class:`AuthorizationFailure` unless the actor holds the grant."""
        ...
