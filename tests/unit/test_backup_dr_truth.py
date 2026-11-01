"""Backup DR-truth extensions: actionable errors + self-describing sidecar.

The pinned verifiability battery lives in ``tests/unit/test_maintenance_backup.py``
(task-167: round-trip proven, false-success turned into hard failure). This
file pins the disaster-recovery additions on top of it:

* every remote failure becomes a typed, operator-actionable
  ``MaintenanceOperationError`` (what / where / why / retryable / action),
  classified from the S3 error CODE — wrong credentials, wrong bucket,
  missing permission and throttling are distinguishable, never one flat
  string (mission chaos legs);
* a successful backup uploads a ``<key>.meta.json`` sidecar manifest
  recording sha256/size/timestamp/source/verification and byte-verifies the
  sidecar too — so a restore drill can prove artifact identity from the
  bucket alone (test names the sidecar contract; killing the sidecar or its
  round-trip must turn tests here red);
* Read-back failure of EITHER object (artifact or manifest) is a hard
  failure — never "uploaded anyway".

Pre-existing contract preserved: ``test_maintenance_backup.py`` (pinned key
set) keeps passing; the additions here are strictly additive.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.config import settings as settings_module
from nexus_ai_agent.maintenance import backup as backup_module
from nexus_ai_agent.maintenance.backup import SIDECAR_SUFFIX, create_backup
from nexus_ai_agent.maintenance.failures import (
    ActionableFailure,
    MaintenanceOperationError,
    classify_storage_failure,
)
from nexus_ai_agent.storage.providers.base import StorageError


@pytest.fixture(autouse=True)
def _uncached_settings():
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()


class _FakeR2:
    """In-memory provider for the repository boundary (unit seam only)."""

    name = "r2"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.configured = True
        self.upload_fails_with: Exception | None = None
        self.readback_fails_with: Exception | None = None
        self.fail_readback_keys: set[str] = set()

    def is_configured(self) -> bool:
        return self.configured

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        if self.upload_fails_with is not None and not self.objects:
            raise self.upload_fails_with
        self.objects[remote_key] = local_path.read_bytes()

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        if self.readback_fails_with is not None and remote_key in self.fail_readback_keys:
            raise self.readback_fails_with
        local_path.write_bytes(self.objects[remote_key])


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    db = tmp_path / "app.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, payload TEXT)")
        conn.execute("INSERT INTO jobs VALUES ('job-1', 'x' * 64)")
        conn.commit()
    monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
    from nexus_ai_agent.config.settings import Settings

    return Settings(db_path=str(db))


def _run_backup(tmp_path: Path, fake: _FakeR2, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(backup_module, "_build_provider", lambda settings: fake)
    return create_backup(settings=_settings(tmp_path, monkeypatch))


class TestSidecarManifest:
    def test_success_uploads_byte_verified_sidecar(self, tmp_path, monkeypatch):
        summary = _run_backup(tmp_path, _FakeR2(), monkeypatch)

        assert summary["sidecar"] == {
            "key": summary["key"] + SIDECAR_SUFFIX,
            "verified": True,
        }

    def test_sidecar_content_is_self_describing_and_matches_artifact(self, tmp_path, monkeypatch):
        fake = _FakeR2()
        summary = _run_backup(tmp_path, fake, monkeypatch)

        manifest = json.loads(fake.objects[f"{summary['key']}{SIDECAR_SUFFIX}"].decode())
        assert manifest["kind"] == "nexus.db-backup.meta/v1"
        assert manifest["sha256"] == summary["sha256"]
        assert manifest["size_bytes"] == summary["size_bytes"]
        assert manifest["timestamp"] == summary["timestamp"]
        assert manifest["source"] == summary["source"]
        # The artifact bytes in the bucket hash to exactly this manifest value:
        import hashlib

        assert hashlib.sha256(fake.objects[summary["key"]]).hexdigest() == manifest["sha256"]
        assert manifest["verification"]["roundtrip"] == "byte-identical"

    def test_sidecar_readback_failure_fails_the_backup(self, tmp_path, monkeypatch):
        # Fail ONLY the sidecar read-back: the artifact itself was verified fine.
        class _TrackUpload(_FakeR2):
            async def upload(self, *, local_path: Path, remote_key: str) -> None:
                await super().upload(local_path=local_path, remote_key=remote_key)
                if remote_key.endswith(SIDECAR_SUFFIX):
                    self.fail_readback_keys = {remote_key}

        fake = _TrackUpload()
        fake.readback_fails_with = StorageError("read denied")
        with pytest.raises(MaintenanceOperationError):
            _run_backup(tmp_path, fake, monkeypatch)


class _Coded(StorageError):
    """StorageError carrying an S3-style error code like botocore does."""

    def __init__(self, code: str):
        self.response = {"Error": {"Code": code, "Message": f"{code} happened"}}
        super().__init__(code)


class TestActionableUploadChaos:
    @pytest.mark.parametrize(
        ("code", "category", "retryable", "expect_in_action"),
        [
            ("InvalidAccessKeyId", "credentials", False, "R2_ACCESS_KEY_ID"),
            ("SignatureDoesNotMatch", "credentials", False, "R2_SECRET_ACCESS_KEY"),
            ("NoSuchBucket", "bucket", False, "R2_BUCKET"),
            ("AccessDenied", "permission", False, "permission"),
            ("SlowDown", "transient", True, "backoff"),
            ("ServiceUnavailable", "transient", True, "retry"),
        ],
    )
    def test_upload_failure_is_classified_actionable(
        self, tmp_path, monkeypatch, code, category, retryable, expect_in_action
    ):
        fake = _FakeR2()
        fake.upload_fails_with = _Coded(code)
        with pytest.raises(MaintenanceOperationError) as exc_info:
            _run_backup(tmp_path, fake, monkeypatch)

        failure: ActionableFailure = exc_info.value.failure
        assert failure.category == category
        assert failure.retryable is retryable
        assert expect_in_action in failure.operator_action
        # Actionable render answers what AND where (the object key):
        rendered = failure.render()
        assert "upload failed" in rendered
        assert "backups/db/" in rendered

    def test_network_endpoint_failure_classified_distinctly(self):
        class _EndpointBlow(StorageError):
            pass

        failure = classify_storage_failure(
            operation="upload",
            where="backups/db/x",
            exc=_EndpointBlow(
                "EndpointConnectionError: Could not connect to "
                "deadbeef.r2.cloudflarestorage.com (Temporary failure in name resolution)"
            ),
            bucket="nexus-blobs",
        )
        assert failure.category == "network"
        assert failure.retryable is True
        assert "R2_ACCOUNT_ID" in failure.operator_action

    def test_unclassifiable_failure_is_honestly_unknown_not_silent(self):
        failure = classify_storage_failure(
            operation="list",
            where="backups/db/",
            exc=StorageError("something the taxonomy has never seen"),
            bucket="b",
        )
        assert failure.category == "unknown"
        assert "classify manually" in failure.operator_action


class TestTaxonomyContract:
    def test_render_shape_is_complete(self):
        failure = ActionableFailure(
            what="download",
            where="backups/db/20250101/x.sqlite3",
            why="NoSuchKey: absent",
            category="key",
            retryable=False,
            operator_action="pick an older backup",
        )
        text = failure.render()
        for needle in ("download", "backups/db/", "NoSuchKey", "key", "false", "older backup"):
            assert needle in text

    def test_maintenance_operation_error_is_runtime_error_and_carries_failure(self):
        failure = ActionableFailure(
            what="upload",
            where="k",
            why="w",
            category="credentials",
            retryable=False,
            operator_action="rotate",
        )
        err = MaintenanceOperationError(failure)
        assert isinstance(err, RuntimeError)
        assert err.failure is failure
        assert str(err) == failure.render()

    def test_cause_chain_walking_reaches_the_coded_error(self):
        inner = _Coded("NoSuchBucket")
        outer = StorageError("provider flattened it")
        outer.__cause__ = inner
        failure = classify_storage_failure(operation="upload", where="k", exc=outer, bucket=None)
        assert failure.category == "bucket"
