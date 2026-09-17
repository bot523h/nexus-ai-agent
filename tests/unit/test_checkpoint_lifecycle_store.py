from datetime import datetime, timedelta, timezone

from nexus_ai_agent.storage.checkpoint_lifecycle import CheckpointRecord
from nexus_ai_agent.storage.checkpoint_lifecycle_store import SQLiteCheckpointLifecycleStore


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
