from __future__ import annotations

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


def get_checkpointer(path: str) -> AsyncSqliteSaver:
    """
    Create an async SQLite checkpointer for LangGraph.

    Note: ':memory:' is supported for tests.
    """

    return AsyncSqliteSaver.from_conn_string(path)

