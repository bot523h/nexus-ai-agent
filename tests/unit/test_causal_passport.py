"""Passport laws: project evidence, reconcile authorities, refuse to invent.

The history used here is driven through the real observer with the same event
shapes the queue emits, and reconciled against a real SQLite row-shaped mapping
and a real file on disk — the three sources of truth the passport must agree
with before it may claim ``COMPLETE``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nexus_ai_agent.application.ports.job_lifecycle_observer import (
    ExecutionFinished,
    JobEnqueued,
    JobReserved,
    JobSettled,
    VerificationFinished,
)
from nexus_ai_agent.causal.journal import TABLE_NAME, CausalJournal
from nexus_ai_agent.causal.models import PassportRefused, Stage, StageStatus
from nexus_ai_agent.causal.observer import CausalObserver
from nexus_ai_agent.causal.passport import (
    JobFacts,
    PassportBuilder,
    canonical_passport_json,
    job_facts_from_chain_row,
)
from nexus_ai_agent.causal.verify import verify_passport

JOB_ID = "5f0a1b2c3d4e5f60718293a4b5c6d7e8"
JOB_TYPE = "creative_render"
IDEMPOTENCY_KEY = "creative:edit:trim:user-7:msg-9"
OPERATION = "timeline.trim"


def _write_artifact(path: Path, payload: bytes) -> str:
    path.write_bytes(payload)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _block(path: Path, digest: str, size: int, *, ok: bool = True) -> dict[str, object]:
    block: dict[str, object] = {
        "logical_identity": {"project_id": f"shot-{IDEMPOTENCY_KEY}", "output_asset_id": "out"},
        "spec_identity": {"operation": OPERATION},
        "physical_identity": {
            "claimed_path": str(path),
            "path": str(path),
            "workspace": str(path.parent),
            "size_bytes": size,
            "sha256": digest,
        },
        "probe": {"duration_us": 2_000_000, "width": 320, "height": 240, "has_audio": True},
        "status": "ok" if ok else "failed",
    }
    if not ok:
        block["reason_code"] = "artifact_sha256_mismatch"
    return block


class _Reader:
    """A job reader with exactly the durability of a queue row (a dict)."""

    def __init__(self, facts: JobFacts | None) -> None:
        self.facts = facts
        self.calls: list[str] = []

    def __call__(self, job_id: str) -> JobFacts | None:
        self.calls.append(job_id)
        return self.facts


def _history(
    journal: CausalJournal,
    workspace: Path,
    *,
    payload: bytes = b"trimmed-video-bytes",
    attempt: int = 1,
    verification_ok: bool = True,
    created: bool = True,
    payload_conflict: bool = False,
) -> tuple[Path, str, dict[str, object]]:
    """Record one complete creative_render history; return path, digest, block."""
    out = workspace / "output.mp4"
    digest = _write_artifact(out, payload)
    size = out.stat().st_size
    block = _block(out, digest, size, ok=verification_ok)
    observer = CausalObserver(journal)
    request_payload = {"command": "edit", "operation": "trim", "args": ["0", "1"]}
    observer.on_enqueued(
        JobEnqueued(
            job_id=JOB_ID,
            job_type=JOB_TYPE,
            idempotency_key=IDEMPOTENCY_KEY,
            payload=request_payload,
            created=created,
            payload_conflict=payload_conflict,
        )
    )
    observer.on_reserved(JobReserved(job_id=JOB_ID, job_type=JOB_TYPE, attempt=attempt))
    observer.on_execution_finished(
        ExecutionFinished(
            job_id=JOB_ID,
            job_type=JOB_TYPE,
            attempt=attempt,
            result={
                "success": True,
                "artifact_path": str(out),
                "sha256": digest,
                "size_bytes": size,
                "operation": OPERATION,
            },
            error=None,
            typed_failure_code=None,
        )
    )
    observer.on_verification_finished(
        VerificationFinished(
            job_id=JOB_ID,
            job_type=JOB_TYPE,
            attempt=attempt,
            ok=verification_ok,
            reason_code=None if verification_ok else "artifact_sha256_mismatch",
            block=block,
            published=False,
        )
    )
    observer.on_settled(
        JobSettled(
            job_id=JOB_ID,
            job_type=JOB_TYPE,
            attempt=attempt,
            status="completed" if verification_ok else "failed_terminal",
            result={"success": True, "artifact_verification": block},
            error=None if verification_ok else "verification_failed:artifact_sha256_mismatch",
        )
    )
    return out, digest, block


def _completed_row(digest: str, block: dict[str, object], *, attempt: int = 1) -> dict:
    physical = block["physical_identity"]
    return {
        "job_id": JOB_ID,
        "project_id": f"shot-{IDEMPOTENCY_KEY}",
        "operation_id": OPERATION,
        "attempt": attempt,
        "execution_status": "completed",
        "verification_status": "ok",
        "logical_identity": block["logical_identity"],
        "spec_identity": block["spec_identity"],
        "physical_identity": physical,
        "sha256": digest,
        "size_bytes": physical["size_bytes"],
        "probe": block["probe"],
        "failure_reason": None,
    }


def _builder(journal: CausalJournal, row: dict | None, workspace: Path) -> PassportBuilder:
    facts = job_facts_from_chain_row(row) if row is not None else None
    return PassportBuilder(journal, _Reader(facts), allowed_roots=[workspace])


def test_complete_passport_from_agreed_evidence(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, block = _history(journal, tmp_path)
    builder = _builder(journal, _completed_row(digest, block), tmp_path)

    passport = builder.for_artifact(digest)

    assert passport.completeness == "COMPLETE"
    assert passport.artifact_id == digest
    assert passport.job_id == JOB_ID
    assert passport.attempt == 1
    assert passport.operation == OPERATION
    assert passport.project_id == f"shot-{IDEMPOTENCY_KEY}"
    assert passport.divergences == ()
    assert passport.measured_artifact is not None
    assert passport.measured_artifact.sha256 == digest
    assert passport.passport_id == passport.compute_passport_id()
    stages = {item.stage: item.status for item in passport.stages}
    assert stages[Stage.REQUEST] is StageStatus.RECORDED
    assert stages[Stage.ARTIFACT] is StageStatus.RECORDED
    assert stages[Stage.RECEIPT] is StageStatus.RECORDED
    for unproduced in (Stage.INTENT, Stage.STRATEGY, Stage.WORK, Stage.PLAN, Stage.TRANSACTION):
        assert stages[unproduced] is StageStatus.NOT_RECORDED
    assert [record.stage for record in passport.chain] == [
        Stage.REQUEST,
        Stage.JOB,
        Stage.ATTEMPT,
        Stage.EXECUTION,
        Stage.ARTIFACT,
        Stage.VERIFICATION,
        Stage.RECEIPT,
    ]
    verified = verify_passport(passport)
    assert verified.ok and verified.reason_code == "ok"
    assert verified.measured_sha256 == digest


def test_passport_is_deterministic(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, block = _history(journal, tmp_path)
    builder = _builder(journal, _completed_row(digest, block), tmp_path)
    first = builder.for_artifact(digest)
    second = builder.for_artifact(digest)
    assert canonical_passport_json(first) == canonical_passport_json(second)
    assert first.passport_id == second.passport_id


def test_unknown_artifact_refuses_instead_of_inventing(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _history(journal, tmp_path)
    builder = _builder(
        journal, _completed_row("sha256:" + "0" * 64, _block(tmp_path, "x", 1)), tmp_path
    )
    with pytest.raises(PassportRefused, match="evidence_missing"):
        builder.for_artifact("sha256:" + "0" * 64)


def test_deleted_artifact_is_reported_not_assumed(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    out, digest, block = _history(journal, tmp_path)
    builder = _builder(journal, _completed_row(digest, block), tmp_path)
    out.unlink()

    passport = builder.for_artifact(digest)

    assert passport.completeness == "DIVERGED"
    assert passport.measured_artifact is None
    assert [item.code for item in passport.divergences] == ["artifact_unreadable"]
    assert verify_passport(passport).ok is False


def test_edited_bytes_are_detected_by_the_builder_and_the_verifier(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    out, digest, block = _history(journal, tmp_path)
    builder = _builder(journal, _completed_row(digest, block), tmp_path)
    out.write_bytes(b"tampered-with-after-the-fact")

    passport = builder.for_artifact(digest)

    assert passport.completeness == "DIVERGED"
    assert [item.code for item in passport.divergences] == ["artifact_bytes_diverged"]
    verdict = verify_passport(passport)
    assert verdict.ok is False
    assert verdict.reason_code == "passport_incomplete"


def test_row_and_journal_disagreement_is_critical(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, block = _history(journal, tmp_path)
    row = _completed_row(digest, block)
    row["sha256"] = "sha256:" + "1" * 64  # the row remembers another artifact
    passport = _builder(journal, row, tmp_path).for_artifact(digest)

    assert passport.completeness == "DIVERGED"
    assert "authority_disagreement" in [item.code for item in passport.divergences]


def test_failed_row_cannot_carry_a_lawful_artifact(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, block = _history(journal, tmp_path)
    row = _completed_row(digest, block)
    row["execution_status"] = "failed_terminal"
    row["failure_reason"] = "verification_failed:artifact_sha256_mismatch"
    passport = _builder(journal, row, tmp_path).for_artifact(digest)

    assert passport.completeness == "DIVERGED"
    assert "artifact_from_failed_job" in [item.code for item in passport.divergences]


def test_attempt_disagreement_and_supersession_are_surfaced(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, block = _history(journal, tmp_path, attempt=1)
    row = _completed_row(digest, block, attempt=2)  # a later attempt owns the row now
    passport = _builder(journal, row, tmp_path).for_artifact(digest)

    codes = [item.code for item in passport.divergences]
    assert "attempt_disagreement" in codes
    assert "superseded_attempt" in codes
    assert passport.completeness == "DIVERGED"


def test_unsettled_row_is_in_flight_not_complete(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, block = _history(journal, tmp_path)
    row = _completed_row(digest, block)
    row["execution_status"] = "verifying"
    passport = _builder(journal, row, tmp_path).for_artifact(digest)

    assert passport.completeness == "IN_FLIGHT"
    assert "attempt_unsettled" in [item.code for item in passport.divergences]
    assert verify_passport(passport).ok is False


def test_missing_row_is_a_critical_divergence(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, _block_ = _history(journal, tmp_path)
    passport = _builder(journal, None, tmp_path).for_artifact(digest)

    assert passport.completeness == "DIVERGED"
    assert "job_row_missing" in [item.code for item in passport.divergences]


def test_journal_gaps_are_reported_as_incomplete(tmp_path: Path) -> None:
    """A ledger that lost its terminal records cannot certify the artifact."""
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, block = _history(journal, tmp_path)
    kept = journal.records()[:5]  # request, job, attempt, execution, artifact
    truncated = CausalJournal(tmp_path / "truncated.sqlite")
    for record in kept:
        truncated.append(
            stage=record.stage,
            subject=record.subject,
            parents=record.parents,
            actor=record.actor,
            authority=record.authority,
            facts=record.facts,
        )
    passport = _builder(truncated, _completed_row(digest, block), tmp_path).for_artifact_path(
        tmp_path / "output.mp4"
    )

    assert passport.completeness == "DIVERGED"
    codes = [item.code for item in passport.divergences]
    assert "verification_not_recorded" in codes
    assert "journal_incomplete" in codes


def test_artifact_outside_allowed_roots_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, block = _history(journal, workspace)
    builder = _builder(journal, _completed_row(digest, block), tmp_path / "other")
    with pytest.raises(PassportRefused, match="path_outside_allowed_roots"):
        builder.for_artifact(digest)


def test_tampered_journal_refuses_a_passport(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "causal.sqlite"
    journal = CausalJournal(db)
    _out, digest, block = _history(journal, tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(f"UPDATE {TABLE_NAME} SET recorded_at = 'forged' WHERE seq = 1")

    builder = _builder(journal, _completed_row(digest, block), tmp_path)
    with pytest.raises(PassportRefused, match="journal_tampered"):
        builder.for_artifact(digest)


def test_edited_passport_is_detected_by_the_verifier(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    _out, digest, block = _history(journal, tmp_path)
    passport = _builder(journal, _completed_row(digest, block), tmp_path).for_artifact(digest)
    forged = passport.model_copy(update={"artifact_id": "sha256:" + "9" * 64})

    verdict = verify_passport(forged)

    assert verdict.ok is False
    assert verdict.reason_code == "passport_edited"


def test_builder_requires_allowed_roots(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "causal.sqlite")
    with pytest.raises(PassportRefused, match="no_allowed_roots"):
        PassportBuilder(journal, _Reader(None), allowed_roots=[])
