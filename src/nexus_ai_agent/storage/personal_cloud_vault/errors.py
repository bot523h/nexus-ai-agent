"""Fail-closed exception hierarchy for the BYOS Personal Cloud & Local Device Vault.

Every error in :mod:`nexus_ai_agent.storage.personal_cloud_vault` derives from
:class:`PersonalVaultError` so callers can handle specific failure classes
(authentication, cryptographic tamper, quota exhaustion, revision conflict,
or transport failure) while maintaining a single fail-closed boundary.
"""

from __future__ import annotations


class PersonalVaultError(Exception):
    """Base fail-closed exception for all Personal Cloud Vault operations."""


class VaultAuthenticationError(PersonalVaultError):
    """Raised when vault key derivation, key verification, or storage auth fails."""


class VaultCryptoError(PersonalVaultError):
    """Raised when a cryptographic primitive receives invalid parameters or geometry."""


class VaultTamperDetectedError(PersonalVaultError):
    """Raised on any integrity, MAC, Merkle root, ordering, or bundle mismatch."""


class VaultIntegrityError(PersonalVaultError):
    """Raised when a destructive operation cannot first authenticate the vault.

    A garbage collection that cannot cryptographically verify every stored
    manifest must refuse to delete anything: an unverifiable manifest is either
    tampered or was written by a key we do not hold, and deleting its chunks
    could destroy data a legitimate peer still needs.
    """


class VaultQuotaExceededError(PersonalVaultError):
    """Raised when writing an object would exceed the configured vault quota."""


class VaultRevisionConflictError(PersonalVaultError):
    """Raised when pushing a snapshot violates parent ancestry on the remote vault."""


class VaultTransportError(PersonalVaultError):
    """Raised when storage I/O, HTTP transport, or object lookup fails."""


class VaultKeyNotFoundError(VaultTransportError):
    """Raised when the object a lease operation needs is provably *absent* (HTTP 404).

    Deliberately a subclass of :class:`VaultTransportError`: every caller that already
    treats "the vault could not be read" as one fail-closed class keeps working, while
    a caller that can retry a *missing* lease differently (a lease that is gone is not
    a transient transport fault) can catch the narrower type. Both are refusals — the
    difference is only which layer gets to decide what "gone" means.
    """


__all__ = [
    "PersonalVaultError",
    "VaultAuthenticationError",
    "VaultCryptoError",
    "VaultIntegrityError",
    "VaultQuotaExceededError",
    "VaultRevisionConflictError",
    "VaultTamperDetectedError",
    "VaultKeyNotFoundError",
    "VaultTransportError",
]
