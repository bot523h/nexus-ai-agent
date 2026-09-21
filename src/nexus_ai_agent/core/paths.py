"""Filesystem containment for user-controlled file names.

Threat model
------------
Telegram hands the bot ``document.file_name`` — an arbitrary, attacker-chosen
string — and several handlers used to do ``directory / file_name`` with it.
``file_name = "../../.ssh/authorized_keys"`` then writes (or reads) outside the
intended directory.  Absolute paths, embedded NUL bytes and Windows-style
``..\\`` separators are the same bug in different costumes.

These helpers make the containment explicit and total:

* the name is reduced to a *basename* (every directory component is dropped);
* the on-disk name is a random token, so the user never controls the path;
* the resulting path is resolved and checked against the directory, so a
  symlink inside the directory cannot point out of it either.

Callers that must show the user something should keep the sanitized
``display_name`` — never the raw input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

#: Longest accepted file name. Telegram's own limit is far lower; this only
#: exists to keep a hostile input from becoming a pathological path.
MAX_NAME_LENGTH = 200

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class UnsafeFileName(ValueError):
    """The supplied name cannot be mapped safely into a directory."""


@dataclass(frozen=True)
class SafeLocalFile:
    """A validated local path plus the label that is safe to display."""

    path: Path
    display_name: str
    suffix: str


def sanitize_file_name(raw: str) -> str:
    """Reduce *raw* to a safe, display-able base name.

    Traversal is *rejected*, not silently renamed: a name containing ``..`` is
    not a file name anybody types by accident, and quietly rewriting it would
    hide the attempt from the logs and the user.  Plain path separators are
    tolerated — the base name is kept and the directory part dropped.

    Raises :class:`UnsafeFileName` when the name is hostile or empty.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeFileName("file name is empty")
    if _CONTROL_CHARS.search(raw):
        raise UnsafeFileName("file name contains control characters")
    # Normalise Windows separators, then inspect every component.
    unified = raw.replace("\\", "/")
    components = [part for part in unified.split("/") if part not in ("", ".")]
    if any(part == ".." for part in components):
        raise UnsafeFileName("file name attempts path traversal")
    name = (components[-1] if components else "").strip().strip(".")
    if not name:
        raise UnsafeFileName("file name has no usable base name")
    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH]
    return name


def safe_suffix(raw: str, *, allowed: frozenset[str] | None = None) -> str:
    """Return a lower-case ``.ext`` for *raw*, or ``""``.

    With *allowed* given, any other extension raises :class:`UnsafeFileName`.
    """
    suffix = Path(sanitize_file_name(raw)).suffix.lower()
    if not suffix:
        return ""
    if allowed is not None and suffix not in allowed:
        raise UnsafeFileName(f"file type {suffix} is not accepted")
    if len(suffix) > 12:
        return ""
    return suffix


def safe_local_path(
    directory: Path,
    raw_name: str,
    *,
    allowed_suffixes: frozenset[str] | None = None,
    keep_name: bool = False,
) -> SafeLocalFile:
    """Map an untrusted *raw_name* onto a path strictly inside *directory*.

    ``keep_name=False`` (default) writes to ``<uuid><ext>``: the attacker
    controls neither the path nor the name, which also removes overwrite and
    symlink-swap races between two uploads of the same name.

    ``keep_name=True`` keeps the sanitized base name — use it only when the
    file is addressed later by that name.
    """
    display = sanitize_file_name(raw_name)
    suffix = safe_suffix(display, allowed=allowed_suffixes)
    stem = Path(display).stem if keep_name else uuid4().hex
    candidate = directory / f"{stem}{suffix}"
    resolved = candidate.resolve()
    root = directory.resolve()
    if resolved != root and root not in resolved.parents:
        # Defence in depth: the base-name reduction above already makes this
        # unreachable for normal input, but a symlinked directory should still
        # not be able to redirect writes outside the intended root.
        raise UnsafeFileName("resolved path escapes the target directory")
    return SafeLocalFile(path=resolved, display_name=display, suffix=suffix)


def safe_remote_key(raw_name: str, *, allowed_suffixes: frozenset[str] | None = None) -> str:
    """Sanitize a name used as an object-storage key (``a/b`` → ``b``)."""
    display = sanitize_file_name(raw_name)
    suffix = safe_suffix(display, allowed=allowed_suffixes)
    return f"{Path(display).stem}{suffix}"
