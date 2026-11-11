"""NAGAR CONNECT contracts: transport metadata, not execution authority.

This package deliberately contains no web framework, persistence, credential
secret, queue, provider, verifier, or CommandBus implementation. It validates
external connection metadata before an adapter delegates to canonical NEXUS
identity, authorization, and execution authorities.
"""

from nexus_ai_agent.connect.contracts import (
    ConnectionContract,
    ConnectionState,
    CredentialDescriptor,
    CredentialState,
    ScopeGrant,
    authorize_read,
)

__all__ = [
    "ConnectionContract",
    "ConnectionState",
    "CredentialDescriptor",
    "CredentialState",
    "ScopeGrant",
    "authorize_read",
]
