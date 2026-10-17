"""Failure boundaries of the external storage providers (audit 2026-10-16).

``storage/providers/{rclone,mega,huggingface}.py`` are reachable from
``api/app.py`` and were flagged with the risk markers ``process-exec``,
``credentials`` and ``network`` while having no dedicated test.  What matters is
not that a happy path works against a real remote (it cannot run in CI) but that
the **boundaries** hold without one:

* an unconfigured provider refuses with a typed error instead of half-running;
* the rclone command line is built as an argv list — no shell, and a hostile
  remote key stays one argv element instead of being re-split;
* a remote key that a provider can only store as *one filename* cannot smuggle a
  path separator into that filename;
* a credential never appears in a URL, an argv or a raised message.

The rclone cases drive a stub executable on ``PATH`` that records its argv; the
Mega and HuggingFace cases inject a fake client instead of a network.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from nexus_ai_agent.storage.providers import huggingface, mega, rclone
from nexus_ai_agent.storage.providers.base import (
    ProviderUnavailable,
    StorageError,
    safe_key_to_filename,
)

SECRET = "hf_do_not_leak_me"


def _stub_rclone(directory: Path, *, exit_code: int, argv_file: Path) -> Path:
    """A fake ``rclone`` that records argv and exits with *exit_code*."""
    binary = directory / "rclone"
    binary.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        "echo 'stub stderr' >&2\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary


@pytest.fixture()
def stub_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    argv_file = tmp_path / "argv.txt"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ['PATH']}")
    return binaries, argv_file


# ── rclone ──────────────────────────────────────────────────────────────────


def test_rclone_reports_itself_unconfigured_without_the_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert rclone.RcloneProvider(remote="gdrive:nexus").is_configured() is False


def test_rclone_empty_remote_is_not_configured() -> None:
    assert rclone.RcloneProvider(remote="").is_configured() is False


async def test_rclone_refuses_to_upload_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    provider = rclone.RcloneProvider(remote="gdrive:nexus")
    with pytest.raises(ProviderUnavailable):
        await provider.upload(local_path=tmp_path / "a.bin", remote_key="a.bin")


async def test_rclone_upload_builds_an_argv_list_without_a_shell(
    stub_path: tuple[Path, Path], tmp_path: Path
) -> None:
    binaries, argv_file = stub_path
    _stub_rclone(binaries, exit_code=0, argv_file=argv_file)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"payload")

    provider = rclone.RcloneProvider(remote="gdrive:nexus/")
    assert provider.is_configured() is True

    await provider.upload(local_path=source, remote_key="clips/a b; rm -rf $HOME.mp4")

    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert argv == [
        "copyto",
        str(source),
        "gdrive:nexus/clips/a b; rm -rf $HOME.mp4",
        "--retries",
        "3",
        "--low-level-retries",
        "3",
    ], "the remote key must be one argv element — never re-split by a shell"


async def test_rclone_upload_failure_is_a_typed_storage_error(
    stub_path: tuple[Path, Path], tmp_path: Path
) -> None:
    binaries, argv_file = stub_path
    _stub_rclone(binaries, exit_code=9, argv_file=argv_file)
    provider = rclone.RcloneProvider(remote="gdrive:nexus")

    with pytest.raises(StorageError) as excinfo:
        await provider.upload(local_path=tmp_path / "a.bin", remote_key="a.bin")

    assert "rclone upload failed" in str(excinfo.value)
    assert "stub stderr" in str(excinfo.value)


async def test_rclone_download_failure_is_a_provider_unavailable(
    stub_path: tuple[Path, Path], tmp_path: Path
) -> None:
    binaries, argv_file = stub_path
    _stub_rclone(binaries, exit_code=1, argv_file=argv_file)
    provider = rclone.RcloneProvider(remote="gdrive:nexus")

    with pytest.raises(ProviderUnavailable):
        await provider.download(remote_key="missing.bin", local_path=tmp_path / "missing.bin")


async def test_rclone_list_files_is_empty_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert await rclone.RcloneProvider(remote="gdrive:nexus").list_files(prefix="x/") == []


# ── single-filename key mapping ─────────────────────────────────────────────


@pytest.mark.parametrize("key", ["../../etc/passwd", "a/../b", "dir/sub/file.bin", "./x"])
def test_a_hostile_remote_key_cannot_smuggle_a_separator(key: str) -> None:
    name = safe_key_to_filename(key)
    assert "/" not in name
    assert Path(name).name == name, "the mapped name must be a single path component"


# ── mega ────────────────────────────────────────────────────────────────────


def test_mega_needs_both_credentials_to_be_configured() -> None:
    assert mega.MegaProvider(email="", password="").is_configured() is False
    assert mega.MegaProvider(email="a@b.c", password="").is_configured() is False
    assert mega.MegaProvider(email="a@b.c", password="pw").is_configured() is True


def test_mega_reports_a_missing_client_package_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = mega.MegaProvider(email="a@b.c", password="pw")
    monkeypatch.setitem(sys.modules, "mega", None)  # ``from mega import Mega`` raises

    with pytest.raises(ProviderUnavailable) as excinfo:
        provider._get_client()

    assert "not installed" in str(excinfo.value)


class FakeMegaClient:
    """Records the calls the provider makes on the mega.py client."""

    def __init__(self, *, root: Any = "ROOT") -> None:
        self.root = root
        self.uploads: list[dict[str, Any]] = []
        self.created: list[str] = []

    def find(self, name: str, *_: Any) -> Any:
        return self.root if name else None

    def create_folder(self, name: str) -> str:
        self.created.append(name)
        return "NEW-ROOT"

    def upload(self, local: str, *, dest: Any, dest_filename: str) -> None:
        self.uploads.append({"local": local, "dest": dest, "name": dest_filename})


async def test_mega_upload_creates_the_root_once_and_flattens_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeMegaClient(root=None)
    provider = mega.MegaProvider(email="a@b.c", password="pw")
    monkeypatch.setattr(provider, "_get_client", lambda: client)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"x")

    await provider.upload(local_path=source, remote_key="clips/../../escape.mp4")

    assert client.created == ["NEXUS"]
    assert client.uploads == [
        {"local": str(source), "dest": "NEW-ROOT", "name": "clips__..__..__escape.mp4"}
    ]


async def test_mega_download_without_the_root_folder_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeMegaClient(root=None)
    provider = mega.MegaProvider(email="a@b.c", password="pw")
    monkeypatch.setattr(provider, "_get_client", lambda: client)

    with pytest.raises(ProviderUnavailable) as excinfo:
        await provider.download(remote_key="a.bin", local_path=tmp_path / "a.bin")

    assert "root folder not found" in str(excinfo.value)


async def test_mega_client_never_appears_in_raised_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = mega.MegaProvider(email="user@example.com", password="hunter2-secret")

    def failing_client() -> Any:
        raise ProviderUnavailable("MEGA credentials rejected")

    monkeypatch.setattr(provider, "_get_client", failing_client)

    with pytest.raises(ProviderUnavailable) as excinfo:
        await provider.upload(local_path=tmp_path / "a.bin", remote_key="a.bin")

    assert "hunter2-secret" not in str(excinfo.value)


# ── huggingface ─────────────────────────────────────────────────────────────


def test_huggingface_sends_no_authorization_header_without_a_token() -> None:
    assert "Authorization" not in huggingface.HuggingFaceProvider(token=None)._headers()
    headers = huggingface.HuggingFaceProvider(token="")._headers()
    assert "Authorization" not in headers


def test_huggingface_token_is_carried_only_in_the_header() -> None:
    headers = huggingface.HuggingFaceProvider(token=SECRET)._headers()
    assert headers == {"Authorization": f"Bearer {SECRET}"}


class FakeResponse:
    def __init__(self, *, status_code: int, content: bytes = b"gguf") -> None:
        self.status_code = status_code
        self.content = content


class FakeClient:
    """Records GET calls; returns a canned response."""

    requests: ClassVar[list[dict[str, Any]]] = []
    response: ClassVar[FakeResponse] = FakeResponse(status_code=200)

    def __init__(self, **_: Any) -> None:
        pass

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        FakeClient.requests.append({"url": url, **kwargs})
        return FakeClient.response


@pytest.fixture()
def fake_httpx(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.requests = []
    FakeClient.response = FakeResponse(status_code=200)
    monkeypatch.setattr(huggingface, "httpx", SimpleNamespace(AsyncClient=FakeClient), raising=True)
    return FakeClient


async def test_huggingface_download_url_and_headers(
    fake_httpx: type[FakeClient], tmp_path: Path
) -> None:
    provider = huggingface.HuggingFaceProvider(token=SECRET)
    destination = tmp_path / "model.gguf"

    await provider.download(
        remote_key="hf://TheBloke/Mistral-7B@main/model.gguf", local_path=destination
    )

    assert len(fake_httpx.requests) == 1
    request = fake_httpx.requests[0]
    assert request["url"] == "https://huggingface.co/TheBloke/Mistral-7B/resolve/main/model.gguf"
    assert request["headers"] == {"Authorization": f"Bearer {SECRET}"}
    assert SECRET not in request["url"], "the token must never be part of the URL"
    assert destination.read_bytes() == b"gguf"


async def test_the_documented_key_format_downloads_the_model_it_names(
    fake_httpx: type[FakeClient], tmp_path: Path
) -> None:
    """The module's own example must produce its own URL (defect P1-1)."""
    documented = (
        "hf://TheBloke/Mistral-7B-Instruct-v0.2-GGUF@main/mistral-7b-instruct-v0.2.Q4_K_M.gguf"
    )
    provider = huggingface.HuggingFaceProvider(token=None)

    await provider.download(remote_key=documented, local_path=tmp_path / "model.gguf")

    assert fake_httpx.requests[0]["url"] == (
        "https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/"
        "mistral-7b-instruct-v0.2.Q4_K_M.gguf"
    )


async def test_a_key_without_a_revision_uses_main(
    fake_httpx: type[FakeClient], tmp_path: Path
) -> None:
    provider = huggingface.HuggingFaceProvider(token=None)

    await provider.download(
        remote_key="hf://owner/model/sub/dir/model.gguf", local_path=tmp_path / "m.gguf"
    )

    assert fake_httpx.requests[0]["url"] == (
        "https://huggingface.co/owner/model/resolve/main/sub/dir/model.gguf"
    )


@pytest.mark.parametrize(
    "key",
    [
        "hf://owner/model@main/../../etc/passwd",
        "hf://owner/model@main//etc/passwd",
        "hf://owner@main/model.gguf",  # repo id without <owner>/<name>
        "hf://owner/model@/model.gguf",  # empty revision
        "hf://owner/model@main/",  # empty filename
        "hf://owner/model@main/file.gguf?download=1",
        "hf://owner/model@main/file with space.gguf",
        "hf://owner/model@../evil/file.gguf",
        "hf://owner/model@refs%2Fheads%2Fmain/file.gguf",  # percent-encoded revision
        "hf://owner/model@main/file.gguf#fragment",
        "hf://owner/model@main/sub/../../x.gguf",  # traversal in the sub-path
        "hf://owner@/model@main/file.gguf",  # two @, the first of which is empty
        "hf://owner//model@main/file.gguf",  # empty name segment
        "hf://owner/model",  # a repository, but no file
    ],
)
async def test_unsafe_keys_are_refused_before_any_request(
    fake_httpx: type[FakeClient], tmp_path: Path, key: str
) -> None:
    provider = huggingface.HuggingFaceProvider(token=None)

    with pytest.raises(ProviderUnavailable):
        await provider.download(remote_key=key, local_path=tmp_path / "x.gguf")

    assert fake_httpx.requests == [], "a rejected key must never reach the network"


async def test_huggingface_missing_model_and_server_error_are_distinguishable(
    fake_httpx: type[FakeClient], tmp_path: Path
) -> None:
    provider = huggingface.HuggingFaceProvider(token=None)
    destination = tmp_path / "model.gguf"
    key = "hf://owner/repo@main/model.gguf"

    fake_httpx.response = FakeResponse(status_code=404)
    with pytest.raises(ProviderUnavailable):
        await provider.download(remote_key=key, local_path=destination)
    assert not destination.exists(), "a 404 must not leave a file behind"

    fake_httpx.response = FakeResponse(status_code=500)
    with pytest.raises(StorageError) as excinfo:
        await provider.download(remote_key=key, local_path=destination)
    assert not isinstance(excinfo.value, ProviderUnavailable)
    assert not destination.exists()


async def test_a_filename_containing_an_at_sign_is_refused_as_ambiguous(
    fake_httpx: type[FakeClient], tmp_path: Path
) -> None:
    """``@`` separates the revision, so it cannot also appear inside the path.

    ``hf://o/r@main/model@2.gguf`` has two readings — revision ``main`` with the
    file ``model@2.gguf``, or revision ``main/model`` with the file ``2.gguf`` —
    and the convention has no escape for it.  The provider refuses instead of
    guessing, which is the fail-closed choice: a guess would silently download a
    *different* file than the operator asked for.
    """
    with pytest.raises(ProviderUnavailable):
        await huggingface.HuggingFaceProvider(token=None).download(
            remote_key="hf://o/r@main/model@2.gguf", local_path=tmp_path / "m.gguf"
        )
    assert fake_httpx.requests == []


async def test_the_flattened_legacy_form_is_refused_because_nothing_ever_wrote_it(
    fake_httpx: type[FakeClient], tmp_path: Path
) -> None:
    """``safe_key_to_filename`` (``a/b`` → ``a__b``) is never applied to HF keys.

    It is used by the single-filename providers only (``mega.py``,
    ``github_releases.py`` — verified by grep), so no stored HF key can be in
    that form.  Accepting it would give one model two names.
    """
    with pytest.raises(ProviderUnavailable):
        await huggingface.HuggingFaceProvider(token=None).download(
            remote_key="hf://TheBloke__Mistral-7B@main/model.gguf", local_path=tmp_path / "m.gguf"
        )
    assert fake_httpx.requests == []


async def test_a_commit_shaped_revision_is_accepted(
    fake_httpx: type[FakeClient], tmp_path: Path
) -> None:
    """A revision may be a branch, a tag or a commit sha — all are path segments."""
    provider = huggingface.HuggingFaceProvider(token=None)
    await provider.download(
        remote_key="hf://owner/model@935c7190eb1f/model.gguf", local_path=tmp_path / "m.gguf"
    )
    assert fake_httpx.requests[0]["url"] == (
        "https://huggingface.co/owner/model/resolve/935c7190eb1f/model.gguf"
    )


async def test_huggingface_is_read_only(fake_httpx: type[FakeClient], tmp_path: Path) -> None:
    with pytest.raises(ProviderUnavailable):
        await huggingface.HuggingFaceProvider(token=None).upload(
            local_path=tmp_path / "a.bin", remote_key="hf://owner/repo@main/a.bin"
        )
    assert fake_httpx.requests == [], "a refused upload must not talk to the network"


async def test_huggingface_malformed_key_is_refused_without_a_request(
    fake_httpx: type[FakeClient], tmp_path: Path
) -> None:
    with pytest.raises(ProviderUnavailable):
        await huggingface.HuggingFaceProvider(token=None).download(
            remote_key="not-an-hf-key", local_path=tmp_path / "x.bin"
        )
    assert fake_httpx.requests == []
