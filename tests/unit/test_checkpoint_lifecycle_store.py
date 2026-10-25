import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_adapter import SQLiteCheckpointLifecycleAdapter
from nexus_ai_agent.storage.checkpoint_lifecycle_store import (
    SQLiteCheckpointLifecycleStore,
    cleanup_lock,
)


def test_sqlite_lifecycle_store_round_trip(tmp_path) -> None:
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "lifecycle.sqlite"))
    record = CheckpointRecord(
        "thread", "checkpoint", datetime.now(timezone.utc) - timedelta(days=2)
    )
    try:
        store.upsert(record)
        assert store.records() == [record]
        store.delete_index(record)
        assert store.records() == []
    finally:
        store.close()


def test_schema_fingerprint_is_stable(tmp_path) -> None:
    path = str(tmp_path / "lifecycle.sqlite")
    first = SQLiteCheckpointLifecycleStore(path)
    fingerprint = first.schema_fingerprint()
    first.close()
    second = SQLiteCheckpointLifecycleStore(path)
    try:
        assert second.schema_fingerprint() == fingerprint
    finally:
        second.close()


def test_failed_write_transaction_rolls_back_and_propagates(tmp_path) -> None:
    store = SQLiteCheckpointLifecycleStore(str(tmp_path / "rollback.sqlite"))
    record = CheckpointRecord("thread", "checkpoint", datetime.now(timezone.utc))
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            with store._write_transaction() as connection:
                connection.execute(
                    "INSERT INTO nexus_checkpoint_lifecycle "
                    "(thread_id, checkpoint_id, created_at, last_accessed_at, active_until) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("thread", "injected", record.created_at.isoformat(), None, None),
                )
                connection.execute("INSERT INTO missing_lifecycle_table VALUES (1)")

        assert store.records() == []
        store.upsert(record)
        assert store.records() == [record]
    finally:
        store.close()


def test_cleanup_lock_is_exclusive(tmp_path) -> None:
    path = str(tmp_path / "lifecycle.sqlite")
    with cleanup_lock(path):
        try:
            with cleanup_lock(path):
                raise AssertionError("nested cleanup lock unexpectedly acquired")
        except RuntimeError as exc:
            assert "already in progress" in str(exc)


@pytest.mark.asyncio
async def test_concurrent_adapter_writes_survive_shared_connection_reopen(tmp_path) -> None:
    """Each async writer uses a worker thread but shares one SQLite handle."""
    path = tmp_path / "concurrent-lifecycle.sqlite"
    store = SQLiteCheckpointLifecycleStore(str(path))
    adapter = SQLiteCheckpointLifecycleAdapter(store)
    created_at = datetime.now(timezone.utc)

    operations = [
        *(
            adapter.record_checkpoint("thread", f"checkpoint-{index}", created_at=created_at)
            for index in range(500)
        ),
        *(adapter.inspect() for _ in range(25)),
        *(adapter.schema_fingerprint() for _ in range(25)),
    ]
    try:
        outcomes = await asyncio.gather(*operations, return_exceptions=True)
        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        assert failures == []
        assert len(store.records()) == 500
    finally:
        store.close()

    reopened = SQLiteCheckpointLifecycleStore(str(path))
    try:
        records = reopened.records()
        assert len(records) == 500
        assert {record.checkpoint_id for record in records} == {
            f"checkpoint-{index}" for index in range(500)
        }
    finally:
        reopened.close()
