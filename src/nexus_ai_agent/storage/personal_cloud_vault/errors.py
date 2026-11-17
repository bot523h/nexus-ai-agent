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


class VaultQuotaExceededError(PersonalVaultError):
    """Raised when writing an object would exceed the configured vault quota."""


class VaultRevisionConflictError(PersonalVaultError):
    """Raised when pushing a snapshot violates parent ancestry on the remote vault."""


class VaultTransportError(PersonalVaultError):
    """Raised when storage I/O, HTTP transport, or object lookup fails."""


__all__ = [
    "PersonalVaultError",
    "VaultAuthenticationError",
    "VaultCryptoError",
    "VaultQuotaExceededError",
    "VaultRevisionConflictError",
    "VaultTamperDetectedError",
    "VaultTransportError",
]
