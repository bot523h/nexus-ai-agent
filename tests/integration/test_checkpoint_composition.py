"""Wiring trio: the runtime passes through LifecycleRecordingSaver.

(1) the composition root wraps the SQLite saver with the lifecycle wrapper;
(2) data flows through the wrapper unchanged while lifecycle metadata is
    recorded;
(3) the kill-switch removes the wrapper entirely (no lifecycle writes).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus_ai_agent.adapters.langgraph.lifecycle_recording import (
    LifecycleRecordingSaver,
    access_context,
)
from nexus_ai_agent.storage.checkpoint_lifecycle_store import SQLiteCheckpointLifecycleStore
from nexus_ai_agent.storage.langgraph_checkpoint import (
    AsyncCompatibleSqliteSaver,
    PostgresCheckpointer,
    get_checkpointer,
)

CHECKPOINT = {
    "v": 1,
    "id": "cp1",
    "ts": "2026-01-01T00:00:00Z",
    "channel_values": {"a": 1},
    "channel_versions": {},
    "versions_seen": {},
    "pending_sends": [],
}
CONFIG = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}


@pytest.fixture()
def settings_env(monkeypatch) -> None:
    from nexus_ai_agent.config.settings import get_settings

    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    monkeypatch.setenv("NEXUS_LIFECYCLE_HOOKS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _store_for(tmp_path: Path) -> SQLiteCheckpointLifecycleStore:
    return SQLiteCheckpointLifecycleStore(str(tmp_path / "lg.sqlite.lifecycle"))


@pytest.mark.asyncio
async def test_composition_root_wraps_saver(settings_env, tmp_path: Path) -> None:
    checkpointer = get_checkpointer(str(tmp_path / "lg.sqlite"))
    try:
        assert isinstance(checkpointer, LifecycleRecordingSaver)
        inner = checkpointer._saver
        assert isinstance(inner, AsyncCompatibleSqliteSaver)
    finally:
        checkpointer.conn.close()


@pytest.mark.asyncio
async def test_wrapper_records_lifecycle_end_to_end(settings_env, tmp_path: Path) -> None:
    checkpointer = get_checkpointer(str(tmp_path / "lg.sqlite"))
    try:
        async with access_context("user"):
            result = await checkpointer.aput(CONFIG, CHECKPOINT, {}, {})
            # The delegate result is exactly the underlying saver's result.
            assert result["configurable"]["thread_id"] == "t1"
            assert result["configurable"]["checkpoint_id"] == "cp1"
        for _ in range(5):
            await asyncio.sleep(0)  # let the best-effort record task run

        # A user read schedules a coalesced touch.
        async with access_context("user"):
            got = await checkpointer.aget_tuple(result)
        assert got is not None
        await checkpointer.flush()

        store = _store_for(tmp_path)
        try:
            records = store.records()
        finally:
            store.close()
        assert [(r.thread_id, r.checkpoint_id) for r in records] == [("t1", "cp1")]
        assert records[0].last_accessed_at is not None  # touch was applied
    finally:
        checkpointer.conn.close()


@pytest.mark.asyncio
async def test_kill_switch_removes_wrapper(settings_env, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_LIFECYCLE_HOOKS_ENABLED", "false")
    from nexus_ai_agent.config.settings import get_settings

    get_settings.cache_clear()
    checkpointer = get_checkpointer(str(tmp_path / "lg.sqlite"))
    try:
        assert isinstance(checkpointer, AsyncCompatibleSqliteSaver)
        assert not isinstance(checkpointer, LifecycleRecordingSaver)
        # And no lifecycle index file is created at all.
        assert not (tmp_path / "lg.sqlite.lifecycle").exists()
    finally:
        checkpointer.conn.close()


@pytest.mark.asyncio
async def test_postgres_path_stays_unwrapped(settings_env, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_DATABASE_URL", "postgresql://nexus:nexus@localhost:5432/nexus")
    from nexus_ai_agent.config.settings import get_settings

    get_settings.cache_clear()
    checkpointer = get_checkpointer(str(Path("unused") / "lg.sqlite"))
    assert isinstance(checkpointer, PostgresCheckpointer)
    assert not isinstance(checkpointer, LifecycleRecordingSaver)
