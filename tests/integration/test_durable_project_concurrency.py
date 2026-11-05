"""Integration: real interleaving and atomicity for project revisions.

Two independent stores over the same database race the compare-and-swap; exactly
one advances the head and the loser is refused with :class:`RevisionConflict`.
A crash injected between the revision insert and the head advance proves the
whole commit is atomic: neither the revision row nor the head survives.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.project_state.models import RevisionConflict
from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore
from nexus_ai_agent.creative.studio.models import (
    Timeline,
    compute_state_hash,
    new_project,
)
from nexus_ai_agent.creative.studio.persistence import canonical_state_bytes, content_address
from nexus_ai_agent.storage import models as orm
from nexus_ai_agent.storage.db import get_session

pytestmark = [pytest.mark.integration]

PROJECT = "proj-race"


def _store(db_path: Path) -> SqlProjectRevisionStore:
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with get_session(str(db_path)) as session:
            yield session

    return SqlProjectRevisionStore(factory)


def _genesis():
    return new_project(PROJECT, "Alpha", Timeline(timeline_id="tl", duration_us=0))


def _advance(project, *, revision: int, name: str):
    nxt = project.model_copy(deep=True)
    nxt.state_revision = revision
    nxt.name = name
    nxt.state_hash = compute_state_hash(nxt)
    return nxt


async def test_concurrent_children_exactly_one_wins(tmp_path: Path) -> None:
    db = tmp_path / "race.sqlite"
    store = _store(db)
    genesis = await store.commit(
        _genesis(), authorized_project_id=PROJECT, expected_parent_revision_id=None
    )

    left = _advance(_genesis(), revision=1, name="Left")
    right = _advance(_genesis(), revision=1, name="Right")

    async def writer(state):
        try:
            record = await store.commit(
                state,
                authorized_project_id=PROJECT,
                expected_parent_revision_id=genesis.revision_id,
            )
            return ("ok", record.revision_id)
        except RevisionConflict:
            return ("conflict", None)

    results = await asyncio.gather(writer(left), writer(right))
    outcomes = sorted(r[0] for r in results)
    assert outcomes == ["conflict", "ok"], results

    winner_id = next(r[1] for r in results if r[0] == "ok")
    head = await store.load_head(PROJECT)
    assert head is not None and head.revision_id == winner_id


async def test_concurrent_genesis_exactly_one_wins(tmp_path: Path) -> None:
    db = tmp_path / "genesis.sqlite"
    store = _store(db)

    # Distinct names give distinct states, so this is a real race for the head.
    left = _genesis()
    left.name = "Left"
    left.state_hash = compute_state_hash(left)
    right = _genesis()
    right.name = "Right"
    right.state_hash = compute_state_hash(right)

    async def writer_state(state):
        try:
            record = await store.commit(
                state, authorized_project_id=PROJECT, expected_parent_revision_id=None
            )
            return ("ok", record.revision_id)
        except RevisionConflict:
            return ("conflict", None)

    results = await asyncio.gather(writer_state(left), writer_state(right))
    assert sorted(r[0] for r in results) == ["conflict", "ok"], results


async def test_crash_between_insert_and_head_advance_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "atomic.sqlite"
    store = _store(db)
    project = _genesis()
    revision_id = content_address(canonical_state_bytes(project))

    async def boom(*args, **kwargs):
        raise RuntimeError("injected crash before the head advance")

    monkeypatch.setattr(store, "_advance_head", boom)
    with pytest.raises(RuntimeError):
        await store.commit(project, authorized_project_id=PROJECT, expected_parent_revision_id=None)

    # The transaction rolled back: no head and no revision row survived.
    assert await store.load_head(PROJECT) is None
    async with get_session(str(db)) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(orm.ProjectRevision)
                .where(orm.ProjectRevision.revision_id == revision_id)
            )
        ).scalar_one()
    assert count == 0
