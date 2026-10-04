"""Artifact Passport end-to-end over the REAL creative chain — no mock at any seam.

Real input clip → real queue → real render handler (FFmpeg via the
imageio-ffmpeg wheel) → real default verifier → durable COMPLETED row →
passport derived from journal + row + a fresh re-measurement of the bytes.

Proven here at the artifact level:

* a VERIFIED passport's subject digest equals the bytes on disk (re-measured);
* tampered artifact bytes → COMPROMISED (the producer is never the judge);
* delivered/removed artifact → VERIFIED_WITH_LIMITATIONS, never VERIFIED;
* a corrupted journal → COMPROMISED for every job it covers;
* a lost journal + labeled backfill → honest limitations, never fake VERIFIED;
* the passport is read-only: building it leaves the authoritative row and the
  journal untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
from nexus_ai_agent.application.ports.job_queue import JobStatus
from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.creative.slideshow.ffmpeg import resolve_ffmpeg_bin
from nexus_ai_agent.provenance import (
    CausalJournal,
    PassportBuilder,
    PassportStatus,
    QueueLedgerObserver,
)
from nexus_ai_agent.provenance.backfill import backfill_journal
from nexus_ai_agent.worker import default_job_handlers


def _clip(path: Path, seconds: int = 2) -> None:
    binary = resolve_ffmpeg_bin()
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _payload(workspace: Path, key: str) -> dict[str, Any]:
    return {
        "command": "edit",
        "operation": "trim",
        "args": ["0", "1"],
        "workspace_dir": str(workspace),
        "input_path": str(workspace / "input.mp4"),
        "media_duration_us": 2_000_000,
        "user_id": 42,
        "chat_id": 4242,
        "lang": "fa",
        "idempotency_key": key,
    }


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CREATIVE_TEMP_DIR", str(tmp_path / "creative_tmp"))
    (tmp_path / "creative_tmp").mkdir(parents=True)
    settings_module.get_settings.cache_clear()

    journal = CausalJournal(tmp_path / "jobs.sqlite3.causal.sqlite3")
    queue = InProcessJobQueue(
        tmp_path / "jobs.sqlite3", causal_observer=QueueLedgerObserver(journal)
    )
    for job_type, handler in default_job_handlers().items():
        queue.register_handler(job_type, handler)
    yield queue, journal
    settings_module.get_settings.cache_clear()


async def _render_trim(queue: InProcessJobQueue, key: str) -> str:
    """A real /edit trim through the real handler; the workspace lives inside
    the CREATIVE_TEMP_DIR the worker validates against (render_jobs law)."""
    workspace = queue.db_path.parent / "creative_tmp" / f"creative_{key}"
    workspace.mkdir(parents=True)
    _clip(workspace / "input.mp4")
    job_id = await queue.enqueue(
        job_type="creative_render",
        idempotency_key=key,
        payload=_payload(workspace, key),
    )
    for _ in range(600):
        if await queue.get_status(job_id) is JobStatus.COMPLETED:
            return job_id
        await asyncio.sleep(0.05)
    raise AssertionError("creative_render did not complete")


def _row_fingerprint(queue: InProcessJobQueue, job_id: str) -> str:
    """Authoritative-row fingerprint for the read-only projection proof."""
    facts = queue.get_job_facts(job_id)
    assert facts is not None
    return repr(
        (
            facts.status,
            facts.attempt,
            facts.payload_digest,
            facts.result_digest,
            facts.error,
        )
    )


async def test_real_render_passport_is_verified_and_remeasured(harness) -> None:  # noqa: ANN001
    queue, journal = harness
    job_id = await _render_trim(queue, "e2e-1")

    builder = PassportBuilder(journal, queue)
    passport = builder.build(job_id)

    assert passport.status is PassportStatus.VERIFIED
    assert passport.findings == ()
    subject = passport.content["subject"]
    assert subject["name"] == "output.mp4"
    assert subject["digest"]["sha256"].startswith("sha256:")
    assert subject["remeasurement"]["matches"] is True

    # Independent digest: recompute from the bytes ourselves, the hard way.
    artifact = Path(passport.content["predicate"]["verification"]["physical_identity"]["path"])
    assert (
        "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest() == subject["digest"]["sha256"]
    )
    # Authority is the durable actor — never an inferred one.
    assert passport.content["predicate"]["authority"]["actor"] == {
        "user_id": 42,
        "chat_id": 4242,
    }
    # The full causal account: enqueue → reserve → verify → complete.
    kinds = [r["kind"] for r in passport.content["predicate"]["chain"]["records"]]
    assert kinds == [
        "job_enqueued",
        "job_reserved",
        "job_verification_started",
        "job_completed",
    ]
    # The passport is a projection: building it changed nothing — neither the
    # authoritative row nor the journal moved.
    row_before = _row_fingerprint(queue, job_id)
    journal_before = journal.count()
    builder.build(job_id)
    assert _row_fingerprint(queue, job_id) == row_before
    assert journal.count() == journal_before


async def test_tampered_artifact_bytes_flip_passport_to_compromised(harness) -> None:  # noqa: ANN001
    queue, journal = harness
    job_id = await _render_trim(queue, "e2e-2")
    builder = PassportBuilder(journal, queue)
    assert builder.build(job_id).status is PassportStatus.VERIFIED

    result = await queue.get_result(job_id)
    artifact = Path(str(result["artifact_path"]))
    original = artifact.read_bytes()
    artifact.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))  # flip a bit

    passport = builder.build(job_id)
    assert passport.status is PassportStatus.COMPROMISED
    assert any(f.code == "artifact_digest_mismatch" for f in passport.findings)


async def test_removed_artifact_is_a_limitation_never_a_lie(harness) -> None:  # noqa: ANN001
    queue, journal = harness
    job_id = await _render_trim(queue, "e2e-3")
    result = await queue.get_result(job_id)
    Path(str(result["artifact_path"])).unlink()

    passport = PassportBuilder(journal, queue).build(job_id)
    assert passport.status is PassportStatus.VERIFIED_WITH_LIMITATIONS
    assert any(f.code == "artifact_bytes_unavailable" for f in passport.findings)


async def test_corrupted_journal_compromises_every_passport(harness) -> None:  # noqa: ANN001
    queue, journal = harness
    job_id = await _render_trim(queue, "e2e-4")

    connection = sqlite3.connect(journal.db_path)
    connection.execute(
        "UPDATE nexus_causal_journal SET record_json = REPLACE(record_json,"
        " 'job_reserved', 'job_failed') WHERE kind = 'job_reserved'"
    )
    connection.commit()
    connection.close()

    passport = PassportBuilder(journal, queue).build(job_id)
    assert passport.status is PassportStatus.COMPROMISED
    assert any(f.code == "chain_broken" for f in passport.findings)


async def test_journal_lost_then_backfilled_is_honested(harness) -> None:  # noqa: ANN001
    """A fresh journal + labeled backfill → limitations, never fake VERIFIED."""
    queue, journal = harness
    job_id = await _render_trim(queue, "e2e-5")

    fresh = CausalJournal(":memory:")
    assert PassportBuilder(fresh, queue).build(job_id).status is PassportStatus.INCOMPLETE

    report = backfill_journal(fresh, queue, [job_id])
    assert report.appended == 3
    passport = PassportBuilder(fresh, queue).build(job_id)
    assert passport.status is PassportStatus.VERIFIED_WITH_LIMITATIONS
    assert any(f.code == "backfilled_history" for f in passport.findings)
