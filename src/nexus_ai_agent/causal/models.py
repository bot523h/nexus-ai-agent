"""Typed causal facts: identities, stages, and the record that binds them.

Design laws this module enforces (they are the reason it exists):

1. **Identity is deterministic where the domain requires it.**  Every node
   identity a record can name is derived from durable facts by a documented
   formula (:func:`request_node_id`, :func:`job_node_id`, …), never randomly.
   A content-addressed artifact identity IS the measured ``sha256:`` of its
   bytes; two observations of the same bytes are the same node.
2. **A record states facts, it does not create authority.**  A record carries
   who acted and under which authority *as observed*; nothing here can grant,
   widen or infer a permission.
3. **Replay is idempotent.**  The identity of a *logical* observation
   (:attr:`CausalRecord.record_key`) excludes volatile fields (timestamps,
   durations), so re-observing the same committed fact after a lost
   acknowledgement collapses onto the existing record instead of inventing a
   second one.
4. **Evidence is tamper-evident.**  Each record is bound to the previous one
   by :attr:`CausalRecord.prev_hash` and carries its own
   :attr:`CausalRecord.record_hash`; any in-place edit, interior deletion or
   reordering breaks verification (:meth:`CausalRecord.digest_matches`).
5. **Facts are whitelisted, bounded and JSON-exact.**  ``NaN``/``Infinity``,
   unknown keys, non-scalar nesting deeper than one level and oversized
   payloads are refused — a ledger row is evidence, so it must be
   re-readable by every consumer forever.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Genesis link of every chain: the hash a first record points back to.
GENESIS_HASH: Final[str] = "0" * 64

#: Maximum accepted serialized size of one record's facts (bytes, UTF-8).
MAX_FACTS_BYTES: Final[int] = 64 * 1024


class CausalError(Exception):
    """Base class for every refusal/defect raised by the causal substrate."""


class CausalRefused(CausalError, ValueError):
    """A fact set was refused *before* it could be written (fail-closed)."""


class JournalCorrupted(CausalError, RuntimeError):
    """The durable journal cannot be trusted — no append, no read-through."""


class JournalUnavailable(CausalError, RuntimeError):
    """The journal storage could not be opened or written."""


class PassportRefused(CausalError, RuntimeError):
    """No passport may be issued for this subject.

    Raised when canonical evidence is absent, unreadable or tampered — the
    refusal is the honest answer: a passport must never be produced by
    inventing the proof it is supposed to project.
    """

    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(f"[{reason_code}] {detail}".strip())
        self.reason_code = reason_code
        self.detail = detail


class Stage(str, Enum):
    """The canonical stages of the intent → artifact chain.

    The stages exist as a closed vocabulary *now* so that whichever typed
    proposal/compiler contract lands (the open spine work) can record its
    stages without a schema change.  This module does not implement them,
    and a passport never claims a stage that has no record: it reports the
    stage as ``not_recorded``.
    """

    REQUEST = "request"
    INTENT = "intent"
    STRATEGY = "strategy"
    WORK = "work"
    PLAN = "plan"
    TRANSACTION = "transaction"
    JOB = "job"
    ATTEMPT = "attempt"
    EXECUTION = "execution"
    ARTIFACT = "artifact"
    VERIFICATION = "verification"
    RECEIPT = "receipt"


#: The stages whose causal order this module can assert today, in order.
#: ``intent``/``strategy``/``work``/``plan``/``transaction`` are deliberately
#: absent from the *ordering* table: no producer exists on this tree yet, and
#: claiming an order for unproduced facts would be fabrication.
RECORDED_STAGE_ORDER: Final[tuple[Stage, ...]] = (
    Stage.REQUEST,
    Stage.JOB,
    Stage.ATTEMPT,
    Stage.EXECUTION,
    Stage.ARTIFACT,
    Stage.VERIFICATION,
    Stage.RECEIPT,
)

#: Keys a record may carry, per stage.  Anything else is refused: the ledger
#: is evidence, so an unplanned key (a user id, a token, a blob) must fail
#: loudly at the boundary instead of silently entering durable history.
FACT_KEYS: Final[Mapping[Stage, frozenset[str]]] = {
    Stage.REQUEST: frozenset({"job_type", "idempotency_digest", "payload_digest"}),
    Stage.JOB: frozenset(
        {"job_type", "idempotency_digest", "payload_digest", "created", "payload_conflict"}
    ),
    Stage.ATTEMPT: frozenset({"job_type", "attempt", "reservation"}),
    Stage.EXECUTION: frozenset(
        {
            "job_type",
            "attempt",
            "result_digest",
            "typed_failure_code",
            "error_present",
            "artifact_digest",
            "artifact_size_bytes",
            "artifact_path",
            "success",
        }
    ),
    Stage.ARTIFACT: frozenset(
        {"artifact_digest", "artifact_size_bytes", "artifact_path", "operation", "project_id"}
    ),
    Stage.VERIFICATION: frozenset(
        {
            "attempt",
            "verdict",
            "reason_code",
            "evidence_digest",
            "measured_digest",
            "measured_size_bytes",
            "probe",
            "published",
        }
    ),
    Stage.RECEIPT: frozenset(
        {
            "attempt",
            "status",
            "result_digest",
            "verification_status",
            "error_present",
        }
    ),
}

#: Fields excluded from a record's logical identity: they describe *when* an
#: observation happened, not *what* was observed.  Replaying a lost
#: acknowledgement must collapse onto the existing record, so they must not
#: participate in :func:`record_key`.
VOLATILE_FIELDS: Final[frozenset[str]] = frozenset({"recorded_at", "prev_hash", "seq"})

Scalar = str | int | float | bool | None


def _reject_float(value: float, where: str) -> float:
    if math.isnan(value) or math.isinf(value):
        raise CausalRefused(f"{where} is not a finite number: {value!r}")
    return value


def canonical_json(value: object) -> str:
    """Serialize ``value`` deterministically (sorted keys, no NaN/Infinity).

    Determinism is a correctness boundary here: every digest in this package
    is taken over this encoding, so two processes must produce byte-identical
    JSON for equal facts or the chain would not verify.
    """
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # non-JSON or NaN/Infinity payload
        raise CausalRefused(f"value is not canonically serializable: {exc}") from exc


def digest_of(value: object) -> str:
    """Content-addressed identity: ``sha256:<hex>`` over canonical JSON."""
    encoded = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def digest_of_text(text: str) -> str:
    """Content-addressed identity of raw bytes-as-text (no JSON layer)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_node_id(job_type: str, idempotency_key: str) -> str:
    """The logical request identity: the queue's own dedupe key, hashed.

    The queue collapses duplicate requests on ``(job_type, idempotency_key)``
    (first payload wins); this mirrors that rule so the causal graph has
    exactly one request node per logical request, whatever the transport did.
    """
    if not job_type or not idempotency_key:
        raise CausalRefused("request identity needs a job type and an idempotency key")
    return "request:" + digest_of({"job_type": job_type, "idempotency_key": idempotency_key})[7:39]


def job_node_id(job_id: str) -> str:
    return f"job:{job_id}"


def attempt_node_id(job_id: str, attempt: int) -> str:
    if attempt < 1:
        raise CausalRefused("attempt identity requires a minted fencing token (attempt >= 1)")
    return f"attempt:{job_id}#{attempt}"


def execution_node_id(job_id: str, attempt: int) -> str:
    if attempt < 1:
        raise CausalRefused("execution identity requires a minted fencing token (attempt >= 1)")
    return f"execution:{job_id}#{attempt}"


def verification_node_id(job_id: str, attempt: int) -> str:
    if attempt < 1:
        raise CausalRefused("verification identity requires a minted fencing token (attempt >= 1)")
    return f"verification:{job_id}#{attempt}"


def receipt_node_id(job_id: str, attempt: int) -> str:
    if attempt < 1:
        raise CausalRefused("receipt identity requires a minted fencing token (attempt >= 1)")
    return f"receipt:{job_id}#{attempt}"


def artifact_node_id(measured_digest: str) -> str:
    """Artifact identity IS the measured content digest (content-addressed)."""
    if not measured_digest.startswith("sha256:") or len(measured_digest) != len("sha256:") + 64:
        raise CausalRefused(f"artifact identity must be a sha256 digest: {measured_digest!r}")
    return measured_digest


def record_key_for(
    *,
    stage: Stage,
    subject: NodeRef,
    parents: Sequence[NodeRef],
    actor: ActorRef | None,
    authority: AuthorityRef | None,
    facts: Mapping[str, object],
) -> str:
    """The logical identity of one observation (timestamps excluded).

    Two observations of the same committed fact — the original and a replay
    after a lost acknowledgement, for example — share this key, which is what
    makes the append idempotent (law 3 in the module docstring).  Volatile
    fields are excluded by construction: nothing time-dependent is hashed
    here, and only whitelisted facts participate.
    """
    return digest_of(
        {
            "stage": stage.value,
            "subject": subject.model_dump(mode="json"),
            "parents": [parent.model_dump(mode="json") for parent in parents],
            "actor": actor.model_dump(mode="json") if actor else None,
            "authority": authority.model_dump(mode="json") if authority else None,
            "facts": dict(facts),
        }
    )


def sanitize_facts(stage: Stage, facts: Mapping[str, object]) -> dict[str, object]:
    """Whitelist, bound and JSON-exactify one record's facts (fail-closed).

    Refusals are deliberate: an unknown key means a producer is trying to
    record something the evidence contract has not defined (possibly PII, a
    secret, or a blob), and silence there would poison durable history.
    """
    allowed = FACT_KEYS.get(stage)
    if allowed is None:
        raise CausalRefused(f"stage {stage.value!r} has no fact contract; it cannot be recorded")
    unknown = set(facts) - allowed
    if unknown:
        raise CausalRefused(
            f"facts for stage {stage.value!r} carry unplanned keys: {sorted(unknown)}"
        )
    sanitized: dict[str, object] = {}
    for key, raw in facts.items():
        sanitized[key] = _sanitize_value(raw, where=f"{stage.value}.{key}", depth=0)
    encoded = canonical_json(sanitized)
    if len(encoded.encode("utf-8")) > MAX_FACTS_BYTES:
        raise CausalRefused(f"facts for stage {stage.value!r} exceed {MAX_FACTS_BYTES} bytes")
    return sanitized


def _sanitize_value(value: object, *, where: str, depth: int) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return _reject_float(value, where)
    if isinstance(value, Mapping):
        if depth >= 1:
            raise CausalRefused(f"{where}: facts nest at most one level")
        nested: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CausalRefused(f"{where}: nested keys must be strings")
            nested[key] = _sanitize_value(item, where=f"{where}.{key}", depth=depth + 1)
        return nested
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if depth >= 1:
            raise CausalRefused(f"{where}: facts nest at most one level")
        return [
            _sanitize_value(item, where=f"{where}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
    raise CausalRefused(f"{where}: unsupported fact type {type(value).__name__}")


class NodeRef(BaseModel):
    """A reference to one node of the causal graph (never a bare foreign key)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: Stage
    node_id: str = Field(min_length=1, max_length=200)
    role: str = Field(default="causal", min_length=1, max_length=64)


class ActorRef(BaseModel):
    """Who acted, as observed — an identity, never a proof of authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=32)
    actor_id: str = Field(min_length=1, max_length=200)


class AuthorityRef(BaseModel):
    """The decision that made an execution lawful, as observed.

    ``decision`` is recorded, not evaluated: this module never grants
    authority, and a record carrying ``granted`` is evidence about the
    authorizer, not a capability to act.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    authorizer: str = Field(min_length=1, max_length=128)
    decision: str = Field(pattern=r"^(granted|denied|not_required)$")
    capability: str | None = Field(default=None, max_length=128)
    capability_version: str | None = Field(default=None, max_length=64)


class CausalRecord(BaseModel):
    """One immutable statement of observed fact, chained to its predecessor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(ge=1)
    stage: Stage
    subject: NodeRef
    parents: tuple[NodeRef, ...] = ()
    actor: ActorRef | None = None
    authority: AuthorityRef | None = None
    facts: dict[str, object]
    facts_digest: str = Field(min_length=len("sha256:") + 64, max_length=len("sha256:") + 64)
    record_key: str = Field(min_length=len("sha256:") + 64, max_length=len("sha256:") + 64)
    recorded_at: str = Field(min_length=1, max_length=64)
    prev_hash: str = Field(min_length=64, max_length=64)
    record_hash: str = Field(min_length=64, max_length=64)

    @field_validator("facts")
    @classmethod
    def _facts_are_bounded(cls, value: dict[str, object]) -> dict[str, object]:
        if len(canonical_json(value).encode("utf-8")) > MAX_FACTS_BYTES:
            raise ValueError(f"facts exceed {MAX_FACTS_BYTES} bytes")
        return value

    def hashable_payload(self) -> dict[str, object]:
        """The exact projection the record hash is taken over."""
        return {
            "seq": self.seq,
            "stage": self.stage.value,
            "subject": self.subject.model_dump(mode="json"),
            "parents": [parent.model_dump(mode="json") for parent in self.parents],
            "actor": self.actor.model_dump(mode="json") if self.actor else None,
            "authority": self.authority.model_dump(mode="json") if self.authority else None,
            "facts": self.facts,
            "facts_digest": self.facts_digest,
            "record_key": self.record_key,
            "recorded_at": self.recorded_at,
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.hashable_payload()).encode("utf-8")).hexdigest()

    def digest_matches(self) -> bool:
        return self.compute_hash() == self.record_hash


class AppendOutcome(BaseModel):
    """Result of an append attempt: created, or collapsed onto an existing record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: CausalRecord
    created: bool
    duplicate_of_seq: int | None = None


class ChainVerification(BaseModel):
    """Verdict of an integrity walk over the durable chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    record_count: int = Field(ge=0)
    head_hash: str
    first_bad_seq: int | None = None
    reason: str | None = None


class StageStatus(str, Enum):
    """Per-stage presence in a reconstructed chain — never an assumption."""

    RECORDED = "recorded"
    NOT_RECORDED = "not_recorded"
    UNPROVEN = "unproven"


__all__ = [
    "AppendOutcome",
    "ActorRef",
    "AuthorityRef",
    "CausalError",
    "CausalRecord",
    "CausalRefused",
    "ChainVerification",
    "FACT_KEYS",
    "GENESIS_HASH",
    "JournalCorrupted",
    "JournalUnavailable",
    "MAX_FACTS_BYTES",
    "NodeRef",
    "PassportRefused",
    "RECORDED_STAGE_ORDER",
    "Stage",
    "StageStatus",
    "VOLATILE_FIELDS",
    "artifact_node_id",
    "attempt_node_id",
    "canonical_json",
    "digest_of",
    "digest_of_text",
    "execution_node_id",
    "job_node_id",
    "receipt_node_id",
    "record_key_for",
    "request_node_id",
    "sanitize_facts",
    "verification_node_id",
]
