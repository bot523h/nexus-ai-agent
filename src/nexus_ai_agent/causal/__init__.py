"""Nagar's durable causal evidence layer.

The pieces, and why each exists:

* :mod:`~nexus_ai_agent.causal.models` — deterministic identities, the closed
  stage vocabulary, the bounded/whitelisted fact contract and the record
  whose hash binds it to its predecessor.
* :mod:`~nexus_ai_agent.causal.journal` — the append-only, hash-chained,
  idempotent SQLite journal: durable causal order, tamper-evident, with no
  update and no delete statement.
* :mod:`~nexus_ai_agent.causal.observer` — the translation from committed
  Job-lifecycle facts (``application.ports.job_lifecycle_observer``) to
  records; stateless, so restarts reconstruct rather than remember.
* :mod:`~nexus_ai_agent.causal.passport` — the projection that answers "why
  and how did this artifact come into existence?", reconciling the journal,
  the queue row and freshly measured bytes.
* :mod:`~nexus_ai_agent.causal.verify` — the independent judge of a passport.

None of these grant authority, execute work, or touch the queue's tables:
the journal creates exactly one table of its own, and every write is an
``INSERT`` after a fact became durable somewhere authoritative.
"""

from __future__ import annotations

from nexus_ai_agent.causal.journal import CausalJournal
from nexus_ai_agent.causal.models import (
    AppendOutcome,
    CausalRecord,
    CausalRefused,
    ChainVerification,
    JournalCorrupted,
    JournalUnavailable,
    NodeRef,
    PassportRefused,
    Stage,
    StageStatus,
)
from nexus_ai_agent.causal.observer import CausalObserver
from nexus_ai_agent.causal.passport import (
    ArtifactPassport,
    Divergence,
    JobFacts,
    MeasuredArtifact,
    PassportBuilder,
    StageEvidence,
    job_facts_from_chain_row,
)
from nexus_ai_agent.causal.verify import PassportVerification, require_complete, verify_passport

__all__ = [
    "AppendOutcome",
    "ArtifactPassport",
    "CausalJournal",
    "CausalObserver",
    "CausalRecord",
    "CausalRefused",
    "ChainVerification",
    "Divergence",
    "JobFacts",
    "JournalCorrupted",
    "JournalUnavailable",
    "MeasuredArtifact",
    "NodeRef",
    "PassportBuilder",
    "PassportRefused",
    "PassportVerification",
    "Stage",
    "StageEvidence",
    "StageStatus",
    "job_facts_from_chain_row",
    "require_complete",
    "verify_passport",
]
