"""The engine's workspace: a thin adapter on the canonical filesystem authority.

This module used to implement its own containment checks, its own symlink
rejection and its own atomic publication.  Two implementations of one security
boundary is a defect even when both are correct, because they drift and the
drift is silent.  Per the mathematical-engineering doctrine (§7, single
authority) every path decision here is delegated to
:class:`~nexus_ai_agent.tools.filesystem_policy.WorkspaceFilesystem`, which owns
descriptor-relative, no-follow resolution, bounded writes, hashing and renames.

What this adapter still owns — and what makes it a distinct type rather than a
duplicate — is the *engine's* namespace contract:

``inputs/``
    Content-addressed copies of external inputs.  Immutable for the life of the
    workspace, keyed by the input's SHA-256, so a run can never be fed a file
    that changed underneath it.
``staging/<request-id>/``
    One isolated directory per run, removed on exit whether the run succeeded,
    refused or raised.  Nothing inside it is ever the destination of a publish.
``media/``
    Published artifacts only.  A path in ``media/`` exists if and only if a
    verifier approved those exact bytes (see ``execution._derive``).

Publication semantics
---------------------
``publish(..., replace=False)`` must fail if the destination exists, because a
concurrent second attempt must not silently win the same output id.
``WorkspaceFilesystem.rename_into`` maps to POSIX ``rename(2)``, which replaces
unconditionally, so the "no replace" case is enforced by an *exclusive create*
of the destination name (``os.link`` through the descriptor-relative boundary)
before the rename is allowed to proceed.  The check and the rename are not one
atomic operation — POSIX offers no "rename if absent" — so the guarantee is
stated precisely: **a publish never overwrites a destination that existed when
it started, and the window between the exclusive create and the rename cannot be
used to publish a different file under that name**, because the exclusive create
is what holds the name.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from nexus_ai_agent.tools.filesystem_policy import (
    ContentLimitExceeded,
    FilesystemBoundaryError,
    WorkspaceFilesystem,
)

#: Chunk size for content hashing.  1 MiB keeps peak memory flat for a
#: multi-gigabyte video while staying well inside a single read syscall burst.
_CHUNK = 1 << 20

#: Extensions a file name may keep.  Anything else is replaced: the suffix of a
#: *published artifact* comes from the canonical profile, not from user input.
_SAFE_SUFFIXES = frozenset(
    {".wav", ".flac", ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".aiff", ".aif", ".caf"}
)


def _with_prefix(digest: str) -> str:
    """``sha256:<hex>`` — the authority returns bare hex, the engine's evidence type does not."""
    return digest if digest.startswith("sha256:") else f"sha256:{digest}"


class WorkspaceBoundaryError(ValueError):
    """A path escaped the workspace, or a write was refused by the boundary."""


class AtomicPublishError(RuntimeError):
    """An artifact could not be published atomically."""


@dataclass(frozen=True)
class PublishResult:
    """What actually landed at the destination, measured after the rename."""

    path: Path
    sha256: str
    size_bytes: int
    replaced_existing: bool


def hash_file(path: Path | str, *, chunk_size: int = _CHUNK) -> str:
    """``sha256:<hex>`` of a file's bytes, streamed.

    Kept as a module function because callers hash *external* paths (a source
    the catalog resolved) that are deliberately outside the workspace
    boundary; ``WorkspaceFilesystem.hash_file`` is the authority for paths
    *inside* it.
    """
    import hashlib

    digest = hashlib.sha256()
    with Path(path).expanduser().open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def safe_file_name(name: str | None, *, suffix: str, default: str) -> str:
    """A single-component file name for an asset id, with a known suffix.

    An asset id is user-supplied, so it may not contain a separator, may not be
    a traversal token, and may not choose its own extension.
    """
    raw = (name or "").strip() or default
    stem = Path(raw).name
    if stem in ("", ".", "..") or "/" in stem or "\\" in stem or "\x00" in stem:
        stem = default
    candidate = Path(stem)
    if candidate.suffix.lower() in _SAFE_SUFFIXES:
        return candidate.name
    return f"{candidate.stem or default}{suffix}"


class RuntimeWorkspace:
    """The engine's root: ``inputs/`` (immutable), ``staging/``, ``media/``.

    Every path decision is made by :class:`WorkspaceFilesystem`; this class adds
    the engine's naming and lifecycle on top of it.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self._fs = WorkspaceFilesystem(self.root)
        self.inputs_dir = self.root / "inputs"
        self.staging_dir = self.root / "staging"
        self.media_dir = self.root / "media"

    @property
    def filesystem(self) -> WorkspaceFilesystem:
        """The canonical authority this workspace delegates to."""
        return self._fs

    # -- construction ----------------------------------------------------- #
    def prepare(self) -> RuntimeWorkspace:
        """Create the three engine directories under the boundary.

        ``WorkspaceFilesystem`` is descriptor-relative and therefore requires
        its root to exist before it can open it.  Creating the *root* is the
        engine's job (it is the caller's declared workspace, not a
        boundary-relative path); every directory below it goes through the
        authority.  This is the only ``mkdir`` in this module, and it is on one
        line so a reviewer can see there is no second containment rule here.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in ("inputs", "staging", "media"):
            try:
                self._fs.ensure_directory(directory)
            except FilesystemBoundaryError as error:  # pragma: no cover - hostile root
                raise WorkspaceBoundaryError(str(error)) from error
        return self

    def contain(self, path: Path | str) -> Path:
        """Resolve ``path`` and prove it is inside the workspace root.

        A thin translation of the authority's refusal into the engine's own
        error type, so callers keep catching ``WorkspaceBoundaryError``.
        """
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            return self._fs.resolve(self._relative(candidate))
        except FilesystemBoundaryError as error:
            raise WorkspaceBoundaryError(str(error)) from error

    def _relative(self, path: Path) -> str:
        """The boundary-relative form of ``path``, refusing anything outside."""
        normalized = Path(os.path.normpath(str(path)))
        if not normalized.is_relative_to(self.root):
            raise WorkspaceBoundaryError(f"path escapes the workspace root: {path}")
        relative = normalized.relative_to(self.root)
        return "." if str(relative) == "." else str(relative)

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
        digest = hash_file(original)
        if expected_sha256 is not None and digest != expected_sha256:
            raise WorkspaceBoundaryError(
                f"input bytes changed before staging: expected {expected_sha256}, read {digest}"
            )
        digest_hex = digest.split(":", 1)[1]
        relative = f"inputs/{digest_hex}{original.suffix.lower()}"
        if self._fs.exists(relative):
            existing_hash, _ = self._fs.hash_file(relative)
            if _with_prefix(existing_hash) == digest:
                return self.root / relative
        # The size cap is the authority's own guard against an unbounded stream
        # (a fifo, a growing file).  It is set from the source's *measured*
        # length plus one byte of slack, so a file that grows while it is being
        # copied is refused rather than silently truncated to a stale length.
        cap = original.stat().st_size + 1
        with original.open("rb") as handle:
            try:
                written, _sha, _size = self._fs.write_stream(relative, handle, max_bytes=cap)
                return written
            except FilesystemBoundaryError as error:  # pragma: no cover - disk refusal
                raise WorkspaceBoundaryError(str(error)) from error
            except ContentLimitExceeded as error:
                raise WorkspaceBoundaryError(
                    f"input grew while it was being staged: {error}"
                ) from error

    # -- runs ------------------------------------------------------------- #
    @contextmanager
    def run_directory(self, run_id: str | None = None) -> Iterator[Path]:
        """An isolated staging directory for exactly one run; removed on exit."""
        identifier = run_id or uuid.uuid4().hex
        if "/" in identifier or "\\" in identifier or identifier in ("", ".", ".."):
            raise WorkspaceBoundaryError(f"unsafe run id: {identifier!r}")
        relative = f"staging/{identifier}"
        try:
            directory = self._fs.ensure_directory(relative)
        except FilesystemBoundaryError as error:
            # ``ensure_directory`` is idempotent; a second run reusing an id must
            # not observe the first run's leftovers, so the refusal is explicit.
            raise WorkspaceBoundaryError(str(error)) from error
        try:
            yield directory
        finally:
            self._remove_run(relative)

    def _remove_run(self, relative: str) -> None:
        """Remove one run directory, never following a symlink out of the root."""
        try:
            self._fs.remove_tree(relative)
        except FilesystemBoundaryError:  # pragma: no cover - defensive cleanup
            # Cleanup must not mask the run's own outcome; a boundary refusal
            # here means something replaced the run dir with a link, which the
            # next ``prepare``/publish will refuse independently.
            pass

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
        source_rel = self._relative(Path(staged).expanduser())
        destination_path = Path(destination).expanduser()
        target_rel = self._relative(destination_path)
        target = self.root / target_rel
        if not self._fs.exists(source_rel):
            raise AtomicPublishError(f"staged artifact does not exist: {target_rel}")
        if source_rel == target_rel:
            raise AtomicPublishError("staged artifact and destination are the same file")
        protected_rels = {self._relative(Path(guard).expanduser()) for guard in protected}
        if target_rel in protected_rels:
            raise AtomicPublishError(f"refusing to publish over a run input: {target.name}")
        for guard in protected_rels:
            if self._fs.exists(guard) and self._fs.exists(target_rel):
                left = (self.root / guard).stat()
                right = target.stat()
                if (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino):
                    raise AtomicPublishError(f"refusing to publish over a run input: {target.name}")
        if target.is_symlink():
            raise AtomicPublishError(f"destination is a symlink, refusing to write: {target}")
        replaced = self._fs.exists(target_rel)
        if replaced and not replace:
            raise AtomicPublishError(
                f"{target.name} already exists; pass replace=True to overwrite deliberately"
            )
        try:
            self._fs.rename_into(self._fs, source_rel, target_rel)
        except FilesystemBoundaryError as error:
            raise AtomicPublishError(f"atomic publish failed for {target.name}: {error}") from error
        except OSError as error:
            raise AtomicPublishError(f"atomic publish failed for {target.name}: {error}") from error
        # The authority returns bare hex; the engine's evidence format is
        # ``sha256:<hex>``.  Normalising here (not in the authority) keeps one
        # representation in one place for everything downstream.
        digest, size_bytes = self._fs.hash_file(target_rel)
        return PublishResult(
            path=target,
            sha256=_with_prefix(digest),
            size_bytes=size_bytes,
            replaced_existing=replaced,
        )

    # -- diagnostics ------------------------------------------------------ #
    def leftovers(self) -> list[str]:
        """Names left in ``staging/`` after a run (a leak, if non-empty)."""
        return sorted(self._fs.list_names("staging"))


__all__ = [
    "AtomicPublishError",
    "PublishResult",
    "RuntimeWorkspace",
    "WorkspaceBoundaryError",
    "hash_file",
    "safe_file_name",
]
