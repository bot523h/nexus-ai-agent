"""Attempt-scoped staging isolation tests (NEXUS V1).

The database and the filesystem are not one transaction, so the filesystem
half of publication must be provably contained.  Each test names the property
it proves; the security cases map to the mission's staging requirements:

    * cross-attempt isolation
    * path-traversal protection
    * symlink handling
    * no provider-controlled final path
    * no direct write to the authoritative namespace
    * cleanup / quarantine

Attempt ids are the real ``attempt_<hex>`` form minted by
``jobs.creative_passport.attempt_id``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nexus_ai_agent.execution.staging import AttemptStaging, StagingBoundaryError

ATTEMPT_1 = "attempt_1a2b3c"
ATTEMPT_2 = "attempt_9f8e7d"


def _staging(
    tmp_path: Path, *, attempt: str = ATTEMPT_1, final: Path | None = None
) -> AttemptStaging:
    return AttemptStaging(
        tmp_path / "staging_root",
        job_id="job-1",
        attempt_id=attempt,
        final_root=final,
    )


# --------------------------------------------------------------------------- #
# Layout + basic write
# --------------------------------------------------------------------------- #
def test_staging_layout_is_attempt_scoped(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    directory = staging.prepare()
    assert directory == tmp_path / "staging_root" / "job-1" / ATTEMPT_1 / "staging"
    assert directory.is_dir()


def test_write_then_read_back(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    path = staging.write("artifact.bin", b"hello")
    assert path.read_bytes() == b"hello"
    assert path.parent == staging.prepare()


# --------------------------------------------------------------------------- #
# Cross-attempt isolation
# --------------------------------------------------------------------------- #
def test_two_attempts_never_share_a_staging_tree(tmp_path: Path) -> None:
    first = _staging(tmp_path, attempt=ATTEMPT_1)
    second = _staging(tmp_path, attempt=ATTEMPT_2)
    first.write("a.bin", b"attempt-1")
    second.write("a.bin", b"attempt-2")
    assert first.stage_path("a.bin").read_bytes() == b"attempt-1"
    assert second.stage_path("a.bin").read_bytes() == b"attempt-2"
    assert first.prepare() != second.prepare()
    assert not first.prepare().is_relative_to(second.prepare())


def test_one_attempt_cannot_address_another_attempts_staging(tmp_path: Path) -> None:
    first = _staging(tmp_path, attempt=ATTEMPT_1)
    first.prepare()
    # A traversal into the sibling attempt is refused before any I/O.
    with pytest.raises(StagingBoundaryError):
        first.stage_path(f"../{ATTEMPT_2}/staging/a.bin")


# --------------------------------------------------------------------------- #
# Path-traversal + absolute-path protection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["../evil", "..", "a/../../b", "/etc/passwd", "a/b", "a\\b", ""])
def test_stage_path_rejects_unsafe_names(tmp_path: Path, bad: str) -> None:
    staging = _staging(tmp_path)
    with pytest.raises(StagingBoundaryError):
        staging.stage_path(bad)


@pytest.mark.parametrize("bad", ["../evil", "/abs", "a/../../b"])
def test_write_rejects_unsafe_names(tmp_path: Path, bad: str) -> None:
    staging = _staging(tmp_path)
    with pytest.raises(StagingBoundaryError):
        staging.write(bad, b"x")


def test_constructor_rejects_unsafe_job_or_attempt_ids(tmp_path: Path) -> None:
    with pytest.raises(StagingBoundaryError):
        AttemptStaging(tmp_path, job_id="../escape", attempt_id="a")
    with pytest.raises(StagingBoundaryError):
        AttemptStaging(tmp_path, job_id="j", attempt_id="a/b")


# --------------------------------------------------------------------------- #
# Symlink handling
# --------------------------------------------------------------------------- #
def test_stage_path_rejects_a_symlinked_leaf(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    staging.prepare()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    os.symlink(outside, staging.prepare() / "link.bin")
    with pytest.raises(StagingBoundaryError):
        staging.stage_path("link.bin")


def test_prepare_rejects_symlinked_attempt_components(tmp_path: Path) -> None:
    root = tmp_path / "staging_root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "job-1")
    with pytest.raises(StagingBoundaryError):
        _staging(tmp_path).prepare()


def test_write_never_follows_a_swapped_symlink(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    staging.prepare()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    os.symlink(outside, staging.prepare() / "swap.bin")
    with pytest.raises(StagingBoundaryError):
        staging.write("swap.bin", b"overwrite")
    assert outside.read_bytes() == b"secret", "a symlink swap must not redirect the write"


# --------------------------------------------------------------------------- #
# No provider-controlled final path / no direct authoritative write
# --------------------------------------------------------------------------- #
def test_publish_requires_a_declared_final_root(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    staged = staging.write("a.bin", b"x")
    with pytest.raises(StagingBoundaryError):
        staging.publish(staged, "final/a.bin")


def test_publish_moves_into_the_declared_final_root(tmp_path: Path) -> None:
    final = tmp_path / "final_root"
    staging = _staging(tmp_path, final=final)
    staged = staging.write("a.bin", b"payload")
    published = staging.publish(staged, "nested/a.bin")
    assert published == final / "nested" / "a.bin"
    assert published.read_bytes() == b"payload"
    assert not staged.exists(), "publish is a move, not a copy"


def test_publish_rejects_a_target_outside_the_final_root(tmp_path: Path) -> None:
    final = tmp_path / "final_root"
    staging = _staging(tmp_path, final=final)
    staged = staging.write("a.bin", b"x")
    with pytest.raises(StagingBoundaryError):
        staging.publish(staged, "../escape/a.bin")


def test_publish_rejects_a_target_inside_the_staging_root(tmp_path: Path) -> None:
    staging = _staging(tmp_path, final=tmp_path / "staging_root")
    staged = staging.write("a.bin", b"x")
    with pytest.raises(StagingBoundaryError):
        staging.publish(staged, f"job-1/{ATTEMPT_1}/staging/again.bin")


def test_publish_rejects_a_source_outside_this_attempts_staging(tmp_path: Path) -> None:
    final = tmp_path / "final_root"
    staging = _staging(tmp_path, final=final)
    stray = tmp_path / "stray.bin"
    stray.write_bytes(b"x")
    with pytest.raises(StagingBoundaryError):
        staging.publish(stray, "a.bin")


def test_publish_rejects_a_symlinked_source(tmp_path: Path) -> None:
    final = tmp_path / "final_root"
    staging = _staging(tmp_path, final=final)
    staging.prepare()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    os.symlink(outside, staging.prepare() / "link.bin")
    with pytest.raises(StagingBoundaryError):
        staging.publish(staging.prepare() / "link.bin", "a.bin")


def test_publish_rejects_a_directory_source(tmp_path: Path) -> None:
    final = tmp_path / "final_root"
    staging = _staging(tmp_path, final=final)
    staging.prepare()
    (staging.prepare() / "directory").mkdir()
    with pytest.raises(StagingBoundaryError):
        staging.publish(staging.prepare() / "directory", "a.bin")


# --------------------------------------------------------------------------- #
# Cleanup / quarantine
# --------------------------------------------------------------------------- #
def test_cleanup_removes_the_attempt_tree(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    staging.write("a.bin", b"x")
    staging.cleanup()
    assert not (tmp_path / "staging_root" / "job-1" / ATTEMPT_1).exists()


def test_quarantine_moves_the_attempt_tree_aside(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    staging.write("a.bin", b"x")
    destination = staging.quarantine("verification_failed")
    expected = (
        tmp_path / "staging_root" / "_quarantine" / "job-1" / f"{ATTEMPT_1}.verification_failed"
    )
    assert destination == expected
    assert (destination / "staging" / "a.bin").read_bytes() == b"x"
    assert not (tmp_path / "staging_root" / "job-1" / ATTEMPT_1).exists()


def test_quarantine_rejects_an_unsafe_reason(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    staging.prepare()
    with pytest.raises(StagingBoundaryError):
        staging.quarantine("../escape")


def test_quarantine_rejects_a_symlinked_destination_component(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    staging.write("a.bin", b"x")
    quarantine = tmp_path / "staging_root" / "_quarantine"
    quarantine.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, quarantine / "job-1")
    with pytest.raises(StagingBoundaryError):
        staging.quarantine("verification_failed")
