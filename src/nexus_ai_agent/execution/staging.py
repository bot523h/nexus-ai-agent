"""Attempt-scoped staging isolation for the execution core.

The database and the filesystem are **not** one transaction.  A physical
artifact existing on disk is not the same as an authoritative artifact being
committed: the queue's fenced DB commit is what makes an artifact
authoritative.  This module supplies the filesystem half of that two-phase
publication, isolated per attempt:

    stage (this attempt's own directory)
      -> atomic rename into an immutable, uniquely-addressed final location
      -> (the queue then performs its fenced, atomic DB commit referencing it)

Isolation guarantees (each is tested in
``tests/unit/test_execution_staging.py``):

* **Cross-attempt isolation** — every attempt writes under
  ``<root>/<job_id>/<attempt_id>/staging/``; one attempt can never address
  another attempt's staging tree, so a superseded attempt cannot clobber the
  current owner's bytes.
* **Path-traversal protection** — every component is validated; absolute
  paths, ``..``, separators inside a name, and NUL bytes are rejected.
* **Symlink handling** — no path component may be a symlink, and files are
  opened with ``O_NOFOLLOW`` so a swapped leaf cannot redirect a write.  The
  ancestor (``job_id`` / ``attempt_id`` / ``staging``), the staged source, and
  every quarantine source/destination component are validated too.
* **No provider-controlled final path** — a publish target must be a relative
  path contained in the *declared* ``final_root``; a caller (or provider)
  cannot name an arbitrary absolute destination.
* **No direct write to the authoritative namespace** — staging writes are
  confined to the staging directory; only :meth:`AttemptStaging.publish`
  (an atomic rename) crosses into ``final_root``.
* **Cleanup / quarantine** — a finished or failed attempt is removed or moved
  aside, never left to be mistaken for a published artifact.

The mutation primitives (create/rename/remove) are the additive, generic
capabilities of :class:`nexus_ai_agent.tools.filesystem_policy.WorkspaceFilesystem`;
this module adds the attempt-scoped layout and the publish/quarantine
lifecycle on top of that one security boundary rather than defining a second,
competing one.  The path-component validation below is the shared lexical
gate both layers use.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from nexus_ai_agent.tools.filesystem_policy import FilesystemBoundaryError, WorkspaceFilesystem

__all__ = ["AttemptStaging", "StagingBoundaryError"]

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

#: A single path component must be a conservative token: no separators, no
#: traversal, no NUL.  This is deliberately stricter than a filesystem allows.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class StagingBoundaryError(ValueError):
    """A staging path cannot be proven to remain inside its boundary."""


def _safe_component(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise StagingBoundaryError(f"{field_name} must be a non-empty string")
    if "\x00" in value:
        raise StagingBoundaryError(f"{field_name} contains a NUL byte")
    if value in (".", "..") or "/" in value or "\\" in value:
        raise StagingBoundaryError(f"{field_name} must be a single safe path component")
    if not _SAFE_COMPONENT.match(value):
        raise StagingBoundaryError(
            f"{field_name} must match {_SAFE_COMPONENT.pattern!r}; got {value!r}"
        )
    return value


def _relative_parts(raw: str) -> tuple[str, ...]:
    """Validate a relative path and return its components (no traversal)."""
    if not isinstance(raw, str) or not raw:
        raise StagingBoundaryError("path must be a non-empty string")
    if "\x00" in raw:
        raise StagingBoundaryError("path contains a NUL byte")
    if raw.startswith(("/", "\\")) or Path(raw).is_absolute():
        raise StagingBoundaryError("absolute paths are not allowed")
    normalized = raw.replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts:
        raise StagingBoundaryError("path must name at least one component")
    for part in parts:
        if part == "..":
            raise StagingBoundaryError("path traversal is not allowed")
        _safe_component(part, field_name="path component")
    return parts


def _assert_no_symlink_components(base: Path, parts: tuple[str, ...]) -> None:
    """Reject any existing path component under ``base`` that is a symlink."""
    current = base
    for part in parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise StagingBoundaryError(f"symlinks are not allowed in staging paths: {current}")


class AttemptStaging:
    """An attempt-scoped, symlink-resistant staging workspace.

    ``root`` is the staging namespace (operator-declared).  ``final_root`` is
    the *declared* authoritative namespace a publish may target; when omitted,
    publishing is disabled and only staging/cleanup are available.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        job_id: str,
        attempt_id: str,
        final_root: str | Path | None = None,
    ) -> None:
        self.job_id = _safe_component(job_id, field_name="job_id")
        self.attempt_id = _safe_component(attempt_id, field_name="attempt_id")
        self.root = Path(root).expanduser().resolve(strict=False)
        self.final_root = (
            None if final_root is None else Path(final_root).expanduser().resolve(strict=False)
        )
        self.staging_dir = self.root / self.job_id / self.attempt_id / "staging"
        # The one filesystem security boundary (descriptor-relative, no-follow).
        self._fs = WorkspaceFilesystem(self.root)
        self._final_fs = None if self.final_root is None else WorkspaceFilesystem(self.final_root)

    # -- layout helpers --------------------------------------------------- #
    def _attempt_rel(self) -> str:
        return f"{self.job_id}/{self.attempt_id}"

    def _staging_rel(self) -> str:
        return f"{self._attempt_rel()}/staging"

    def _source_relative(self, staged: str | Path) -> str:
        """Return the contained source path relative to this attempt's staging.

        Lexical containment only (no symlink resolution): the physical
        symlink check is done descriptor-relative by ``require_regular_file``.
        """
        candidate = Path(staged)
        if not candidate.is_absolute():
            candidate = self.staging_dir / candidate
        normalized = Path(os.path.normpath(str(candidate)))
        if not normalized.is_relative_to(self.staging_dir):
            raise StagingBoundaryError("staged source must live inside this attempt's staging dir")
        rel_parts = normalized.relative_to(self.staging_dir).parts
        if not rel_parts:
            raise StagingBoundaryError("staged source must name a file")
        for part in rel_parts:
            _safe_component(part, field_name="staged source component")
        return f"{self._staging_rel()}/" + "/".join(rel_parts)

    # -- staging --------------------------------------------------------- #
    def prepare(self) -> Path:
        """Create (idempotently) this attempt's staging directory."""
        # Ancestors before creation: a symlinked job_id/attempt_id/staging
        # must be refused, not followed.  (The root itself is operator-declared.)
        _assert_no_symlink_components(self.root, (self.job_id, self.attempt_id, "staging"))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._fs.ensure_directory(self._attempt_rel())
            self._fs.ensure_directory(self._staging_rel())
        except (FilesystemBoundaryError, OSError) as exc:
            raise StagingBoundaryError(str(exc)) from exc
        # And after creation: the freshly created directories are real.
        _assert_no_symlink_components(self.root, (self.job_id, self.attempt_id, "staging"))
        return self.staging_dir

    def stage_path(self, name: str) -> Path:
        """Return a contained path inside this attempt's staging directory."""
        component = _safe_component(name, field_name="staged file name")
        _assert_no_symlink_components(self.staging_dir, (component,))
        return self.staging_dir / component

    def write(self, name: str, data: bytes) -> Path:
        """Write bytes into staging with ``O_NOFOLLOW`` (no leaf swap)."""
        component = _safe_component(name, field_name="staged file name")
        self.prepare()
        _assert_no_symlink_components(self.staging_dir, (component,))
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _CLOEXEC
        try:
            fd = os.open(self.staging_dir / component, flags, 0o600)
        except OSError as exc:  # pragma: no cover - platform dependent
            raise StagingBoundaryError("staged file could not be opened safely") from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except OSError as exc:  # pragma: no cover - platform dependent
            raise StagingBoundaryError("staged file could not be written") from exc
        return self.staging_dir / component

    # -- publication ----------------------------------------------------- #
    def publish(self, staged: str | Path, target_rel: str) -> Path:
        """Atomically move a staged file to a contained final location.

        The target is a *relative* path inside the declared ``final_root``; the
        staged source must be a regular file inside this attempt's staging
        directory (never a symlink, never a directory).  The move is a single
        descriptor-relative ``os.rename`` (atomic on one filesystem) that
        cannot be redirected by a symlinked component on either side.
        """
        if self.final_root is None or self._final_fs is None:
            raise StagingBoundaryError("publishing requires a declared final_root")
        # 3B: validate the source lexically, then prove it is a regular file
        # descriptor-relative (rejects every symlinked ancestor and leaf).
        source_rel = self._source_relative(staged)
        try:
            self._fs.require_regular_file(source_rel)
        except FilesystemBoundaryError as exc:
            raise StagingBoundaryError(str(exc)) from exc
        parts = _relative_parts(target_rel)
        destination = self.final_root.joinpath(*parts)
        resolved_destination = destination.resolve(strict=False)
        if not resolved_destination.is_relative_to(self.final_root):
            raise StagingBoundaryError("publish target escapes the declared final root")
        if resolved_destination.is_relative_to(self.root):
            raise StagingBoundaryError("publish target must not be inside the staging root")
        _assert_no_symlink_components(self.final_root, parts)
        dest_rel = "/".join(parts)
        try:
            self.final_root.mkdir(parents=True, exist_ok=True)
            if len(parts) > 1:
                self._final_fs.ensure_directory("/".join(parts[:-1]))
            self._fs.rename_into(self._final_fs, source_rel, dest_rel)
        except (FilesystemBoundaryError, OSError) as exc:
            raise StagingBoundaryError(str(exc)) from exc
        return resolved_destination

    # -- lifecycle ------------------------------------------------------- #
    def cleanup(self) -> None:
        """Remove this attempt's staging tree (best-effort, symlink-safe)."""
        # 3A cleanup: refuse to follow a symlinked job_id/attempt_id ancestor.
        _assert_no_symlink_components(self.root, (self.job_id, self.attempt_id))
        try:
            self._fs.remove_tree(self._attempt_rel())
        except FilesystemBoundaryError as exc:
            raise StagingBoundaryError(str(exc)) from exc

    def _remove_destination(self, rel: str) -> None:
        """Remove an existing quarantine destination (dir or file), safely."""
        try:
            self._fs.remove_tree(rel)
        except FilesystemBoundaryError:
            pass
        try:
            self._fs.require_regular_file(rel)
        except FilesystemBoundaryError:
            return
        self._fs.unlink(rel)

    def quarantine(self, reason: str) -> Path:
        """Move this attempt's staging tree aside under ``_quarantine``.

        Quarantine keeps failed/incomplete bytes observable for forensics
        without leaving them where a publish could mistake them for an
        authoritative artifact.
        """
        component = _safe_component(reason, field_name="quarantine reason")
        dest_rel = f"_quarantine/{self.job_id}/{self.attempt_id}.{component}"
        # 3C: validate the source components and the complete destination
        # components before creating/removing/replacing anything.
        _assert_no_symlink_components(self.root, (self.job_id, self.attempt_id))
        _assert_no_symlink_components(self.root, ("_quarantine", self.job_id))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self._fs.ensure_directory(f"_quarantine/{self.job_id}")
            self._remove_destination(dest_rel)
            self._fs.rename_within(self._attempt_rel(), dest_rel)
        except (FilesystemBoundaryError, OSError) as exc:
            raise StagingBoundaryError(str(exc)) from exc
        return self.root / "_quarantine" / self.job_id / f"{self.attempt_id}.{component}"
