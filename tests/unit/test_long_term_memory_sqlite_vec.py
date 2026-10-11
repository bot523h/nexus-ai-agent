from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import LongTermMemory, _open_sqlite_connection


def test_standard_sqlite3_preferred_when_load_extension_supported(tmp_path: Path) -> None:
    """When stdlib sqlite3 connection supports enable_load_extension, stdlib sqlite3 is used."""
    db_file = str(tmp_path / "std_sqlite.db")

    import sqlite3 as std_sqlite3

    std_conn = std_sqlite3.connect(":memory:")
    has_std_ext = hasattr(std_conn, "enable_load_extension")
    std_conn.close()

    conn, can_load_ext = _open_sqlite_connection(db_file)
    try:
        assert can_load_ext is True
        if has_std_ext:
            assert type(conn).__module__.startswith("sqlite3")
        else:
            assert type(conn).__module__.startswith("pysqlite3")
    finally:
        conn.close()


def test_pysqlite3_fallback_when_stdlib_lacks_extension_loading(tmp_path: Path) -> None:
    """When stdlib sqlite3 lacks enable_load_extension, fallback to pysqlite3 connection."""
    db_file = str(tmp_path / "fallback.db")

    mock_std_conn = MagicMock(spec=["connect", "close"])
    del mock_std_conn.enable_load_extension  # Ensure hasattr returns False

    with patch("sqlite3.connect", return_value=mock_std_conn):
        conn, can_load_ext = _open_sqlite_connection(db_file)
        try:
            assert mock_std_conn.close.called
            if "pysqlite3" in sys.modules or hasattr(conn, "enable_load_extension"):
                assert can_load_ext is True
        finally:
            conn.close()


def test_missing_fallback_and_no_extension_support_fails_safe(tmp_path: Path) -> None:
    """When stdlib lacks enable_load_extension and pysqlite3 missing, fallback is safe."""
    db_file = str(tmp_path / "no_ext.db")

    class BareConn:
        def close(self) -> None:
            pass

    bare_conn = BareConn()

    with (
        patch("sqlite3.connect", return_value=bare_conn),
        patch.dict("sys.modules", {"pysqlite3": None}),
    ):
        conn, can_load_ext = _open_sqlite_connection(db_file)
        assert can_load_ext is False
        assert conn is bare_conn


@pytest.mark.asyncio
async def test_extension_loading_disabled_in_finally_block_on_exception(
    tmp_path: Path,
) -> None:
    """An exception during sqlite_vec.load must still execute enable_load_extension(False)."""
    calls: list[bool] = []

    mock_conn = MagicMock()
    mock_conn.enable_load_extension.side_effect = lambda state: calls.append(state)

    def mock_load(connection: object) -> None:
        raise RuntimeError("simulated sqlite_vec load failure")

    with (
        patch(
            "nexus_ai_agent.memory.long_term._open_sqlite_connection",
            return_value=(mock_conn, True),
        ),
        patch("sqlite_vec.load", side_effect=mock_load),
    ):
        memory = LongTermMemory(str(tmp_path / "vec_err.db"), FakeLLMProvider())
        _ = memory._conn_()
        assert memory._use_vec is False
        assert calls == [True, False], "enable_load_extension(False) must run in finally block"


@pytest.mark.asyncio
async def test_persistence_and_reopening_vector_capability(tmp_path: Path) -> None:
    """Data stored in LongTermMemory persists and can be queried after reopening DB."""
    db_file = str(tmp_path / "persistent_memory.sqlite")
    provider = FakeLLMProvider()

    # Phase 1: Write memories
    mem1 = LongTermMemory(db_file, provider)
    await mem1.store("thread-1", "Python testing best practices")
    await mem1.store("thread-1", "Vector search with sqlite-vec")
    assert mem1._conn is not None

    # Phase 2: Reopen and search
    mem2 = LongTermMemory(db_file, provider)
    results = await mem2.search("thread-1", "sqlite-vec", top_k=2)
    assert len(results) > 0
    assert any("sqlite-vec" in r for r in results)
