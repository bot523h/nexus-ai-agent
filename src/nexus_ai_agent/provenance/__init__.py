"""Durable causal provenance for the creative execution chain.

The causal ledger of the Project Graph (task-231): an append-only,
hash-chained journal of durable job-lifecycle transitions, a fail-safe queue
observer that feeds it, and the Artifact Passport — the read-only projection
that reconciles chain against the authoritative job row and re-measures
artifacts instead of trusting anybody's word.

Authority law: nothing in this package executes, authorizes, or mutates job
state. It observes facts after they are durably committed and proves (or
honestly refuses to prove) what happened.
"""

from nexus_ai_agent.provenance.journal import AppendResult, CausalJournal
from nexus_ai_agent.provenance.models import (
    GENESIS_HASH,
    RECORD_DOMAIN,
    CausalEvent,
    ChainVerdict,
    EventKind,
    JobFacts,
    LedgerRecord,
    canonical_json,
    compute_record_hash,
    digest_of,
    verify_chain,
)
from nexus_ai_agent.provenance.observer import CausalObserver, QueueLedgerObserver
from nexus_ai_agent.provenance.passport import (
    ArtifactPassport,
    Finding,
    PassportBuilder,
    PassportStatus,
)

__all__ = [
    "GENESIS_HASH",
    "RECORD_DOMAIN",
    "AppendResult",
    "ArtifactPassport",
    "CausalEvent",
    "CausalJournal",
    "CausalObserver",
    "ChainVerdict",
    "EventKind",
    "Finding",
    "JobFacts",
    "LedgerRecord",
    "PassportBuilder",
    "PassportStatus",
    "QueueLedgerObserver",
    "canonical_json",
    "compute_record_hash",
    "digest_of",
    "verify_chain",
]
