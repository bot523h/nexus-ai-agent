"""Adversarial owner fencing for local and portable-bundle push leases."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


@pytest.mark.parametrize(
    "driver_type",
    [
        byos_drivers.LocalDeviceVaultDriver,
        byos_drivers.S3CompatibleSigV4Driver,
        byos_drivers.PortableBundleVaultDriver,
    ],
)
def test_recover_owned_lease_keeps_optional_etag_call_signature(driver_type: type[Any]) -> None:
    """Omitting the ETag remains callable, but the default is explicitly fail-closed."""
    parameter = inspect.signature(driver_type.recover_owned_lease).parameters["expected_etag"]
    assert parameter.default is None
    assert parameter.annotation == "str | None"


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


@pytest.mark.parametrize("backend", ["local", "bundle"])
def test_restarted_lease_owner_recovers_only_with_token_and_etag_then_releases(
    backend: str, tmp_path: Path
) -> None:
    restarted: LocalDeviceVaultDriver | PortableBundleVaultDriver
    if backend == "local":
        path = tmp_path / "restart-vault"
        local_owner = LocalDeviceVaultDriver(path)
        lease_id = local_owner.acquire_push_lease(
            [], project_id="project-1", lease_id="restart-owner"
        )
        credential = local_owner.export_lease_credential(lease_id)
        raw = local_owner._lease_path(lease_id).read_bytes()
        restarted = LocalDeviceVaultDriver(path)
    else:
        path = tmp_path / "restart.nagarvault"
        bundle_owner = PortableBundleVaultDriver(path)
        lease_id = bundle_owner.acquire_push_lease(
            [], project_id="project-1", lease_id="restart-owner"
        )
        credential = bundle_owner.export_lease_credential(lease_id)
        bundle_owner._read_locked()
        raw = bundle_owner._leases[lease_id]
        restarted = PortableBundleVaultDriver(path)

    assert credential["lease_token"] not in raw.decode("utf-8")
    assert '"lease_token"' not in raw.decode("utf-8")
    assert byos_drivers._parse_lease_token_verifier(raw)
    with pytest.raises(VaultRevisionConflictError):
        restarted.release_push_lease(lease_id)
    with pytest.raises(VaultTransportError):
        restarted.export_lease_credential(lease_id)
    with pytest.raises(VaultTransportError):
        restarted.recover_owned_lease(
            "wrong-project", lease_id, credential["lease_token"], credential["etag"]
        )
    with pytest.raises(VaultTransportError):
        restarted.recover_owned_lease("project-1", lease_id, "stranger-token", credential["etag"])
    with pytest.raises(VaultTransportError):
        restarted.recover_owned_lease("project-1", lease_id, credential["lease_token"], "0" * 64)
    assert (
        restarted.recover_owned_lease(
            "project-1",
            lease_id,
            credential["lease_token"],
            credential["etag"],
        )
        == credential["etag"]
    )
    assert restarted.release_push_lease(lease_id)
    with pytest.raises(VaultTransportError):
        restarted.recover_owned_lease(
            "project-1",
            lease_id,
            credential["lease_token"],
            credential["etag"],
        )


def _s3_ambiguous_fixture(
    failure_mode: str,
) -> tuple[byos_drivers.S3CompatibleSigV4Driver, dict[str, Any], str, str]:
    lease_id = "ambiguous-put"
    prior_token = "original-owner-token"
    prior_body = byos_drivers._build_lease_json_bytes(
        lease_id=lease_id,
        project_id="project-1",
        now_epoch=1_800_000_000.0,
        ttl_seconds=3600,
        chunk_ids=[],
        lease_token=prior_token,
    )
    state: dict[str, Any] = {
        "body": prior_body,
        "etag": hashlib.sha256(prior_body).hexdigest(),
        "puts": 0,
        "failure_mode": failure_mode,
    }
    config = byos_drivers.S3SigV4Config(
        endpoint_url="https://account.example.com",
        bucket="leases",
        region="us-east-1",
        access_key_id="access",
        secret_access_key="secret",
    )

    def transport(request: byos_drivers.S3HttpRequest) -> byos_drivers.S3HttpResponse:
        method = request.method.upper()
        if method == "HEAD":
            return byos_drivers.S3HttpResponse(
                status_code=200,
                headers={"etag": str(state["etag"])},
            )
        if method == "GET":
            if failure_mode == "get-fails" and int(state["puts"]) > 0:
                return byos_drivers.S3HttpResponse(status_code=503)
            return byos_drivers.S3HttpResponse(
                status_code=200,
                body=bytes(state["body"]),
                headers={"etag": str(state["etag"])},
            )
        if method == "PUT":
            state["puts"] = int(state["puts"]) + 1
            if failure_mode == "not-committed":
                raise VaultTransportError("injected lost PUT before commit")
            if failure_mode == "competitor":
                rival = byos_drivers._build_lease_json_bytes(
                    lease_id=lease_id,
                    project_id="project-1",
                    now_epoch=1_800_000_000.0,
                    ttl_seconds=3600,
                    chunk_ids=[],
                    lease_token="rival-owner-token",
                )
                state["body"] = rival
                state["etag"] = hashlib.sha256(rival).hexdigest()
                raise VaultTransportError("injected lost PUT after rival replacement")
            if request.headers.get("if-match") != state["etag"]:
                return byos_drivers.S3HttpResponse(status_code=412)
            state["body"] = request.body
            state["etag"] = hashlib.sha256(request.body).hexdigest()
            if failure_mode in ("committed", "get-fails"):
                raise VaultTransportError("injected lost successful PUT response")
            return byos_drivers.S3HttpResponse(
                status_code=200, headers={"etag": str(state["etag"])}
            )
        if method == "DELETE":
            if request.headers.get("if-match") == state["etag"]:
                state["body"] = b""
                state["etag"] = ""
                return byos_drivers.S3HttpResponse(status_code=204)
            return byos_drivers.S3HttpResponse(status_code=412)
        return byos_drivers.S3HttpResponse(status_code=404)

    driver = byos_drivers.S3CompatibleSigV4Driver(config, http_transport=transport)
    obj_key = driver._object_key("leases", f"{lease_id}.json")
    driver._owned_lease_etags[lease_id] = str(state["etag"])
    driver._owned_lease_tokens[lease_id] = prior_token
    state["object_key"] = obj_key
    return driver, state, lease_id, prior_token


def test_restarted_s3_owner_recovers_only_with_token_then_releases() -> None:
    driver, state, lease_id, token = _s3_ambiguous_fixture("safe")
    restarted = byos_drivers.S3CompatibleSigV4Driver(
        driver.config, http_transport=driver._transport
    )
    with pytest.raises(VaultRevisionConflictError):
        restarted.release_push_lease(lease_id)
    with pytest.raises(VaultTransportError):
        restarted.export_lease_credential(lease_id)
    with pytest.raises(VaultTransportError):
        restarted.recover_owned_lease("wrong-project", lease_id, token, state["etag"])
    with pytest.raises(VaultTransportError):
        restarted.recover_owned_lease("project-1", lease_id, "stranger-token", state["etag"])
    with pytest.raises(VaultTransportError):
        restarted.recover_owned_lease("project-1", lease_id, token, "0" * 64)
    body = bytes(state["body"]).decode("utf-8")
    assert token not in body
    assert '"lease_token"' not in body
    assert (
        restarted.recover_owned_lease("project-1", lease_id, token, state["etag"]) == state["etag"]
    )
    restarted.release_push_lease(lease_id)
    assert state["body"] == b""


def test_s3_ambiguous_put_committed_response_lost_reconciles_and_releases() -> None:
    driver, state, lease_id, _prior_token = _s3_ambiguous_fixture("committed")
    driver.update_push_lease(lease_id, [], project_id="project-1")
    written_token = byos_drivers._parse_lease_token_verifier(bytes(state["body"]))
    assert written_token
    assert driver._owned_lease_tokens[lease_id] != written_token
    assert driver._owned_lease_etags[lease_id] == str(state["etag"])
    driver.release_push_lease(lease_id)
    assert state["body"] == b""


def test_s3_ambiguous_put_not_committed_fails_without_claiming_prior() -> None:
    driver, state, lease_id, prior_token = _s3_ambiguous_fixture("not-committed")
    prior_etag = str(state["etag"])
    with pytest.raises(VaultTransportError, match="prior version remains"):
        driver.update_push_lease(lease_id, [], project_id="project-1")
    assert driver._owned_lease_etags[lease_id] == prior_etag
    assert driver._owned_lease_tokens[lease_id] == prior_token


def test_s3_ambiguous_put_never_claims_competing_writer() -> None:
    driver, state, lease_id, _prior_token = _s3_ambiguous_fixture("competitor")
    with pytest.raises(VaultRevisionConflictError, match="another writer"):
        driver.update_push_lease(lease_id, [], project_id="project-1")
    rival_verifier = byos_drivers._compute_lease_token_verifier(
        "project-1", lease_id, "rival-owner-token"
    )
    assert byos_drivers._parse_lease_token_verifier(bytes(state["body"])) == rival_verifier
    assert driver._owned_lease_etags[lease_id] != state["etag"]


def test_s3_ambiguous_put_reconciliation_get_failure_fails_closed() -> None:
    driver, state, lease_id, prior_token = _s3_ambiguous_fixture("get-fails")
    prior_etag = str(driver._owned_lease_etags[lease_id])
    with pytest.raises(VaultTransportError, match="reconciliation read failed"):
        driver.update_push_lease(lease_id, [], project_id="project-1")
    assert driver._owned_lease_etags[lease_id] == prior_etag
    assert driver._owned_lease_tokens[lease_id] == prior_token
    assert state["body"] != b""


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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes require os.mkfifo")
@pytest.mark.parametrize("operation", ["read", "scan", "refresh", "release"])
def test_fifo_lease_is_nonblocking_rejected_and_never_unlinked(
    operation: str, tmp_path: Path
) -> None:
    import stat
    import time

    driver = LocalDeviceVaultDriver(tmp_path / "fifo-vault")
    lease_path = driver._lease_path("fifo-no-writer")
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(lease_path)

    started = time.monotonic()
    with pytest.raises(VaultTransportError):
        if operation == "read":
            byos_drivers._read_lease_file_once(lease_path)
        elif operation == "scan":
            driver.read_active_leased_chunks()
        elif operation == "refresh":
            driver.update_push_lease("fifo-no-writer", [])
        else:
            driver.release_push_lease("fifo-no-writer")
    assert time.monotonic() - started < 0.5
    assert stat.S_ISFIFO(lease_path.lstat().st_mode)
    assert lease_path.exists(), "a FIFO is untrusted input, never a lease to clean up"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes require os.mkfifo")
@pytest.mark.parametrize("operation", ["read", "refresh", "release"])
def test_fifo_portable_bundle_is_nonblocking_rejected_and_never_unlinked(
    operation: str, tmp_path: Path
) -> None:
    import stat
    import time

    bundle_path = tmp_path / "fifo-bundle.nagarvault"
    driver = PortableBundleVaultDriver(bundle_path)
    os.mkfifo(bundle_path)

    started = time.monotonic()
    with pytest.raises(VaultTransportError):
        if operation == "read":
            driver.read_active_leased_chunks()
        elif operation == "refresh":
            driver.update_push_lease("fifo-bundle", [])
        else:
            driver.release_push_lease("fifo-bundle")
    assert time.monotonic() - started < 0.5
    assert stat.S_ISFIFO(bundle_path.lstat().st_mode)
    assert bundle_path.exists()


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


@pytest.mark.skipif(
    byos_drivers._FCNTL is None or not hasattr(os, "O_NONBLOCK"),
    reason="POSIX O_NONBLOCK fail-closed gate",
)
@pytest.mark.parametrize("backend", ["local", "bundle"])
def test_posix_missing_o_nonblock_fails_closed(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never fall back to a potentially blocking open when POSIX lacks O_NONBLOCK."""
    import os

    if backend == "local":
        local_driver = LocalDeviceVaultDriver(tmp_path / "missing-nonblock")
        lease_id = local_driver.acquire_push_lease([], lease_id="nonblock-required")
        target = local_driver._lease_path(lease_id)
    else:
        bundle_driver = PortableBundleVaultDriver(tmp_path / "missing-nonblock.nagarvault")
        bundle_driver.acquire_push_lease([], lease_id="nonblock-required")
        target = None

    monkeypatch.delattr(os, "O_NONBLOCK")
    with pytest.raises(VaultTransportError, match="O_NONBLOCK"):
        if backend == "local":
            assert target is not None
            byos_drivers._read_lease_file_once(target)
        else:
            bundle_driver.verify_bundle()


@pytest.mark.parametrize("backend", ["local", "bundle"])
def test_recovery_kills_raw_token_verifier_mutant(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw-token-as-verifier implementation cannot recover a hashed lease record."""
    if backend == "local":
        owner: LocalDeviceVaultDriver | PortableBundleVaultDriver = LocalDeviceVaultDriver(
            tmp_path / "raw-token-mutant"
        )
        lease_id = owner.acquire_push_lease([], project_id="project-1", lease_id="raw-token-mutant")
    else:
        owner = PortableBundleVaultDriver(tmp_path / "raw-token-mutant.nagarvault")
        lease_id = owner.acquire_push_lease([], project_id="project-1", lease_id="raw-token-mutant")
    credential = owner.export_lease_credential(lease_id)
    restarted: LocalDeviceVaultDriver | PortableBundleVaultDriver
    restarted = (
        LocalDeviceVaultDriver(tmp_path / "raw-token-mutant")
        if backend == "local"
        else PortableBundleVaultDriver(tmp_path / "raw-token-mutant.nagarvault")
    )

    monkeypatch.setattr(
        byos_drivers,
        "_compute_lease_token_verifier",
        lambda _project, _lease, token: token,
    )
    with pytest.raises(VaultTransportError, match="owner token did not verify"):
        restarted.recover_owned_lease(
            "project-1",
            lease_id,
            credential["lease_token"],
            credential["etag"],
        )


def test_lease_owner_helpers_fail_closed_on_malformed_and_wrong_owner_data() -> None:
    """Malformed JSON, missing verifiers, and mismatched owner IDs prove nothing."""
    raw = byos_drivers._build_lease_json_bytes(
        lease_id="helper-owner",
        project_id="project-1",
        now_epoch=1_900_000_000.0,
        ttl_seconds=60.0,
        chunk_ids=[],
        lease_token="private-helper-token",
    )
    assert byos_drivers._lease_owner_fields(b"not-json") == ("", "", "")
    assert byos_drivers._lease_owner_fields(b"[]") == ("", "", "")
    assert not byos_drivers._lease_record_matches_owner(
        raw,
        project_id="different-project",
        lease_id="helper-owner",
        lease_token="private-helper-token",
    )
    with pytest.raises(VaultTransportError, match="requested owner"):
        byos_drivers._lease_verifier_for_recovery(
            raw,
            project_id="different-project",
            lease_id="helper-owner",
            source_label="test lease",
        )

    payload = json.loads(raw)
    payload.pop("lease_token_verifier")
    without_verifier = json.dumps(payload).encode("utf-8")
    assert byos_drivers._lease_owner_fields(without_verifier)[2] == ""
    with pytest.raises(VaultTransportError, match="no recoverable owner verifier"):
        byos_drivers._lease_verifier_for_recovery(
            without_verifier,
            project_id="project-1",
            lease_id="helper-owner",
            source_label="legacy lease",
        )

    assert byos_drivers._owned_lease_pair({"id": "secret"}, {"id": "etag"}, "id", "wrong") is None
    with pytest.raises(VaultTransportError, match="non-empty expected ETag"):
        byos_drivers._owned_lease_pair({"id": "secret"}, {"id": "etag"}, "id", " ")
