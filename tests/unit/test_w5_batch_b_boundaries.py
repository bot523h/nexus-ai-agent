"""W5 asset boundary and interrupted-ingest state machine tests."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import (
    BlobStoreError,
    ContentIntegrityError,
    IngestAlreadyResolved,
    RecoveryError,
)
from nexus_ai_agent.creative.assets.ingestion import (
    RecoveryOutcome,
    publish_and_commit,
    reconcile_ingest,
)
from nexus_ai_agent.creative.assets.models import IngestRecord, IngestState, MediaKind
from nexus_ai_agent.creative.assets.service import (
    AssetLibraryService,
    _result_from,
    async_reconcile_asset_library_startup,
    reconcile_asset_library_startup,
    reconcile_configured_asset_library,
)


def record(**changes: object) -> IngestRecord:
    now = datetime.now(timezone.utc)
    values = dict(
        ingest_id="ingest",
        idempotency_key="key",
        project_id="project",
        request_fingerprint="fingerprint",
        state=IngestState.STAGING,
        created_at=now,
        updated_at=now,
    )
    values.update(changes)
    return IngestRecord(**values)


def repo_mock() -> Mock:
    repo = Mock()
    repo.mark_failed = AsyncMock(return_value=True)
    repo.complete_ingest = AsyncMock(return_value=True)
    repo.add_asset = AsyncMock()
    repo.add_version = AsyncMock()
    repo.add_link = AsyncMock()
    return repo


def store_mock() -> Mock:
    store = Mock()
    store.blob_key_for.return_value = "blob"
    store.verify.return_value = True
    store.wait_for_in_flight_publication = AsyncMock(return_value=True)
    store.stage_identity.return_value = ("a" * 64, 12)
    store.read_staging_header.return_value = b"not a media header"
    store.publish_from_staging_async = AsyncMock(return_value=("a" * 64, 12, False))
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("measured,verified", [(False, True), (True, False)])
async def test_publish_rejects_invalid_publication_before_metadata(
    measured: bool, verified: bool
) -> None:
    repo, store = repo_mock(), store_mock()
    if not measured:
        store.publish_from_staging_async.return_value = ("b" * 64, 12, False)
    store.verify.return_value = verified
    with pytest.raises(ContentIntegrityError):
        await publish_and_commit(
            repo,
            store,
            ingest_id="i",
            project_id="p",
            staging_key="s",
            content_sha256="a" * 64,
            size_bytes=12,
            media_kind=MediaKind.IMAGE,
            media_type="image/png",
        )
    repo.add_asset.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_failed_record_is_not_retried() -> None:
    repo, store = repo_mock(), store_mock()
    report = await reconcile_ingest(repo, store, record(state=IngestState.FAILED))
    assert report.outcome is RecoveryOutcome.FAILED
    repo.mark_failed.assert_not_awaited()
    store.wait_for_in_flight_publication.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["mismatch", "unsupported", "published", "missing", "lost"])
async def test_recovery_lost_races_do_not_report_success(case: str) -> None:
    repo, store = repo_mock(), store_mock()
    repo.mark_failed.return_value = False
    repo.complete_ingest.return_value = False
    if case == "mismatch":
        item = record(staging_key="stage", content_sha256="b" * 64, size_bytes=12)
    elif case == "unsupported":
        item = record(staging_key="stage")
    elif case in ("published", "missing"):
        item = record(content_sha256="a" * 64, size_bytes=12, media_type="image/png")
        store.verify.return_value = case == "published"
    else:
        item = record()
    with pytest.raises(IngestAlreadyResolved):
        await reconcile_ingest(repo, store, item)
    if case != "published":
        repo.add_asset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_identity", [False, True])
async def test_missing_recovery_returns_failure_without_inventing_asset(has_identity: bool) -> None:
    repo, store = repo_mock(), store_mock()
    store.verify.return_value = False
    item = (
        record(content_sha256="a" * 64, size_bytes=12, media_type="image/png")
        if has_identity
        else record()
    )
    report = await reconcile_ingest(repo, store, item)
    assert report.outcome is (
        RecoveryOutcome.MISSING_BLOB if has_identity else RecoveryOutcome.FAILED
    )
    repo.mark_failed.assert_awaited_once_with(
        "ingest", error_code="blob_missing" if has_identity else "staging_lost"
    )
    repo.add_asset.assert_not_awaited()


def test_ready_record_without_classification_is_rejected() -> None:
    with pytest.raises(RecoveryError, match="classification"):
        _result_from(record(state=IngestState.READY), reused=False)


@pytest.mark.asyncio
async def test_unconfigured_recovery_and_invalid_startup(tmp_path: Path) -> None:
    assert await reconcile_configured_asset_library() is None
    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    service = AssetLibraryService(blob_store=store, authorizer=Mock())
    for value in (0, True, -1):
        with pytest.raises(ValueError, match="positive integer"):
            await async_reconcile_asset_library_startup(service, max_sweep_items=value)
    service._blob_store = Mock()
    with pytest.raises(TypeError, match="local blob store"):
        await async_reconcile_asset_library_startup(service)
    with pytest.raises(RuntimeError, match="event loop"):
        reconcile_asset_library_startup(service)


def test_blob_invalid_cleanup_lease_and_missing_header(tmp_path: Path) -> None:
    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    with pytest.raises(PermissionError, match="exclusive"):
        store.reconcile_staging_cleanups(active_staging_keys=set(), admission_lease=object())
    with pytest.raises(BlobStoreError, match="missing or unreadable"):
        store.read_staging_header("staging/missing", 10)


@pytest.mark.asyncio
async def test_blob_invalid_pin_releases_publication_tracking(tmp_path: Path) -> None:
    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    with pytest.raises(PermissionError, match="invalid"):
        await store.publish_from_staging_async("staging/a", "blobs/b", admission_lease=object())
    assert "staging/a" not in store.in_flight_staging_keys()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "winner_state,storage_error,expect_raise",
    [
        (None, True, True),
        (IngestState.STAGING, True, True),
        (IngestState.READY, True, False),
        (IngestState.FAILED, False, False),
    ],
)
async def test_lost_reconciliation_rolls_back_and_checks_winner(
    monkeypatch: pytest.MonkeyPatch,
    winner_state: IngestState | None,
    storage_error: bool,
    expect_raise: bool,
) -> None:
    from nexus_ai_agent.creative.assets import service as module

    store = store_mock()
    service = AssetLibraryService(blob_store=store, authorizer=Mock())
    repo, session = repo_mock(), Mock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    repo.get_ingest_fresh = AsyncMock(
        return_value=record(state=winner_state) if winner_state else None
    )
    error = BlobStoreError("storage fault") if storage_error else IngestAlreadyResolved("ingest")
    monkeypatch.setattr(module, "reconcile_ingest", AsyncMock(side_effect=error))
    if expect_raise:
        with pytest.raises(BlobStoreError, match="storage fault"):
            await service._reconcile_committing(
                session, repo, record(), owned_staging_key="staging/replay"
            )
        store.remove_staging.assert_not_called()
    else:
        result = await service._reconcile_committing(
            session, repo, record(), owned_staging_key="staging/replay"
        )
        assert result.outcome is (
            RecoveryOutcome.ALREADY_READY
            if winner_state is IngestState.READY
            else RecoveryOutcome.FAILED
        )
        store.remove_staging.assert_called_once_with("staging/replay", admission_lease=None)
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fresh,exception",
    [
        (None, "IngestNotFound"),
        (record(request_fingerprint="other"), "IdempotencyConflict"),
        (record(state=IngestState.FAILED), "RecoveryError"),
    ],
)
async def test_replay_checks_fresh_snapshot(fresh: IngestRecord | None, exception: str) -> None:
    import io

    from nexus_ai_agent.creative.assets import errors

    service = AssetLibraryService(blob_store=store_mock(), authorizer=Mock())
    repo = repo_mock()
    repo.get_ingest_fresh = AsyncMock(return_value=fresh)
    session = Mock(bind=None)
    with pytest.raises(getattr(errors, exception)):
        await service._resolve_existing(repo, session, record(), "fingerprint", io.BytesIO(b"data"))
    repo.get_ingest_fresh.assert_awaited_once_with("ingest")


def test_cleanup_marker_is_only_hint_and_invalid_markers_are_ignored(tmp_path: Path) -> None:
    import hashlib

    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    staging = tmp_path / "blob" / "staging"
    (staging / (".cleanup-" + "a" * 64)).write_bytes(b"wrong hash")
    raw = b"\xff"
    (staging / (".cleanup-" + hashlib.sha256(raw).hexdigest())).write_bytes(raw)
    (staging / (".cleanup-" + "b" * 64)).mkdir()
    with asset_admission_lock(store._fs.root, exclusive=True) as lease:
        for bad in (0, True):
            with pytest.raises(ValueError, match="positive integer"):
                store.cleanup_orphaned_staging_keys(
                    active_staging_keys=set(), admission_lease=lease, max_entries=bad
                )
        assert (
            store.cleanup_orphaned_staging_keys(active_staging_keys=set(), admission_lease=lease)
            == ()
        )
    assert len(list(staging.iterdir())) == 3


def test_blob_rejects_out_of_namespace_and_oversize_stream(tmp_path: Path) -> None:
    import io

    from nexus_ai_agent.creative.assets.errors import ContentTooLarge

    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    with pytest.raises(BlobStoreError, match="namespace"):
        store.store("elsewhere/name", "irrelevant")
    with pytest.raises(ContentTooLarge):
        store.hash_stream(io.BytesIO(b"too long"), max_bytes=2)


def test_cleanup_records_failure_when_marker_write_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import io

    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    staging, sha, size = store.stage(io.BytesIO(b"test bytes"), max_bytes=100)
    monkeypatch.setattr(store._fs, "unlink", Mock(side_effect=OSError("failure")))
    monkeypatch.setattr(store, "_record_cleanup_marker", Mock(side_effect=OSError("failure")))
    assert store.store(staging, store.blob_key_for(sha)) == (sha, size, False)
    assert staging in store._pending_staging_cleanups
    assert "durable cleanup marker could not be recorded" in caplog.text
    assert store.verify(store.blob_key_for(sha), sha256=sha, size_bytes=size)


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True])
async def test_executor_rejection_releases_publication_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    import asyncio

    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "run_in_executor", Mock(side_effect=RuntimeError("executor stopped")))
    with asset_admission_lock(store._fs.root, exclusive=False) as lease:
        with pytest.raises(RuntimeError, match="executor stopped"):
            await store.publish_from_staging_async(
                "staging/a", "blobs/b", admission_lease=lease if pinned else None
            )
        assert not store.in_flight_staging_keys()
        lease.validate_for(store._fs.root)


@pytest.mark.asyncio
async def test_async_store_alias_publishes_real_bytes(tmp_path: Path) -> None:
    import io

    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    staging, sha, size = store.stage(io.BytesIO(b"content"), max_bytes=100)
    key = store.blob_key_for(sha)
    assert await store.store_async(staging, key) == (sha, size, False)
    assert store.open(key) == b"content"


@pytest.mark.asyncio
async def test_replay_registry_evicts_only_idle_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from nexus_ai_agent.creative.assets import service as module

    monkeypatch.setattr(module, "_MAX_REPLAY_LOCKS", 65)
    loop = asyncio.get_running_loop()
    held = asyncio.Lock()
    await held.acquire()
    registry = {"held": held, **{f"idle-{i}": asyncio.Lock() for i in range(64)}}
    monkeypatch.setitem(module._replay_locks, loop, registry)
    new = module._replay_lock_for(Mock(bind=None), "new")
    assert registry["held"] is held and held.locked()
    assert registry["unknown:new"] is new
    assert len(registry) == 2
    held.release()


@pytest.mark.asyncio
async def test_replay_registry_keeps_all_held_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from nexus_ai_agent.creative.assets import service as module

    monkeypatch.setattr(module, "_MAX_REPLAY_LOCKS", 1)
    held = asyncio.Lock()
    await held.acquire()
    registry = {"held": held}
    monkeypatch.setitem(module._replay_locks, asyncio.get_running_loop(), registry)
    module._replay_lock_for(Mock(bind=None), "new")
    assert registry["held"] is held and len(registry) == 2
    held.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", [False, True])
async def test_ingest_unique_conflict_rolls_back_before_resolving(
    monkeypatch: pytest.MonkeyPatch, winner: bool
) -> None:
    import io
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import IntegrityError

    from nexus_ai_agent.creative.assets import service as module
    from nexus_ai_agent.creative.assets.models import IngestRequest

    session = Mock(
        commit=AsyncMock(side_effect=IntegrityError("insert", {}, Exception("unique"))),
        rollback=AsyncMock(),
    )

    @asynccontextmanager
    async def factory():
        yield session

    repo = repo_mock()
    repo.find_ingest_by_key = AsyncMock(side_effect=[None, record() if winner else None])
    repo.create_ingest = AsyncMock()
    monkeypatch.setattr(module, "SqlAssetRepository", lambda _: repo)
    store = store_mock()
    service = AssetLibraryService(blob_store=store, authorizer=Mock(), session_factory=factory)
    service._resolve_existing = AsyncMock(return_value="winner")
    if winner:
        assert (
            await service._ingest_admitted(
                project_id="p", request=IngestRequest(idempotency_key="k"), stream=io.BytesIO(b"x")
            )
            == "winner"
        )
        service._resolve_existing.assert_awaited_once()
    else:
        with pytest.raises(IntegrityError):
            await service._ingest_admitted(
                project_id="p", request=IngestRequest(idempotency_key="k"), stream=io.BytesIO(b"x")
            )
        service._resolve_existing.assert_not_awaited()
    session.rollback.assert_awaited_once()
    store.stage_to.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["different", "failed"])
async def test_adopted_replay_never_returns_mismatched_or_failed_result(outcome: str) -> None:
    import io

    from nexus_ai_agent.creative.assets.errors import IdempotencyConflict
    from nexus_ai_agent.creative.assets.ingestion import IngestRecoveryReport

    store, repo = store_mock(), repo_mock()
    store.new_staging_key.return_value = "staging/replay"
    store.stage_to.return_value = ("a" * 64, 12)
    repo.set_staging_key = AsyncMock(return_value=True)
    repo.get_ingest_fresh = AsyncMock(return_value=record(content_sha256="b" * 64, size_bytes=12))
    service = AssetLibraryService(blob_store=store, authorizer=Mock())
    state = IngestState.READY if outcome == "different" else IngestState.FAILED
    service._reconcile_committing = AsyncMock(
        return_value=IngestRecoveryReport("ingest", state, RecoveryOutcome.FAILED)
    )
    with pytest.raises(IdempotencyConflict if outcome == "different" else RecoveryError):
        await service._resolve_interrupted(repo, Mock(), record(), io.BytesIO(b"replay"))
    store.remove_staging.assert_not_called()
    repo.set_staging_key.assert_awaited_once_with("ingest", "staging/replay")


@pytest.mark.asyncio
async def test_metadata_incomplete_is_not_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from nexus_ai_agent.creative.assets import service as module
    from nexus_ai_agent.creative.assets.errors import AssetNotFound

    @asynccontextmanager
    async def factory():
        yield object()

    repo = repo_mock()
    repo.get_link_for_project = AsyncMock(
        return_value=SimpleNamespace(asset_id="a", version_id="v")
    )
    repo.list_project_assets = AsyncMock(
        return_value=[SimpleNamespace(asset_id="a", version_id="v")]
    )
    repo.get_asset = AsyncMock(return_value=None)
    repo.get_version = AsyncMock(return_value=object())
    monkeypatch.setattr(module, "SqlAssetRepository", lambda _: repo)
    service = AssetLibraryService(
        blob_store=store_mock(), authorizer=Mock(), session_factory=factory
    )
    with pytest.raises(AssetNotFound, match="incomplete"):
        await service.get_asset(actor_kind="user", actor_id="u", project_id="p", asset_id="a")
    assert await service.list_project_assets(actor_kind="user", actor_id="u", project_id="p") == []


@pytest.mark.asyncio
async def test_resolved_race_without_owned_staging_does_not_remove_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus_ai_agent.creative.assets import service as module

    store, repo = store_mock(), repo_mock()
    repo.get_ingest_fresh = AsyncMock(return_value=record(state=IngestState.READY))
    session = Mock(rollback=AsyncMock(), commit=AsyncMock())
    monkeypatch.setattr(
        module, "reconcile_ingest", AsyncMock(side_effect=IngestAlreadyResolved("i"))
    )
    service = AssetLibraryService(blob_store=store, authorizer=Mock())
    report = await service._reconcile_committing(session, repo, record())
    assert report.outcome is RecoveryOutcome.ALREADY_READY
    session.rollback.assert_awaited_once()
    store.remove_staging.assert_not_called()


@pytest.mark.asyncio
async def test_startup_refuses_recovered_row_without_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    from nexus_ai_agent.creative.assets import service as module
    from nexus_ai_agent.creative.assets.ingestion import IngestRecoveryReport

    @asynccontextmanager
    async def factory():
        yield object()

    repo = repo_mock()
    repo.list_pending_ingests = AsyncMock(return_value=[])
    repo.get_ingest = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "SqlAssetRepository", lambda _: repo)
    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    service = AssetLibraryService(blob_store=store, authorizer=Mock(), session_factory=factory)
    service.recover_all = AsyncMock(
        return_value=[
            IngestRecoveryReport("failed", IngestState.FAILED, RecoveryOutcome.FAILED),
            IngestRecoveryReport("ingest", IngestState.READY, RecoveryOutcome.COMPLETED),
        ]
    )
    with pytest.raises(RecoveryError, match="missing its asset identity"):
        await async_reconcile_asset_library_startup(service)
    repo.get_ingest.assert_awaited_once_with("ingest")
    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    with asset_admission_lock(store._fs.root, exclusive=True, timeout_seconds=0.1):
        pass


def test_sync_startup_entrypoint_runs_async_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_ai_agent.creative.assets import service as module

    expected = module.StartupReconciliationReport((), (), 0)
    worker = AsyncMock(return_value=expected)
    monkeypatch.setattr(module, "async_reconcile_asset_library_startup", worker)
    service = AssetLibraryService(blob_store=store_mock(), authorizer=Mock())
    assert reconcile_asset_library_startup(service, max_sweep_items=3) == expected
    worker.assert_awaited_once_with(service, max_sweep_items=3)


def test_marker_cleanup_missing_file_and_max_entries(tmp_path: Path) -> None:
    import hashlib

    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    for key in ("staging/one", "staging/two"):
        raw = key.encode()
        (
            tmp_path / "blob" / "staging" / (".cleanup-" + hashlib.sha256(raw).hexdigest())
        ).write_bytes(raw)
    with asset_admission_lock(store._fs.root, exclusive=True) as lease:
        removed = store.cleanup_orphaned_staging_keys(
            active_staging_keys=set(), admission_lease=lease, max_entries=1
        )
    assert len(removed) == 1
    assert len(list((tmp_path / "blob" / "staging").iterdir())) == 1


def test_pending_cleanup_does_not_remove_authoritative_active_key(tmp_path: Path) -> None:
    import io

    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    key, _, _ = store.stage(io.BytesIO(b"active"), max_bytes=100)
    store._pending_staging_cleanups.add(key)
    with asset_admission_lock(store._fs.root, exclusive=True) as lease:
        assert store.reconcile_staging_cleanups(
            active_staging_keys={key}, admission_lease=lease
        ) == (key,)
    assert (tmp_path / "blob" / key).read_bytes() == b"active"


def test_remove_staging_is_best_effort_on_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    monkeypatch.setattr(store._fs, "unlink", Mock(side_effect=OSError("io failure")))
    assert store.remove_staging("staging/missing") is None


@pytest.mark.parametrize("bad_call", [1, 2, 3])
def test_blob_root_and_staging_permissions_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_call: int
) -> None:
    import os
    import stat

    from nexus_ai_agent.creative.assets import blob_store as module

    real_fstat = os.fstat
    calls = 0
    chmod_calls = 0
    real_fchmod = os.fchmod

    def track_fchmod(fd: int, mode: int) -> None:
        nonlocal chmod_calls
        chmod_calls += 1
        real_fchmod(fd, mode)

    def bad_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        info = real_fstat(fd)
        if (bad_call < 3 and calls == bad_call) or (bad_call == 3 and chmod_calls == 2):
            fields = list(info)
            fields[0] = (stat.S_IFREG | 0o600) if bad_call == 1 else (stat.S_IFDIR | 0o755)
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(module.os, "fstat", bad_fstat)
    monkeypatch.setattr(module.os, "fchmod", track_fchmod)
    with pytest.raises(BlobStoreError, match="not a directory|permissions are unsafe"):
        LocalContentAddressedBlobStore(tmp_path / "blob")


@pytest.mark.asyncio
async def test_unpinned_acquisition_failure_is_not_leaked(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    store = LocalContentAddressedBlobStore(tmp_path / "blob", lock_timeout_seconds=0.01)
    with asset_admission_lock(store._fs.root, exclusive=True):
        with pytest.raises(TimeoutError):
            await store.publish_from_staging_async("staging/a", "blobs/b")
    assert not store.in_flight_staging_keys()
    with asset_admission_lock(store._fs.root, exclusive=True, timeout_seconds=0.1):
        pass


@pytest.mark.asyncio
async def test_worker_submission_failure_releases_acquired_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    loop = asyncio.get_running_loop()
    run = loop.run_in_executor
    calls = 0

    def submit(*args: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("worker rejected")
        return run(*args)

    monkeypatch.setattr(loop, "run_in_executor", submit)
    with pytest.raises(RuntimeError, match="worker rejected"):
        await store.publish_from_staging_async("staging/a", "blobs/b")
    assert not store.in_flight_staging_keys()
    with asset_admission_lock(store._fs.root, exclusive=True, timeout_seconds=0.1):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("acquisition_fails", [False, True])
async def test_cancellation_after_acquisition_settles_releases_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acquisition_fails: bool
) -> None:
    import asyncio

    from nexus_ai_agent.creative.assets import blob_store as module
    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    store = LocalContentAddressedBlobStore(tmp_path / "blob", lock_timeout_seconds=0.01)
    real_shield = asyncio.shield

    async def cancel_after_settlement(future):
        try:
            await real_shield(future)
        except TimeoutError:
            pass
        raise asyncio.CancelledError

    monkeypatch.setattr(module.asyncio, "shield", cancel_after_settlement)
    if acquisition_fails:
        with asset_admission_lock(store._fs.root, exclusive=True):
            with pytest.raises(asyncio.CancelledError):
                await store.publish_from_staging_async("staging/a", "blobs/b")
    else:
        with pytest.raises(asyncio.CancelledError):
            await store.publish_from_staging_async("staging/a", "blobs/b")
    assert not store.in_flight_staging_keys()
    with asset_admission_lock(store._fs.root, exclusive=True, timeout_seconds=0.1):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pinned", "unpinned", "acquisition"])
async def test_cancelled_completion_callbacks_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    import asyncio
    import io

    from nexus_ai_agent.creative.assets import blob_store as module
    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    store = LocalContentAddressedBlobStore(tmp_path / "blob")
    staging, sha, size = store.stage(io.BytesIO(b"data"), max_bytes=100)
    loop = asyncio.get_running_loop()
    original_submit = loop.run_in_executor
    callback_names: list[str] = []

    def submit(*args: object):
        future = original_submit(*args)
        original_add = future.add_done_callback

        def add(callback, *cb_args, **cb_kwargs):
            original_add(callback, *cb_args, **cb_kwargs)
            if callback.__name__.startswith("consume_"):
                callback_names.append(callback.__name__)
                cancelled = loop.create_future()
                cancelled.cancel()
                loop.call_soon(callback, cancelled)

        future.add_done_callback = add
        return future

    monkeypatch.setattr(loop, "run_in_executor", submit)
    if mode == "acquisition":
        shield = asyncio.shield

        async def cancel_after_settlement(future):
            await shield(future)
            raise asyncio.CancelledError

        monkeypatch.setattr(module.asyncio, "shield", cancel_after_settlement)
        with pytest.raises(asyncio.CancelledError):
            await store.publish_from_staging_async(staging, store.blob_key_for(sha))
        assert (tmp_path / "blob" / staging).read_bytes() == b"data"
    elif mode == "pinned":
        with asset_admission_lock(store._fs.root, exclusive=False) as lease:
            assert await store.publish_from_staging_async(
                staging, store.blob_key_for(sha), admission_lease=lease
            ) == (sha, size, False)
    else:
        assert await store.publish_from_staging_async(staging, store.blob_key_for(sha)) == (
            sha,
            size,
            False,
        )
    await asyncio.sleep(0)
    assert callback_names == [
        {
            "pinned": "consume_pinned",
            "unpinned": "consume_result",
            "acquisition": "consume_acquisition",
        }[mode]
    ]
    assert not store.in_flight_staging_keys()
    with asset_admission_lock(store._fs.root, exclusive=True, timeout_seconds=0.1):
        pass
