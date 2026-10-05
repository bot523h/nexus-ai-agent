"""The Artifact Passport — a read-only, evidence-derived projection.

The passport answers the Project-Graph question for one job:

    "Exactly why and how did this artifact (or failure) come into existence?"

It is **not** a source of truth and never becomes one. Every claim it makes is
derived, at build time, from exactly two places:

1. the causal journal (``provenance.journal``) — verified hash chain;
2. the authoritative job row (``get_job_facts`` on the queue adapter) — the
   execution facts the ledger only digests.

…plus, when the artifact bytes are still reachable, an **independent
re-measurement** (the runtime's own ``sha256_file``) of those bytes.

Status contract (fail-closed — never upgrade without evidence):

=================  ==============================================================
``VERIFIED``       chain intact + completeness holds + live authority matches
                   every recorded digest + artifact bytes re-measure to the
                   recorded digest (failure passports expect no artifact).
``VERIFIED_WITH_   like VERIFIED, but the bytes are no longer reachable
LIMITATIONS``      (delivered/retention-cleaned — normal in this system)
                   and/or part of the history is a labeled backfill.
``INCOMPLETE``     the chain cannot account for the row's durable state
                   (missing transition records), no artifact identity was
                   ever recorded, or the job is not terminal yet.
``COMPROMISED``    broken chain, digest divergence between ledger and live
                   authority, or artifact bytes that no longer match the
                   recorded identity — a passport NEVER papers over
                   divergence.
=================  ==============================================================

Shape: deliberately aligned with the in-toto/SLSA statement model (``subject``
= artifact name + digest; ``predicate`` = the typed causal account) so a later
signing/anchoring layer can wrap it without redesign. It is a local,
self-contained dialect — no new dependency, no network claim.

Known honesty boundary (documented, not hidden): hash chaining detects any
*edit* of an existing record and any *middle* deletion, but a truncated tail
is detected only through the completeness check of the affected job — tail
truncation of *other* jobs' records requires an externally anchored head
(``predicate.chain.journal_head`` is published precisely so a future signed
checkpoint can close this).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import (
    EventKind,
    JobFacts,
    LedgerRecord,
    digest_of,
    verify_chain,
)

PASSPORT_TYPE = "https://nexus-ai.dev/passport/creative-execution/v1"

#: Result-dialect keys the chain already defines
#: (``jobs.verification.ArtifactClaim.from_handler_result``).
_PATH_KEYS = ("artifact_path", "output_path")
_SHA_KEYS = ("sha256", "content_sha256")

_SEVERITY_ORDER = {"note": 0, "limitation": 1, "incomplete": 2, "compromised": 3}

_TERMINAL_SUCCESS = "completed"
_IN_FLIGHT = frozenset({"pending", "processing", "verifying"})


class PassportStatus(str, Enum):
    VERIFIED = "VERIFIED"
    VERIFIED_WITH_LIMITATIONS = "VERIFIED_WITH_LIMITATIONS"
    INCOMPLETE = "INCOMPLETE"
    COMPROMISED = "COMPROMISED"


class JobFactsProvider(Protocol):
    """Structural port to the execution authority (satisfied by the queue)."""

    def get_job_facts(self, job_id: str) -> JobFacts | None: ...


@dataclass(frozen=True)
class Finding:
    """One reconciliation observation, with the severity it forces."""

    severity: str  # note | limitation | incomplete | compromised
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class ArtifactPassport:
    """The durable-evidence projection for one job (immutable once built)."""

    job_id: str
    status: PassportStatus
    content: dict[str, Any]
    passport_digest: str
    generated_at: str
    findings: tuple[Finding, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        document = dict(self.content)
        document["passport_digest"] = self.passport_digest
        document["status"] = self.status.value
        document["generated_at"] = self.generated_at
        return document


class PassportBuilder:
    """Builds passports from the journal + live authority. Reads only."""

    def __init__(
        self,
        journal: CausalJournal,
        facts_provider: JobFactsProvider,
        *,
        sha256_fn: Any = None,
    ) -> None:
        self._journal = journal
        self._facts = facts_provider
        if sha256_fn is not None:
            # Injected digest function (tests); production uses the runtime's.
            self._sha256_fn = sha256_fn
        else:
            from nexus_ai_agent.creative.slideshow.ffmpeg import sha256_file

            self._sha256_fn = sha256_file

    # -- public -----------------------------------------------------------

    def build(self, job_id: str) -> ArtifactPassport:
        """Reconstruct the causal account for ``job_id`` (sync; thread-safe).

        Raises ``KeyError`` for a job unknown to BOTH the authority and the
        journal (mirrors ``get_status``).
        """
        generated_at = datetime.now(timezone.utc).isoformat()
        findings: list[Finding] = []
        facts = self._facts.get_job_facts(job_id)
        records = self._journal.records_for_job(job_id)
        if facts is None and not records:
            raise KeyError(f"unknown job: {job_id}")

        chain = verify_chain(self._journal.all_records())
        if not chain.ok:
            findings.append(
                Finding(
                    "compromised",
                    "chain_broken",
                    f"causal journal integrity failed at seq {chain.first_broken_seq}:"
                    f" {chain.reason}",
                )
            )

        subject: dict[str, Any] | None = None
        verification: dict[str, Any] | None = None
        if facts is None:
            findings.append(
                Finding(
                    "incomplete",
                    "row_unknown",
                    "no authoritative row exists for this job; the ledger alone cannot"
                    " prove execution facts",
                )
            )
            status = PassportStatus.INCOMPLETE
            predicate_job: dict[str, Any] = {"job_id": job_id}
            payload: dict[str, Any] = {}
        else:
            verification = facts.verification
            status_known = facts.status_known
            if not status_known:
                # The AUTHORITY row carries a status spelling outside the
                # canonical state machine — an impossible persisted state
                # (the queue fail-closed-writes only known spellings).
                # Evidence is corrupt, not merely incomplete: say so and
                # stop inferring which transitions to expect.
                findings.append(
                    Finding(
                        "compromised",
                        "unparseable_row_status",
                        f"the authoritative row's status {facts.status!r} is outside the"
                        " canonical state machine; the row itself is corrupt — no"
                        " transition can be proven from it",
                    )
                )
            subject, subject_findings = self._subject(facts)
            findings.extend(subject_findings)
            findings.extend(self._reconcile(facts, records, status_known=status_known))
            predicate_job = {
                "job_id": facts.job_id,
                "job_type": facts.job_type,
                "idempotency_key": facts.idempotency_key,
                "status": facts.status,
                "attempt": facts.attempt,
                "error": facts.error,
                "created_at": facts.created_at,
                "finished_at": facts.finished_at,
            }
            payload = facts.payload
            status = self._status(facts, subject, findings)

        passport_content: dict[str, Any] = {
            "_type": PASSPORT_TYPE,
            "predicateType": PASSPORT_TYPE,
            "subject": subject,
            "predicate": {
                "job": predicate_job,
                "authority": self._authority(payload),
                "verification": verification,
                "chain": {
                    "record_count_for_job": len(records),
                    "chain_ok": chain.ok,
                    "records": [self._record_ref(record) for record in records],
                },
                "reconciliation": {"findings": [f.to_dict() for f in findings]},
            },
        }
        head = self._journal.head()
        passport_content["predicate"]["chain"]["journal_head"] = (
            {"seq": head[0], "record_hash": head[1]} if head is not None else None
        )
        limitations = [f.detail for f in findings if f.severity == "limitation"]
        if limitations:
            passport_content["predicate"]["limitations"] = limitations
        passport_digest = digest_of(passport_content)
        return ArtifactPassport(
            job_id=job_id,
            status=status,
            content=passport_content,
            passport_digest=passport_digest,
            generated_at=generated_at,
            findings=tuple(findings),
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _record_ref(record: LedgerRecord) -> dict[str, Any]:
        payload = record.payload
        return {
            "seq": record.seq,
            "kind": str(payload.get("kind")),
            "attempt": payload.get("attempt"),
            "occurred_at": payload.get("occurred_at"),
            "record_hash": record.record_hash,
            "backfilled": bool(payload.get("detail", {}).get("backfilled", False)),
        }

    @staticmethod
    def _authority(payload: dict[str, Any]) -> dict[str, Any]:
        """The durable actor reference — never an inferred or default actor."""
        user_id = payload.get("user_id")
        chat_id = payload.get("chat_id")
        actor: dict[str, Any] | None = None
        if user_id is not None or chat_id is not None:
            actor = {"user_id": user_id, "chat_id": chat_id}
        return {
            "actor": actor,
            "basis": "dispatch payload durable in the authoritative job row "
            "(surface gate + payload schema validation ran before enqueue)",
        }

    def _subject(self, facts: JobFacts) -> tuple[dict[str, Any] | None, list[Finding]]:
        """The artifact subject: recorded identity + live re-measurement."""
        if facts.status != _TERMINAL_SUCCESS:
            return None, []
        sha = self._first(facts.verification, ("physical_identity",), _SHA_KEYS)
        path_str = self._first(facts.verification, ("physical_identity",), _PATH_KEYS)
        if sha is None:
            sha = self._first(facts.result, (), _SHA_KEYS)
        if path_str is None:
            path_str = self._first(facts.result, (), _PATH_KEYS)
        if sha is None or path_str is None:
            return None, [
                Finding(
                    "incomplete",
                    "no_recorded_artifact_identity",
                    "a completed job carries no durable artifact identity; nothing can"
                    " be proven about what it produced",
                )
            ]
        subject: dict[str, Any] = {
            "name": Path(str(path_str)).name,
            "digest": {"sha256": sha},
        }
        size = self._first(facts.verification, ("physical_identity",), ("size_bytes",))
        if isinstance(size, int):
            subject["size_bytes"] = size
        subject["remeasurement"] = self._remeasure(str(path_str), str(sha))
        return subject, []

    def _remeasure(self, path_str: str, recorded_sha: str) -> dict[str, Any]:
        """Re-measure the artifact bytes when reachable (honesty either way)."""
        path = Path(path_str)
        if not path.is_file():
            return {
                "possible": False,
                "reason": "artifact bytes not present at passport time (delivered or"
                " retention-cleaned); the durable verification block remains the"
                " recorded measurement",
            }
        measured = str(self._sha256_fn(path))
        return {
            "possible": True,
            "measured_sha256": measured,
            "matches": measured == recorded_sha,
        }

    def _reconcile(
        self, facts: JobFacts, records: list[LedgerRecord], *, status_known: bool = True
    ) -> list[Finding]:
        """Cross-check the ledger against the live authoritative row."""
        findings: list[Finding] = []
        by_kind = self._by_kind(records)
        for record in records:
            if record.kind is EventKind.EVENT_CONFLICT:
                detail = record.payload.get("detail", {})
                findings.append(
                    Finding(
                        "compromised",
                        "event_conflict_recorded",
                        f"seq {record.seq}: a redelivered"
                        f" {detail.get('rejected_kind', 'transition')} claimed different"
                        f" evidence for a record already at seq {detail.get('kept_seq')}"
                        " — the conflicting claim was quarantined, the account cannot"
                        " be certified",
                    )
                )
        # With an unparseable row status, the transitions to expect cannot be
        # derived without guessing — only the digest checks below still run.
        expected = self._expected_events(facts) if status_known else []
        for kind, attempt in expected:
            present = any(
                record.kind is kind
                and (attempt is None or record.payload.get("attempt") == attempt)
                for record in records
            )
            if not present:
                findings.append(
                    Finding(
                        "incomplete",
                        "missing_transition_record",
                        f"durable row implies {kind.value} (attempt={attempt}) but the"
                        " journal has no such record; the causal account is incomplete",
                    )
                )
        enqueued = by_kind.get(EventKind.JOB_ENQUEUED)
        if enqueued is not None:
            recorded = enqueued.payload.get("payload_digest")
            if recorded is not None and recorded != facts.payload_digest:
                findings.append(
                    Finding(
                        "compromised",
                        "payload_digest_divergence",
                        "the authoritative payload no longer matches the payload the"
                        " ledger observed at enqueue time",
                    )
                )
        for kind in (EventKind.JOB_COMPLETED, EventKind.JOB_FAILED):
            terminal = by_kind.get(kind)
            if terminal is None:
                continue
            # The row's live facts describe the CURRENT attempt only. A
            # terminal record from an OLDER attempt is legitimate superseded
            # history (a retry reopened the job) — it can never "diverge"
            # from the row, or every successful retry would read as
            # COMPROMISED. Cross-check the current attempt's terminal record.
            if facts.attempt is not None:
                current_match = [
                    r
                    for r in records
                    if r.kind is kind and r.payload.get("attempt") == facts.attempt
                ]
                if not current_match:
                    continue  # only an older attempt's terminal exists
                terminal = current_match[-1]
            recorded_result = terminal.payload.get("result_digest")
            if recorded_result is not None and recorded_result != facts.result_digest:
                findings.append(
                    Finding(
                        "compromised",
                        "result_digest_divergence",
                        f"the authoritative result no longer matches the result the"
                        f" ledger observed at {kind.value}",
                    )
                )
            recorded_status = terminal.payload.get("status")
            if recorded_status is not None and recorded_status != facts.status:
                findings.append(
                    Finding(
                        "compromised",
                        "status_divergence",
                        "the ledger-recorded terminal status diverges from the row",
                    )
                )
        for record in records:
            if record.payload.get("detail", {}).get("backfilled"):
                findings.append(
                    Finding(
                        "limitation",
                        "backfilled_history",
                        f"seq {record.seq} ({record.payload.get('kind')}) is an explicit"
                        " recovery reconstruction from the authoritative row, not an"
                        " event-time record",
                    )
                )
        earlier = sorted(
            {
                int(record.payload["attempt"])
                for record in records
                if record.kind is EventKind.JOB_RESERVED
                and isinstance(record.payload.get("attempt"), int)
            }
        )
        for attempt in earlier:
            if facts.attempt > attempt:
                findings.append(
                    Finding(
                        "note",
                        "earlier_attempts_unrecorded",
                        f"the row's fencing token is {facts.attempt}; superseded attempts"
                        f" (e.g. {attempt}) produced no surviving artifact",
                    )
                )
        return findings

    def _status(
        self,
        facts: JobFacts,
        subject: dict[str, Any] | None,
        findings: list[Finding],
    ) -> PassportStatus:
        """Worst-severity wins; a terminal, fully-accounted row verifies."""
        worst = max((_SEVERITY_ORDER.get(finding.severity, 0) for finding in findings), default=0)
        remeasurement = (subject or {}).get("remeasurement", {})
        if remeasurement.get("possible") and not remeasurement.get("matches", True):
            findings.append(
                Finding(
                    "compromised",
                    "artifact_digest_mismatch",
                    "the artifact bytes on disk no longer match the recorded identity",
                )
            )
            worst = max(worst, _SEVERITY_ORDER["compromised"])
        elif facts.status == _TERMINAL_SUCCESS and not remeasurement.get("possible", False):
            findings.append(
                Finding(
                    "limitation",
                    "artifact_bytes_unavailable",
                    "verified against the durable verification block only; the artifact"
                    " bytes were not present to re-measure",
                )
            )
            worst = max(worst, _SEVERITY_ORDER["limitation"])
        if facts.status in _IN_FLIGHT:
            findings.append(
                Finding(
                    "incomplete",
                    "job_not_terminal",
                    f"the job is still {facts.status}; this passport is a snapshot of an"
                    " unfinished execution",
                )
            )
            worst = max(worst, _SEVERITY_ORDER["incomplete"])
        if worst >= _SEVERITY_ORDER["compromised"]:
            return PassportStatus.COMPROMISED
        if worst >= _SEVERITY_ORDER["incomplete"]:
            return PassportStatus.INCOMPLETE
        if worst >= _SEVERITY_ORDER["limitation"]:
            return PassportStatus.VERIFIED_WITH_LIMITATIONS
        return PassportStatus.VERIFIED

    @staticmethod
    def _expected_events(facts: JobFacts) -> list[tuple[EventKind, int | None]]:
        """The transitions the durable row state *proves* must have happened.

        A completed row carrying the queue-owned verification block proves
        the verification phase RAN — ``job_verification_started`` is part of
        the canonical successful chain and its absence is a real gap in the
        account (the row is the truth; the journal must account for it).  A
        completed row WITHOUT such a block belongs to a no-verifier lane: no
        verification phase ever existed, and expecting its record would
        invent a requirement the system never had.
        """
        expected: list[tuple[EventKind, int | None]] = [(EventKind.JOB_ENQUEUED, None)]
        if facts.status in _IN_FLIGHT:
            return expected
        if facts.status == _TERMINAL_SUCCESS:
            expected.append((EventKind.JOB_RESERVED, facts.attempt))
            if facts.verification is not None:
                expected.append((EventKind.JOB_VERIFICATION_STARTED, facts.attempt))
            expected.append((EventKind.JOB_COMPLETED, facts.attempt))
            return expected
        # Failure states: the fencing token proves whether a reservation ran.
        if facts.attempt >= 1:
            expected.append((EventKind.JOB_RESERVED, facts.attempt))
        expected.append((EventKind.JOB_FAILED, facts.attempt if facts.attempt >= 1 else None))
        return expected

    @staticmethod
    def _by_kind(records: list[LedgerRecord]) -> dict[EventKind, LedgerRecord]:
        result: dict[EventKind, LedgerRecord] = {}
        for record in records:
            result.setdefault(record.kind, record)
        return result

    @staticmethod
    def _first(
        block: dict[str, Any] | None, nest: tuple[str, ...], keys: tuple[str, ...]
    ) -> str | int | None:
        cursor: Any = block
        for name in nest:
            if not isinstance(cursor, dict):
                return None
            cursor = cursor.get(name)
        if not isinstance(cursor, dict):
            return None
        for key in keys:
            value = cursor.get(key)
            if isinstance(value, (str, int)) and value:
                return value
        return None


__all__ = [
    "PASSPORT_TYPE",
    "ArtifactPassport",
    "Finding",
    "JobFactsProvider",
    "PassportBuilder",
    "PassportStatus",
]
