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
    DEFAULT_PREFIX,
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
    InvalidObjectKey,
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
        #: Every provider operation attempted, in order — lets a test prove a
        #: refusal happened *before* the network, not after a 404/403.
        self.calls: list[str] = []

    # -- failure injection -------------------------------------------------- #
    def _maybe_fail(self) -> None:
        if self.fail_always is not None:
            raise self.fail_always
        if self.failures:
            raise self.failures.pop(0)

    # -- S3 surface --------------------------------------------------------- #
    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("put_object")
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
        self.calls.append("upload_fileobj")
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
        self.calls.append("head_object")
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
        self.calls.append("get_object")
        self._maybe_fail()
        key = kwargs["Key"]
        entry = self.objects.get(key)
        if entry is None:
            raise ProviderError("NoSuchKey", 404)
        return {"Body": FakeBody(entry["Body"]), "Metadata": entry["Metadata"]}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("delete_object")
        self._maybe_fail()
        self.objects.pop(kwargs["Key"], None)
        return {}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("list_objects_v2")
        self._maybe_fail()
        prefix = kwargs.get("Prefix", "")
        contents = [
            {"Key": key, "Size": len(entry["Body"])}
            for key, entry in sorted(self.objects.items())
            if key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("head_bucket")
        self._maybe_fail()
        return {}

    # -- test helpers ------------------------------------------------------- #
    def corrupt_bytes(self, key: str, data: bytes) -> None:
        """Replace the stored payload, leaving metadata (incl. sha256) untouched."""
        self.objects[key]["Body"] = data


def owned_key(hex_char: str) -> str:
    """An absolute key inside this store's namespace for a synthetic digest.

    Keys are absolute, so a key produced without the namespace prefix is a
    foreign key — it is refused on purpose (see the ownership tests).
    """
    return f"nagar/assets/{content_key_for(hex_char * 64)}"


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


def test_aws_aliases_need_an_explicit_neon_region_and_never_borrow_aws_region() -> None:
    """Credential/endpoint aliases are allowed; the *region* is never an AWS guess."""
    aliases_without_region = {
        "AWS_ENDPOINT_URL_S3": ENDPOINT,
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "aws-key",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "NEXUS_NEON_STORAGE_BUCKET": "nagar-assets",
    }
    assert build_neon_object_store(aliases_without_region) is None, (
        "AWS_REGION must not silently become the Neon branch region"
    )

    aliases_with_region = dict(aliases_without_region)
    aliases_with_region["NEXUS_NEON_STORAGE_REGION"] = "us-east-2"
    store = build_neon_object_store(aliases_with_region)
    assert store is not None
    assert store.config.endpoint_url == ENDPOINT
    assert store.config.region == "us-east-2", "the explicit Neon region wins over AWS_REGION"


def test_explicit_neon_names_win_over_the_aws_aliases() -> None:
    env = {
        "AWS_ENDPOINT_URL_S3": "https://other-branch.storage.c-2.us-east-2.aws.neon.tech",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "aws-key",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "NEXUS_NEON_STORAGE_ENDPOINT": ENDPOINT,
        "NEXUS_NEON_STORAGE_REGION": "us-east-2",
        "NEXUS_NEON_STORAGE_ACCESS_KEY_ID": ACCESS_KEY,
        "NEXUS_NEON_STORAGE_SECRET_ACCESS_KEY": SECRET,
        "NEXUS_NEON_STORAGE_BUCKET": "nagar-assets",
    }
    store = build_neon_object_store(env)
    assert store is not None
    assert store.config.endpoint_url == ENDPOINT
    assert store.config.access_key_id == ACCESS_KEY
    assert store.config.secret_access_key == SECRET


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example?.neon.tech",
        "https://evil.example#neon.tech",
        "https://br-x.storage.c-2.us-east-2.aws.neon.tech.evil.example",
        "https://user:pass@br-x.storage.c-2.us-east-2.aws.neon.tech",
        "https://br-x.storage.c-2.us-east-2.aws.neon.tech/bucket",
        "https://br-x.storage.c-2.us-east-2.aws.neon.tech:8443",
    ],
)
def test_lookalike_or_ambiguous_endpoints_are_refused(endpoint: str) -> None:
    """Host parsing is by authority, not by a substring/suffix test on the URL."""
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


def test_namespace_is_the_configured_prefix() -> None:
    store, _ = make_store()
    assert store.namespace == "nagar/assets"
    assert DEFAULT_PREFIX == "nagar/assets"


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
        store.open_bytes(owned_key("d"))


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
    key = owned_key("b")
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
    outcome = store.delete(owned_key("c"))
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


# --------------------------------------------------------------------------- #
# Contract conformance — the laws the shared fixture could not exercise
# --------------------------------------------------------------------------- #
def test_round_trip_publish_head_exists_read() -> None:
    """1. Publish → head → exists → complete read round-trip."""
    store, _ = make_store()
    payload = b"round-trip payload " * 64
    published = store.put_stream(io.BytesIO(payload), max_bytes=1 << 20)

    assert store.exists(published.key) is True
    metadata = store.head(published.key)
    assert metadata is not None
    assert metadata.key == published.key
    assert metadata.size_bytes == len(payload)
    assert metadata.sha256 == published.sha256

    assert store.open_bytes(published.key) == payload
    assert b"".join(store.iter_chunks(published.key, chunk_size=7)) == payload


def test_listed_keys_are_absolute_and_addressable() -> None:
    """2. Returned-key consistency: listed keys are the keys the API accepts."""
    store, _ = make_store()
    published = store.put_stream(io.BytesIO(b"address me"), max_bytes=1024)

    listed = store.list_keys()
    assert published.key in listed
    for key in listed:
        assert key.startswith(store.namespace), (
            f"listed key {key!r} escapes the store namespace {store.namespace!r}"
        )
        metadata = store.head(key)
        assert metadata is not None and metadata.key == key, (
            "every listed key must be accepted by head() unchanged"
        )
        # ... and readable, which is what "addressable" means for a caller.
        assert isinstance(store.open_bytes(key), bytes)


def test_prefix_filters_over_absolute_keys_not_a_hidden_namespace() -> None:
    """3. list_keys(prefix=...) against keys returned by the same backend."""
    store, _ = make_store()
    first = store.put_stream(io.BytesIO(b"one"), max_bytes=64)
    second = store.put_stream(io.BytesIO(b"two"), max_bytes=64)

    narrow = first.key.rsplit("/", 1)[0]  # e.g. nagar/assets/blobs/ab
    assert store.list_keys(prefix=narrow) == [first.key]
    assert store.list_keys(prefix=store.namespace) == store.list_keys()
    assert second.key not in store.list_keys(prefix=narrow)

    # A foreign prefix is refused, never silently translated or emptied.
    with pytest.raises(InvalidObjectKey):
        store.list_keys(prefix="other-tenant")
    with pytest.raises(InvalidObjectKey):
        store.list_keys(prefix=content_key_for("f" * 64))  # the relative form


def test_namespace_isolation_ignores_foreign_objects() -> None:
    """6b. Objects outside our namespace are never listed, measured or read."""
    store, client = make_store()
    store.put_stream(io.BytesIO(b"ours"), max_bytes=64)
    client.objects["other-tenant/secret"] = {
        "Body": b"not ours",
        "Metadata": {},
        "ContentType": None,
        "ETag": '"x"',
    }
    assert all(key.startswith("nagar/assets/") for key in store.list_keys())
    assert "other-tenant/secret" not in store.list_keys()
    total, count = store.measure_namespace()
    assert (total, count) == (4, 1)


def test_idempotent_republish_and_conflicting_content() -> None:
    """4. Idempotent republish and conflicting existing content (kept together)."""
    store, client = make_store()
    payload = b"same bytes"
    first = store.put_stream(io.BytesIO(payload), max_bytes=1024)
    second = store.put_stream(io.BytesIO(payload), max_bytes=1024)
    assert (first.created, second.created) == (True, False)
    assert len(client.put_calls) == 1

    # A key whose recorded content disagrees is a conflict, never a silent rewrite.
    client.objects[first.key]["Metadata"] = {META_SHA256: "0" * 64}
    with pytest.raises(ObjectConflict):
        store.put_stream(io.BytesIO(payload), max_bytes=1024)


def test_read_time_bounds_and_digest_mismatch() -> None:
    """5. Read-time byte bounds and digest mismatch detection."""
    store, client = make_store()
    published = store.put_stream(io.BytesIO(b"0123456789"), max_bytes=1024)

    with pytest.raises(ObjectTooLarge):
        store.open_bytes(published.key, max_bytes=4)

    # Same length, different bytes: only a byte-level check can see this.
    client.corrupt_bytes(published.key, b"9876543210")
    with pytest.raises(IntegrityMismatch):
        store.open_bytes(published.key)
    with pytest.raises(IntegrityMismatch):
        list(store.iter_chunks(published.key, chunk_size=4))


# --------------------------------------------------------------------------- #
# Integrity: metadata presence is not byte verification
# --------------------------------------------------------------------------- #
def test_head_does_not_verify_bytes() -> None:
    """The precisely narrowed claim: head reports metadata, it does not read."""
    store, client = make_store()
    published = store.put_stream(io.BytesIO(b"authentic!"), max_bytes=64)
    client.corrupt_bytes(published.key, b"TAMPERED!")

    metadata = store.head(published.key)
    assert metadata is not None
    assert metadata.sha256 == published.sha256, "head reports the recorded digest"
    assert metadata.size_bytes == len(b"TAMPERED!")

    assert store.verify_bytes(published.key) is False
    with pytest.raises(IntegrityMismatch):
        store.open_bytes(published.key)


def test_verify_bytes_is_true_only_after_a_real_byte_re_read() -> None:
    store, client = make_store()
    published = store.put_stream(io.BytesIO(b"healthy bytes"), max_bytes=64)
    assert store.verify_bytes(published.key) is True
    client.corrupt_bytes(published.key, b"healthy BYTES")
    assert store.verify_bytes(published.key) is False


def test_verify_bytes_refuses_a_missing_object() -> None:
    store, _ = make_store()
    with pytest.raises(ObjectNotFound):
        store.verify_bytes(owned_key("a"))


def test_verify_bytes_is_false_when_no_identity_exists_at_all() -> None:
    """No key digest and no recorded digest: the comparison is impossible.

    ``False`` is reserved for that case. A *content-addressed* key is itself an
    identity (see the next test), so removing only the metadata does not make
    verification impossible.
    """
    store, client = make_store()
    key = "nagar/assets/notes/readme.txt"
    client.objects[key] = {
        "Body": b"no identity",
        "Metadata": {},
        "ContentType": None,
        "ETag": '"e"',
    }
    assert store.verify_bytes(key) is False


def test_a_content_addressed_key_verifies_even_without_recorded_metadata() -> None:
    """The immutable key is a real identity; the metadata is only the second one."""
    store, client = make_store()
    published = store.put_stream(io.BytesIO(b"no metadata"), max_bytes=64)
    client.objects[published.key]["Metadata"] = {}
    assert store.verify_bytes(published.key) is True

    client.corrupt_bytes(published.key, b"tampered")
    assert store.verify_bytes(published.key) is False


def test_put_reports_that_it_did_not_verify_bytes() -> None:
    store, _ = make_store()
    created = store.put_stream(io.BytesIO(b"fresh"), max_bytes=64)
    assert created.created is True
    assert created.content_verified is False, (
        "put_stream checks size + recorded metadata after upload; it must not claim more"
    )

    duplicate = store.put_stream(io.BytesIO(b"fresh"), max_bytes=64)
    assert duplicate.created is False
    assert duplicate.content_verified is False

    verified = store.put_stream(io.BytesIO(b"fresh"), max_bytes=64, verify_existing=True)
    assert verified.created is False
    assert verified.content_verified is True


def test_verify_existing_refuses_metadata_that_lies_about_the_bytes() -> None:
    """The defect the narrowed claim points at: matching metadata, corrupted bytes."""
    store, client = make_store()
    published = store.put_stream(io.BytesIO(b"payload!"), max_bytes=64)
    client.corrupt_bytes(published.key, b"CORRUPT!")

    # Default reuse trusts metadata (documented, and reported as unverified) ...
    reuse = store.put_stream(io.BytesIO(b"payload!"), max_bytes=64)
    assert (reuse.created, reuse.content_verified) == (False, False)

    # ... and the opt-in path refuses it instead of handing back a corrupt object.
    with pytest.raises(IntegrityMismatch):
        store.put_stream(io.BytesIO(b"payload!"), max_bytes=64, verify_existing=True)


# --------------------------------------------------------------------------- #
# Failure paths (mission 1 item 6)
# --------------------------------------------------------------------------- #
def test_not_found_is_typed_on_every_read_path() -> None:
    store, _ = make_store()
    key = owned_key("b")
    assert store.head(key) is None
    assert store.exists(key) is False
    with pytest.raises(ObjectNotFound):
        store.open_bytes(key)
    with pytest.raises(ObjectNotFound):
        list(store.iter_chunks(key))
    assert store.delete(key).existed is False


# --------------------------------------------------------------------------- #
# Namespace ownership (review round 2, finding A)
#
# `list_objects_v2` matches a raw string prefix, so asking for "nagar/assets"
# also returns "nagar/assets-other/…" from the provider. Ownership therefore
# cannot be delegated to the provider's string match: it is enforced here, on
# the way in (keys) and on the way out (listing and measurement).
# --------------------------------------------------------------------------- #
SIBLING_KEY = f"nagar/assets-other/blobs/{'ab'}/{('ab' * 32)}"
UNRELATED_KEY = "other-tenant/secret"


@pytest.fixture(params=[SIBLING_KEY, UNRELATED_KEY], ids=["sibling", "unrelated"])
def foreign_key(request) -> str:
    return request.param


@pytest.mark.parametrize(
    "operation",
    ["head", "exists", "open_bytes", "iter_chunks", "verify_bytes", "delete"],
)
def test_a_foreign_key_is_refused_before_any_provider_request(operation, foreign_key) -> None:
    """The refusal must happen locally; a provider round-trip would already leak intent."""
    store, client = make_store()
    with pytest.raises(InvalidObjectKey):
        if operation == "iter_chunks":
            list(store.iter_chunks(foreign_key))
        else:
            getattr(store, operation)(foreign_key)
    assert client.calls == [], f"{operation} reached the provider before refusing the key"


def test_listing_excludes_a_sibling_namespace() -> None:
    store, client = make_store()
    ours = store.put_stream(io.BytesIO(b"ours"), max_bytes=1024)
    client.objects[SIBLING_KEY] = {
        "Body": b"theirs",
        "Metadata": {},
        "ContentType": None,
        "ETag": '"e"',
    }
    listed = store.list_keys()
    assert ours.key in listed
    assert SIBLING_KEY not in listed, "a sibling namespace leaked into this store's listing"
    assert all(key.startswith("nagar/assets/") for key in listed)


def test_measurement_excludes_a_sibling_namespace() -> None:
    store, client = make_store()
    store.put_stream(io.BytesIO(b"abc"), max_bytes=1024)
    client.objects[SIBLING_KEY] = {
        "Body": b"a-much-longer-foreign-object",
        "Metadata": {},
        "ContentType": None,
        "ETag": '"e"',
    }
    assert store.measure_namespace() == (3, 1)


def test_a_prefix_inside_a_sibling_namespace_is_refused() -> None:
    store, _ = make_store()
    with pytest.raises(InvalidObjectKey):
        store.list_keys(prefix="nagar/assets-other")


def test_delete_refuses_a_foreign_key_without_a_request() -> None:
    store, client = make_store()
    with pytest.raises(InvalidObjectKey):
        store.delete(SIBLING_KEY)
    assert client.calls == []


# --------------------------------------------------------------------------- #
# Content-address integrity (review round 2, finding C)
#
# Two identities exist: the digest the immutable key encodes, and the digest in
# the rewritable metadata. An attacker who replaces the bytes *and* updates the
# metadata leaves the key untouched — a metadata-only check would then call the
# object intact.
# --------------------------------------------------------------------------- #
def test_rewritten_bytes_and_metadata_cannot_pass_verification() -> None:
    store, client = make_store()
    published = store.put_stream(io.BytesIO(b"authentic payload"), max_bytes=1024)

    forged = b"forged payload with the same key"
    import hashlib

    client.objects[published.key]["Body"] = forged
    client.objects[published.key]["Metadata"]["sha256"] = hashlib.sha256(forged).hexdigest()

    with pytest.raises(IntegrityMismatch):
        store.verify_bytes(published.key)
    with pytest.raises(IntegrityMismatch):
        list(store.iter_chunks(published.key))
    with pytest.raises(IntegrityMismatch):
        store.open_bytes(published.key)


def test_the_key_contradiction_is_reported_before_the_object_is_streamed() -> None:
    store, client = make_store()
    published = store.put_stream(io.BytesIO(b"authentic payload"), max_bytes=1024)
    client.objects[published.key]["Metadata"]["sha256"] = "f" * 64

    client.calls.clear()
    with pytest.raises(IntegrityMismatch):
        list(store.iter_chunks(published.key))
    assert "get_object" not in client.calls, "a contradictory object was streamed before refusal"


def test_metadata_digest_still_must_match_the_bytes() -> None:
    """The pre-existing law is not weakened: metadata alone can still catch corruption."""
    store, client = make_store()
    published = store.put_stream(io.BytesIO(b"authentic payload"), max_bytes=1024)
    client.corrupt_bytes(published.key, b"tampered")
    assert store.verify_bytes(published.key) is False


# --------------------------------------------------------------------------- #
# Root namespace edge case (finding A: validate deliberately)
# --------------------------------------------------------------------------- #
def test_an_empty_namespace_claims_the_whole_bucket_and_needs_an_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="allow_root_namespace"):
        NeonStorageConfig(
            endpoint_url=ENDPOINT,
            region="us-east-2",
            access_key_id=ACCESS_KEY,
            secret_access_key=SECRET,
            bucket="nagar-assets",
            prefix="",
        )


def test_an_opted_in_root_namespace_owns_every_key() -> None:
    client = FakeS3()
    store = NeonObjectStorage(
        NeonStorageConfig(
            endpoint_url=ENDPOINT,
            region="us-east-2",
            access_key_id=ACCESS_KEY,
            secret_access_key=SECRET,
            bucket="nagar-assets",
            prefix="",
            allow_root_namespace=True,
        ),
        client=client,
    )
    assert store.namespace == ""
    published = store.put_stream(io.BytesIO(b"root"), max_bytes=64)
    assert published.key.startswith("blobs/")
    assert store.open_bytes(published.key) == b"root"
    assert store.list_keys() == [published.key]
    assert store.measure_namespace() == (4, 1)
