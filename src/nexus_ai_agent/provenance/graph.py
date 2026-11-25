"""Durable creative graph: mathematically verified causal lineage (law R28).

The repository has durable authorities that this module never replaces:

* the durable job queue — sole authority for job state and outcomes (law R14);
* the append-only causal journal — hash-chained ledger of committed lifecycle
  transitions (evidence, never authority);
* ``ArtifactPassport`` — read-only, fail-closed verification of one artifact.

This module provides the durable, indexed, **rebuildable projection** that
answers: *"where did this artifact come from, what is its canonical identity
through change, what is its epistemic proof status, and which downstream
entities become causally invalidated if an upstream node changes?"*

Upgrades codified by Stage 2-A (Law R28):

1. **Formal Ontology of Identity Through Change:** every node explicitly
   separates ``logical_entity_id`` (semantic identity across revisions),
   ``revision_id`` (historical state in the causal graph), and
   ``content_digest`` (immutable SHA-256 bytes fingerprint).
2. **Strict DAG Mathematics:** every edge write enforces fail-closed
   acyclicity inside ``BEGIN IMMEDIATE``; ``topological_sort`` implements
   deterministic Lexicographic Kahn's Algorithm (min-heap on ``node_id``);
   canonical digests use RFC 8785 JCS semantics with exact ``int`` and
   ``fractions.Fraction`` encoding and fail-closed refusal of ``float``.
3. **Causal Counterfactual & Invalidation Propagation:**
   ``compute_causal_impact_set`` computes the exact causally dependent
   downstream subgraph, canonical witness paths, hop depths, topological
   recomputation order, and JCS impact digest.
4. **Epistemology of Proof & Tamper-Evident Persistence:** every node and edge
   carries an explicit :class:`EpistemicStatus` (``HYPOTHESIS``,
   ``MEASURED_EVIDENCE``, ``VERIFIED_COMMIT``); promotion to
   ``VERIFIED_COMMIT`` fails closed without a verified cryptographic
   :class:`EpistemicProof`. Every mutation appends to an internal hash chain
   verified across restarts, and all SQL queries are static literals (zero
   SQL string interpolation).
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import hmac
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

from nexus_ai_agent.provenance.canonicalization import (
    CanonicalizationScheme,
    canonical_digest,
)
from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import (
    EventKind,
    JobFacts,
    LedgerRecord,
)
from nexus_ai_agent.provenance.models import (
    dedupe_key as journal_dedupe_key,
)
from nexus_ai_agent.provenance.models import (
    verify_chain as verify_journal_chain,
)

#: Closed vocabulary of creative-domain node kinds.
NODE_KINDS: tuple[str, ...] = (
    "PROJECT",
    "INTENT",
    "REVISION",
    "DECISION",
    "PLAN",
    "COMMAND",
    "EXECUTION",
    "ARTIFACT",
    "EVIDENCE",
    "VERIFICATION",
    "PASSPORT",
    "ACTOR",
)

#: Closed vocabulary of directed causal relations (upstream -> downstream).
RELATIONS: tuple[str, ...] = (
    "PROJECT_HAS_INTENT",
    "INTENT_COMPILED_TO_REVISION",
    "REVISION_HAS_PLAN",
    "PLAN_EMITS_COMMAND",
    "COMMAND_EXECUTED_AS",
    "EXECUTION_PRODUCED_ARTIFACT",
    "ARTIFACT_VERIFIED_BY",
    "ARTIFACT_HAS_PASSPORT",
    "ARTIFACT_DERIVED_FROM",
    "DECISION_AFFECTS",
    "ACTOR_AUTHORIZED",
)

#: Upward lineage walk from an artifact to its originating project.
LINEAGE_CHAIN: tuple[tuple[str, str], ...] = (
    ("EXECUTION_PRODUCED_ARTIFACT", "in"),
    ("COMMAND_EXECUTED_AS", "in"),
    ("PLAN_EMITS_COMMAND", "in"),
    ("REVISION_HAS_PLAN", "in"),
    ("INTENT_COMPILED_TO_REVISION", "in"),
    ("PROJECT_HAS_INTENT", "in"),
)

#: Attachments anchored directly on an artifact node.
LINEAGE_ATTACHMENTS: tuple[str, ...] = ("ARTIFACT_VERIFIED_BY", "ARTIFACT_HAS_PASSPORT")

#: Closed node lifecycle statuses.
STATUSES: tuple[str, ...] = ("active", "superseded", "invalidated", "revoked")
TERMINAL_STATUSES: frozenset[str] = frozenset({"superseded", "invalidated", "revoked"})

#: Chain operations that each advance a node's ``revision_count`` by exactly one.
REVISION_CHAIN_OPS: frozenset[str] = frozenset({"node_create", "node_advance"})

#: Explicit finite-state transition matrix (F01): terminal statuses are absorbing
#: and can NEVER be resurrected to ``active`` or rewritten to another terminal state.
ALLOWED_NODE_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"active", "superseded", "invalidated", "revoked"}),
    "superseded": frozenset({"superseded"}),
    "invalidated": frozenset({"invalidated"}),
    "revoked": frozenset({"revoked"}),
}

#: Projection-owned SQLite tables (scope boundary for restore safety).
GRAPH_TABLES: frozenset[str] = frozenset(
    {
        "nexus_graph_node",
        "nexus_graph_edge",
        "nexus_graph_node_history",
        "nexus_graph_chain",
    }
)

#: Genesis hash for the internal tamper-evident mutation chain.
GRAPH_CHAIN_GENESIS: str = "sha256:" + ("0" * 64)

#: Layer-1 domain-to-JSON AST canonical encoding contract version (F07/F08).
CANONICAL_ENCODING_VERSION: str = "NEXUS_GRAPH_CANONICAL_V1"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HMAC_SHA256_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_MIN_HMAC_KEY_BYTES: int = 16
_RATIONAL_KEY = "__rational__"
_PROOF_DOMAIN = "nexus.provenance.epistemic_proof.v2"
_CHAIN_DOMAIN = "nexus.provenance.graph_chain.v1"


class EpistemicStatus(str, Enum):
    """Explicit epistemic tier of a graph node or causal edge."""

    HYPOTHESIS = "HYPOTHESIS"
    MEASURED_EVIDENCE = "MEASURED_EVIDENCE"
    VERIFIED_COMMIT = "VERIFIED_COMMIT"


_EPISTEMIC_RANK: dict[EpistemicStatus, int] = {
    EpistemicStatus.HYPOTHESIS: 0,
    EpistemicStatus.MEASURED_EVIDENCE: 1,
    EpistemicStatus.VERIFIED_COMMIT: 2,
}


class GraphStateError(RuntimeError):
    """A write or operation that would compromise graph truth, refused fail-closed."""


class GraphStateTransitionError(GraphStateError):
    """Raised on illegal lifecycle transitions (e.g. resurrecting a terminal node)."""


class GraphCycleError(GraphStateError):
    """Raised when an edge would violate strict DAG acyclicity."""


class EpistemicProofError(GraphStateError):
    """Raised when an epistemic status transition or proof binding fails."""


class GraphIntegrityError(GraphStateError):
    """Raised when on-disk tampering or chain corruption is detected."""


class CanonicalValueError(ValueError):
    """Raised when a value cannot be canonically serialized without ambiguity."""


# --------------------------------------------------------------------------- #
# Static SQL statements (zero string interpolation or concatenation)
# --------------------------------------------------------------------------- #
_SCHEMA_NODE = """
CREATE TABLE IF NOT EXISTS nexus_graph_node (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    logical_entity_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    evidence_digest TEXT,
    proof_json TEXT,
    status TEXT NOT NULL,
    superseded_by TEXT,
    source TEXT NOT NULL,
    source_seq INTEGER,
    revision_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_SCHEMA_EDGE = """
CREATE TABLE IF NOT EXISTS nexus_graph_edge (
    edge_id TEXT PRIMARY KEY,
    relation TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    evidence_digest TEXT,
    proof_json TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    source_seq INTEGER,
    created_at TEXT NOT NULL
)
"""

_SCHEMA_HISTORY = """
CREATE TABLE IF NOT EXISTS nexus_graph_node_history (
    history_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    proof_json TEXT,
    status TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""

_SCHEMA_CHAIN = """
CREATE TABLE IF NOT EXISTS nexus_graph_chain (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE,
    operation TEXT NOT NULL,
    target_id TEXT NOT NULL,
    entry_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""

_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_graph_edge_src ON nexus_graph_edge (src, relation)",
    "CREATE INDEX IF NOT EXISTS idx_graph_edge_dst ON nexus_graph_edge (dst, relation)",
    "CREATE INDEX IF NOT EXISTS idx_graph_edge_rel ON nexus_graph_edge (relation)",
    "CREATE INDEX IF NOT EXISTS idx_graph_node_kind ON nexus_graph_node (kind, status)",
    "CREATE INDEX IF NOT EXISTS idx_graph_node_entity ON nexus_graph_node (logical_entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_graph_node_digest ON nexus_graph_node (content_digest)",
    "CREATE INDEX IF NOT EXISTS idx_graph_history_node ON nexus_graph_node_history (node_id)",
)

_SQL_EDGES_OUT = (
    "SELECT edge_id, relation, src, dst, payload_json, payload_digest,"
    " epistemic_status, evidence_digest, proof_json, status, source,"
    " source_seq, created_at FROM nexus_graph_edge"
    " WHERE src = ? AND status = 'active' ORDER BY edge_id"
)
_SQL_EDGES_IN = (
    "SELECT edge_id, relation, src, dst, payload_json, payload_digest,"
    " epistemic_status, evidence_digest, proof_json, status, source,"
    " source_seq, created_at FROM nexus_graph_edge"
    " WHERE dst = ? AND status = 'active' ORDER BY edge_id"
)
_SQL_EDGES_OUT_REL = (
    "SELECT edge_id, relation, src, dst, payload_json, payload_digest,"
    " epistemic_status, evidence_digest, proof_json, status, source,"
    " source_seq, created_at FROM nexus_graph_edge"
    " WHERE src = ? AND status = 'active' AND relation = ? ORDER BY edge_id"
)
_SQL_EDGES_IN_REL = (
    "SELECT edge_id, relation, src, dst, payload_json, payload_digest,"
    " epistemic_status, evidence_digest, proof_json, status, source,"
    " source_seq, created_at FROM nexus_graph_edge"
    " WHERE dst = ? AND status = 'active' AND relation = ? ORDER BY edge_id"
)

_EDGE_QUERY_TABLE: dict[tuple[str, bool], str] = {
    ("out", False): _SQL_EDGES_OUT,
    ("out", True): _SQL_EDGES_OUT_REL,
    ("in", False): _SQL_EDGES_IN,
    ("in", True): _SQL_EDGES_IN_REL,
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fsync_path(path: Path) -> None:
    """Fsync a file and its parent directory on POSIX filesystems."""
    with path.open("rb") as fh:
        os.fsync(fh.fileno())
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def validate_sha256_digest(digest: str, *, field_name: str = "content_digest") -> str:
    """Validate a lowercase ``sha256:<64-hex>`` digest fail-closed."""
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        raise ValueError(
            f"{field_name} must match 'sha256:<64 lowercase hex chars>', got {digest!r}"
        )
    return digest


def validate_hmac_sha256_signature(signature: str, *, field_name: str = "chain_signature") -> str:
    """Validate a lowercase ``hmac-sha256:<64-hex>`` authentication tag fail-closed."""
    if not isinstance(signature, str) or not _HMAC_SHA256_RE.match(signature):
        raise ValueError(
            f"{field_name} must match 'hmac-sha256:<64 lowercase hex chars>', got {signature!r}"
        )
    return signature


def _validate_non_empty(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


# --------------------------------------------------------------------------- #
# Two-Layer Versioned Canonicalization Contract (F07):
#   Layer 1 (NEXUS_GRAPH_CANONICAL_V1): Domain-to-JSON AST normalization that
#     rejects IEEE-754 float (including NaN/Inf) fail-closed and maps exact
#     fractions.Fraction(p, q) to reduced {"__rational__": [num, den]} with
#     den > 0 and math.gcd(num, den) == 1.
#   Layer 2 (CanonicalizationScheme.JCS_RFC8785_V2): Strict RFC 8785 JCS
#     byte serialization and SHA-256 digest over the Layer-1 JSON tree.
# --------------------------------------------------------------------------- #
def _normalize_recursive(value: Any, seen: set[int], path: str) -> Any:
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if type(value) is float:
        raise CanonicalValueError(
            f"floating-point value rejected at {path}: use int or fractions.Fraction "
            f"under {CANONICAL_ENCODING_VERSION}"
        )
    if isinstance(value, Fraction):
        num = value.numerator
        den = value.denominator
        assert den > 0 and math.gcd(num, den) == 1  # pragma: no cover
        return {_RATIONAL_KEY: [num, den]}
    if type(value) in (list, tuple):
        ident = id(value)
        if ident in seen:
            raise CanonicalValueError(f"cyclic sequence detected at {path}")
        seen.add(ident)
        try:
            return [
                _normalize_recursive(item, seen, f"{path}[{idx}]") for idx, item in enumerate(value)
            ]
        finally:
            seen.discard(ident)
    if type(value) is dict:
        ident = id(value)
        if ident in seen:
            raise CanonicalValueError(f"cyclic mapping detected at {path}")
        seen.add(ident)
        try:
            normalized: dict[str, Any] = {}
            for key in sorted(value.keys(), key=str):
                if type(key) is not str:
                    raise CanonicalValueError(
                        f"mapping key at {path} must be exact str, got {type(key).__name__}"
                    )
                if key == _RATIONAL_KEY:
                    raise CanonicalValueError(
                        f"reserved key {_RATIONAL_KEY!r} is forbidden in user mappings at {path}"
                    )
                normalized[key] = _normalize_recursive(value[key], seen, f"{path}.{key}")
            return normalized
        finally:
            seen.discard(ident)
    raise CanonicalValueError(
        f"unsupported canonical type {type(value).__name__} at {path}; "
        "allowed: None, bool, int, str, Fraction, list, tuple, dict"
    )


def normalize_canonical_value(value: Any) -> Any:
    """Convert ``value`` into an exact, float-free JSON-compatible tree."""
    return _normalize_recursive(value, set(), "$")


def canonical_graph_digest(value: Any) -> str:
    """Compute a deterministic RFC 8785 JCS SHA-256 digest with zero float ambiguity."""
    normalized = normalize_canonical_value(value)
    return canonical_digest(normalized, CanonicalizationScheme.JCS_RFC8785_V2)


def canonical_graph_json(value: Any) -> str:
    """Serialize a canonicalized value to deterministic compact UTF-8 JSON."""
    normalized = normalize_canonical_value(value)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_stored_normalized(normalized_obj: dict[str, Any]) -> str:
    """Digest an already-normalized JSON object loaded back from SQLite."""
    return canonical_digest(normalized_obj, CanonicalizationScheme.JCS_RFC8785_V2)


# --------------------------------------------------------------------------- #
# Formal Ontology & Epistemology Models
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EntityIdentityTriple:
    """Formal ontology separating identity through change.

    * ``logical_entity_id`` — persistent semantic entity across revisions;
    * ``revision_id`` — historical state identifier within the causal graph;
    * ``content_digest`` — immutable SHA-256 fingerprint of content bytes.
    """

    logical_entity_id: str
    revision_id: str
    content_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "logical_entity_id",
            _validate_non_empty(self.logical_entity_id, field_name="logical_entity_id"),
        )
        object.__setattr__(
            self,
            "revision_id",
            _validate_non_empty(self.revision_id, field_name="revision_id"),
        )
        validate_sha256_digest(self.content_digest, field_name="content_digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "logical_entity_id": self.logical_entity_id,
            "revision_id": self.revision_id,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True)
class EpistemicProof:
    """Keyed HMAC-SHA256 verification witness required for ``VERIFIED_COMMIT``."""

    key_id: str
    verifier_id: str
    evidence_digest: str
    chain_signature: str
    issued_at_utc: str
    canonical_version: str = CANONICAL_ENCODING_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_id", _validate_non_empty(self.key_id, field_name="key_id"))
        object.__setattr__(
            self, "verifier_id", _validate_non_empty(self.verifier_id, field_name="verifier_id")
        )
        validate_sha256_digest(self.evidence_digest, field_name="evidence_digest")
        validate_hmac_sha256_signature(self.chain_signature, field_name="chain_signature")
        object.__setattr__(
            self,
            "issued_at_utc",
            _validate_non_empty(self.issued_at_utc, field_name="issued_at_utc"),
        )
        object.__setattr__(
            self,
            "canonical_version",
            _validate_non_empty(self.canonical_version, field_name="canonical_version"),
        )

    @property
    def verified_at(self) -> str:
        """Alias for ``issued_at_utc``."""
        return self.issued_at_utc

    def to_dict(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "verifier_id": self.verifier_id,
            "evidence_digest": self.evidence_digest,
            "chain_signature": self.chain_signature,
            "issued_at_utc": self.issued_at_utc,
            "canonical_version": self.canonical_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EpistemicProof:
        return cls(
            key_id=str(data["key_id"]),
            verifier_id=str(data["verifier_id"]),
            evidence_digest=str(data["evidence_digest"]),
            chain_signature=str(data["chain_signature"]),
            issued_at_utc=str(data["issued_at_utc"]),
            canonical_version=str(data["canonical_version"]),
        )


def _validate_secret_key(secret_key: bytes) -> bytes:
    if not isinstance(secret_key, (bytes, bytearray)) or len(secret_key) < _MIN_HMAC_KEY_BYTES:
        raise EpistemicProofError(
            f"HMAC verification secret_key must be bytes of length >= {_MIN_HMAC_KEY_BYTES}"
        )
    return bytes(secret_key)


def compute_proof_signature(
    *,
    secret_key: bytes,
    key_id: str,
    subject_id: str,
    subject_digest: str,
    verifier_id: str,
    evidence_digest: str,
    issued_at_utc: str,
    canonical_version: str = CANONICAL_ENCODING_VERSION,
) -> str:
    """Compute a keyed ``hmac-sha256:<hex>`` tag over canonical proof fields."""
    key_bytes = _validate_secret_key(secret_key)
    resolved_key_id = _validate_non_empty(key_id, field_name="key_id")
    validate_sha256_digest(subject_digest, field_name="subject_digest")
    validate_sha256_digest(evidence_digest, field_name="evidence_digest")
    verifier = _validate_non_empty(verifier_id, field_name="verifier_id")
    subject = _validate_non_empty(subject_id, field_name="subject_id")
    issued = _validate_non_empty(issued_at_utc, field_name="issued_at_utc")
    version = _validate_non_empty(canonical_version, field_name="canonical_version")
    envelope = canonical_graph_json(
        {
            "domain": _PROOF_DOMAIN,
            "canonical_version": version,
            "key_id": resolved_key_id,
            "verifier_id": verifier,
            "subject_id": subject,
            "subject_digest": subject_digest,
            "evidence_digest": evidence_digest,
            "issued_at_utc": issued,
        }
    ).encode("utf-8")
    tag = hmac.new(key_bytes, envelope, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{tag}"


def create_epistemic_proof(
    *,
    secret_key: bytes,
    key_id: str,
    subject_id: str,
    subject_digest: str,
    verifier_id: str,
    evidence_digest: str,
    issued_at_utc: str | None = None,
    canonical_version: str = CANONICAL_ENCODING_VERSION,
) -> EpistemicProof:
    """Mint a keyed HMAC-SHA256 :class:`EpistemicProof` for a node or edge."""
    issued = issued_at_utc or _now()
    signature = compute_proof_signature(
        secret_key=secret_key,
        key_id=key_id,
        subject_id=subject_id,
        subject_digest=subject_digest,
        verifier_id=verifier_id,
        evidence_digest=evidence_digest,
        issued_at_utc=issued,
        canonical_version=canonical_version,
    )
    return EpistemicProof(
        key_id=key_id,
        verifier_id=verifier_id,
        evidence_digest=evidence_digest,
        chain_signature=signature,
        issued_at_utc=issued,
        canonical_version=canonical_version,
    )


def _resolve_verification_key(
    key_id: str,
    proof_verification_keys: Mapping[str, bytes] | Callable[[str], bytes | None] | None,
    revoked_proof_key_ids: AbstractSet[str],
) -> bytes | None:
    if key_id in revoked_proof_key_ids or proof_verification_keys is None:
        return None
    raw_key = (
        proof_verification_keys(key_id)
        if callable(proof_verification_keys)
        else proof_verification_keys.get(key_id)
    )
    if not isinstance(raw_key, (bytes, bytearray)) or len(raw_key) < _MIN_HMAC_KEY_BYTES:
        return None
    return bytes(raw_key)


def verify_epistemic_proof(
    proof: EpistemicProof,
    *,
    subject_id: str,
    subject_digest: str,
    proof_verification_keys: Mapping[str, bytes] | Callable[[str], bytes | None] | None,
    revoked_proof_key_ids: AbstractSet[str] = frozenset(),
) -> bool:
    """Verify constant-time keyed HMAC-SHA256 authentication of an :class:`EpistemicProof`."""
    if proof.canonical_version != CANONICAL_ENCODING_VERSION:
        return False
    secret_key = _resolve_verification_key(
        proof.key_id, proof_verification_keys, revoked_proof_key_ids
    )
    if secret_key is None:
        return False
    expected = compute_proof_signature(
        secret_key=secret_key,
        key_id=proof.key_id,
        subject_id=subject_id,
        subject_digest=subject_digest,
        verifier_id=proof.verifier_id,
        evidence_digest=proof.evidence_digest,
        issued_at_utc=proof.issued_at_utc,
        canonical_version=proof.canonical_version,
    )
    return hmac.compare_digest(expected, proof.chain_signature)


def _coerce_epistemic_status(status: EpistemicStatus | str) -> EpistemicStatus:
    if isinstance(status, EpistemicStatus):
        return status
    try:
        return EpistemicStatus(status)
    except ValueError:
        allowed = tuple(item.value for item in EpistemicStatus)
        raise EpistemicProofError(
            f"unknown epistemic status {status!r}; expected one of {allowed}"
        ) from None


def _validate_epistemic_requirements(
    *,
    epistemic_status: EpistemicStatus,
    subject_id: str,
    subject_digest: str,
    proof: EpistemicProof | None,
    evidence_digest: str | None,
    proof_verification_keys: Mapping[str, bytes] | Callable[[str], bytes | None] | None = None,
    revoked_proof_key_ids: AbstractSet[str] = frozenset(),
) -> tuple[str | None, EpistemicProof | None]:
    """Enforce fail-closed keyed epistemic proof rules for nodes and edges."""
    resolved_evidence = evidence_digest
    if proof is not None:
        if resolved_evidence is not None and resolved_evidence != proof.evidence_digest:
            raise EpistemicProofError(
                "evidence_digest conflicts with proof.evidence_digest; refusing ambiguous evidence"
            )
        resolved_evidence = proof.evidence_digest
    if resolved_evidence is not None:
        try:
            validate_sha256_digest(resolved_evidence, field_name="evidence_digest")
        except ValueError as exc:
            raise EpistemicProofError(str(exc)) from exc

    if epistemic_status is EpistemicStatus.MEASURED_EVIDENCE and resolved_evidence is None:
        raise EpistemicProofError(
            f"{subject_id} cannot claim MEASURED_EVIDENCE without a valid evidence_digest"
        )
    if epistemic_status is EpistemicStatus.VERIFIED_COMMIT:
        if proof is None:
            raise EpistemicProofError(
                f"{subject_id} cannot enter VERIFIED_COMMIT without a verified EpistemicProof"
            )
        if not verify_epistemic_proof(
            proof,
            subject_id=subject_id,
            subject_digest=subject_digest,
            proof_verification_keys=proof_verification_keys,
            revoked_proof_key_ids=revoked_proof_key_ids,
        ):
            raise EpistemicProofError(
                f"{subject_id} proof HMAC-SHA256 authentication failed "
                f"(key_id={proof.key_id!r} untrusted, revoked, or forged)"
            )
    return resolved_evidence, proof


def node_identity(kind: str, identity: dict[str, Any]) -> str:
    """Deterministic, kind-scoped, RFC 8785 JCS identity of a graph node."""
    if kind not in NODE_KINDS:
        raise ValueError(f"unknown node kind {kind!r}; expected one of {NODE_KINDS}")
    if not identity:
        raise ValueError(f"a {kind} node needs a non-empty identity payload")
    return f"{kind}:{canonical_graph_digest(identity)}"


def edge_identity(relation: str, src: str, dst: str) -> str:
    """Deterministic identity of a directed causal edge."""
    if relation not in RELATIONS:
        raise ValueError(f"unknown relation {relation!r}; expected one of {RELATIONS}")
    return f"{relation}:{src}->{dst}"


# --------------------------------------------------------------------------- #
# Graph Data Structures
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GraphNode:
    node_id: str
    kind: str
    identity: dict[str, Any]
    logical_entity_id: str
    revision_id: str
    content_digest: str
    payload: dict[str, Any]
    payload_digest: str
    epistemic_status: EpistemicStatus
    evidence_digest: str | None
    proof: EpistemicProof | None
    status: str
    superseded_by: str | None
    source: str
    source_seq: int | None
    revision_count: int
    created_at: str
    updated_at: str

    @property
    def entity_triple(self) -> EntityIdentityTriple:
        return EntityIdentityTriple(
            logical_entity_id=self.logical_entity_id,
            revision_id=self.revision_id,
            content_digest=self.content_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "identity": self.identity,
            "logical_entity_id": self.logical_entity_id,
            "revision_id": self.revision_id,
            "content_digest": self.content_digest,
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "epistemic_status": self.epistemic_status.value,
            "evidence_digest": self.evidence_digest,
            "proof": None if self.proof is None else self.proof.to_dict(),
            "status": self.status,
            "superseded_by": self.superseded_by,
            "source": self.source,
            "source_seq": self.source_seq,
            "revision_count": self.revision_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    relation: str
    src: str
    dst: str
    payload: dict[str, Any]
    payload_digest: str
    epistemic_status: EpistemicStatus
    evidence_digest: str | None
    proof: EpistemicProof | None
    status: str
    source: str
    source_seq: int | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "relation": self.relation,
            "src": self.src,
            "dst": self.dst,
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "epistemic_status": self.epistemic_status.value,
            "evidence_digest": self.evidence_digest,
            "proof": None if self.proof is None else self.proof.to_dict(),
            "status": self.status,
            "source": self.source,
            "source_seq": self.source_seq,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class WriteResult:
    """Explicit write outcome exposing idempotency and revision advancement."""

    node: GraphNode | None = None
    edge: GraphEdge | None = None
    created: bool = False
    unchanged: bool = False
    superseded_previous: bool = False


@dataclass(frozen=True)
class CausalImpactResult:
    """Deterministic counterfactual impact set of modifying an upstream node."""

    origin_node_id: str
    impacted_node_ids: tuple[str, ...]
    impact_depths: dict[str, int]
    causal_paths: dict[str, tuple[str, ...]]
    by_kind: dict[str, tuple[str, ...]]
    impact_digest: str

    @property
    def affected_count(self) -> int:
        return len(self.impacted_node_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin_node_id": self.origin_node_id,
            "affected_count": self.affected_count,
            "impacted_node_ids": list(self.impacted_node_ids),
            "impact_depths": dict(self.impact_depths),
            "causal_paths": {k: list(v) for k, v in self.causal_paths.items()},
            "by_kind": {k: list(v) for k, v in self.by_kind.items()},
            "impact_digest": self.impact_digest,
        }


@dataclass(frozen=True)
class ChainEntry:
    """One append-only hash-chained mutation record in ``nexus_graph_chain``."""

    seq: int
    prev_hash: str
    entry_hash: str
    operation: str
    target_id: str
    entry_digest: str
    recorded_at: str


@dataclass(frozen=True)
class IntegrityFinding:
    code: str
    severity: str
    message: str
    witness: str


@dataclass
class IntegrityReport:
    findings: list[IntegrityFinding] = field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    history_count: int = 0
    chain_length: int = 0
    chain_head: str = GRAPH_CHAIN_GENESIS
    graph_digest: str | None = None

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "history_count": self.history_count,
            "chain_length": self.chain_length,
            "chain_head": self.chain_head,
            "graph_digest": self.graph_digest,
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity,
                    "message": f.message,
                    "witness": f.witness,
                }
                for f in self.findings
            ],
        }


def _compute_chain_entry_hash(
    *,
    prev_hash: str,
    operation: str,
    target_id: str,
    entry_digest: str,
    recorded_at: str,
) -> str:
    return canonical_graph_digest(
        {
            "domain": _CHAIN_DOMAIN,
            "prev_hash": prev_hash,
            "operation": operation,
            "target_id": target_id,
            "entry_digest": entry_digest,
            "recorded_at": recorded_at,
        }
    )


# --------------------------------------------------------------------------- #
# CreativeGraph — Durable, Strictly Acyclic, Tamper-Evident Projection
# --------------------------------------------------------------------------- #
class CreativeGraph:
    """Durable, rebuildable, mathematically verified creative provenance graph."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        connect_timeout: float = 30.0,
        verify_on_open: bool = True,
        proof_verification_keys: (
            Mapping[str, bytes] | Callable[[str], bytes | None] | None
        ) = None,
        revoked_proof_key_ids: AbstractSet[str] = frozenset(),
    ) -> None:
        self.db_path = Path(db_path)
        self._sqlite_path = str(db_path)
        self._connect_timeout = connect_timeout
        self._proof_verification_keys: dict[str, bytes] | Callable[[str], bytes | None] | None = (
            dict(proof_verification_keys)
            if isinstance(proof_verification_keys, Mapping)
            else proof_verification_keys
        )
        self._revoked_proof_key_ids: frozenset[str] = frozenset(revoked_proof_key_ids)
        self._lock = threading.RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if self._sqlite_path == ":memory:":
            self._memory_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_connection.row_factory = sqlite3.Row
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            if self._memory_connection is None:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
            connection.execute(_SCHEMA_NODE)
            connection.execute(_SCHEMA_EDGE)
            connection.execute(_SCHEMA_HISTORY)
            connection.execute(_SCHEMA_CHAIN)
            for index_sql in _INDEXES:
                connection.execute(index_sql)
        if verify_on_open:
            report = self.integrity_check()
            if not report.ok:
                codes = ", ".join(f.code for f in report.findings)
                raise GraphIntegrityError(
                    f"creative graph failed integrity verification on open: {codes}"
                )

    # ------------------------------------------------------------------ #
    # Writes — transactional, idempotent, acyclic, tamper-evident
    # ------------------------------------------------------------------ #
    def put_node(
        self,
        kind: str,
        identity: dict[str, Any],
        payload: dict[str, Any] | None = None,
        *,
        logical_entity_id: str | None = None,
        revision_id: str | None = None,
        content_digest: str | None = None,
        entity_triple: EntityIdentityTriple | None = None,
        epistemic_status: EpistemicStatus | str = EpistemicStatus.HYPOTHESIS,
        proof: EpistemicProof | None = None,
        evidence_digest: str | None = None,
        source: str = "direct",
        source_seq: int | None = None,
    ) -> WriteResult:
        """Create or advance one node with explicit ontology & epistemic checks."""
        with self._lock, self._write_connection() as connection:
            return self._put_node_tx(
                connection,
                kind,
                identity,
                payload,
                logical_entity_id=logical_entity_id,
                revision_id=revision_id,
                content_digest=content_digest,
                entity_triple=entity_triple,
                epistemic_status=epistemic_status,
                proof=proof,
                evidence_digest=evidence_digest,
                source=source,
                source_seq=source_seq,
            )

    def _resolve_ontology(
        self,
        *,
        kind: str,
        node_id: str,
        body_digest: str,
        logical_entity_id: str | None,
        revision_id: str | None,
        content_digest: str | None,
        entity_triple: EntityIdentityTriple | None,
        next_revision_number: int,
    ) -> EntityIdentityTriple:
        if entity_triple is not None:
            if (
                logical_entity_id is not None
                or revision_id is not None
                or content_digest is not None
            ):
                raise ValueError(
                    "pass either entity_triple or individual ontology fields, not both"
                )
            return entity_triple
        resolved_logical = logical_entity_id or f"{kind.lower()}:{node_id.split(':', 1)[1][:16]}"
        resolved_revision = revision_id or f"rev:{next_revision_number}"
        resolved_content = content_digest or body_digest
        return EntityIdentityTriple(
            logical_entity_id=resolved_logical,
            revision_id=resolved_revision,
            content_digest=resolved_content,
        )

    def _append_chain_tx(
        self,
        connection: sqlite3.Connection,
        *,
        operation: str,
        target_id: str,
        entry_digest: str,
        recorded_at: str,
    ) -> str:
        head_row = connection.execute(
            "SELECT entry_hash FROM nexus_graph_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = str(head_row[0]) if head_row is not None else GRAPH_CHAIN_GENESIS
        entry_hash = _compute_chain_entry_hash(
            prev_hash=prev_hash,
            operation=operation,
            target_id=target_id,
            entry_digest=entry_digest,
            recorded_at=recorded_at,
        )
        connection.execute(
            "INSERT INTO nexus_graph_chain"
            " (prev_hash, entry_hash, operation, target_id, entry_digest, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (prev_hash, entry_hash, operation, target_id, entry_digest, recorded_at),
        )
        return entry_hash

    def _put_node_tx(
        self,
        connection: sqlite3.Connection,
        kind: str,
        identity: dict[str, Any],
        payload: dict[str, Any] | None = None,
        *,
        logical_entity_id: str | None = None,
        revision_id: str | None = None,
        content_digest: str | None = None,
        entity_triple: EntityIdentityTriple | None = None,
        epistemic_status: EpistemicStatus | str = EpistemicStatus.HYPOTHESIS,
        proof: EpistemicProof | None = None,
        evidence_digest: str | None = None,
        source: str = "direct",
        source_seq: int | None = None,
    ) -> WriteResult:
        node_id = node_identity(kind, identity)
        body = dict(payload or {})
        body_json = canonical_graph_json(body)
        body_digest = canonical_graph_digest(body)
        epistemic = _coerce_epistemic_status(epistemic_status)
        stamp = _now()

        row = connection.execute(
            "SELECT payload_digest, status, revision_count, logical_entity_id,"
            " revision_id, content_digest, epistemic_status, evidence_digest, proof_json"
            " FROM nexus_graph_node WHERE node_id = ?",
            (node_id,),
        ).fetchone()

        next_rev = int(row[2]) + 1 if row is not None else 1
        triple = self._resolve_ontology(
            kind=kind,
            node_id=node_id,
            body_digest=body_digest,
            logical_entity_id=logical_entity_id,
            revision_id=revision_id,
            content_digest=content_digest,
            entity_triple=entity_triple,
            next_revision_number=next_rev,
        )
        resolved_evidence, resolved_proof = _validate_epistemic_requirements(
            epistemic_status=epistemic,
            subject_id=node_id,
            subject_digest=triple.content_digest,
            proof=proof,
            evidence_digest=evidence_digest,
            proof_verification_keys=self._proof_verification_keys,
            revoked_proof_key_ids=self._revoked_proof_key_ids,
        )
        proof_json = (
            None if resolved_proof is None else canonical_graph_json(resolved_proof.to_dict())
        )

        if row is not None:
            prev_digest = str(row[0])
            prev_status = str(row[1])
            prev_logical = str(row[3])
            prev_rev = str(row[4])
            prev_content = str(row[5])
            prev_epistemic = str(row[6])
            prev_evidence = None if row[7] is None else str(row[7])
            prev_proof_json = None if row[8] is None else str(row[8])
            if prev_status in TERMINAL_STATUSES:
                raise GraphStateError(
                    f"node {node_id} is {prev_status}; a write must supersede it "
                    "explicitly, never overwrite it"
                )
            same_revision_or_default = (
                revision_id is None and entity_triple is None
            ) or triple.revision_id == prev_rev
            if (
                prev_digest == body_digest
                and prev_status == "active"
                and prev_logical == triple.logical_entity_id
                and same_revision_or_default
                and prev_content == triple.content_digest
                and prev_epistemic == epistemic.value
                and prev_evidence == resolved_evidence
                and prev_proof_json == proof_json
            ):
                return WriteResult(node=self._node(connection, node_id), unchanged=True)
            if _EPISTEMIC_RANK[epistemic] < _EPISTEMIC_RANK[EpistemicStatus(prev_epistemic)]:
                raise EpistemicProofError(
                    f"node {node_id} cannot downgrade epistemic status from "
                    f"{prev_epistemic} to {epistemic.value}"
                )
            connection.execute(
                "UPDATE nexus_graph_node SET logical_entity_id = ?, revision_id = ?,"
                " content_digest = ?, payload_json = ?, payload_digest = ?,"
                " epistemic_status = ?, evidence_digest = ?, proof_json = ?,"
                " status = 'active', superseded_by = NULL, source = ?, source_seq = ?,"
                " revision_count = revision_count + 1, updated_at = ? WHERE node_id = ?",
                (
                    triple.logical_entity_id,
                    triple.revision_id,
                    triple.content_digest,
                    body_json,
                    body_digest,
                    epistemic.value,
                    resolved_evidence,
                    proof_json,
                    source,
                    source_seq,
                    stamp,
                    node_id,
                ),
            )
            connection.execute(
                "INSERT INTO nexus_graph_node_history"
                " (node_id, revision_id, content_digest, payload_json, payload_digest,"
                " epistemic_status, proof_json, status, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    node_id,
                    triple.revision_id,
                    triple.content_digest,
                    body_json,
                    body_digest,
                    epistemic.value,
                    proof_json,
                    stamp,
                ),
            )
            self._append_chain_tx(
                connection,
                operation="node_advance",
                target_id=node_id,
                entry_digest=canonical_graph_digest(
                    {
                        "triple": triple.to_dict(),
                        "payload_digest": body_digest,
                        "epistemic_status": epistemic.value,
                    }
                ),
                recorded_at=stamp,
            )
            return WriteResult(node=self._node(connection, node_id), superseded_previous=True)

        identity_json = canonical_graph_json(identity)
        identity_digest = canonical_graph_digest(identity)
        connection.execute(
            "INSERT INTO nexus_graph_node"
            " (node_id, kind, identity_json, identity_digest, logical_entity_id,"
            " revision_id, content_digest, payload_json, payload_digest,"
            " epistemic_status, evidence_digest, proof_json, status, superseded_by,"
            " source, source_seq, revision_count, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, 1, ?, ?)",
            (
                node_id,
                kind,
                identity_json,
                identity_digest,
                triple.logical_entity_id,
                triple.revision_id,
                triple.content_digest,
                body_json,
                body_digest,
                epistemic.value,
                resolved_evidence,
                proof_json,
                source,
                source_seq,
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO nexus_graph_node_history"
            " (node_id, revision_id, content_digest, payload_json, payload_digest,"
            " epistemic_status, proof_json, status, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                node_id,
                triple.revision_id,
                triple.content_digest,
                body_json,
                body_digest,
                epistemic.value,
                proof_json,
                stamp,
            ),
        )
        self._append_chain_tx(
            connection,
            operation="node_create",
            target_id=node_id,
            entry_digest=canonical_graph_digest(
                {
                    "triple": triple.to_dict(),
                    "payload_digest": body_digest,
                    "epistemic_status": epistemic.value,
                }
            ),
            recorded_at=stamp,
        )
        return WriteResult(node=self._node(connection, node_id), created=True)

    def _path_exists_tx(self, connection: sqlite3.Connection, start: str, target: str) -> bool:
        """Return True iff ``target`` is reachable from ``start`` along active edges."""
        visited: set[str] = {start}
        queue: deque[str] = deque([start])
        while queue:
            current = queue.popleft()
            rows = connection.execute(
                "SELECT dst FROM nexus_graph_edge WHERE src = ? AND status = 'active'",
                (current,),
            ).fetchall()
            for row in rows:
                nxt = str(row[0])
                if nxt == target:
                    return True
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        return False

    def put_edge(
        self,
        relation: str,
        src: str,
        dst: str,
        payload: dict[str, Any] | None = None,
        *,
        epistemic_status: EpistemicStatus | str = EpistemicStatus.HYPOTHESIS,
        proof: EpistemicProof | None = None,
        evidence_digest: str | None = None,
        source: str = "direct",
        source_seq: int | None = None,
    ) -> WriteResult:
        """Create one directed causal edge. Refuses missing endpoints and cycles."""
        with self._lock, self._write_connection() as connection:
            return self._put_edge_tx(
                connection,
                relation,
                src,
                dst,
                payload,
                epistemic_status=epistemic_status,
                proof=proof,
                evidence_digest=evidence_digest,
                source=source,
                source_seq=source_seq,
            )

    def _put_edge_tx(
        self,
        connection: sqlite3.Connection,
        relation: str,
        src: str,
        dst: str,
        payload: dict[str, Any] | None = None,
        *,
        epistemic_status: EpistemicStatus | str = EpistemicStatus.HYPOTHESIS,
        proof: EpistemicProof | None = None,
        evidence_digest: str | None = None,
        source: str = "direct",
        source_seq: int | None = None,
    ) -> WriteResult:
        edge_id = edge_identity(relation, src, dst)
        body = dict(payload or {})
        body_json = canonical_graph_json(body)
        body_digest = canonical_graph_digest(body)
        epistemic = _coerce_epistemic_status(epistemic_status)
        stamp = _now()

        endpoint_statuses: dict[str, str] = {}
        for endpoint in (src, dst):
            endpoint_row = connection.execute(
                "SELECT status FROM nexus_graph_node WHERE node_id = ?", (endpoint,)
            ).fetchone()
            if endpoint_row is None:
                raise GraphStateError(f"cannot link {relation}: endpoint {endpoint} does not exist")
            endpoint_statuses[endpoint] = str(endpoint_row[0])

        if src == dst:
            raise GraphCycleError(f"strict DAG violation: self-loop refused on {src}")
        if self._path_exists_tx(connection, dst, src):
            raise GraphCycleError(
                f"strict DAG violation: edge {src} -> {dst} ({relation}) creates a cycle"
            )

        resolved_evidence, resolved_proof = _validate_epistemic_requirements(
            epistemic_status=epistemic,
            subject_id=edge_id,
            subject_digest=body_digest,
            proof=proof,
            evidence_digest=evidence_digest,
            proof_verification_keys=self._proof_verification_keys,
            revoked_proof_key_ids=self._revoked_proof_key_ids,
        )
        proof_json = (
            None if resolved_proof is None else canonical_graph_json(resolved_proof.to_dict())
        )

        existing = connection.execute(
            "SELECT payload_digest, status, epistemic_status, evidence_digest, proof_json"
            " FROM nexus_graph_edge WHERE edge_id = ?",
            (edge_id,),
        ).fetchone()
        if existing is None and endpoint_statuses[src] in TERMINAL_STATUSES:
            raise GraphStateTransitionError(
                f"cannot attach outgoing edge {relation} from terminal node {src} "
                f"(status={endpoint_statuses[src]!r})"
            )
        if existing is not None:
            prev_digest = str(existing[0])
            prev_status = str(existing[1])
            prev_epistemic = str(existing[2])
            prev_evidence = None if existing[3] is None else str(existing[3])
            prev_proof_json = None if existing[4] is None else str(existing[4])
            if prev_status == "revoked":
                raise GraphStateError(
                    f"edge {edge_id} is revoked; re-asserting it needs an explicit revoke "
                    "reversal, not a silent write"
                )
            if (
                prev_digest == body_digest
                and prev_status == "active"
                and prev_epistemic == epistemic.value
                and prev_evidence == resolved_evidence
                and prev_proof_json == proof_json
            ):
                return WriteResult(edge=self._edge(connection, edge_id), unchanged=True)
            if _EPISTEMIC_RANK[epistemic] < _EPISTEMIC_RANK[EpistemicStatus(prev_epistemic)]:
                raise EpistemicProofError(
                    f"edge {edge_id} cannot downgrade epistemic status from "
                    f"{prev_epistemic} to {epistemic.value}"
                )
            connection.execute(
                "UPDATE nexus_graph_edge SET payload_json = ?, payload_digest = ?,"
                " epistemic_status = ?, evidence_digest = ?, proof_json = ?,"
                " status = 'active', source = ?, source_seq = ? WHERE edge_id = ?",
                (
                    body_json,
                    body_digest,
                    epistemic.value,
                    resolved_evidence,
                    proof_json,
                    source,
                    source_seq,
                    edge_id,
                ),
            )
            self._append_chain_tx(
                connection,
                operation="edge_update",
                target_id=edge_id,
                entry_digest=body_digest,
                recorded_at=stamp,
            )
            return WriteResult(edge=self._edge(connection, edge_id))

        connection.execute(
            "INSERT INTO nexus_graph_edge"
            " (edge_id, relation, src, dst, payload_json, payload_digest,"
            " epistemic_status, evidence_digest, proof_json, status, source,"
            " source_seq, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                edge_id,
                relation,
                src,
                dst,
                body_json,
                body_digest,
                epistemic.value,
                resolved_evidence,
                proof_json,
                source,
                source_seq,
                stamp,
            ),
        )
        self._append_chain_tx(
            connection,
            operation="edge_create",
            target_id=edge_id,
            entry_digest=body_digest,
            recorded_at=stamp,
        )
        return WriteResult(edge=self._edge(connection, edge_id), created=True)

    def promote_epistemic_status(
        self,
        target_id: str,
        new_status: EpistemicStatus | str,
        *,
        proof: EpistemicProof | None = None,
        evidence_digest: str | None = None,
        is_edge: bool = False,
    ) -> GraphNode | GraphEdge:
        """Monotonically promote a node or edge epistemic status with proof checks."""
        target_epistemic = _coerce_epistemic_status(new_status)
        stamp = _now()
        with self._lock, self._write_connection() as connection:
            if is_edge:
                edge = self._edge(connection, target_id)
                if edge is None:
                    raise GraphStateError(f"unknown edge {target_id}")
                if edge.status in TERMINAL_STATUSES:
                    raise GraphStateTransitionError(
                        f"cannot promote epistemic status of edge {target_id}: "
                        f"edge is {edge.status} (terminal is absorbing)"
                    )
                if _EPISTEMIC_RANK[target_epistemic] < _EPISTEMIC_RANK[edge.epistemic_status]:
                    raise EpistemicProofError(
                        f"cannot demote edge {target_id} from "
                        f"{edge.epistemic_status.value} to {target_epistemic.value}"
                    )
                resolved_evidence, resolved_proof = _validate_epistemic_requirements(
                    epistemic_status=target_epistemic,
                    subject_id=edge.edge_id,
                    subject_digest=edge.payload_digest,
                    proof=proof,
                    evidence_digest=evidence_digest or edge.evidence_digest,
                    proof_verification_keys=self._proof_verification_keys,
                    revoked_proof_key_ids=self._revoked_proof_key_ids,
                )
                proof_json = (
                    None
                    if resolved_proof is None
                    else canonical_graph_json(resolved_proof.to_dict())
                )
                connection.execute(
                    "UPDATE nexus_graph_edge SET epistemic_status = ?, evidence_digest = ?,"
                    " proof_json = ? WHERE edge_id = ?",
                    (target_epistemic.value, resolved_evidence, proof_json, target_id),
                )
                self._append_chain_tx(
                    connection,
                    operation="edge_epistemic_promote",
                    target_id=target_id,
                    entry_digest=canonical_graph_digest(
                        {
                            "epistemic_status": target_epistemic.value,
                            "evidence_digest": resolved_evidence,
                        }
                    ),
                    recorded_at=stamp,
                )
                updated_edge = self._edge(connection, target_id)
                assert updated_edge is not None  # pragma: no cover
                return updated_edge

            node = self._node(connection, target_id)
            if node is None:
                raise GraphStateError(f"unknown node {target_id}")
            if node.status in TERMINAL_STATUSES:
                raise GraphStateTransitionError(
                    f"cannot promote epistemic status of {target_id}: "
                    f"node is {node.status} (terminal is absorbing)"
                )
            if _EPISTEMIC_RANK[target_epistemic] < _EPISTEMIC_RANK[node.epistemic_status]:
                raise EpistemicProofError(
                    f"cannot demote node {target_id} from "
                    f"{node.epistemic_status.value} to {target_epistemic.value}"
                )
            resolved_evidence, resolved_proof = _validate_epistemic_requirements(
                epistemic_status=target_epistemic,
                subject_id=node.node_id,
                subject_digest=node.content_digest,
                proof=proof,
                evidence_digest=evidence_digest or node.evidence_digest,
                proof_verification_keys=self._proof_verification_keys,
                revoked_proof_key_ids=self._revoked_proof_key_ids,
            )
            proof_json = (
                None if resolved_proof is None else canonical_graph_json(resolved_proof.to_dict())
            )
            connection.execute(
                "UPDATE nexus_graph_node SET epistemic_status = ?, evidence_digest = ?,"
                " proof_json = ?, updated_at = ? WHERE node_id = ?",
                (target_epistemic.value, resolved_evidence, proof_json, stamp, target_id),
            )
            connection.execute(
                "INSERT INTO nexus_graph_node_history"
                " (node_id, revision_id, content_digest, payload_json, payload_digest,"
                " epistemic_status, proof_json, status, recorded_at)"
                " SELECT node_id, revision_id, content_digest, payload_json, payload_digest,"
                " ?, ?, status, ? FROM nexus_graph_node WHERE node_id = ?",
                (target_epistemic.value, proof_json, stamp, target_id),
            )
            self._append_chain_tx(
                connection,
                operation="node_epistemic_promote",
                target_id=target_id,
                entry_digest=canonical_graph_digest(
                    {
                        "epistemic_status": target_epistemic.value,
                        "evidence_digest": resolved_evidence,
                    }
                ),
                recorded_at=stamp,
            )
            updated_node = self._node(connection, target_id)
            assert updated_node is not None  # pragma: no cover
            return updated_node

    def set_status(
        self, node_id: str, status: str, *, superseded_by: str | None = None
    ) -> GraphNode:
        """Explicit lifecycle transition — preserves full append-only history."""
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
        stamp = _now()
        with self._lock, self._write_connection() as connection:
            self._set_status_tx(
                connection, node_id, status, superseded_by=superseded_by, stamp=stamp
            )
            node = self._node(connection, node_id)
        assert node is not None  # pragma: no cover
        return node

    def _set_status_tx(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        status: str,
        *,
        superseded_by: str | None,
        stamp: str,
    ) -> None:
        row = connection.execute(
            "SELECT status, superseded_by FROM nexus_graph_node WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            raise GraphStateError(f"unknown node {node_id}")
        current_status = str(row[0])
        allowed_targets = ALLOWED_NODE_STATUS_TRANSITIONS.get(current_status, frozenset())
        if status not in allowed_targets:
            raise GraphStateTransitionError(
                f"illegal status transition for {node_id}: {current_status!r} -> {status!r} "
                "(terminal statuses cannot be resurrected or reassigned)"
            )
        if current_status in TERMINAL_STATUSES:
            # Absorbing terminal state: idempotent no-op when re-asserting the exact same status.
            return
        connection.execute(
            "INSERT INTO nexus_graph_node_history"
            " (node_id, revision_id, content_digest, payload_json, payload_digest,"
            " epistemic_status, proof_json, status, recorded_at)"
            " SELECT node_id, revision_id, content_digest, payload_json, payload_digest,"
            " epistemic_status, proof_json, ?, ? FROM nexus_graph_node WHERE node_id = ?",
            (status, stamp, node_id),
        )
        connection.execute(
            "UPDATE nexus_graph_node SET status = ?, superseded_by = ?, updated_at = ?"
            " WHERE node_id = ?",
            (status, superseded_by, stamp, node_id),
        )
        self._append_chain_tx(
            connection,
            operation="node_status",
            target_id=node_id,
            entry_digest=canonical_graph_digest({"status": status, "superseded_by": superseded_by}),
            recorded_at=stamp,
        )

    def revoke_edge(self, edge_id: str) -> GraphEdge:
        """Explicitly revoke an edge so silent re-assertion is blocked."""
        stamp = _now()
        with self._lock, self._write_connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM nexus_graph_edge WHERE edge_id = ?", (edge_id,)
            ).fetchone()
            if row is None:
                raise GraphStateError(f"unknown edge {edge_id}")
            connection.execute(
                "UPDATE nexus_graph_edge SET status = 'revoked' WHERE edge_id = ?",
                (edge_id,),
            )
            self._append_chain_tx(
                connection,
                operation="edge_revoke",
                target_id=edge_id,
                entry_digest=canonical_graph_digest({"status": "revoked"}),
                recorded_at=stamp,
            )
            edge = self._edge(connection, edge_id)
        assert edge is not None  # pragma: no cover
        return edge

    # ------------------------------------------------------------------ #
    # Reads, Ontology Queries & Exact Graph Theory
    # ------------------------------------------------------------------ #
    def get_node(self, node_id: str) -> GraphNode | None:
        with self._lock, self._connection() as connection:
            return self._node(connection, node_id)

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        with self._lock, self._connection() as connection:
            return self._edge(connection, edge_id)

    def find(self, kind: str, identity: dict[str, Any]) -> GraphNode | None:
        return self.get_node(node_identity(kind, identity))

    def revisions_of_entity(self, logical_entity_id: str) -> list[GraphNode]:
        """Return every graph node sharing a persistent ``logical_entity_id``."""
        target = _validate_non_empty(logical_entity_id, field_name="logical_entity_id")
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT node_id, kind, identity_json, logical_entity_id, revision_id,"
                " content_digest, payload_json, payload_digest, epistemic_status,"
                " evidence_digest, proof_json, status, superseded_by, source,"
                " source_seq, revision_count, created_at, updated_at"
                " FROM nexus_graph_node WHERE logical_entity_id = ?"
                " ORDER BY revision_id, node_id",
                (target,),
            ).fetchall()
            return [self._node_from_row(row) for row in rows]

    def nodes_by_content_digest(self, content_digest: str) -> list[GraphNode]:
        """Return every node referencing an immutable ``content_digest``."""
        validate_sha256_digest(content_digest, field_name="content_digest")
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT node_id, kind, identity_json, logical_entity_id, revision_id,"
                " content_digest, payload_json, payload_digest, epistemic_status,"
                " evidence_digest, proof_json, status, superseded_by, source,"
                " source_seq, revision_count, created_at, updated_at"
                " FROM nexus_graph_node WHERE content_digest = ? ORDER BY node_id",
                (content_digest,),
            ).fetchall()
            return [self._node_from_row(row) for row in rows]

    def history(self, node_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT history_seq, revision_id, content_digest, payload_json,"
                " payload_digest, epistemic_status, proof_json, status, recorded_at"
                " FROM nexus_graph_node_history WHERE node_id = ? ORDER BY history_seq",
                (node_id,),
            ).fetchall()
        return [
            {
                "history_seq": int(row[0]),
                "revision_id": str(row[1]),
                "content_digest": str(row[2]),
                "payload": json.loads(str(row[3])),
                "payload_digest": str(row[4]),
                "epistemic_status": str(row[5]),
                "proof": None if row[6] is None else json.loads(str(row[6])),
                "status": str(row[7]),
                "recorded_at": str(row[8]),
            }
            for row in rows
        ]

    def _edges_conn(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        *,
        direction: str = "out",
        relation: str | None = None,
    ) -> list[GraphEdge]:
        sql = _EDGE_QUERY_TABLE[(direction, relation is not None)]
        params: tuple[str, ...] = (node_id,) if relation is None else (node_id, relation)
        rows = connection.execute(sql, params).fetchall()
        return [self._edge_from_row(row) for row in rows]

    def edges(
        self, node_id: str, *, direction: str = "out", relation: str | None = None
    ) -> list[GraphEdge]:
        """Query active incident edges using strictly static SQL statements."""
        if direction not in ("in", "out"):
            raise ValueError(f"direction must be 'in' or 'out', not {direction!r}")
        with self._lock, self._connection() as connection:
            return self._edges_conn(connection, node_id, direction=direction, relation=relation)

    def _lexicographic_kahn(self, node_ids: set[str], adjacency: dict[str, set[str]]) -> list[str]:
        """Deterministic Lexicographic Kahn's Algorithm (min-heap on ``node_id``)."""
        in_degree: dict[str, int] = {nid: 0 for nid in node_ids}
        for src in node_ids:
            for dst in adjacency.get(src, ()):
                if dst in node_ids:
                    in_degree[dst] += 1
        heap: list[str] = [nid for nid, deg in in_degree.items() if deg == 0]
        heapq.heapify(heap)
        ordered: list[str] = []
        while heap:
            current = heapq.heappop(heap)
            ordered.append(current)
            for nxt in sorted(adjacency.get(current, ())):
                if nxt in in_degree:
                    in_degree[nxt] -= 1
                    if in_degree[nxt] == 0:
                        heapq.heappush(heap, nxt)
        if len(ordered) != len(node_ids):
            raise GraphCycleError("strict DAG violation: cycle detected during topological sort")
        return ordered

    def _load_active_topology(
        self, connection: sqlite3.Connection, *, include_inactive: bool
    ) -> tuple[set[str], dict[str, set[str]]]:
        if include_inactive:
            node_rows = connection.execute("SELECT node_id FROM nexus_graph_node").fetchall()
        else:
            node_rows = connection.execute(
                "SELECT node_id FROM nexus_graph_node WHERE status = 'active'"
            ).fetchall()
        node_ids = {str(row[0]) for row in node_rows}
        edge_rows = connection.execute(
            "SELECT src, dst FROM nexus_graph_edge WHERE status = 'active'"
        ).fetchall()
        adjacency: dict[str, set[str]] = {nid: set() for nid in node_ids}
        for row in edge_rows:
            src, dst = str(row[0]), str(row[1])
            if src in node_ids and dst in node_ids:
                adjacency[src].add(dst)
        return node_ids, adjacency

    def topological_sort(self, *, include_inactive: bool = False) -> list[str]:
        """Return the unique deterministic lexicographic topological ordering."""
        with self._lock, self._connection() as connection:
            node_ids, adjacency = self._load_active_topology(
                connection, include_inactive=include_inactive
            )
        return self._lexicographic_kahn(node_ids, adjacency)

    def verify_acyclic(self, *, include_inactive: bool = False) -> bool:
        """Return True iff the graph is strictly acyclic."""
        try:
            self.topological_sort(include_inactive=include_inactive)
        except GraphCycleError:
            return False
        return True

    def _graph_digest_conn(
        self, connection: sqlite3.Connection, *, include_inactive: bool = False
    ) -> str:
        node_ids, adjacency = self._load_active_topology(
            connection, include_inactive=include_inactive
        )
        ordered_ids = self._lexicographic_kahn(node_ids, adjacency)
        node_rows = connection.execute(
            "SELECT node_id, kind, identity_json, logical_entity_id, revision_id,"
            " content_digest, payload_json, payload_digest, epistemic_status,"
            " evidence_digest, proof_json, status, superseded_by, source,"
            " source_seq, revision_count, created_at, updated_at"
            " FROM nexus_graph_node"
        ).fetchall()
        nodes_by_id = {str(row[0]): self._node_from_row(row) for row in node_rows}
        nodes_repr: list[dict[str, Any]] = []
        for nid in ordered_ids:
            node = nodes_by_id[nid]
            nodes_repr.append(
                {
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "logical_entity_id": node.logical_entity_id,
                    "revision_id": node.revision_id,
                    "content_digest": node.content_digest,
                    "payload_digest": node.payload_digest,
                    "epistemic_status": node.epistemic_status.value,
                    "evidence_digest": node.evidence_digest,
                    "proof_signature": (None if node.proof is None else node.proof.chain_signature),
                    "status": node.status,
                }
            )
        edge_rows = connection.execute(
            "SELECT edge_id, relation, src, dst, payload_json, payload_digest,"
            " epistemic_status, evidence_digest, proof_json, status, source,"
            " source_seq, created_at FROM nexus_graph_edge WHERE status = 'active'"
            " ORDER BY src, dst, relation, edge_id"
        ).fetchall()
        edges_repr: list[dict[str, Any]] = []
        for row in edge_rows:
            edge = self._edge_from_row(row)
            if edge.src in node_ids and edge.dst in node_ids:
                edges_repr.append(
                    {
                        "edge_id": edge.edge_id,
                        "relation": edge.relation,
                        "src": edge.src,
                        "dst": edge.dst,
                        "payload_digest": edge.payload_digest,
                        "epistemic_status": edge.epistemic_status.value,
                        "evidence_digest": edge.evidence_digest,
                        "proof_signature": (
                            None if edge.proof is None else edge.proof.chain_signature
                        ),
                    }
                )
        return canonical_graph_digest(
            {
                "canonical_version": CANONICAL_ENCODING_VERSION,
                "nodes": nodes_repr,
                "edges": edges_repr,
            }
        )

    def graph_digest(self, *, include_inactive: bool = False) -> str:
        """Compute an insertion-order-independent RFC 8785 JCS digest of the graph."""
        with self._lock, self._connection() as connection:
            return self._graph_digest_conn(connection, include_inactive=include_inactive)

    def traverse(
        self,
        start: str,
        *,
        direction: str = "out",
        relations: tuple[str, ...] | None = None,
        max_depth: int = 32,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Deterministic breadth-first traversal with a cycle guard."""
        if direction not in ("in", "out"):
            raise ValueError(f"direction must be 'in' or 'out', not {direction!r}")
        allowed = set(relations) if relations else None
        with self._lock:
            return self._traverse_locked(start, direction, allowed, max_depth, include_inactive)

    def _traverse_locked(
        self,
        start: str,
        direction: str,
        allowed: set[str] | None,
        max_depth: int,
        include_inactive: bool,
    ) -> list[dict[str, Any]]:
        seen: dict[str, int] = {start: 0}
        queue: deque[tuple[str, int, str]] = deque()
        for edge in self.edges(start, direction=direction):
            if allowed and edge.relation not in allowed:
                continue
            queue.append((edge.dst if direction == "out" else edge.src, 1, edge.relation))
        out: list[dict[str, Any]] = []
        while queue:
            current, depth, via = queue.popleft()
            if depth > max_depth or current in seen:
                continue
            seen[current] = depth
            node = self.get_node(current)
            if node is None:
                continue
            if node.status != "active" and not include_inactive:
                continue
            out.append({"node": node.to_dict(), "depth": depth, "via": via})
            if depth >= max_depth:
                continue
            for edge in self.edges(current, direction=direction):
                if allowed and edge.relation not in allowed:
                    continue
                nxt = edge.dst if direction == "out" else edge.src
                if nxt not in seen:
                    queue.append((nxt, depth + 1, edge.relation))
        out.sort(key=lambda row: (int(row["depth"]), str(row["node"]["node_id"])))
        return out

    def ancestors(self, node_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.traverse(node_id, direction="in", **kwargs)

    def descendants(self, node_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.traverse(node_id, direction="out", **kwargs)

    def lineage(self, artifact_node_id: str) -> dict[str, Any]:
        """Answer 'where did this artifact come from?' honestly without guessing."""
        with self._lock:
            return self._lineage_locked(artifact_node_id)

    def _lineage_locked(self, artifact_node_id: str) -> dict[str, Any]:
        node = self.get_node(artifact_node_id)
        if node is None:
            raise GraphStateError(f"unknown artifact node {artifact_node_id}")

        attachments: dict[str, list[dict[str, Any]]] = {}
        for relation in LINEAGE_ATTACHMENTS:
            found: list[dict[str, Any]] = []
            for edge in self.edges(artifact_node_id, direction="out", relation=relation):
                attached = self.get_node(edge.dst)
                if attached is not None and attached.status == "active":
                    found.append(attached.to_dict())
            attachments[relation] = sorted(found, key=lambda item: str(item["node_id"]))

        levels: list[dict[str, Any]] = []
        frontier = [artifact_node_id]
        reached_project = False
        for relation, direction in LINEAGE_CHAIN:
            nxt: list[str] = []
            found_level: list[dict[str, Any]] = []
            for current in frontier:
                for edge in self.edges(current, direction=direction, relation=relation):
                    parent_id = edge.src if direction == "in" else edge.dst
                    parent = self.get_node(parent_id)
                    if parent is None or parent.status != "active":
                        continue
                    if parent.node_id not in nxt:
                        nxt.append(parent.node_id)
                        found_level.append(parent.to_dict())
                        reached_project = reached_project or parent.kind == "PROJECT"
            levels.append(
                {
                    "relation": relation,
                    "found": sorted(found_level, key=lambda item: str(item["node_id"])),
                }
            )
            frontier = nxt
            if not nxt:
                break
        return {
            "artifact": node.to_dict(),
            "attachments": attachments,
            "levels": levels,
            "complete": reached_project,
            "deepest_kind": next(
                (level["found"][0]["kind"] for level in reversed(levels) if level["found"]),
                None,
            ),
        }

    def lineage_digest(self, node_id: str) -> str:
        """Compute the RFC 8785 JCS digest over a node's deterministic lineage."""
        with self._lock:
            node = self.get_node(node_id)
            if node is None:
                raise GraphStateError(f"unknown node {node_id}")
            ancestors = self.ancestors(node_id)
            ancestor_ids = {str(row["node"]["node_id"]) for row in ancestors}
            sub_ids = ancestor_ids | {node_id}
            with self._connection() as connection:
                all_nodes, full_adj = self._load_active_topology(connection, include_inactive=False)
                sub_active = sub_ids & all_nodes
                ordered = self._lexicographic_kahn(sub_active, full_adj)
                node_rows = connection.execute(
                    "SELECT node_id, kind, identity_json, logical_entity_id, revision_id,"
                    " content_digest, payload_json, payload_digest, epistemic_status,"
                    " evidence_digest, proof_json, status, superseded_by, source,"
                    " source_seq, revision_count, created_at, updated_at"
                    " FROM nexus_graph_node"
                ).fetchall()
                nodes_by_id = {str(r[0]): self._node_from_row(r) for r in node_rows}
                chain_nodes = [
                    {
                        "node_id": nodes_by_id[nid].node_id,
                        "logical_entity_id": nodes_by_id[nid].logical_entity_id,
                        "revision_id": nodes_by_id[nid].revision_id,
                        "content_digest": nodes_by_id[nid].content_digest,
                        "epistemic_status": nodes_by_id[nid].epistemic_status.value,
                    }
                    for nid in ordered
                ]
                edge_rows = connection.execute(
                    "SELECT src, dst, relation, payload_digest FROM nexus_graph_edge"
                    " WHERE status = 'active' ORDER BY src, dst, relation"
                ).fetchall()
                chain_edges = [
                    {
                        "src": str(r[0]),
                        "dst": str(r[1]),
                        "relation": str(r[2]),
                        "payload_digest": str(r[3]),
                    }
                    for r in edge_rows
                    if str(r[0]) in sub_active and str(r[1]) in sub_active
                ]
        return canonical_graph_digest(
            {
                "canonical_version": CANONICAL_ENCODING_VERSION,
                "target_node_id": node_id,
                "nodes": chain_nodes,
                "edges": chain_edges,
            }
        )

    def _compute_causal_impact_set_conn(
        self,
        connection: sqlite3.Connection,
        node_id: str,
        *,
        relations: tuple[str, ...] | None = None,
        include_inactive: bool = False,
    ) -> CausalImpactResult:
        """Compute exact downstream causal invalidation set on an active SQLite connection."""
        allowed = set(relations) if relations is not None else None
        origin = self._node(connection, node_id)
        if origin is None:
            raise GraphStateError(f"unknown origin node {node_id}")

        depths: dict[str, int] = {}
        paths: dict[str, tuple[str, ...]] = {}
        impacted_nodes: dict[str, GraphNode] = {}
        sub_adjacency: dict[str, set[str]] = {}

        # Deterministic level-by-level BFS for shortest lexicographic witness paths.
        frontier: dict[str, tuple[str, ...]] = {node_id: (node_id,)}
        visited: set[str] = {node_id}
        depth = 0
        while frontier:
            depth += 1
            next_frontier: dict[str, tuple[str, ...]] = {}
            for current in sorted(frontier.keys()):
                current_path = frontier[current]
                outgoing = self._edges_conn(connection, current, direction="out")
                for edge in outgoing:
                    if allowed is not None and edge.relation not in allowed:
                        continue
                    nxt = edge.dst
                    target_node = self._node(connection, nxt)
                    if target_node is None:
                        continue
                    if target_node.status != "active" and not include_inactive:
                        continue
                    if current != node_id:
                        sub_adjacency.setdefault(current, set()).add(nxt)
                    candidate_path = current_path + (nxt,)
                    if nxt not in visited:
                        prev_best = next_frontier.get(nxt)
                        if prev_best is None or candidate_path < prev_best:
                            next_frontier[nxt] = candidate_path
                        impacted_nodes[nxt] = target_node
            for nxt, best_path in sorted(next_frontier.items()):
                visited.add(nxt)
                depths[nxt] = depth
                paths[nxt] = best_path
            frontier = next_frontier

        # Capture edges between already-visited impacted nodes for exact topological order.
        impacted_id_set = set(impacted_nodes.keys())
        for nid in impacted_id_set:
            sub_adjacency.setdefault(nid, set())
            for edge in self._edges_conn(connection, nid, direction="out"):
                if allowed is not None and edge.relation not in allowed:
                    continue
                if edge.dst in impacted_id_set:
                    sub_adjacency[nid].add(edge.dst)

        ordered_impacted = tuple(self._lexicographic_kahn(impacted_id_set, sub_adjacency))
        by_kind_mut: dict[str, list[str]] = {}
        for nid in ordered_impacted:
            kind = impacted_nodes[nid].kind
            by_kind_mut.setdefault(kind, []).append(nid)
        by_kind = {kind: tuple(sorted(ids)) for kind, ids in sorted(by_kind_mut.items())}
        impact_digest = canonical_graph_digest(
            {
                "canonical_version": CANONICAL_ENCODING_VERSION,
                "origin_node_id": node_id,
                "impacted_node_ids": list(ordered_impacted),
                "impact_depths": {k: depths[k] for k in ordered_impacted},
                "causal_paths": {k: list(paths[k]) for k in ordered_impacted},
            }
        )
        return CausalImpactResult(
            origin_node_id=node_id,
            impacted_node_ids=ordered_impacted,
            impact_depths={k: depths[k] for k in ordered_impacted},
            causal_paths={k: paths[k] for k in ordered_impacted},
            by_kind=by_kind,
            impact_digest=impact_digest,
        )

    def compute_causal_impact_set(
        self,
        node_id: str,
        *,
        relations: tuple[str, ...] | None = None,
        include_inactive: bool = False,
    ) -> CausalImpactResult:
        """Compute exact downstream causal invalidation set with witness paths.

        Walks directed outgoing causal dependency edges from ``node_id`` to
        determine every downstream node that becomes stale/invalidated if
        ``node_id`` changes, ordered in deterministic Lexicographic Kahn order
        for reproducible recomputation.
        """
        with self._lock, self._connection() as connection:
            return self._compute_causal_impact_set_conn(
                connection,
                node_id,
                relations=relations,
                include_inactive=include_inactive,
            )

    def propagate_invalidation(
        self, node_id: str, *, reason: str, apply: bool = False
    ) -> CausalImpactResult:
        """Preview or atomically apply causal invalidation across downstream nodes.

        When ``apply=True``, SQLite ``BEGIN IMMEDIATE`` is acquired BEFORE
        computing the causal impact cone so concurrent OS processes cannot
        attach an active child downstream of an invalidated ancestor (F02).
        """
        _validate_non_empty(reason, field_name="reason")
        if not apply:
            return self.compute_causal_impact_set(node_id)
        stamp = _now()
        with self._lock, self._write_connection() as connection:
            impact = self._compute_causal_impact_set_conn(connection, node_id)
            for target_id in (node_id, *impact.impacted_node_ids):
                self._set_status_tx(
                    connection,
                    target_id,
                    "invalidated",
                    superseded_by=None,
                    stamp=stamp,
                )
            self._append_chain_tx(
                connection,
                operation="causal_invalidation_propagate",
                target_id=node_id,
                entry_digest=canonical_graph_digest(
                    {"reason": reason, "impact_digest": impact.impact_digest}
                ),
                recorded_at=stamp,
            )
            return impact

    def invalidation_candidates(self, node_id: str) -> dict[str, Any]:
        """Backward-compatible wrapper over :meth:`compute_causal_impact_set`."""
        impact = self.compute_causal_impact_set(node_id)
        rows = self.descendants(node_id)
        return {
            "origin": node_id,
            "affected_count": impact.affected_count,
            "by_kind": {k: list(v) for k, v in impact.by_kind.items()},
            "affected": rows,
            "impact_digest": impact.impact_digest,
        }

    def project_history(self, project_node_id: str) -> list[dict[str, Any]]:
        return self.descendants(project_node_id)

    def artifact_history(self, artifact_node_id: str) -> list[dict[str, Any]]:
        return self.descendants(artifact_node_id)

    def _chain_entries_conn(self, connection: sqlite3.Connection) -> list[ChainEntry]:
        rows = connection.execute(
            "SELECT seq, prev_hash, entry_hash, operation, target_id,"
            " entry_digest, recorded_at FROM nexus_graph_chain ORDER BY seq"
        ).fetchall()
        return [
            ChainEntry(
                seq=int(r[0]),
                prev_hash=str(r[1]),
                entry_hash=str(r[2]),
                operation=str(r[3]),
                target_id=str(r[4]),
                entry_digest=str(r[5]),
                recorded_at=str(r[6]),
            )
            for r in rows
        ]

    def chain_entries(self) -> list[ChainEntry]:
        with self._lock, self._connection() as connection:
            return self._chain_entries_conn(connection)

    # ------------------------------------------------------------------ #
    # Integrity, Tamper Detection, Atomic Snapshots, Backup & Restore
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_json(raw: object) -> dict[str, Any] | None:
        try:
            value = json.loads(str(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _verify_chain_conn(self, connection: sqlite3.Connection) -> list[IntegrityFinding]:
        findings: list[IntegrityFinding] = []
        entries = self._chain_entries_conn(connection)
        expected_prev = GRAPH_CHAIN_GENESIS
        for entry in entries:
            if not hmac.compare_digest(entry.prev_hash, expected_prev):
                findings.append(
                    IntegrityFinding(
                        "GRAPH050",
                        "error",
                        "chain prev_hash discontinuity",
                        f"seq={entry.seq}",
                    )
                )
            recomputed = _compute_chain_entry_hash(
                prev_hash=entry.prev_hash,
                operation=entry.operation,
                target_id=entry.target_id,
                entry_digest=entry.entry_digest,
                recorded_at=entry.recorded_at,
            )
            if not hmac.compare_digest(recomputed, entry.entry_hash):
                findings.append(
                    IntegrityFinding(
                        "GRAPH051",
                        "error",
                        "chain entry_hash mismatch",
                        f"seq={entry.seq}",
                    )
                )
            expected_prev = entry.entry_hash
        return findings

    def verify_chain(self) -> list[IntegrityFinding]:
        """Verify continuity and cryptographic hash of every chain record."""
        with self._lock, self._connection() as connection:
            return self._verify_chain_conn(connection)

    def _integrity_check_conn(
        self, connection: sqlite3.Connection, *, target_label: str
    ) -> IntegrityReport:
        """Full forensic audit on an open connection (nodes, edges, history, DAG & chain)."""
        report = IntegrityReport()
        nodes = connection.execute(
            "SELECT node_id, kind, payload_json, payload_digest, status, identity_json,"
            " identity_digest, logical_entity_id, revision_id, content_digest,"
            " epistemic_status, evidence_digest, proof_json, revision_count"
            " FROM nexus_graph_node"
        ).fetchall()
        edges = connection.execute(
            "SELECT edge_id, relation, src, dst, payload_json, payload_digest,"
            " epistemic_status, evidence_digest, proof_json FROM nexus_graph_edge"
        ).fetchall()
        history = connection.execute(
            "SELECT history_seq, node_id, revision_id, content_digest, payload_json,"
            " payload_digest, epistemic_status, proof_json, status, recorded_at"
            " FROM nexus_graph_node_history ORDER BY history_seq ASC"
        ).fetchall()
        chain = connection.execute(
            "SELECT seq, operation, target_id, entry_hash FROM nexus_graph_chain ORDER BY seq"
        ).fetchall()

        report.node_count = len(nodes)
        report.edge_count = len(edges)
        report.history_count = len(history)
        report.chain_length = len(chain)
        report.chain_head = str(chain[-1][3]) if chain else GRAPH_CHAIN_GENESIS

        known = {str(row[0]) for row in nodes}
        node_live_state: dict[str, tuple[str, str, str, str]] = {}
        revision_count_by_node: dict[str, int] = {}
        for row in nodes:
            node_id = str(row[0])
            kind = str(row[1])
            node_live_state[node_id] = (str(row[8]), str(row[9]), str(row[3]), str(row[4]))
            revision_count_by_node[node_id] = int(row[13])
            if kind not in NODE_KINDS:
                report.findings.append(
                    IntegrityFinding("GRAPH010", "error", "unknown node kind", node_id)
                )
            if str(row[4]) not in STATUSES:
                report.findings.append(
                    IntegrityFinding("GRAPH011", "error", "unknown node status", node_id)
                )
            payload = self._safe_json(row[2])
            if payload is None:
                report.findings.append(
                    IntegrityFinding(
                        "GRAPH012", "error", "payload_json is not a JSON object", node_id
                    )
                )
            elif _digest_stored_normalized(payload) != str(row[3]):
                report.findings.append(
                    IntegrityFinding("GRAPH012", "error", "payload digest mismatch", node_id)
                )
            identity = self._safe_json(row[5])
            if identity is None:
                report.findings.append(
                    IntegrityFinding(
                        "GRAPH013", "error", "identity_json is not a JSON object", node_id
                    )
                )
            else:
                computed_identity_digest = _digest_stored_normalized(identity)
                if computed_identity_digest != str(row[6]):
                    report.findings.append(
                        IntegrityFinding("GRAPH013", "error", "identity digest mismatch", node_id)
                    )
                if kind in NODE_KINDS:
                    expected_nid = f"{kind}:{computed_identity_digest}"
                    if not hmac.compare_digest(node_id, expected_nid):
                        report.findings.append(
                            IntegrityFinding(
                                "GRAPH016",
                                "error",
                                "node_id does not match canonical kind/identity derivation",
                                node_id,
                            )
                        )
            try:
                EntityIdentityTriple(
                    logical_entity_id=str(row[7]),
                    revision_id=str(row[8]),
                    content_digest=str(row[9]),
                )
            except ValueError:
                report.findings.append(
                    IntegrityFinding("GRAPH014", "error", "invalid ontology triple", node_id)
                )
            self._audit_epistemic_row(
                report=report,
                subject_id=node_id,
                subject_digest=str(row[9]),
                raw_status=str(row[10]),
                raw_evidence=None if row[11] is None else str(row[11]),
                raw_proof=row[12],
                code="GRAPH015",
            )

        for row in edges:
            edge_id = str(row[0])
            relation = str(row[1])
            src = str(row[2])
            dst = str(row[3])
            if relation not in RELATIONS:
                report.findings.append(
                    IntegrityFinding("GRAPH020", "error", "unknown relation", edge_id)
                )
            else:
                expected_eid = edge_identity(relation, src, dst)
                if not hmac.compare_digest(edge_id, expected_eid):
                    report.findings.append(
                        IntegrityFinding(
                            "GRAPH024",
                            "error",
                            "edge_id does not match canonical relation/src/dst derivation",
                            edge_id,
                        )
                    )
            for endpoint in (src, dst):
                if endpoint not in known:
                    report.findings.append(
                        IntegrityFinding(
                            "GRAPH021",
                            "error",
                            "edge endpoint does not exist",
                            f"{edge_id}->{endpoint}",
                        )
                    )
            edge_payload = self._safe_json(row[4])
            if edge_payload is None:
                report.findings.append(
                    IntegrityFinding(
                        "GRAPH022", "error", "edge payload_json is not a JSON object", edge_id
                    )
                )
            elif _digest_stored_normalized(edge_payload) != str(row[5]):
                report.findings.append(
                    IntegrityFinding("GRAPH022", "error", "edge digest mismatch", edge_id)
                )
            self._audit_epistemic_row(
                report=report,
                subject_id=edge_id,
                subject_digest=str(row[5]),
                raw_status=str(row[6]),
                raw_evidence=None if row[7] is None else str(row[7]),
                raw_proof=row[8],
                code="GRAPH023",
            )

        # Full historical row audit (F09): payload digest, ontology/status continuity & chain link
        history_by_node: dict[str, list[tuple[int, str, str, str, str]]] = {}
        node_chain_ops = frozenset(
            {"node_create", "node_advance", "node_status", "node_epistemic_promote"}
        )
        chain_count_by_node: dict[str, int] = {}
        # Parity is measured from the node's most recent ``node_create``: a
        # replace-rebuild re-creates a projected node (revision_count restarts at 1)
        # while the append-only chain keeps the entries of the deleted projection.
        last_create_seq_by_node: dict[str, int] = {}
        for chain_row in chain:
            if str(chain_row[1]) == "node_create":
                last_create_seq_by_node[str(chain_row[2])] = int(chain_row[0])
        revision_chain_count_by_node: dict[str, int] = {}
        for chain_row in chain:
            operation = str(chain_row[1])
            tid = str(chain_row[2])
            if operation in node_chain_ops:
                chain_count_by_node[tid] = chain_count_by_node.get(tid, 0) + 1
            if operation in REVISION_CHAIN_OPS and int(chain_row[0]) >= last_create_seq_by_node.get(
                tid, -1
            ):
                revision_chain_count_by_node[tid] = revision_chain_count_by_node.get(tid, 0) + 1

        for hrow in history:
            h_seq = int(hrow[0])
            h_node_id = str(hrow[1])
            h_rev = str(hrow[2])
            h_content = str(hrow[3])
            h_payload = self._safe_json(hrow[4])
            h_payload_digest = str(hrow[5])
            h_epistemic = str(hrow[6])
            h_status = str(hrow[8])
            h_label = f"history_seq={h_seq}:{h_node_id}"
            history_by_node.setdefault(h_node_id, []).append(
                (h_seq, h_rev, h_content, h_payload_digest, h_status)
            )
            if h_payload is None or _digest_stored_normalized(h_payload) != h_payload_digest:
                report.findings.append(
                    IntegrityFinding(
                        "GRAPH031", "error", "history payload digest mismatch", h_label
                    )
                )
            valid_h_meta = (
                h_node_id in known
                and h_status in STATUSES
                and bool(h_rev.strip())
                and bool(_SHA256_RE.match(h_content))
            )
            try:
                _coerce_epistemic_status(h_epistemic)
            except EpistemicProofError:
                valid_h_meta = False
            if not valid_h_meta:
                report.findings.append(
                    IntegrityFinding(
                        "GRAPH032", "error", "history metadata or ontology invalid", h_label
                    )
                )

        for node_id in sorted(known):
            declared_revisions = revision_count_by_node[node_id]
            observed_revisions = revision_chain_count_by_node.get(node_id, 0)
            if declared_revisions != observed_revisions:
                report.findings.append(
                    IntegrityFinding(
                        "GRAPH034",
                        "error",
                        f"revision_count {declared_revisions} disagrees with "
                        f"{observed_revisions} node_create/node_advance chain entr(ies)",
                        node_id,
                    )
                )
            node_hist = history_by_node.get(node_id)
            if not node_hist:
                report.findings.append(
                    IntegrityFinding("GRAPH030", "error", "node has no history row", node_id)
                )
                continue
            prev_status: str | None = None
            for h_seq, _h_rev, _h_content, _h_pdig, h_status in node_hist:
                if prev_status is not None and h_status not in ALLOWED_NODE_STATUS_TRANSITIONS.get(
                    prev_status, frozenset()
                ):
                    report.findings.append(
                        IntegrityFinding(
                            "GRAPH032",
                            "error",
                            f"non-monotonic terminal history transition {prev_status}->{h_status}",
                            f"history_seq={h_seq}:{node_id}",
                        )
                    )
                prev_status = h_status
            _, last_rev, last_content, last_pdig, last_status = node_hist[-1]
            if (last_rev, last_content, last_pdig, last_status) != node_live_state[node_id]:
                report.findings.append(
                    IntegrityFinding(
                        "GRAPH032",
                        "error",
                        "latest history row disagrees with live node state",
                        node_id,
                    )
                )
            if chain_count_by_node.get(node_id, 0) < len(node_hist):
                report.findings.append(
                    IntegrityFinding(
                        "GRAPH033",
                        "error",
                        "history rows lack corresponding hash-chain linkage",
                        node_id,
                    )
                )

        node_ids_all, adj_all = self._load_active_topology(connection, include_inactive=True)
        try:
            self._lexicographic_kahn(node_ids_all, adj_all)
        except GraphCycleError:
            report.findings.append(
                IntegrityFinding("GRAPH040", "error", "graph contains a cycle", target_label)
            )
        else:
            if not report.findings:
                report.graph_digest = self._graph_digest_conn(connection, include_inactive=False)

        report.findings.extend(self._verify_chain_conn(connection))
        return report

    def integrity_check(self) -> IntegrityReport:
        """Full forensic audit of nodes, ontology, proofs, edges, history, DAG & chain."""
        with self._lock, self._connection() as connection:
            return self._integrity_check_conn(connection, target_label=self._sqlite_path)

    def _audit_epistemic_row(
        self,
        *,
        report: IntegrityReport,
        subject_id: str,
        subject_digest: str,
        raw_status: str,
        raw_evidence: str | None,
        raw_proof: object,
        code: str,
    ) -> None:
        try:
            epistemic = _coerce_epistemic_status(raw_status)
        except EpistemicProofError:
            report.findings.append(
                IntegrityFinding(code, "error", "unknown epistemic status", subject_id)
            )
            return
        proof_obj: EpistemicProof | None = None
        if raw_proof is not None:
            parsed = self._safe_json(raw_proof)
            if parsed is None:
                report.findings.append(
                    IntegrityFinding(code, "error", "corrupt proof_json", subject_id)
                )
                return
            try:
                proof_obj = EpistemicProof.from_dict(parsed)
            except (KeyError, ValueError):
                report.findings.append(
                    IntegrityFinding(code, "error", "malformed EpistemicProof", subject_id)
                )
                return
        try:
            _validate_epistemic_requirements(
                epistemic_status=epistemic,
                subject_id=subject_id,
                subject_digest=subject_digest,
                proof=proof_obj,
                evidence_digest=raw_evidence,
                proof_verification_keys=self._proof_verification_keys,
                revoked_proof_key_ids=self._revoked_proof_key_ids,
            )
        except (EpistemicProofError, ValueError):
            report.findings.append(
                IntegrityFinding(code, "error", "epistemic proof binding invalid", subject_id)
            )

    def export_sealed_snapshot(self, target: Path | str) -> Path:
        """Atomically export a canonical, fsynced, tamper-evident snapshot file."""
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = self.integrity_check()
        if not report.ok:
            raise GraphIntegrityError("refusing to export snapshot from an unhealthy graph")
        with self._lock:
            ordered = self.topological_sort(include_inactive=True)
            nodes = [node.to_dict() for nid in ordered if (node := self.get_node(nid)) is not None]
            with self._connection() as connection:
                edge_rows = connection.execute(
                    "SELECT edge_id FROM nexus_graph_edge ORDER BY edge_id"
                ).fetchall()
                edges = [
                    edge.to_dict()
                    for row in edge_rows
                    if (edge := self._edge(connection, str(row[0]))) is not None
                ]
            body = {
                "schema_version": "creative_graph_snapshot.v2",
                "canonical_version": CANONICAL_ENCODING_VERSION,
                "graph_digest": report.graph_digest,
                "chain_head": report.chain_head,
                "chain_length": report.chain_length,
                "nodes": nodes,
                "edges": edges,
            }
            seal = _digest_stored_normalized(body)
            envelope = {"body": body, "snapshot_seal": seal}
            encoded = json.dumps(
                envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )

        fd, tmp_name = tempfile.mkstemp(
            prefix=".graph_snapshot_", suffix=".tmp", dir=str(destination.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, destination)
            _fsync_path(destination)
        except BaseException:  # pragma: no cover
            tmp_path.unlink(missing_ok=True)
            raise
        return destination

    @staticmethod
    def verify_sealed_snapshot(source: Path | str) -> dict[str, Any]:
        """Read and cryptographically verify an exported snapshot file."""
        path = Path(source)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GraphIntegrityError(f"unreadable snapshot {path}: {exc}") from exc
        if not isinstance(parsed, dict) or "body" not in parsed or "snapshot_seal" not in parsed:
            raise GraphIntegrityError(f"malformed snapshot envelope in {path}")
        body = parsed["body"]
        seal = str(parsed["snapshot_seal"])
        if not isinstance(body, dict):
            raise GraphIntegrityError(f"malformed snapshot body in {path}")
        if body.get("canonical_version") != CANONICAL_ENCODING_VERSION:
            raise GraphIntegrityError(
                f"unsupported snapshot canonical_version in {path}: "
                f"{body.get('canonical_version')!r}"
            )
        actual = _digest_stored_normalized(body)
        if not hmac.compare_digest(actual, seal):
            raise GraphIntegrityError(
                f"snapshot seal mismatch in {path}: expected {seal}, computed {actual}"
            )
        return body

    def backup(self, target: Path | str) -> Path:
        """Consistent online SQLite backup followed by file & directory fsync."""
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        output = sqlite3.connect(str(destination))
        try:
            with self._lock, self._connection() as connection:
                connection.backup(output)
        finally:
            output.close()
        _fsync_path(destination)
        return destination

    def restore(self, source: Path | str, *, whole_database: bool = False) -> None:
        """Whole-file restore with pre-restore logical integrity & rollback safety (F05)."""
        source_path = Path(source)
        # 1A: open the backup strictly read-only so a restore can never mutate its source.
        try:
            origin = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True)
        except sqlite3.DatabaseError as exc:
            raise GraphStateError(f"restore source {source} is not a readable database") from exc
        origin.row_factory = sqlite3.Row
        try:
            try:
                missing = GRAPH_TABLES - _tables(origin)
                check = origin.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.DatabaseError as exc:
                raise GraphStateError(
                    f"restore source {source} is not a readable database"
                ) from exc
            if missing:
                raise GraphStateError(
                    f"restore source {source} is not a graph backup: "
                    f"missing table(s) {sorted(missing)}"
                )
            if check is None or str(check[0]) != "ok":
                raise GraphStateError(f"restore source {source} failed PRAGMA integrity_check")

            # F05: Verify full logical graph & hash-chain integrity BEFORE modifying target.
            source_report = self._integrity_check_conn(origin, target_label=str(source_path))
            if not source_report.ok:
                codes = ", ".join(f.code for f in source_report.findings)
                raise GraphIntegrityError(
                    f"restore source {source} failed logical integrity check: {codes}"
                )

            with self._lock:
                if self._memory_connection is not None:
                    foreign = _tables(self._memory_connection) - GRAPH_TABLES
                    if foreign and not whole_database:
                        raise GraphStateError(
                            f"restore would replace the WHOLE in-memory database holding "
                            f"non-graph table(s) {sorted(foreign)}; pass whole_database=True "
                            "only if you own the entire file"
                        )
                    rollback_mem = sqlite3.connect(":memory:")
                    try:
                        self._memory_connection.backup(rollback_mem)
                        origin.backup(self._memory_connection)
                        post_mem = self._integrity_check_conn(
                            self._memory_connection, target_label=self._sqlite_path
                        )
                        if not post_mem.ok:  # pragma: no cover
                            rollback_mem.backup(self._memory_connection)
                            codes = ", ".join(f.code for f in post_mem.findings)
                            raise GraphIntegrityError(
                                f"post-restore integrity check failed: {codes}"
                            )
                    finally:
                        rollback_mem.close()
                    return

                pre_restore_fd, pre_restore_name = tempfile.mkstemp(
                    prefix=".graph_pre_restore_",
                    suffix=".sqlite",
                    dir=str(self.db_path.parent),
                )
                os.close(pre_restore_fd)
                pre_restore_path = Path(pre_restore_name)
                try:
                    connection = sqlite3.connect(self._sqlite_path, timeout=self._connect_timeout)
                    try:
                        foreign = _tables(connection) - GRAPH_TABLES
                        if foreign and not whole_database:
                            raise GraphStateError(
                                f"restore would replace the WHOLE database {self._sqlite_path} "
                                f"holding non-graph table(s) {sorted(foreign)}; pass "
                                "whole_database=True only if you own the entire file"
                            )
                        staging = sqlite3.connect(str(pre_restore_path))
                        try:
                            connection.backup(staging)
                        finally:
                            staging.close()
                        origin.backup(connection)
                        connection.commit()
                    finally:
                        connection.close()
                    _fsync_path(self.db_path)
                    post_report = self.integrity_check()
                    if not post_report.ok:  # pragma: no cover
                        rollback_src = sqlite3.connect(str(pre_restore_path))
                        rollback_dst = sqlite3.connect(
                            self._sqlite_path, timeout=self._connect_timeout
                        )
                        try:
                            rollback_src.backup(rollback_dst)
                            rollback_dst.commit()
                        finally:
                            rollback_dst.close()
                            rollback_src.close()
                        _fsync_path(self.db_path)
                        codes = ", ".join(f.code for f in post_report.findings)
                        raise GraphIntegrityError(f"post-restore integrity check failed: {codes}")
                finally:
                    pre_restore_path.unlink(missing_ok=True)
        finally:
            origin.close()

    @staticmethod
    def _is_verification_passed(verification: dict[str, Any] | None) -> bool:
        if not isinstance(verification, dict) or not verification:
            return False
        if verification.get("passed") is False or verification.get("verified") is False:
            return False
        if verification.get("passed") is True or verification.get("verified") is True:
            return True
        status_val = str(verification.get("status", "")).strip().lower()
        return status_val in {"passed", "verified", "ok"}

    @staticmethod
    def _is_passport_authoritative(facts: JobFacts | None) -> bool:
        if (
            facts is None
            or not facts.artifact_passport_known
            or not isinstance(facts.artifact_passport, dict)
            or not facts.artifact_passport
        ):
            return False
        status_val = str(facts.artifact_passport.get("status", "")).strip().lower()
        return status_val not in {"compromised", "failed", "invalid", "unverified", "degraded"}

    @staticmethod
    def _validate_source_journal_records(records: list[LedgerRecord]) -> None:
        """Fail-closed cryptographic & schema verification of source ledger records (F03)."""
        try:
            verdict = verify_journal_chain(records)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise GraphIntegrityError(f"malformed source causal journal record: {exc}") from exc
        if not verdict.ok:
            raise GraphIntegrityError(
                f"source causal journal chain invalid at seq={verdict.first_broken_seq}: "
                f"{verdict.reason}"
            )
        for record in sorted(records, key=lambda item: item.seq):
            try:
                payload = record.payload
                kind = EventKind(str(payload["kind"]))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise GraphIntegrityError(
                    f"source causal journal seq={record.seq} has invalid kind/payload: {exc}"
                ) from exc
            job_id = payload.get("job_id")
            if not isinstance(job_id, str) or not job_id.strip():
                raise GraphIntegrityError(
                    f"source causal journal seq={record.seq} is missing a valid job_id"
                )
            attempt_val = payload.get("attempt")
            if attempt_val is not None and (
                isinstance(attempt_val, bool) or not isinstance(attempt_val, int)
            ):
                raise GraphIntegrityError(
                    f"source causal journal seq={record.seq} has invalid attempt {attempt_val!r}"
                )
            expected_dedupe = journal_dedupe_key(kind, job_id, attempt_val)
            if record.dedupe_key != expected_dedupe:
                raise GraphIntegrityError(
                    f"source causal journal seq={record.seq} dedupe_key mismatch: "
                    f"expected {expected_dedupe!r}, got {record.dedupe_key!r}"
                )

    @staticmethod
    def _validate_journal_store_columns(journal: CausalJournal) -> None:
        """Delegate indexed-column vs ``record_json`` check to :class:`CausalJournal` (F03)."""
        try:
            journal.verify_column_integrity()
        except ValueError as exc:
            raise GraphIntegrityError(str(exc)) from exc

    def rebuild_from_journal_store(
        self,
        journal: CausalJournal,
        *,
        replace: bool = False,
        job_facts: Mapping[str, JobFacts] | None = None,
    ) -> dict[str, Any]:
        """Rebuild the projection by reading records through :class:`CausalJournal`."""
        self._validate_journal_store_columns(journal)
        return self.rebuild_from_journal(
            journal.all_records(), replace=replace, job_facts=job_facts
        )

    def rebuild_from_journal(
        self,
        records: list[LedgerRecord],
        *,
        replace: bool = False,
        job_facts: Mapping[str, JobFacts] | None = None,
    ) -> dict[str, Any]:
        """Atomically reconstruct ledger-witnessed facts with chain & proof honesty (F03/F04)."""
        self._validate_source_journal_records(records)
        facts_map = dict(job_facts) if job_facts is not None else {}

        executions: dict[str, dict[str, Any]] = {}
        artifacts: dict[str, dict[str, Any]] = {}
        verifications: dict[str, dict[str, Any]] = {}
        passports: dict[str, dict[str, Any]] = {}
        order: list[str] = []

        for record in sorted(records, key=lambda item: item.seq):
            payload = record.payload
            job_id = str(payload["job_id"])
            attempt = payload.get("attempt")
            kind = str(payload["kind"])
            seq = int(record.seq)
            evidence_hash = (
                f"sha256:{record.record_hash}"
                if not record.record_hash.startswith("sha256:")
                else record.record_hash
            )
            exe_key = canonical_graph_json({"job_id": job_id, "attempt": attempt})
            executions[exe_key] = {
                "identity": {"job_id": job_id, "attempt": attempt},
                "logical_entity_id": f"execution:{job_id}",
                "revision_id": f"attempt:{attempt if attempt is not None else 0}:seq:{seq}",
                "epistemic_status": EpistemicStatus.MEASURED_EVIDENCE,
                "evidence_digest": evidence_hash,
                "payload": {
                    "job_type": payload.get("job_type"),
                    "status": payload.get("status"),
                    "ledger_kind": kind,
                    "ledger_seq": seq,
                    "ledger_record_hash": record.record_hash,
                },
                "seq": seq,
            }
            order.append(exe_key)
            facts = facts_map.get(job_id)
            ver_passed = facts is not None and self._is_verification_passed(facts.verification)
            if kind == EventKind.JOB_VERIFICATION_STARTED.value:
                # Verification initiation ONLY (PROCESSING -> VERIFYING), never a passed proof.
                verifications[exe_key] = {
                    "identity": {"job_id": job_id, "attempt": attempt},
                    "logical_entity_id": f"verification:{job_id}",
                    "revision_id": f"seq:{seq}",
                    "epistemic_status": EpistemicStatus.HYPOTHESIS,
                    "evidence_digest": None,
                    "verified": False,
                    "payload": {
                        "verification_state": "started",
                        "verified": False,
                        "ledger_seq": seq,
                        "ledger_record_hash": record.record_hash,
                    },
                    "seq": seq,
                }
            if kind == EventKind.JOB_COMPLETED.value and payload.get("result_digest"):
                digest = str(payload["result_digest"])
                a_key = canonical_graph_json({"job_id": job_id, "result_digest": digest})
                content_digest = digest if _SHA256_RE.match(digest) else None
                passport_ok = self._is_passport_authoritative(facts)
                if ver_passed and facts is not None and facts.verification is not None:
                    ver_digest = canonical_graph_digest(facts.verification)
                    verifications[exe_key] = {
                        "identity": {"job_id": job_id, "attempt": attempt},
                        "logical_entity_id": f"verification:{job_id}",
                        "revision_id": f"verified:seq:{seq}",
                        "content_digest": ver_digest,
                        "epistemic_status": EpistemicStatus.MEASURED_EVIDENCE,
                        "evidence_digest": ver_digest,
                        "verified": True,
                        "payload": {
                            "verification_state": "verified",
                            "verified": True,
                            "verification": facts.verification,
                            "ledger_seq": seq,
                            "ledger_record_hash": record.record_hash,
                        },
                        "seq": seq,
                    }
                art_epistemic = (
                    EpistemicStatus.MEASURED_EVIDENCE
                    if (ver_passed or passport_ok)
                    else EpistemicStatus.HYPOTHESIS
                )
                art_ver_state = (
                    "verified"
                    if ver_passed
                    else ("passport_attested" if passport_ok else "unverified")
                )
                artifacts[a_key] = {
                    "identity": {"job_id": job_id, "result_digest": digest},
                    "logical_entity_id": f"artifact:{job_id}",
                    "revision_id": f"seq:{seq}",
                    "content_digest": content_digest,
                    "epistemic_status": art_epistemic,
                    "evidence_digest": evidence_hash if (ver_passed or passport_ok) else None,
                    "payload": {
                        "result_digest": digest,
                        "job_type": payload.get("job_type"),
                        "ledger_seq": seq,
                        "verification_state": art_ver_state,
                        "passport_present": passport_ok,
                    },
                    "seq": seq,
                    "execution": exe_key,
                    "verification": exe_key if ver_passed else None,
                    "edge_evidence_digest": evidence_hash,
                }
                if passport_ok and facts is not None and facts.artifact_passport is not None:
                    passport_digest = canonical_graph_digest(facts.artifact_passport)
                    passports[a_key] = {
                        "identity": {"job_id": job_id, "result_digest": digest},
                        "logical_entity_id": f"passport:{job_id}",
                        "revision_id": f"seq:{seq}",
                        "content_digest": passport_digest,
                        "epistemic_status": EpistemicStatus.MEASURED_EVIDENCE,
                        "evidence_digest": passport_digest,
                        "payload": {
                            "artifact_passport": facts.artifact_passport,
                            "ledger_seq": seq,
                            "ledger_record_hash": record.record_hash,
                        },
                        "seq": seq,
                    }

        projected = {"EXECUTION": 0, "ARTIFACT": 0, "VERIFICATION": 0, "PASSPORT": 0}
        edges = 0
        skipped_terminal: list[dict[str, str]] = []

        with self._lock, self._write_connection() as connection:

            def terminal(node_id: str) -> str | None:
                row = connection.execute(
                    "SELECT status FROM nexus_graph_node WHERE node_id = ?", (node_id,)
                ).fetchone()
                status = str(row[0]) if row is not None else ""
                return status if status in TERMINAL_STATUSES else None

            def project(kind: str, spec: dict[str, Any]) -> WriteResult | None:
                node_id = node_identity(kind, spec["identity"])
                status = terminal(node_id)
                if status is not None:
                    skipped_terminal.append({"kind": kind, "node_id": node_id, "status": status})
                    return None
                return self._put_node_tx(
                    connection,
                    kind,
                    spec["identity"],
                    spec["payload"],
                    logical_entity_id=str(spec["logical_entity_id"]),
                    revision_id=str(spec["revision_id"]),
                    content_digest=spec.get("content_digest"),
                    epistemic_status=spec["epistemic_status"],
                    evidence_digest=spec.get("evidence_digest"),
                    source="causal_journal",
                    source_seq=int(spec["seq"]),
                )

            if replace:
                connection.execute(
                    "DELETE FROM nexus_graph_node_history WHERE node_id IN"
                    " (SELECT node_id FROM nexus_graph_node WHERE source = 'causal_journal'"
                    " AND status NOT IN ('invalidated', 'revoked', 'superseded'))"
                )
                connection.execute("DELETE FROM nexus_graph_edge WHERE source = 'causal_journal'")
                connection.execute(
                    "DELETE FROM nexus_graph_node WHERE source = 'causal_journal'"
                    " AND status NOT IN ('invalidated', 'revoked', 'superseded')"
                )

            for key in sorted(set(order), key=lambda k: int(executions[k]["seq"])):
                result = project("EXECUTION", executions[key])
                if result is not None and result.created:
                    projected["EXECUTION"] += 1
            for key in sorted(verifications, key=lambda k: int(verifications[k]["seq"])):
                result = project("VERIFICATION", verifications[key])
                if result is not None and result.created:
                    projected["VERIFICATION"] += 1
            for key in sorted(artifacts, key=lambda k: int(artifacts[k]["seq"])):
                artifact = artifacts[key]
                node = project("ARTIFACT", artifact)
                if node is not None and node.created:
                    projected["ARTIFACT"] += 1
                artifact_id = node_identity("ARTIFACT", artifact["identity"])
                execution_id = node_identity(
                    "EXECUTION", executions[artifact["execution"]]["identity"]
                )
                if (
                    terminal(execution_id) is None
                    and self._put_edge_tx(
                        connection,
                        "EXECUTION_PRODUCED_ARTIFACT",
                        execution_id,
                        artifact_id,
                        {"ledger_seq": artifact["seq"]},
                        epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
                        evidence_digest=str(artifact["edge_evidence_digest"]),
                        source="causal_journal",
                        source_seq=int(artifact["seq"]),
                    ).created
                ):
                    edges += 1
                ver_key = artifact["verification"]
                if (
                    ver_key is not None
                    and ver_key in verifications
                    and verifications[ver_key]["verified"]
                    and terminal(artifact_id) is None
                ):
                    verification_id = node_identity(
                        "VERIFICATION", verifications[ver_key]["identity"]
                    )
                    if self._put_edge_tx(
                        connection,
                        "ARTIFACT_VERIFIED_BY",
                        artifact_id,
                        verification_id,
                        {"ledger_seq": artifact["seq"]},
                        epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
                        evidence_digest=str(verifications[ver_key]["evidence_digest"]),
                        source="causal_journal",
                        source_seq=int(artifact["seq"]),
                    ).created:
                        edges += 1
                if key in passports:
                    passport = passports[key]
                    passport_result = project("PASSPORT", passport)
                    if passport_result is not None and passport_result.created:
                        projected["PASSPORT"] += 1
                    passport_id = node_identity("PASSPORT", passport["identity"])
                    if (
                        terminal(artifact_id) is None
                        and self._put_edge_tx(
                            connection,
                            "ARTIFACT_HAS_PASSPORT",
                            artifact_id,
                            passport_id,
                            {"ledger_seq": passport["seq"]},
                            epistemic_status=EpistemicStatus.MEASURED_EVIDENCE,
                            evidence_digest=str(passport["evidence_digest"]),
                            source="causal_journal",
                            source_seq=int(passport["seq"]),
                        ).created
                    ):
                        edges += 1

        return {
            "skipped_terminal": skipped_terminal,
            "records_read": len(records),
            "projected_nodes": projected,
            "projected_edges": edges,
            "unmigrated_levels": {
                "PROJECT": "UNMIGRATED — the ledger never observes projects",
                "INTENT": "UNMIGRATED — the ledger never observes intent",
                "REVISION": "UNMIGRATED — spine state, not a ledger fact",
                "PLAN": "UNMIGRATED — spine state, not a ledger fact",
                "COMMAND": "UNMIGRATED — CommandBus state, not a ledger fact",
                "DECISION": "UNMIGRATED — not recorded by any current source",
                "ACTOR": "UNMIGRATED — not recorded by any current source",
            },
            "canonical_source": "causal_journal (execution facts) + job_queue (authority)",
        }

    def counts(self) -> dict[str, int]:
        with self._lock, self._connection() as connection:
            nodes = connection.execute("SELECT COUNT(*) FROM nexus_graph_node").fetchone()
            edges = connection.execute("SELECT COUNT(*) FROM nexus_graph_edge").fetchone()
            history = connection.execute("SELECT COUNT(*) FROM nexus_graph_node_history").fetchone()
            chain = connection.execute("SELECT COUNT(*) FROM nexus_graph_chain").fetchone()
        return {
            "nodes": int(nodes[0]) if nodes else 0,
            "edges": int(edges[0]) if edges else 0,
            "history": int(history[0]) if history else 0,
            "chain": int(chain[0]) if chain else 0,
        }

    # ------------------------------------------------------------------ #
    # Connection plumbing
    # ------------------------------------------------------------------ #
    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._memory_connection is not None:
            try:
                yield self._memory_connection
                self._memory_connection.commit()
            except BaseException:
                self._memory_connection.rollback()
                raise
            return
        connection = sqlite3.connect(self._sqlite_path, timeout=self._connect_timeout)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextlib.contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            if self._memory_connection is None:
                connection.execute("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> GraphNode:
        raw_proof = row[10]
        proof = None if raw_proof is None else EpistemicProof.from_dict(json.loads(str(raw_proof)))
        return GraphNode(
            node_id=str(row[0]),
            kind=str(row[1]),
            identity=json.loads(str(row[2])),
            logical_entity_id=str(row[3]),
            revision_id=str(row[4]),
            content_digest=str(row[5]),
            payload=json.loads(str(row[6])),
            payload_digest=str(row[7]),
            epistemic_status=EpistemicStatus(str(row[8])),
            evidence_digest=None if row[9] is None else str(row[9]),
            proof=proof,
            status=str(row[11]),
            superseded_by=None if row[12] is None else str(row[12]),
            source=str(row[13]),
            source_seq=None if row[14] is None else int(row[14]),
            revision_count=int(row[15]),
            created_at=str(row[16]),
            updated_at=str(row[17]),
        )

    @staticmethod
    def _edge_from_row(row: sqlite3.Row) -> GraphEdge:
        raw_proof = row[8]
        proof = None if raw_proof is None else EpistemicProof.from_dict(json.loads(str(raw_proof)))
        return GraphEdge(
            edge_id=str(row[0]),
            relation=str(row[1]),
            src=str(row[2]),
            dst=str(row[3]),
            payload=json.loads(str(row[4])),
            payload_digest=str(row[5]),
            epistemic_status=EpistemicStatus(str(row[6])),
            evidence_digest=None if row[7] is None else str(row[7]),
            proof=proof,
            status=str(row[9]),
            source=str(row[10]),
            source_seq=None if row[11] is None else int(row[11]),
            created_at=str(row[12]),
        )

    def _node(self, connection: sqlite3.Connection, node_id: str) -> GraphNode | None:
        row = connection.execute(
            "SELECT node_id, kind, identity_json, logical_entity_id, revision_id,"
            " content_digest, payload_json, payload_digest, epistemic_status,"
            " evidence_digest, proof_json, status, superseded_by, source,"
            " source_seq, revision_count, created_at, updated_at"
            " FROM nexus_graph_node WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        return None if row is None else self._node_from_row(row)

    def _edge(self, connection: sqlite3.Connection, edge_id: str) -> GraphEdge | None:
        row = connection.execute(
            "SELECT edge_id, relation, src, dst, payload_json, payload_digest,"
            " epistemic_status, evidence_digest, proof_json, status, source,"
            " source_seq, created_at FROM nexus_graph_edge WHERE edge_id = ?",
            (edge_id,),
        ).fetchone()
        return None if row is None else self._edge_from_row(row)


__all__ = [
    "ALLOWED_NODE_STATUS_TRANSITIONS",
    "CANONICAL_ENCODING_VERSION",
    "GRAPH_CHAIN_GENESIS",
    "GRAPH_TABLES",
    "LINEAGE_ATTACHMENTS",
    "LINEAGE_CHAIN",
    "NODE_KINDS",
    "RELATIONS",
    "REVISION_CHAIN_OPS",
    "STATUSES",
    "TERMINAL_STATUSES",
    "CanonicalValueError",
    "CausalImpactResult",
    "ChainEntry",
    "CreativeGraph",
    "EntityIdentityTriple",
    "EpistemicProof",
    "EpistemicProofError",
    "EpistemicStatus",
    "GraphCycleError",
    "GraphEdge",
    "GraphIntegrityError",
    "GraphNode",
    "GraphStateError",
    "GraphStateTransitionError",
    "IntegrityFinding",
    "IntegrityReport",
    "WriteResult",
    "canonical_graph_digest",
    "canonical_graph_json",
    "compute_proof_signature",
    "create_epistemic_proof",
    "edge_identity",
    "node_identity",
    "normalize_canonical_value",
    "validate_hmac_sha256_signature",
    "validate_sha256_digest",
    "verify_epistemic_proof",
]
