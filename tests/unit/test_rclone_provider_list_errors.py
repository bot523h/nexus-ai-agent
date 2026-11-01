"""REL-006 regression: an rclone provider failure must never look like an empty listing.

``RcloneProvider.list_files`` is the source the housekeeping/rotation code prunes
backups from, so a silent ``[]`` on a provider error would read as "nothing to
keep".  These tests drive the real ``list_files`` code path with a fake rclone
process (the binary is not installed offline) and assert the failure semantics.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus_ai_agent.storage.providers.base import ProviderUnavailable, StorageError
from nexus_ai_agent.storage.providers.rclone import RcloneProvider


class _FakeProc:
    def __init__(self, *, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _install_fake_rclone(monkeypatch, proc: _FakeProc) -> None:
    async def _fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    # is_configured() checks for the binary in PATH; pretend it is present so the
    # failure paths below are reachable without rclone installed.
    monkeypatch.setattr(
        "nexus_ai_agent.storage.providers.rclone.shutil.which", lambda _n: "/usr/bin/rclone"
    )


def test_nonzero_exit_raises_storage_error_not_empty_list(monkeypatch):
    _install_fake_rclone(
        monkeypatch, _FakeProc(returncode=3, stderr=b"Failed to lsf: directory not found")
    )
    provider = RcloneProvider(remote="gdrive:nexus")
    with pytest.raises(StorageError) as exc:
        asyncio.run(provider.list_files(prefix="backups/"))
    assert "exit 3" in str(exc.value)


def test_unconfigured_provider_raises_provider_unavailable(monkeypatch):
    monkeypatch.setattr("nexus_ai_agent.storage.providers.rclone.shutil.which", lambda _n: None)
    provider = RcloneProvider(remote="gdrive:nexus")
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.list_files(prefix="backups/"))


def test_empty_remote_configured_returns_empty_list(monkeypatch):
    _install_fake_rclone(monkeypatch, _FakeProc(returncode=0, stdout=b""))
    provider = RcloneProvider(remote="gdrive:nexus")
    assert asyncio.run(provider.list_files(prefix="backups/")) == []


def test_successful_listing_is_prefixed(monkeypatch):
    _install_fake_rclone(monkeypatch, _FakeProc(returncode=0, stdout=b"a.db\nb.db\n"))
    provider = RcloneProvider(remote="gdrive:nexus")
    assert asyncio.run(provider.list_files(prefix="backups")) == [
        "backups/a.db",
        "backups/b.db",
    ]


def test_failure_diagnostics_are_bounded_and_redacted(monkeypatch):
    secret = b"http://user:sup3rsecret@webdav.example/path " + b"x" * 500
    _install_fake_rclone(monkeypatch, _FakeProc(returncode=1, stderr=secret))
    provider = RcloneProvider(remote="webdav:nexus")
    with pytest.raises(StorageError) as exc:
        asyncio.run(provider.list_files(prefix=""))
    message = str(exc.value)
    assert "sup3rsecret" not in message
    assert "<redacted>@" in message
    # 200-char diagnostic cap plus the short prefix.
    assert len(message) < 260
