from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from nexus_ai_agent.storage.providers import local_cache as local_cache_module
from nexus_ai_agent.storage.providers.base import StorageError
from nexus_ai_agent.storage.providers.local_cache import LocalCacheProvider


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def cache(tmp_path: Path) -> LocalCacheProvider:
    if not local_cache_module._supports_descriptor_containment():
        pytest.skip("LocalCache filesystem tests require POSIX descriptor-relative operations")
    return LocalCacheProvider(tmp_path / "cache")


def test_upload_download_and_listing_keep_normal_keys_under_cache(cache, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"cache payload")

    _run(cache.upload(local_path=source, remote_key="projects/demo/result.bin"))

    cached = cache.path_for_key("projects/demo/result.bin")
    assert cached == cache.cache_dir / "projects" / "demo" / "result.bin"
    assert cached.read_bytes() == b"cache payload"

    destination = tmp_path / "downloads" / "result.bin"
    _run(cache.download(remote_key="projects/demo/result.bin", local_path=destination))
    assert destination.read_bytes() == b"cache payload"
    assert _run(cache.list_files()) == ["projects/demo/result.bin"]
    assert _run(cache.list_files(prefix="projects/demo/")) == ["projects/demo/result.bin"]


@pytest.mark.parametrize(
    "key",
    [
        "",
        ".",
        "./secret.txt",
        "../outside.txt",
        "nested/../../outside.txt",
        "nested/../secret.txt",
        "/etc/passwd",
        "//server/share/secret.txt",
        "C:/Windows/system32/secret.dll",
        "C:relative.txt",
        "..\\outside.txt",
        "nested\\..\\outside.txt",
        "safe\x00name.txt",
        "%00secret.txt",
        "%2e%2e%2foutside.txt",
        "%252e%252e%252foutside.txt",
        "nested%2F..%2Foutside.txt",
        "%2Fetc%2Fpasswd",
        "%2e%2e%5coutside.txt",
    ],
)
def test_path_for_key_rejects_traversal_absolute_nul_and_encoded_keys(
    cache: LocalCacheProvider,
    key: str,
) -> None:
    with pytest.raises(StorageError):
        cache.path_for_key(key)


def test_invalid_upload_and_download_keys_fail_before_external_paths_are_touched(
    cache: LocalCacheProvider,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"do not escape")
    outside = tmp_path / "outside.bin"

    with pytest.raises(StorageError):
        _run(cache.upload(local_path=source, remote_key="../outside.bin"))
    with pytest.raises(StorageError):
        _run(cache.download(remote_key="%252e%252e%252foutside.bin", local_path=outside))

    assert not outside.exists()
    assert not (cache.cache_dir.parent / "outside.bin").exists()


@pytest.mark.parametrize("prefix", ["../", "/etc", "C:/", "%2e%2e%2f", "bad\x00prefix"])
def test_list_files_rejects_unsafe_prefixes(cache: LocalCacheProvider, prefix: str) -> None:
    with pytest.raises(StorageError):
        _run(cache.list_files(prefix=prefix))


@pytest.mark.skipif(
    not local_cache_module._supports_descriptor_containment(),
    reason="POSIX descriptor-relative filesystem operations are unavailable",
)
def test_symlinked_key_components_cannot_escape_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platforms without symlink support
        pytest.skip(f"symlinks unavailable: {exc}")

    provider = LocalCacheProvider(root)
    source = tmp_path / "source.txt"
    source.write_text("overwrite attempt", encoding="utf-8")

    with pytest.raises(StorageError):
        provider.path_for_key("linked/secret.txt")
    with pytest.raises(StorageError):
        _run(provider.upload(local_path=source, remote_key="linked/new.txt"))
    with pytest.raises(StorageError):
        _run(provider.download(remote_key="linked/secret.txt", local_path=tmp_path / "copy.txt"))
    with pytest.raises(StorageError):
        _run(provider.list_files(prefix="linked"))

    assert (outside / "secret.txt").read_text(encoding="utf-8") == "outside"
    assert not (outside / "new.txt").exists()


@pytest.mark.skipif(
    not local_cache_module._supports_descriptor_containment(),
    reason="POSIX descriptor-relative filesystem operations are unavailable",
)
def test_leaf_symlinks_are_rejected_even_when_the_target_is_inside_the_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    (root / "real.txt").write_text("original", encoding="utf-8")
    try:
        (root / "alias.txt").symlink_to(root / "real.txt")
    except OSError as exc:  # pragma: no cover - platforms without symlink support
        pytest.skip(f"symlinks unavailable: {exc}")

    provider = LocalCacheProvider(root)
    with pytest.raises(StorageError):
        provider.path_for_key("alias.txt")
    assert _run(provider.list_files()) == ["real.txt"]


@pytest.mark.skipif(
    not local_cache_module._supports_descriptor_containment(),
    reason="POSIX descriptor-relative filesystem operations are unavailable",
)
def test_cache_root_symlink_is_pinned_to_its_canonical_directory(tmp_path: Path) -> None:
    target = tmp_path / "real-cache"
    target.mkdir()
    link = tmp_path / "cache-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platforms without symlink support
        pytest.skip(f"symlinks unavailable: {exc}")

    provider = LocalCacheProvider(link)
    assert provider.cache_dir == target.resolve()
    assert provider.path_for_key("file.txt") == target / "file.txt"


def test_download_reads_the_opened_inode_if_a_key_is_replaced_with_a_symlink(
    cache: LocalCacheProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = cache.cache_dir / "payload.txt"
    entry.write_text("trusted cache bytes", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    probe_link = tmp_path / "symlink-probe"
    try:
        probe_link.symlink_to(outside)
        probe_link.unlink()
    except OSError as exc:  # pragma: no cover - platforms without symlink support
        pytest.skip(f"symlinks unavailable: {exc}")

    real_copyfileobj = local_cache_module.shutil.copyfileobj
    replacements: list[bool] = []

    def replace_key_then_copy(source, destination, *args, **kwargs) -> None:
        entry.unlink()
        entry.symlink_to(outside)
        replacements.append(True)
        real_copyfileobj(source, destination, *args, **kwargs)

    monkeypatch.setattr(local_cache_module.shutil, "copyfileobj", replace_key_then_copy)
    output = tmp_path / "downloaded.txt"
    _run(cache.download(remote_key="payload.txt", local_path=output))

    assert replacements == [True]
    assert output.read_text(encoding="utf-8") == "trusted cache bytes"
    assert outside.read_text(encoding="utf-8") == "outside secret"
    assert entry.is_symlink()


def test_upload_atomically_replaces_a_key_symlink_without_writing_its_target(
    cache: LocalCacheProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside-target.txt"
    outside.write_text("keep these bytes", encoding="utf-8")
    source = tmp_path / "upload.txt"
    source.write_text("uploaded cache bytes", encoding="utf-8")
    entry = cache.cache_dir / "nested" / "payload.txt"
    probe_link = tmp_path / "symlink-probe"
    try:
        probe_link.symlink_to(outside)
        probe_link.unlink()
    except OSError as exc:  # pragma: no cover - platforms without symlink support
        pytest.skip(f"symlinks unavailable: {exc}")

    real_copyfileobj = local_cache_module.shutil.copyfileobj
    replacements: list[bool] = []

    def create_key_symlink_then_copy(source_file, destination_file, *args, **kwargs) -> None:
        entry.symlink_to(outside)
        replacements.append(True)
        real_copyfileobj(source_file, destination_file, *args, **kwargs)

    monkeypatch.setattr(local_cache_module.shutil, "copyfileobj", create_key_symlink_then_copy)
    _run(cache.upload(local_path=source, remote_key="nested/payload.txt"))

    assert replacements == [True]
    assert entry.read_text(encoding="utf-8") == "uploaded cache bytes"
    assert not entry.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep these bytes"


@pytest.mark.skipif(
    not local_cache_module._supports_descriptor_containment(),
    reason="POSIX descriptor-relative filesystem operations are unavailable",
)
def test_download_rejects_a_hard_linked_cache_entry(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside hard-link target", encoding="utf-8")
    try:
        (root / "linked.txt").hardlink_to(outside)
    except OSError as exc:  # pragma: no cover - platforms without hard-link support
        pytest.skip(f"hard links unavailable: {exc}")

    provider = LocalCacheProvider(root)
    with pytest.raises(StorageError, match="private regular file"):
        _run(provider.download(remote_key="linked.txt", local_path=tmp_path / "copy.txt"))
    assert not (tmp_path / "copy.txt").exists()
    assert outside.read_text(encoding="utf-8") == "outside hard-link target"


@pytest.mark.skipif(
    not local_cache_module._supports_descriptor_containment(),
    reason="POSIX descriptor-relative filesystem operations are unavailable",
)
def test_cache_root_writable_by_other_uids_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "shared-cache"
    root.mkdir()
    root.chmod(0o777)

    with pytest.raises(StorageError, match="group/world writable"):
        LocalCacheProvider(root)


def test_local_cache_fails_closed_without_descriptor_containment_primitives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_cache_module, "_supports_descriptor_containment", lambda: False)

    with pytest.raises(StorageError, match="POSIX descriptor-relative"):
        LocalCacheProvider(tmp_path / "unsupported-cache")


def test_cache_subdirectory_writable_by_other_uids_is_rejected(
    cache: LocalCacheProvider,
    tmp_path: Path,
) -> None:
    nested = cache.cache_dir / "nested"
    nested.mkdir()
    nested.chmod(0o777)
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")

    with pytest.raises(StorageError, match="unsafe ownership or permissions"):
        _run(cache.upload(local_path=source, remote_key="nested/payload.txt"))


@pytest.mark.skipif(
    not local_cache_module._supports_descriptor_containment(),
    reason="POSIX descriptor-relative filesystem operations are unavailable",
)
def test_download_rejects_nonblocking_fifo_entries(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    root = tmp_path / "cache"
    root.mkdir()
    os.mkfifo(root / "pipe")
    provider = LocalCacheProvider(root)

    with pytest.raises(StorageError, match="private regular file"):
        _run(provider.download(remote_key="pipe", local_path=tmp_path / "copy.txt"))

    assert not (tmp_path / "copy.txt").exists()
