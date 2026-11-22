"""Adversarial owner fencing for local and portable-bundle push leases."""

from __future__ import annotations

import hashlib
import os
import types
from collections.abc import Callable
from pathlib import Path

import pytest

from nexus_ai_agent.storage.personal_cloud_vault import byos_drivers
from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import (
    LocalDeviceVaultDriver,
    PortableBundleVaultDriver,
)
from nexus_ai_agent.storage.personal_cloud_vault.errors import (
    VaultRevisionConflictError,
    VaultTransportError,
)


@pytest.mark.parametrize("backend", ["local", "bundle"])
def test_unowned_peer_cannot_refresh_or_release_existing_lease(
    backend: str, tmp_path: Path
) -> None:
    owner: LocalDeviceVaultDriver | PortableBundleVaultDriver
    peer: LocalDeviceVaultDriver | PortableBundleVaultDriver
    get_raw: Callable[[], bytes | None]
    assert_absent: Callable[[], None]
    if backend == "local":
        local_owner = LocalDeviceVaultDriver(tmp_path / "local")
        local_peer = LocalDeviceVaultDriver(tmp_path / "local")
        lease_path = local_owner._lease_path("peer-fence")
        owner, peer = local_owner, local_peer

        def local_get_raw() -> bytes | None:
            return lease_path.read_bytes() if lease_path.exists() else None

        def local_assert_absent() -> None:
            assert not lease_path.exists()

        get_raw = local_get_raw
        assert_absent = local_assert_absent
    else:
        bundle_path = tmp_path / "owned.nagarvault"
        bundle_owner = PortableBundleVaultDriver(bundle_path)
        bundle_peer = PortableBundleVaultDriver(bundle_path)
        owner, peer = bundle_owner, bundle_peer

        def get_bundle_raw() -> bytes | None:
            bundle_owner._read_locked()
            return bundle_owner._leases.get("peer-fence")

        def assert_bundle_absent() -> None:
            bundle_owner._read_locked()
            assert "peer-fence" not in bundle_owner._leases

        get_raw = get_bundle_raw
        assert_absent = assert_bundle_absent

    lease_id = owner.acquire_push_lease([], lease_id="peer-fence")
    initial = get_raw()
    assert initial is not None
    old_etag = hashlib.sha256(initial).hexdigest()

    with pytest.raises(VaultRevisionConflictError):
        peer.update_push_lease(lease_id, ["a" * 64])
    assert get_raw() == initial
    with pytest.raises(VaultRevisionConflictError):
        peer.update_push_lease(lease_id, ["a" * 64], expected_etag="0" * 64)
    assert get_raw() == initial

    with pytest.raises(VaultRevisionConflictError):
        peer.release_push_lease(lease_id)
    assert get_raw() == initial
    with pytest.raises(VaultRevisionConflictError):
        peer.release_push_lease(lease_id, expected_etag="0" * 64)
    assert get_raw() == initial

    owner.update_push_lease(lease_id, ["b" * 64])
    refreshed = get_raw()
    assert refreshed is not None and refreshed != initial
    with pytest.raises(VaultRevisionConflictError):
        peer.release_push_lease(lease_id, expected_etag=old_etag)
    assert get_raw() == refreshed

    owner.release_push_lease(lease_id)
    owner.release_push_lease(lease_id)  # already absent is an idempotent no-op
    assert_absent()


def _owned_local_lease(tmp_path: Path) -> tuple[LocalDeviceVaultDriver, str, Path]:
    driver = LocalDeviceVaultDriver(tmp_path / "local")
    lease_id = driver.acquire_push_lease([], lease_id="fence-edge")
    return driver, lease_id, driver._lease_path(lease_id)


def test_local_refresh_and_release_refuse_a_blank_etag(tmp_path: Path) -> None:
    driver, lease_id, path = _owned_local_lease(tmp_path)
    with pytest.raises(VaultTransportError):
        driver.update_push_lease(lease_id, [], expected_etag="   ")
    with pytest.raises(VaultTransportError):
        driver.release_push_lease(lease_id, expected_etag="")
    assert path.is_file()


def test_local_lease_read_refuses_a_symlink_or_a_directory(tmp_path: Path) -> None:
    driver, lease_id, path = _owned_local_lease(tmp_path)
    target = path.with_name("elsewhere.json")
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(VaultTransportError):
        byos_drivers._read_lease_file_once(path)
    path.unlink()
    path.mkdir()
    with pytest.raises(VaultTransportError, match="not a regular file"):
        byos_drivers._read_lease_file_once(path)
    path.rmdir()
    assert byos_drivers._read_lease_file_once(path) is None


def test_lease_read_fails_closed_when_the_inode_changes_during_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _driver, _lease_id, path = _owned_local_lease(tmp_path)
    real_stat = os.stat

    def _swapped(target: object, *args: object, **kwargs: object) -> object:
        if str(target) == str(path) and kwargs.get("follow_symlinks") is False:
            real = real_stat(path, follow_symlinks=False)
            return types.SimpleNamespace(st_dev=real.st_dev, st_ino=real.st_ino + 1)
        return real_stat(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", _swapped)
    with pytest.raises(VaultRevisionConflictError, match="changed while it was being read"):
        byos_drivers._read_lease_file_once(path)


def test_lease_read_fails_closed_when_the_post_read_stat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _driver, _lease_id, path = _owned_local_lease(tmp_path)
    real_stat = os.stat

    def _gone(target: object, *args: object, **kwargs: object) -> object:
        if str(target) == str(path) and kwargs.get("follow_symlinks") is False:
            raise OSError(2, "lease vanished")
        return real_stat(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", _gone)
    with pytest.raises(VaultRevisionConflictError, match="changed while it was being read"):
        byos_drivers._read_lease_file_once(path)


def test_lease_read_reports_a_failed_read_as_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _driver, _lease_id, path = _owned_local_lease(tmp_path)

    def _broken_read(_fd: int, _size: int) -> bytes:
        raise OSError(5, "injected read failure")

    monkeypatch.setattr(os, "read", _broken_read)
    with pytest.raises(VaultTransportError, match="Cannot read push lease"):
        byos_drivers._read_lease_file_once(path)


def test_lease_read_refuses_when_the_platform_lacks_no_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _driver, _lease_id, path = _owned_local_lease(tmp_path)
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(VaultTransportError, match="cannot safely open"):
        byos_drivers._read_lease_file_once(path)


def test_local_release_reports_a_failed_unlink_as_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, lease_id, path = _owned_local_lease(tmp_path)

    def _refuse_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError(13, "injected unlink failure")

    monkeypatch.setattr(Path, "unlink", _refuse_unlink)
    with pytest.raises(VaultTransportError, match="Failed releasing push lease"):
        driver.release_push_lease(lease_id)
    monkeypatch.undo()
    assert path.is_file(), "a failed release must leave the lease in place"


def test_bundle_refresh_refuses_a_blank_etag(tmp_path: Path) -> None:
    bundle = PortableBundleVaultDriver(tmp_path / "blank.nagarvault")
    lease_id = bundle.acquire_push_lease([], lease_id="bundle-blank")
    with pytest.raises(VaultTransportError):
        bundle.update_push_lease(lease_id, [], expected_etag="  ")
