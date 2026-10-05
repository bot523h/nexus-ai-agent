"""Integration test for the ``nexus jobs resume`` command (D1).

Drives the real Typer app (the exact CLI wiring an operator hits) against
an isolated job-queue sidecar:

1. an empty queue reports "no pending jobs" and exits 0;
2. a seeded pending ``pdf_extract`` job is requeued, executed by the same
   default handlers the bot registers, and reported as completed — proving
   the D1 resume path and the D3 pypdf extraction path end-to-end.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from nexus_ai_agent.config import settings as settings_module
from typer.testing import CliRunner

from nexus_ai_agent.cli import app

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point NEXUS_DB_PATH at an isolated file and clear cached settings."""
    db_file = tmp_path / "app.sqlite"
    monkeypatch.setenv("NEXUS_DB_PATH", str(db_file))
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings_module.get_settings.cache_clear()
    return db_file


def _seed_pending_pdf_job(db_file: Path, job_id: str, pdf_path: Path) -> None:
    import json

    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.worker import job_queue_db_path

    InProcessJobQueue(job_queue_db_path(db_file))  # create the owned schema
    payload = json.dumps(
        {
            "user_id": 7,
            "chat_id": 555,
            "file_path": str(pdf_path),
            "file_id": "file-cli",
        }
    )
    with sqlite3.connect(job_queue_db_path(db_file)) as connection:
        connection.execute(
            """
            INSERT INTO nexus_job_queue
                (id, job_type, idempotency_key, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                "pdf_extract",
                f"cli-{job_id}",
                payload,
                "pending",
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def test_resume_reports_empty_queue(cli_env: Path) -> None:
    from nexus_ai_agent.adapters.in_process_job_queue import InProcessJobQueue
    from nexus_ai_agent.worker import job_queue_db_path

    InProcessJobQueue(job_queue_db_path(cli_env))  # empty sidecar
    runner = CliRunner()

    result = runner.invoke(app, ["jobs", "resume"])

    assert result.exit_code == 0, result.output
    assert "No pending jobs to resume." in result.output


def test_resume_drains_seeded_pdf_job(
    cli_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pytest.importorskip("pypdf")
    captured: dict[str, Any] = {}

    class FakeRag:
        def __init__(self) -> None:
            pass

        async def add_document(self, user_id: int, text: str, metadata: dict[str, object]) -> None:
            captured.update(user_id=user_id, text=text, metadata=metadata)

    fake_rag_module = types.ModuleType("nexus_ai_agent.features.rag")
    fake_rag_module.AdvancedRAGEngine = FakeRag  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nexus_ai_agent.features.rag", fake_rag_module)

    pdf_path = tmp_path / "upload.pdf"
    pdf_path.write_bytes((FIXTURES / "minimal.pdf").read_bytes())
    _seed_pending_pdf_job(cli_env, "cli-job-1", pdf_path)

    runner = CliRunner()
    result = runner.invoke(app, ["jobs", "resume"])

    assert result.exit_code == 0, result.output
    assert "Requeued 1 job(s): cli-job-1" in result.output
    assert "cli-job-1: completed" in result.output
    assert captured == {
        "user_id": 7,
        "text": "Hello NEXUS job queue",
        "metadata": {"file_id": "file-cli"},
    }

    from nexus_ai_agent.worker import job_queue_db_path

    with sqlite3.connect(job_queue_db_path(cli_env)) as connection:
        row = connection.execute(
            "SELECT status FROM nexus_job_queue WHERE id = 'cli-job-1'"
        ).fetchone()
    assert row is not None
    assert row[0] == "completed"


def test_resume_survives_journal_evidence_conflict(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A conflicting claim already on the ledger must NOT abort the drain.

    The journal holds a JOB_ENQUEUED whose payload digest disagrees with the
    authoritative row — backfill refuses (CausalConflictError + quarantine)
    and the CLI must degrade with an explicit warning while the job still
    executes: evidence conflict, never an execution veto (D-0024).
    """
    pytest.importorskip("pypdf")
    fake_rag_module = types.ModuleType("nexus_ai_agent.features.rag")

    class FakeRag:
        async def add_document(self, user_id: int, text: str, metadata: dict[str, object]) -> None:
            pass

    fake_rag_module.AdvancedRAGEngine = FakeRag  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nexus_ai_agent.features.rag", fake_rag_module)

    from nexus_ai_agent.provenance.paths import causal_journal_db_path
    from nexus_ai_agent.worker import job_queue_db_path

    from nexus_ai_agent.provenance import CausalEvent, CausalJournal, EventKind

    pdf_path = tmp_path / "upload.pdf"
    pdf_path.write_bytes((FIXTURES / "minimal.pdf").read_bytes())
    _seed_pending_pdf_job(cli_env, "cli-job-c", pdf_path)

    journal = CausalJournal(causal_journal_db_path(job_queue_db_path(cli_env)))
    journal.append(
        CausalEvent(
            kind=EventKind.JOB_ENQUEUED,
            job_id="cli-job-c",
            job_type="pdf_extract",
            payload_digest="sha256:" + ("f" * 64),  # a DIFFERENT, older claim
        )
    )

    runner = CliRunner()
    result = runner.invoke(app, ["jobs", "resume"])

    assert result.exit_code == 0, result.output
    assert "evidence conflict" in result.output
    assert "cli-job-c: completed" in result.output

    with sqlite3.connect(job_queue_db_path(cli_env)) as connection:
        row = connection.execute(
            "SELECT status FROM nexus_job_queue WHERE id = 'cli-job-c'"
        ).fetchone()
    assert row is not None and row[0] == "completed"
    kinds = [r.kind for r in journal.records_for_job("cli-job-c")]
    assert EventKind.EVENT_CONFLICT in kinds  # the rejected claim is durably kept
