"""Typed failure modes of the resource-pack delivery plane.

Every failure is a distinct type so the studio UI (and tests) can react to the
*reason* — "not enough disk", "tampered bytes", "server dropped the
connection" — instead of string-matching messages.  All of them derive from
:class:`ResourcePackError` so a caller can still catch the whole family.
"""

from __future__ import annotations

__all__ = [
    "ActivePointerError",
    "IntegrityError",
    "InsufficientStorageError",
    "ManifestValidationError",
    "PackLeaseError",
    "PackLockTimeoutError",
    "PackLockUnavailableError",
    "PackSyncError",
    "PackVerificationError",
    "RangeFetchError",
    "RangeFetchInterrupted",
    "ReentrantSyncError",
    "ResourcePackError",
    "SeedRecipeError",
    "SignatureBackendUnavailableError",
    "SignatureVerificationError",
    "UnknownPackError",
    "UnsafeSourceError",
    "VersionRollbackError",
]


class ResourcePackError(Exception):
    """Base class of every resource-pack failure."""


class ManifestValidationError(ResourcePackError, ValueError):
    """A manifest (or one of its entries) is structurally malformed."""


class PackVerificationError(ResourcePackError):
    """An installed pack or a manifest failed fail-closed verification."""

    def __init__(self, problems: tuple[str, ...] | list[str] | str) -> None:
        items = (problems,) if isinstance(problems, str) else tuple(problems)
        self.problems: tuple[str, ...] = items
        super().__init__("; ".join(items) if items else "verification failed")


class SignatureVerificationError(PackVerificationError):
    """The Ed25519 manifest signature is missing, malformed or invalid."""


class SignatureBackendUnavailableError(SignatureVerificationError):
    """The Ed25519 backend (``cryptography``) is not installed.

    This is a *verification failure*, never a pass: a missing library must not
    decay into "not verified, but allowed".
    """


class IntegrityError(ResourcePackError):
    """Bytes on disk (downloaded, hydrated or cached) do not match the manifest."""


class SeedRecipeError(ResourcePackError, ValueError):
    """A local DNA seed recipe is unknown or carries invalid parameters."""


class RangeFetchError(ResourcePackError):
    """A pack source could not serve a (ranged) blob request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UnsafeSourceError(RangeFetchError):
    """A request (or a redirect) targets a scheme/host the source policy forbids.

    Raised *before* any network I/O towards the forbidden destination and never
    retried by the fetcher.
    """


class RangeFetchInterrupted(RangeFetchError):
    """The transfer stopped early; the ``.part`` file is kept for resumption."""

    def __init__(self, message: str, *, bytes_on_disk: int) -> None:
        super().__init__(message)
        self.bytes_on_disk = bytes_on_disk


class InsufficientStorageError(ResourcePackError):
    """The disk guard refused the sync before a single byte was written."""

    def __init__(self, *, pack_id: str, required_bytes: int, available_bytes: int) -> None:
        self.pack_id = pack_id
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        super().__init__(
            f"pack {pack_id!r} needs {required_bytes} free bytes, only {available_bytes} available"
        )


class UnknownPackError(ResourcePackError, LookupError):
    """The requested pack id is not part of the target catalog."""


class PackSyncError(ResourcePackError):
    """The sync manager's on-disk state is inconsistent (e.g. a tampered pointer)."""


class PackLeaseError(PackSyncError):
    """An in-flight lease file is unreadable: garbage collection refuses to run."""


class PackLockTimeoutError(PackSyncError):
    """The cross-process storage lock could not be acquired in time."""


class PackLockUnavailableError(PackSyncError):
    """No safe OS-level lock primitive exists here: mutating operations fail closed.

    The storage lock relies on ``fcntl.flock`` (released by the kernel when
    the owner dies).  There is deliberately no ``O_EXCL`` lock-file fallback:
    such a fallback can only recover from a crashed owner by *stealing* a
    "stale" lock, which can delete a live owner's lock.
    """


class ActivePointerError(PackSyncError):
    """``active.json`` exists but is unreadable, malformed or not a regular file.

    Anti-rollback cannot know the active version, so installs refuse to
    replace the pointer unless the caller deliberately passes
    ``allow_rollback=True`` to repair.
    """


class ReentrantSyncError(PackSyncError):
    """``sync_pack`` was re-entered on the same manager from inside its own sync

    (typically from a progress callback).  The nested call is refused
    immediately — before it reads or writes anything — instead of deadlocking
    or starting a second, conflicting transaction.
    """


class VersionRollbackError(ResourcePackError):
    """The target manifest is older than the active version (anti-rollback guard)."""

    def __init__(self, *, pack_id: str, active_version: str, target_version: str, reason: str):
        self.pack_id = pack_id
        self.active_version = active_version
        self.target_version = target_version
        super().__init__(
            f"refusing {pack_id!r} {active_version} -> {target_version}: {reason} "
            "(pass allow_rollback=True to downgrade deliberately)"
        )
