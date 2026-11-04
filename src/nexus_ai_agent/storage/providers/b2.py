"""Backblaze B2 (S3-compatible) storage provider — the maintenance blob tier.

B2 is a drop-in alternative to Cloudflare R2 for the *same* narrow job: the
stateless maintenance artifacts that :mod:`nexus_ai_agent.maintenance.backup`
produces (database dumps under ``backups/db/``) and that
:mod:`nexus_ai_agent.maintenance.housekeeping` prunes. It is **not** part of
the user-file cloud round-robin (``storage/unified_cloud.py``) and it never
serves the Asset Library.

Why a second S3-compatible provider instead of reusing :class:`R2Provider`
----------------------------------------------------------------------
B2 and R2 share the S3 wire protocol but *not* their addressing model, and the
differences are exactly the ones that decide whether a request is routed to the
right place:

* **R2** derives the endpoint host from the account id
  (``https://<account_id>.r2.cloudflarestorage.com``) and uses the literal
  region ``auto``; there is nothing to validate and nothing to mismatch.
* **B2** takes an explicit ``endpoint`` and an explicit ``region`` that must
  *agree* (``s3.us-east-005.backblazeb2.com`` ⇔ ``us-east-005``), requires a
  non-AWS host, and — for boto3 ≥ 1.36 — rejects the default data-integrity
  checksum headers unless ``request_checksum_calculation`` is relaxed.

Forcing those B2-only rules into ``R2Provider`` would make the R2 provider
carry a configuration surface it can never use (and vice versa). The two
providers therefore stay separate classes that implement the *same*
``StorageProvider`` protocol, and the maintenance tier selects one through the
single factory ``nexus_ai_agent.maintenance.backup.build_blob_provider``.

Configuration (five ``NEXUS_B2_*`` settings; never hardcoded, never logged)
---------------------------------------------------------------------------
``NEXUS_B2_KEY_ID``           application key id      → ``aws_access_key_id``
``NEXUS_B2_APPLICATION_KEY``  application key secret  → ``aws_secret_access_key``
``NEXUS_B2_BUCKET``           bucket name
``NEXUS_B2_ENDPOINT``         S3 endpoint, must be https
``NEXUS_B2_REGION``           region, must match the endpoint

Fail-closed rules (validated *before* any network call, never after):

1. every field is present (an absent credential raises, it never degrades to a
   local store or to another provider);
2. the endpoint is ``https://`` and its host ends in ``.backblazeb2.com`` (a
   non-B2 host is a misconfiguration, and refusing it also removes an SSRF
   surface where a swapped endpoint would silently exfiltrate the credentials);
3. the endpoint host and the region agree (``s3.<region>.backblazeb2.com``);
4. the bucket name is a valid B2 name;
5. every remote key is confined to the ``backups/db/`` prefix, so a bug in a
   caller cannot read or delete a sibling namespace.

Error messages name the *setting*, never its value.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nexus_ai_agent.config.settings import Settings

from .base import ProviderUnavailable, StorageError

#: Backblaze endpoint host suffix. A host that does not end in this is not B2,
#: and pointing the credentials at an arbitrary host is the misconfiguration
#: this provider refuses (also an SSRF/credential-exfiltration boundary).
B2_HOST_SUFFIX = ".backblazeb2.com"

#: Every remote operation is confined to the database-backup namespace. The
#: Asset Library and the RAG document tier are NOT served by this provider, so
#: a key outside the prefix is a bug, not a feature.
BACKUP_PREFIX = "backups/db/"

#: B2 bucket naming rule (6-63 chars; letters, digits, hyphens and periods; no
#: leading/trailing period; no ``..``; not an IPv4 literal; not ``b2-``-prefixed).
#: We keep the conservative subset B2 itself recommends (lowercase, digits,
#: hyphens) so a name that passes here also works with virtual-hosted addressing.
_B2_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{4,61}[a-z0-9]$")


def _normalize_backup_prefix(prefix: str) -> str:
    """Return a slash-terminated prefix confined to ``backups/db/``.

    An empty prefix means "the whole backup namespace" for this provider, so it
    is mapped to ``backups/db/``; a bare ``backups/db`` is given its trailing
    slash so a listing cannot also match a sibling like ``backups/db-old``. A
    prefix outside the namespace is slash-terminated but still fails
    ``_guard_key``, which rejects it with a clear error rather than silently
    widening the listing.
    """
    if not prefix:
        return BACKUP_PREFIX
    return prefix if prefix.endswith("/") else f"{prefix}/"


#: ``s3.<region>.backblazeb2.com`` — the region is the second host label.
_ENDPOINT_REGION_RE = re.compile(r"^s3\.([a-z0-9-]+)\.backblazeb2\.com$")


def validate_endpoint(endpoint: str) -> str:
    """Return the host of a valid B2 HTTPS endpoint, or raise.

    Rejects a non-https scheme (B2 rejects plain HTTP anyway, and a downgrade
    would expose the SigV4-signed request), a host that is not a B2 endpoint,
    and an endpoint that carries a path/query/credentials.
    """
    parts = urlsplit(endpoint)
    if parts.scheme != "https":
        raise ProviderUnavailable("NEXUS_B2_ENDPOINT must be an https:// URL")
    if parts.username or parts.password:
        raise ProviderUnavailable("NEXUS_B2_ENDPOINT must not embed credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ProviderUnavailable(
            "NEXUS_B2_ENDPOINT must be a bare host (no path, query or fragment)"
        )
    host = (parts.hostname or "").lower()
    # A single guard covers both boundaries: the host must be a canonical
    # ``s3.<region>.backblazeb2.com``. A non-B2 host would send the application
    # key elsewhere (SSRF / credential exfiltration), and a B2-suffixed host
    # without the ``s3.<region>`` shape has no readable region, so an endpoint
    # and region could disagree unnoticed. Requiring the canonical shape means
    # the region is always verifiable.
    if not _ENDPOINT_REGION_RE.match(host):
        raise ProviderUnavailable(
            "NEXUS_B2_ENDPOINT must be the canonical B2 endpoint "
            "'s3.<region>.backblazeb2.com' (a non-B2 host would send the "
            "application key elsewhere, and the region must be verifiable "
            "against NEXUS_B2_REGION)"
        )
    return host


def endpoint_region(endpoint: str) -> str | None:
    """The region encoded in ``s3.<region>.backblazeb2.com``, or ``None``."""
    host = validate_endpoint(endpoint)
    match = _ENDPOINT_REGION_RE.match(host)
    return match.group(1) if match else None


class B2Provider:
    """Backblaze B2 provider speaking the S3 API via boto3 (SigV4).

    The boto3 client is created lazily and can be injected (``client=``) so unit
    tests run fully offline, exactly like :class:`R2Provider`.
    """

    name = "b2"

    def __init__(
        self,
        *,
        key_id: str,
        application_key: str,
        bucket: str,
        endpoint: str,
        region: str,
        client: Any | None = None,
    ) -> None:
        self._key_id = key_id
        self._application_key = application_key
        self._bucket = bucket
        self._endpoint = endpoint
        self._region = region
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> B2Provider:
        """Build a provider from the five ``NEXUS_B2_*`` settings fields."""
        return cls(
            key_id=settings.b2_key_id or "",
            application_key=settings.b2_application_key or "",
            bucket=settings.b2_bucket or "",
            endpoint=settings.b2_endpoint or "",
            region=settings.b2_region or "",
        )

    def is_configured(self) -> bool:
        """True only when all five fields are present *and* syntactically valid.

        A value that is present but malformed is worse than an absent one: the
        caller proceeds and boto3 fails deep inside the SDK with a message that
        names neither the setting nor the fix. This returns ``False`` so the
        maintenance job refuses early with :meth:`configuration_error`.
        """
        return self.configuration_error() is None

    def configuration_error(self) -> str | None:
        """Return the first actionable configuration problem, else ``None``.

        Names the offending setting and never the value.
        """
        if not (self._key_id and self._application_key and self._bucket):
            return (
                "B2 is not configured — set NEXUS_B2_KEY_ID, "
                "NEXUS_B2_APPLICATION_KEY and NEXUS_B2_BUCKET"
            )
        if not self._endpoint:
            return "B2 is not configured — set NEXUS_B2_ENDPOINT"
        if not self._region:
            return "B2 is not configured — set NEXUS_B2_REGION"
        try:
            host = validate_endpoint(self._endpoint)
        except ProviderUnavailable as exc:
            return str(exc)
        # ``validate_endpoint`` guarantees the canonical shape, so the encoded
        # region is always present here.
        encoded = endpoint_region(self._endpoint)
        if encoded != self._region.strip().lower():
            return (
                "NEXUS_B2_ENDPOINT and NEXUS_B2_REGION disagree — "
                f"the endpoint names region {encoded!r}"
            )
        bucket = self._bucket
        if bucket != bucket.strip():
            return "NEXUS_B2_BUCKET has leading or trailing whitespace"
        if not _B2_BUCKET_RE.match(bucket):
            return (
                "NEXUS_B2_BUCKET is not a valid bucket name "
                "(6-63 chars, lowercase letters/digits/hyphens, must start and "
                "end alphanumeric)"
            )
        if not host:
            return "NEXUS_B2_ENDPOINT is not a usable host"
        return None

    def _require_configured(self) -> None:
        reason = self.configuration_error()
        if reason is not None:
            raise ProviderUnavailable(reason)

    @property
    def client(self) -> Any:
        """Lazily constructed boto3 S3 client pointed at the B2 endpoint."""
        if self._client is None:
            self._require_configured()
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                region_name=self._region,
                aws_access_key_id=self._key_id,
                aws_secret_access_key=self._application_key,
                # B2 compatibility: boto3 >= 1.36 computes data-integrity
                # checksums by default and B2 rejects the resulting
                # ``x-amz-sdk-checksum-algorithm`` header on PutObject
                # ("Unsupported header ..."). Relax both directions, and pin
                # path-style addressing (B2 supports both, path-style is the
                # safe default for a configurable endpoint).
                config=Config(
                    signature_version="s3v4",
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                    s3={"addressing_style": "path"},
                ),
            )
        return self._client

    @staticmethod
    def _guard_key(remote_key: str) -> None:
        """Confine every key to the backup prefix before any network call."""
        if not remote_key.startswith(BACKUP_PREFIX) or ".." in remote_key.split("/"):
            raise StorageError(
                f"B2 key {remote_key!r} is outside the {BACKUP_PREFIX!r} backup prefix"
            )

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        self._require_configured()
        self._guard_key(remote_key)
        client = self.client
        bucket = self._bucket

        def _do() -> None:
            client.upload_file(str(local_path), bucket, remote_key)

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise StorageError(f"B2 upload failed for '{remote_key}': {e}") from e

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        self._require_configured()
        self._guard_key(remote_key)
        client = self.client
        bucket = self._bucket
        local_path.parent.mkdir(parents=True, exist_ok=True)

        def _do() -> None:
            client.download_file(bucket, remote_key, str(local_path))

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise StorageError(f"B2 download failed for '{remote_key}': {e}") from e

    async def list_files(self, *, prefix: str = "") -> list[str]:
        self._require_configured()
        # The remote request must use the *normalized, slash-terminated* prefix.
        # Sending the bare ``backups/db`` would also match a sibling namespace
        # such as ``backups/db-old``, and an empty prefix would list the whole
        # bucket — both hand the caller keys outside the protected namespace.
        effective_prefix = _normalize_backup_prefix(prefix)
        self._guard_key(effective_prefix)
        client = self.client
        bucket = self._bucket
        keys: list[str] = []
        token: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": effective_prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                response = await asyncio.to_thread(client.list_objects_v2, **kwargs)
                for item in response.get("Contents", []):
                    keys.append(item["Key"])
                if not response.get("IsTruncated"):
                    break
                token = response.get("NextContinuationToken")
                if not token:
                    raise StorageError(
                        "B2 list failed: provider returned a truncated page without "
                        "NextContinuationToken"
                    )
        except ProviderUnavailable:
            raise
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"B2 list failed (prefix='{prefix}'): {e}") from e
        return keys

    async def delete_objects(self, *, keys: list[str]) -> int:
        """Delete objects in batches of up to 1000; return the deleted count."""
        self._require_configured()
        if not keys:
            return 0
        for key in keys:
            self._guard_key(key)
        client = self.client
        bucket = self._bucket
        deleted = 0
        try:
            for start in range(0, len(keys), 1000):
                batch = keys[start : start + 1000]
                response = await asyncio.to_thread(
                    client.delete_objects,
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                )
                errors = response.get("Errors", [])
                if errors:
                    codes = sorted(
                        {
                            str(error.get("Code", "Unknown"))
                            for error in errors
                            if isinstance(error, dict)
                        }
                    )
                    code_summary = ",".join(codes) if codes else "Unknown"
                    raise StorageError(
                        "B2 delete partial failure: "
                        f"{len(errors)} provider error(s), codes={code_summary}"
                    )
                # B2 (like AWS with Quiet=True) omits successful keys from
                # ``Deleted``, so the batch is counted once the error check
                # above passes.
                deleted += len(batch)
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"B2 delete failed ({len(keys)} keys): {e}") from e
        return deleted


__all__ = ["B2Provider", "BACKUP_PREFIX", "B2_HOST_SUFFIX", "endpoint_region", "validate_endpoint"]
