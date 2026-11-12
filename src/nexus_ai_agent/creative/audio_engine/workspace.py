"""A contained runtime workspace: immutable inputs, staging, atomic publish.

The repository's filesystem discipline already exists twice — the descriptor
relative :class:`nexus_ai_agent.tools.filesystem_policy.WorkspaceFilesystem`
(used by :class:`nexus_ai_agent.execution.staging.AttemptStaging` for queue
attempts) and the ``.<name>.part`` + :func:`Path.replace` convention in
:mod:`nexus_ai_agent.creative.slideshow.ffmpeg`.  This module is the third
*use*, not a third rule: it states the four properties the audio engine needs
and reuses the same primitives to get them.

Four properties, each tested in ``tests/unit/test_omega_workspace.py``:

1. **The source is immutable.**  An input file is never written, renamed or
   re-encoded in place.  The engine copies an input into the workspace under its
   content hash before FFmpeg ever opens it, and re-checks the copy's hash; the
   caller's bytes are re-hashed *after* the run by the verifier.
2. **Staging is isolated per run.**  Every run gets
   ``<staging>/<run_id>/`` and writes only there.  A stale or partial file can
   never be mistaken for a published artifact, and a failed run removes its own
   tree.
3. **Publish is atomic and never clobbers.**  ``os.link`` into the destination
   directory, then ``os.rename`` onto the final name — both on one filesystem,
   so a reader either sees the previous file or the whole new one, never a
   partial.  An existing destination is refused unless the caller asked for
   replacement, and the *input* path is refused as a destination.
4. **Nothing escapes the root.**  Every path is resolved and proven to be a
   descendant of the workspace root; symlinked leaves are rejected.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "WorkspaceBoundaryError",
    "AtomicPublishError",
    "PublishResult",
    "RuntimeWorkspace",
    "hash_file",
    "safe_file_name",
]

_CHUNK = 1024 * 1024


class WorkspaceBoundaryError(ValueError):
    """A path cannot be proven to stay inside the workspace (fail-closed)."""


class AtomicPublishError(RuntimeError):
    """An artifact could not be published atomically (nothing was published)."""


def hash_file(path: Path | str, *, chunk_size: int = _CHUNK) -> str:
    """``sha256:<hex>`` of a file's bytes, streamed (bounded memory)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def safe_file_name(name: str | None, *, suffix: str, default: str) -> str:
    """A single safe file name: no separators, no traversal, expected suffix."""
    candidate = (name or "").strip()
    if not candidate:
        candidate = default
    if "/" in candidate or "\\" in candidate or candidate in (".", "..") or "\x00" in candidate:
        raise WorkspaceBoundaryError(f"unsafe file name: {candidate!r}")
    if suffix and not candidate.lower().endswith(suffix.lower()):
        candidate += suffix
    return candidate


@dataclass(frozen=True)
class PublishResult:
    """Where an artifact landed and what bytes are there (re-read from disk)."""

    path: Path
    sha256: str
    size_bytes: int
    replaced_existing: bool


class RuntimeWorkspace:
    """The engine's root: ``inputs/`` (immutable), ``staging/``, ``media/`` (final)."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.inputs_dir = self.root / "inputs"
        self.staging_dir = self.root / "staging"
        self.media_dir = self.root / "media"

    # -- construction ----------------------------------------------------- #
    def prepare(self) -> RuntimeWorkspace:
        for directory in (self.root, self.inputs_dir, self.staging_dir, self.media_dir):
            directory.mkdir(parents=True, exist_ok=True)
        for directory in (self.inputs_dir, self.staging_dir, self.media_dir):
            self.contain(directory)
        return self

    def contain(self, path: Path | str) -> Path:
        """Resolve ``path`` and prove it is inside the workspace root."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"path escapes the workspace root: {candidate}")
        if resolved != self.root and resolved.is_symlink():
            raise WorkspaceBoundaryError(f"symlinked workspace path refused: {candidate}")
        return resolved

    # -- inputs ----------------------------------------------------------- #
    def stage_input(self, source: Path | str, *, expected_sha256: str | None = None) -> Path:
        """Copy an external input into ``inputs/`` under its content hash.

        Idempotent: the same bytes are staged once and reused.  ``expected_sha256``
        (when the caller already measured the source) is enforced on the *copy*,
        so a file mutated between measurement and staging is caught here rather
        than silently used.
        """
        original = Path(source).expanduser()
        if not original.is_file():
            raise WorkspaceBoundaryError(f"input file not found: {original}")
        # The source is external by definition; it is only ever *read* here.
        # Containment applies to the staged copy, which is what FFmpeg opens.
        digest = hash_file(original)
        if expected_sha256 is not None and digest != expected_sha256:
            raise WorkspaceBoundaryError(
                f"input bytes changed before staging: expected {expected_sha256}, read {digest}"
            )
        self.inputs_dir.mkdir(parents=True, exist_ok=True)
        digest_hex = digest.split(":", 1)[1]
        target = self.contain(self.inputs_dir / f"{digest_hex}{original.suffix.lower()}")
        if target.is_file():
            if hash_file(target) == digest:
                return target
            raise WorkspaceBoundaryError(f"staged input for {digest} is corrupt: {target}")
        temporary = self.inputs_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copyfile(original, temporary)
            if hash_file(temporary) != digest:
                raise WorkspaceBoundaryError(
                    f"staged copy of {original} does not match its source hash"
                )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @contextmanager
    def run_directory(self, run_id: str | None = None) -> Iterator[Path]:
        """An isolated staging directory for exactly one run; removed on exit."""
        identifier = run_id or uuid.uuid4().hex
        if "/" in identifier or "\\" in identifier or identifier in ("", ".", ".."):
            raise WorkspaceBoundaryError(f"unsafe run id: {identifier!r}")
        directory = self.contain(self.staging_dir / identifier)
        directory.mkdir(parents=True, exist_ok=False)
        try:
            yield directory
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    # -- publish ---------------------------------------------------------- #
    def media_path(self, asset_id: str, *, suffix: str) -> Path:
        name = safe_file_name(asset_id, suffix=suffix, default="artifact")
        return self.contain(self.media_dir / name)

    def publish(
        self,
        staged: Path | str,
        destination: Path | str,
        *,
        replace: bool = False,
        protected: tuple[Path | str, ...] = (),
    ) -> PublishResult:
        """Atomically move ``staged`` onto ``destination`` (never a partial file).

        The staged source must live inside the workspace and must not be the
        destination; ``protected`` names files that must never be replaced (the
        engine passes every run input).
        """
        source = self.contain(staged)
        target = self.contain(destination)
        if not source.is_file():
            raise AtomicPublishError(f"staged artifact does not exist: {source}")
        if source == target:
            raise AtomicPublishError("staged artifact and destination are the same file")
        for guard in protected:
            guarded = self.contain(guard)
            if target == guarded or (
                target.exists() and guarded.exists() and os.path.samefile(target, guarded)
            ):
                raise AtomicPublishError(f"refusing to publish over a run input: {guarded.name}")
        if target.is_symlink():
            raise AtomicPublishError(f"destination is a symlink, refusing to write: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        replaced = target.exists()
        if replaced and not replace:
            raise AtomicPublishError(
                f"{target.name} already exists; pass replace=True to overwrite deliberately"
            )
        staging_link = target.parent / f".{target.name}.{uuid.uuid4().hex}.publish"
        # 1. link into the destination directory (same fs, atomic visibility),
        # 2. rename onto the final name (replaces only when asked).
        try:
            os.link(source, staging_link)
            if replaced:
                os.replace(staging_link, target)
            else:
                # link() fails when the target exists, so a concurrent creator
                # cannot be silently overwritten by the publish.
                os.link(staging_link, target)
                staging_link.unlink()
            with target.open("rb") as handle:
                os.fsync(handle.fileno())
        except OSError as error:
            raise AtomicPublishError(f"atomic publish failed for {target.name}: {error}") from error
        finally:
            staging_link.unlink(missing_ok=True)
            source.unlink(missing_ok=True)
        return PublishResult(
            path=target,
            sha256=hash_file(target),
            size_bytes=target.stat().st_size,
            replaced_existing=replaced,
        )
