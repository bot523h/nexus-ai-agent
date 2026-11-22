"""Adversarial owner fencing for local and portable-bundle push leases."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from nexus_ai_agent.storage.personal_cloud_vault.byos_drivers import (
    LocalDeviceVaultDriver,
    PortableBundleVaultDriver,
)
from nexus_ai_agent.storage.personal_cloud_vault.errors import (
    VaultRevisionConflictError,
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
