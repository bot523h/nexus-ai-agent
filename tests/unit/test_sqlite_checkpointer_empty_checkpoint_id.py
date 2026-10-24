"""An empty ``checkpoint_id`` must mean "latest" for delta-channel history.

``langgraph-checkpoint-sqlite`` 3.1.1 (the exact pin, see
``test_checkpoint_dependency_pin.py``) only treats ``None`` as "latest", so an
empty id silently returned no writes although ``get_tuple`` resolves it to the
latest checkpoint.  Upgrading to 3.1.2 would fix that upstream but changes the
checkpoint schema (``writes.task_path``), so the adapter normalises it instead.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from langgraph.checkpoint.base import create_checkpoint, empty_checkpoint

from nexus_ai_agent.storage.langgraph_checkpoint import AsyncCompatibleSqliteSaver


def _saver_with_two_checkpoints() -> tuple[
    AsyncCompatibleSqliteSaver, dict[str, Any], dict[str, Any]
]:
    saver = AsyncCompatibleSqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))
    saver.setup()
    base = {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}
    first = empty_checkpoint()
    cfg1 = saver.put(base, first, {"source": "input", "step": 0, "parents": {}}, {})
    saver.put_writes(cfg1, [("x", "hello")], "task1")
    second = create_checkpoint(first, {}, 1)
    cfg2 = saver.put(cfg1, second, {"source": "loop", "step": 1, "parents": {}}, {})
    return saver, base, cfg2


def _with_id(base: dict[str, Any], checkpoint_id: str | None) -> dict[str, Any]:
    configurable = dict(base["configurable"])
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


def test_empty_checkpoint_id_resolves_to_the_latest_checkpoint() -> None:
    saver, base, latest = _saver_with_two_checkpoints()
    expected = saver.get_delta_channel_history(config=latest, channels=["x"])
    assert expected["x"]["writes"], "fixture must produce a non-empty history"

    assert saver.get_delta_channel_history(config=_with_id(base, ""), channels=["x"]) == expected
    assert saver.get_delta_channel_history(config=_with_id(base, None), channels=["x"]) == expected


@pytest.mark.asyncio
async def test_async_empty_checkpoint_id_resolves_to_the_latest_checkpoint() -> None:
    saver, base, latest = _saver_with_two_checkpoints()
    expected = await saver.aget_delta_channel_history(config=latest, channels=["x"])
    got = await saver.aget_delta_channel_history(config=_with_id(base, ""), channels=["x"])
    assert got == expected and got["x"]["writes"]


def test_caller_config_is_not_mutated() -> None:
    saver, base, _ = _saver_with_two_checkpoints()
    config = _with_id(base, "")
    saver.get_delta_channel_history(config=config, channels=["x"])
    assert config["configurable"]["checkpoint_id"] == ""


def test_explicit_checkpoint_id_is_passed_through_untouched() -> None:
    """A real id must still address *that* checkpoint, not be rewritten to latest."""
    saver, base, latest = _saver_with_two_checkpoints()
    parent_id = latest["configurable"]["checkpoint_id"]
    tuple_ = saver.get_tuple(latest)
    assert tuple_ is not None and tuple_.parent_config is not None
    first_id = tuple_.parent_config["configurable"]["checkpoint_id"]
    assert first_id != parent_id

    at_first = saver.get_delta_channel_history(config=_with_id(base, first_id), channels=["x"])
    at_latest = saver.get_delta_channel_history(config=latest, channels=["x"])
    # History is the writes between a seed and the target, so the same write is
    # absent when addressing the first checkpoint and present from the latest.
    # Different answers prove the explicit id was honoured, not rewritten to latest.
    assert at_first["x"]["writes"] == []
    assert at_latest["x"]["writes"] == [("task1", "x", "hello")]
