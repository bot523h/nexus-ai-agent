"""inspect-v1 contract: read-only, schema key, unknown_fields, estimates.

The inspect command must never touch the data: no access timestamps, no
created files, no writes of any kind (invariant I1 + "No hidden mutation").
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from typer.testing import CliRunner

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
    from nexus_ai_agent.config.settings import get_settings

    monkeypatch.setenv("NEXUS_CHECKPOINT_PATH", str(tmp_path / "lg.sqlite"))
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
