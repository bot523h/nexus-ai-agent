"""Neon Object Storage adapter tests (task-272) — offline, injected client.

A fake S3 client stands in for the network so every branch is deterministic:
configuration refusal, path-style/SigV4 client arguments, idempotent publish,
conflict on a reused key, bounded retry, non-retry of a 403, checksum
verification, soft-delete accounting and the honest ``UNKNOWN`` capacity.

What these tests prove is adapter behaviour, **not** a live connection. A live
round-trip needs a Neon branch with object storage enabled plus a
``storage:read``/``storage:write`` credential; that is recorded as BLOCKED in the
PR, because a mocked test cannot stand in for it.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any

import pytest

from nexus_ai_agent.storage.neon_object_storage import (
    META_SHA256,
    NeonObjectStorage,
    NeonStorageConfig,
    build_neon_object_store,
)
from nexus_ai_agent.storage.object_store import (
    AuthorizationDenied,
    BackendNotConfigured,
    CapacityKind,
    IntegrityMismatch,
    ObjectConflict,
    ObjectNotFound,
    ObjectStoreError,
    ObjectTooLarge,
    TransientBackendError,
    content_key_for,
)

ENDPOINT = "https://br-winter-pond-aptw82ef.storage.c-2.us-east-2.aws.neon.tech"
SECRET = "nsk_live_super_secret_value"
ACCESS_KEY = "nak_live_access_key_value"


class ProviderError(Exception):
    """Stands in for ``botocore.exceptions.ClientError`` (only ``.response`` matters)."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(f"{code} {status}")
        self.response = {
            "Error": {"Code": code, "Message": "provider detail that must not leak"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._cursor = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self._cursor >= len(self._data):
            return b""
        end = len(self._data) if size < 0 else self._cursor + size
        chunk = self._data[self._cursor : end]
        self._cursor += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeS3:
    """Minimal S3 surface the adapter uses, with failure injection."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.put_calls: list[str] = []
        self.failures: list[ProviderError] = []
        self.fail_always: ProviderError | None = None
        self.upload_fileobj_calls: list[str] = []

    # -- failure injection -------------------------------------------------- #
    def _maybe_fail(self) -> None:
        if self.fail_always is not None:
            raise self.fail_always
        if self.failures:
            raise self.failures.pop(0)

    # -- S3 surface --------------------------------------------------------- #
    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail()
        body = kwargs["Body"]
        data = body.read()
        key = kwargs["Key"]
        self.objects[key] = {
            "Body": data,
            "Metadata": dict(kwargs.get("Metadata") or {}),
            "ContentType": kwargs.get("ContentType"),
            "ETag": '"etag-"',
        }
        self.put_calls.append(key)
        return {"ETag": '"etag-"'}

    def upload_fileobj(self, fileobj: Any, bucket: str, key: str, **kwargs: Any) -> None:
        self._maybe_fail()
        data = fileobj.read()
        self.objects[key] = {
            "Body": data,
            "Metadata": dict((kwargs.get("ExtraArgs") or {}).get("Metadata") or {}),
            "ContentType": (kwargs.get("ExtraArgs") or {}).get("ContentType"),
            "ETag": '"etag-multipart"',
        }
        self.put_calls.append(key)
        self.upload_fileobj_calls.append(key)

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail()
        key = kwargs["Key"]
        entry = self.objects.get(key)
        if entry is None:
            raise ProviderError("404", 404)
        return {
            "ContentLength": len(entry["Body"]),
            "Metadata": entry["Metadata"],
            "ContentType": entry["ContentType"],
            "ETag": entry["ETag"],
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail()
        key = kwargs["Key"]
        entry = self.objects.get(key)
        if entry is None:
            raise ProviderError("NoSuchKey", 404)
        return {"Body": FakeBody(entry["Body"]), "Metadata": entry["Metadata"]}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail()
        self.objects.pop(kwargs["Key"], None)
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail()
        prefix = kwargs.get("Prefix", "")
        contents = [
            {"Key": key, "Size": len(entry["Body"])}
            for key, entry in sorted(self.objects.items())
            if key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_fail()
        return {}


def make_store(**overrides: Any) -> tuple[NeonObjectStorage, FakeS3]:
    client = FakeS3()
    config = NeonStorageConfig(
        endpoint_url=ENDPOINT,
        region="us-east-2",
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET,
        bucket="nagar-assets",
        **overrides,
    )
    return NeonObjectStorage(config, client=client, sleep=lambda _seconds: None), client


# --------------------------------------------------------------------------- #
# Configuration: fail closed, never at import time
# --------------------------------------------------------------------------- #
def test_from_env_returns_none_when_any_credential_is_missing() -> None:
    complete = {
        "NEXUS_NEON_STORAGE_ENDPOINT": ENDPOINT,
        "NEXUS_NEON_STORAGE_REGION": "us-east-2",
        "NEXUS_NEON_STORAGE_ACCESS_KEY_ID": ACCESS_KEY,
        "NEXUS_NEON_STORAGE_SECRET_ACCESS_KEY": SECRET,
        "NEXUS_NEON_STORAGE_BUCKET": "nagar-assets",
    }
    assert build_neon_object_store(complete) is not None
    for missing in complete:
        partial = dict(complete)
        partial.pop(missing)
        assert build_neon_object_store(partial) is None, (
            f"an incomplete environment ({missing} absent) must disable the feature, not raise"
        )


def test_a_host_aws_configuration_cannot_capture_nagar_files() -> None:
    """A generic AWS setup must disable the feature, not point it at Amazon S3."""
    host_only = {
        "AWS_ENDPOINT_URL_S3": "https://s3.amazonaws.com",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "aws-key",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "NEXUS_NEON_STORAGE_BUCKET": "nagar-assets",
    }
    assert build_neon_object_store(host_only) is None

    neon_aliases = {
        "AWS_ENDPOINT_URL_S3": ENDPOINT,
        "AWS_REGION": "us-east-2",
        "AWS_ACCESS_KEY_ID": "aws-key",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "NEXUS_NEON_STORAGE_BUCKET": "nagar-assets",
    }
    store = build_neon_object_store(neon_aliases)
    assert store is not None
    assert store.config.endpoint_url == ENDPOINT

    # ... and the explicit NEXUS names win over the aliases.
    explicit = dict(neon_aliases)
    explicit.update(
        {
            "NEXUS_NEON_STORAGE_ENDPOINT": ENDPOINT,
            "NEXUS_NEON_STORAGE_REGION": "us-east-2",
            "NEXUS_NEON_STORAGE_ACCESS_KEY_ID": ACCESS_KEY,
            "NEXUS_NEON_STORAGE_SECRET_ACCESS_KEY": SECRET,
            "AWS_ENDPOINT_URL_S3": "https://other-branch.storage.c-2.us-east-2.aws.neon.tech",
        }
    )
    store = build_neon_object_store(explicit)
    assert store is not None
    assert store.config.access_key_id == ACCESS_KEY


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://br-x.storage.c-2.us-east-2.aws.neon.tech",
        "https://s3.us-east-2.amazonaws.com",
        "https://bucket.r2.cloudflarestorage.com",
    ],
)
def test_non_neon_or_insecure_endpoints_are_refused(endpoint: str) -> None:
    with pytest.raises(ValueError):
        NeonStorageConfig(
            endpoint_url=endpoint,
            region="us-east-2",
            access_key_id=ACCESS_KEY,
            secret_access_key=SECRET,
            bucket="nagar-assets",
        )


def test_config_repr_and_redaction_hide_credentials() -> None:
    config = NeonStorageConfig(
        endpoint_url=ENDPOINT,
        region="us-east-2",
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET,
        bucket="nagar-assets",
    )
    assert SECRET not in repr(config)
    assert ACCESS_KEY not in repr(config)
    redacted = config.redacted()
    assert redacted["access_key_id"] == "***"
    assert redacted["secret_access_key"] == "***"
    assert redacted["bucket"] == "nagar-assets"


def test_client_arguments_use_path_style_and_sigv4() -> None:
    store, _ = make_store()
    kwargs = store._client_kwargs()
    config = kwargs["config"]
    assert kwargs["endpoint_url"] == ENDPOINT
    assert kwargs["region_name"] == "us-east-2"
    assert config.s3 == {"addressing_style": "path"}, "Neon rejects virtual-hosted addressing"
    assert config.signature_version == "s3v4", "Neon rejects SigV2"
    assert config.request_checksum_calculation == "when_required"


def test_an_unconfigured_backend_refuses_work_with_a_typed_error() -> None:
    """The guard is reachable even though the dataclass forbids empty credentials."""

    class _Unconfigured(NeonObjectStorage):
        def is_configured(self) -> bool:
            return False

    store = _Unconfigured(
        NeonStorageConfig(
            endpoint_url=ENDPOINT,
            region="us-east-2",
            access_key_id=ACCESS_KEY,
            secret_access_key=SECRET,
            bucket="nagar-assets",
        ),
        client=FakeS3(),
    )
    with pytest.raises(BackendNotConfigured):
        store.put_stream(io.BytesIO(b"x"), max_bytes=16)
    with pytest.raises(BackendNotConfigured):
        store.open_bytes(content_key_for("d" * 64))


# --------------------------------------------------------------------------- #
# Publish
# --------------------------------------------------------------------------- #
def test_put_publishes_content_addressed_key_with_checksum_metadata() -> None:
    store, client = make_store()
    payload = b"nagar" * 500
    result = store.put_stream(io.BytesIO(payload), max_bytes=1 << 20, content_type="image/png")

    expected = hashlib.sha256(payload).hexdigest()
    assert result.sha256 == expected
    assert result.created is True
    assert result.key == f"nagar/assets/{content_key_for(expected)}"
    stored = client.objects[result.key]
    assert stored["Body"] == payload
    assert stored["Metadata"][META_SHA256] == expected
    assert stored["ContentType"] == "image/png"


def test_put_is_idempotent_and_does_not_reupload() -> None:
    store, client = make_store()
    payload = b"idempotent"
    first = store.put_stream(io.BytesIO(payload), max_bytes=1024)
    second = store.put_stream(io.BytesIO(payload), max_bytes=1024)
    assert first.created is True
    assert second.created is False
    assert client.put_calls == [first.key], "an identical republish must not write again"


def test_put_refuses_to_overwrite_a_key_holding_other_content() -> None:
    store, client = make_store()
    payload = b"original"
    result = store.put_stream(io.BytesIO(payload), max_bytes=1024)
    client.objects[result.key]["Metadata"] = {META_SHA256: "f" * 64}
    with pytest.raises(ObjectConflict):
        store.put_stream(io.BytesIO(payload), max_bytes=1024)


def test_put_rejects_pinned_digest_mismatch_before_any_upload() -> None:
    store, client = make_store()
    with pytest.raises(IntegrityMismatch):
        store.put_stream(io.BytesIO(b"data"), max_bytes=1024, expected_sha256="0" * 64)
    assert client.put_calls == []


def test_put_enforces_max_bytes() -> None:
    store, client = make_store()
    with pytest.raises(ObjectTooLarge):
        store.put_stream(io.BytesIO(b"x" * 100), max_bytes=10)
    assert client.put_calls == []


def test_put_rejects_unsafe_custom_metadata() -> None:
    store, _ = make_store()
    with pytest.raises(ValueError):
        store.put_stream(io.BytesIO(b"x"), max_bytes=16, metadata={"bad key": "v"})
    with pytest.raises(ValueError):
        store.put_stream(io.BytesIO(b"x"), max_bytes=16, metadata={"k": "x" * 300})


def test_put_detects_a_provider_that_drops_the_checksum() -> None:
    store, client = make_store()
    original = client.put_object

    def strip_metadata(**kwargs: Any) -> dict[str, Any]:
        response = original(**kwargs)
        for entry in client.objects.values():
            entry["Metadata"] = {}
        return response

    client.put_object = strip_metadata  # type: ignore[method-assign]
    with pytest.raises(IntegrityMismatch):
        store.put_stream(io.BytesIO(b"payload"), max_bytes=1024)


def test_large_objects_take_the_multipart_path() -> None:
    store, client = make_store(multipart_threshold_bytes=8)
    result = store.put_stream(io.BytesIO(b"0123456789abcdef"), max_bytes=1024)
    assert client.upload_fileobj_calls == [result.key]


# --------------------------------------------------------------------------- #
# Retry policy: bounded, typed, never on authorization
# --------------------------------------------------------------------------- #
def test_transient_failure_is_retried_then_succeeds() -> None:
    store, client = make_store()
    client.failures = [ProviderError("503", 503)]
    result = store.put_stream(io.BytesIO(b"retry me"), max_bytes=1024)
    assert result.created is True


def test_transient_failure_after_the_budget_raises_typed_error() -> None:
    store, client = make_store(max_attempts=3)
    client.fail_always = ProviderError("SlowDown", 503)
    with pytest.raises(TransientBackendError):
        store.put_stream(io.BytesIO(b"never"), max_bytes=1024)
    assert client.put_calls == []


def test_authorization_failure_is_not_retried() -> None:
    store, client = make_store(max_attempts=5)
    client.fail_always = ProviderError("AccessDenied", 403)
    calls: list[float] = []
    store._sleep = calls.append  # type: ignore[assignment]
    with pytest.raises(AuthorizationDenied):
        store.put_stream(io.BytesIO(b"denied"), max_bytes=1024)
    assert calls == [], "a 403 must never be retried"


def test_unknown_provider_failure_is_typed_and_redacted() -> None:
    store, client = make_store()
    client.fail_always = ProviderError("InvalidRequest", 400)
    with pytest.raises(ObjectStoreError) as caught:
        store.put_stream(io.BytesIO(b"x"), max_bytes=1024)
    message = str(caught.value)
    assert "provider detail" not in message, "provider message must not be echoed"
    assert SECRET not in message
    assert ACCESS_KEY not in message


# --------------------------------------------------------------------------- #
# Reads, deletion, capacity
# --------------------------------------------------------------------------- #
def test_read_verifies_the_recorded_digest() -> None:
    store, client = make_store()
    result = store.put_stream(io.BytesIO(b"authentic bytes"), max_bytes=1024)
    assert store.open_bytes(result.key) == b"authentic bytes"

    client.objects[result.key]["Body"] = b"tampered bytes"
    with pytest.raises(IntegrityMismatch):
        store.open_bytes(result.key)


def test_missing_object_is_none_and_not_found() -> None:
    store, _ = make_store()
    key = content_key_for("b" * 64)
    assert store.head(key) is None
    assert store.exists(key) is False
    with pytest.raises(ObjectNotFound):
        store.open_bytes(key)


def test_read_bound_is_enforced() -> None:
    store, _ = make_store()
    result = store.put_stream(io.BytesIO(b"0123456789"), max_bytes=1024)
    with pytest.raises(ObjectTooLarge):
        store.open_bytes(result.key, max_bytes=4)


def test_delete_is_reported_as_soft_and_never_reclaims() -> None:
    store, _ = make_store()
    result = store.put_stream(io.BytesIO(b"delete me"), max_bytes=1024)
    outcome = store.delete(result.key)
    assert outcome.existed is True
    assert outcome.soft is True
    assert outcome.reclaimed_bytes == 0

    capabilities = store.capabilities()
    assert capabilities.delete_is_soft is True
    assert "soft" in capabilities.notes.lower()


def test_delete_of_absent_key_is_not_an_error() -> None:
    store, _ = make_store()
    outcome = store.delete(content_key_for("c" * 64))
    assert outcome.existed is False
    assert outcome.reclaimed_bytes == 0


def test_list_keys_is_prefixed_and_bounded() -> None:
    store, _ = make_store()
    for payload in (b"one", b"two", b"three"):
        store.put_stream(io.BytesIO(payload), max_bytes=1024)
    assert len(store.list_keys()) == 3
    assert len(store.list_keys(limit=2)) == 2


def test_measure_namespace_counts_only_our_prefix() -> None:
    store, client = make_store()
    store.put_stream(io.BytesIO(b"abc"), max_bytes=1024)
    client.objects["someone-else/object"] = {
        "Body": b"zzzzz",
        "Metadata": {},
        "ContentType": None,
        "ETag": '"e"',
    }
    total, count = store.measure_namespace()
    assert (total, count) == (3, 1)


def test_capacity_is_unknown_and_carries_no_invented_quota() -> None:
    store, _ = make_store()
    report = store.capacity()
    assert report.kind is CapacityKind.UNKNOWN
    assert report.quota_bytes is None
    assert report.used_bytes is None
    assert report.verified_remaining_bytes is None
    assert "no quota" in report.source.lower()


def test_health_reports_reachability_without_leaking_credentials() -> None:
    store, client = make_store()
    healthy = store.health()
    assert healthy.ok is True

    client.fail_always = ProviderError("403", 403)
    unhealthy = store.health()
    assert unhealthy.ok is False
    assert SECRET not in unhealthy.detail
    assert ACCESS_KEY not in unhealthy.detail
