"""Integration tests: durable ingest recovery across a simulated crash.

Crashes are *injected* at exact boundaries through the service's fault hook,
never faked with a comment.  Each test then rebuilds the service (a restart)
over the same database file and blob root and asserts the recovery outcome.

Invariant under test: filesystem existence is **not** authoritative completion,
and recovery never invents success.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai_agent.creative.assets.authz import GrantTableAuthorizer, StudioAuthorizationSeam
from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import IngestNotFound
from nexus_ai_agent.creative.assets.ingestion import RecoveryOutcome
from nexus_ai_agent.creative.assets.models import (
    IngestRecord,
    IngestRequest,
    IngestResult,
    IngestState,
)
from nexus_ai_agent.creative.assets.service import AssetLibraryService
from nexus_ai_agent.storage import models as orm
from nexus_ai_agent.storage.db import get_session

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(96))
PROJECT = "project-alpha"
ACTOR = "user-1"


def _authorizer() -> StudioAuthorizationSeam:
    return StudioAuthorizationSeam(
        GrantTableAuthorizer({(ACTOR, PROJECT): frozenset({"project:read", "project:write"})})
    )


def _session_factory(db_path: Path) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with get_session(str(db_path)) as session:
            yield session

    return factory


def _service(tmp_path: Path, *, fault: str | None = None) -> AssetLibraryService:
    def hook(point: str) -> None:
        if fault is not None and point == fault:
            raise RuntimeError(f"injected crash at {point}")

    return AssetLibraryService(
        blob_store=LocalContentAddressedBlobStore(tmp_path / "blobs"),
        authorizer=_authorizer(),
        session_factory=_session_factory(tmp_path / "library.sqlite"),
        fault_hook=hook,
    )


async def _only_ingest(tmp_path: Path) -> orm.AssetIngest:
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        table = cast(Table, orm.AssetIngest.__table__)  # type: ignore[attr-defined]
        row = (
            (await session.execute(select(orm.AssetIngest).order_by(table.c.created_at)))
            .scalars()
            .one()
        )
        return cast(orm.AssetIngest, row)


async def _counts(tmp_path: Path) -> tuple[int, int, int]:
    async with get_session(str(tmp_path / "library.sqlite")) as session:
        assets = (await session.execute(select(func.count()).select_from(orm.Asset))).scalar_one()
        versions = (
            await session.execute(select(func.count()).select_from(orm.AssetVersion))
        ).scalar_one()
        links = (
            await session.execute(select(func.count()).select_from(orm.ProjectAssetLink))
        ).scalar_one()
        return assets, versions, links


def _ingest(service: AssetLibraryService, key: str = "k") -> Coroutine[Any, Any, IngestResult]:
    return service.ingest(
        actor_kind="user",
        actor_id=ACTOR,
        project_id=PROJECT,
        request=IngestRequest(idempotency_key=key, original_filename="pic.png"),
        stream=io.BytesIO(PNG),
    )


@pytest.mark.asyncio
async def test_crash_after_ingest_row_recovers_to_failed_not_success(tmp_path: Path) -> None:
    """C1/C2: a durable row exists but no bytes were staged — never a false READY."""
    service = _service(tmp_path, fault="after_ingest_row")
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(service)

    row = await _only_ingest(tmp_path)
    assert row.state == IngestState.STAGING.value
    assert row.staging_key  # recorded before the crash

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(row.ingest_id)
    assert report.state is IngestState.FAILED
    assert report.outcome is RecoveryOutcome.FAILED
    assert await _counts(tmp_path) == (0, 0, 0)  # no fabricated asset


@pytest.mark.asyncio
async def test_crash_after_stage_recovers_to_ready(tmp_path: Path) -> None:
    """C2: staged bytes are recoverable — the single completion path finalises them."""
    service = _service(tmp_path, fault="after_stage")
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(service)

    row = await _only_ingest(tmp_path)
    assert row.state == IngestState.STAGING.value
    assert row.content_sha256 is None  # Tx2 had not committed yet

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(row.ingest_id)
    assert report.state is IngestState.READY
    assert report.outcome is RecoveryOutcome.COMPLETED
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_crash_after_publish_before_commit_recovers_to_ready(tmp_path: Path) -> None:
    """C4: the physical artifact exists but is not authoritative until the commit."""
    service = _service(tmp_path, fault="after_publish_before_commit")
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(service)

    # The blob is on disk, but the DB has no asset yet: existence != authority.
    assert await _counts(tmp_path) == (0, 0, 0)
    row = await _only_ingest(tmp_path)
    assert row.state == IngestState.STAGING.value

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(row.ingest_id)
    assert report.state is IngestState.READY
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_crash_after_commit_is_already_ready_and_not_re_executed(tmp_path: Path) -> None:
    """C5: a completed ingest is observed COMPLETED and never duplicated."""
    service = _service(tmp_path)
    result = await _ingest(service)
    assert result.state is IngestState.READY

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(result.ingest_id)
    assert report.outcome is RecoveryOutcome.ALREADY_READY
    assert await _counts(tmp_path) == (1, 1, 1)

    # A retry with the same key is served from the durable result.
    again = await _ingest(restarted)
    assert again.asset_id == result.asset_id
    assert again.reused_result is True
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_ready_row_with_missing_blob_is_downgraded_to_failed(tmp_path: Path) -> None:
    """A READY row whose bytes vanished must never stay falsely READY."""
    service = _service(tmp_path)
    result = await _ingest(service)

    # Remove the physical blob behind the metadata's back.
    blob_dir = tmp_path / "blobs" / "blobs"
    for f in blob_dir.rglob("*"):
        if f.is_file():
            f.unlink()

    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(result.ingest_id)
    assert report.state is IngestState.FAILED
    assert report.outcome is RecoveryOutcome.MISSING_BLOB


@pytest.mark.asyncio
async def test_unknown_ingest_is_not_fabricated(tmp_path: Path) -> None:
    """C8: recovery of an unknown identity refuses rather than inventing success."""
    service = _service(tmp_path)
    with pytest.raises(IngestNotFound):
        await service.recover_ingest("does-not-exist")


@pytest.mark.asyncio
async def test_recover_all_only_touches_pending_rows(tmp_path: Path) -> None:
    service = _service(tmp_path, fault="after_stage")
    with pytest.raises(RuntimeError):
        await _ingest(service, key="pending")

    # A second, fully-completed ingest must be left untouched by recover_all.
    good = _service(tmp_path)
    done = await _ingest(good, key="done")

    restarted = _service(tmp_path)
    reports = await restarted.recover_all()
    assert [r.outcome for r in reports] == [RecoveryOutcome.COMPLETED]
    assert await _counts(tmp_path) == (2, 2, 2)
    # The previously completed ingest is still exactly one asset.
    identity, _ = await restarted.get_asset(
        actor_kind="user", actor_id=ACTOR, project_id=PROJECT, asset_id=done.asset_id
    )
    assert identity.asset_id == done.asset_id


@pytest.mark.asyncio
async def test_retry_of_interrupted_ingest_completes_it(tmp_path: Path) -> None:
    """A retry with the same key recovers the interrupted ingest rather than duplicating."""
    service = _service(tmp_path, fault="after_stage")
    with pytest.raises(RuntimeError):
        await _ingest(service, key="retry")

    restarted = _service(tmp_path)
    result = await _ingest(restarted, key="retry")
    assert result.state is IngestState.READY
    assert result.reused_result is True
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_cancelled_worker_finishes_then_restart_recovers_without_false_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    service = _service(tmp_path)
    store = service._blob_store
    original_store = store.store
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_publish(staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(5):
                raise TimeoutError("publication worker was not released")
            return original_store(staging_key, blob_key)
        finally:
            finished.set()

    monkeypatch.setattr(store, "store", blocked_publish)
    task = asyncio.create_task(_ingest(service))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = await _only_ingest(tmp_path)
        assert row.state == IngestState.STAGING.value
        assert await _counts(tmp_path) == (0, 0, 0)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)

    assert row.content_sha256 is not None and row.size_bytes is not None
    assert store.verify(
        store.blob_key_for(row.content_sha256),
        sha256=row.content_sha256,
        size_bytes=row.size_bytes,
    )
    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(row.ingest_id)
    assert report.outcome is RecoveryOutcome.COMPLETED
    assert await _counts(tmp_path) == (1, 1, 1)
    repeated = await restarted.recover_ingest(row.ingest_id)
    assert repeated.outcome is RecoveryOutcome.ALREADY_READY
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_recovery_defers_cancelled_inflight_worker_across_store_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    service = _service(tmp_path)
    store = service._blob_store
    original = store.store
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def paused(staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(5):
                raise TimeoutError("worker was not released")
            return original(staging_key, blob_key)
        finally:
            finished.set()

    monkeypatch.setattr(store, "store", paused)
    task = asyncio.create_task(_ingest(service, "in-flight"))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = await _only_ingest(tmp_path)
        assert row.staging_key is not None
        staging = tmp_path / "blobs" / row.staging_key
        restarted = _service(tmp_path)
        report = await restarted.recover_ingest(row.ingest_id)
        assert report.outcome is RecoveryOutcome.STILL_STAGING
        assert report.state is IngestState.STAGING
        assert staging.is_file()
        assert await _counts(tmp_path) == (0, 0, 0)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    report = await restarted.recover_ingest(row.ingest_id)
    assert report.outcome is RecoveryOutcome.COMPLETED
    assert await _counts(tmp_path) == (1, 1, 1)
    assert (await restarted.recover_ingest(row.ingest_id)).outcome is RecoveryOutcome.ALREADY_READY


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["same_size", "different_size", "partial_identity"])
async def test_recovery_refuses_changed_staging_after_tx2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """A durable measured identity must not be replaced by altered disk bytes."""
    service = _service(tmp_path)

    async def crash_before_publish(staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        raise RuntimeError("injected crash after tx2")

    monkeypatch.setattr(service._blob_store, "publish_from_staging_async", crash_before_publish)
    with pytest.raises(RuntimeError, match="after tx2"):
        await _ingest(service)
    row = await _only_ingest(tmp_path)
    assert row.state == IngestState.STAGING.value
    assert row.staging_key and row.content_sha256 and row.size_bytes == len(PNG)
    stage = tmp_path / "blobs" / row.staging_key
    if mutation == "same_size":
        stage.write_bytes(PNG[:-1] + bytes([PNG[-1] ^ 1]))
    elif mutation == "different_size":
        stage.write_bytes(PNG + b"extra")
    else:
        async with get_session(str(tmp_path / "library.sqlite")) as session:
            stored = await session.get(orm.AssetIngest, row.ingest_id)
            assert stored is not None
            stored.size_bytes = None
            await session.commit()
    expected = stage.read_bytes()
    restarted = _service(tmp_path)
    report = await restarted.recover_ingest(row.ingest_id)
    assert report.state is IngestState.FAILED
    assert report.outcome is RecoveryOutcome.FAILED
    assert (await _only_ingest(tmp_path)).error_code == "staging_integrity_mismatch"
    assert await _counts(tmp_path) == (0, 0, 0)
    assert stage.read_bytes() == expected  # kept for inspection, not silently deleted
    assert not any((tmp_path / "blobs" / "blobs").rglob("*"))


@pytest.mark.asyncio
async def test_ingest_transaction_fences_other_process_until_sigkill(tmp_path: Path) -> None:
    import asyncio
    import os
    import signal
    import subprocess
    import sys

    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    program = r"""
import asyncio, io, sys
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, cast
from nexus_ai_agent.creative.assets.authz import GrantTableAuthorizer, StudioAuthorizationSeam
from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.models import IngestRequest
from nexus_ai_agent.creative.assets.service import AssetLibraryService
from nexus_ai_agent.storage.db import get_session
root = Path(sys.argv[1])
@asynccontextmanager
async def factory():
    async with get_session(str(root / "library.sqlite")) as session:
        yield session
def hook(point):
    if point == "after_stage":
        print("staged", flush=True)
        sys.stdin.readline()
auth = StudioAuthorizationSeam(
    GrantTableAuthorizer({("user-1", "project-alpha"): frozenset({"project:write"})})
)
service = AssetLibraryService(
    blob_store=LocalContentAddressedBlobStore(root / "blobs"),
    authorizer=auth, session_factory=factory, fault_hook=hook,
)
asyncio.run(service.ingest(
    actor_kind="user", actor_id="user-1", project_id="project-alpha",
    request=IngestRequest(idempotency_key="k"),
    stream=io.BytesIO(b"\x89PNG\r\n\x1a\n" + bytes(range(96))),
))
"""
    process = subprocess.Popen(  # noqa: S603 - interpreter and fixed test program
        [sys.executable, "-c", program, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10) == "staged\n"
        row = await _only_ingest(tmp_path)
        assert row.staging_key is not None
        staging = tmp_path / "blobs" / row.staging_key
        assert staging.read_bytes() == PNG

        def attempt_exclusive() -> None:
            with asset_admission_lock(tmp_path / "blobs", exclusive=True, timeout_seconds=0.1):
                pytest.fail("maintenance acquired the lock during ingest")

        with pytest.raises(TimeoutError):
            await asyncio.to_thread(attempt_exclusive)
        assert staging.read_bytes() == PNG
        os.kill(process.pid, signal.SIGKILL)
        assert await asyncio.to_thread(process.wait, 5) == -signal.SIGKILL
        with asset_admission_lock(tmp_path / "blobs", exclusive=True, timeout_seconds=1):
            pass
        report = await _service(tmp_path).recover_ingest(row.ingest_id)
        assert report.outcome is RecoveryOutcome.COMPLETED
        assert await _counts(tmp_path) == (1, 1, 1)
    finally:
        if process.poll() is None:
            process.kill()
        await asyncio.to_thread(process.communicate, timeout=5)


@pytest.mark.asyncio
async def test_recover_all_reuses_exclusive_lease_without_deadlock(tmp_path: Path) -> None:
    import asyncio

    from nexus_ai_agent.creative.assets.admission import async_asset_admission_lock

    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(_service(tmp_path, fault="after_stage"))
    restarted = _service(tmp_path)
    async with async_asset_admission_lock(tmp_path / "blobs", exclusive=True) as lease:
        reports = await asyncio.wait_for(restarted.recover_all(lease=lease), timeout=5)
        assert [report.outcome for report in reports] == [RecoveryOutcome.COMPLETED]
        assert lease.is_active
        assert await _counts(tmp_path) == (1, 1, 1)
    assert not lease.is_active


@pytest.mark.asyncio
async def test_cancelled_exclusive_recovery_keeps_kernel_lock_until_worker_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading

    from nexus_ai_agent.creative.assets.admission import (
        asset_admission_lock,
        async_asset_admission_lock,
    )

    interrupted = _service(tmp_path)

    async def crash_after_tx2(staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        raise RuntimeError("injected crash after tx2")

    monkeypatch.setattr(interrupted._blob_store, "publish_from_staging_async", crash_after_tx2)
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(interrupted)
    row = await _only_ingest(tmp_path)
    assert row.staging_key and row.content_sha256
    restarted = _service(tmp_path)
    store = restarted._blob_store
    assert isinstance(store, LocalContentAddressedBlobStore)
    original = store._store_admitted
    started, release = threading.Event(), threading.Event()

    def slow(staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        started.set()
        if not release.wait(5):
            raise TimeoutError("recovery worker not released")
        return original(staging_key, blob_key)

    monkeypatch.setattr(store, "_store_admitted", slow)

    async def recover() -> None:
        async with async_asset_admission_lock(tmp_path / "blobs", exclusive=True) as lease:
            await restarted.recover_all(lease=lease)

    task = asyncio.create_task(recover())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        import nexus_ai_agent.creative.assets.blob_store as blob_module

        with blob_module._IN_FLIGHT_LOCK:
            finished = blob_module._IN_FLIGHT_PUBLICATIONS[(store._fs.root, row.staging_key)][0][0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        def attempt_exclusive() -> None:
            with asset_admission_lock(tmp_path / "blobs", exclusive=True, timeout_seconds=0.1):
                pytest.fail("cancelled worker still owns the lock")

        with pytest.raises(TimeoutError):
            await asyncio.to_thread(attempt_exclusive)
        assert (tmp_path / "blobs" / row.staging_key).exists()
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    with asset_admission_lock(tmp_path / "blobs", exclusive=True, timeout_seconds=1):
        pass
    report = await _service(tmp_path).recover_ingest(row.ingest_id)
    assert report.outcome is RecoveryOutcome.COMPLETED
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_startup_recovers_tx2_and_sweeps_marked_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from nexus_ai_agent.creative.assets.service import async_reconcile_asset_library_startup

    interrupted = _service(tmp_path)

    async def crash(staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        raise RuntimeError("injected crash")

    monkeypatch.setattr(interrupted._blob_store, "publish_from_staging_async", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        await _ingest(interrupted)
    restarted = _service(tmp_path)
    store = restarted._blob_store
    assert isinstance(store, LocalContentAddressedBlobStore)
    orphan = store.new_staging_key()
    store.stage_to(orphan, io.BytesIO(PNG), max_bytes=1024)
    store._record_cleanup_marker(orphan)
    report = await asyncio.wait_for(async_reconcile_asset_library_startup(restarted), 5)
    assert len(report.recovered_asset_ids) == 1
    assert report.swept_staging_keys == (orphan,)
    assert report.active_staging_keys_count == 0
    assert not (tmp_path / "blobs" / orphan).exists()
    assert await _counts(tmp_path) == (1, 1, 1)


@pytest.mark.asyncio
async def test_startup_snapshot_failure_never_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nexus_ai_agent.creative.assets.repository import SqlAssetRepository
    from nexus_ai_agent.creative.assets.service import async_reconcile_asset_library_startup

    service = _service(tmp_path)
    store = service._blob_store
    assert isinstance(store, LocalContentAddressedBlobStore)
    orphan = store.new_staging_key()
    store.stage_to(orphan, io.BytesIO(PNG), max_bytes=1024)
    store._record_cleanup_marker(orphan)
    original = SqlAssetRepository.list_pending_ingests
    calls = 0

    async def fail_snapshot(repo: SqlAssetRepository) -> list[IngestRecord]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("snapshot failure")
        return await original(repo)

    monkeypatch.setattr(SqlAssetRepository, "list_pending_ingests", fail_snapshot)
    with pytest.raises(RuntimeError, match="snapshot failure"):
        await async_reconcile_asset_library_startup(service)
    assert (tmp_path / "blobs" / orphan).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", ["service", "paths"])
async def test_api_lifespan_reconciles_real_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: str
) -> None:
    from types import SimpleNamespace

    from fastapi import FastAPI

    import nexus_ai_agent.api.app as api_module

    interrupted = _service(tmp_path)

    async def crash(staging_key: str, blob_key: str) -> tuple[str, int, bool]:
        raise RuntimeError("crash at tx2")

    monkeypatch.setattr(interrupted._blob_store, "publish_from_staging_async", crash)
    with pytest.raises(RuntimeError, match="crash at tx2"):
        await _ingest(interrupted)
    restarted = _service(tmp_path)
    store = restarted._blob_store
    assert isinstance(store, LocalContentAddressedBlobStore)
    orphan = store.new_staging_key()
    store.stage_to(orphan, io.BytesIO(PNG), max_bytes=1024)
    store._record_cleanup_marker(orphan)

    class Queue:
        async def acquire_ownership(self) -> None:
            pass

        async def resume_pending(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    monkeypatch.setattr(api_module, "get_creative_registry", lambda: SimpleNamespace(queue=Queue()))
    app = FastAPI()
    if configuration == "service":
        app.state.asset_library_service = restarted
    else:
        app.state.asset_library_root = tmp_path / "blobs"
        app.state.asset_library_db_path = tmp_path / "library.sqlite"
    async with api_module._api_lifespan(app):
        report = app.state.asset_library_startup_report
        assert len(report.recovered_asset_ids) == 1
        assert report.swept_staging_keys == (orphan,)
        assert report.active_staging_keys_count == 0
    assert await _counts(tmp_path) == (1, 1, 1)
    assert not (tmp_path / "blobs" / orphan).exists()


@pytest.mark.asyncio
async def test_path_configuration_rejects_missing_or_wrong_database_before_sweep(
    tmp_path: Path,
) -> None:
    import sqlite3

    from nexus_ai_agent.creative.assets.service import reconcile_configured_asset_library

    store = LocalContentAddressedBlobStore(tmp_path / "blobs")
    orphan = store.new_staging_key()
    store.stage_to(orphan, io.BytesIO(PNG), max_bytes=1024)
    store._record_cleanup_marker(orphan)
    with pytest.raises(ValueError, match="both be configured"):
        await reconcile_configured_asset_library(root=tmp_path / "blobs")
    with pytest.raises(OSError):
        await reconcile_configured_asset_library(
            root=tmp_path / "blobs", db_path=tmp_path / "missing"
        )
    database = tmp_path / "wrong.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ValueError, match="ingest authority"):
        await reconcile_configured_asset_library(root=tmp_path / "blobs", db_path=database)
    assert (tmp_path / "blobs" / orphan).exists()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='assetingest'").fetchone()
            is None
        )
