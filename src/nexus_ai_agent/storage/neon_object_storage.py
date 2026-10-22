"""Neon Object Storage backend over its S3-compatible branch endpoint.

Verified against Neon's published documentation (2026-10-22):

* ``GET /api/v2/projects/{project_id}/branches/{branch_id}/storage`` returns
  ``s3_endpoint`` (``https://br-….storage.c-<N>.<region>.aws.neon.tech``),
  ``region`` and ``force_path_style``. Object storage is **beta** and only
  available in a published set of regions.
* The S3 surface is a **subset**: ``PutObject``/``GetObject`` (Range)/
  ``HeadObject``/``DeleteObject``/``DeleteObjects``/``CopyObject``/
  ``GetObjectAttributes``/``ListObjectsV2``/multipart/presigned GET+PUT are
  supported; ACL/policy writes, encryption configuration, replication and
  notifications return ``501``.
* **Path-style only**, **SigV4 only**. A virtual-hosted request silently targets
  a different host (the documented "requests go to AWS instead of Neon" trap).
* ``DeleteObject`` **soft-deletes** on the branch. Versioning and lifecycle are
  stored but not enforced. Deleting therefore reclaims nothing, and this adapter
  says so in :class:`DeleteResult`.
* No quota/usage endpoint is published, so :meth:`capacity` reports
  :attr:`CapacityKind.UNKNOWN` rather than inventing a free-tier number.

Credentials are Neon scoped credentials (``storage:read`` / ``storage:write``)
mapped to an S3 access-key id/secret. They are secrets: they are never in the
config's ``repr``, never in an exception message, and never logged here.

``boto3`` is imported lazily inside the client property, so importing this
module (or constructing a config from an incomplete environment) cannot break
startup.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import IO, Any, BinaryIO
from urllib.parse import urlsplit

from nexus_ai_agent.storage.object_store import (
    AuthorizationDenied,
    BackendCapabilities,
    BackendNotConfigured,
    BackendUnavailable,
    CapacityKind,
    CapacityReport,
    DeleteResult,
    HealthReport,
    IntegrityMismatch,
    InvalidObjectKey,
    ObjectConflict,
    ObjectMetadata,
    ObjectNotFound,
    ObjectStoreError,
    ObjectTooLarge,
    PutResult,
    TransientBackendError,
    content_key_for,
    utcnow,
    validate_object_key,
)

#: Every Neon Object Storage data-plane host ends in this suffix.
NEON_ENDPOINT_SUFFIX = ".neon.tech"

#: Namespace for the objects Nagar owns inside the bucket (content-addressed keys
#: are appended: ``<prefix>/blobs/<2>/<sha256>``).
DEFAULT_PREFIX = "nagar/assets"

#: Environment variables read by :meth:`NeonStorageConfig.from_env`. The
#: ``AWS_*`` names are Neon's own documented aliases; the ``NEXUS_NEON_STORAGE_*``
#: names win when both are present so a host-level AWS configuration cannot
#: silently capture Nagar's user files.
#: ``AWS_ENDPOINT_URL_S3`` is accepted as a *discovery convenience* only: it may
#: switch the backend on when it points at a Neon host, but it is never the value
#: used to build the client (the endpoint is canonicalized from the environment
#: first). ``AWS_REGION`` is deliberately NOT an accepted source: Neon returns the
#: branch's own region, and an AWS region would silently target the wrong one.
_ENV = {
    "endpoint_url": ("NEXUS_NEON_STORAGE_ENDPOINT",),
    "region": ("NEXUS_NEON_STORAGE_REGION",),
    "access_key_id": ("NEXUS_NEON_STORAGE_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
    "secret_access_key": ("NEXUS_NEON_STORAGE_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
    "bucket": ("NEXUS_NEON_STORAGE_BUCKET",),
}

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_META_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_META_VALUE = re.compile(r"^[\x20-\x7e]{0,256}$")

#: Stored under ``x-amz-meta-sha256`` so an existing object's identity can be
#: checked with a HEAD instead of a GET.
META_SHA256 = "sha256"

#: Error codes / HTTP statuses that are worth retrying (bounded).
_TRANSIENT_CODES = frozenset(
    {
        "500",
        "502",
        "503",
        "504",
        "InternalError",
        "RequestTimeout",
        "ServiceUnavailable",
        "SlowDown",
        "Throttling",
        "ThrottlingException",
    }
)
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
_DENIED_CODES = frozenset(
    {"403", "AccessDenied", "AccountProblem", "InvalidAccessKeyId", "SignatureDoesNotMatch"}
)

#: Single-part PUT ceiling enforced by S3; larger objects must use multipart.
SINGLE_PUT_LIMIT = 5 * 1024**3


def _first(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class NeonStorageConfig:
    """Explicit configuration for the Neon Object Storage user-asset backend.

    Deliberately separate from the ``R2_*`` settings: R2 keeps its existing
    maintenance-blob role (database backups, heavy RAG documents), and neither
    backend may silently inherit the other's bucket or credentials.
    """

    endpoint_url: str
    region: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    bucket: str
    prefix: str = DEFAULT_PREFIX
    max_attempts: int = 3
    retry_backoff_seconds: float = 0.25
    multipart_threshold_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        endpoint = (self.endpoint_url or "").strip()
        parsed = urlsplit(endpoint)
        # A naive ``endswith(".neon.tech")`` on the raw string accepts
        # ``https://evil.example?.neon.tech``; parsing the authority is the only
        # way to know which host will actually receive the signed request.
        if parsed.scheme != "https":
            raise ValueError(
                "Neon Object Storage endpoint must be an https:// URL "
                "(http would send credentials in clear text)"
            )
        if parsed.username or parsed.password:
            raise ValueError("endpoint must not embed credentials in the URL")
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint must not carry a query string or fragment")
        if parsed.path not in ("", "/"):
            raise ValueError("endpoint must be a bare host (no path component)")
        host = (parsed.hostname or "").lower()
        if not host.endswith(NEON_ENDPOINT_SUFFIX):
            raise ValueError(
                f"endpoint host {host!r} is not a Neon Object Storage host "
                f"(*{NEON_ENDPOINT_SUFFIX}); an AWS S3 endpoint here means every "
                "request silently leaves Neon"
            )
        if parsed.port is not None and parsed.port != 443:
            raise ValueError("endpoint port must be 443 (the documented Neon endpoint)")
        object.__setattr__(self, "endpoint_url", f"https://{host}")
        if not self.region or any(char.isspace() for char in self.region):
            raise ValueError("Neon Object Storage region must be a non-empty region string")
        if not _BUCKET.match(self.bucket or ""):
            raise ValueError("bucket must be a valid S3 bucket name (3-63 chars, [a-z0-9.-])")
        object.__setattr__(self, "prefix", validate_object_key(self.prefix))
        if not self.access_key_id or not self.secret_access_key:
            raise ValueError("Neon Object Storage requires both an access key id and a secret")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        if self.multipart_threshold_bytes < 1:
            raise ValueError("multipart_threshold_bytes must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> NeonStorageConfig | None:
        """Build a config from the environment, or return ``None`` when incomplete.

        Returning ``None`` (instead of raising) is the feature boundary: an
        installation without Neon Object Storage credentials keeps the local
        content-addressed backend and starts normally.

        The endpoint alone decides whether Neon is switched on. ``AWS_*`` names
        are honoured only as documented Neon aliases: the S3 endpoint variable is
        accepted when it points at a Neon host (Neon's own ``get started`` writes
        those names), and the credential pair is accepted as the access-key/secret
        alias. The region is **never** taken from ``AWS_REGION`` — Neon's branch
        region is authoritative and an AWS region would silently target the wrong
        one. An unrelated AWS configuration on the host therefore cannot capture
        Nagar's user files, and cannot crash startup.
        """
        source = os.environ if env is None else env
        endpoint = _first(source, _ENV["endpoint_url"])
        if endpoint is None:
            alias = _first(source, ("AWS_ENDPOINT_URL_S3",))
            host = (urlsplit(alias or "").hostname or "").lower()
            endpoint = alias if host.endswith(NEON_ENDPOINT_SUFFIX) else None
        if endpoint is None:
            return None
        values = {
            key: _first(source, names) for key, names in _ENV.items() if key != "endpoint_url"
        }
        if any(value is None for value in values.values()):
            return None
        return cls(
            endpoint_url=endpoint,
            region=str(values["region"]),
            access_key_id=str(values["access_key_id"]),
            secret_access_key=str(values["secret_access_key"]),
            bucket=str(values["bucket"]),
        )

    def redacted(self) -> dict[str, Any]:
        """A loggable view: endpoint/region/bucket are public, credentials are not."""
        return {
            "endpoint_url": self.endpoint_url,
            "region": self.region,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "access_key_id": "***" if self.access_key_id else "",
            "secret_access_key": "***" if self.secret_access_key else "",
        }


def build_neon_object_store(
    env: Mapping[str, str] | None = None,
) -> NeonObjectStorage | None:
    """Return a configured backend, or ``None`` when the feature is switched off."""
    config = NeonStorageConfig.from_env(env)
    if config is None:
        return None
    return NeonObjectStorage(config)


def _classify(exc: BaseException, *, operation: str, key: str | None = None) -> ObjectStoreError:
    """Map a provider exception to the contract taxonomy — without echoing it.

    The provider's message can contain request context; only the error code and
    HTTP status are consulted, and the message is rebuilt from the operation and
    key. That keeps credentials and signed query strings out of logs and job
    records.
    """
    response = getattr(exc, "response", None)
    code = ""
    status: int | None = None
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code", ""))
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, dict):
            raw_status = metadata.get("HTTPStatusCode")
            status = raw_status if isinstance(raw_status, int) else None
    where = f" for {key!r}" if key else ""
    if code in _NOT_FOUND_CODES or status == 404:
        return ObjectNotFound(f"{operation}{where}: object not found")
    if code in _DENIED_CODES or status == 403:
        return AuthorizationDenied(
            f"{operation}{where}: the Neon credential lacks the required storage scope"
        )
    if code == "NoSuchBucket":
        return BackendUnavailable(f"{operation}{where}: the configured bucket does not exist")
    if code in _TRANSIENT_CODES or status in _TRANSIENT_STATUS:
        return TransientBackendError(
            f"{operation}{where}: transient provider failure ({code or status})"
        )
    if status is not None and status >= 500:
        return TransientBackendError(f"{operation}{where}: provider error {status}")
    return ObjectStoreError(f"{operation}{where}: provider rejected the request ({code or status})")


def _validate_metadata(metadata: Mapping[str, str] | None) -> dict[str, str]:
    """HTTP-header-safe custom metadata only (S3 puts it in ``x-amz-meta-*``)."""
    if not metadata:
        return {}
    cleaned: dict[str, str] = {}
    for name, value in metadata.items():
        if not _META_KEY.match(str(name)):
            raise ValueError(f"invalid metadata key {name!r}: [A-Za-z0-9_-]{{1,64}} only")
        if not _META_VALUE.match(str(value)):
            raise ValueError(f"invalid metadata value for {name!r}: printable ASCII, max 256")
        cleaned[str(name)] = str(value)
    return cleaned


class NeonObjectStorage:
    """Content-addressed object storage on a Neon branch bucket."""

    name = "neon"

    def __init__(
        self,
        config: NeonStorageConfig,
        *,
        client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._client = client
        self._sleep = sleep

    # -- construction ------------------------------------------------------- #
    @property
    def config(self) -> NeonStorageConfig:
        return self._config

    @property
    def namespace(self) -> str:
        """The bucket prefix this store owns: stored keys are ``<namespace>/...``.

        Keys handed to callers are always this absolute form, so a stored key can
        be passed straight back to ``head``/``open_bytes``/``delete``.
        """
        return self._config.prefix

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise BackendNotConfigured(
                "Neon Object Storage is not configured; set NEXUS_NEON_STORAGE_ENDPOINT, "
                "NEXUS_NEON_STORAGE_REGION, NEXUS_NEON_STORAGE_ACCESS_KEY_ID, "
                "NEXUS_NEON_STORAGE_SECRET_ACCESS_KEY and NEXUS_NEON_STORAGE_BUCKET"
            )

    def is_configured(self) -> bool:
        return bool(
            self._config.endpoint_url
            and self._config.region
            and self._config.access_key_id
            and self._config.secret_access_key
            and self._config.bucket
        )

    def _client_kwargs(self) -> dict[str, Any]:
        """boto3 client arguments: path-style, SigV4, no hidden retries.

        ``total_max_attempts=1`` disables botocore's own retry loop so the single
        bounded retry policy in :meth:`_call` is the only one — otherwise retries
        multiply and a failure path becomes untestable.
        """
        from botocore.config import Config

        return {
            "endpoint_url": self._config.endpoint_url,
            "region_name": self._config.region,
            "aws_access_key_id": self._config.access_key_id,
            "aws_secret_access_key": self._config.secret_access_key,
            "config": Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                retries={"total_max_attempts": 1, "mode": "standard"},
                connect_timeout=10,
                read_timeout=60,
            ),
        }

    @property
    def client(self) -> Any:
        if self._client is None:
            self._require_configured()
            import boto3

            self._client = boto3.client("s3", **self._client_kwargs())
        return self._client

    # -- retry -------------------------------------------------------------- #
    def _call(
        self,
        operation: str,
        fn: Callable[..., Any],
        *args: Any,
        key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run ``fn`` with a bounded, typed retry.

        Only :class:`TransientBackendError` is retried, a fixed number of times;
        an authorization failure is never retried (retrying a 403 is how a bad
        credential becomes a rate-limit incident).
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # provider exceptions are not in our taxonomy
                typed = _classify(exc, operation=operation, key=key)
                if isinstance(typed, TransientBackendError) and attempt < self._config.max_attempts:
                    self._sleep(self._config.retry_backoff_seconds * (2 ** (attempt - 1)))
                    continue
                raise typed from None

    # -- capabilities ------------------------------------------------------- #
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_range_reads=True,
            supports_multipart=True,
            supports_presign=True,
            supports_delete=True,
            delete_is_soft=True,
            supports_custom_metadata=True,
            max_object_bytes=None,
            notes=(
                "Neon Object Storage (beta, S3 subset): path-style + SigV4 only; "
                "DeleteObject soft-deletes on the branch; object size limit not published"
            ),
        )

    # -- writes ------------------------------------------------------------- #
    def put_stream(
        self,
        source: BinaryIO,
        *,
        max_bytes: int,
        content_type: str | None = None,
        expected_sha256: str | None = None,
        metadata: Mapping[str, str] | None = None,
        verify_existing: bool = False,
    ) -> PutResult:
        self._require_configured()
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        custom = _validate_metadata(metadata)
        spool = tempfile.SpooledTemporaryFile(max_size=min(max_bytes, 8 * 1024 * 1024))
        try:
            sha256, size = self._spool(source, spool, max_bytes=max_bytes)
            if expected_sha256 is not None and sha256 != expected_sha256:
                raise IntegrityMismatch(
                    "stream digest does not match the pinned expected_sha256; nothing was published"
                )
            key = content_key_for(sha256, prefix=self._config.prefix)
            existing = self.head(key)
            if existing is not None:
                stored_sha = dict(existing.custom).get(META_SHA256)
                if stored_sha != sha256 or existing.size_bytes != size:
                    raise ObjectConflict(
                        f"key {key!r} already holds different content; refusing to overwrite an "
                        "immutable content-addressed object"
                    )
                # Metadata says "same object". Verify it only when asked: a
                # byte-level check costs a full GET, and silently treating the
                # metadata as proof is exactly the claim this contract forbids.
                if verify_existing and not self.verify_bytes(key):
                    raise IntegrityMismatch(
                        f"key {key!r} carries matching metadata but its bytes do not hash "
                        "to the recorded digest"
                    )
                return PutResult(
                    key=key,
                    sha256=sha256,
                    size_bytes=size,
                    created=False,
                    etag=existing.etag,
                    content_verified=verify_existing,
                )
            spool.seek(0)
            extra: dict[str, Any] = {
                "ContentType": content_type or "application/octet-stream",
                "Metadata": {**custom, META_SHA256: sha256},
            }
            self._upload(key=key, spool=spool, size=size, extra=extra)
            verified = self.head(key)
            if verified is None:
                raise IntegrityMismatch(f"object {key!r} is missing immediately after upload")
            stored = dict(verified.custom).get(META_SHA256)
            if verified.size_bytes != size or stored != sha256:
                digest_state = "present" if stored else "missing"
                raise IntegrityMismatch(
                    f"object {key!r} failed post-upload verification "
                    f"(size {verified.size_bytes} vs {size}, digest {digest_state})"
                )
            # Size + recorded digest metadata were re-read; the bytes were not.
            return PutResult(
                key=key,
                sha256=sha256,
                size_bytes=size,
                created=True,
                etag=verified.etag,
                content_verified=False,
            )
        finally:
            spool.close()

    @staticmethod
    def _spool(source: BinaryIO, spool: IO[bytes], *, max_bytes: int) -> tuple[str, int]:
        """Measure and spool in one bounded pass (provider streams need a rewind)."""
        import hashlib

        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = source.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ObjectTooLarge(f"stream exceeds the {max_bytes}-byte limit")
            digest.update(chunk)
            spool.write(chunk)
        return digest.hexdigest(), size

    def _upload(self, *, key: str, spool: IO[bytes], size: int, extra: dict[str, Any]) -> None:
        bucket = self._config.bucket
        client = self.client
        if size <= self._config.multipart_threshold_bytes:
            self._call(
                "put_object",
                client.put_object,
                Bucket=bucket,
                Key=key,
                Body=spool,
                ContentLength=size,
                **extra,
                key=key,
            )
            return
        # Imported dynamically: ``boto3.s3.transfer`` ships no type marker, and
        # adding a mypy override would mean editing the packaging zone for one
        # symbol. A dynamic import keeps the module typed without that coupling.
        import importlib

        transfer_config = importlib.import_module("boto3.s3.transfer").TransferConfig
        transfer = transfer_config(
            multipart_threshold=self._config.multipart_threshold_bytes,
            max_concurrency=4,
            use_threads=True,
        )
        self._call(
            "upload_fileobj",
            client.upload_fileobj,
            spool,
            bucket,
            key,
            ExtraArgs=extra,
            Config=transfer,
            key=key,
        )

    # -- reads -------------------------------------------------------------- #
    def head(self, key: str) -> ObjectMetadata | None:
        self._require_configured()
        key = validate_object_key(key)
        try:
            response = self._call(
                "head_object",
                self.client.head_object,
                Bucket=self._config.bucket,
                Key=key,
                key=key,
            )
        except ObjectNotFound:
            return None
        custom = response.get("Metadata") or {}
        return ObjectMetadata(
            key=key,
            size_bytes=int(response.get("ContentLength", 0)),
            sha256=dict(custom).get(META_SHA256),
            content_type=response.get("ContentType"),
            etag=response.get("ETag"),
            last_modified=response.get("LastModified"),
            custom={str(k): str(v) for k, v in dict(custom).items()},
        )

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def verify_bytes(self, key: str) -> bool:
        """Re-read the object and compare its bytes with the recorded digest.

        This is the only statement in this adapter that the *stored bytes* are
        intact; ``head`` reports recorded metadata and never reads content.
        Returns ``False`` when no digest was recorded at upload (nothing to
        compare against, so a "verified" verdict would be a guess).
        """
        key = validate_object_key(key)
        metadata = self.head(key)
        if metadata is None:
            raise ObjectNotFound(f"{key!r} not found")
        expected = dict(metadata.custom).get(META_SHA256)
        if expected is None:
            return False
        import hashlib

        digest = hashlib.sha256()
        try:
            for chunk in self._iter_payload(key):
                digest.update(chunk)
        except ObjectStoreError:
            raise
        return digest.hexdigest() == expected

    def open_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        return b"".join(self.iter_chunks(key, max_bytes=max_bytes))

    def iter_chunks(
        self, key: str, *, chunk_size: int = 1 << 20, max_bytes: int | None = None
    ) -> Iterator[bytes]:
        """Stream an object, verifying the stored digest at the end.

        A consumer that stops early has consumed unverified bytes; a consumer that
        iterates to completion gets :class:`IntegrityMismatch` if the bytes do not
        hash to the identity recorded at upload time.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        key = validate_object_key(key)
        expected = self._recorded_digest(key)
        import hashlib

        digest = hashlib.sha256()
        total = 0
        for chunk in self._iter_payload(key, chunk_size=chunk_size):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ObjectTooLarge(f"object {key!r} exceeds the {max_bytes}-byte read bound")
            digest.update(chunk)
            yield chunk
        if expected is not None and digest.hexdigest() != expected:
            raise IntegrityMismatch(
                f"object {key!r} does not hash to the identity recorded at upload"
            )

    def _recorded_digest(self, key: str) -> str | None:
        """The digest recorded at upload time, read from object metadata."""
        metadata = self.head(key)
        if metadata is None:
            raise ObjectNotFound(f"{key!r} not found")
        return dict(metadata.custom).get(META_SHA256)

    def _iter_payload(self, key: str, *, chunk_size: int = 1 << 20) -> Iterator[bytes]:
        """Yield the raw stored bytes without any digest check."""
        self._require_configured()
        response = self._call(
            "get_object",
            self.client.get_object,
            Bucket=self._config.bucket,
            Key=key,
            key=key,
        )
        body = response.get("Body")
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    # -- deletion ----------------------------------------------------------- #
    def delete(self, key: str) -> DeleteResult:
        """Soft-delete on the branch; nothing is reclaimed and nothing is orphaned.

        S3 ``DeleteObject`` is idempotent and answers ``204`` whether or not the
        key existed, so ``existed`` is measured with a prior HEAD instead of being
        guessed from a successful delete.
        """
        self._require_configured()
        key = validate_object_key(key)
        existed = self.head(key) is not None
        try:
            self._call(
                "delete_object",
                self.client.delete_object,
                Bucket=self._config.bucket,
                Key=key,
                key=key,
            )
        except ObjectNotFound:
            return DeleteResult(key=key, existed=False, soft=True, reclaimed_bytes=0)
        return DeleteResult(key=key, existed=existed, soft=True, reclaimed_bytes=0)

    def list_keys(self, *, prefix: str = "", limit: int = 1000) -> list[str]:
        """Absolute object keys under ``prefix`` (default: this store's namespace).

        The provider is asked for ``<namespace>/[prefix]`` and the returned keys
        are the provider's own key strings, which are also what
        ``head``/``open_bytes``/``delete`` accept. A prefix outside the store's
        namespace is refused (never silently translated), so a caller cannot
        accidentally list another tenant's objects — or mistake another
        namespace for "no assets".
        """
        self._require_configured()
        if limit <= 0:
            raise ValueError("limit must be positive")
        effective = self._effective_prefix(prefix)
        keys: list[str] = []
        for item in self._iter_listing(effective):
            keys.append(item["Key"])
            if len(keys) >= limit:
                break
        return keys

    def _effective_prefix(self, prefix: str) -> str:
        """Resolve a caller prefix to a bucket-absolute one, or refuse it."""
        namespace = self._config.prefix
        if not prefix:
            return namespace
        validate_object_key(prefix)
        if not (prefix == namespace or prefix.startswith(f"{namespace}/")):
            raise InvalidObjectKey(
                f"prefix {prefix!r} is outside this store's namespace {namespace!r}"
            )
        return prefix

    def _iter_listing(self, search: str) -> Iterator[dict[str, Any]]:
        """Page through ``list_objects_v2`` until a page or the listing ends."""
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._config.bucket, "Prefix": search}
            if token:
                kwargs["ContinuationToken"] = token
            response = self._call(
                "list_objects_v2", self.client.list_objects_v2, **kwargs, key=search
            )
            for item in response.get("Contents", []) or []:
                yield {"Key": str(item.get("Key", "")), "Size": int(item.get("Size", 0))}
            if not response.get("IsTruncated"):
                return
            token = response.get("NextContinuationToken")
            if not token:
                raise ObjectStoreError(
                    "list_objects_v2 returned a truncated page without NextContinuationToken"
                )

    def measure_namespace(self) -> tuple[int, int]:
        """Measure ``(bytes, objects)`` actually present under our namespace.

        This is a listing-based measurement of Nagar's own objects — deliberately
        not a quota, and never added to another backend's numbers.
        """
        self._require_configured()
        total = 0
        count = 0
        for item in self._iter_listing(self._config.prefix):
            total += item["Size"]
            count += 1
        return total, count

    # -- truth -------------------------------------------------------------- #
    def capacity(self) -> CapacityReport:
        """No published quota API ⇒ an honest ``UNKNOWN``.

        ``measured_unique_bytes`` is intentionally left empty here: computing it
        requires a full listing, which belongs to an explicit measurement call
        (:meth:`measure_namespace`), not to a cheap status read.
        """
        return CapacityReport(
            backend=self.name,
            kind=CapacityKind.UNKNOWN,
            measured_at=utcnow(),
            source=(
                "Neon Object Storage publishes no quota/usage API (docs: storage/s3-compatibility)"
            ),
            note=(
                "soft deletes and stored-but-unenforced lifecycle rules mean deleted bytes may "
                "still be billed; use measure_namespace() for a listing-based measurement"
            ),
        )

    def health(self) -> HealthReport:
        if not self.is_configured():
            return HealthReport(
                backend=self.name,
                ok=False,
                detail="not configured",
                checked_at=utcnow(),
            )
        try:
            self._call(
                "head_bucket",
                self.client.head_bucket,
                Bucket=self._config.bucket,
                key=self._config.bucket,
            )
        except ObjectStoreError as exc:
            return HealthReport(
                backend=self.name,
                ok=False,
                detail=f"{exc.code}: unreachable",
                checked_at=utcnow(),
            )
        return HealthReport(
            backend=self.name, ok=True, detail="bucket reachable", checked_at=utcnow()
        )


__all__ = [
    "DEFAULT_PREFIX",
    "META_SHA256",
    "NEON_ENDPOINT_SUFFIX",
    "NeonObjectStorage",
    "NeonStorageConfig",
    "build_neon_object_store",
]
