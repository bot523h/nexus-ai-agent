"""inspect-v1 contract: read-only, schema key, unknown_fields, estimates.

The inspect command must never touch the data: no access timestamps, no
created files, no writes of any kind (invariant I1 + "No hidden mutation").
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from typer.testing import CliRunner

from nexus_ai_agent.config.settings import get_settings
from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_store import SQLiteCheckpointLifecycleStore

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _make_db(path: Path, thread: str, checkpoints: int) -> None:
    conn = sqlite3.connect(path)
    SqliteSaver(conn).setup()
    for i in range(1, checkpoints + 1):
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
            "type, checkpoint, metadata) VALUES (?, '', ?, ?, 'json', '{}', '{}')",
            (thread, f"cp{i}", None if i == 1 else f"cp{i - 1}"),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "lg.sqlite"))
    # Hermetic backend: pin the SQLite path even if a PG URL is present in
    # the environment (PR3 backend branching in the CLI).
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _invoke() -> list[dict[str, object]]:
    from nexus_ai_agent.cli import app

    result = CliRunner().invoke(app, ["checkpoints", "inspect", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_inspect_v1_schema_key_and_known_thread(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "t1", 2)
    store = SQLiteCheckpointLifecycleStore(str(db) + ".lifecycle")
    for i in (1, 2):
        store.upsert(
            CheckpointRecord(
                "t1",
                f"cp{i}",
                NOW - timedelta(days=10),
                last_accessed_at=NOW - timedelta(hours=1),
            )
        )
    store.close()

    output = _invoke()

    assert [item["thread_id"] for item in output] == ["t1"]
    entry = output[0]
    assert entry["schema"] == "inspect-v1"
    assert entry["unknown_fields"] == []
    assert entry["missing_lifecycle"] is False
    assert entry["checkpoint_count"] == 2
    assert isinstance(entry["would_free_bytes_estimate"], int)
    assert entry["would_free_bytes_estimate"] > 0  # two checkpoints stored


def test_inspect_v1_unknown_fields_for_missing_lifecycle(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "t2", 1)

    output = _invoke()

    entry = output[0]
    assert entry["schema"] == "inspect-v1"
    assert entry["missing_lifecycle"] is True
    assert entry["unknown_fields"] == [
        "last_accessed_at",
        "newest_created_at",
        "oldest_created_at",
        "pinned",
    ]
    # Unknown values stay live in the output (I10: unknown => inspect live).
    assert entry["oldest_created_at"] == "unknown"


def test_inspect_never_creates_the_lifecycle_index(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "t3", 1)
    lifecycle_file = Path(str(db) + ".lifecycle")
    assert not lifecycle_file.exists()

    _invoke()

    # "No hidden mutation": a missing index must stay missing after inspect.
    assert not lifecycle_file.exists()


def test_inspect_does_not_touch_access_timestamps(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "t4", 1)
    store = SQLiteCheckpointLifecycleStore(str(db) + ".lifecycle")
    store.upsert(CheckpointRecord("t4", "cp1", NOW - timedelta(days=2)))
    store.close()

    _invoke()

    store = SQLiteCheckpointLifecycleStore(str(db) + ".lifecycle")
    try:
        records = store.records()
    finally:
        store.close()
    assert len(records) == 1
    assert records[0].last_accessed_at is None  # untouched by the admin read


def test_inspect_thread_filter(cli_env: Path) -> None:
    db = cli_env / "lg.sqlite"
    _make_db(db, "ta", 1)
    _make_db(db, "tb", 1)

    from nexus_ai_agent.cli import app

    result = CliRunner().invoke(app, ["checkpoints", "inspect", "--json", "--thread", "tb"])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert [item["thread_id"] for item in output] == ["tb"]


# ─────────────────────────────────────────────────────────────────────────
# inspect-v1 retention verdict: thread-scoped, never row-position-scoped.
#
# A thread owns many lifecycle rows.  The destructive fields
# (``pinned`` / ``active`` / ``resumable_within_window`` / ``would_delete``)
# used to be derived from ``metadata[-1]`` -- one arbitrary row of a
# ``SELECT`` with no ``ORDER BY``.  The lifecycle index is a rowid table, so
# "the last row" is *insertion order*, i.e. a storage-engine artefact.  Two
# logically identical threads could therefore get opposite destructive
# verdicts, and a thread pinned until +7d and accessed a day ago could be
# reported as ``pinned: false, would_delete: true``.
#
# These tests pin the corrected contract: protection is existential,
# destruction is universal, and the verdict is invariant under row order.
# ─────────────────────────────────────────────────────────────────────────

RETENTION_NOW = datetime.now(timezone.utc)


def _live() -> CheckpointRecord:
    """Accessed 1 day ago, pinned until +7d: protected on both counts."""
    return CheckpointRecord(
        "t1",
        "cp_live",
        RETENTION_NOW - timedelta(days=1),
        last_accessed_at=RETENTION_NOW - timedelta(days=1),
        active_until=RETENTION_NOW + timedelta(days=7),
    )


def _stale(checkpoint_id: str = "cp_old") -> CheckpointRecord:
    """40 days old and untouched since: individually deletable."""
    return CheckpointRecord(
        "t1",
        checkpoint_id,
        RETENTION_NOW - timedelta(days=40),
        last_accessed_at=RETENTION_NOW - timedelta(days=40),
    )


_SCENARIO = 0


def _thread_with_rows(cli_env: Path, rows: list[CheckpointRecord]) -> None:
    """Build one real thread on a FRESH checkpoint DB, rows written in order.

    Each call gets its own database and re-points the CLI at it, so one test
    can compare two logically identical threads that differ only in the
    physical order their lifecycle rows were written in.
    """
    global _SCENARIO
    _SCENARIO += 1
    db = cli_env / f"scenario_{_SCENARIO}.sqlite"
    _make_db(db, "t1", 2)
    store = SQLiteCheckpointLifecycleStore(str(db) + ".lifecycle")
    try:
        for record in rows:
            store.upsert(record)
    finally:
        store.close()

    os.environ["NEXUS_CHECKPOINT_PATH"] = str(db)
    get_settings.cache_clear()


def test_inspect_reports_a_pinned_thread_as_pinned(cli_env: Path) -> None:
    """One pinned row protects the thread, even when it is not the last row."""
    _thread_with_rows(cli_env, [_live(), _stale()])

    entry = _invoke()[0]

    assert entry["pinned"] is True
    assert entry["active"] is True
    assert entry["resumable_within_window"] is True
    # The safety-critical field: a live thread is never recommended for deletion.
    assert entry["would_delete"] is False


def test_inspect_verdict_is_invariant_under_lifecycle_row_order(cli_env: Path) -> None:
    """Logically identical threads get identical verdicts.

    This is the direct regression test for the ``metadata[-1]`` defect: the
    same two rows in the opposite physical order must not flip a destructive
    recommendation.
    """
    _thread_with_rows(cli_env, [_live(), _stale()])
    live_first = _invoke()[0]

    _thread_with_rows(cli_env, [_stale(), _live()])
    stale_first = _invoke()[0]

    fields = ("pinned", "active", "resumable_within_window", "would_delete")
    assert {key: live_first[key] for key in fields} == {key: stale_first[key] for key in fields}, (
        f"retention verdict depends on lifecycle row order: {live_first} != {stale_first}"
    )
    assert live_first["would_delete"] is False


def test_inspect_would_delete_requires_every_row_to_be_deletable(cli_env: Path) -> None:
    """``would_delete`` is universal over the thread's rows, not one of them."""
    _thread_with_rows(cli_env, [_stale("cp_a"), _stale("cp_b")])
    assert _invoke()[0]["would_delete"] is True

    # A single recently-accessed sibling vetoes the whole thread.
    _thread_with_rows(cli_env, [_stale("cp_a"), _live()])
    assert _invoke()[0]["would_delete"] is False


def test_inspect_never_accessed_row_blocks_deletion(cli_env: Path) -> None:
    """Unknown age is retained: a row with no ``last_accessed_at`` vetoes."""
    never_accessed = CheckpointRecord("t1", "cp_unknown", RETENTION_NOW - timedelta(days=40))
    _thread_with_rows(cli_env, [_stale(), never_accessed])

    entry = _invoke()[0]

    assert entry["would_delete"] is False
    assert entry["resumable_within_window"] is True


# ─────────────────────────────────────────────────────────────────────────
# inspect-v1 `protection_reason`: a destructive verdict must be actionable.
#
# `would_delete: false` alone does not tell an operator whether the thread is
# protected by an explicit pin, by ordinary recency, or merely retained because
# its age is unknown.  Those are three different situations and they used to
# collapse into one boolean (`resumable_within_window`).  The reason field names
# the evidence without changing the type of any existing field.
# ─────────────────────────────────────────────────────────────────────────


def test_inspect_reports_why_a_thread_is_retained(cli_env: Path) -> None:
    """An explicit pin is reported as a pin, not as an anonymous 'not deletable'."""
    _thread_with_rows(cli_env, [_live(), _stale()])

    entry = _invoke()[0]

    assert entry["would_delete"] is False
    assert entry["protection_reason"] == "pinned"


def test_inspect_distinguishes_recency_from_an_explicit_pin(cli_env: Path) -> None:
    """Recent access protects too, and says so — a different reason, same verdict."""
    recent = CheckpointRecord(
        "t1",
        "cp_recent",
        RETENTION_NOW - timedelta(days=2),
        last_accessed_at=RETENTION_NOW - timedelta(days=1),
    )
    _thread_with_rows(cli_env, [recent])

    entry = _invoke()[0]

    assert entry["would_delete"] is False
    assert entry["pinned"] is False
    assert entry["protection_reason"] == "recent_access"


def test_inspect_reports_absence_of_evidence_as_no_evidence(cli_env: Path) -> None:
    """`no_evidence` is a retention reason, and it is never a claim of safety."""
    never_accessed = CheckpointRecord("t1", "cp_unknown", RETENTION_NOW - timedelta(days=40))
    _thread_with_rows(cli_env, [never_accessed])

    entry = _invoke()[0]

    assert entry["would_delete"] is False
    assert entry["protection_reason"] == "no_evidence"


def test_inspect_reports_no_protection_for_a_deletable_thread(cli_env: Path) -> None:
    """`none` is the only reason that co-occurs with `would_delete: true`."""
    _thread_with_rows(cli_env, [_stale("cp_a"), _stale("cp_b")])

    entry = _invoke()[0]

    assert entry["would_delete"] is True
    assert entry["protection_reason"] == "none"


def test_inspect_reason_is_never_the_unknown_sentinel(cli_env: Path) -> None:
    """The reason field must not enter `unknown_fields` — it always has a value.

    Adding a key whose value can be the string "unknown" would silently change
    the inspect-v1 `unknown_fields` contract; `no_evidence` is the explicit
    spelling for "we do not know", so the list stays stable.
    """
    db = cli_env / "lg.sqlite"
    _make_db(db, "t1", 1)  # checkpoints exist, but no lifecycle index at all

    entry = _invoke()[0]

    assert entry["missing_lifecycle"] is True
    assert entry["protection_reason"] == "no_evidence"
    assert "protection_reason" not in entry["unknown_fields"]
    assert entry["unknown_fields"] == [
        "last_accessed_at",
        "newest_created_at",
        "oldest_created_at",
        "pinned",
    ]


def test_inspect_does_not_claim_resumability_it_has_no_evidence_for(cli_env: Path) -> None:
    """`resumable_within_window` is a truth claim, not the negation of a verdict.

    A thread with checkpoints but no lifecycle index at all is *retained*
    (``would_delete: false``, fail-closed) — but nothing is known about it, so
    it must not be reported as resumable.  ``known and not deletable`` keeps
    those two facts apart; plain ``not deletable`` would conflate them.
    """
    db = cli_env / "lg.sqlite"
    _make_db(db, "t1", 1)

    entry = _invoke()[0]

    assert entry["missing_lifecycle"] is True
    assert entry["would_delete"] is False  # retained, fail-closed
    assert entry["resumable_within_window"] is False  # but NOT claimed resumable
    assert entry["pinned"] == "unknown"
