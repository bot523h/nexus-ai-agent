"""Unit tests for the Cloudflare R2 provider and the technical blob tier.

Every test runs fully offline: the boto3 client is replaced by an
in-memory stub mirroring exactly the calls ``R2Provider`` makes
(``upload_file``, ``download_file``, ``list_objects_v2``,
``delete_objects``, ``generate_presigned_url``). No network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus_ai_agent.storage.ai_storage_manager import (
    BLOB_KEY_PREFIXES,
    AIStorageManager,
    is_blob_key,
)
from nexus_ai_agent.storage.providers.base import (
    ProviderConfig,
    ProviderUnavailable,
    StorageError,
)
from nexus_ai_agent.storage.providers.r2 import PRESIGN_MAX_SECONDS, R2Provider

# ── in-memory boto3 stub ──────────────────────────────────────────────


class FakeR2Client:
    """Mimics the boto3 S3 client surface used by R2Provider (no network)."""

    def __init__(self, *, page_size: int = 2) -> None:
        self.objects: dict[str, bytes] = {}
        self.presign_calls: list[tuple[str, dict[str, str], int]] = []
        self.deleted: list[str] = []
        self._page_size = page_size

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
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
        if truncated:
            response["NextContinuationToken"] = str(start + self._page_size)
        return response

    def delete_objects(self, *, Bucket: str, Delete: dict) -> dict:
        assert Bucket == "bkt"  # the real boto3 call requires the bucket
        objects = Delete["Objects"]
        for obj in objects:
            self.objects.pop(obj["Key"], None)
            self.deleted.append(obj["Key"])
        return {"Deleted": [{"Key": obj["Key"]} for obj in objects]}

    def generate_presigned_url(self, ClientMethod: str, Params: dict, ExpiresIn: int) -> str:
        self.presign_calls.append((ClientMethod, dict(Params), ExpiresIn))
        return f"https://signed.example/{Params['Bucket']}/{Params['Key']}"


def make_provider(**overrides: object) -> tuple[R2Provider, FakeR2Client]:
    client = FakeR2Client()
    kwargs: dict = {
        "account_id": "acc",
        "access_key_id": "ak",
        "secret_access_key": "sk",
        "bucket": "bkt",
        "client": client,
    }
    kwargs.update(overrides)
    return R2Provider(**kwargs), client  # type: ignore[arg-type]


# ── is_configured ─────────────────────────────────────────────────────


def test_is_configured_true_with_all_fields() -> None:
    provider, _ = make_provider()
    assert provider.is_configured() is True


@pytest.mark.parametrize("missing", ["account_id", "access_key_id", "secret_access_key", "bucket"])
def test_is_configured_false_when_a_field_is_missing(missing: str) -> None:
    provider, _ = make_provider(**{missing: ""})
    assert provider.is_configured() is False


def test_unconfigured_provider_raises_on_all_ops() -> None:
    provider, _ = make_provider(account_id="")
    assert provider.is_configured() is False
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.upload(local_path=Path("/tmp/x"), remote_key="k"))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.download(remote_key="k", local_path=Path("/tmp/x")))
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.list_files())
    # Without an injected client the lazy boto3 construction must refuse.
    bare = R2Provider(account_id="", access_key_id="", secret_access_key="", bucket="")
    with pytest.raises(ProviderUnavailable):
        bare.generate_presigned_url("k")


# ── upload / download / list / delete ────────────────────────────────


def test_upload_and_download_roundtrip(tmp_path: Path) -> None:
    provider, client = make_provider()
    src = tmp_path / "dump.sql"
    src.write_bytes(b"nexus-backup-payload")
    dst = tmp_path / "out" / "dump.sql"

    asyncio.run(provider.upload(local_path=src, remote_key="backups/db/x.sql"))
    assert client.objects["backups/db/x.sql"] == b"nexus-backup-payload"

    asyncio.run(provider.download(remote_key="backups/db/x.sql", local_path=dst))
    assert dst.read_bytes() == b"nexus-backup-payload"


def test_download_missing_key_raises_storage_error(tmp_path: Path) -> None:
    provider, _ = make_provider()
    with pytest.raises(StorageError):
        asyncio.run(provider.download(remote_key="nope", local_path=tmp_path / "nope.bin"))


def test_list_files_paginates(tmp_path: Path) -> None:
    provider, client = make_provider()  # stub page size = 2
    client.objects = {f"backups/db/{i}.sql": b"" for i in range(5)}

    keys = asyncio.run(provider.list_files(prefix="backups/db/"))
    assert keys == sorted(client.objects)
    assert len(keys) == 5  # more than one page → pagination loop exercised


def test_delete_objects_batches(tmp_path: Path) -> None:
    provider, client = make_provider()
    client.objects = {f"backups/db/old/{i}.sql": b"" for i in range(3)}

    deleted = asyncio.run(
        provider.delete_objects(keys=["backups/db/old/0.sql", "backups/db/old/1.sql"])
    )
    assert deleted == 2
    assert client.deleted == ["backups/db/old/0.sql", "backups/db/old/1.sql"]
    assert set(client.objects) == {"backups/db/old/2.sql"}


def test_delete_objects_empty_is_noop() -> None:
    provider, client = make_provider()
    assert asyncio.run(provider.delete_objects(keys=[])) == 0
    assert client.deleted == []


# ── presigned URLs (7-day R2 cap) ─────────────────────────────────────


def test_presigned_url_valid() -> None:
    provider, client = make_provider()
    url = provider.generate_presigned_url("backups/db/x.sql", 3600)
    assert url == "https://signed.example/bkt/backups/db/x.sql"
    expected_params = {"Bucket": "bkt", "Key": "backups/db/x.sql"}
    assert client.presign_calls == [("get_object", expected_params, 3600)]


def test_presigned_url_accepts_exactly_seven_days() -> None:
    provider, client = make_provider()
    provider.generate_presigned_url("k", PRESIGN_MAX_SECONDS)
    assert client.presign_calls[0][2] == PRESIGN_MAX_SECONDS


def test_presigned_url_rejects_more_than_seven_days() -> None:
    provider, client = make_provider()
    with pytest.raises(ValueError, match="7 days"):
        provider.generate_presigned_url("k", PRESIGN_MAX_SECONDS + 1)
    with pytest.raises(ValueError, match="7 days"):
        provider.generate_presigned_url("k", 30 * 24 * 60 * 60)  # 30 days
    assert client.presign_calls == []  # never reached the client


def test_presigned_url_rejects_non_positive_ttl() -> None:
    provider, _ = make_provider()
    with pytest.raises(ValueError, match="positive"):
        provider.generate_presigned_url("k", 0)
    with pytest.raises(ValueError, match="positive"):
        provider.generate_presigned_url("k", -5)


# ── blob-tier routing in AIStorageManager ─────────────────────────────


class _RecordingProvider:
    """Stands in for a user-file provider and records every call."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.uploaded: list[str] = []
        self.downloaded: list[str] = []

    def is_configured(self) -> bool:
        return True

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        self.uploaded.append(remote_key)

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        self.downloaded.append(remote_key)

    async def list_files(self, *, prefix: str = "") -> list[str]:
        return []


def make_manager(tmp_path: Path) -> tuple[AIStorageManager, R2Provider, list[_RecordingProvider]]:
    config = ProviderConfig(
        github_token="t",
        github_repo="o/r",
        mega_email="a@b.c",
        mega_password="p",
        r2_account_id="acc",
        r2_access_key_id="ak",
        r2_secret_access_key="sk",
        r2_bucket="bkt",
    )
    manager = AIStorageManager(cache_dir=tmp_path / "cache", config=config)
    assert manager.r2 is not None and manager.r2.is_configured()
    r2_client = FakeR2Client()
    manager.r2._client = r2_client  # inject offline client

    # Swap the user-file providers for recording fakes (they are configured,
    # so the round-robin WOULD pick them if the blob tier leaked into it).
    fakes = [_RecordingProvider(name) for name in ("github_releases", "mega")]
    manager.github = fakes[0]  # type: ignore[assignment]
    manager.mega = fakes[1]  # type: ignore[assignment]
    return manager, manager.r2, fakes


def test_blob_key_prefixes_and_helper() -> None:
    assert "backups/" in BLOB_KEY_PREFIXES and "rag-docs/" in BLOB_KEY_PREFIXES
    assert is_blob_key("backups/db/20260920-031700/nexus-pg.sql")
    assert is_blob_key("rag-docs/doc-1.pdf")
    assert not is_blob_key("photos/cat.png")
    assert not is_blob_key("backups-are-nice.txt")  # prefix, not substring


def test_blob_upload_goes_to_r2_only(tmp_path: Path) -> None:
    manager, r2, fakes = make_manager(tmp_path)
    f = tmp_path / "nexus-pg.sql"
    f.write_bytes(b"dump")

    asyncio.run(manager.upload(local_path=f, remote_key="backups/db/20260920-031700/nexus-pg.sql"))

    assert r2._client.objects == {  # type: ignore[attr-defined]
        "backups/db/20260920-031700/nexus-pg.sql": b"dump"
    }
    for fake in fakes:
        assert fake.uploaded == []  # round-robin never touched


def test_user_file_never_reaches_r2(tmp_path: Path) -> None:
    manager, r2, fakes = make_manager(tmp_path)
    f = tmp_path / "photo.png"
    f.write_bytes(b"jpeg-data")

    asyncio.run(manager.upload(local_path=f, remote_key="photos/photo.png"))

    assert fakes[0].uploaded == ["photos/photo.png"]  # github_releases preferred
    assert r2._client.objects == {}  # type: ignore[attr-defined] — R2 untouched


def test_r2_never_enters_user_file_candidates(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    upload_names = [p.name for p in manager._upload_candidates(preferred="github_releases")]
    download_names = [p.name for p in manager._download_candidates()]
    assert "r2" not in upload_names
    assert "r2" not in download_names
    assert manager.get_best_provider(size_bytes=10**9) in {"mega", "github_releases", "rclone"}


def test_blob_download_uses_r2_only(tmp_path: Path) -> None:
    manager, r2, fakes = make_manager(tmp_path)
    key = "backups/db/20260920-031700/nexus-pg.sql"
    r2._client.objects[key] = b"dump"  # type: ignore[attr-defined]
    dst = tmp_path / "restored.sql"

    result = asyncio.run(manager.download(remote_key=key, local_path=dst))

    assert result == dst
    assert dst.read_bytes() == b"dump"
    for fake in fakes:
        assert fake.downloaded == []


def test_blob_key_without_r2_configured_raises(tmp_path: Path) -> None:
    config = ProviderConfig()  # nothing configured at all
    manager = AIStorageManager(cache_dir=tmp_path / "cache", config=config)
    assert manager.r2 is None
    f = tmp_path / "dump.sql"
    f.write_bytes(b"dump")
    with pytest.raises(ProviderUnavailable):
        asyncio.run(manager.upload(local_path=f, remote_key="backups/db/x.sql"))


def test_blob_presigned_url_via_manager(tmp_path: Path) -> None:
    manager, _, _ = make_manager(tmp_path)
    url = manager.blob_presigned_url(remote_key="rag-docs/doc-1.pdf", expires_in=600)
    assert "rag-docs/doc-1.pdf" in url
