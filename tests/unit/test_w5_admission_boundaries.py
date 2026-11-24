"""Boundary checks for the cooperative asset admission lease."""

import os
from pathlib import Path

import pytest

from nexus_ai_agent.creative.assets import admission
from nexus_ai_agent.storage.asset_startup import check_asset_database


def test_startup_rejects_directory_as_database(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="regular file"):
        check_asset_database(tmp_path)


@pytest.mark.parametrize("exclusive", [1, None, "yes"])
def test_rejects_nonboolean_exclusive(tmp_path: Path, exclusive: object) -> None:
    with (
        pytest.raises(ValueError, match="boolean"),
        admission.asset_admission_lock(
            tmp_path,
            exclusive=exclusive,  # type: ignore[arg-type]
        ),
    ):
        pytest.fail("invalid lock accepted")


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan"), "1"])
def test_rejects_invalid_timeout(tmp_path: Path, timeout: object) -> None:
    with (
        pytest.raises(ValueError, match="timeout"),
        admission.asset_admission_lock(
            tmp_path,
            exclusive=True,
            timeout_seconds=timeout,  # type: ignore[arg-type]
        ),
    ):
        pytest.fail("invalid timeout accepted")


def test_rejects_nonlease(tmp_path: Path) -> None:
    with (
        pytest.raises(PermissionError, match="invalid"),
        admission.asset_admission_lock(
            tmp_path,
            exclusive=True,
            admission_lease=object(),  # type: ignore[arg-type]
        ),
    ):
        pytest.fail("invalid lease accepted")


def test_rejects_regular_file_as_root(tmp_path: Path) -> None:
    root = tmp_path / "file"
    root.touch()
    with (
        pytest.raises(ValueError, match="not a directory"),
        admission.asset_admission_lock(root, exclusive=True),
    ):
        pytest.fail("file root accepted")


def test_rejects_lock_with_unsafe_permissions(tmp_path: Path) -> None:
    lock = tmp_path / admission._LOCK_NAME
    lock.touch(mode=0o644)
    lock.chmod(0o644)
    with (
        pytest.raises(PermissionError, match="unsafe"),
        admission.asset_admission_lock(tmp_path, exclusive=True),
    ):
        pytest.fail("unsafe lock accepted")


def test_deleted_lock_invalidates_lease(tmp_path: Path) -> None:
    with admission.asset_admission_lock(tmp_path, exclusive=True) as lease:
        (tmp_path / admission._LOCK_NAME).unlink()
        with pytest.raises(PermissionError, match="invalid"):
            lease.validate_for(tmp_path)


def test_root_replacement_invalidates_lease(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with admission.asset_admission_lock(root, exclusive=True) as lease:
        root.rename(tmp_path / "old")
        root.mkdir()
        with pytest.raises(PermissionError, match="inode changed"):
            lease.validate_for(root)


def test_pin_failure_closes_first_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with admission.asset_admission_lock(tmp_path, exclusive=True) as lease:
        real_dup = os.dup
        duplicates: list[int] = []

        def failing_dup(fd: int) -> int:
            if duplicates:
                raise OSError("second dup failed")
            duplicates.append(real_dup(fd))
            return duplicates[-1]

        monkeypatch.setattr(admission.os, "dup", failing_dup)
        with pytest.raises(OSError, match="second dup failed"), lease.pin_for_worker():
            pytest.fail("pin accepted")
        with pytest.raises(OSError):
            os.fstat(duplicates[0])
        lease.validate_for(tmp_path)


def test_pin_constructor_failure_closes_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with admission.asset_admission_lock(tmp_path, exclusive=True) as lease:
        real_dup = os.dup
        duplicates: list[int] = []

        def recording_dup(fd: int) -> int:
            duplicates.append(real_dup(fd))
            return duplicates[-1]

        def fail_constructor(*args: object, **kwargs: object) -> None:
            raise OSError("constructor failed")

        monkeypatch.setattr(admission.os, "dup", recording_dup)
        monkeypatch.setattr(admission, "AdmissionLease", fail_constructor)
        with pytest.raises(OSError, match="constructor failed"), lease.pin_for_worker():
            pytest.fail("pin accepted")
        for fd in duplicates:
            with pytest.raises(OSError):
                os.fstat(fd)
        lease.validate_for(tmp_path)


def test_acquisition_rejects_lock_replaced_after_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_stat = os.stat

    def changed_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        info = real_stat(path, *args, **kwargs)  # type: ignore[arg-type,call-overload]
        if path == admission._LOCK_NAME and kwargs.get("dir_fd") is not None:
            fields = list(info)
            fields[1] += 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(admission.os, "stat", changed_stat)
    with (
        pytest.raises(PermissionError, match="replaced"),
        admission.asset_admission_lock(tmp_path, exclusive=True),
    ):
        pytest.fail("replaced lock accepted")


def test_cancellation_after_worker_acquires_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def cancel_after_acquire(future: asyncio.Future[None]) -> None:
        await future
        raise asyncio.CancelledError

    async def run() -> None:
        monkeypatch.setattr(admission.asyncio, "shield", cancel_after_acquire)
        with pytest.raises(asyncio.CancelledError):
            async with admission.async_asset_admission_lock(tmp_path, exclusive=True):
                pytest.fail("cancelled lock entered")
        with admission.asset_admission_lock(tmp_path, exclusive=True, timeout_seconds=0.1):
            pass

    asyncio.run(run())


def test_cancelled_acquisition_future_is_consumed(tmp_path: Path) -> None:
    import asyncio
    from unittest.mock import patch

    async def run() -> None:
        loop = asyncio.get_running_loop()
        cancelled: asyncio.Future[None] = loop.create_future()
        cancelled.cancel()
        with patch.object(loop, "run_in_executor", return_value=cancelled):
            with pytest.raises(asyncio.CancelledError):
                async with admission.async_asset_admission_lock(tmp_path, exclusive=True):
                    pytest.fail("cancelled lock entered")
            await asyncio.sleep(0)
        with admission.asset_admission_lock(tmp_path, exclusive=True, timeout_seconds=0.1):
            pass

    asyncio.run(run())
