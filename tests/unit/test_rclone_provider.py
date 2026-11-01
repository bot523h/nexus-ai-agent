from __future__ import annotations

import asyncio

import pytest

from nexus_ai_agent.storage.providers.rclone import RcloneOperationError, RcloneProvider


class _Process:
    def __init__(self, *, code: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = code
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


async def _spawn(process: _Process) -> _Process:
    return process


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> RcloneProvider:
    monkeypatch.setattr(
        "nexus_ai_agent.storage.providers.rclone.shutil.which", lambda _: "/usr/bin/rclone"
    )
    return RcloneProvider(remote="gdrive:nexus")


def test_successful_non_empty_listing(
    configured: RcloneProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nexus_ai_agent.storage.providers.rclone.asyncio.create_subprocess_exec",
        lambda *args, **kwargs: _spawn(_Process(code=0, stdout=b"a.txt\ndir/b.txt\n")),
    )
    assert asyncio.run(configured.list_files(prefix="backups/")) == [
        "backups/a.txt",
        "backups/dir/b.txt",
    ]


def test_successful_empty_listing_is_empty(
    configured: RcloneProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nexus_ai_agent.storage.providers.rclone.asyncio.create_subprocess_exec",
        lambda *args, **kwargs: _spawn(_Process(code=0)),
    )
    assert asyncio.run(configured.list_files()) == []


@pytest.mark.parametrize(
    ("stderr", "category", "retryable"),
    [
        (b"ERROR : invalid credentials", "authentication", False),
        (b"ERROR : permission denied", "permission", False),
        (b"ERROR : temporary network timeout", "network", True),
    ],
)
def test_failed_listing_never_becomes_empty(
    configured: RcloneProvider,
    monkeypatch: pytest.MonkeyPatch,
    stderr: bytes,
    category: str,
    retryable: bool,
) -> None:
    monkeypatch.setattr(
        "nexus_ai_agent.storage.providers.rclone.asyncio.create_subprocess_exec",
        lambda *args, **kwargs: _spawn(_Process(code=1, stderr=stderr)),
    )
    with pytest.raises(RcloneOperationError) as exc_info:
        asyncio.run(configured.list_files())
    assert exc_info.value.category == category
    assert exc_info.value.retryable is retryable
    assert "empty" not in str(exc_info.value).lower()


def test_unconfigured_listing_is_distinguishable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus_ai_agent.storage.providers.rclone.shutil.which", lambda _: None)
    with pytest.raises(Exception, match="not configured"):
        asyncio.run(RcloneProvider(remote="gdrive:nexus").list_files())
