"""Source and Git provenance shared by every Continuum evidence producer/verifier.

Continuum evidence (the committed project-state snapshot and the pack-coverage
artifact) is only meaningful when it is bound to an exact source state.  This
module owns that binding so the snapshot verifier and the coverage harness
cannot drift apart:

* :data:`EVIDENCE_SOURCE_PATHS` — the checkout paths whose content can change
  runtime behaviour, pytest collection, pack membership, migrations or
  dependency resolution.  Every entry must exist (pinned by a unit test), so a
  renamed directory cannot silently leave the evidence surface.
* :func:`working_tree_drift` — tracked, untracked **and ignored-but-importable**
  changes.  ``git status`` alone hides ignored files, yet an ignored ``.py``
  under ``src/`` (``.gitignore`` ignores every ``models/`` and ``data/``
  directory) is imported and collected like any other module.
* :func:`resolve_commit` / :func:`is_ancestor` / :func:`paths_match` — history
  questions that raise instead of guessing when Git cannot answer them.
* :func:`isolated_python_environment` — the hermetic environment for the
  pytest subprocesses (no inherited ``PYTEST_ADDOPTS``/``PYTEST_PLUGINS`` that
  could deselect tests or inject plugins).
* :func:`atomic_write_bytes` — fsync + ``os.replace`` publication.

Every failure to answer a provenance question raises :class:`RuntimeError`;
callers turn that into a non-green finding.  Nothing here returns "probably
fine".
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Paths whose drift invalidates Continuum evidence (relative to the checkout).
EVIDENCE_SOURCE_PATHS: tuple[str, ...] = (
    "src",
    "tests",
    "scripts",
    "migrations",
    "assets",
    "pyproject.toml",
    "alembic.ini",
    "VERSION",
)

#: A full, unabbreviated Git object id (SHA-1 or SHA-256 repositories).
COMMIT_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

#: Ignored paths that are regenerated caches, never importable source.
_DISPOSABLE_COMPONENTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_DISPOSABLE_SUFFIXES = (".pyc", ".pyo", ".egg-info")

#: Environment variables that can silently change what a pytest run selects.
_PYTEST_STEERING_VARIABLES = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")


def _run_git(
    root: Path, arguments: Sequence[str], description: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"{description}: cannot execute git ({exc})") from exc


def _git_failure(description: str, outcome: subprocess.CompletedProcess[str]) -> RuntimeError:
    detail = (outcome.stderr or outcome.stdout).strip().replace("\n", " ")
    return RuntimeError(f"{description}: git exited {outcome.returncode} ({detail})")


def git_output(root: Path, arguments: Sequence[str], description: str) -> str:
    """Run git and return stripped stdout, raising on any non-zero exit."""

    outcome = _run_git(root, arguments, description)
    if outcome.returncode != 0:
        raise _git_failure(description, outcome)
    return outcome.stdout.strip()


def current_commit(root: Path) -> str:
    """Resolve ``HEAD`` to a full commit id (a detached HEAD is valid)."""

    head = git_output(root, ["rev-parse", "--verify", "HEAD^{commit}"], "cannot resolve HEAD")
    if not COMMIT_ID_PATTERN.fullmatch(head):
        raise RuntimeError(f"cannot resolve HEAD: git returned {head!r}, not a full commit id")
    return head


def is_shallow_repository(root: Path) -> bool:
    return (
        git_output(root, ["rev-parse", "--is-shallow-repository"], "cannot inspect git history")
        == "true"
    )


def commit_exists(root: Path, commit: str) -> bool:
    """Whether *commit* names a commit object present in this repository."""

    outcome = _run_git(root, ["cat-file", "-e", f"{commit}^{{commit}}"], "cannot inspect commit")
    return outcome.returncode == 0


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    """Return ancestry, or raise when Git cannot establish the relationship.

    A shallow clone cannot prove a negative: a missing ancestor may simply be
    outside the fetched depth.  That case is therefore *unavailable*, never
    "not an ancestor" and never "ancestor".
    """

    description = "cannot verify ancestry"
    outcome = _run_git(root, ["merge-base", "--is-ancestor", ancestor, descendant], description)
    if outcome.returncode == 0:
        return True
    if outcome.returncode == 1:
        if is_shallow_repository(root):
            raise RuntimeError(f"{description} in a shallow repository")
        return False
    raise _git_failure(description, outcome)


def paths_match(root: Path, left: str, right: str, paths: Sequence[str]) -> bool:
    """Whether *paths* are byte-identical between two commits."""

    description = "cannot compare source trees"
    outcome = _run_git(root, ["diff", "--quiet", left, right, "--", *paths], description)
    if outcome.returncode == 0:
        return True
    if outcome.returncode == 1:
        return False
    raise _git_failure(description, outcome)


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _disposable(path: str) -> bool:
    parts = [part for part in path.split("/") if part]
    if any(part in _DISPOSABLE_COMPONENTS for part in parts):
        return True
    return bool(parts) and parts[-1].endswith(_DISPOSABLE_SUFFIXES)


def working_tree_drift(
    root: Path,
    *,
    scope: Sequence[str] | None = None,
    source_paths: Sequence[str] = EVIDENCE_SOURCE_PATHS,
) -> tuple[str, ...]:
    """Return every checkout change that the recorded commit cannot vouch for.

    Tracked modifications and untracked files are drift within *scope* (the
    whole checkout when ``None``).  Ignored paths are drift when they sit under
    *source_paths* and are not a regenerated cache, because Python imports and
    pytest collects ignored files exactly like tracked ones.
    """

    description = "cannot inspect working tree"
    outcome = _run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"],
        description,
    )
    if outcome.returncode != 0:
        raise _git_failure(description, outcome)
    # never strip: the first record's status column may begin with a space
    raw = outcome.stdout
    entries: list[str] = []
    records = iter(raw.split("\0"))
    for record in records:
        if not record:
            continue
        status, path = record[:2], record[3:]
        if status[0] in {"R", "C"}:
            # porcelain -z emits the rename/copy source as a separate record
            next(records, None)
        if status == "!!":
            if _under(path, source_paths) and not _disposable(path):
                entries.append(f"ignored {path}")
            continue
        if scope is None or _under(path, scope):
            entries.append(f"{status.strip() or '?'} {path}")
    return tuple(sorted(entries))


def source_digest(root: Path, files: Iterable[Path]) -> str:
    """Return a stable SHA-256 over ``(relative path, content)`` of *files*."""

    resolved_root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted({Path(file).resolve() for file in files}, key=lambda item: item.as_posix()):
        try:
            name = path.relative_to(resolved_root).as_posix()
        except ValueError:
            name = path.as_posix()
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"\n")
    return "sha256:" + digest.hexdigest()


def isolated_python_environment(
    root: Path, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Environment for an evidence subprocess rooted at *root*.

    ``src`` comes first on ``PYTHONPATH`` so the measured checkout (not an
    unrelated installation) is imported; plugin auto-loading and every
    selection-steering pytest variable are removed so the caller's shell cannot
    deselect tests or inject plugins into an evidence run.
    """

    environment = dict(os.environ if base is None else base)
    for variable in _PYTEST_STEERING_VARIABLES:
        environment.pop(variable, None)
    source = str(root / "src")
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not inherited else source + os.pathsep + inherited
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return environment


def interpreter_identity() -> str:
    """The interpreter facts that change compiler line tables (and thus coverage)."""

    info = sys.version_info
    return f"{sys.implementation.name}-{info.major}.{info.minor}.{info.micro}"


def _fsync_directory(directory: Path) -> None:
    """Persist a rename's directory entry where the platform supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes) -> Path:
    """Publish *payload* at *path* atomically (temp file, fsync, ``os.replace``).

    An interruption before the replacement leaves the previous file intact and
    removes the temporary file; readers never observe a partial document.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return path


__all__ = [
    "COMMIT_ID_PATTERN",
    "EVIDENCE_SOURCE_PATHS",
    "REPO_ROOT",
    "atomic_write_bytes",
    "commit_exists",
    "current_commit",
    "git_output",
    "interpreter_identity",
    "is_ancestor",
    "is_shallow_repository",
    "isolated_python_environment",
    "paths_match",
    "source_digest",
    "working_tree_drift",
]
