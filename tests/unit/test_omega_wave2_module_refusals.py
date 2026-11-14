"""The refusal paths in the remaining engine modules, module by module.

The Wave-2.1 bar is per-module, not aggregate: a package can sit comfortably
above 95% while one file inside it is barely exercised, and the file that is
barely exercised is usually the one whose refusal path nobody has run.

Every test here drives a guard that exists to stop something bad, so a failure
means the engine would have accepted it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
import pytest

from nexus_ai_agent.creative.audio_engine.ed25519_sign import (
    Ed25519SigningError,
    derive_public_key,
    key_id_for,
    verify_ed25519,
)
from nexus_ai_agent.creative.audio_engine.stereo_hpss import _smoothness, hpss_masks
from nexus_ai_agent.creative.audio_engine.workspace import (
    AtomicPublishError,
    RuntimeWorkspace,
    WorkspaceBoundaryError,
    safe_file_name,
)

# --------------------------------------------------------------------------- #
# ed25519_sign — the three argument guards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", [b"", b"\x00" * 31, b"\x00" * 33, "not-bytes", None, 12345])
def test_a_seed_that_is_not_exactly_32_bytes_is_refused(seed: object) -> None:
    """The seed length is not advisory: a short seed is not a weaker key."""
    with pytest.raises(Ed25519SigningError, match="exactly 32 bytes"):
        derive_public_key(seed)  # type: ignore[arg-type]


def test_a_bytearray_seed_is_accepted_like_bytes() -> None:
    """A mutable buffer must derive the same key as its immutable twin."""
    seed = bytes(range(32))
    assert derive_public_key(bytearray(seed)) == derive_public_key(seed)


def test_verifying_a_non_bytes_message_is_refused() -> None:
    """A signature over "a string" is a signature over nothing defined."""
    public = derive_public_key(bytes(range(32)))
    with pytest.raises((Ed25519SigningError, TypeError, ValueError)):
        verify_ed25519(public_key=public, message="a string", signature=b"\x00" * 64)  # type: ignore[arg-type]


@pytest.mark.parametrize("length", [0, 16, 31, 33, 64])
def test_a_public_key_that_is_not_32_bytes_has_no_key_id(length: int) -> None:
    """A truncated key must not produce an id that looks trustworthy."""
    with pytest.raises(Ed25519SigningError, match="32 bytes"):
        key_id_for(b"\x01" * length)


def test_a_key_id_is_stable_and_namespaced() -> None:
    public = derive_public_key(bytes(range(32)))
    expected = "ed25519:" + hashlib.sha256(public).hexdigest()[:16]
    assert key_id_for(public) == expected
    assert key_id_for(public) == key_id_for(public)
    assert key_id_for(public, prefix="test") == "test:" + expected.split(":", 1)[1]


# --------------------------------------------------------------------------- #
# stereo_hpss — kernel validation and the single-row smoothness edge
# --------------------------------------------------------------------------- #


def test_a_one_row_matrix_has_zero_smoothness_not_a_crash() -> None:
    """``np.diff`` on a length-1 axis is empty; averaging it must not divide by 0."""
    single = np.zeros((4, 1), dtype=np.float64)
    smoothed = _smoothness(single, axis=1)
    # The smoothed axis collapses; the other axis is untouched.
    assert smoothed.shape == (4, 1)
    assert np.all(smoothed == 0.0)


def test_smoothness_is_the_mean_absolute_difference_along_the_axis() -> None:
    values = np.array([[0.0, 1.0, 3.0]], dtype=np.float64)
    # |1-0| and |3-1| -> [1, 2] -> mean 1.5
    assert _smoothness(values, axis=1)[0] == pytest.approx(1.5)


def test_a_one_dimensional_magnitude_is_refused() -> None:
    with pytest.raises(ValueError, match=r"\(frames, bins\)"):
        hpss_masks(np.zeros((64,), dtype=np.float64))


@pytest.mark.parametrize("shape", [(1, 64), (64, 1), (1, 1)])
def test_a_degenerate_magnitude_is_refused(shape: tuple[int, int]) -> None:
    """A single frame or a single bin has no time/frequency contrast to split."""
    with pytest.raises(ValueError, match="at least 2 frames and 2 bins"):
        hpss_masks(np.ones(shape, dtype=np.float64))


@pytest.mark.parametrize("kernel", [2, 4, 0, -1])
def test_an_even_or_negative_kernel_is_refused(kernel: int) -> None:
    """An even median averages two centres and breaks the pass symmetry."""
    with pytest.raises(ValueError):
        hpss_masks(
            np.ones((8, 8), dtype=np.float64),
            kernel_harmonic=kernel,
            kernel_percussive=3,
        )
    with pytest.raises(ValueError):
        hpss_masks(
            np.ones((8, 8), dtype=np.float64),
            kernel_harmonic=3,
            kernel_percussive=kernel,
        )


# --------------------------------------------------------------------------- #
# workspace.safe_file_name — user-supplied names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "..", ".", "", "   ", None, "a/b", "a\\b", "bad\x00name"],
)
def test_a_traversing_or_empty_name_falls_back_to_the_default(name: object) -> None:
    """An asset id is attacker-influenced; it may not choose a path or an extension."""
    result = safe_file_name(name, suffix=".flac", default="artifact")  # type: ignore[arg-type]
    # Whatever the input, the result is one component with the engine's suffix.
    assert "/" not in result and "\\" not in result and "\x00" not in result
    assert result.endswith(".flac")
    assert result not in ("", ".", "..")
    assert Path(result).name == result
    # A name that reduces to nothing at all must fall back, not return ".flac".
    if name is None or not str(name).strip() or str(name).strip() in (".", ".."):
        assert result.startswith("artifact")


def test_an_unknown_extension_is_replaced_not_trusted() -> None:
    """A caller cannot smuggle ``.sh`` through by naming it as the extension."""
    assert safe_file_name("track.exe", suffix=".flac", default="artifact") == "track.flac"
    assert safe_file_name("track", suffix=".flac", default="artifact") == "track.flac"


def test_a_safe_extension_is_preserved() -> None:
    assert safe_file_name("track.flac", suffix=".flac", default="artifact") == "track.flac"
    assert safe_file_name("track.WAV", suffix=".flac", default="artifact") == "track.WAV"


# --------------------------------------------------------------------------- #
# workspace.run_directory — unsafe ids
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("run_id", [".", "..", "a/b", "a\\b"])
def test_an_unsafe_run_id_is_refused(tmp_path: Path, run_id: str) -> None:
    """A run id becomes a directory name, so it cannot contain a separator."""
    root = tmp_path / "root"
    root.mkdir()
    workspace = RuntimeWorkspace(root)
    with pytest.raises(WorkspaceBoundaryError, match="unsafe run id"):
        with workspace.run_directory(run_id):
            pass


@pytest.mark.parametrize("run_id", [None, ""])
def test_a_missing_run_id_generates_one(tmp_path: Path, run_id: object) -> None:
    """``None`` and ``""`` both mean "generate", never "use the parent"."""
    root = tmp_path / "root"
    root.mkdir()
    workspace = RuntimeWorkspace(root)
    with workspace.run_directory(run_id) as staging:  # type: ignore[arg-type]
        assert staging.is_dir()
        assert staging.name
        assert staging.parent.name == "staging"


def test_a_generated_run_directory_is_isolated_and_removed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    workspace = RuntimeWorkspace(root)
    with workspace.run_directory() as staging:
        assert staging.is_dir()
        (staging / "scratch.txt").write_text("x", encoding="utf-8")
        seen = staging
    assert not seen.exists()


# --------------------------------------------------------------------------- #
# workspace.publish — every refusal
# --------------------------------------------------------------------------- #


@pytest.fixture()
def workspace(tmp_path: Path):
    root = tmp_path / "ws"
    (root / "media").mkdir(parents=True)
    (root / "staging").mkdir()
    return RuntimeWorkspace(root)


def test_publishing_a_file_that_was_never_staged_is_refused(workspace) -> None:
    with pytest.raises(AtomicPublishError, match="staged artifact does not exist"):
        workspace.publish(
            workspace.root / "staging" / "nope.flac", workspace.root / "media" / "a.flac"
        )


def test_publishing_onto_the_staged_file_itself_is_refused(workspace) -> None:
    with workspace.run_directory("run-a") as staging:
        staged = staging / "out.flac"
        staged.write_bytes(b"data")
        with pytest.raises(AtomicPublishError, match="same file"):
            workspace.publish(staged, staged)


def test_publishing_over_a_protected_input_is_refused(workspace) -> None:
    """A run input is immutable; publishing over it destroys the evidence."""
    with workspace.run_directory("run-b") as staging:
        staged = staging / "out.flac"
        staged.write_bytes(b"data")
        target = workspace.root / "media" / "keep.flac"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original")
        with pytest.raises(AtomicPublishError, match="refusing to publish over a run input"):
            workspace.publish(staged, target, protected=(target,))


def test_publishing_over_an_existing_file_needs_replace(workspace) -> None:
    with workspace.run_directory("run-c") as staging:
        staged = staging / "out.flac"
        staged.write_bytes(b"new")
        target = workspace.root / "media" / "exists.flac"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"old")
        with pytest.raises(AtomicPublishError, match="already exists"):
            workspace.publish(staged, target)
        result = workspace.publish(staged, target, replace=True)
        assert target.read_bytes() == b"new"
        assert result.sha256.startswith("sha256:")


def test_publishing_onto_a_symlink_is_refused(workspace, tmp_path: Path) -> None:
    """Writing through a symlink is the classic way to escape a workspace."""
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"untouched")
    with workspace.run_directory("run-d") as staging:
        staged = staging / "out.flac"
        staged.write_bytes(b"payload")
        target = workspace.root / "media" / "link.flac"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(outside)
        with pytest.raises(AtomicPublishError, match="symlink"):
            workspace.publish(staged, target, replace=True)
    assert outside.read_bytes() == b"untouched"


def test_a_successful_publish_reports_the_measured_digest(workspace) -> None:
    payload = b"canonical bytes"
    with workspace.run_directory("run-e") as staging:
        staged = staging / "out.flac"
        staged.write_bytes(payload)
        target = workspace.root / "media" / "fresh.flac"
        target.parent.mkdir(parents=True, exist_ok=True)
        result = workspace.publish(staged, target)
    assert result.sha256 == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert target.read_bytes() == payload
    assert result.size_bytes == len(payload)


# --------------------------------------------------------------------------- #
# workspace — the error translations and the input-growth guard
# --------------------------------------------------------------------------- #

#: A run id that is safe by construction, used to force specific failure modes.
_SAFE_RUN = "run-fixed"


def test_contain_resolves_a_relative_path_against_the_root(workspace) -> None:
    """A relative path is inside the workspace by definition of "relative"."""
    target = workspace.root / "media" / "kept.flac"
    target.write_bytes(b"x")
    resolved = workspace.contain("media/kept.flac")
    assert resolved == target


def test_contain_translates_a_boundary_refusal_into_the_engine_error(workspace) -> None:
    """Callers catch ``WorkspaceBoundaryError``; the authority's type must not leak."""
    with pytest.raises(WorkspaceBoundaryError):
        workspace.contain(workspace.root / ".." / "outside" / "x.flac")


class _FailingFilesystem:
    """A real filesystem authority with one operation replaced by a raise.

    Everything else is delegated, so the call reaches the branch under test
    instead of dying earlier on an unrelated missing method.
    """

    def __init__(self, delegate: object, failing: str, error: BaseException) -> None:
        self._delegate = delegate
        self._failing = failing
        self._error = error

    def __getattr__(self, name: str):
        if name == self._failing:

            def _raise(*args: object, **kwargs: object):
                raise self._error

            return _raise
        return getattr(self._delegate, name)


def test_an_input_that_grows_while_being_staged_is_refused(workspace) -> None:
    """The size cap is the authority's guard against a fifo or a growing file.

    ``ContentLimitExceeded`` subclasses ``FilesystemBoundaryError``, so the
    generic handler catches it first. The engine's guarantee is therefore that
    the input is *refused* as a ``WorkspaceBoundaryError``; the more specific
    "grew while being staged" wording is unreachable while that inheritance
    holds, which is why the assertion here is on the refusal, not the wording.
    """
    from nexus_ai_agent.tools.filesystem_policy import ContentLimitExceeded

    source = workspace.root.parent / "growing.flac"
    source.write_bytes(b"stable bytes")
    workspace._fs = _FailingFilesystem(  # type: ignore[assignment]
        workspace._fs, "write_stream", ContentLimitExceeded("too much")
    )
    with pytest.raises(WorkspaceBoundaryError, match="too much"):
        workspace.stage_input(source)


def test_a_staging_disk_refusal_is_translated(workspace) -> None:
    from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError

    source = workspace.root.parent / "disk.flac"
    source.write_bytes(b"bytes")
    workspace._fs = _FailingFilesystem(  # type: ignore[assignment]
        workspace._fs, "write_stream", FilesystemBoundaryError("no space")
    )
    with pytest.raises(WorkspaceBoundaryError, match="no space"):
        workspace.stage_input(source)


def test_a_run_directory_that_cannot_be_created_is_translated(workspace) -> None:
    from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError

    workspace._fs = _FailingFilesystem(  # type: ignore[assignment]
        workspace._fs, "ensure_directory", FilesystemBoundaryError("read-only")
    )
    with pytest.raises(WorkspaceBoundaryError, match="read-only"):
        with workspace.run_directory(_SAFE_RUN):
            pass


def test_publishing_over_a_protected_inode_by_hard_link_is_refused(workspace) -> None:
    """A second name for a run input is still that run input.

    The path comparison above catches the obvious case; this catches the one it
    cannot, where the destination is a *different path* pointing at the same
    inode.
    """
    run_input = workspace.root / "inputs" / "content.flac"
    run_input.parent.mkdir(parents=True, exist_ok=True)
    run_input.write_bytes(b"original")
    alias = workspace.root / "media" / "alias.flac"
    alias.parent.mkdir(parents=True, exist_ok=True)
    os.link(run_input, alias)
    with workspace.run_directory(_SAFE_RUN) as staging:
        staged = staging / "out.flac"
        staged.write_bytes(b"new")
        with pytest.raises(AtomicPublishError, match="run input"):
            workspace.publish(staged, alias, replace=True, protected=(run_input,))
    assert run_input.read_bytes() == b"original"


def test_a_filesystem_boundary_error_during_rename_is_translated(workspace) -> None:
    from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError

    with workspace.run_directory(_SAFE_RUN) as staging:
        staged = staging / "out.flac"
        staged.write_bytes(b"payload")
        target = workspace.root / "media" / "fresh2.flac"
        workspace._fs = _FailingFilesystem(  # type: ignore[assignment]
            workspace._fs, "rename_into", FilesystemBoundaryError("cross-device")
        )
        with pytest.raises(AtomicPublishError, match="cross-device"):
            workspace.publish(staged, target)


def test_an_os_error_during_rename_is_translated(workspace) -> None:
    with workspace.run_directory(_SAFE_RUN) as staging:
        staged = staging / "out.flac"
        staged.write_bytes(b"payload")
        target = workspace.root / "media" / "fresh3.flac"
        workspace._fs = _FailingFilesystem(  # type: ignore[assignment]
            workspace._fs, "rename_into", OSError("EXDEV")
        )
        with pytest.raises(AtomicPublishError, match="EXDEV"):
            workspace.publish(staged, target)
