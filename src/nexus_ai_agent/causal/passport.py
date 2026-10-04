"""The Artifact Passport: a projection of canonical evidence, never its source.

A passport answers one question deterministically:

    "Exactly why and how did this artifact come into existence?"

and it answers it the only honest way available: by projecting facts that are
already durable somewhere authoritative, and by *re-measuring* the artifact
itself.  Three sources of truth, each for its own facts:

===========================  ==============================================
fact                          source of truth
===========================  ==============================================
what happened, in what order  the causal journal (append-only, chained)
what the job's durable state  the queue row (``JobQueuePort``-owned facts)
is (status, attempt, digests)
what the artifact's bytes are the artifact file on disk, re-measured here
===========================  ==============================================

Consequences, by construction:

* **No evidence, no passport.**  If nothing in the journal mentions the
  requested subject the builder raises :class:`PassportRefused` — it does not
  emit an empty certificate.
* **Disagreement is reported, never smoothed.**  When the journal and the
  queue row disagree (status, attempt, digest), or the bytes on disk no
  longer hash to the recorded identity, the passport says so in
  ``divergences`` and its ``completeness`` is downgraded.  ``COMPLETE`` means
  *the three sources agree* — it never means "the creative output is good".
* **Stages that were never recorded stay empty.**  ``intent``, ``strategy``,
  ``work``, ``plan`` and ``transaction`` have no producer on this tree; the
  passport reports them as ``not_recorded`` rather than filling them with a
  plausible story.
* **The passport is not a new authority.**  It is a derived, read-only
  projection with its own content digest (``passport_id``); it can be
  rebuilt at any time from the sources above, and deleting it loses nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai_agent.causal.journal import CausalJournal
from nexus_ai_agent.causal.models import (
    CausalRecord,
    PassportRefused,
    Stage,
    StageStatus,
    canonical_json,
    digest_of,
)

#: Verified job statuses that mean "no lawful artifact exists for this job".
_FAILURE_STATUSES: Final[frozenset[str]] = frozenset({"failed_retryable", "failed_terminal"})
_TERMINAL_SUCCESS: Final[str] = "completed"


class JobFacts(BaseModel):
    """The authoritative job facts a passport is reconciled against.

    Built from the queue's durable row only (``get_result_chain``), never from
    handler return values.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    execution_status: str
    attempt: int = Field(ge=0)
    verification_status: str = "not_applicable"
    project_id: str | None = None
    operation_id: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    artifact_path: str | None = None
    failure_reason: str | None = None


class JobFactsReader(Protocol):
    """Read-only access to the queue-owned facts for one job."""

    def __call__(self, job_id: str) -> JobFacts | None: ...


class MeasuredArtifact(BaseModel):
    """The passport builder's own re-measurement of the artifact's bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str
    size_bytes: int = Field(ge=0)


class StageEvidence(BaseModel):
    """Whether a canonical stage is present in the reconstructed history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: Stage
    status: StageStatus
    record_seqs: tuple[int, ...] = ()
    node_ids: tuple[str, ...] = ()


DivergenceSeverity = Literal["info", "warning", "critical"]
Completeness = Literal["COMPLETE", "PARTIAL", "IN_FLIGHT", "DIVERGED", "UNPROVEN"]


class Divergence(BaseModel):
    """One disagreement or gap between the sources of truth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=64)
    severity: DivergenceSeverity
    detail: str = Field(default="", max_length=1000)


class ArtifactPassport(BaseModel):
    """The evidence-backed projection (rebuildable, never authoritative)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passport_id: str
    artifact_id: str
    completeness: Completeness
    job_id: str | None = None
    attempt: int | None = None
    project_id: str | None = None
    operation: str | None = None
    stages: tuple[StageEvidence, ...]
    chain: tuple[CausalRecord, ...]
    divergences: tuple[Divergence, ...]
    measured_artifact: MeasuredArtifact | None = None
    journal_head_hash: str
    journal_record_count: int = Field(ge=0)

    def canonical_body(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        payload.pop("passport_id", None)
        return payload

    def compute_passport_id(self) -> str:
        """Content identity of this projection (detects post-hoc edits to it)."""
        return digest_of(self.canonical_body())


def default_measure(path: Path) -> tuple[str, int]:
    """Independent measurement of one file: ``(sha256:<hex>, size)``.

    Deliberately implemented here with the standard library instead of
    reusing the render lane's helper: the verifier must not share the
    producer's measuring code (``jobs.verification`` makes the same choice in
    the other direction).  Same algorithm, different implementation chain.
    """
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def job_facts_from_chain_row(chain: Mapping[str, object]) -> JobFacts:
    """Adapt a durable ``get_result_chain`` mapping onto :class:`JobFacts`.

    Only durable fields are read; the caller passes whatever the queue's
    reader returned for a real row.  Missing optional fields stay ``None`` —
    the passport treats absence as absence, not as agreement.
    """
    physical = chain.get("physical_identity")
    path = physical.get("path") if isinstance(physical, Mapping) else None
    projected = {
        "job_id": str(chain.get("job_id") or ""),
        "execution_status": str(chain.get("execution_status") or ""),
        "attempt": chain.get("attempt") if isinstance(chain.get("attempt"), int) else 0,
        "verification_status": str(chain.get("verification_status") or "not_applicable"),
        "project_id": _optional_str(chain.get("project_id")),
        "operation_id": _optional_str(chain.get("operation_id")),
        "sha256": _optional_str(chain.get("sha256")),
        "size_bytes": chain.get("size_bytes") if isinstance(chain.get("size_bytes"), int) else None,
        "artifact_path": str(path) if isinstance(path, str) and path else None,
        "failure_reason": _optional_str(chain.get("failure_reason")),
    }
    return JobFacts.model_validate(projected)


def _optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


class PassportBuilder:
    """Build passports from the journal, the queue row and the artifact bytes."""

    def __init__(
        self,
        journal: CausalJournal,
        job_reader: JobFactsReader,
        *,
        allowed_roots: Sequence[Path],
        measure: Callable[[Path], tuple[str, int]] = default_measure,
    ) -> None:
        if not allowed_roots:
            raise PassportRefused(
                "no_allowed_roots",
                "artifact measurement requires at least one allowed root (path safety)",
            )
        self._journal = journal
        self._job_reader = job_reader
        self._roots = tuple(Path(root).resolve() for root in allowed_roots)
        self._measure = measure

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def for_artifact(self, artifact_id: str) -> ArtifactPassport:
        """Reconstruct the history of one content-addressed artifact."""
        return self._build(artifact_id=artifact_id, subject_node_id=artifact_id)

    def for_artifact_path(self, path: Path | str) -> ArtifactPassport:
        """Reconstruct the history of the artifact most recently recorded at ``path``."""
        target = str(path)
        candidates = [
            record
            for record in self._journal.records()
            if record.stage is Stage.ARTIFACT and record.facts.get("artifact_path") == target
        ]
        if not candidates:
            raise PassportRefused(
                "evidence_missing", f"no artifact record references path {target!r}"
            )
        latest = candidates[-1]
        return self._build(
            artifact_id=latest.subject.node_id, subject_node_id=latest.subject.node_id
        )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build(self, *, artifact_id: str, subject_node_id: str) -> ArtifactPassport:
        verification = self._journal.verify_chain()
        if not verification.ok:
            raise PassportRefused(
                "journal_tampered",
                f"the causal journal does not verify (seq={verification.first_bad_seq}): "
                f"{verification.reason}",
            )
        artifact_records = [
            record
            for record in self._journal.records_for(subject_node_id)
            if record.stage is Stage.ARTIFACT
        ]
        if not artifact_records:
            raise PassportRefused(
                "evidence_missing",
                f"the journal holds no artifact record for {subject_node_id!r}",
            )
        artifact_record = artifact_records[-1]
        job_id, attempt = _job_and_attempt(artifact_record)
        job_facts = self._job_reader(job_id) if job_id else None
        divergences: list[Divergence] = []
        chain = self._reconstruct_chain(artifact_record, job_id, divergences)
        measured, measure_divergences = self._measure_artifact(artifact_record, job_facts)
        divergences.extend(measure_divergences)
        divergences.extend(self._reconcile(artifact_record, job_facts, attempt, chain))
        stages = _stage_evidence(chain)
        completeness = _completeness(
            divergences=divergences,
            measured=measured,
            artifact_id=artifact_id,
            job_facts=job_facts,
        )
        passport = ArtifactPassport(
            passport_id="pending",
            artifact_id=artifact_id,
            completeness=completeness,
            job_id=job_id,
            attempt=attempt,
            project_id=(
                _fact_str(artifact_record, "project_id")
                or (job_facts.project_id if job_facts else None)
            ),
            operation=(
                _fact_str(artifact_record, "operation")
                or (job_facts.operation_id if job_facts else None)
            ),
            stages=stages,
            chain=tuple(chain),
            divergences=tuple(divergences),
            measured_artifact=measured,
            journal_head_hash=verification.head_hash,
            journal_record_count=verification.record_count,
        )
        return _with_passport_id(passport)

    def _reconstruct_chain(
        self,
        artifact_record: CausalRecord,
        job_id: str | None,
        divergences: list[Divergence],
    ) -> list[CausalRecord]:
        """Walk the graph both ways from the artifact, staying inside this job.

        Ancestors come from the records' own parent links (a *dependency*
        walk), descendants from the reverse links (what was derived from a
        node) — but a descendant whose subject belongs to a *different* job is
        excluded, so two jobs that happen to produce identical bytes never
        merge into one history.  Connectivity is checked, not assumed: a
        record naming a parent no record provides is a broken causal link and
        is reported.
        """
        every = self._journal.records()
        by_subject: dict[str, list[CausalRecord]] = {}
        children: dict[str, list[CausalRecord]] = {}
        for record in every:
            by_subject.setdefault(record.subject.node_id, []).append(record)
            for parent in record.parents:
                children.setdefault(parent.node_id, []).append(record)

        included: dict[int, CausalRecord] = {}
        frontier = [artifact_record.subject.node_id]
        while frontier:
            node_id = frontier.pop()
            candidates = [*by_subject.get(node_id, ()), *children.get(node_id, ())]
            for record in candidates:
                if record.seq in included:
                    continue
                owner = _job_of(record.subject.node_id)
                if job_id is not None and owner is not None and owner != job_id:
                    continue  # another job's history: never merged in
                included[record.seq] = record
                frontier.append(record.subject.node_id)
                frontier.extend(parent.node_id for parent in record.parents)

        history = [included[seq] for seq in sorted(included)]
        nodes = {record.subject.node_id for record in history}
        for record in history:
            for parent in record.parents:
                if parent.node_id not in nodes and parent.stage is not Stage.REQUEST:
                    divergences.append(
                        Divergence(
                            code="broken_chain_link",
                            severity="critical",
                            detail=(
                                f"record seq={record.seq} ({record.stage.value}) names parent "
                                f"{parent.node_id!r} that no record in the history provides"
                            ),
                        )
                    )
        return history

    def _measure_artifact(
        self,
        artifact_record: CausalRecord,
        job_facts: JobFacts | None,
    ) -> tuple[MeasuredArtifact | None, list[Divergence]]:
        """Re-measure the bytes at the recorded path, path-safely."""
        divergences: list[Divergence] = []
        raw_path = _fact_str(artifact_record, "artifact_path") or (
            job_facts.artifact_path if job_facts else None
        )
        if not raw_path:
            divergences.append(
                Divergence(
                    code="artifact_path_unknown",
                    severity="warning",
                    detail="no path is recorded for this artifact; bytes cannot be re-measured",
                )
            )
            return None, divergences
        path = Path(raw_path).resolve()
        if not any(_is_within(path, root) for root in self._roots):
            raise PassportRefused(
                "path_outside_allowed_roots",
                f"{path} is outside every allowed root; refusing to measure it",
            )
        if not path.is_file():
            divergences.append(
                Divergence(
                    code="artifact_unreadable",
                    severity="critical",
                    detail=f"recorded artifact path does not exist: {path}",
                )
            )
            return None, divergences
        digest, size = self._measure(path)
        measured = MeasuredArtifact(path=str(path), sha256=digest, size_bytes=size)
        if digest != artifact_record.subject.node_id:
            divergences.append(
                Divergence(
                    code="artifact_bytes_diverged",
                    severity="critical",
                    detail=(
                        f"bytes at {path} hash to {digest}, but the recorded artifact identity "
                        f"is {artifact_record.subject.node_id}"
                    ),
                )
            )
        return measured, divergences

    def _reconcile(
        self,
        artifact_record: CausalRecord,
        job_facts: JobFacts | None,
        attempt: int | None,
        chain: list[CausalRecord],
    ) -> list[Divergence]:
        """Cross-check journal ↔ queue row ↔ bytes (three-authority agreement)."""
        divergences: list[Divergence] = []
        if job_facts is None:
            divergences.append(
                Divergence(
                    code="job_row_missing",
                    severity="critical",
                    detail="the queue row that should own this artifact could not be read",
                )
            )
            return divergences
        if any(
            record.stage is Stage.JOB and record.facts.get("payload_conflict") is True
            for record in chain
        ):
            divergences.append(
                Divergence(
                    code="duplicate_request_with_different_payload",
                    severity="info",
                    detail=(
                        "a later request reused this logical idempotency key with a different "
                        "payload; the first payload kept the key (no second effect)"
                    ),
                )
            )
        required = {Stage.REQUEST, Stage.JOB, Stage.ATTEMPT, Stage.EXECUTION, Stage.ARTIFACT}
        present = {record.stage for record in chain}
        missing = sorted(stage.value for stage in required - present)
        if missing:
            divergences.append(
                Divergence(
                    code="journal_incomplete",
                    severity="warning",
                    detail=f"the causal history is missing stages: {', '.join(missing)}",
                )
            )
        if attempt is not None and job_facts.attempt != attempt:
            divergences.append(
                Divergence(
                    code="attempt_disagreement",
                    severity="critical",
                    detail=(
                        f"the row's fencing token is {job_facts.attempt} but the artifact was "
                        f"produced by attempt {attempt}"
                    ),
                )
            )
        status = job_facts.execution_status
        if status == _TERMINAL_SUCCESS:
            if Stage.RECEIPT not in present:
                divergences.append(
                    Divergence(
                        code="journal_incomplete",
                        severity="warning",
                        detail="the row is completed but no terminal receipt is recorded",
                    )
                )
            if Stage.VERIFICATION not in present:
                divergences.append(
                    Divergence(
                        code="verification_not_recorded",
                        severity="critical",
                        detail="the row is completed but no independent verification is recorded",
                    )
                )
            if job_facts.sha256 and job_facts.sha256 != artifact_record.subject.node_id:
                divergences.append(
                    Divergence(
                        code="authority_disagreement",
                        severity="critical",
                        detail=(
                            f"the queue row records artifact {job_facts.sha256} but the causal "
                            f"journal records {artifact_record.subject.node_id}"
                        ),
                    )
                )
        elif status in _FAILURE_STATUSES:
            divergences.append(
                Divergence(
                    code="artifact_from_failed_job",
                    severity="critical",
                    detail=(
                        f"the queue row is {status}: no lawful artifact exists for this job "
                        f"(failure_reason={job_facts.failure_reason!r})"
                    ),
                )
            )
        else:
            divergences.append(
                Divergence(
                    code="attempt_unsettled",
                    severity="warning",
                    detail=(
                        f"the queue row is still {status!r}: execution is not settled, so this "
                        f"artifact is not final"
                    ),
                )
            )
        # Superseded attempts: a later attempt's artifact is the lawful output.
        if attempt is not None and job_facts.attempt > attempt:
            later = [
                record
                for record in chain
                if record.stage is Stage.ARTIFACT
                and record.subject.node_id != artifact_record.subject.node_id
                and _attempt_of(record) == job_facts.attempt
            ]
            divergences.append(
                Divergence(
                    code="superseded_attempt",
                    severity="warning" if later else "critical",
                    detail=(
                        f"attempt {attempt} was superseded by attempt {job_facts.attempt}; "
                        + (
                            "the later attempt recorded its own artifact"
                            if later
                            else "but no artifact is recorded for the later attempt"
                        )
                    ),
                )
            )
        return divergences


def _with_passport_id(passport: ArtifactPassport) -> ArtifactPassport:
    return passport.model_copy(update={"passport_id": passport.compute_passport_id()})


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _fact_str(record: CausalRecord, key: str) -> str | None:
    value = record.facts.get(key)
    return value if isinstance(value, str) and value else None


#: Node-id prefixes that carry a job identity (content-addressed artifact
#: nodes deliberately do not: identical bytes are the same artifact identity).
_JOB_SCOPED_PREFIXES = ("job:", "attempt:", "execution:", "verification:", "receipt:")


def _job_of(node_id: str) -> str | None:
    """Extract the owning job id from a job-scoped node identity, if any."""
    for prefix in _JOB_SCOPED_PREFIXES:
        if node_id.startswith(prefix):
            tail = node_id[len(prefix) :]
            return tail.partition("#")[0] or None
    return None


def _job_and_attempt(artifact_record: CausalRecord) -> tuple[str | None, int | None]:
    for parent in artifact_record.parents:
        parsed = parse_execution_node_id(parent.node_id)
        if parsed is not None:
            return parsed
    return None, None


def parse_execution_node_id(node_id: str) -> tuple[str, int] | None:
    """Parse ``execution:<job_id>#<attempt>`` (the deterministic identity form)."""
    prefix = "execution:"
    if not node_id.startswith(prefix) or "#" not in node_id:
        return None
    _, _, tail = node_id.partition(prefix)
    job_id, _, raw_attempt = tail.partition("#")
    if not job_id or not raw_attempt.isdigit():
        return None
    attempt = int(raw_attempt)
    return (job_id, attempt) if attempt >= 1 else None


def _attempt_of(record: CausalRecord) -> int | None:
    parsed = _job_and_attempt(record)
    return parsed[1] if parsed else None


def _stage_evidence(chain: list[CausalRecord]) -> tuple[StageEvidence, ...]:
    evidence: list[StageEvidence] = []
    for stage in Stage:
        records = [record for record in chain if record.stage is stage]
        evidence.append(
            StageEvidence(
                stage=stage,
                status=StageStatus.RECORDED if records else StageStatus.NOT_RECORDED,
                record_seqs=tuple(record.seq for record in records),
                node_ids=tuple(sorted({record.subject.node_id for record in records})),
            )
        )
    return tuple(evidence)


def _completeness(
    *,
    divergences: list[Divergence],
    measured: MeasuredArtifact | None,
    artifact_id: str,
    job_facts: JobFacts | None,
) -> Completeness:
    if any(divergence.severity == "critical" for divergence in divergences):
        return "DIVERGED"
    if measured is None or measured.sha256 != artifact_id:
        return "UNPROVEN"
    if job_facts is not None and job_facts.execution_status not in (
        _TERMINAL_SUCCESS,
        *_FAILURE_STATUSES,
    ):
        return "IN_FLIGHT"
    if any(divergence.severity == "warning" for divergence in divergences):
        return "PARTIAL"
    return "COMPLETE"


def canonical_passport_json(passport: ArtifactPassport) -> str:
    """Deterministic rendering of a passport (byte-identical for equal facts)."""
    return canonical_json(passport.model_dump(mode="json"))


__all__ = [
    "ArtifactPassport",
    "Completeness",
    "Divergence",
    "JobFacts",
    "JobFactsReader",
    "MeasuredArtifact",
    "PassportBuilder",
    "StageEvidence",
    "canonical_passport_json",
    "default_measure",
    "job_facts_from_chain_row",
    "parse_execution_node_id",
]
