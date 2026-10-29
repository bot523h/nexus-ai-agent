from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.application.creative_intent import CanonicalCreativeIntent
from nexus_ai_agent.application.principal import PrincipalId
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity, AuthorizationError
from nexus_ai_agent.nagar.causal_memory import (
    CausalMemoryReader,
    MemoryScope,
    MemoryScopeError,
    MemoryStatus,
    TenantScopeUnavailable,
)
from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent, EventKind, JobFacts, digest_of
from nexus_ai_agent.provenance.passport import PassportBuilder, PassportStatus

PROJECT_ID = "project-a"
JOB_ID = "job-a"
ACTOR = ActorIdentity(kind="user", actor_id="usr_test")
PRINCIPAL_ID = str(PrincipalId.new())


def _intent(project_id: str = PROJECT_ID) -> CanonicalCreativeIntent:
    return CanonicalCreativeIntent.create(
        project_id=project_id,
        proposal_id="proposal_" + ("2" * 64),
        source_text_sha256="sha256:" + ("3" * 64),
        operation="timeline.trim",
        input_data={"start_us": 1_000_000, "end_us": 2_000_000},
    )


def _payload(project_id: str = PROJECT_ID) -> dict[str, Any]:
    intent = _intent(project_id)
    return {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": "/tmp/creative_job_a",
        "input_path": "/tmp/creative_job_a/input.mp4",
        "media_duration_us": 2_000_000,
        "user_id": 1,
        "chat_id": 2,
        "lang": "en",
        "idempotency_key": "request-a",
        "schema_version": 2,
        "principal_id": PRINCIPAL_ID,
        "project_id": project_id,
        "channel_binding_id": "cbn_" + ("a" * 64),
        "intent": intent.model_dump(mode="json"),
    }


def _facts(job_id: str = JOB_ID, project_id: str = PROJECT_ID) -> JobFacts:
    payload = _payload(project_id)
    return JobFacts(
        job_id=job_id,
        job_type="creative_render",
        idempotency_key="request-a",
        status="pending",
        attempt=0,
        error=None,
        created_at="2026-10-29T00:00:00+00:00",
        started_at=None,
        finished_at=None,
        payload=payload,
        payload_digest=digest_of(payload),
        result=None,
        result_digest=None,
        verification=None,
    )


class _FactsProvider:
    def __init__(self, facts: JobFacts | None) -> None:
        self.facts = facts
        self.calls: list[str] = []

    def get_job_facts(self, job_id: str) -> JobFacts | None:
        self.calls.append(job_id)
        return self.facts if self.facts is not None and self.facts.job_id == job_id else None


@dataclass
class _StaticAuthorizer:
    grant: ProjectAccess
    calls: list[tuple[ActorIdentity, str]]

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        self.calls.append((actor, project_id))
        grant = self.grant.authorize(actor, project_id)
        grant.require_permissions(("project:read",))
        return grant


def _journal(job_id: str = JOB_ID, project_id: str = PROJECT_ID) -> CausalJournal:
    journal = CausalJournal(":memory:")
    facts = _facts(job_id, project_id)
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=job_id,
            job_type="creative_render",
            idempotency_key=facts.idempotency_key,
            status="pending",
            payload_digest=facts.payload_digest,
            occurred_at="2026-10-29T00:00:01+00:00",
        )
    )
    return journal


def _reader(
    journal: CausalJournal,
    provider: _FactsProvider,
    authorizer: _StaticAuthorizer | None = None,
) -> CausalMemoryReader:
    return CausalMemoryReader(
        journal=journal,
        facts_provider=provider,
        project_authorizer=authorizer
        or _StaticAuthorizer(
            ProjectAccess(
                actor=ACTOR,
                project_id=PROJECT_ID,
                permissions=frozenset({"project:read"}),
            ),
            [],
        ),
        sha256_fn=lambda _path: "sha256:" + ("4" * 64),
    )


def test_projects_only_typed_ids_and_marks_missing_tenant_contract() -> None:
    facts = _facts()
    provider = _FactsProvider(facts)
    journal = _journal()
    reader = _reader(journal, provider)

    snapshot = reader.read_job(JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID))

    node_kinds = {node.kind.value for node in snapshot.nodes}
    assert {"job", "intent", "operation"}.issubset(node_kinds)
    assert snapshot.project_id == PROJECT_ID
    assert snapshot.job_id == JOB_ID
    assert snapshot.status is MemoryStatus.INCOMPLETE
    assert snapshot.tenant_scope == "unavailable"
    assert snapshot.completeness == "single_job"
    assert snapshot.snapshot_digest.startswith("sha256:")
    serialized = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True)
    assert facts.payload["intent"]["intent_id"] in serialized
    assert "start_us" not in serialized
    assert "input.mp4" not in serialized
    assert "user_id" not in serialized
    assert provider.calls == [JOB_ID]
    assert journal.count() == 1


def test_authorization_happens_before_job_lookup() -> None:
    provider = _FactsProvider(_facts())
    journal = _journal()
    authorizer = _StaticAuthorizer(
        ProjectAccess(
            actor=ACTOR,
            project_id="another-project",
            permissions=frozenset({"project:read"}),
        ),
        [],
    )
    reader = _reader(journal, provider, authorizer)

    with pytest.raises(AuthorizationError):
        reader.read_job(JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID))

    assert provider.calls == []
    assert authorizer.calls == [(ACTOR, PROJECT_ID)]


def test_payload_scope_mismatch_is_rejected_without_returning_facts() -> None:
    provider = _FactsProvider(_facts(project_id="another-project"))
    journal = _journal(project_id="another-project")
    reader = _reader(journal, provider)

    with pytest.raises(MemoryScopeError):
        reader.read_job(JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID))


def test_tenant_scope_request_fails_closed_before_sources_are_read() -> None:
    provider = _FactsProvider(_facts())
    journal = _journal()
    reader = _reader(journal, provider)

    with pytest.raises(TenantScopeUnavailable):
        reader.read_job(
            JOB_ID,
            MemoryScope(actor=ACTOR, project_id=PROJECT_ID, tenant_id="tenant-a"),
        )

    assert provider.calls == []
    assert journal.count() == 1


def test_legacy_or_unscoped_payload_is_not_projected() -> None:
    facts = _facts()
    legacy_payload = {
        "command": "edit",
        "operation": "trim",
        "workspace_dir": "/tmp/creative_job_a",
        "user_id": 1,
        "chat_id": 2,
        "idempotency_key": "request-a",
        "schema_version": 1,
    }
    legacy_facts = JobFacts(
        **{
            **facts.__dict__,
            "payload": legacy_payload,
            "payload_digest": digest_of(legacy_payload),
        }
    )
    provider = _FactsProvider(legacy_facts)
    journal = CausalJournal(":memory:")
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=JOB_ID,
            job_type="creative_render",
            idempotency_key="request-a",
            status="pending",
            payload_digest=legacy_facts.payload_digest,
            occurred_at="2026-10-29T00:00:01+00:00",
        )
    )
    reader = _reader(journal, provider)

    with pytest.raises(MemoryScopeError):
        reader.read_job(JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID))


def test_broken_journal_never_becomes_a_verified_memory_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "causal.sqlite3"
    journal = CausalJournal(path)
    facts = _facts()
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=JOB_ID,
            job_type="creative_render",
            idempotency_key=facts.idempotency_key,
            status="pending",
            payload_digest=facts.payload_digest,
            occurred_at="2026-10-29T00:00:01+00:00",
        )
    )
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT record_json FROM nexus_causal_journal WHERE seq = 1"
        ).fetchone()
        assert row is not None
        connection.execute(
            "UPDATE nexus_causal_journal SET record_json = ? WHERE seq = 1",
            (row[0].replace("job_enqueued", "job_completed"),),
        )
    snapshot = _reader(journal, _FactsProvider(facts)).read_job(
        JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID)
    )

    assert snapshot.status is MemoryStatus.COMPROMISED
    assert snapshot.passport_status == "COMPROMISED"
    assert "chain_broken" in snapshot.finding_codes


def test_malformed_journal_hash_is_not_projected_or_leaked(tmp_path: Path) -> None:
    path = tmp_path / "malformed-hash.sqlite3"
    journal = CausalJournal(path)
    facts = _facts()
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=JOB_ID,
            job_type="creative_render",
            idempotency_key=facts.idempotency_key,
            status="pending",
            payload_digest=facts.payload_digest,
            occurred_at="2026-10-29T00:00:01+00:00",
        )
    )
    secret_canary = "/private/customer/prompt-canary.txt"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE nexus_causal_journal SET record_hash = ? WHERE seq = 1",
            (secret_canary,),
        )

    snapshot = _reader(journal, _FactsProvider(facts)).read_job(
        JOB_ID,
        MemoryScope(actor=ACTOR, project_id=PROJECT_ID),
    )
    serialized = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True)

    assert snapshot.status is MemoryStatus.COMPROMISED
    assert "journal_record_hash_invalid" in snapshot.finding_codes
    assert secret_canary not in serialized
    assert all(
        len(item.digest) == 71 and item.digest.startswith("sha256:") for item in snapshot.evidence
    )


def _completed_facts_and_journal(
    artifact_path: Path | None = None,
    journal_path: Path | str = ":memory:",
    *,
    include_verification: bool = True,
) -> tuple[JobFacts, CausalJournal]:
    facts = _facts()
    recorded_sha = "sha256:" + ("a" * 64)
    result: dict[str, Any] = {"success": True, "operation": "timeline.trim"}
    verification: dict[str, Any] | None = None
    if artifact_path is not None:
        artifact_path.write_bytes(b"verified-looking-artifact")
        result.update(
            {
                "artifact_path": str(artifact_path),
                "sha256": recorded_sha,
                "size_bytes": artifact_path.stat().st_size,
            }
        )
        if include_verification:
            verification = {
                "status": "verified",
                "physical_identity": {
                    "path": str(artifact_path),
                    "sha256": recorded_sha,
                    "size_bytes": artifact_path.stat().st_size,
                },
            }
    terminal = JobFacts(
        **{
            **facts.__dict__,
            "status": "completed",
            "attempt": 1,
            "started_at": "2026-10-29T00:00:02+00:00",
            "finished_at": "2026-10-29T00:00:03+00:00",
            "result": result,
            "result_digest": digest_of(result),
            "verification": verification,
        }
    )
    journal = CausalJournal(journal_path)
    lifecycle = [
        (EventKind.JOB_ENQUEUED, "pending"),
        (EventKind.JOB_RESERVED, "processing"),
    ]
    if verification is not None:
        lifecycle.append((EventKind.JOB_VERIFICATION_STARTED, "verifying"))
    lifecycle.append((EventKind.JOB_COMPLETED, "completed"))
    for kind, status in lifecycle:
        journal.append(
            CausalEvent(
                kind=kind,
                job_id=terminal.job_id,
                job_type=terminal.job_type,
                idempotency_key=terminal.idempotency_key,
                attempt=1 if kind is not EventKind.JOB_ENQUEUED else None,
                status=status,
                payload_digest=terminal.payload_digest,
                result_digest=(terminal.result_digest if kind is EventKind.JOB_COMPLETED else None),
                occurred_at=f"2026-10-29T00:00:0{journal.count() + 1}+00:00",
            )
        )
    return terminal, journal


def test_artifact_digest_mismatch_cannot_become_verified_memory(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    facts, journal = _completed_facts_and_journal(artifact)
    reader = CausalMemoryReader(
        journal=journal,
        facts_provider=_FactsProvider(facts),
        project_authorizer=_StaticAuthorizer(
            ProjectAccess(
                actor=ACTOR,
                project_id=PROJECT_ID,
                permissions=frozenset({"project:read"}),
            ),
            [],
        ),
        sha256_fn=lambda _path: "sha256:" + ("b" * 64),
    )

    snapshot = reader.read_job(JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID))

    assert snapshot.status is MemoryStatus.COMPROMISED
    assert snapshot.passport_status == "COMPROMISED"
    assert "artifact_digest_mismatch" in snapshot.finding_codes
    assert str(artifact) not in json.dumps(snapshot.model_dump(mode="json"))


def test_missing_artifact_identity_stays_incomplete() -> None:
    facts, journal = _completed_facts_and_journal()
    snapshot = _reader(journal, _FactsProvider(facts)).read_job(
        JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID)
    )

    assert snapshot.status is MemoryStatus.INCOMPLETE
    assert snapshot.passport_status == "INCOMPLETE"
    assert "no_recorded_artifact_identity" in snapshot.finding_codes


def test_missing_independent_verification_cannot_leak_passport_verified_status(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.bin"
    facts, journal = _completed_facts_and_journal(artifact, include_verification=False)
    recorded_sha = "sha256:" + ("a" * 64)
    passport = PassportBuilder(
        journal,
        _FactsProvider(facts),
        sha256_fn=lambda _path: recorded_sha,
    ).build(JOB_ID)
    assert passport.status is PassportStatus.VERIFIED

    reader = CausalMemoryReader(
        journal=journal,
        facts_provider=_FactsProvider(facts),
        project_authorizer=_StaticAuthorizer(
            ProjectAccess(
                actor=ACTOR,
                project_id=PROJECT_ID,
                permissions=frozenset({"project:read"}),
            ),
            [],
        ),
        sha256_fn=lambda _path: recorded_sha,
    )
    snapshot = reader.read_job(JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID))

    assert snapshot.status is MemoryStatus.INCOMPLETE
    assert snapshot.passport_status == "INCOMPLETE"
    assert "independent_verification_missing" in snapshot.finding_codes
    assert str(artifact) not in json.dumps(snapshot.model_dump(mode="json"))


def test_truncated_lifecycle_tail_stays_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "truncated.sqlite3"
    artifact = tmp_path / "artifact.bin"
    completed, journal = _completed_facts_and_journal(artifact, path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM nexus_causal_journal WHERE seq = 4")

    reader = CausalMemoryReader(
        journal=journal,
        facts_provider=_FactsProvider(completed),
        project_authorizer=_StaticAuthorizer(
            ProjectAccess(
                actor=ACTOR,
                project_id=PROJECT_ID,
                permissions=frozenset({"project:read"}),
            ),
            [],
        ),
        sha256_fn=lambda _path: "sha256:" + ("a" * 64),
    )
    snapshot = reader.read_job(JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID))

    assert snapshot.status is MemoryStatus.INCOMPLETE
    assert snapshot.passport_status == "INCOMPLETE"
    assert "missing_transition_record" in snapshot.finding_codes


def test_reordered_journal_rows_are_compromised_not_verified(tmp_path: Path) -> None:
    facts = _facts()
    path = tmp_path / "reordered.sqlite3"
    journal = CausalJournal(path)
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=JOB_ID,
            job_type="creative_render",
            idempotency_key=facts.idempotency_key,
            status="pending",
            payload_digest=facts.payload_digest,
            occurred_at="2026-10-29T00:00:01+00:00",
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_RESERVED,
            job_id=JOB_ID,
            job_type="creative_render",
            idempotency_key=facts.idempotency_key,
            attempt=1,
            status="processing",
            payload_digest=facts.payload_digest,
            occurred_at="2026-10-29T00:00:02+00:00",
        )
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE nexus_causal_journal SET seq = -1 WHERE seq = 1")
        connection.execute("UPDATE nexus_causal_journal SET seq = 1 WHERE seq = 2")
        connection.execute("UPDATE nexus_causal_journal SET seq = 2 WHERE seq = -1")

    snapshot = _reader(journal, _FactsProvider(facts)).read_job(
        JOB_ID, MemoryScope(actor=ACTOR, project_id=PROJECT_ID)
    )

    assert snapshot.status is MemoryStatus.COMPROMISED
    assert snapshot.passport_status == "COMPROMISED"
    assert "chain_broken" in snapshot.finding_codes
