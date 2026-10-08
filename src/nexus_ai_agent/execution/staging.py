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
  opened with ``O_NOFOLLOW`` so a swapped leaf cannot redirect a write.
* **No provider-controlled final path** — a publish target must be a relative
  path contained in the *declared* ``final_root``; a caller (or provider)
  cannot name an arbitrary absolute destination.
* **No direct write to the authoritative namespace** — staging writes are
  confined to the staging directory; only :meth:`AttemptStaging.publish`
  (an atomic rename) crosses into ``final_root``.
* **Cleanup / quarantine** — a finished or failed attempt is removed or moved
  aside, never left to be mistaken for a published artifact.

The low-level symlink/traversal checks mirror
:class:`nexus_ai_agent.tools.filesystem_policy.WorkspaceFilesystem`; this module
adds the attempt-scoped layout and the publish/quarantine lifecycle on top.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

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

    # -- staging --------------------------------------------------------- #
    def prepare(self) -> Path:
        """Create (idempotently) this attempt's staging directory."""
        self.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        staged source must be inside this attempt's staging directory.  The
        move is a single ``os.replace`` (atomic on one filesystem).
        """
        if self.final_root is None:
            raise StagingBoundaryError("publishing requires a declared final_root")
        source = Path(staged)
        resolved_source = source.resolve(strict=False)
        if not resolved_source.is_relative_to(self.staging_dir.resolve(strict=False)):
            raise StagingBoundaryError("staged source must live inside this attempt's staging dir")
        if not resolved_source.is_file():
            raise StagingBoundaryError("staged source does not exist")
        parts = _relative_parts(target_rel)
        destination = self.final_root.joinpath(*parts)
        resolved_destination = destination.resolve(strict=False)
        if not resolved_destination.is_relative_to(self.final_root):
            raise StagingBoundaryError("publish target escapes the declared final root")
        if resolved_destination.is_relative_to(self.root):
            raise StagingBoundaryError("publish target must not be inside the staging root")
        _assert_no_symlink_components(self.final_root, parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(resolved_source, resolved_destination)
        return resolved_destination

    # -- lifecycle ------------------------------------------------------- #
    def cleanup(self) -> None:
        """Remove this attempt's staging tree (best-effort, symlink-safe)."""
        attempt_dir = self.root / self.job_id / self.attempt_id
        if attempt_dir.exists() and not attempt_dir.is_symlink():
            shutil.rmtree(attempt_dir, ignore_errors=True)

    def quarantine(self, reason: str) -> Path:
        """Move this attempt's staging tree aside under ``_quarantine``.

        Quarantine keeps failed/incomplete bytes observable for forensics
        without leaving them where a publish could mistake them for an
        authoritative artifact.
        """
        component = _safe_component(reason, field_name="quarantine reason")
        attempt_dir = self.root / self.job_id / self.attempt_id
        destination = self.root / "_quarantine" / self.job_id / f"{self.attempt_id}.{component}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if attempt_dir.exists() and not attempt_dir.is_symlink():
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            os.replace(attempt_dir, destination)
        return destination
