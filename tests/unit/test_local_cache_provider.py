from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus_ai_agent.storage.providers.base import StorageError
from nexus_ai_agent.storage.providers.local_cache import LocalCacheProvider


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def cache(tmp_path: Path) -> LocalCacheProvider:
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


def test_symlinked_key_components_cannot_escape_cache(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
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


def test_leaf_symlinks_are_rejected_even_when_the_target_is_inside_the_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    (root / "real.txt").write_text("original", encoding="utf-8")
    try:
        (root / "alias.txt").symlink_to(root / "real.txt")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    provider = LocalCacheProvider(root)
    with pytest.raises(StorageError):
        provider.path_for_key("alias.txt")
    assert _run(provider.list_files()) == ["real.txt"]


def test_cache_root_symlink_is_pinned_to_its_canonical_directory(tmp_path: Path) -> None:
    target = tmp_path / "real-cache"
    target.mkdir()
    link = tmp_path / "cache-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    provider = LocalCacheProvider(link)
    assert provider.cache_dir == target.resolve()
    assert provider.path_for_key("file.txt") == target / "file.txt"
