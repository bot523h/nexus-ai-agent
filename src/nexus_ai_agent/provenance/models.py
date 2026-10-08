"""Typed model of the durable causal ledger (the Project Graph journal).

The ledger answers exactly one question, deterministically and from durable
facts only: **"why and how did this job/artifact come into existence?"**

It is NOT a second source of truth for execution facts. Every execution fact
(status, attempt, payload, result, verification block) stays owned by the
durable job row (``nexus_job_queue``). The ledger records **observations of
durable transitions** at the moment they commit, each bound to the digests of
the authoritative content it observed. The passport (``provenance.passport``)
is the read-only projection that reconciles ledger against live authority and
re-measures artifacts — it never invents proof.

Record identity model (mirrors the chain the repository already defines in
``jobs.lifecycle.JobResult``): the ledger's node granularity is the durable
job/attempt, not a new invented identity — ``job_id`` is the queue's id,
``idempotency_key`` is the logical request identity, ``attempt`` is the
fencing token. No alias, no duplicate identity space.

Cryptography: a flat, single-writer hash chain (Crosby & Wallach's
tamper-evident log; RFC 6962-style leaf hashing) — not a Merkle tree, because
there is exactly one ordered writer and verification always walks the small
per-job sub-chain. Every hash is domain-separated (``RECORD_DOMAIN``) so a
record hash can never be confused with a content digest or a foreign chain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Domain separation for record hashes (RFC 6962 §5.4 rationale): mixing this
#: constant into every hash input structurally prevents type-confusion between
#: a ledger record hash and any other sha256 in the system.
RECORD_DOMAIN: str = "nexus.causal-ledger.record.v1"

#: The predecessor hash of the first record (spelled like the repo's other
#: digest values, ``sha256:<hex>``).
GENESIS_HASH: str = "sha256:" + ("0" * 64)


def canonical_json(obj: Any) -> str:
    """The one canonical JSON spelling used by every digest in this package.

    ``sort_keys`` + compact separators + ``ensure_ascii=False``: two runs over
    the same logical content always produce the same bytes, independently of
    how the content is stored. Digests therefore bind to *semantic content*,
    not to storage whitespace (a deliberate, documented choice — the queue's
    storage spelling uses default separators, and that must not matter).
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: bytes) -> str:
    """``sha256:<hex>`` of raw bytes (the repo's digest spelling)."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_of(obj: Any) -> str:
    """Content digest of a JSON-able value under the canonical spelling."""
    return sha256_hex(canonical_json(obj).encode("utf-8"))


class EventKind(str, Enum):
    """One durable transition of the job lifecycle, observed by the ledger.

    Every kind names a transition that the queue adapter has *already
    committed* to ``nexus_job_queue`` when the event is recorded — the ledger
    never observes intent, only durable fact (source: the call sites in
    ``adapters.in_process_job_queue``, each immediately after a confirmed CAS).
    """

    #: The durable insert committed: a logical job exists (first dispatch won).
    JOB_ENQUEUED = "job_enqueued"
    #: An enqueue reusing an existing idempotency key — observed, never deduped
    #: away (the duplicate/ACK-loss story lives in these records).
    JOB_ENQUEUE_DUPLICATE = "job_enqueue_duplicate"
    #: Reservation CAS committed; a fencing token (attempt) was minted.
    JOB_RESERVED = "job_reserved"
    #: PROCESSING → VERIFYING committed: execution finished, independent
    #: artifact verification started.
    JOB_VERIFICATION_STARTED = "job_verification_started"
    #: The durable success commit (fenced CAS on the winning attempt).
    JOB_COMPLETED = "job_completed"
    #: A durable failure commit (classified, typed error persisted).
    JOB_FAILED = "job_failed"
    #: PROCESSING/VERIFYING → PENDING for the running attempt (cancellation).
    JOB_REOPENED = "job_reopened"
    #: Explicit takeover: an orphaned IN-FLIGHT row (attempt >= 1, i.e. a
    #: real previous owner) was reset to PENDING by recovery. Never recorded
    #: for a row that merely sat in ``pending`` — that is re-scheduling, not
    #: an ownership transfer.
    JOB_TAKEOVER = "job_takeover"
    #: A redelivered transition claimed DIFFERENT evidence for an
    #: already-recorded (kind, job, attempt). The kept record stands; the
    #: rejected claim is durably quarantined here (an observation — appended
    #: every time, never deduped). The passport reads this as COMPROMISED.
    EVENT_CONFLICT = "event_conflict"

    @property
    def is_transition(self) -> bool:
        """Transition events dedupe on (kind, job, attempt); observations don't.

        Fencing guarantees a given (kind, job_id, attempt) transition commits
        at most once, so the key is a sound exactly-once marker. Observations
        (``JOB_ENQUEUE_DUPLICATE``, ``EVENT_CONFLICT``) may legitimately
        repeat and are appended every time.
        """
        return self not in (EventKind.JOB_ENQUEUE_DUPLICATE, EventKind.EVENT_CONFLICT)


def dedupe_key(kind: EventKind, job_id: str, attempt: int | None) -> str | None:
    """The exactly-once key of a transition event (``None`` = never dedupe)."""
    if not kind.is_transition:
        return None
    return f"{kind.value}:{job_id}:{attempt if attempt is not None else '-'}"


@dataclass(frozen=True)
class CausalEvent:
    """A durable-transition observation, en route to the journal.

    ``payload_digest`` / ``result_digest`` bind the event to the exact
    canonical content the authoritative row held at transition time. The
    ledger stores digests, never payloads — full content is retrievable from
    the authoritative row (digests on the ledger, payloads at the authority:
    the transparency-log hybrid).
    """

    kind: EventKind
    job_id: str
    job_type: str
    idempotency_key: str = ""
    attempt: int | None = None
    status: str | None = None
    error: str | None = None
    payload_digest: str | None = None
    result_digest: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    occurred_at: str = ""

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("CausalEvent.job_id must not be empty")
        if not isinstance(self.kind, EventKind):
            raise ValueError("CausalEvent.kind must be an EventKind")

    def payload(self, *, seq: int, prev_hash: str) -> dict[str, Any]:
        """The hash-covered content of the ledger record for this event."""
        return {
            "seq": seq,
            "prev_hash": prev_hash,
            "kind": self.kind.value,
            "job_id": self.job_id,
            "job_type": self.job_type,
            "idempotency_key": self.idempotency_key,
            "attempt": self.attempt,
            "status": self.status,
            "error": self.error,
            "payload_digest": self.payload_digest,
            "result_digest": self.result_digest,
            "detail": self.detail,
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True)
class LedgerRecord:
    """One immutable, hash-chained journal entry (exactly-once per key).

    ``record_json`` is the exact canonical bytes that were hashed — a verifier
    re-hashes the stored bytes instead of re-serializing (binds verification to
    what is actually on disk, not to a re-serialization of it).
    """

    seq: int
    record_hash: str
    prev_hash: str
    record_json: str
    dedupe_key: str | None
    created_at: str

    @property
    def payload(self) -> dict[str, Any]:
        parsed = json.loads(self.record_json)
        if not isinstance(parsed, dict):
            raise ValueError(f"record {self.seq}: record_json is not a JSON object")
        return parsed

    @property
    def kind(self) -> EventKind:
        return EventKind(str(self.payload["kind"]))

    @property
    def job_id(self) -> str:
        return str(self.payload["job_id"])


def compute_record_hash(record_json: str) -> str:
    """Domain-separated hash over the exact canonical record bytes."""
    return sha256_hex(RECORD_DOMAIN.encode("utf-8") + b"\n" + record_json.encode("utf-8"))


@dataclass(frozen=True)
class ChainVerdict:
    """Result of walking a chain from genesis (tamper-evidence verdict)."""

    ok: bool
    record_count: int
    head_hash: str | None
    first_broken_seq: int | None = None
    reason: str | None = None


def verify_chain(records: list[LedgerRecord]) -> ChainVerdict:
    """Recompute the whole chain from genesis; any tampering is pinpointed.

    Checks, in order, per record: the stored bytes still hash to
    ``record_hash`` (content tampering), the linkage to the predecessor holds
    (deletion/splicing), and the sequence is gapless from 1 (truncation or
    reordering). Verification reads only the records — it trusts no index and
    no counter.
    """
    if not records:
        return ChainVerdict(ok=True, record_count=0, head_hash=None)
    ordered = sorted(records, key=lambda r: r.seq)
    previous = GENESIS_HASH
    for index, record in enumerate(ordered, start=1):
        if record.seq != index:
            return ChainVerdict(
                ok=False,
                record_count=len(ordered),
                head_hash=None,
                first_broken_seq=record.seq,
                reason=f"sequence gap/reorder: expected seq {index}, found {record.seq}",
            )
        if record.prev_hash != previous:
            return ChainVerdict(
                ok=False,
                record_count=len(ordered),
                head_hash=None,
                first_broken_seq=record.seq,
                reason="prev_hash does not link to the predecessor (deleted/spliced record)",
            )
        if compute_record_hash(record.record_json) != record.record_hash:
            return ChainVerdict(
                ok=False,
                record_count=len(ordered),
                head_hash=None,
                first_broken_seq=record.seq,
                reason="record_hash does not match the stored record bytes (content tampered)",
            )
        payload = record.payload
        if (
            str(payload.get("prev_hash", "")) != record.prev_hash
            or int(payload.get("seq", -1)) != record.seq
        ):
            return ChainVerdict(
                ok=False,
                record_count=len(ordered),
                head_hash=None,
                first_broken_seq=record.seq,
                reason="record_json disagrees with its columns (tampered record)",
            )
        previous = record.record_hash
    return ChainVerdict(ok=True, record_count=len(ordered), head_hash=ordered[-1].record_hash)


@dataclass(frozen=True)
class JobFacts:
    """An authoritative snapshot of one job row (the ledger never stores this).

    Built by the queue adapter (``get_job_facts``) — the execution authority —
    and consumed by the passport to reconcile chain against live truth.
    """

    job_id: str
    job_type: str
    idempotency_key: str
    status: str
    attempt: int
    error: str | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    payload: dict[str, Any]
    payload_digest: str | None
    result: dict[str, Any] | None
    result_digest: str | None
    verification: dict[str, Any] | None
    #: False when the row's persisted status spelling is unknown — the
    #: authority row itself is corrupt/impossible. The raw spelling stays in
    #: ``status``; consumers must treat the evidence as degraded (never
    #: parse-guess, never crash).
    status_known: bool = True
    #: Queue-owned creative request identity. These fields are projected,
    #: never used by the provenance layer to authorize or alter execution.
    request_id: str | None = None
    request_fingerprint: str | None = None
    transaction_id: str | None = None
    #: Parsed snapshots of the queue row's attempt/passport evidence columns.
    #: A malformed column yields an empty/None value with its ``*_known`` flag
    #: false, so consumers can degrade explicitly instead of inferring proof.
    attempt_history: list[dict[str, Any]] = field(default_factory=list)
    attempt_history_known: bool = True
    artifact_passport: dict[str, Any] | None = None
    artifact_passport_known: bool = True


__all__ = [
    "GENESIS_HASH",
    "RECORD_DOMAIN",
    "CausalEvent",
    "ChainVerdict",
    "EventKind",
    "JobFacts",
    "LedgerRecord",
    "canonical_json",
    "compute_record_hash",
    "dedupe_key",
    "digest_of",
    "sha256_hex",
    "verify_chain",
]
