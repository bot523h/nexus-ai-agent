"""A SQLite path switch must dispose the retired engine *immediately*.

``storage.db`` keeps one cached async engine per SQLite path; switching paths
retires the previous engine into ``_replaced_engines``.  Until this fix the
retired engine was only disposed by the *next* database operation, so the last
switch in a process (and every switch in a test) left an aiosqlite worker
thread alive on a loop that was about to close — the "Event loop is closed"
noise at interpreter shutdown.  The regression test pins the contract: after a
switch the retired list is empty and the old worker thread is gone.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from sqlmodel import select

from nexus_ai_agent.storage import db as db_module
from nexus_ai_agent.storage.db import get_session
from nexus_ai_agent.storage.models import PendingApproval


def _aiosqlite_workers() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if "_connection_worker_thread" in t.name}


async def test_path_switch_disposes_the_retired_engine_immediately(tmp_path: Path) -> None:
    baseline = _aiosqlite_workers()

    async with get_session(str(tmp_path / "first.sqlite")) as session:
        (await session.exec(select(PendingApproval))).all()
    first_workers = _aiosqlite_workers() - baseline
    assert first_workers, "the first engine should own a live aiosqlite worker thread"

    async with get_session(str(tmp_path / "second.sqlite")) as session:
        (await session.exec(select(PendingApproval))).all()

    # The switch itself must have disposed the retired engine — not deferred it
    # to whichever database call happens to come next.
    assert db_module._replaced_engines == []

    # aiosqlite's worker exits within its 100 ms queue poll once closed.
    deadline = time.monotonic() + 2.0
    while any(t.is_alive() for t in first_workers) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(t.is_alive() for t in first_workers), (
        "the retired engine's aiosqlite worker thread outlived the path switch"
    )
