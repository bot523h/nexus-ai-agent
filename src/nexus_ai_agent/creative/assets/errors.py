"""Typed, stable errors for the Asset Library.

Callers switch on the exception *type*; each carries a stable ``code`` string
for structured logging.  No error message ever embeds media bytes, credentials
or an internal absolute storage path.
"""

from __future__ import annotations


class AssetLibraryError(Exception):
    """Base class for every Asset Library failure."""

    code = "asset_library_error"


class AuthorizationFailure(AssetLibraryError):
    """The actor is not authorized for the requested project operation."""

    code = "not_authorized"


class UnsupportedMediaType(AssetLibraryError):
    """The bytes are not one of the supported, allow-listed media types."""

    code = "unsupported_media_type"


class ContentTooLarge(AssetLibraryError):
    """The stream exceeded the configured maximum content size."""

    code = "content_too_large"


class EmptyContent(AssetLibraryError):
    """The stream contained no bytes."""

    code = "empty_content"


class IdempotencyConflict(AssetLibraryError):
    """An idempotency key was reused for a different payload."""

    code = "idempotency_conflict"


class IngestNotFound(AssetLibraryError):
    """No ingest record exists for the requested identity."""

    code = "ingest_not_found"


class AssetNotFound(AssetLibraryError):
    """No asset/version/link is visible to the caller for this project."""

    code = "asset_not_found"


class ContentIntegrityError(AssetLibraryError):
    """Stored content is missing or does not match its recorded identity."""

    code = "content_integrity"


class BlobStoreError(AssetLibraryError):
    """A blob-store operation failed inside the filesystem boundary."""

    code = "blob_store_error"


class RecoveryError(AssetLibraryError):
    """An interrupted ingest could not be safely completed or failed."""

    code = "recovery_error"
