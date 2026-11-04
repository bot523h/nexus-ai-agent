"""Unit tests for the Backblaze B2 provider and the blob-tier selection.

Every test runs fully offline: the boto3 S3 client is replaced by an in-memory
stub that mirrors exactly the calls :class:`B2Provider` makes (``upload_file``,
``download_file``, ``list_objects_v2``, ``delete_objects``). No network, and no
credential ever leaves the process.

Two contracts are pinned here:

1. **Fail closed before the network.** A malformed or partial ``NEXUS_B2_*``
   configuration must raise a typed, actionable error *naming the setting* —
   never a bare boto3 ``ParamValidationError`` from deep inside the SDK, and
   never a silent fallback to another store.
2. **Tier selection is explicit.** ``build_blob_provider`` prefers a complete B2
   tier, falls back to R2, and refuses a *partial* B2 (loud, not silent).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from nexus_ai_agent.config.settings import Settings
from nexus_ai_agent.maintenance.backup import build_blob_provider
from nexus_ai_agent.storage.providers.b2 import (
    BACKUP_PREFIX,
    B2Provider,
    endpoint_region,
    validate_endpoint,
)
from nexus_ai_agent.storage.providers.base import ProviderUnavailable, StorageError
from nexus_ai_agent.storage.providers.r2 import R2Provider

ENDPOINT = "https://s3.us-east-005.backblazeb2.com"
REGION = "us-east-005"


# ── in-memory boto3 stub ──────────────────────────────────────────────


class FakeB2Client:
    """Mimics the boto3 S3 client surface used by B2Provider (no network)."""

    def __init__(
        self,
        *,
        page_size: int = 2,
        omit_truncated_token: bool = False,
        delete_errors: list[dict[str, str]] | None = None,
    ) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.upload_calls: list[tuple[str, str, str]] = []
        self._page_size = page_size
        self._omit_truncated_token = omit_truncated_token
        self._delete_errors = delete_errors or []

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.upload_calls.append((filename, bucket, key))
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        if key not in self.objects:
            raise KeyError(key)
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        Path(filename).write_bytes(self.objects[key])

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "", **kwargs: object) -> dict:
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = 0
        token = kwargs.get("ContinuationToken")
        if isinstance(token, str) and token:
            start = int(token)
        page = keys[start : start + self._page_size]
        truncated = start + self._page_size < len(keys)
        response: dict = {
            "Contents": [{"Key": k} for k in page],
            "IsTruncated": truncated,
        }
        if truncated and not self._omit_truncated_token:
            response["NextContinuationToken"] = str(start + self._page_size)
        return response

    def delete_objects(self, *, Bucket: str, Delete: dict) -> dict:
        objects = Delete["Objects"]
        failed = {error.get("Key") for error in self._delete_errors}
        for obj in objects:
            if obj["Key"] in failed:
                continue
            self.objects.pop(obj["Key"], None)
            self.deleted.append(obj["Key"])
        response: dict = {"Deleted": []}
        if self._delete_errors:
            response["Errors"] = self._delete_errors
        return response


def make_provider(**overrides: object) -> tuple[B2Provider, FakeB2Client]:
    client = FakeB2Client()
    return (
        B2Provider(
            key_id=cast(str, overrides.get("key_id", "keyid")),
            application_key=cast(str, overrides.get("application_key", "appkey")),
            bucket=cast(str, overrides.get("bucket", "nexus-backups")),
            endpoint=cast(str, overrides.get("endpoint", ENDPOINT)),
            region=cast(str, overrides.get("region", REGION)),
            client=overrides.get("client", client),
        ),
        client,
    )


def _settings(**overrides: object) -> Settings:
    base: dict[str, Any] = dict(
        b2_key_id="keyid",
        b2_application_key="appkey",
        b2_bucket="nexus-backups",
        b2_endpoint=ENDPOINT,
        b2_region=REGION,
    )
    base.update(overrides)
    return Settings(**base)


# ── endpoint validation ───────────────────────────────────────────────


def test_validate_endpoint_accepts_https_b2_host() -> None:
    assert validate_endpoint(ENDPOINT) == "s3.us-east-005.backblazeb2.com"
    assert endpoint_region(ENDPOINT) == REGION


@pytest.mark.parametrize(
    "bad",
    [
        "http://s3.us-east-005.backblazeb2.com",  # not https
        "https://s3.amazonaws.com",  # not a B2 host (credential-exfil boundary)
        "https://evil.example.com",  # arbitrary host
        "https://user:pass@s3.us-east-005.backblazeb2.com",  # embedded creds
        "https://s3.us-east-005.backblazeb2.com/some/path",  # path
        "https://s3.us-east-005.backblazeb2.com?x=1",  # query
        "not-a-url",
        "https://foo.backblazeb2.com",  # B2 suffix but no region to verify
        "https://backblazeb2.com",  # bare apex, no region
        "https://s3.backblazeb2.com",  # no region label
    ],
)
def test_validate_endpoint_rejects_unsafe_endpoints(bad: str) -> None:
    with pytest.raises(ProviderUnavailable):
        validate_endpoint(bad)


def test_a_b2_suffix_host_without_a_region_is_refused() -> None:
    """A ``*.backblazeb2.com`` host with no ``s3.<region>`` shape must fail closed.

    Otherwise the endpoint and region could disagree with no way to notice, and
    boto3 would be handed the application key at a host whose region is unknown.
    """
    provider, _ = make_provider(endpoint="https://foo.backblazeb2.com")
    assert provider.is_configured() is False
    reason = provider.configuration_error()
    assert reason is not None and "NEXUS_B2_ENDPOINT" in reason


# ── is_configured / configuration_error ───────────────────────────────


def test_is_configured_true_with_a_complete_valid_configuration() -> None:
    provider, _ = make_provider()
    assert provider.is_configured() is True
    assert provider.configuration_error() is None


@pytest.mark.parametrize(
    "field, setting",
    [
        ("key_id", "NEXUS_B2_KEY_ID"),
        ("application_key", "NEXUS_B2_APPLICATION_KEY"),
        ("bucket", "NEXUS_B2_BUCKET"),
        ("endpoint", "NEXUS_B2_ENDPOINT"),
        ("region", "NEXUS_B2_REGION"),
    ],
)
def test_a_missing_field_is_not_configured_and_names_the_setting(field: str, setting: str) -> None:
    provider, _ = make_provider(**{field: ""})
    assert provider.is_configured() is False
    reason = provider.configuration_error()
    assert reason is not None and setting in reason, (
        f"the refusal must name the missing setting {setting}, got {reason!r}"
    )


def test_endpoint_region_mismatch_is_refused() -> None:
    provider, _ = make_provider(region="us-west-004")  # endpoint says us-east-005
    assert provider.is_configured() is False
    reason = provider.configuration_error()
    assert reason is not None
    assert "NEXUS_B2_REGION" in reason and "NEXUS_B2_ENDPOINT" in reason


@pytest.mark.parametrize(
    "bad_bucket",
    [
        "NEXUS-Backups",  # uppercase
        "short",  # < 6 chars
        "-leading-hyphen",  # must start alphanumeric
        "trailing-",  # must end alphanumeric
        "has_underscore",  # underscore invalid
        "  spaced  ",  # whitespace
    ],
)
def test_an_invalid_bucket_is_refused_naming_the_setting(bad_bucket: str) -> None:
    provider, _ = make_provider(bucket=bad_bucket)
    assert provider.is_configured() is False
    reason = provider.configuration_error()
    assert reason is not None and "NEXUS_B2_BUCKET" in reason


def test_unconfigured_provider_raises_before_any_client_call() -> None:
    provider, _ = make_provider(key_id="")
    assert provider.is_configured() is False
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.upload(local_path=Path("/tmp/x"), remote_key=f"{BACKUP_PREFIX}x"))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.download(remote_key=f"{BACKUP_PREFIX}x", local_path=Path("/tmp/x")))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.list_files(prefix=BACKUP_PREFIX))


# ── upload / download / list / delete ────────────────────────────────


def test_upload_and_download_roundtrip(tmp_path: Path) -> None:
    provider, client = make_provider()
    src = tmp_path / "dump.sql"
    src.write_bytes(b"nexus-b2-payload")
    dst = tmp_path / "out" / "dump.sql"
    key = f"{BACKUP_PREFIX}20261104-000000/nexus-sqlite.sqlite3"

    asyncio.run(provider.upload(local_path=src, remote_key=key))
    assert client.objects[key] == b"nexus-b2-payload"
    assert client.upload_calls[0][1] == "nexus-backups"

    asyncio.run(provider.download(remote_key=key, local_path=dst))
    assert dst.read_bytes() == b"nexus-b2-payload"


def test_download_missing_key_raises_storage_error(tmp_path: Path) -> None:
    provider, _ = make_provider()
    with pytest.raises(StorageError):
        asyncio.run(provider.download(remote_key=f"{BACKUP_PREFIX}nope", local_path=tmp_path / "x"))


def test_list_files_paginates() -> None:
    provider, client = make_provider()  # stub page size = 2
    client.objects = {f"{BACKUP_PREFIX}{i}.sql": b"" for i in range(5)}

    keys = asyncio.run(provider.list_files(prefix=BACKUP_PREFIX))
    assert keys == sorted(client.objects)
    assert len(keys) == 5  # more than one page → pagination loop exercised


def test_list_files_fails_closed_when_truncated_page_has_no_token() -> None:
    client = FakeB2Client(page_size=2, omit_truncated_token=True)
    client.objects = {f"{BACKUP_PREFIX}{i}.sql": b"" for i in range(3)}
    provider, _ = make_provider(client=client)

    with pytest.raises(StorageError):
        asyncio.run(provider.list_files(prefix=BACKUP_PREFIX))


def test_delete_objects_reports_partial_provider_errors_without_fake_success() -> None:
    client = FakeB2Client(delete_errors=[{"Key": f"{BACKUP_PREFIX}b", "Code": "AccessDenied"}])
    provider, _ = make_provider(client=client)
    client.objects = {f"{BACKUP_PREFIX}a": b"", f"{BACKUP_PREFIX}b": b""}

    with pytest.raises(StorageError, match="partial"):
        asyncio.run(provider.delete_objects(keys=[f"{BACKUP_PREFIX}a", f"{BACKUP_PREFIX}b"]))


def test_delete_objects_batches_and_counts(tmp_path: Path) -> None:
    provider, client = make_provider()
    keys = [f"{BACKUP_PREFIX}{i}" for i in range(1500)]
    client.objects = {k: b"" for k in keys}

    deleted = asyncio.run(provider.delete_objects(keys=keys))
    assert deleted == 1500
    assert sorted(client.deleted) == sorted(keys)


def test_delete_objects_empty_is_noop() -> None:
    provider, _ = make_provider()
    assert asyncio.run(provider.delete_objects(keys=[])) == 0


# ── key confinement (no cross-namespace access) ───────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "rag-docs/secret.pdf",  # sibling namespace
        "user-files/photo.jpg",  # user files must never be reachable here
        f"{BACKUP_PREFIX}../escape.sql",  # traversal
        "backups/db",  # not under the trailing-slash prefix
    ],
)
def test_keys_outside_the_backup_prefix_are_refused(key: str) -> None:
    provider, client = make_provider()
    src = Path("/tmp/x")
    with pytest.raises(StorageError):
        asyncio.run(provider.upload(local_path=src, remote_key=key))
    with pytest.raises(StorageError):
        asyncio.run(provider.download(remote_key=key, local_path=Path("/tmp/y")))
    with pytest.raises(StorageError):
        asyncio.run(provider.delete_objects(keys=[key]))
    assert client.upload_calls == []


def test_list_prefix_outside_the_backup_namespace_is_refused() -> None:
    provider, _ = make_provider()
    with pytest.raises(StorageError):
        asyncio.run(provider.list_files(prefix="rag-docs/"))


@pytest.mark.parametrize("prefix", ["", "backups/db", "backups/db/"])
def test_list_files_normalizes_the_prefix_to_the_backup_namespace(prefix: str) -> None:
    """A bare/empty prefix must not leak sibling namespaces into the listing.

    ``backups/db`` must be sent as ``backups/db/`` (so ``backups/db-old`` is
    excluded) and an empty prefix must resolve to the backup namespace rather
    than listing the whole bucket.
    """
    provider, client = make_provider()
    client.objects = {
        f"{BACKUP_PREFIX}20260101.sql": b"",
        "backups/db-old/stale.sql": b"",  # sibling namespace, must be excluded
        "rag-docs/secret.pdf": b"",
    }
    keys = asyncio.run(provider.list_files(prefix=prefix))
    assert keys == [f"{BACKUP_PREFIX}20260101.sql"]


# ── the lazily-built boto3 client is configured for B2 ────────────────


def test_lazy_client_uses_b2_endpoint_region_and_checksum_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_client(service: str, **kwargs: Any) -> object:
        captured["service"] = service
        captured.update(kwargs)
        return FakeB2Client()

    monkeypatch.setattr("boto3.client", fake_client)
    provider = B2Provider(
        key_id="keyid",
        application_key="appkey",
        bucket="nexus-backups",
        endpoint=ENDPOINT,
        region=REGION,
    )
    _ = provider.client  # force lazy construction

    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == ENDPOINT
    assert captured["region_name"] == REGION
    assert captured["aws_access_key_id"] == "keyid"
    assert captured["aws_secret_access_key"] == "appkey"
    # B2 rejects the default data-integrity checksum headers.
    config = captured["config"]
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"


def test_from_settings_reads_only_the_five_nexus_names() -> None:
    provider = B2Provider.from_settings(_settings())
    assert provider.is_configured() is True


# ── tier selection: build_blob_provider ───────────────────────────────


def test_factory_prefers_a_complete_b2_tier() -> None:
    provider = build_blob_provider(_settings())
    assert isinstance(provider, B2Provider)


def test_factory_falls_back_to_r2_when_b2_is_absent() -> None:
    provider = build_blob_provider(Settings())
    assert isinstance(provider, R2Provider)


def test_factory_refuses_a_partial_b2_configuration() -> None:
    """Half a B2 config is a mistake, not a silent R2 fallback."""
    settings = _settings(b2_endpoint="", b2_region="")
    with pytest.raises(ProviderUnavailable) as exc_info:
        build_blob_provider(settings)
    assert "NEXUS_B2_ENDPOINT" in str(exc_info.value)


def test_factory_uses_a_complete_r2_tier_when_b2_is_partial() -> None:
    """A complete R2 tier makes a partial B2 explicit, not fatal (preflight parity)."""
    settings = _settings(
        b2_key_id="keyid",
        b2_bucket="nexus-backups",
        b2_endpoint="",
        b2_region="",
        r2_account_id="acct",
        r2_access_key_id="ak",
        r2_secret_access_key="sk",
        r2_bucket="bucket",
    )
    provider = build_blob_provider(settings)
    assert isinstance(provider, R2Provider)


def test_factory_reports_the_invalid_endpoint_when_b2_is_partial_and_no_r2() -> None:
    settings = _settings(b2_endpoint="http://insecure.example.com")
    with pytest.raises(ProviderUnavailable) as exc_info:
        build_blob_provider(settings)
    assert "https" in str(exc_info.value)
