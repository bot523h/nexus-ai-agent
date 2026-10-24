"""Controlled discovery of authorized knowledge sources.

The scanner is the security boundary of ingestion.  It walks **only**
configured roots (the allowlist), deterministically, and classifies every
file it meets:

* symlinked files are rejected outright — the subsystem never follows a
  link, so a link that leaves the authorized root cannot smuggle content in
  (a resolved-path check underneath catches every other escape attempt);
* secret-shaped filenames (``.env*`` keys, private keys, credentials,
  tokens) are rejected before their extension is even considered;
* anything outside the configured extension set is recorded as
  ``unsupported`` — including binaries mislabelled with a text extension
  (NUL byte sniff) and non-UTF-8 content;
* files above ``max_file_bytes`` are rejected without reading fully;
* directories that are VCS internals, dependency trees or generated caches
  are never descended into.

Nothing here reads file *content* — hashing and decoding happen one file at
a time in the service, so a 2000-file scan never materialises the tree in
memory.  Output order is sorted by path: the same tree always produces the
same run, which is what makes rebuilds deterministic.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

from nexus_ai_agent.knowledge_retrieval.config import KnowledgeConfig
from nexus_ai_agent.knowledge_retrieval.errors import SourceRejectedError
from nexus_ai_agent.knowledge_retrieval.models import ScanIssue

__all__ = [
    "EXCLUDED_DIR_NAMES",
    "SECRET_FILE_PATTERNS",
    "ScanOutcome",
    "ScannedSource",
    "is_secret_filename",
    "scan_roots",
    "source_id_for",
    "validate_member",
]

#: Directory names that are never descended into (VCS, dependencies, caches,
#: build output).  Matched case-sensitively against the entry name.
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".cache",
        ".next",
        ".nuxt",
        ".output",
        ".parcel-cache",
        ".turbo",
        "dist",
        "build",
        "site-packages",
    }
)

#: Secret-shaped filenames — rejected before extension classification so the
#: exclusion is explicit and auditable in the ingest report, not incidental.
SECRET_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "*.asc",
    "*.gpg",
    "*.ppk",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials",
    "credentials.*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*.token",
    "secrets.*",
    ".secrets*",
    "*.secret",
)

#: Directories are reported relative to their root with this display prefix
#: when a member would escape (never happens through a normal walk — belt
#: and braces for mount/symlink trickery).
_TRAVERSAL_MARK = "<escape>"


@dataclass(frozen=True, slots=True)
class ScannedSource:
    """A candidate file that passed every discovery-time check."""

    root: Path  # resolved authorized root
    rel_path: str  # POSIX-style path relative to root (display + identity)
    path: Path  # absolute path inside the root
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    """Deterministic scan result: sorted candidates + classified refusals."""

    sources: tuple[ScannedSource, ...]
    issues: tuple[ScanIssue, ...]
    complete: bool = True
    incomplete_reason: str | None = None


def is_secret_filename(name: str) -> bool:
    """True when the file name looks like credentials/key material."""
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in SECRET_FILE_PATTERNS)


def source_id_for(root: Path, rel_path: str) -> str:
    """Stable source identity: hash of (resolved root, relative path).

    Content-independent on purpose — a file's id stays the same across
    edits (the *version* is its sha256), and two different trees never
    collide because the resolved root is part of the identity.
    """

    import hashlib

    material = f"{root.resolve()}\n{rel_path}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


def validate_member(root: Path, path: Path) -> None:
    """Raise :class:`SourceRejectedError` when ``path`` escapes ``root``.

    ``path.resolve(strict=False)`` follows symlinks, so this single check
    rejects traversal *and* symlink escapes; the scanner additionally never
    follows symlinked directories.
    """
    resolved_root = root.resolve()
    resolved = path.resolve(strict=False)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise SourceRejectedError(
            f"path escapes its authorized root: {path.name}",
            hint="sources must resolve inside a configured root",
        )


def _classify(root: Path, path: Path, rel_path: str, config: KnowledgeConfig) -> ScanIssue | None:
    """Discovery-time classification; ``None`` means the file is a candidate."""
    if path.is_symlink():
        return ScanIssue(path=rel_path, reason="symlink")
    try:
        validate_member(root, path)
    except SourceRejectedError:
        return ScanIssue(path=_TRAVERSAL_MARK, reason="traversal")
    if is_secret_filename(path.name):
        return ScanIssue(path=rel_path, reason="secret")
    if path.suffix.lower() not in config.allowed_extensions:
        return ScanIssue(path=rel_path, reason="unsupported")
    try:
        size = path.stat().st_size
    except OSError:
        return ScanIssue(path=rel_path, reason="unreadable")
    if size > config.max_file_bytes:
        return ScanIssue(path=rel_path, reason="oversize")
    return None


def scan_roots(config: KnowledgeConfig, roots: tuple[Path, ...]) -> ScanOutcome:
    """Walk every authorized root and classify its files, deterministically.

    Never raises for per-file conditions — refusals become
    :class:`~nexus_ai_agent.knowledge_retrieval.models.ScanIssue` rows so the
    report shows exactly what was skipped and why.  A root that vanished
    between configuration validation and the walk surfaces as one
    ``unreadable`` issue rather than an exception (the registry keeps its
    sources; the next run reconciles).

    A failed, interrupted or budget-limited walk must never be treated as
    proof that a source is absent — ``complete`` is ``False`` when any
    configured root could not be fully enumerated (missing root, ``os.walk``
    ``listdir`` error, or an unexpected exception).  The caller must preserve
    existing registry records unless ``complete`` is ``True``.
    """
    candidates: list[ScannedSource] = []
    issues: list[ScanIssue] = []
    complete = True
    incomplete_reason: str | None = None

    for root in roots:
        if not root.is_dir():
            issues.append(ScanIssue(path=str(root), reason="unreadable"))
            complete = False
            if incomplete_reason is None:
                incomplete_reason = f"unreadable_root:{root}"
            continue

        # Collect walk errors via onerror — without it Python's os.walk
        # silently skips unreadable directories and returns an incomplete
        # listing that would otherwise be mistaken for \"file deleted\".
        walk_errors: list[OSError] = []

        def _onerror(err: OSError) -> None:
            walk_errors.append(err)
            # Record as an unreadable issue so the report shows the gap.
            target = getattr(err, "filename", None) or str(root)
            issues.append(ScanIssue(path=str(target), reason="unreadable"))

        try:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_onerror):
                if walk_errors:
                    complete = False
                    if incomplete_reason is None:
                        first = walk_errors[0]
                        incomplete_reason = (
                            f"walk_error:{first.filename or root}:{first.strerror or first.errno}"
                        )
                current = Path(dirpath)
                # Prune excluded directories in place (also drops symlinked dirs,
                # which os.walk would otherwise list but never follow).
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if name not in EXCLUDED_DIR_NAMES and not (current / name).is_symlink()
                )
                for name in sorted(filenames):
                    path = current / name
                    try:
                        rel_path = path.relative_to(root).as_posix()
                    except ValueError:
                        issues.append(ScanIssue(path=_TRAVERSAL_MARK, reason="traversal"))
                        continue
                    issue = _classify(root, path, rel_path, config)
                    if issue is not None:
                        issues.append(issue)
                        continue
                    try:
                        size = path.stat().st_size
                    except OSError:
                        issues.append(ScanIssue(path=rel_path, reason="unreadable"))
                        continue
                    candidates.append(
                        ScannedSource(root=root, rel_path=rel_path, path=path, size_bytes=size)
                    )
        except OSError as exc:  # pragma: no cover — defensive: interrupted walk
            complete = False
            if incomplete_reason is None:
                incomplete_reason = f"walk_exception:{exc}"
            issues.append(ScanIssue(path=str(root), reason="unreadable"))
        except Exception as exc:  # pragma: no cover — never bubble a partial scan
            complete = False
            if incomplete_reason is None:
                incomplete_reason = f"walk_exception:{type(exc).__name__}:{exc}"[:200]
            issues.append(ScanIssue(path=str(root), reason="unreadable"))

        if walk_errors and incomplete_reason is None:
            # Walk finished but onerror already fired.
            complete = False
            first = walk_errors[0]
            incomplete_reason = (
                f"walk_error:{first.filename or root}:{first.strerror or first.errno}"
            )

    candidates.sort(key=lambda item: (str(item.root), item.rel_path))
    issues.sort(key=lambda item: (item.reason, item.path))
    return ScanOutcome(
        sources=tuple(candidates),
        issues=tuple(issues),
        complete=complete,
        incomplete_reason=incomplete_reason,
    )
