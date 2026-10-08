"""The passport's re-measurement verdict is fail-closed (no silent "safe" default).

``PassportBuilder._status`` decides COMPROMISED from the ``remeasurement`` block
that ``_remeasure`` writes onto the subject. That block is
``{"possible": True, "measured_sha256": ..., "matches": <bool>}`` when the bytes
are reachable and ``{"possible": False, ...}`` otherwise.

The comparison used to read ``remeasurement.get("matches", True)`` — a missing
verdict defaulted to *match*. A subject carrying a re-measurement with no (or a
non-boolean) verdict therefore certified as VERIFIED: a fail-open default, the
same class as ``result.get("x", True)`` (AGENTS Rule 08).

Contract now pinned here:

* ``matches is False``            → COMPROMISED / ``artifact_digest_mismatch``
* ``possible`` and verdict absent → COMPROMISED / ``artifact_remeasurement_indeterminate``
* ``matches is True``             → not COMPROMISED by the re-measurement

The subject is injected directly (the invariant is ``_status``'s input contract,
independent of how ``_subject`` produces it), so the tests are deterministic and
need no real artifact bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent, EventKind, JobFacts
from nexus_ai_agent.provenance.observer import utc_now_iso
from nexus_ai_agent.provenance.passport import PassportBuilder, PassportStatus


class _FactsMap:
    def __init__(self, facts: JobFacts) -> None:
        self._facts = {facts.job_id: facts}

    def get_job_facts(self, job_id: str) -> JobFacts | None:
        return self._facts.get(job_id)


def _completed_facts(job_id: str) -> JobFacts:
    return JobFacts(
        job_id=job_id,
        job_type="creative_render",
        idempotency_key="ik-remeasure",
        status="completed",
        attempt=1,
        error=None,
        created_at=None,
        started_at=None,
        finished_at=None,
        payload={},
        payload_digest="sha256:" + ("a" * 64),
        result={"ok": True},
        result_digest="sha256:" + ("b" * 64),
        verification=None,
        status_known=True,
    )


def _build(tmp_path: Path, job_id: str) -> tuple[PassportBuilder, JobFacts]:
    journal = CausalJournal(tmp_path / f"{job_id}.sqlite3")
    facts = _completed_facts(job_id)
    now = utc_now_iso()
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id=facts.job_id,
            job_type=facts.job_type,
            idempotency_key=facts.idempotency_key,
            attempt=None,
            payload_digest=facts.payload_digest,
            occurred_at=now,
        )
    )
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_COMPLETED,
            job_id=facts.job_id,
            job_type=facts.job_type,
            idempotency_key=facts.idempotency_key,
            attempt=1,
            status="completed",
            payload_digest=facts.payload_digest,
            result_digest=facts.result_digest,
            occurred_at=now,
        )
    )
    return PassportBuilder(journal, _FactsMap(facts)), facts


def _status_for(
    tmp_path: Path, job_id: str, remeasurement: dict[str, Any]
) -> tuple[PassportStatus, set[str]]:
    builder, facts = _build(tmp_path, job_id)
    subject = {"name": "out.mp4", "digest": {"sha256": "sha256:" + ("c" * 64)}}
    subject["remeasurement"] = remeasurement
    # A fully-accounted history: the re-measurement block is the only variable.
    status = builder._status(facts, subject, [])
    return status, set()


def test_mismatch_is_compromised(tmp_path: Path) -> None:
    status, _ = _status_for(
        tmp_path, "job-mismatch", {"possible": True, "measured_sha256": "x", "matches": False}
    )
    assert status is PassportStatus.COMPROMISED


def test_measurement_without_verdict_is_not_a_pass(tmp_path: Path) -> None:
    """The exact fail-open: ``possible`` present, ``matches`` absent."""
    status, _ = _status_for(tmp_path, "job-no-verdict", {"possible": True, "measured_sha256": "x"})
    assert status is PassportStatus.COMPROMISED


@pytest.mark.parametrize("bogus", ["true", 1, None, [], {}])
def test_non_boolean_verdict_is_not_a_pass(tmp_path: Path, bogus: Any) -> None:
    status, _ = _status_for(
        tmp_path, f"job-bogus-{type(bogus).__name__}", {"possible": True, "matches": bogus}
    )
    assert status is PassportStatus.COMPROMISED


def test_true_verdict_is_not_compromised(tmp_path: Path) -> None:
    status, _ = _status_for(
        tmp_path, "job-true", {"possible": True, "measured_sha256": "x", "matches": True}
    )
    assert status is not PassportStatus.COMPROMISED


def test_indeterminate_finding_is_reported(tmp_path: Path) -> None:
    builder, facts = _build(tmp_path, "job-finding")
    subject = {
        "name": "out.mp4",
        "digest": {"sha256": "sha256:" + ("c" * 64)},
        "remeasurement": {"possible": True, "measured_sha256": "x"},
    }
    findings: list[Any] = []
    builder._status(facts, subject, findings)
    codes = {f.code for f in findings}
    assert "artifact_remeasurement_indeterminate" in codes
