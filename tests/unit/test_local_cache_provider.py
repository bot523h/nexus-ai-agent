from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nexus_ai_agent.storage.providers.base import StorageError
from nexus_ai_agent.storage.providers.local_cache import LocalCacheProvider


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


@pytest.fixture
def cache(tmp_path: Path) -> LocalCacheProvider:
    return LocalCacheProvider(tmp_path / "cache")


def test_upload_download_and_listing_keep_normal_keys_under_cache(
    cache: LocalCacheProvider, tmp_path: Path
) -> None:
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
        "\\\\server\\share\\secret.txt",
        "C:/Windows/system32/secret.dll",
        "C:\\Windows\\system32\\secret.dll",
        "C:relative.txt",
        "..\\outside.txt",
        "nested\\..\\outside.txt",
        "safe\x00name.txt",
        "safe\x1fname.txt",
        "safe\x7fname.txt",
        "%00secret.txt",
        "%2500secret.txt",
        "%2e%2e%2foutside.txt",
        "%252e%252e%252foutside.txt",
        "nested%2F..%2Foutside.txt",
        "%2Fetc%2Fpasswd",
        "%2e%2e%5coutside.txt",
        "%252e%252e%255coutside.txt",
        "CON",
        "con.txt",
        "nested/PRN.log",
        "AUX",
        "NUL.bin",
        "COM1.txt",
        "LPT9.dat",
        "file.txt:zone.identifier",
        "trailingdot./file.txt",
        "nested/trailingspace ",
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


def test_toctou_directory_component_substitution_after_path_for_key_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove descriptor-relative O_NOFOLLOW blocks TOCTOU after path_for_key()."""
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "victim.txt").write_text("untouched", encoding="utf-8")
    provider = LocalCacheProvider(root)

    source = tmp_path / "payload.txt"
    source.write_text("attacker-payload", encoding="utf-8")

    real_path_for_key = provider.path_for_key

    def race_injecting_path_for_key(remote_key: str) -> Path:
        result = real_path_for_key(remote_key)
        # Simulate an attacker replacing the intermediate directory with a
        # symlink to `outside` immediately after path_for_key() succeeds.
        subdir = root / "racedir"
        if subdir.exists() and not subdir.is_symlink():
            for child in subdir.iterdir():
                child.unlink()
            subdir.rmdir()
        if not subdir.exists():
            subdir.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(provider, "path_for_key", race_injecting_path_for_key)

    with pytest.raises(StorageError):
        _run(provider.upload(local_path=source, remote_key="racedir/victim.txt"))

    assert (outside / "victim.txt").read_text(encoding="utf-8") == "untouched"

    with pytest.raises(StorageError):
        _run(
            provider.download(
                remote_key="racedir/victim.txt",
                local_path=tmp_path / "stolen.txt",
            )
        )
    assert not (tmp_path / "stolen.txt").exists()


def test_toctou_leaf_symlink_substitution_after_parent_open_is_blocked(
    tmp_path: Path,
) -> None:
    """Prove O_NOFOLLOW on leaf open blocks symlink replacement in place."""
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("safe-content", encoding="utf-8")

    provider = LocalCacheProvider(root)
    (root / "sub").mkdir()
    (root / "sub" / "leaf.txt").symlink_to(victim)

    source = tmp_path / "attacker.txt"
    source.write_text("compromised", encoding="utf-8")

    with pytest.raises(StorageError):
        LocalCacheProvider._copy_into_cache(provider, source, "sub/leaf.txt")
    assert victim.read_text(encoding="utf-8") == "safe-content"

    with pytest.raises(StorageError):
        LocalCacheProvider._copy_from_cache(provider, "sub/leaf.txt", tmp_path / "out.txt")
    assert not (tmp_path / "out.txt").exists()


def test_replaced_cache_root_directory_is_rejected_by_inode_pin(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    provider = LocalCacheProvider(root)

    # Replace the root directory with a symlink to `outside`
    root.rmdir()
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(StorageError):
        provider._open_pinned_root_fd()
