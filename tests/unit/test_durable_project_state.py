"""Unit contract tests for the durable Studio project/revision lifecycle.

These pin the source-of-truth rules directly against a real SQLite database:

* a revision id is the canonical content address of the state bytes, so the
  same state always maps to the same id and history cannot be rewritten;
* a revision row is immutable (a re-commit of identical state is idempotent);
* the head is the single mutable pointer, advanced by a compare-and-swap that
  fails closed on a stale token;
* a revision is only reachable through the project that owns it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.project_state.models import (
    CrossProjectAccess,
    RevisionConflict,
    RevisionNotFound,
)
from nexus_ai_agent.creative.project_state.store import SqlProjectRevisionStore
from nexus_ai_agent.creative.studio.models import (
    Timeline,
    compute_state_hash,
    new_project,
)
from nexus_ai_agent.creative.studio.persistence import canonical_state_bytes, content_address
from nexus_ai_agent.storage import models as orm
from nexus_ai_agent.storage.db import get_session

PROJECT_A = "proj-alpha"
PROJECT_B = "proj-beta"


def _session_factory(db_path: Path):
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with get_session(str(db_path)) as session:
            yield session

    return factory


def _store(tmp_path: Path) -> SqlProjectRevisionStore:
    return SqlProjectRevisionStore(_session_factory(tmp_path / "revisions.sqlite"))


def _genesis(name: str = "Alpha"):
    return new_project(PROJECT_A, name, Timeline(timeline_id="tl", duration_us=0))


def _advance(project, *, revision: int, name: str):
    nxt = project.model_copy(deep=True)
    nxt.state_revision = revision
    nxt.name = name
    nxt.state_hash = compute_state_hash(nxt)
    return nxt


def test_content_address_is_canonical_and_deterministic() -> None:
    project = _genesis()
    a = canonical_state_bytes(project)
    b = canonical_state_bytes(project.model_copy(deep=True))
    assert a == b
    assert content_address(a) == content_address(b)
    assert content_address(a).startswith("sha256:")
    # A different state is a different address.
    other = _advance(project, revision=1, name="Beta")
    assert content_address(canonical_state_bytes(other)) != content_address(a)


async def test_genesis_commit_sets_head_and_is_content_addressed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    project = _genesis()
    record = await store.commit(
        project, authorized_project_id=PROJECT_A, expected_parent_revision_id=None
    )
    assert record.parent_revision_id is None
    assert record.state_revision == 0
    assert record.state_hash == project.state_hash
    assert record.revision_id == content_address(canonical_state_bytes(project))
    head = await store.load_head(PROJECT_A)
    assert head is not None and head.revision_id == record.revision_id


async def test_commit_requires_an_explicit_cas_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RevisionConflict):
        await store.commit(_genesis(), authorized_project_id=PROJECT_A)


async def test_child_revision_records_parent_lineage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    genesis = await store.commit(
        _genesis(), authorized_project_id=PROJECT_A, expected_parent_revision_id=None
    )
    child = await store.commit(
        _advance(_genesis(), revision=1, name="Alpha2"),
        authorized_project_id=PROJECT_A,
        expected_parent_revision_id=genesis.revision_id,
    )
    assert child.parent_revision_id == genesis.revision_id
    assert child.state_revision == 1


async def test_revision_row_is_immutable_under_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    genesis = await store.commit(
        _genesis(), authorized_project_id=PROJECT_A, expected_parent_revision_id=None
    )
    replay = await store.commit(
        _genesis(), authorized_project_id=PROJECT_A, expected_parent_revision_id=None
    )
    # Same content address, same parent (None) — history was not rewritten.
    assert replay.revision_id == genesis.revision_id
    assert replay.parent_revision_id is None
    assert replay.created_at == genesis.created_at


async def test_idempotent_replay_of_the_head_is_a_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    genesis = await store.commit(
        _genesis(), authorized_project_id=PROJECT_A, expected_parent_revision_id=None
    )
    child_state = _advance(_genesis(), revision=1, name="Alpha2")
    child = await store.commit(
        child_state,
        authorized_project_id=PROJECT_A,
        expected_parent_revision_id=genesis.revision_id,
    )
    # Re-committing the same accepted state (even with a stale token) is a
    # no-op success, never a conflict and never a rewrite.
    again = await store.commit(
        child_state,
        authorized_project_id=PROJECT_A,
        expected_parent_revision_id=genesis.revision_id,
    )
    assert again.revision_id == child.revision_id
    assert again.created_at == child.created_at


async def test_stale_cas_token_is_refused_and_head_is_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    genesis = await store.commit(
        _genesis(), authorized_project_id=PROJECT_A, expected_parent_revision_id=None
    )
    child_state = _advance(_genesis(), revision=1, name="Alpha2")
    await store.commit(
        child_state,
        authorized_project_id=PROJECT_A,
        expected_parent_revision_id=genesis.revision_id,
    )
    # A second writer that still believes genesis is current must be refused.
    loser_state = _advance(_genesis(), revision=1, name="Alpha-Rogue")
    with pytest.raises(RevisionConflict):
        await store.commit(
            loser_state,
            authorized_project_id=PROJECT_A,
            expected_parent_revision_id=genesis.revision_id,
        )
    head = await store.load_head(PROJECT_A)
    assert head is not None and head.revision_id == content_address(
        canonical_state_bytes(child_state)
    )


async def test_second_genesis_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.commit(
        _genesis(), authorized_project_id=PROJECT_A, expected_parent_revision_id=None
    )
    with pytest.raises(RevisionConflict):
        await store.commit(
            _genesis(name="Other"),
            authorized_project_id=PROJECT_A,
            expected_parent_revision_id=None,
        )


async def test_cross_project_commit_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(CrossProjectAccess):
        await store.commit(
            _genesis(),
            authorized_project_id=PROJECT_B,
            expected_parent_revision_id=None,
        )


async def test_cross_project_read_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    genesis = await store.commit(
        _genesis(), authorized_project_id=PROJECT_A, expected_parent_revision_id=None
    )
    with pytest.raises(CrossProjectAccess):
        await store.load_revision(PROJECT_B, genesis.revision_id)


async def test_unknown_revision_is_typed_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RevisionNotFound):
        await store.load_revision(PROJECT_A, "sha256:" + "0" * 64)


async def test_load_head_is_none_for_an_unknown_project(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert await store.load_head("never-committed") is None


async def test_commit_creates_the_canonical_project_identity_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.commit(
        _genesis(), authorized_project_id=PROJECT_A, expected_parent_revision_id=None
    )
    async with _session_factory(tmp_path / "revisions.sqlite")() as session:
        assert await session.get(orm.AppProject, PROJECT_A) is not None
