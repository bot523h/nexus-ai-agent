"""Integration: durable project revisions survive a real process restart.

A **separate OS process** commits a genesis revision and a child revision to a
SQLite file, prints the durable head it produced, and exits.  The parent then
opens the same file with a brand-new store and proves it rebuilds the identical
revision id, state hash and full project state.  This is real durability across
process death, not a reused in-memory object.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore

pytestmark = [pytest.mark.integration]

PROJECT = "proj-restart"

# A child process that commits genesis + one child revision and reports the
# durable facts it produced, so the parent can verify them independently.
_CHILD = """
import asyncio, json, sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, {src!r})
from nexus_ai_agent.storage.db import get_session, create_all_tables
from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore
from nexus_ai_agent.creative.studio.models import new_project, Timeline, compute_state_hash

db_path = Path(sys.argv[1])
project_id = sys.argv[2]


def factory():
    @asynccontextmanager
    async def f():
        async with get_session(str(db_path)) as s:
            yield s
    return f()


async def main():
    await create_all_tables(str(db_path))
    store = SqlProjectRevisionStore(factory)
    genesis = new_project(project_id, "Alpha", Timeline(timeline_id="tl", duration_us=0))
    r0 = await store.commit(
        genesis, authorized_project_id=project_id, expected_parent_revision_id=None
    )
    child = genesis.model_copy(deep=True)
    child.state_revision = 1
    child.name = "Alpha-Durable"
    child.state_hash = compute_state_hash(child)
    r1 = await store.commit(
        child, authorized_project_id=project_id, expected_parent_revision_id=r0.revision_id
    )
    print(json.dumps({{
        "revision_id": r1.revision_id,
        "state_hash": r1.state_hash,
        "state_revision": r1.state_revision,
        "parent_revision_id": r1.parent_revision_id,
        "name": child.name,
    }}))


asyncio.run(main())
"""


def _run_child(tmp_path: Path) -> dict:
    src = str(Path(__file__).resolve().parents[2] / "src")
    script = _CHILD.format(src=src)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "durable.sqlite"), PROJECT],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": src},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(os.name != "posix", reason="subprocess restart proof is POSIX-oriented")
async def test_head_revision_survives_a_real_process_restart(tmp_path: Path) -> None:
    produced = _run_child(tmp_path)

    # A brand-new store over the same file — a fresh process/composition.
    store = SqlProjectRevisionStore(lambda: _factory(tmp_path / "durable.sqlite"))
    head = await store.load_head(PROJECT)
    assert head is not None
    assert head.revision_id == produced["revision_id"]
    assert head.state_hash == produced["state_hash"]
    assert head.state_revision == produced["state_revision"]
    assert head.parent_revision_id == produced["parent_revision_id"]

    rebuilt = head.to_project()
    assert rebuilt.name == produced["name"]
    assert rebuilt.state_hash == produced["state_hash"]
    assert rebuilt.project_id == PROJECT


def _factory(db_path: Path):
    from contextlib import asynccontextmanager

    from nexus_ai_agent.storage.db import get_session

    @asynccontextmanager
    async def f():
        async with get_session(str(db_path)) as s:
            yield s

    return f()
