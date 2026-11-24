"""Durable causal provenance for the creative execution chain.

The causal ledger of the Project Graph (task-231): an append-only,
hash-chained journal of durable job-lifecycle transitions, a fail-safe queue
observer that feeds it, the Artifact Passport — the read-only projection
that reconciles chain against the authoritative job row and re-measures
artifacts instead of trusting anybody's word — and the Durable Creative-Graph
projection (law R28) for mathematically verified causal lineage, ontology
separation, counterfactual impact analysis, and epistemic proof tracking.

Authority law: nothing in this package executes, authorizes, or mutates job
state. It observes facts after they are durably committed and proves (or
honestly refuses to prove) what happened.
"""

from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_digest,
    verify_canonical_digest,
)
from nexus_ai_agent.provenance.graph import (
    GRAPH_CHAIN_GENESIS,
    GRAPH_TABLES,
    LINEAGE_ATTACHMENTS,
    LINEAGE_CHAIN,
    NODE_KINDS,
    RELATIONS,
    STATUSES,
    TERMINAL_STATUSES,
    CanonicalValueError,
    CausalImpactResult,
    ChainEntry,
    CreativeGraph,
    EntityIdentityTriple,
    EpistemicProof,
    EpistemicProofError,
    EpistemicStatus,
    GraphCycleError,
    GraphEdge,
    GraphIntegrityError,
    GraphNode,
    GraphStateError,
    IntegrityFinding,
    IntegrityReport,
    WriteResult,
    canonical_graph_digest,
    canonical_graph_json,
    compute_proof_signature,
    create_epistemic_proof,
    edge_identity,
    node_identity,
    normalize_canonical_value,
    validate_sha256_digest,
    verify_epistemic_proof,
)
from nexus_ai_agent.provenance.jcs import (
    JcsError,
    canonical_bytes,
    canonical_text,
    jcs_digest,
)
from nexus_ai_agent.provenance.journal import (
    AppendResult,
    CausalConflictError,
    CausalJournal,
)
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
    "GRAPH_CHAIN_GENESIS",
    "GRAPH_TABLES",
    "LINEAGE_ATTACHMENTS",
    "LINEAGE_CHAIN",
    "NODE_KINDS",
    "RECORD_DOMAIN",
    "RELATIONS",
    "STATUSES",
    "TERMINAL_STATUSES",
    "AppendResult",
    "ArtifactPassport",
    "CanonicalValueError",
    "CanonicalizationScheme",
    "CausalConflictError",
    "CausalEvent",
    "CausalImpactResult",
    "CausalJournal",
    "CausalObserver",
    "ChainEntry",
    "ChainVerdict",
    "CreativeGraph",
    "EntityIdentityTriple",
    "EpistemicProof",
    "EpistemicProofError",
    "EpistemicStatus",
    "EventKind",
    "Finding",
    "GraphCycleError",
    "GraphEdge",
    "GraphIntegrityError",
    "GraphNode",
    "GraphStateError",
    "IntegrityFinding",
    "IntegrityReport",
    "JcsError",
    "JobFacts",
    "LedgerRecord",
    "PassportBuilder",
    "PassportStatus",
    "QueueLedgerObserver",
    "WriteResult",
    "canonical_bytes",
    "canonical_digest",
    "canonical_graph_digest",
    "canonical_graph_json",
    "canonical_json",
    "canonical_text",
    "compute_proof_signature",
    "compute_record_hash",
    "create_epistemic_proof",
    "digest_of",
    "edge_identity",
    "jcs_digest",
    "node_identity",
    "normalize_canonical_value",
    "validate_sha256_digest",
    "verify_canonical_digest",
    "verify_chain",
    "verify_epistemic_proof",
]
