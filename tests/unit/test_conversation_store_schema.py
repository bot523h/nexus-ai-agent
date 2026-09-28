"""A failed conversation-history index creation must be recorded (task-204).

``ConversationStore._ensure_table`` wrapped its ``CREATE INDEX`` in a bare
``except Exception: pass`` annotated ``# Index may already exist``.  That
justification is **provably false**: ``IF NOT EXISTS`` already suppresses the
index-already-exists error, so the only exceptions the handler can ever see are
real ones.  What it actually swallowed was a genuine schema failure, leaving
``get_history`` scanning without its index and nothing at all in the logs.

The reproducer below is deterministic and needs no mocking: a *table* squatting
the index name makes SQLite raise ``OperationalError("there is already a table
named ix_conv_history_conv_id")``.  ``IF NOT EXISTS`` does not cover that case,
which is the whole point.

Note the inconsistency the fix also removes: the ``CREATE TABLE`` immediately
above was never wrapped, so a table failure propagated while an index failure
vanished.
"""

from __future__ import annotations

import sqlite3

import pytest
from structlog.testing import capture_logs

from nexus_ai_agent.features.conversation_store import ConversationStore


def _squat_the_index_name(db_path) -> None:
    """Pre-create a TABLE with the index's name so index creation really fails."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE conversation_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "conv_id VARCHAR NOT NULL, role VARCHAR NOT NULL, "
            "parts_json TEXT NOT NULL, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("CREATE TABLE ix_conv_history_conv_id (x INTEGER)")
        conn.commit()
    finally:
        conn.close()


def test_the_reproducer_really_does_make_index_creation_fail(tmp_path) -> None:
    """Guard the guard: if SQLite stops raising, the test below proves nothing."""
    db = tmp_path / "app.sqlite"
    _squat_the_index_name(db)
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_conv_history_conv_id "
                "ON conversation_history(conv_id)"
            )
    finally:
        conn.close()


def test_a_failed_index_creation_is_recorded(tmp_path) -> None:
    """DEFECT: the failure produced no record at all."""
    db = tmp_path / "app.sqlite"
    _squat_the_index_name(db)

    with capture_logs() as logs:
        ConversationStore(db_path=str(db))

    warnings = [entry for entry in logs if entry.get("log_level") == "warning"]
    assert len(warnings) == 1, f"expected exactly one warning, got {logs!r}"
    assert warnings[0]["error_type"] == "OperationalError"


def test_the_store_still_works_when_the_index_cannot_be_created(tmp_path) -> None:
    """The non-fatal behaviour is preserved deliberately, not by accident.

    A missing index is a performance problem, not a correctness one, so
    construction must keep succeeding — but now it says so.
    """
    db = tmp_path / "app.sqlite"
    _squat_the_index_name(db)

    with capture_logs():
        store = ConversationStore(db_path=str(db))

    store.append("tg:1", {"role": "user", "parts": [{"text": "hello"}]})
    history = store.get_history("tg:1")
    assert len(history) == 1
    assert history[0]["parts"][0]["text"] == "hello"


def test_a_healthy_database_records_nothing(tmp_path) -> None:
    """No false alarms, or the warning stops meaning anything."""
    with capture_logs() as logs:
        store = ConversationStore(db_path=str(tmp_path / "clean.sqlite"))

    assert [entry for entry in logs if entry.get("log_level") == "warning"] == []

    store.append("tg:2", {"role": "user", "parts": [{"text": "hi"}]})
    assert len(store.get_history("tg:2")) == 1


def test_history_is_ordered_and_bounded(tmp_path) -> None:
    """The store's actual contract, pinned so the fix cannot quietly break it."""
    store = ConversationStore(db_path=str(tmp_path / "order.sqlite"))
    for i in range(5):
        store.append("tg:3", {"role": "user", "parts": [{"text": f"m{i}"}]})

    latest = store.get_history("tg:3", limit=2)
    assert [m["parts"][0]["text"] for m in latest] == ["m3", "m4"]
