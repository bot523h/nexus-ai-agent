from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


def get_checkpointer(path: str) -> AsyncSqliteSaver:
    return AsyncSqliteSaver.from_conn_string(path)
