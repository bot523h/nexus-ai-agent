"""File-name sanitisation helpers for bot file commands (P0-6).

``/cloud`` and ``/download`` used to build filesystem paths directly from
user-controlled strings (``doc.file_name``, ``" ".join(context.args)``),
which allowed ``../../`` traversal out of the temp/download directories.
These helpers enforce:

- only the **base name** of a name is ever used for a path component;
- empty / dot-only / control-character names are rejected;
- the final resolved path must stay inside the intended base directory
  (checked with :meth:`pathlib.Path.is_relative_to` after ``resolve()``).

Pure stdlib, no I/O — trivially unit-testable.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Maximum length for a user-facing file name (Telegram's own limit is 512;
#: we stay well under it).
MAX_FILE_NAME_LENGTH = 255

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class UnsafeFileNameError(ValueError):
    """Raised when a file name cannot be made safe."""


def sanitize_file_name(raw: str) -> str:
    """Return a safe base name for *raw*.

    Takes only the final path component (``Path(raw).name``) so both
    ``../../etc/passwd`` and ``C:\\Windows\\x`` collapse to their base
    name, then rejects names that are empty, dot-only, or contain
    control characters.
    """
    if not isinstance(raw, str):
        raise UnsafeFileNameError("file name must be a string")
    base = Path(raw.replace("\\", "/")).name
    if not base or base in (".", ".."):
        raise UnsafeFileNameError("empty file name")
    if _CONTROL_CHARS_RE.search(base):
        raise UnsafeFileNameError("file name contains control characters")
    if len(base) > MAX_FILE_NAME_LENGTH:
        raise UnsafeFileNameError("file name too long")
    return base


def safe_join(base_dir: Path, raw_name: str, *, suffix: str = "") -> Path:
    """Join *raw_name* onto *base_dir* safely and verify containment.

    *suffix* is appended before the file extension (e.g. a uuid) so that
    same-named uploads cannot overwrite each other. Raises
    :class:`UnsafeFileNameError` if the resolved path would escape
    *base_dir*.
    """
    name = sanitize_file_name(raw_name)
    if suffix:
        stem, dot, ext = name.rpartition(".")
        if dot and stem:
            name = f"{stem}{suffix}.{ext}"
        else:
            name = f"{name}{suffix}"
    candidate = base_dir / name
    base = base_dir.resolve()
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise UnsafeFileNameError("path escapes the allowed directory")
    return resolved
