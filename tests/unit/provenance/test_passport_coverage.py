"""Focused unit coverage for PassportBuilder branches and edge cases."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent, EventKind, JobFacts, digest_of
from nexus_ai_agent.provenance.passport import PassportBuilder, PassportStatus


class _StubFactsProvider:
    def __init__(self, facts: Any) -> None:
        self._facts = facts

    def get_job_facts(self, job_id: str) -> Any:
        return self._facts


def test_passport_unknown_job_raises_key_error(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "j.sqlite3")
    builder = PassportBuilder(journal, _StubFactsProvider(None), sha256_fn=lambda _p: "0" * 64)
    with pytest.raises(KeyError, match="unknown job"):
        builder.build("never-seen")


def test_passport_to_dict_and_unreadable_evidence_and_payload_divergence(tmp_path: Path) -> None:
    journal = CausalJournal(tmp_path / "j.sqlite3")
    job_id = "job-corrupt-ev"
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=job_id,
            job_type="creative_render",
            idempotency_key="k-1",
            status="pending",
            payload_digest=digest_of({"x": 1}),
            occurred_at="2026-11-25T00:00:00+00:00",
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_RESERVED,
            job_id=job_id,
            job_type="creative_render",
            idempotency_key="k-1",
            attempt=1,
            status="processing",
            occurred_at="2026-11-25T00:00:01+00:00",
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_COMPLETED,
            job_id=job_id,
            job_type="creative_render",
            idempotency_key="k-1",
            attempt=1,
            status="completed",
            result_digest=digest_of({"res": "recorded"}),
            occurred_at="2026-11-25T00:00:02+00:00",
        )
    )

    facts = JobFacts(
        job_id=job_id,
        job_type="creative_render",
        idempotency_key="k-1",
        status="completed",
        attempt=1,
        error=None,
        created_at="2026-11-25T00:00:00+00:00",
        started_at="2026-11-25T00:00:01+00:00",
        finished_at="2026-11-25T00:00:02+00:00",
        payload={"x": 2},
        payload_digest=digest_of({"x": 2}),
        result={"ok": True},
        result_digest=digest_of({"res": "different"}),
        verification=None,
        attempt_history=[{"attempt": 99, "attempt_id": "other"}],
        attempt_history_known=False,
        artifact_passport_known=False,
    )
    pp = PassportBuilder(journal, _StubFactsProvider(facts), sha256_fn=lambda _p: "0" * 64).build(
        job_id
    )
    assert pp.status is PassportStatus.COMPROMISED
    codes = {f.code for f in pp.findings}
    assert {
        "queue_attempt_history_unreadable",
        "queue_artifact_passport_unreadable",
        "payload_digest_divergence",
        "result_digest_divergence",
    } <= codes

    as_dict = pp.to_dict()
    assert as_dict["passport_digest"] == pp.passport_digest
    assert as_dict["status"] == "COMPROMISED"
    assert as_dict["generated_at"] == pp.generated_at
    assert pp.content["predicate"]["lineage_binding"]["attempt_id"] is None
    assert pp.content["predicate"]["lineage_binding"]["provenance_node_id"] is None

    # Failed job with sha present in result but attempt == 0 keeps provenance_node_id None.
    sha_zero_facts = JobFacts(
        job_id=job_id,
        job_type="creative_render",
        idempotency_key="k-1",
        status="failed",
        attempt=0,
        error="early",
        created_at="2026-11-25T00:00:00+00:00",
        started_at=None,
        finished_at="2026-11-25T00:00:01+00:00",
        payload={"x": 1},
        payload_digest=digest_of({"x": 1}),
        result={"sha256": "sha256:" + "a" * 64},
        result_digest=digest_of({"sha256": "sha256:" + "a" * 64}),
        verification=None,
    )
    sha_zero_pp = PassportBuilder(
        journal, _StubFactsProvider(sha_zero_facts), sha256_fn=lambda _p: "0" * 64
    ).build(job_id)
    assert sha_zero_pp.content["predicate"]["lineage_binding"]["provenance_node_id"] is None

    # Explicit provenance_node_id in result skips fallback derivation (387->403).
    explicit_facts = JobFacts(
        job_id=job_id,
        job_type="creative_render",
        idempotency_key="k-1",
        status="failed",
        attempt=1,
        error="e",
        created_at="2026-11-25T00:00:00+00:00",
        started_at=None,
        finished_at="2026-11-25T00:00:01+00:00",
        payload={"x": 1},
        payload_digest=digest_of({"x": 1}),
        result={"provenance_node_id": "ARTIFACT:explicit"},
        result_digest=digest_of({"provenance_node_id": "ARTIFACT:explicit"}),
        verification=None,
    )
    explicit_pp = PassportBuilder(
        journal, _StubFactsProvider(explicit_facts), sha256_fn=lambda _p: "0" * 64
    ).build(job_id)
    assert explicit_pp.content["predicate"]["lineage_binding"]["provenance_node_id"] == (
        "ARTIFACT:explicit"
    )


def test_passport_inflight_and_failed_zero_attempt_and_older_terminal_retry(
    tmp_path: Path,
) -> None:
    journal = CausalJournal(tmp_path / "j.sqlite3")

    # 1. In-flight job (processing) -> INCOMPLETE with job_not_terminal
    inflight_id = "job-inflight"
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=inflight_id,
            job_type="creative_render",
            idempotency_key="k-inf",
            status="pending",
            payload_digest=digest_of({"p": 1}),
            occurred_at="2026-11-25T00:00:00+00:00",
        )
    )
    inflight_facts = JobFacts(
        job_id=inflight_id,
        job_type="creative_render",
        idempotency_key="k-inf",
        status="processing",
        attempt=1,
        error=None,
        created_at="2026-11-25T00:00:00+00:00",
        started_at="2026-11-25T00:00:01+00:00",
        finished_at=None,
        payload={"p": 1},
        payload_digest=digest_of({"p": 1}),
        result=None,
        result_digest=None,
        verification=None,
    )
    inf_pp = PassportBuilder(
        journal, _StubFactsProvider(inflight_facts), sha256_fn=lambda _p: "0" * 64
    ).build(inflight_id)
    assert inf_pp.status is PassportStatus.INCOMPLETE
    assert "job_not_terminal" in {f.code for f in inf_pp.findings}

    # 2. Failed job with attempt == 0 -> _expected_events skips JOB_RESERVED
    zero_id = "job-failed-zero"
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=zero_id,
            job_type="creative_render",
            idempotency_key="k-z",
            status="pending",
            payload_digest=digest_of({"p": 0}),
            occurred_at="2026-11-25T00:00:00+00:00",
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_FAILED,
            job_id=zero_id,
            job_type="creative_render",
            idempotency_key="k-z",
            attempt=None,
            status="failed",
            error="precheck",
            result_digest=digest_of(None),
            occurred_at="2026-11-25T00:00:01+00:00",
        )
    )
    zero_facts = JobFacts(
        job_id=zero_id,
        job_type="creative_render",
        idempotency_key="k-z",
        status="failed",
        attempt=0,
        error="precheck",
        created_at="2026-11-25T00:00:00+00:00",
        started_at=None,
        finished_at="2026-11-25T00:00:01+00:00",
        payload={"p": 0},
        payload_digest=digest_of({"p": 0}),
        result=None,
        result_digest=digest_of(None),
        verification=None,
    )
    zero_pp = PassportBuilder(
        journal, _StubFactsProvider(zero_facts), sha256_fn=lambda _p: "0" * 64
    ).build(zero_id)
    assert zero_pp.status is PassportStatus.VERIFIED

    # 3. Retry job where terminal exists only for older attempt + custom facts with attempt=None
    retry_id = "job-retry"
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=retry_id,
            job_type="creative_render",
            idempotency_key="k-r",
            status="pending",
            payload_digest=digest_of({"r": 1}),
            occurred_at="2026-11-25T00:00:00+00:00",
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_FAILED,
            job_id=retry_id,
            job_type="creative_render",
            idempotency_key="k-r",
            attempt=1,
            status="failed",
            error="transient",
            result_digest=digest_of({"err": 1}),
            occurred_at="2026-11-25T00:00:01+00:00",
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_RESERVED,
            job_id=retry_id,
            job_type="creative_render",
            idempotency_key="k-r",
            attempt=2,
            status="processing",
            occurred_at="2026-11-25T00:00:02+00:00",
        )
    )
    retry_art = tmp_path / "retry.mp4"
    retry_art.write_bytes(b"ok")
    retry_sha = "0" * 64
    retry_res = {"artifact_path": str(retry_art), "sha256": retry_sha}
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_COMPLETED,
            job_id=retry_id,
            job_type="creative_render",
            idempotency_key="k-r",
            attempt=2,
            status="completed",
            result_digest=digest_of(retry_res),
            occurred_at="2026-11-25T00:00:03+00:00",
        )
    )
    retry_facts = JobFacts(
        job_id=retry_id,
        job_type="creative_render",
        idempotency_key="k-r",
        status="completed",
        attempt=2,
        error=None,
        created_at="2026-11-25T00:00:00+00:00",
        started_at="2026-11-25T00:00:02+00:00",
        finished_at="2026-11-25T00:00:03+00:00",
        payload={"r": 1},
        payload_digest=digest_of({"r": 1}),
        result=retry_res,
        result_digest=digest_of(retry_res),
        verification=None,
    )
    retry_pp = PassportBuilder(
        journal, _StubFactsProvider(retry_facts), sha256_fn=lambda _p: "0" * 64
    ).build(retry_id)
    assert retry_pp.status is PassportStatus.VERIFIED

    # Also exercise _reconcile directly with attempt=None on an un-attempted facts stub.
    class _AttemptNoneFacts:
        job_id = zero_id
        job_type = "creative_render"
        idempotency_key = "k-z"
        status = "completed"
        attempt = None
        payload_digest = digest_of({"p": 0})
        result_digest = digest_of(None)

    builder = PassportBuilder(
        journal, _StubFactsProvider(retry_facts), sha256_fn=lambda _p: "0" * 64
    )
    reconcile_findings = builder._reconcile(
        _AttemptNoneFacts(),  # type: ignore[arg-type]
        journal.records_for_job(zero_id),
        status_known=False,
    )
    assert any(f.code == "status_divergence" for f in reconcile_findings)
