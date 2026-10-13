"""Deterministic lost-race tests for the ingest compare-and-set (task-266, D-0026).

The concurrent-replay tests exercise real interleavings but depend on timing.  Here
the race is *forced*: just before a session's ``complete_ingest`` runs, a rival
session completes the very same ingest and commits.  The session under test must
then lose the compare-and-set, discard its own writes, and serve the winner.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets import repository as repo_module
from nexus_ai_agent.creative.assets.authz import GrantTableAuthorizer, StudioAuthorizationSeam
from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import IngestAlreadyResolved
from nexus_ai_agent.creative.assets.ingestion import publish_and_commit
from nexus_ai_agent.creative.assets.models import IngestRequest, IngestState, MediaKind
from nexus_ai_agent.creative.assets.service import AssetLibraryService
from nexus_ai_agent.storage import models as orm
from nexus_ai_agent.storage.db import get_session

PROJECT = "project-alpha"
ACTOR = "user-1"
FIRST = b"\x89PNG\r\n\x1a\n" + bytes(range(96))
SECOND = b"\x89PNG\r\n\x1a\n" + bytes(range(96, 192))


def _service(tmp_path: Path, *, fault: str | None = None) -> AssetLibraryService:
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with get_session(str(tmp_path / "library.sqlite")) as session:
            yield session

    def hook(point: str) -> None:
        if fault is not None and point == fault:
            raise RuntimeError(f"injected crash at {point}")

    return AssetLibraryService(
        blob_store=LocalContentAddressedBlobStore(tmp_path / "blobs"),
        authorizer=StudioAuthorizationSeam(
            GrantTableAuthorizer({(ACTOR, PROJECT): frozenset({"project:read", "project:write"})})
        ),
        session_factory=factory,
        max_bytes=1 << 20,
        fault_hook=hook,
    )


def _ingest(service: AssetLibraryService, data: bytes, key: str):
    return service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT,
        request=IngestRequest(idempotency_key=key, original_filename="pic.png"),
        stream=io.BytesIO(data),
    )


async def _crash_after_stage(tmp_path: Path, data: bytes, key: str) -> None:
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(_service(tmp_path, fault="after_stage"), data, key)


async def _rows(tmp_path: Path) -> list[orm.AssetIngest]:
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        return list((await session.execute(select(orm.AssetIngest))).scalars().all())


async def _count(tmp_path: Path, model: type) -> int:
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


def _staging_files(tmp_path: Path) -> list[Path]:
    root = tmp_path / "blobs" / "staging"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _rival_completes_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: bytes, key: str, *, at: str = "stage_to"
) -> list[int]:
    """Make a rival finish the same ingest before the session under test writes.

    ``at`` names a *file-level* blob-store step (``stage_to`` for a replay, ``store``
    for recovery's publish).  The rival runs inside that step, i.e. before the session
    under test has issued any database write, so SQLite's single write lock is free
    and the rival simply commits.  The session under test then proceeds from a view
    taken before that commit, which is the stale view a real concurrent retry has.
    """
    calls: list[int] = []
    real = getattr(LocalContentAddressedBlobStore, at)

    def rigged(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            monkeypatch.setattr(LocalContentAddressedBlobStore, at, real)
            _run_rival(tmp_path, data, key)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(LocalContentAddressedBlobStore, at, rigged)
    return calls


def _run_rival(tmp_path: Path, data: bytes, key: str) -> None:
    """Run the rival ingest to completion on its own thread/event loop."""
    import asyncio
    import threading

    errors: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(_ingest(_service(tmp_path), data, key))
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "the rival never finished"
    if errors:
        raise errors[0]


@pytest.mark.asyncio
async def test_a_loser_in_recovery_discards_its_graph_and_serves_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _crash_after_stage(tmp_path, FIRST, "k1")
    ingest_id = (await _rows(tmp_path))[0].ingest_id
    calls = _rival_completes_first(monkeypatch, tmp_path, FIRST, "k1", at="store")

    report = await _service(tmp_path).recover_ingest(ingest_id)

    assert calls, "the rigged compare-and-set was never reached"
    assert report.state is IngestState.READY
    assert await _count(tmp_path, orm.Asset) == 1  # the loser added no second asset
    assert await _count(tmp_path, orm.AssetVersion) == 1
    assert await _count(tmp_path, orm.ProjectAssetLink) == 1
    (row,) = await _rows(tmp_path)
    assert row.state == "READY"
    assert row.content_sha256 == hashlib.sha256(FIRST).hexdigest()


@pytest.mark.asyncio
async def test_publish_and_commit_raises_when_it_loses_the_compare_and_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path / "blobs")
    key = store.new_staging_key()
    sha, size = store.stage_to(key, io.BytesIO(FIRST), max_bytes=1 << 20)

    async def lose(self: Any, ingest_id: str, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(repo_module.SqlAssetRepository, "complete_ingest", lose)
    async with get_session(str(tmp_path / "x.sqlite")) as session:
        repo = repo_module.SqlAssetRepository(session)
        with pytest.raises(IngestAlreadyResolved):
            await publish_and_commit(
                repo,
                store,
                ingest_id="i1",
                project_id="p",
                staging_key=key,
                content_sha256=sha,
                size_bytes=size,
                media_kind=MediaKind.IMAGE,
                media_type="image/png",
            )


@pytest.mark.asyncio
async def test_an_adopted_replay_that_loses_the_race_is_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Original staged file lost -> replay adopted -> a rival completes first -> no orphan."""
    await _crash_after_stage(tmp_path, FIRST, "k1")
    (row,) = await _rows(tmp_path)
    assert row.staging_key
    (tmp_path / "blobs" / row.staging_key).unlink()  # the original is unrecoverable
    calls = _rival_completes_first(monkeypatch, tmp_path, FIRST, "k1")

    result = await _ingest(_service(tmp_path), FIRST, "k1")

    assert calls
    assert result.state is IngestState.READY
    assert await _count(tmp_path, orm.Asset) == 1
    assert _staging_files(tmp_path) == []  # the adopted replay file did not leak


@pytest.mark.asyncio
async def test_recover_all_keeps_other_ingests_when_one_loses_a_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _crash_after_stage(tmp_path, FIRST, "k1")
    await _crash_after_stage(tmp_path, SECOND, "k2")
    calls = _rival_completes_first(monkeypatch, tmp_path, FIRST, "k1", at="store")

    reports = await _service(tmp_path).recover_all()

    assert calls
    assert len(reports) == 2
    assert all(r.state is IngestState.READY for r in reports), reports
    assert sorted(r.state for r in await _rows(tmp_path)) == ["READY", "READY"]
    assert await _count(tmp_path, orm.Asset) == 2  # one per distinct ingest, none duplicated
