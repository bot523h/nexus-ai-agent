from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from nexus_ai_agent.application.creative_intent import CanonicalCreativeIntent
from nexus_ai_agent.creative.render_jobs import CreativeRenderPayload
from nexus_ai_agent.creative.studio.authorization import ProjectAccess, ProjectAuthorizer
from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import JobFacts, LedgerRecord, digest_of
from nexus_ai_agent.provenance.passport import ArtifactPassport, PassportBuilder, PassportStatus

from .models import (
    MemoryEdge,
    MemoryEvidenceRef,
    MemoryNode,
    MemoryNodeKind,
    MemoryScope,
    MemoryScopeError,
    MemorySnapshot,
    MemoryStatus,
    TenantScopeUnavailable,
)

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _is_sha256_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _safe_record_hash(record: LedgerRecord) -> str:
    if _is_sha256_digest(record.record_hash):
        return record.record_hash
    # Do not echo malformed on-disk hash text: it may contain arbitrary secrets.
    return digest_of({"malformed_journal_record_sequence": record.seq})


class JobFactsSource(Protocol):
    """Read-only structural view of the authoritative queue adapter."""

    def get_job_facts(self, job_id: str) -> JobFacts | None: ...


class _SnapshotFactsSource:
    """Pin one authoritative row snapshot while reusing PassportBuilder."""

    def __init__(self, facts: JobFacts) -> None:
        self._facts = facts

    def get_job_facts(self, job_id: str) -> JobFacts | None:
        return self._facts if job_id == self._facts.job_id else None


class CausalMemoryReader:
    """Build a deterministic, read-only view of one authorized creative job.

    This reader adds no persistence, job enumeration, verifier, policy, or
    execution authority. Every result is tied to one explicit job ID. The
    project authorizer is checked before the queue row is read; the row's
    canonical schema-2 payload must independently name the same project.
    """

    def __init__(
        self,
        *,
        journal: CausalJournal,
        facts_provider: JobFactsSource,
        project_authorizer: ProjectAuthorizer,
        sha256_fn: Callable[[Path], str] | None = None,
    ) -> None:
        self._journal = journal
        self._facts = facts_provider
        self._authorizer = project_authorizer
        self._sha256_fn = sha256_fn

    def read_job(self, job_id: str, scope: MemoryScope) -> MemorySnapshot:
        """Project one job after fresh project-read authorization.

        ``tenant_id`` is refused because no canonical tenant source or
        project-to-tenant join exists on main. Legacy/untyped payloads and
        cross-project jobs also fail closed without returning their facts.
        """
        if not job_id.strip():
            raise ValueError("job_id must not be empty")
        if scope.tenant_id is not None:
            raise TenantScopeUnavailable(
                "tenant-scoped reads are unavailable: canonical tenant identity is not defined"
            )

        access = self._authorizer.authorize(scope.actor, scope.project_id)
        if not isinstance(access, ProjectAccess):
            raise MemoryScopeError("project authorizer returned no canonical ProjectAccess")
        access.authorize(scope.actor, scope.project_id)
        access.require_permissions(("project:read",))

        facts = self._facts.get_job_facts(job_id)
        if facts is None or facts.job_id != job_id:
            raise MemoryScopeError("authoritative job row is unavailable in the requested scope")
        intent = self._validate_project_payload(facts, scope.project_id)

        head_before = self._journal.head()
        records = self._journal.records_for_job(job_id)
        passport_builder = PassportBuilder(
            self._journal,
            _SnapshotFactsSource(facts),
            sha256_fn=self._sha256_fn,
        )
        passport = passport_builder.build(job_id)
        head_after = self._journal.head()
        source_changed = head_before != head_after

        finding_codes = {finding.code for finding in passport.findings}
        if any(not _is_sha256_digest(record.record_hash) for record in records):
            finding_codes.add("journal_record_hash_invalid")

        subject_digest, subject_digest_invalid = self._subject_digest(passport)
        if subject_digest_invalid:
            finding_codes.add("artifact_digest_invalid")

        passport_digest = passport.passport_digest
        if not _is_sha256_digest(passport_digest):
            passport_digest = digest_of({"invalid_passport_digest_for_job": job_id})
            finding_codes.add("artifact_passport_digest_invalid")

        evidence: list[MemoryEvidenceRef] = []
        if facts.payload_digest is not None:
            if _is_sha256_digest(facts.payload_digest):
                evidence.append(
                    MemoryEvidenceRef(
                        evidence_id=f"queue-payload:{job_id}:{facts.payload_digest}",
                        source="queue_payload",
                        digest=facts.payload_digest,
                    )
                )
            else:
                finding_codes.add("queue_payload_digest_invalid")
        if facts.result_digest is not None:
            if _is_sha256_digest(facts.result_digest):
                evidence.append(
                    MemoryEvidenceRef(
                        evidence_id=f"queue-result:{job_id}:{facts.result_digest}",
                        source="queue_result",
                        digest=facts.result_digest,
                    )
                )
            else:
                finding_codes.add("queue_result_digest_invalid")
        evidence.extend(self._journal_evidence(records))
        evidence.append(
            MemoryEvidenceRef(
                evidence_id=f"artifact-passport:{job_id}:{passport_digest}",
                source="artifact_passport",
                digest=passport_digest,
            )
        )
        evidence.sort(key=lambda item: item.evidence_id)

        nodes, edges = self._graph(
            facts,
            intent,
            records,
            passport,
            evidence,
            subject_digest=subject_digest,
        )
        if source_changed:
            finding_codes.add("journal_changed_during_projection")
        missing_independent_verification = (
            facts.status == "completed" and facts.verification is None
        )
        if missing_independent_verification:
            finding_codes.add("independent_verification_missing")
            effective_passport_status = (
                PassportStatus.COMPROMISED
                if passport.status is PassportStatus.COMPROMISED
                else PassportStatus.INCOMPLETE
            )
        else:
            effective_passport_status = passport.status
        # Project-only authorization is available, but the tenant mapping is
        # not. Never report a complete/verified snapshot without that proof.
        if effective_passport_status is PassportStatus.COMPROMISED:
            status = MemoryStatus.COMPROMISED
        else:
            status = MemoryStatus.INCOMPLETE

        digest_input = {
            "job_id": job_id,
            "project_id": scope.project_id,
            "status": status.value,
            "passport_status": effective_passport_status.value,
            "tenant_scope": "unavailable",
            "completeness": "single_job",
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "edges": [edge.model_dump(mode="json") for edge in edges],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "finding_codes": sorted(finding_codes),
        }
        return MemorySnapshot(
            job_id=job_id,
            project_id=scope.project_id,
            status=status,
            passport_status=effective_passport_status.value,
            tenant_scope="unavailable",
            completeness="single_job",
            nodes=tuple(nodes),
            edges=tuple(edges),
            evidence=tuple(evidence),
            finding_codes=tuple(sorted(finding_codes)),
            snapshot_digest=digest_of(digest_input),
        )

    @staticmethod
    def _validate_project_payload(facts: JobFacts, project_id: str) -> CanonicalCreativeIntent:
        if facts.job_type != "creative_render":
            raise MemoryScopeError("only canonical creative-render jobs are project-readable")
        try:
            payload = CreativeRenderPayload.model_validate(facts.payload)
        except (ValidationError, TypeError, ValueError) as exc:
            raise MemoryScopeError("job payload has no valid canonical project scope") from exc
        if (
            payload.schema_version != 2
            or payload.project_id != project_id
            or payload.intent is None
        ):
            raise MemoryScopeError("job payload does not prove the requested project scope")
        if payload.intent.project_id != project_id:
            raise MemoryScopeError("canonical intent and queue row disagree on project scope")
        return payload.intent

    @staticmethod
    def _journal_evidence(records: list[LedgerRecord]) -> list[MemoryEvidenceRef]:
        return [
            MemoryEvidenceRef(
                evidence_id=f"journal:{record.seq}:{_safe_record_hash(record)}",
                source="journal",
                digest=_safe_record_hash(record),
                sequence=record.seq if record.seq >= 1 else None,
            )
            for record in records
        ]

    @staticmethod
    def _subject_digest(passport: ArtifactPassport) -> tuple[str | None, bool]:
        subject = passport.content.get("subject")
        if not isinstance(subject, dict):
            return None, False
        digest = subject.get("digest")
        if not isinstance(digest, dict):
            return None, False
        raw_digest = digest.get("sha256")
        if raw_digest is None:
            return None, False
        if _is_sha256_digest(raw_digest):
            return raw_digest, False
        return None, True

    @staticmethod
    def _graph(
        facts: JobFacts,
        intent: CanonicalCreativeIntent,
        records: list[LedgerRecord],
        passport: ArtifactPassport,
        evidence: list[MemoryEvidenceRef],
        *,
        subject_digest: str | None,
    ) -> tuple[list[MemoryNode], list[MemoryEdge]]:
        # Do not copy the mutable passport document or raw evidence into the graph.
        passport_status = passport.status.value
        passport_content = passport.content
        nodes: list[MemoryNode] = [
            MemoryNode(
                node_id=f"job:{facts.job_id}",
                kind=MemoryNodeKind.JOB,
                attributes={
                    "job_type": facts.job_type,
                    "status": facts.status,
                    "attempt": facts.attempt,
                },
            ),
            MemoryNode(
                node_id=f"intent:{intent.intent_id}",
                kind=MemoryNodeKind.INTENT,
                attributes={
                    "intent_id": intent.intent_id,
                    "operation": intent.operation,
                    "proposal_id": intent.proposal_id,
                    "source_text_sha256": intent.source_text_sha256,
                },
            ),
            MemoryNode(
                node_id=f"operation:{intent.operation}",
                kind=MemoryNodeKind.OPERATION,
                attributes={"operation_id": intent.operation},
            ),
        ]
        edges: list[MemoryEdge] = [
            MemoryEdge(
                edge_id=f"edge:intent:{intent.intent_id}:job:{facts.job_id}",
                source_id=f"intent:{intent.intent_id}",
                target_id=f"job:{facts.job_id}",
                relation="intent_for_job",
                evidence_ids=tuple(
                    item.evidence_id for item in evidence if item.source == "queue_payload"
                ),
            ),
            MemoryEdge(
                edge_id=f"edge:job:{facts.job_id}:operation:{intent.operation}",
                source_id=f"job:{facts.job_id}",
                target_id=f"operation:{intent.operation}",
                relation="job_requests_operation",
                evidence_ids=tuple(
                    item.evidence_id for item in evidence if item.source == "queue_payload"
                ),
            ),
        ]

        attempt_numbers = {
            int(record.payload["attempt"])
            for record in records
            if isinstance(record.payload.get("attempt"), int) and record.payload["attempt"] > 0
        }
        if facts.attempt > 0:
            attempt_numbers.add(facts.attempt)
        journal_refs = {item.sequence: item.evidence_id for item in evidence if item.sequence}
        for attempt_number in sorted(attempt_numbers):
            attempt_id = f"attempt:{facts.job_id}:{attempt_number}"
            nodes.append(
                MemoryNode(
                    node_id=attempt_id,
                    kind=MemoryNodeKind.ATTEMPT,
                    attributes={"attempt_number": attempt_number},
                )
            )
            edges.append(
                MemoryEdge(
                    edge_id=f"edge:job:{facts.job_id}:attempt:{attempt_number}",
                    source_id=f"job:{facts.job_id}",
                    target_id=attempt_id,
                    relation="job_has_attempt",
                )
            )
        for record in records:
            payload = record.payload
            event_id = f"event:{record.seq}:{_safe_record_hash(record)}"
            attempt = payload.get("attempt")
            nodes.append(
                MemoryNode(
                    node_id=event_id,
                    kind=MemoryNodeKind.JOURNAL_EVENT,
                    attributes={
                        "sequence": record.seq,
                        "event_kind": record.kind.value,
                        "attempt": attempt if isinstance(attempt, int) else None,
                        "passport_status": passport_status,
                    },
                )
            )
            evidence_id = journal_refs.get(record.seq)
            evidence_ids = (evidence_id,) if evidence_id else ()
            edges.append(
                MemoryEdge(
                    edge_id=f"edge:job:{facts.job_id}:event:{record.seq}",
                    source_id=f"job:{facts.job_id}",
                    target_id=event_id,
                    relation="job_has_event",
                    evidence_ids=evidence_ids,
                )
            )
            if isinstance(attempt, int) and attempt > 0:
                attempt_id = f"attempt:{facts.job_id}:{attempt}"
                edges.append(
                    MemoryEdge(
                        edge_id=f"edge:attempt:{facts.job_id}:{attempt}:event:{record.seq}",
                        source_id=attempt_id,
                        target_id=event_id,
                        relation="attempt_has_event",
                        evidence_ids=evidence_ids,
                    )
                )

        if subject_digest is not None:
            artifact_id = f"artifact:{facts.job_id}:{subject_digest}"
            nodes.append(
                MemoryNode(
                    node_id=artifact_id,
                    kind=MemoryNodeKind.ARTIFACT,
                    attributes={"sha256": subject_digest, "claim_status": passport_status},
                )
            )
            edges.append(
                MemoryEdge(
                    edge_id=f"edge:job:{facts.job_id}:artifact:{subject_digest}",
                    source_id=f"job:{facts.job_id}",
                    target_id=artifact_id,
                    relation="job_records_subject",
                    evidence_ids=tuple(
                        item.evidence_id for item in evidence if item.source == "artifact_passport"
                    ),
                )
            )
        verification = passport_content.get("predicate", {}).get("verification")
        if isinstance(verification, dict):
            verification_digest = digest_of(verification)
            verification_id = f"verification:{facts.job_id}:{verification_digest}"
            verification_status = verification.get("status")
            safe_verification_status = (
                verification_status if isinstance(verification_status, str) else None
            )
            nodes.append(
                MemoryNode(
                    node_id=verification_id,
                    kind=MemoryNodeKind.VERIFICATION,
                    attributes={
                        "evidence_digest": verification_digest,
                        "status": safe_verification_status,
                        "claim_status": passport_status,
                    },
                )
            )
            if subject_digest is not None:
                edges.append(
                    MemoryEdge(
                        edge_id=f"edge:artifact:{facts.job_id}:{subject_digest}:verification:{verification_digest}",
                        source_id=f"artifact:{facts.job_id}:{subject_digest}",
                        target_id=verification_id,
                        relation="subject_has_verification",
                        evidence_ids=tuple(
                            item.evidence_id
                            for item in evidence
                            if item.source == "artifact_passport"
                        ),
                    )
                )

        nodes.sort(key=lambda node: (node.kind.value, node.node_id))
        edges.sort(key=lambda edge: edge.edge_id)
        return nodes, edges
