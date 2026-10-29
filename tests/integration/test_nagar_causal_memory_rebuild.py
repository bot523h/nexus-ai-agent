from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.creative_intent import CanonicalCreativeIntent
from nexus_ai_agent.application.principal import PrincipalId
from nexus_ai_agent.creative.studio.authorization import ProjectAccess
from nexus_ai_agent.creative.studio.models import ActorIdentity
from nexus_ai_agent.nagar.causal_memory import CausalMemoryReader, MemoryScope, MemoryStatus
from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.observer import QueueLedgerObserver

PROJECT_ID = "project-rebuild"


class _PassiveQueue(InProcessJobQueue):
    """Keep the durable row pending; the projection test never dispatches work."""

    def _schedule(self, job_id: str) -> None:
        return None


class _ProjectAuthorizer:
    def __init__(self, actor: ActorIdentity) -> None:
        self._grant = ProjectAccess(
            actor=actor,
            project_id=PROJECT_ID,
            permissions=frozenset({"project:read"}),
        )

    def authorize(self, actor: ActorIdentity, project_id: str) -> ProjectAccess:
        return self._grant.authorize(actor, project_id)


def _payload() -> tuple[dict[str, object], ActorIdentity]:
    principal = str(PrincipalId.new())
    intent = CanonicalCreativeIntent.create(
        project_id=PROJECT_ID,
        proposal_id="proposal_" + ("b" * 64),
        source_text_sha256="sha256:" + ("c" * 64),
        operation="timeline.trim",
        input_data={"start_us": 0, "end_us": 1_000_000},
    )
    payload: dict[str, object] = {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": "/tmp/creative_rebuild",
        "input_path": "/tmp/creative_rebuild/input.mp4",
        "media_duration_us": 2_000_000,
        "user_id": 1,
        "chat_id": 2,
        "lang": "en",
        "idempotency_key": "rebuild-key",
        "schema_version": 2,
        "principal_id": principal,
        "project_id": PROJECT_ID,
        "channel_binding_id": "cbn_" + ("d" * 64),
        "intent": intent.model_dump(mode="json"),
    }
    return payload, ActorIdentity(kind="user", actor_id=principal)


def _queue(db_path: Path, journal: CausalJournal) -> _PassiveQueue:
    return _PassiveQueue(
        db_path,
        artifact_verifiers={},
        artifact_publications={},
        causal_observer=QueueLedgerObserver(journal),
    )


def _fingerprint(queue: InProcessJobQueue, job_id: str) -> tuple[object, ...]:
    facts = queue.get_job_facts(job_id)
    assert facts is not None
    return (
        facts.job_id,
        facts.status,
        facts.attempt,
        facts.payload_digest,
        facts.result_digest,
        facts.error,
    )


def test_rebuild_after_queue_and_journal_reopen_is_stable_and_read_only(
    tmp_path: Path,
) -> None:
    queue_path = tmp_path / "jobs.sqlite3"
    journal_path = tmp_path / "jobs.sqlite3.causal.sqlite3"
    journal = CausalJournal(journal_path)
    queue = _queue(queue_path, journal)
    payload, actor = _payload()
    job_id = asyncio.run(
        queue.enqueue(
            job_type="creative_render",
            idempotency_key="rebuild-key",
            payload=payload,
        )
    )
    scope = MemoryScope(actor=actor, project_id=PROJECT_ID)
    authorizer = _ProjectAuthorizer(actor)

    first = CausalMemoryReader(
        journal=journal,
        facts_provider=queue,
        project_authorizer=authorizer,
        sha256_fn=lambda _path: "sha256:" + ("e" * 64),
    )
    before_row = _fingerprint(queue, job_id)
    before_head = journal.head()
    before_count = journal.count()
    original = first.read_job(job_id, scope)
    repeated = first.read_job(job_id, scope)
    assert original.snapshot_digest == repeated.snapshot_digest
    assert original.status is MemoryStatus.INCOMPLETE
    assert _fingerprint(queue, job_id) == before_row
    assert journal.head() == before_head
    assert journal.count() == before_count

    # A new adapter instance simulates process restart: only the canonical
    # SQLite job row and hash-chained journal are inputs to the rebuilt view.
    reopened_journal = CausalJournal(journal_path)
    reopened_queue = _queue(queue_path, reopened_journal)
    rebuilt = CausalMemoryReader(
        journal=reopened_journal,
        facts_provider=reopened_queue,
        project_authorizer=authorizer,
        sha256_fn=lambda _path: "sha256:" + ("e" * 64),
    ).read_job(job_id, scope)

    assert rebuilt.snapshot_digest == original.snapshot_digest
    assert rebuilt.nodes == original.nodes
    assert rebuilt.edges == original.edges
    assert _fingerprint(reopened_queue, job_id) == before_row
    assert reopened_journal.head() == before_head
    assert reopened_journal.count() == before_count


def test_restart_after_journal_tamper_and_missing_lineage_fails_closed(tmp_path: Path) -> None:
    journal_path = tmp_path / "causal.sqlite3"
    journal = CausalJournal(journal_path)
    observed_queue_path = tmp_path / "observed-jobs.sqlite3"
    observed_queue = _queue(observed_queue_path, journal)
    observed_payload, observed_actor = _payload()
    observed_job_id = asyncio.run(
        observed_queue.enqueue(
            job_type="creative_render",
            idempotency_key="rebuild-key",
            payload=observed_payload,
        )
    )

    unobserved_journal_path = tmp_path / "unobserved-causal.sqlite3"
    CausalJournal(unobserved_journal_path)
    unobserved_queue_path = tmp_path / "unobserved-jobs.sqlite3"
    unobserved_queue = _PassiveQueue(
        unobserved_queue_path,
        artifact_verifiers={},
        artifact_publications={},
    )
    unobserved_payload, unobserved_actor = _payload()
    unobserved_payload["idempotency_key"] = "unobserved-key"
    unobserved_job_id = asyncio.run(
        unobserved_queue.enqueue(
            job_type="creative_render",
            idempotency_key="unobserved-key",
            payload=unobserved_payload,
        )
    )

    with sqlite3.connect(journal_path) as connection:
        row = connection.execute(
            "SELECT record_json FROM nexus_causal_journal WHERE seq = 1"
        ).fetchone()
        assert row is not None
        connection.execute(
            "UPDATE nexus_causal_journal SET record_json = ? WHERE seq = 1",
            (row[0].replace("job_enqueued", "job_completed"),),
        )

    # Reopen both durable sources to model restart after the tamper.
    reopened_journal = CausalJournal(journal_path)
    reopened_observed = _queue(observed_queue_path, reopened_journal)
    reopened_unobserved = _PassiveQueue(
        unobserved_queue_path,
        artifact_verifiers={},
        artifact_publications={},
    )
    reopened_unobserved_journal = CausalJournal(unobserved_journal_path)
    tampered = CausalMemoryReader(
        journal=reopened_journal,
        facts_provider=reopened_observed,
        project_authorizer=_ProjectAuthorizer(observed_actor),
        sha256_fn=lambda _path: "sha256:" + ("e" * 64),
    ).read_job(
        observed_job_id,
        MemoryScope(actor=observed_actor, project_id=PROJECT_ID),
    )
    missing_lineage = CausalMemoryReader(
        journal=reopened_unobserved_journal,
        facts_provider=reopened_unobserved,
        project_authorizer=_ProjectAuthorizer(unobserved_actor),
        sha256_fn=lambda _path: "sha256:" + ("e" * 64),
    ).read_job(
        unobserved_job_id,
        MemoryScope(actor=unobserved_actor, project_id=PROJECT_ID),
    )

    assert tampered.status is MemoryStatus.COMPROMISED
    assert tampered.passport_status == "COMPROMISED"
    assert "chain_broken" in tampered.finding_codes
    assert missing_lineage.status is MemoryStatus.INCOMPLETE
    assert missing_lineage.passport_status != "VERIFIED"
    assert missing_lineage.finding_codes
