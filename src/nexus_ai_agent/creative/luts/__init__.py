"""Local 3D LUT library for ``color.apply_lut`` (session 3, task-174 lane half).

Session-2 honesty finding: ``color.apply_lut`` derived an ``AssetRecord`` whose
``content_sha256`` was a hash of ``"<clip-hash>:lut:<name>:..."`` — a spec
digest masquerading as media identity (NAG-003).  This module is the other
half of the fix: a versioned, content-addressed library of real ``.cube``
files plus the strict resolver the render plan uses to turn a LUT *name*
into a staged *file* for the lane's ``lut3d`` stage.

Design decisions (§13):

* **Where .cube assets live.** Shipped inside this package
  (``creative/luts/*.cube``) — small, auditable text, no network fetch.
  Explicit absolute paths are also accepted (operator-staged looks) but go
  through the same validation.
* **Versioning.** Files are immutable: the plan records ``lut_sha256`` (bytes
  of the .cube) in the op, and artifact provenance carries it, so a render
  is reproducible from ``(lut_name, lut_sha256)`` alone.
* **Signing.** Out of scope — the library is trusted local data (like the
  lane's own filter strings), never downloaded.  No signature is claimed.
* **Offline.** Always — no fetch, no model download.
* **Missing LUT.** Typed :class:`LutNotFoundError` at plan time.  Never a
  fake render, never a silent identity pass.

Validation (:func:`validate_cube_file`) parses the header strictly
(``LUT_3D_SIZE`` 2..64, optional ``DOMAIN_MIN/MAX``, exact ``N³`` data rows
of floats in ``[0, 1]``) because ``lut3d`` fails late and cryptically on a
malformed file — the plan must fail closed first, with the file named.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

#: Directory that ships the builtin looks (this package's own directory).
LUT_LIBRARY_DIR = Path(__file__).resolve().parent

#: ``.cube`` files shipped in this package (auditable, immutable).
BUILTIN_LUTS: tuple[str, ...] = ("identity", "warm")

#: LUT names are bare identifiers — never paths (fail closed on traversal).
_LUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

#: Header keys the validator understands (``TITLE``/``DOMAIN_*`` informational).
_SIZE_RE = re.compile(r"^LUT_3D_SIZE\s+(\d+)\s*$")


class LutError(ValueError):
    """Base error for the LUT library (typed, fail-closed)."""


class LutNotFoundError(LutError):
    """No LUT answers this name or path — the plan must refuse, not fake."""


class LutValidationError(LutError):
    """The ``.cube`` file exists but is malformed — ``lut3d`` would fail late."""


def list_luts() -> tuple[str, ...]:
    """Builtin LUT names in deterministic order."""
    return BUILTIN_LUTS


def library_path(lut_name: str) -> Path:
    """Path of a builtin LUT (membership-checked, traversal-proof)."""
    if not _LUT_NAME_RE.match(lut_name):
        raise LutNotFoundError(f"invalid LUT name (bare identifier required): {lut_name!r}")
    if lut_name not in BUILTIN_LUTS:
        raise LutNotFoundError(
            f"unknown builtin LUT {lut_name!r} (shipped: {', '.join(BUILTIN_LUTS)})"
        )
    return LUT_LIBRARY_DIR / f"{lut_name}.cube"


def resolve_lut(ref: str) -> Path:
    """Resolve a LUT reference to a validated ``.cube`` path.

    Bare names resolve inside the shipped library; absolute paths resolve to
    operator-staged files (same validation either way).  Anything else —
    relative paths, traversal, missing files — raises :class:`LutNotFoundError`
    or :class:`LutValidationError`; the caller (the render plan) turns that
    into a typed plan refusal.
    """
    candidate: Path
    if _LUT_NAME_RE.match(ref) and "/" not in ref and "\\" not in ref and not ref.startswith("."):
        if ref in BUILTIN_LUTS:
            candidate = library_path(ref)
        else:
            raise LutNotFoundError(
                f"unknown LUT {ref!r} (shipped: {', '.join(BUILTIN_LUTS)}; "
                "stage operator looks by absolute path)"
            )
    else:
        candidate = Path(ref)
        if not candidate.is_absolute():
            raise LutNotFoundError(f"LUT path must be absolute: {ref!r}")
    if not candidate.is_file():
        raise LutNotFoundError(f"LUT file not found: {candidate}")
    validate_cube_file(candidate)
    return candidate


def validate_cube_file(path: Path) -> int:
    """Strictly validate a ``.cube`` file; returns ``LUT_3D_SIZE``.

    Raises :class:`LutValidationError` naming the file and the defect.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LutValidationError(f"{path}: unreadable: {exc}") from exc
    size: int | None = None
    data_rows = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("TITLE"):
            continue
        if upper.startswith("DOMAIN_MIN") or upper.startswith("DOMAIN_MAX"):
            parts = line.split()[1:]
            if len(parts) != 3:
                raise LutValidationError(f"{path}:{lineno}: bad DOMAIN line: {line!r}")
            try:
                [float(part) for part in parts]
            except ValueError:
                raise LutValidationError(f"{path}:{lineno}: bad DOMAIN floats: {line!r}") from None
            continue
        if upper.startswith("LUT_1D_SIZE"):
            raise LutValidationError(f"{path}:{lineno}: 1D LUTs are not supported: {line!r}")
        match = _SIZE_RE.match(line)
        if match:
            if size is not None:
                raise LutValidationError(f"{path}:{lineno}: duplicate LUT_3D_SIZE")
            size = int(match.group(1))
            if not 2 <= size <= 64:
                raise LutValidationError(
                    f"{path}:{lineno}: LUT_3D_SIZE {size} outside the 2..64 lane band"
                )
            continue
        if upper.startswith("LUT_3D_INPUT_RANGE"):
            continue
        if size is None:
            raise LutValidationError(f"{path}:{lineno}: data before LUT_3D_SIZE: {line!r}")
        parts = line.split()
        if len(parts) != 3:
            raise LutValidationError(f"{path}:{lineno}: expected 3 floats: {line!r}")
        try:
            values = [float(part) for part in parts]
        except ValueError:
            raise LutValidationError(f"{path}:{lineno}: bad floats: {line!r}") from None
        if any(not 0.0 <= value <= 1.0 for value in values):
            raise LutValidationError(f"{path}:{lineno}: sample outside [0, 1]: {line!r}")
        data_rows += 1
    if size is None:
        raise LutValidationError(f"{path}: missing LUT_3D_SIZE header")
    expected = size**3
    if data_rows != expected:
        raise LutValidationError(
            f"{path}: expected {expected} data rows for LUT_3D_SIZE {size}, got {data_rows}"
        )
    return size


def lut_sha256(path: Path) -> str:
    """Content hash (``sha256:<hex>``) of the ``.cube`` bytes — the plan pins this."""
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "BUILTIN_LUTS",
    "LUT_LIBRARY_DIR",
    "LutError",
    "LutNotFoundError",
    "LutValidationError",
    "library_path",
    "list_luts",
    "lut_sha256",
    "resolve_lut",
    "validate_cube_file",
]
