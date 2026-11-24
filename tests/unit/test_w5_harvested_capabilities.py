"""Regression tests for content-addressed publication under collision."""

from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus_ai_agent.creative.assets.blob_store import LocalContentAddressedBlobStore
from nexus_ai_agent.creative.assets.errors import ContentIntegrityError


@pytest.mark.parametrize("winner", [b"payload", b"corrupt"])
def test_interleaved_destination_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winner: bytes
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    real_link = os.link

    def competing_link(src: str, dst: str, **kwargs: object) -> None:
        target = tmp_path / key
        target.write_bytes(winner)
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", competing_link)
    if winner == b"payload":
        assert store.store(staging, key) == (digest, size, True)
        assert not (tmp_path / staging).exists()
    else:
        with pytest.raises(ContentIntegrityError):
            store.store(staging, key)
    assert (tmp_path / key).read_bytes() == winner


def test_source_replacement_never_publishes_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    real_link = os.link

    def replace_source(src: str, dst: str, **kwargs: object) -> None:
        path = tmp_path / staging
        path.unlink()
        path.write_bytes(b"forged")
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", replace_source)
    assert store.store(staging, key) == (digest, len(b"payload"), False)
    assert (tmp_path / key).read_bytes() == b"payload"


def test_source_mutated_after_copy_cannot_modify_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    real_link = os.link

    def modify_source(src: str, dst: str, **kwargs: object) -> None:
        (tmp_path / staging).write_bytes(b"forged")
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", modify_source)
    assert store.store(staging, key) == (digest, len(b"payload"), False)
    assert (tmp_path / key).read_bytes() == b"payload"


def test_changed_source_before_copy_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    real_publish = store._fs.link_within_if_absent

    def mutate(source: str, destination: str, *, sha256: str, size: int) -> bool:
        (tmp_path / staging).write_bytes(b"forged")
        return real_publish(source, destination, sha256=sha256, size=size)

    monkeypatch.setattr(store._fs, "link_within_if_absent", mutate)
    with pytest.raises(ContentIntegrityError):
        store.store(staging, key)
    assert not (tmp_path / key).exists()
    assert not list((tmp_path / key).parent.glob(".pub_*"))


@pytest.mark.parametrize("replace_destination", [False, True])
def test_corrupted_candidate_rolls_back_only_its_own_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace_destination: bool
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    target = tmp_path / key
    real_link = os.link
    real_digest = store._fs._fd_digest
    calls = 0

    def corrupt_candidate(src: str, dst: str, **kwargs: object) -> None:
        parent = kwargs["src_dir_fd"]
        assert isinstance(parent, int)
        os.chmod(src, 0o600, dir_fd=parent, follow_symlinks=False)
        fd = os.open(src, os.O_WRONLY | os.O_TRUNC, dir_fd=parent)
        try:
            os.write(fd, b"forged")
        finally:
            os.close(fd)
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    def replacement(fd: int) -> tuple[str, int]:
        nonlocal calls
        calls += 1
        result = real_digest(fd)
        if calls == 2 and replace_destination:
            target.unlink()
            target.write_bytes(b"other process")
        return result

    monkeypatch.setattr(os, "link", corrupt_candidate)
    monkeypatch.setattr(store._fs, "_fd_digest", replacement)
    with pytest.raises(ContentIntegrityError):
        store.store(staging, key)
    if replace_destination:
        assert target.read_bytes() == b"other process"
    else:
        assert not target.exists()


def test_concurrent_publication_has_one_winner(tmp_path: Path) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    a, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    b, _, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda staging: store.store(staging, key), (a, b)))
    assert sorted(result[2] for result in results) == [False, True]
    assert all(result[:2] == (digest, size) for result in results)
    assert (tmp_path / key).read_bytes() == b"payload"


def test_root_and_staging_permissions_and_symlink_refusal(tmp_path: Path) -> None:
    import stat

    root = tmp_path / "root"
    root.mkdir(mode=0o755)
    (root / "staging").mkdir(mode=0o755)
    LocalContentAddressedBlobStore(root)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "staging").stat().st_mode) == 0o700
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        LocalContentAddressedBlobStore(link)


def test_locked_publication_times_out(tmp_path: Path) -> None:
    import fcntl

    store = LocalContentAddressedBlobStore(tmp_path, lock_timeout_seconds=0.01)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    fd = os.open(tmp_path / ".blob_publish.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TimeoutError, match="publication lock timed out"):
            store.store(staging, store.blob_key_for(digest))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_async_publication_and_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import stat

    store = LocalContentAddressedBlobStore(tmp_path)
    stage, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    real_fsync = os.fsync
    directories: list[int] = []

    def tracking_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    assert asyncio.run(store.publish_from_staging_async(stage, store.blob_key_for(digest))) == (
        digest,
        size,
        False,
    )
    assert directories


def test_successful_publication_survives_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    unlink = store._fs.unlink

    def fail(_key: str) -> None:
        raise PermissionError("simulated cleanup failure")

    monkeypatch.setattr(store._fs, "unlink", fail)
    assert store.store(staging, key) == (digest, size, False)
    assert store.open(key) == b"payload"
    assert store.reconcile_staging_cleanups() == (staging,)
    monkeypatch.setattr(store._fs, "unlink", unlink)
    assert store.reconcile_staging_cleanups() == ()
    assert not (tmp_path / staging).exists()


def test_ingestion_publishes_in_worker_without_blocking_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading
    from typing import Any, cast
    from unittest.mock import AsyncMock

    from nexus_ai_agent.creative.assets.ingestion import publish_and_commit
    from nexus_ai_agent.creative.assets.ports import AssetRepository

    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    real_store = store.store
    release = threading.Event()
    worker_threads: list[int] = []
    repo = AsyncMock()
    repo.complete_ingest.return_value = True

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        main_thread = threading.get_ident()
        started = asyncio.Event()

        def blocking_publish(staging_key: str, blob_key: str) -> tuple[str, int, bool]:
            worker_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=5):
                raise TimeoutError("test worker was not released")
            return real_store(staging_key, blob_key)

        monkeypatch.setattr(store, "store", blocking_publish)
        task = asyncio.create_task(
            publish_and_commit(
                cast(AssetRepository, repo),
                store,
                ingest_id="i",
                project_id="p",
                staging_key=staging,
                content_sha256=digest,
                size_bytes=size,
                media_kind=cast(Any, "image"),
                media_type="image/png",
            )
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert not task.done()
            assert worker_threads[0] != main_thread
        finally:
            release.set()
        assert (await task)[2] is False

    asyncio.run(exercise())


def test_published_blob_is_owner_read_only(tmp_path: Path) -> None:
    import stat

    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, size = store.stage(io.BytesIO(b"private payload"), max_bytes=1024)
    key = store.blob_key_for(digest)
    store.store(staging, key)
    assert stat.S_IMODE((tmp_path / key).stat().st_mode) == 0o400
    (tmp_path / key).chmod(0o600)
    (tmp_path / key).write_bytes(b"tampered")
    (tmp_path / key).chmod(0o400)
    assert not store.verify(key, sha256=digest, size_bytes=size)


def _publish_in_process(root: str, staging: str, digest: str, result: object) -> None:
    store = LocalContentAddressedBlobStore(root, lock_timeout_seconds=0.15)
    try:
        store.store(staging, store.blob_key_for(digest))
    except TimeoutError:
        result.put("timeout")  # type: ignore[attr-defined]
    else:
        result.put("published")  # type: ignore[attr-defined]


def test_publication_flock_across_real_processes(tmp_path: Path) -> None:
    import fcntl
    import multiprocessing

    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    fd = os.open(tmp_path / ".blob_publish.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        child = context.Process(
            target=_publish_in_process, args=(str(tmp_path), staging, digest, result)
        )
        child.start()
        child.join(10)
        assert child.exitcode == 0
        assert result.get(timeout=2) == "timeout"
        assert (tmp_path / staging).exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    child = context.Process(
        target=_publish_in_process, args=(str(tmp_path), staging, digest, result)
    )
    child.start()
    child.join(10)
    assert child.exitcode == 0
    assert result.get(timeout=2) == "published"
    assert store.verify(store.blob_key_for(digest), sha256=digest, size_bytes=size)
    result.close()
    result.join_thread()


def test_cleanup_marker_survives_restart_and_protects_active_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, size = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    original_unlink = store._fs.unlink

    def fail_staging(name: str) -> None:
        if name == staging:
            raise PermissionError("simulated failure")
        original_unlink(name)

    monkeypatch.setattr(store._fs, "unlink", fail_staging)
    assert store.store(staging, key) == (digest, size, False)
    assert len(list((tmp_path / "staging").glob(".cleanup-*"))) == 1
    restarted = LocalContentAddressedBlobStore(tmp_path)
    assert restarted.cleanup_orphaned_staging(active_staging_keys={staging}) == 0
    assert (tmp_path / staging).exists()
    assert restarted.cleanup_orphaned_staging(active_staging_keys=set()) == 1
    assert not (tmp_path / staging).exists()
    assert not list((tmp_path / "staging").glob(".cleanup-*"))
    assert restarted.cleanup_orphaned_staging(active_staging_keys=set()) == 0
    assert restarted.verify(key, sha256=digest, size_bytes=size)


def test_cleanup_marker_never_follows_staging_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalContentAddressedBlobStore(tmp_path / "root")
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    monkeypatch.setattr(store._fs, "unlink", lambda _key: (_ for _ in ()).throw(PermissionError()))
    store.store(staging, store.blob_key_for(digest))
    target = tmp_path / "outside"
    target.write_bytes(b"outside")
    path = tmp_path / "root" / staging
    path.unlink()
    path.symlink_to(target)
    restarted = LocalContentAddressedBlobStore(tmp_path / "root")
    assert restarted.cleanup_orphaned_staging(active_staging_keys=set()) == 0
    assert target.read_bytes() == b"outside"
    assert path.is_symlink()


def test_cleanup_marker_repairs_incomplete_prior_marker(tmp_path: Path) -> None:
    import hashlib

    store = LocalContentAddressedBlobStore(tmp_path)
    staging, _, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    marker = tmp_path / "staging" / (".cleanup-" + hashlib.sha256(staging.encode()).hexdigest())
    marker.write_bytes(b"truncated")
    store._record_cleanup_marker(staging)
    assert marker.read_bytes() == staging.encode()
    restarted = LocalContentAddressedBlobStore(tmp_path)
    assert restarted.cleanup_orphaned_staging(active_staging_keys={staging}) == 0
    assert (tmp_path / staging).exists()


def test_cleanup_marker_refuses_symlink_target(tmp_path: Path) -> None:
    import hashlib

    store = LocalContentAddressedBlobStore(tmp_path / "store")
    staging, _, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    marker = (
        tmp_path
        / "store"
        / "staging"
        / (".cleanup-" + hashlib.sha256(staging.encode()).hexdigest())
    )
    marker.symlink_to(outside)
    from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError

    with pytest.raises(FilesystemBoundaryError):
        store._record_cleanup_marker(staging)
    assert outside.read_bytes() == b"untouched"


def test_admission_lock_blocks_other_process_and_recovers_after_sigkill(tmp_path: Path) -> None:
    import signal
    import subprocess
    import sys

    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    program = """
import sys, time
from pathlib import Path
from nexus_ai_agent.creative.assets.admission import asset_admission_lock
with asset_admission_lock(Path(sys.argv[1]), exclusive=False):
    print('locked', flush=True)
    time.sleep(30)
"""
    process = subprocess.Popen(  # noqa: S603 - known interpreter and local test source
        [sys.executable, "-c", program, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError):
            with asset_admission_lock(tmp_path, exclusive=True, timeout_seconds=0.1):
                pytest.fail("exclusive lock entered while child was holding shared lock")
        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=5) == -signal.SIGKILL
        with asset_admission_lock(tmp_path, exclusive=True, timeout_seconds=1):
            assert (tmp_path / ".asset-library-maintenance.lock").is_file()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.asyncio
async def test_cancelled_publication_retains_kernel_lock_until_worker_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import subprocess
    import sys
    import threading

    store = LocalContentAddressedBlobStore(tmp_path)
    staging, digest, _ = store.stage(io.BytesIO(b"payload"), max_bytes=100)
    key = store.blob_key_for(digest)
    start = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    original = store.store
    loop = asyncio.get_running_loop()

    def slow(stage: str, blob: str) -> tuple[str, int, bool]:
        loop.call_soon_threadsafe(start.set)
        try:
            if not release.wait(5):
                raise TimeoutError("test worker was not released")
            return original(stage, blob)
        finally:
            finished.set()

    monkeypatch.setattr(store, "store", slow)
    task = asyncio.create_task(store.publish_from_staging_async(staging, key))
    program = """
import sys
from pathlib import Path
from nexus_ai_agent.creative.assets.admission import asset_admission_lock
with asset_admission_lock(Path(sys.argv[1]), exclusive=True, timeout_seconds=0.1):
    pass
"""
    try:
        await asyncio.wait_for(start.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        blocked = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", program, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert blocked.returncode != 0
        assert "TimeoutError" in blocked.stderr
        assert (tmp_path / staging).read_bytes() == b"payload"
        assert staging in store.in_flight_staging_keys()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    acquired = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert acquired.returncode == 0, acquired.stderr
    assert store.verify(key, sha256=digest, size_bytes=len(b"payload"))


def test_admission_lock_refuses_symlink_and_invalid_timeout(tmp_path: Path) -> None:
    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    target = tmp_path / "outside"
    target.write_text("untouched")
    (tmp_path / ".asset-library-maintenance.lock").symlink_to(target)
    with pytest.raises(OSError):
        with asset_admission_lock(tmp_path, exclusive=True):
            pass
    assert target.read_text() == "untouched"
    for value in (0, -1, True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            with asset_admission_lock(tmp_path, exclusive=True, timeout_seconds=value):
                pass


@pytest.mark.asyncio
async def test_queued_publication_keeps_process_lock_after_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import subprocess
    import sys
    import threading
    from concurrent.futures import ThreadPoolExecutor

    store = LocalContentAddressedBlobStore(tmp_path)
    stage, digest, _ = store.stage(io.BytesIO(b"queued payload"), max_bytes=100)
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    release = threading.Event()
    entered = asyncio.Event()
    original_submit = loop.run_in_executor

    def occupy_executor() -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise TimeoutError("queue test was not released")

    def intercept(pool: object, func: object, *args: object):
        if getattr(func, "__name__", "") == "publish":
            executor.submit(occupy_executor)
        return original_submit(pool, func, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(loop, "run_in_executor", intercept)
    task = asyncio.create_task(store.publish_from_staging_async(stage, store.blob_key_for(digest)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        program = """
import sys
from pathlib import Path
from nexus_ai_agent.creative.assets.admission import asset_admission_lock
with asset_admission_lock(Path(sys.argv[1]), exclusive=True, timeout_seconds=0.1):
    pass
"""
        blocked = subprocess.run(  # noqa: S603 - known interpreter and test source
            [sys.executable, "-c", program, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert blocked.returncode != 0 and "TimeoutError" in blocked.stderr
        assert stage in store.in_flight_staging_keys()
        assert (tmp_path / stage).read_bytes() == b"queued payload"
    finally:
        release.set()
        for _ in range(500):
            if stage not in store.in_flight_staging_keys():
                break
            await asyncio.sleep(0.01)
        executor.shutdown(wait=True)
    assert stage not in store.in_flight_staging_keys()
    assert store.verify(
        store.blob_key_for(digest), sha256=digest, size_bytes=len(b"queued payload")
    )


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_admission_releases_late_lease(tmp_path: Path) -> None:
    import asyncio

    from nexus_ai_agent.creative.assets.admission import asset_admission_lock

    store = LocalContentAddressedBlobStore(tmp_path, lock_timeout_seconds=1)
    stage, digest, _ = store.stage(io.BytesIO(b"waiting payload"), max_bytes=100)
    with asset_admission_lock(tmp_path, exclusive=True):
        task = asyncio.create_task(
            store.publish_from_staging_async(stage, store.blob_key_for(digest))
        )
        for _ in range(100):
            if stage in store.in_flight_staging_keys():
                break
            await asyncio.sleep(0.01)
        assert stage in store.in_flight_staging_keys()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stage in store.in_flight_staging_keys()
    for _ in range(200):
        if stage not in store.in_flight_staging_keys():
            break
        await asyncio.sleep(0.01)
    assert stage not in store.in_flight_staging_keys()
    assert (tmp_path / stage).read_bytes() == b"waiting payload"
    with asset_admission_lock(tmp_path, exclusive=True, timeout_seconds=0.5):
        pass
