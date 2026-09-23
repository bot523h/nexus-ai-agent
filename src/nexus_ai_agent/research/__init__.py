"""Research V2 substrate package.

P1 (CLAIM + LEASE) and P2 (INBOX / RECEIPT) live as domain vocabulary under
``nexus_ai_agent.domain`` and durable adapters under ``nexus_ai_agent.adapters``.
This package re-exports the public surface so Research V2 call sites have one
import root without coupling features to adapter internals.
"""

from __future__ import annotations

from nexus_ai_agent.adapters.job_claim_lease import ClaimLeaseStore
from nexus_ai_agent.adapters.update_inbox import UpdateInboxStore
from nexus_ai_agent.domain.inbox import (
    AcceptOutcome,
    ReceiptStatus,
    ReceiptTransitionError,
    UpdateReceipt,
)
from nexus_ai_agent.domain.lease import (
    DEFAULT_LEASE_TTL_SECONDS,
    JobLease,
    LeaseClaimOutcome,
    LeaseMutationOutcome,
)

__all__ = [
    "AcceptOutcome",
    "ClaimLeaseStore",
    "DEFAULT_LEASE_TTL_SECONDS",
    "JobLease",
    "LeaseClaimOutcome",
    "LeaseMutationOutcome",
    "ReceiptStatus",
    "ReceiptTransitionError",
    "UpdateInboxStore",
    "UpdateReceipt",
]
