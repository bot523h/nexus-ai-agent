from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus_ai_agent.storage.providers.base import StorageError
from nexus_ai_agent.storage.unified_cloud import (
    CloudProviderInfo,
    UnifiedCloudStorage,
    _DropboxProvider,
)


class RecordingProvider:
    name = "recording"

    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str]] = []

    def is_configured(self) -> bool:
        return True

    async def upload(self, *, local_path: Path, remote_key: str) -> None:
        self.uploads.append((local_path, remote_key))

    async def download(self, *, remote_key: str, local_path: Path) -> None:
        local_path.write_bytes(b"ok")

    async def list_files(self, *, prefix: str = "") -> list[str]:
        return []


class LogSpy:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.records.append(("info", event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.records.append(("warning", event, kwargs))


class AsyncClientRecorder:
    requests: list[dict[str, Any]] = []

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> AsyncClientRecorder:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append({"url": url, **kwargs})
        return httpx.Response(200, content=b"{}")


def test_provider_usage_percent_uses_bytes_once() -> None:
    info = CloudProviderInfo("dropbox", free_gb=2.0, configured=True)
    info.used_bytes = 1 * 1024**3

    assert info.used_gb == 1.0
    assert info.usage_percent == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_unified_cloud_rejects_traversal_remote_keys_before_provider(tmp_path: Path) -> None:
    provider = RecordingProvider()
    storage = UnifiedCloudStorage(mega_provider=provider)
    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")

    result = await storage.upload_file(local, remote_key="../escape.bin")

    assert result["success"] is False
    assert "remote_key" in result["error"]
    assert provider.uploads == []


@pytest.mark.asyncio
async def test_unified_cloud_success_log_uses_fingerprint_not_object_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = RecordingProvider()
    storage = UnifiedCloudStorage(mega_provider=provider)
    local = tmp_path / "payload.bin"
    local.write_bytes(b"payload")
    log_spy = LogSpy()
    monkeypatch.setattr("nexus_ai_agent.storage.unified_cloud.log", log_spy)

    result = await storage.upload_file(local, remote_key="private/family-video.mp4")

    assert result["success"] is True
    assert isinstance(result["remote_key_sha256"], str)
    assert len(result["remote_key_sha256"]) == 64
    assert log_spy.records == [
        (
            "info",
            "cloud_upload_ok",
            {"provider": "recording", "remote_key_sha256": result["remote_key_sha256"], "size": 7},
        )
    ]
    assert "private/family-video.mp4" not in repr(log_spy.records)


@pytest.mark.asyncio
async def test_dropbox_api_arg_is_valid_json_for_adversarial_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClientRecorder)
    AsyncClientRecorder.requests = []
    local = tmp_path / "payload.txt"
    local.write_text("x", encoding="utf-8")
    key = 'folder/quote" , "mode":"overwrite" , "path":"/pwn.txt'

    await _DropboxProvider(token="token").upload(local_path=local, remote_key=key)

    header = AsyncClientRecorder.requests[0]["headers"]["Dropbox-API-Arg"]
    payload = json.loads(header)
    assert payload["path"] == f"/NEXUS/{key}"
    assert payload["mode"] == "add"


@pytest.mark.asyncio
async def test_dropbox_rejects_control_character_remote_key(tmp_path: Path) -> None:
    local = tmp_path / "payload.txt"
    local.write_text("x", encoding="utf-8")

    with pytest.raises(StorageError, match="remote_key"):
        await _DropboxProvider(token="token").upload(local_path=local, remote_key="bad\nkey.txt")


@pytest.mark.asyncio
async def test_unified_cloud_rejects_explicit_empty_remote_key(tmp_path: Path) -> None:
    """An explicit empty key is invalid; only ``None`` falls back to the name."""
    local = tmp_path / "payload.txt"
    local.write_text("x", encoding="utf-8")
    storage = UnifiedCloudStorage(mega_provider=RecordingProvider())

    result = await storage.upload_file(local, remote_key="")
    assert result["success"] is False
    assert result["remote_key"] is None
    assert "remote_key" in result["error"]
