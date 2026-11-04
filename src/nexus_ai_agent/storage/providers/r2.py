"""Cloudflare R2 (S3-compatible) storage provider — the "technical blob" tier.

R2 is deliberately NOT part of the user-file round-robin in
``storage/unified_cloud.py``. It is a separate routing branch reserved for
stateless maintenance artifacts: database backups and heavy RAG documents
(see ``AIStorageManager.is_blob_key``).

Known compatibility trap: newer boto3/botocore versions default to
``when_supported`` checksum calculation, which R2 rejects on some
operations. The client is therefore always built with
``Config(request_checksum_calculation="when_required")``.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from nexus_ai_agent.config.settings import Settings

from .base import ProviderUnavailable, StorageError

# R2 caps presigned URL lifetime at 7 days (S3 allows more; R2 does not).
PRESIGN_MAX_SECONDS = 7 * 24 * 60 * 60  # 604800

# Cloudflare R2 bucket naming rule (docs: "Bucket names must be 3-63 chars,
# lowercase alphanumeric and hyphens, start/end with alphanumeric").
# A value that does not match makes boto3 raise an opaque ``ParamValidationError``
# ("Invalid bucket name ...") from deep inside the SDK, so it is validated here
# where the message can name the offending setting.
_R2_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class R2Provider:
    """Cloudflare R2 provider speaking the S3 API via boto3.

    The boto3 client is created lazily and can be injected (``client=``)
    so unit tests run fully offline.
    """

    name = "r2"

    def __init__(
        self,
        *,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        client: Any | None = None,
    ) -> None:
        self._account_id = account_id
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._bucket = bucket
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> R2Provider:
        """Build a provider from the four ``R2_*`` settings fields."""
        return cls(
            account_id=settings.r2_account_id or "",
            access_key_id=settings.r2_access_key_id or "",
            secret_access_key=settings.r2_secret_access_key or "",
            bucket=settings.r2_bucket or "",
        )

    def is_configured(self) -> bool:
        return bool(
            self._account_id and self._access_key_id and self._secret_access_key and self._bucket
        )

    def invalid_bucket_reason(self) -> str | None:
        """Return a diagnostic when the bucket value cannot be used, else ``None``.

        A syntactically invalid ``R2_BUCKET`` (whitespace, uppercase, too short)
        is worse than an absent one: :meth:`is_configured` is True, so the caller
        proceeds and boto3 fails deep inside with ``ParamValidationError: Invalid
        bucket name`` — a message that names neither the setting nor the fix.
        This turns that into a typed, actionable refusal at the boundary.
        """
        bucket = self._bucket
        if not bucket:
            return None
        if bucket != bucket.strip():
            return "R2_BUCKET has leading or trailing whitespace"
        if not _R2_BUCKET_RE.match(bucket):
            return (
                f"R2_BUCKET={bucket!r} is not a valid bucket name "
                "(3-63 chars, lowercase letters/digits/hyphens, must start and end alphanumeric)"
            )
        return None

    def _require_bucket(self) -> None:
        reason = self.invalid_bucket_reason()
        if reason is not None:
            raise ProviderUnavailable(reason)

    @property
    def client(self) -> Any:
        """Lazily constructed boto3 S3 client pointed at the R2 endpoint."""
        if self._client is None:
            if not self.is_configured():
                raise ProviderUnavailable("R2 credentials are not configured")
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=f"https://{self._account_id}.r2.cloudflarestorage.com",
                region_name="auto",
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                # R2 compatibility: only compute checksums when the
                # operation requires them (see module docstring).
                config=Config(request_checksum_calculation="when_required"),
            )
        return self._client

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        if not self.is_configured():
            raise ProviderUnavailable("R2 is not configured")
        self._require_bucket()
        client = self.client
        bucket = self._bucket

        def _do() -> None:
            client.upload_file(str(local_path), bucket, remote_key)

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise StorageError(f"R2 upload failed for '{remote_key}': {e}") from e

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        if not self.is_configured():
            raise ProviderUnavailable("R2 is not configured")
        self._require_bucket()
        client = self.client
        bucket = self._bucket
        local_path.parent.mkdir(parents=True, exist_ok=True)

        def _do() -> None:
            client.download_file(bucket, remote_key, str(local_path))

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            raise StorageError(f"R2 download failed for '{remote_key}': {e}") from e

    async def list_files(self, *, prefix: str = "") -> list[str]:
        if not self.is_configured():
            raise ProviderUnavailable("R2 is not configured")
        self._require_bucket()
        client = self.client
        bucket = self._bucket
        keys: list[str] = []
        token: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
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
                        "R2 list failed: provider returned a truncated page without "
                        "NextContinuationToken"
                    )
        except ProviderUnavailable:
            raise
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"R2 list failed (prefix='{prefix}'): {e}") from e
        return keys

    async def delete_objects(self, *, keys: list[str]) -> int:
        """Delete objects in batches of up to 1000; return the deleted count."""
        if not self.is_configured():
            raise ProviderUnavailable("R2 is not configured")
        self._require_bucket()
        if not keys:
            return 0
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
                        "R2 delete partial failure: "
                        f"{len(errors)} provider error(s), codes={code_summary}"
                    )
                # AWS omits successful keys from ``Deleted`` when Quiet=True, so
                # the returned count is unreliable; the whole batch is deleted
                # once the error check above passes.
                deleted += len(batch)
        except StorageError:
            raise
        except Exception as e:
            raise StorageError(f"R2 delete failed ({len(keys)} keys): {e}") from e
        return deleted

    def generate_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Presigned GET URL for ``key``. R2 caps the TTL at 7 days."""
        if expires_in <= 0:
            raise ValueError(f"expires_in must be positive, got {expires_in}")
        if expires_in > PRESIGN_MAX_SECONDS:
            raise ValueError(
                f"R2 presigned URL TTL is capped at 7 days "
                f"({PRESIGN_MAX_SECONDS}s); got {expires_in}s"
            )
        self._require_bucket()
        client = self.client
        return str(
            client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        )
