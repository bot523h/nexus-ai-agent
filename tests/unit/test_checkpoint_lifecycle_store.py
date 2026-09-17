from datetime import datetime, timedelta, timezone

from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
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


def test_cleanup_lock_is_exclusive(tmp_path) -> None:
    path = str(tmp_path / "lifecycle.sqlite")
    with cleanup_lock(path):
        try:
            with cleanup_lock(path):
                raise AssertionError("nested cleanup lock unexpectedly acquired")
        except RuntimeError as exc:
            assert "already in progress" in str(exc)
