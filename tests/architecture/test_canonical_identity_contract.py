"""ADR-0008 confirmation: one authority per identity, one durable lineage.

The executable half of
``docs/architecture/adr/0008-canonical-identity-arbitration.md``. Each test maps to
one row or rule of that contract, and each is green on merged main today:

* the queue request identity is deterministic, domain-separated, and nullable for
  legacy rows — and a queue id can never enter a CommandBus envelope;
* the CommandBus mints one ``tx_`` identity per applied commit, keeps the parent
  revision chain, and separates content identity (``state_hash``) from sequence
  identity (``state_revision``);
* recorded history stays immutable while new commits append;
* exactly one module writes the durable causal ledger, and building the
  passport projection appends nothing to it;
* a contradictory reassignment fails closed, is quarantined, and reads back the
  same account from a fresh instance on the same file.

A failure here means an implementation is redefining an identity authority rather
than extending the canonical one: read the ADR before relaxing the assertion.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from nexus_ai_agent.creative.studio import (
    CommandBus,
    CommandValidationError,
    Timeline,
    build_wave1_registry,
    new_project,
)
from nexus_ai_agent.creative.studio.models import compute_state_hash
from nexus_ai_agent.jobs.creative_passport import attempt_id, request_identity
from nexus_ai_agent.provenance.journal import CausalConflictError, CausalJournal
from nexus_ai_agent.provenance.models import (
    CausalEvent,
    EventKind,
    JobFacts,
    verify_chain,
)
from nexus_ai_agent.provenance.passport import PassportBuilder

SRC = Path(__file__).parents[2] / "src" / "nexus_ai_agent"
BUS_TRANSACTION_ID = re.compile(r"^tx_[0-9a-f]{32}$")
QUEUE_TRANSACTION_ID = re.compile(r"^transaction_[0-9a-f]{64}$")


def _empty_project_bus() -> CommandBus:
    """A fresh one-project bus with the shipped Wave 1 registry (no authorizer)."""
    project = new_project(
        "proj_identity", "identity", Timeline(timeline_id="tl_identity", duration_us=1_000_000)
    )
    return CommandBus(project, registry=build_wave1_registry())


# --------------------------------------------------------------------------- #
# 1. queue identity: deterministic, namespaced, nullable, never a bus identity
# --------------------------------------------------------------------------- #
def test_queue_identity_is_deterministic_namespaced_and_legacy_stays_null() -> None:
    first = request_identity("creative_render", "key-1", {"args": ["0", "1"]})
    again = request_identity("creative_render", "key-1", {"args": ["0", "1"]})
    changed = request_identity("creative_render", "key-1", {"args": ["0", "2"]})

    assert first == again, "one request plus one payload must resolve to one durable identity"
    assert first.request_id.startswith("request_")
    assert QUEUE_TRANSACTION_ID.match(first.transaction_id)
    # A changed payload keeps the request identity but must fail closed through the
    # fingerprint instead of silently reusing the recorded transaction identity.
    assert changed.request_id == first.request_id
    assert changed.request_fingerprint != first.request_fingerprint
    assert changed.transaction_id != first.transaction_id

    assert attempt_id("job_1", 1) == attempt_id("job_1", 1)
    assert attempt_id("job_1", 1) != attempt_id("job_1", 2)
    with pytest.raises(ValueError):
        attempt_id("job_1", 0)

    # Legacy rows carry no queue identity; unknown stays nullable, never invented.
    facts = JobFacts(
        job_id="legacy",
        job_type="creative_render",
        idempotency_key="legacy-key",
        status="completed",
        attempt=0,
        error=None,
        created_at=None,
        started_at=None,
        finished_at=None,
        payload={},
        payload_digest=None,
        result=None,
        result_digest=None,
        verification=None,
    )
    assert (facts.request_id, facts.request_fingerprint, facts.transaction_id) == (None, None, None)
    assert facts.status_known and facts.attempt_history_known and facts.artifact_passport_known


def test_a_queue_transaction_id_can_never_be_a_commandbus_transaction() -> None:
    bus = _empty_project_bus()
    queue_identity = request_identity("creative_render", "key-1", {"args": ["0", "1"]})

    # TypedCommand forbids unknown fields: a caller cannot smuggle a queue
    # transaction id into a bus transaction.
    with pytest.raises(CommandValidationError):
        bus.dispatch(
            {
                "command_id": "cmd_smuggle",
                "operation": "media.pause",
                "transaction_id": queue_identity.transaction_id,
            }
        )

    result = bus.dispatch({"command_id": "cmd_1", "operation": "media.pause"})
    assert result.transaction_id != queue_identity.transaction_id
    assert not result.transaction_id.startswith("transaction_")


# --------------------------------------------------------------------------- #
# 2. CommandBus identity: unique per commit, revision chain, content vs sequence
# --------------------------------------------------------------------------- #
def test_commandbus_transaction_identity_is_unique_and_the_revision_chain_holds() -> None:
    bus = _empty_project_bus()
    first = bus.dispatch({"command_id": "cmd_1", "operation": "media.pause"})
    second = bus.dispatch({"command_id": "cmd_2", "operation": "media.pause"})

    assert BUS_TRANSACTION_ID.match(first.transaction_id)
    assert BUS_TRANSACTION_ID.match(second.transaction_id)
    assert first.transaction_id != second.transaction_id
    assert [first.state_revision, second.state_revision] == [1, 2]

    history = bus.history
    assert [entry.parent_revision for entry in history] == [0, 1]
    assert history[1].previous_state_hash == history[0].new_state_hash
    assert history[1].new_state_hash == second.state_hash
    # media.pause changes no hashed content: two revisions, one content identity.
    # Sequence identity and content identity are deliberately different facts.
    assert history[0].new_state_hash == history[1].new_state_hash


def test_state_hash_excludes_revision_and_recorded_history_is_immutable() -> None:
    bus = _empty_project_bus()
    bus.dispatch({"command_id": "cmd_1", "operation": "media.pause"})

    def fingerprint() -> tuple[tuple[str, int, str, str], ...]:
        return tuple(
            (
                entry.transaction_id,
                entry.parent_revision,
                entry.previous_state_hash,
                entry.new_state_hash,
            )
            for entry in bus.history
        )

    recorded = fingerprint()

    project = bus.project
    assert compute_state_hash(project) == project.state_hash
    project.state_revision = 999
    assert compute_state_hash(project) == project.state_hash

    bus.dispatch({"command_id": "cmd_2", "operation": "media.pause"})
    assert len(bus.history) == len(recorded) + 1, "a new commit appends, it never rewrites"
    assert fingerprint()[: len(recorded)] == recorded


# --------------------------------------------------------------------------- #
# 3. lineage authority: one ledger writer, one read-only projection
# --------------------------------------------------------------------------- #
def test_exactly_one_module_writes_the_durable_ledger() -> None:
    ledger_writers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "nexus_causal_journal" in path.read_text(encoding="utf-8")
    )
    assert ledger_writers == ["provenance/journal.py"], (
        "the durable causal ledger has exactly one writer (one authority per identity); "
        f"found: {ledger_writers}"
    )


def test_the_passport_builds_without_writing_to_the_ledger(tmp_path: Path) -> None:
    """The passport is a read-only projection: building it must append nothing.

    A spelling check for SQL keywords cannot prove this (an ``append()`` call
    needs none of them), so this test exercises the real builder against an
    isolated journal and compares the stored records byte-for-byte.
    """
    journal_path = tmp_path / "passport-read-only.sqlite3"
    journal = CausalJournal(journal_path)
    for kind, attempt, status in (
        (EventKind.JOB_ENQUEUED, None, "pending"),
        (EventKind.JOB_RESERVED, 1, "processing"),
        (EventKind.JOB_VERIFICATION_STARTED, 1, "verifying"),
        (EventKind.JOB_COMPLETED, 1, "completed"),
    ):
        journal.append(
            CausalEvent(
                kind=kind,
                job_id="job_readonly",
                job_type="creative.render",
                idempotency_key="key-readonly",
                attempt=attempt,
                status=status,
                occurred_at="2026-10-05T00:00:00Z",
            )
        )

    facts = JobFacts(
        job_id="job_readonly",
        job_type="creative.render",
        idempotency_key="key-readonly",
        status="completed",
        attempt=1,
        error=None,
        created_at=None,
        started_at=None,
        finished_at=None,
        payload={},
        payload_digest=None,
        result={},
        result_digest=None,
        verification={"status": "verified"},
    )

    class _Facts:
        def get_job_facts(self, job_id: str) -> JobFacts | None:
            return facts if job_id == "job_readonly" else None

    before = [(record.seq, record.record_hash) for record in journal.all_records()]
    PassportBuilder(journal, _Facts()).build("job_readonly")
    after = [(record.seq, record.record_hash) for record in journal.all_records()]
    assert after == before, "building a passport must not append, mutate, or repair the ledger"
    assert CausalJournal(journal_path).count() == len(before)


# --------------------------------------------------------------------------- #
# 4. fail-closed contradiction and restart readback
# --------------------------------------------------------------------------- #
def test_contradictory_lineage_fails_closed_and_reads_back_after_reopen(tmp_path: Path) -> None:
    journal_path = tmp_path / "causal.sqlite3"
    journal = CausalJournal(journal_path)
    committed = CausalEvent(
        kind=EventKind.JOB_COMPLETED,
        job_id="job_1",
        job_type="creative.render",
        idempotency_key="key-1",
        attempt=1,
        status="completed",
        result_digest="sha256:" + "aa" * 32,
        occurred_at="2026-10-05T00:00:00Z",
    )
    journal.append(committed)

    contradictory = dataclasses.replace(
        committed, status="failed", result_digest="sha256:" + "bb" * 32
    )
    with pytest.raises(CausalConflictError):
        journal.append(contradictory)

    # The kept record stands, the rejected claim is quarantined, the chain verifies.
    reopened = CausalJournal(journal_path)
    assert [record.dedupe_key for record in reopened.all_records()] == [
        "job_completed:job_1:1",
        None,
    ]
    assert [record.kind for record in reopened.all_records()] == [
        EventKind.JOB_COMPLETED,
        EventKind.EVENT_CONFLICT,
    ]
    verdict = verify_chain(reopened.all_records())
    assert verdict.ok
    assert verdict.record_count == 2
