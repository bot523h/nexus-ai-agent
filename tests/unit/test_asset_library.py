"""Adversarial contract of the content-addressed Asset Library (Phase 4 §4).

Every property named in ``execution/asset_library.py`` has a test here, and
each hostile case asserts the *refusal* (typed error, nothing published), not
merely the happy path.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus_ai_agent.execution.asset_library import (
    AdmittedAsset,
    AssetBoundaryError,
    AssetIntegrityError,
    AssetLibrary,
    AssetLibraryError,
    AssetTooLargeError,
    sniff_mime,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def lib(tmp_path: Path) -> AssetLibrary:
    return AssetLibrary(tmp_path / "artifacts", max_bytes=1024 * 1024)


def _blobs(lib: AssetLibrary) -> list[Path]:
    base = lib.root / "cas" / "sha256"
    return sorted(p for p in base.rglob("*") if p.is_file()) if base.exists() else []


def _parts(lib: AssetLibrary) -> list[Path]:
    inc = lib.root / "cas" / "incoming"
    return sorted(inc.glob("*.part")) if inc.exists() else []


# ── content addressing, dedup, atomic finalize ───────────────────────────


def test_admit_is_content_addressed_and_read_only(lib: AssetLibrary, tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    src.write_bytes(PNG)
    asset = lib.admit(src, attempt_id="job1:1", declared_mime="image/png")
    assert asset == AdmittedAsset(
        sha256=_sha(PNG),
        size_bytes=len(PNG),
        storage_key=f"cas/sha256/{_sha(PNG)[7:9]}/{_sha(PNG)[9:11]}/{_sha(PNG)[7:]}",
        mime="image/png",
        declared_mime="image/png",
        mime_mismatch=False,
        deduplicated=False,
        attempt_id="job1:1",
    )
    blob = lib.root / asset.storage_key
    assert blob.read_bytes() == PNG
    assert not (blob.stat().st_mode & stat.S_IWUSR)  # published blobs are read-only
    assert _parts(lib) == []  # no staging residue
    assert lib.exists(asset.sha256) and lib.verify(asset.sha256) == len(PNG)


def test_same_bytes_from_different_attempts_dedupe_to_one_blob(
    lib: AssetLibrary, tmp_path: Path
) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"same content")
    b.write_bytes(b"same content")
    first = lib.admit(a, attempt_id="job1:1")
    second = lib.admit(b, attempt_id="job2:7")
    assert (first.deduplicated, second.deduplicated) == (False, True)
    assert first.storage_key == second.storage_key
    assert (first.attempt_id, second.attempt_id) == ("job1:1", "job2:7")  # attempts stay distinct
    assert len(_blobs(lib)) == 1


def test_idempotent_retry_of_the_same_attempt_is_safe(lib: AssetLibrary) -> None:
    one = lib.admit_bytes(b"retry me", attempt_id="job1:3")
    two = lib.admit_bytes(b"retry me", attempt_id="job1:3")
    assert one.storage_key == two.storage_key and two.deduplicated
    assert len(_blobs(lib)) == 1 and _parts(lib) == []


def test_crash_before_publish_leaves_no_blob_and_a_reapable_part(
    lib: AssetLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise OSError("simulated crash while linking")

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(AssetLibraryError, match="cannot publish"):
        lib.admit_bytes(b"never published", attempt_id="job9:1")
    assert _blobs(lib) == []
    assert not lib.exists(_sha(b"never published"))


def test_stale_part_files_are_reaped_only_after_min_age(lib: AssetLibrary) -> None:
    lib.admit_bytes(b"x", attempt_id="seed:1")  # creates the layout
    stale = lib.root / "cas" / "incoming" / "dead-0000.part"
    stale.write_bytes(b"partial")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = lib.root / "cas" / "incoming" / "live-0001.part"
    fresh.write_bytes(b"in flight")
    report = lib.fsck([_sha(b"x")], min_age_seconds=3600)
    assert report.stale_incoming == ("cas/incoming/dead-0000.part",)
    removed = lib.gc([_sha(b"x")], min_age_seconds=3600)
    assert removed == ("cas/incoming/dead-0000.part",)
    assert fresh.exists() and not stale.exists()


# ── hostile sources ──────────────────────────────────────────────────────


def test_symlink_source_is_refused_before_reading(lib: AssetLibrary, tmp_path: Path) -> None:
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"outside")
    link = tmp_path / "link.bin"
    link.symlink_to(secret)
    with pytest.raises(AssetBoundaryError, match="symlink"):
        lib.admit(link, attempt_id="job1:1")
    assert _blobs(lib) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_fifo_source_is_refused_and_does_not_block(lib: AssetLibrary, tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    done = threading.Event()
    outcome: list[BaseException | None] = []

    def attempt() -> None:
        try:
            lib.admit(fifo, attempt_id="job1:1")
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 - we assert on the type below
            outcome.append(exc)
        finally:
            done.set()

    threading.Thread(target=attempt, daemon=True).start()
    assert done.wait(5), "admit() blocked on a FIFO with no writer"
    assert isinstance(outcome[0], AssetBoundaryError)
    assert _blobs(lib) == []


def test_directory_and_missing_sources_are_typed_refusals(
    lib: AssetLibrary, tmp_path: Path
) -> None:
    with pytest.raises(AssetBoundaryError, match="not a regular file"):
        lib.admit(tmp_path, attempt_id="job1:1")
    with pytest.raises(AssetLibraryError, match="cannot open"):
        lib.admit(tmp_path / "nope.bin", attempt_id="job1:1")


def test_oversized_source_is_refused_without_publishing(lib: AssetLibrary, tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(b"\x00" * (1024 * 1024 + 1))
    with pytest.raises(AssetTooLargeError):
        lib.admit(big, attempt_id="job1:1")
    with pytest.raises(AssetTooLargeError):
        lib.admit_bytes(b"\x00" * (1024 * 1024 + 1), attempt_id="job1:1")
    assert _blobs(lib) == [] and _parts(lib) == []


@pytest.mark.parametrize("attempt", ["", "../x", "a/b", "a b", "x" * 200, "\x00"])
def test_unsafe_attempt_ids_are_refused(lib: AssetLibrary, attempt: str) -> None:
    with pytest.raises(AssetBoundaryError, match="attempt id"):
        lib.admit_bytes(b"data", attempt_id=attempt)


@pytest.mark.parametrize("address", ["", "sha256:zz", "abc", "sha256:" + "a" * 63, "../etc"])
def test_malformed_addresses_cannot_reach_the_filesystem(lib: AssetLibrary, address: str) -> None:
    with pytest.raises(AssetBoundaryError):
        lib.path_for(address)
    with pytest.raises(AssetBoundaryError):
        lib.verify(address)


# ── races ────────────────────────────────────────────────────────────────


def test_concurrent_admissions_of_identical_bytes_yield_one_blob_and_one_winner(
    lib: AssetLibrary,
) -> None:
    payload = os.urandom(300_000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda i: lib.admit_bytes(payload, attempt_id=f"job{i}:1"), range(16))
        )
    assert {r.storage_key for r in results} == {lib.key_for(_sha(payload))}
    assert sum(1 for r in results if not r.deduplicated) == 1
    assert len(_blobs(lib)) == 1 and _parts(lib) == []
    assert lib.verify(_sha(payload)) == len(payload)


def test_concurrent_distinct_contents_do_not_interfere(lib: AssetLibrary) -> None:
    payloads = [os.urandom(50_000) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda p: lib.admit_bytes(p, attempt_id="job:1"), payloads))
    assert len({r.storage_key for r in results}) == 12
    for payload, result in zip(payloads, results, strict=True):
        assert result.sha256 == _sha(payload)
        with lib.open_verified(result.sha256) as stream:
            assert stream.read() == payload


# ── integrity / consistency / retention ──────────────────────────────────


def test_corrupt_blob_is_detected_never_repaired_by_overwrite(lib: AssetLibrary) -> None:
    asset = lib.admit_bytes(b"original", attempt_id="job1:1")
    blob = lib.root / asset.storage_key
    os.chmod(blob, 0o644)
    blob.write_bytes(b"tampered")
    with pytest.raises(AssetIntegrityError, match="corrupt"):
        lib.verify(asset.sha256)
    with pytest.raises(AssetIntegrityError):
        lib.admit_bytes(
            b"original", attempt_id="job1:2"
        )  # re-admission refuses, does not overwrite
    assert blob.read_bytes() == b"tampered"  # evidence of the incident preserved
    report = lib.fsck([asset.storage_key])
    assert report.corrupt == (asset.storage_key,) and not report.consistent
    with pytest.raises(AssetIntegrityError):
        with lib.open_verified(asset.sha256):
            pass


def test_fsck_reports_missing_orphans_and_unexpected_entries(
    lib: AssetLibrary, tmp_path: Path
) -> None:
    kept = lib.admit_bytes(b"kept", attempt_id="job1:1")
    orphan = lib.admit_bytes(b"orphan", attempt_id="job2:1")
    ghost = _sha(b"never stored")
    stray = lib.root / "cas" / "sha256" / "zz" / "stray.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("not a blob")
    link = lib.root / "cas" / "sha256" / "zz" / "link"
    link.symlink_to(tmp_path)
    report = lib.fsck([kept.storage_key, ghost])
    assert report.checked == 2
    assert report.missing == (lib.key_for(ghost),)
    assert report.orphans == (orphan.storage_key,)
    assert set(report.unexpected) == {"cas/sha256/zz/stray.txt", "cas/sha256/zz/link"}
    assert not report.consistent
    clean = lib.fsck([kept.storage_key, orphan.storage_key])
    assert clean.missing == () and clean.orphans == () and clean.corrupt == ()


def test_gc_removes_only_old_unreferenced_blobs(lib: AssetLibrary) -> None:
    kept = lib.admit_bytes(b"kept", attempt_id="job1:1")
    old_orphan = lib.admit_bytes(b"old orphan", attempt_id="job2:1")
    new_orphan = lib.admit_bytes(b"new orphan", attempt_id="job3:1")
    stale = time.time() - 10_000
    for key in (kept.storage_key, old_orphan.storage_key):
        os.utime(lib.root / key, (stale, stale))
    removed = lib.gc([kept.sha256], min_age_seconds=3600)
    assert removed == (old_orphan.storage_key,)
    assert lib.exists(kept.sha256) and lib.exists(new_orphan.sha256)
    assert not lib.exists(old_orphan.sha256)


def test_gc_never_deletes_a_referenced_blob_even_when_corrupt(lib: AssetLibrary) -> None:
    asset = lib.admit_bytes(b"referenced", attempt_id="job1:1")
    blob = lib.root / asset.storage_key
    os.chmod(blob, 0o644)
    blob.write_bytes(b"corrupted")
    stale = time.time() - 10_000
    os.utime(blob, (stale, stale))
    assert lib.gc([asset.storage_key], min_age_seconds=0) == ()
    assert blob.exists()


# ── MIME ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("head", "mime"),
    [
        (PNG, "image/png"),
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (MP4, "video/mp4"),
        (b"\x00\x00\x00\x14ftypqt  ", "video/quicktime"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
        (b"%PDF-1.7", "application/pdf"),
        (b"ID3\x04", "audio/mpeg"),
        (b"1\n00:00:01,000 --> 00:00:02,000\nhello\n", "text/plain"),
        (b"\x00\x01\x02binary", "application/octet-stream"),
        (b"", "application/octet-stream"),
    ],
)
def test_sniff_mime_table(head: bytes, mime: str) -> None:
    assert sniff_mime(head) == mime


def test_declared_mime_is_never_trusted_over_the_bytes(lib: AssetLibrary) -> None:
    asset = lib.admit_bytes(MP4, attempt_id="job1:1", declared_mime="image/png")
    assert asset.mime == "video/mp4"
    assert asset.declared_mime == "image/png"
    assert asset.mime_mismatch is True
    honest = lib.admit_bytes(PNG, attempt_id="job1:2", declared_mime="image/png")
    assert honest.mime_mismatch is False
