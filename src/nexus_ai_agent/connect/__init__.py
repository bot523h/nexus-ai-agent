"""NAGAR CONNECT contracts and encrypted credential persistence.

Transport adapters still own authentication and canonical ProjectAccess. This
package provides immutable contracts plus a database adapter that persists only
encrypted credential envelopes; it is not a command, API, MCP, or provider
authority.
"""

from nexus_ai_agent.connect.contracts import (
    ConnectionContract,
    ConnectionState,
    CredentialDescriptor,
    CredentialState,
    ScopeGrant,
    authorize_read,
)
from nexus_ai_agent.connect.crypto import EncryptedSecret, EnvelopeCipher, SecretEnvelopeError
from nexus_ai_agent.connect.key_provider import (
    KeyEncryptionKey,
    KeyProvider,
    KeyProviderError,
    StaticKeyRing,
)
from nexus_ai_agent.connect.persistence import (
    ConnectionConflict,
    ConnectionPersistenceError,
    PersistedConnection,
    SqlConnectionStore,
)

__all__ = [
    "ConnectionConflict",
    "ConnectionContract",
    "ConnectionPersistenceError",
    "ConnectionState",
    "CredentialDescriptor",
    "CredentialState",
    "EncryptedSecret",
    "EnvelopeCipher",
    "KeyEncryptionKey",
    "KeyProvider",
    "KeyProviderError",
    "PersistedConnection",
    "ScopeGrant",
    "SecretEnvelopeError",
    "SqlConnectionStore",
    "StaticKeyRing",
    "authorize_read",
]
