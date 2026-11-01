from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus_ai_agent.llm.fake_llm import FakeLLMProvider
from nexus_ai_agent.memory.long_term import (
    LongTermMemory,
    _open_sqlite_connection,
    _test_extension_capability,
)


def test_standard_sqlite3_preferred_when_load_extension_supported(tmp_path: Path) -> None:
    """When stdlib sqlite3 connection supports enable_load_extension, stdlib sqlite3 is used."""
    db_file = str(tmp_path / "std_sqlite.db")

    std_conn = sqlite3.connect(":memory:")
    try:
        std_conn.enable_load_extension(True)
        std_conn.enable_load_extension(False)
        has_std_ext = True
    except Exception:
        has_std_ext = False
    finally:
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


def test_test_extension_capability_fails_if_disable_raises() -> None:
    """If enable_load_extension(False) raises, _test_extension_capability returns False."""
    mock_conn = MagicMock()

    def mock_enable(state: bool) -> None:
        if not state:
            raise RuntimeError("disable failed")

    mock_conn.enable_load_extension.side_effect = mock_enable
    assert _test_extension_capability(mock_conn) is False


def test_pysqlite3_fallback_when_stdlib_enable_load_extension_raises(tmp_path: Path) -> None:
    """When stdlib enable_load_extension(True) raises, fallback to pysqlite3 connection occurs."""
    db_file = str(tmp_path / "fallback_raise.db")

    mock_std_conn = MagicMock()
    mock_std_conn.enable_load_extension.side_effect = RuntimeError("disabled in C build")

    mock_fallback_conn = MagicMock()
    mock_pysqlite3 = MagicMock()
    mock_pysqlite3.connect.return_value = mock_fallback_conn

    with (
        patch("sqlite3.connect", return_value=mock_std_conn),
        patch.dict("sys.modules", {"pysqlite3": mock_pysqlite3}),
    ):
        conn, can_load_ext = _open_sqlite_connection(db_file)
        try:
            assert mock_std_conn.close.called
            assert can_load_ext is True
            assert conn is mock_fallback_conn
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
async def test_search_raises_genuine_database_error(tmp_path: Path) -> None:
    """Genuine database errors in search must raise rather than being masked as fallback."""
    db_file = str(tmp_path / "err_search.sqlite")
    provider = FakeLLMProvider()

    memory = LongTermMemory(db_file, provider)
    memory._use_vec = True

    mock_conn = MagicMock()
    mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error or corrupted table")
    memory._conn = mock_conn

    with pytest.raises(sqlite3.OperationalError) as exc_info:
        await memory.search("thread-1", "query")

    assert "corrupted table" in str(exc_info.value)


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

    driver_module = type(mem1._conn).__module__
    assert driver_module.startswith("sqlite3") or driver_module.startswith("pysqlite3")

    # Phase 2: Reopen and search
    mem2 = LongTermMemory(db_file, provider)
    results = await mem2.search("thread-1", "sqlite-vec", top_k=2)
    assert mem2._use_vec == mem1._use_vec
    assert len(results) > 0
    assert any("sqlite-vec" in r or "testing" in r for r in results)


@pytest.mark.asyncio
async def test_non_vector_fallback_search(tmp_path: Path) -> None:
    """When _use_vec is False, search uses recency-based ordering."""
    db_file = str(tmp_path / "recency.sqlite")
    provider = FakeLLMProvider()

    mem = LongTermMemory(db_file, provider)
    mem._use_vec = False

    await mem.store("thread-1", "first note")
    await mem.store("thread-1", "second note")

    results = await mem.search("thread-1", "note", top_k=2)
    assert results == ["second note", "first note"]


def test_vec_unavailable_classifier_only_matches_vec_function_errors() -> None:
    """Only an unknown/no-such *vec_* function may degrade to recency search."""
    from nexus_ai_agent.memory.long_term import _is_vec_function_unavailable

    assert _is_vec_function_unavailable(
        sqlite3.OperationalError("no such function: vec_distance_cosine")
    )
    assert _is_vec_function_unavailable(
        sqlite3.OperationalError("unknown function: vec_distance_L2")
    )
    # A genuine SQL failure must never be masked.
    assert not _is_vec_function_unavailable(sqlite3.OperationalError("no such table: memories"))
    assert not _is_vec_function_unavailable(sqlite3.OperationalError("database is locked"))
    assert not _is_vec_function_unavailable(sqlite3.OperationalError("disk I/O error"))


def test_fallback_retries_pysqlite3_when_vec_load_fails_on_stdlib(tmp_path: Path) -> None:
    """If the stdlib connection can load extensions but sqlite-vec fails to load,
    the optional pysqlite3 driver is retried and wins when it loads the extension."""
    db_file = str(tmp_path / "loadfail_retry.db")
    std_conn = MagicMock()
    std_conn.__class__.__module__ = "sqlite3"
    py_conn = MagicMock()
    py_conn.__class__.__module__ = "pysqlite3"

    fake_pysqlite3 = MagicMock()
    fake_pysqlite3.connect.return_value = py_conn

    with (
        patch(
            "nexus_ai_agent.memory.long_term._open_sqlite_connection",
            return_value=(std_conn, True),
        ),
        patch("nexus_ai_agent.memory.long_term._pysqlite3_module", return_value=fake_pysqlite3),
        # first attempt (stdlib) fails, retry (pysqlite3) succeeds
        patch("nexus_ai_agent.memory.long_term._enable_sqlite_vec", side_effect=[False, True]),
    ):
        memory = LongTermMemory(db_file, FakeLLMProvider())
        memory._conn_()
        assert memory._use_vec is True
        assert memory._conn is py_conn
        assert std_conn.close.called  # the stdlib connection is released on swap


def test_fallback_keeps_stdlib_connection_when_pysqlite3_also_fails(tmp_path: Path) -> None:
    """When neither driver can load sqlite-vec, the stdlib connection is kept open
    (never left closed) and _use_vec stays False — honest degradation, no crash."""
    db_file = str(tmp_path / "bothfail.db")
    std_conn = MagicMock()
    std_conn.__class__.__module__ = "sqlite3"
    py_conn = MagicMock()
    py_conn.__class__.__module__ = "pysqlite3"

    fake_pysqlite3 = MagicMock()
    fake_pysqlite3.connect.return_value = py_conn

    with (
        patch(
            "nexus_ai_agent.memory.long_term._open_sqlite_connection",
            return_value=(std_conn, True),
        ),
        patch("nexus_ai_agent.memory.long_term._pysqlite3_module", return_value=fake_pysqlite3),
        patch("nexus_ai_agent.memory.long_term._enable_sqlite_vec", side_effect=[False, False]),
    ):
        memory = LongTermMemory(db_file, FakeLLMProvider())
        memory._conn_()
        assert memory._use_vec is False
        assert memory._conn is std_conn
        assert not std_conn.close.called  # the working stdlib connection is NOT closed
        assert py_conn.close.called  # the unusable retry connection is cleaned up


def test_enable_sqlite_vec_returns_false_on_load_failure_and_redisisables() -> None:
    """_enable_sqlite_vec reports success honestly and always re-disables loading."""
    from nexus_ai_agent.memory.long_term import _enable_sqlite_vec

    calls: list[bool] = []
    conn = MagicMock()
    conn.enable_load_extension.side_effect = lambda state: calls.append(state)

    with patch("sqlite_vec.load", side_effect=RuntimeError("boom")):
        assert _enable_sqlite_vec(conn) is False
    assert calls == [True, False], "extension loading must be disabled again after a failure"

    calls.clear()
    with patch("sqlite_vec.load", return_value=None):
        assert _enable_sqlite_vec(conn) is True
    assert calls == [True, False]


def test_enable_sqlite_vec_false_when_extension_module_absent() -> None:
    from nexus_ai_agent.memory.long_term import _enable_sqlite_vec

    conn = MagicMock()
    with patch.dict("sys.modules", {"sqlite_vec": None}):
        assert _enable_sqlite_vec(conn) is False
    assert not conn.enable_load_extension.called


def test_open_sqlite_connection_degrades_when_pysqlite3_absent(tmp_path: Path) -> None:
    """When the optional driver is absent and stdlib cannot load extensions,
    the stdlib connection is still returned (never None) with capability False."""
    from nexus_ai_agent.memory.long_term import _open_sqlite_connection

    db_file = str(tmp_path / "absent_driver.db")
    with (
        patch("nexus_ai_agent.memory.long_term._test_extension_capability", return_value=False),
        patch("nexus_ai_agent.memory.long_term._pysqlite3_module", return_value=None),
    ):
        conn, can_load_ext = _open_sqlite_connection(db_file)
    try:
        assert conn is not None
        assert can_load_ext is False
    finally:
        conn.close()


def test_open_sqlite_connection_keeps_stdlib_when_fallback_connect_raises(tmp_path: Path) -> None:
    """A fallback driver that cannot open the database must not lose the working
    stdlib connection: the stdlib connection is returned, still usable."""
    from nexus_ai_agent.memory.long_term import _open_sqlite_connection

    db_file = str(tmp_path / "fallback_connect_boom.db")
    exploding = MagicMock()
    exploding.connect.side_effect = RuntimeError("cannot open fallback db")
    with (
        patch("nexus_ai_agent.memory.long_term._test_extension_capability", return_value=False),
        patch("nexus_ai_agent.memory.long_term._pysqlite3_module", return_value=exploding),
    ):
        conn, can_load_ext = _open_sqlite_connection(db_file)
    try:
        assert can_load_ext is False
        conn.execute("SELECT 1")  # the returned connection is genuinely usable
    finally:
        conn.close()
