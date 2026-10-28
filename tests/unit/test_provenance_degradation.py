"""Degradation contract: evidence failure must never kill the execution plane.

The policy point is ``provenance.paths.try_open_causal_journal`` — every
composition root opens the ledger through it, so a corrupt, locked or
unavailable sidecar degrades to ``None`` (logged ``causal_journal_unavailable``)
and execution continues without recording. The passport plane then honestly
reports absent evidence; nothing ever pretends it is complete.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus_ai_agent.provenance.journal import CausalJournal
from nexus_ai_agent.provenance.models import CausalEvent, EventKind
from nexus_ai_agent.provenance.paths import (
    causal_journal_db_path,
    try_open_causal_journal,
)


class TestTryOpenCausalJournal:
    def test_healthy_path_opens_and_ensures_schema(self, tmp_path: Path) -> None:
        journal = try_open_causal_journal(tmp_path / "causal.sqlite3")
        assert journal is not None
        journal.append(
            CausalEvent(
                kind=EventKind.JOB_ENQUEUED,
                job_id="j1",
                job_type="creative_render",
            )
        )
        assert journal.count() == 1

    def test_missing_directory_is_created_not_fatal(self, tmp_path: Path) -> None:
        journal = try_open_causal_journal(tmp_path / "deep/nested/causal.sqlite3")
        assert journal is not None

    def test_corrupt_sidecar_degrades_to_none_with_logged_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "causal.sqlite3"
        path.write_bytes(b"this is not a sqlite database at all" * 10)
        with caplog.at_level("WARNING"):
            journal = try_open_causal_journal(path)
        assert journal is None
        assert any("causal_journal_unavailable" in record.message for record in caplog.records)

    def test_locked_sidecar_degrades_after_busy_timeout(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Another process holding the write lock: degrade, never hang/raise."""
        path = tmp_path / "causal.sqlite3"
        CausalJournal(path)  # ensure the schema exists first
        holder = sqlite3.connect(path, timeout=0.01)
        holder.execute("BEGIN EXCLUSIVE")  # a foreign writer owns the file
        try:
            with caplog.at_level("WARNING"):
                journal = try_open_causal_journal(path, connect_timeout=0.05)
            assert journal is None
            assert any("causal_journal_unavailable" in record.message for record in caplog.records)
        finally:
            holder.rollback()
            holder.close()

    def test_sidecar_path_convention_is_stable(self, tmp_path: Path) -> None:
        assert causal_journal_db_path("/x/db.sqlite3.jobs.sqlite3") == Path(
            "/x/db.sqlite3.jobs.sqlite3.causal.sqlite3"
        )
