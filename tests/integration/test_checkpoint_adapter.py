import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from nexus_ai_agent.storage.checkpoint_adapter import (
    CleanupDisabled,
    SQLiteCheckpointAdapter,
)

GOLDEN = Path(__file__).parents[2] / "src/nexus_ai_agent/storage/golden/sqlite.langgraph.json"


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "checkpoint.sqlite"
    connection = sqlite3.connect(path)
    SqliteSaver(connection).setup()
    connection.close()
    return path


def test_setup_schema_matches_golden(tmp_path: Path) -> None:
    adapter = SQLiteCheckpointAdapter(str(_database(tmp_path)))
    try:
        adapter.assert_golden(GOLDEN)
    finally:
        adapter.close()


def test_schema_drift_disables_cleanup(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE checkpoints ADD COLUMN synthetic_drift TEXT")
    connection.commit()
    connection.close()
    adapter = SQLiteCheckpointAdapter(str(path))
    try:
        with pytest.raises(CleanupDisabled, match="fingerprint mismatch"):
            adapter.assert_golden(GOLDEN)
    finally:
        adapter.close()


def test_lineage_and_blob_reads_are_read_only(tmp_path: Path) -> None:
    adapter = SQLiteCheckpointAdapter(str(_database(tmp_path)))
    try:
        assert adapter.list_threads() == []
        assert adapter.get_blob_refs("unknown") == []
        assert adapter.verify_lineage("unknown")
        with pytest.raises(NotImplementedError, match="POST_V1"):
            adapter.delete_checkpoint("checkpoint")
        with pytest.raises(NotImplementedError, match="POST_V1"):
            adapter.delete_thread("thread")
    finally:
        adapter.close()


def test_missing_golden_disables_cleanup(tmp_path: Path) -> None:
    adapter = SQLiteCheckpointAdapter(str(_database(tmp_path)))
    try:
        with pytest.raises(FileNotFoundError):
            adapter.assert_golden(tmp_path / "missing.json")
    finally:
        adapter.close()


def test_connection_error_is_not_reported_as_schema_drift(tmp_path: Path) -> None:
    adapter = SQLiteCheckpointAdapter(str(_database(tmp_path)))
    adapter.close()
    with pytest.raises(Exception) as error:
        adapter.schema_fingerprint()
    assert "fingerprint mismatch" not in str(error.value)
