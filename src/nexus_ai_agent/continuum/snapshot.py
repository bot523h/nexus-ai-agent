"""Read, write and verify ``.nexus/continuum.json``.

Why this file exists
--------------------
Commits are never lost — they are on the remote.  What is lost between sessions
is *where the project is*: which phase is closed, which commit was last known
good, how many tests were green, and what is blocked on whom.  Rediscovering
that from scratch every turn is exactly how work gets repeated or, worse,
reverted.  The snapshot is the answer: one small JSON file, committed, so it
travels with the code and is reviewable in a diff.

Contract
--------
* ``schema_version`` is bumped when the shape changes; :func:`load` refuses a
  file it does not understand rather than silently mis-reading it.
* :func:`verify` never mutates anything and never raises for a merely
  *stale* snapshot — staleness is reported as a problem string, because "HEAD
  moved on" is a normal, healthy condition, not corruption.
* Git access is best-effort: :func:`current_git_state` returns ``None`` outside
  a repository (or without git installed), and :func:`verify` downgrades the
  commit/branch checks to "unchecked" instead of failing.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Bump when the on-disk shape changes incompatibly.
SCHEMA_VERSION = 1

#: File name inside the repository root.
SNAPSHOT_FILENAME = "continuum.json"

#: Directory the snapshot lives in (committed, not git-ignored).
SNAPSHOT_DIRNAME = ".nexus"


class ContinuumError(RuntimeError):
    """The snapshot is missing, unreadable, or of an unknown schema version."""


# ── Model ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PhaseRecord:
    """One completed unit of work and the commit that carries it."""

    id: str
    commit: str
    status: str = "complete"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PhaseRecord:
        """Build a record from its JSON form, requiring ``id`` and ``commit``."""
        return cls(
            id=str(data["id"]),
            commit=str(data["commit"]),
            status=str(data.get("status", "complete")),
        )


@dataclass(frozen=True)
class PendingItem:
    """Work that is deliberately *not* done, with an owner and a reason."""

    id: str
    status: str
    reason: str = ""
    owner: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PendingItem:
        """Build a pending item from its JSON form."""
        return cls(
            id=str(data["id"]),
            status=str(data["status"]),
            reason=str(data.get("reason", "")),
            owner=str(data.get("owner", "")),
        )


@dataclass(frozen=True)
class ContinuumSnapshot:
    """The whole cross-turn state of the project, in one immutable value."""

    phase: str
    phase_status: str
    last_good_commit: str
    last_good_branch: str
    test_count_expected: int
    env_fingerprint: Mapping[str, str] = field(default_factory=dict)
    phases_completed: tuple[PhaseRecord, ...] = ()
    pending: tuple[PendingItem, ...] = ()
    next_phase: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Render the JSON-ready form (``phases_completed`` → the documented key)."""
        data = asdict(self)
        data["phases_completed"] = [asdict(record) for record in self.phases_completed]
        data["pending"] = [asdict(item) for item in self.pending]
        data["env_fingerprint"] = dict(self.env_fingerprint)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ContinuumSnapshot:
        """Parse the JSON form, refusing an unknown ``schema_version``."""
        version = int(data.get("schema_version", SCHEMA_VERSION))
        if version != SCHEMA_VERSION:
            raise ContinuumError(
                f"continuum.json has schema_version {version}, this build understands "
                f"{SCHEMA_VERSION}. Refusing to guess at the shape — migrate the file "
                f"or check out the commit that wrote it."
            )
        completed_key = "phases_completed" if "phases_completed" in data else "d_phases_completed"
        return cls(
            schema_version=version,
            phase=str(data["phase"]),
            phase_status=str(data["phase_status"]),
            last_good_commit=str(data["last_good_commit"]),
            last_good_branch=str(data["last_good_branch"]),
            test_count_expected=int(data["test_count_expected"]),
            env_fingerprint={str(k): str(v) for k, v in data.get("env_fingerprint", {}).items()},
            phases_completed=tuple(
                PhaseRecord.from_dict(item) for item in data.get(completed_key, [])
            ),
            pending=tuple(PendingItem.from_dict(item) for item in data.get("pending", [])),
            next_phase=str(data.get("next_phase", "")),
        )


@dataclass(frozen=True)
class GitState:
    """The repository's current position, when it can be determined at all."""

    commit: str
    branch: str


@dataclass(frozen=True)
class VerifyReport:
    """Outcome of :func:`verify`: never raises, always explains itself."""

    ok: bool
    problems: tuple[str, ...]
    checks: Mapping[str, Any]


# ── Paths and I/O ────────────────────────────────────────────────────────


def repo_root() -> Path:
    """Return the repository root (four levels up from this file)."""
    return Path(__file__).resolve().parents[3]


def default_snapshot_path() -> Path:
    """Return ``<repo>/.nexus/continuum.json``."""
    return repo_root() / SNAPSHOT_DIRNAME / SNAPSHOT_FILENAME


def load(path: Path | None = None) -> ContinuumSnapshot:
    """Load the snapshot, raising :class:`ContinuumError` if unusable."""
    target = path or default_snapshot_path()
    if not target.exists():
        raise ContinuumError(f"no continuum snapshot at {target} — nothing has been recorded yet")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuumError(f"continuum snapshot at {target} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ContinuumError(f"continuum snapshot at {target} is not a JSON object")
    try:
        return ContinuumSnapshot.from_dict(data)
    except KeyError as exc:
        raise ContinuumError(f"continuum snapshot at {target} is missing key {exc}") from exc


def save(snapshot: ContinuumSnapshot, path: Path | None = None) -> Path:
    """Write the snapshot atomically-ish and return the path written.

    Serialised with sorted keys and a trailing newline so successive writes
    produce a minimal, reviewable diff.
    """
    target = path or default_snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
    target.write_text(payload + "\n", encoding="utf-8")
    return target


# ── Git ──────────────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a read-only git command, returning its stdout or ``None``."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def current_git_state(root: Path | None = None) -> GitState | None:
    """Return HEAD and the checked-out branch, or ``None`` outside a repo."""
    cwd = root or repo_root()
    commit = _git(["rev-parse", "HEAD"], cwd)
    branch = _git(["branch", "--show-current"], cwd)
    if commit is None or branch is None:
        return None
    return GitState(commit=commit, branch=branch)


def is_ancestor(older: str, newer: str, root: Path | None = None) -> bool | None:
    """Whether ``older`` is an ancestor of ``newer``.

    ``None`` means "cannot tell" (git missing, unknown object, or a shallow
    clone that does not contain the history) — callers must treat that as
    *unchecked*, never as "diverged".
    """
    cwd = root or repo_root()
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(cwd), "merge-base", "--is-ancestor", older, newer],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def commits_ahead(older: str, newer: str, root: Path | None = None) -> int | None:
    """Number of commits from ``older`` to ``newer``, or ``None`` if unknown."""
    cwd = root or repo_root()
    count = _git(["rev-list", "--count", f"{older}..{newer}"], cwd)
    return int(count) if count is not None and count.isdigit() else None


# ── Verification ─────────────────────────────────────────────────────────


def verify(
    snapshot: ContinuumSnapshot | None = None,
    path: Path | None = None,
    actual_test_count: int | None = None,
    root: Path | None = None,
    strict: bool = False,
) -> VerifyReport:
    """Check the snapshot against reality, without changing anything.

    Checks, in order:

    1. schema version is one this build understands;
    2. ``phase_status`` is ``complete`` when the phase claims to be closed;
    3. ``last_good_commit`` is related to ``HEAD``: being *behind* HEAD is
       normal progress and is only reported (see below), while being unrelated
       to HEAD means history diverged or was rewritten, which is a problem;
    4. ``test_count_expected`` equals ``actual_test_count``, when supplied.

    A snapshot can never contain the hash of the commit that contains it, so
    "HEAD is one commit newer" is the ordinary state right after saving one.
    Treating that as a failure would make ``verify`` cry wolf at exactly the
    moment it should say everything is fine, so staleness is reported in
    ``checks["head_relation"]`` and only becomes a problem under
    ``strict=True``.

    ``last_good_branch`` is reported, never asserted: a closed phase is normally
    reviewed from ``main`` after the merge, so branch drift is normal.  Git
    checks report ``"unchecked"`` rather than failing when git is unavailable,
    so the command still works on a source tarball or a shallow clone.
    """
    problems: list[str] = []
    checks: dict[str, Any] = {}

    try:
        snap = snapshot if snapshot is not None else load(path)
    except ContinuumError as exc:
        return VerifyReport(ok=False, problems=(str(exc),), checks={"loaded": False})

    checks["loaded"] = True
    checks["phase"] = snap.phase
    checks["phase_status"] = snap.phase_status
    checks["last_good_commit"] = snap.last_good_commit
    checks["last_good_branch"] = snap.last_good_branch
    checks["test_count_expected"] = snap.test_count_expected
    checks["pending"] = [item.id for item in snap.pending]

    if snap.schema_version != SCHEMA_VERSION:  # pragma: no cover - load() guards this
        problems.append(
            f"schema_version {snap.schema_version} != {SCHEMA_VERSION} understood by this build"
        )

    if snap.phase_status != "complete":
        problems.append(f"phase {snap.phase} is not complete (status={snap.phase_status!r})")

    git = current_git_state(root)
    if git is None:
        checks["head"] = "unchecked"
        checks["branch"] = "unchecked"
    else:
        checks["head"] = git.commit
        # Branch drift is informational, not a problem: the snapshot records the
        # branch the work was *done* on, and a merged phase is normally reviewed
        # from main afterwards.
        checks["branch_matches"] = git.branch == snap.last_good_branch

        if git.commit == snap.last_good_commit:
            checks["head_relation"] = "at snapshot commit"
        else:
            relation = is_ancestor(snap.last_good_commit, git.commit, root)
            if relation is None:
                checks["head_relation"] = "unchecked (history unavailable)"
            elif relation:
                ahead = commits_ahead(snap.last_good_commit, git.commit, root)
                stale = (
                    f"snapshot is an ancestor of HEAD ({ahead} commit(s) newer)"
                    if ahead is not None
                    else "snapshot is an ancestor of HEAD"
                )
                checks["head_relation"] = stale
                if strict:
                    problems.append(
                        f"HEAD has moved past the snapshot ({snap.last_good_commit[:12]}); "
                        f"re-save it once this work is verified"
                    )
            else:
                checks["head_relation"] = "diverged"
                problems.append(
                    f"the snapshot's last_good_commit {snap.last_good_commit[:12]} is not an "
                    f"ancestor of HEAD {git.commit[:12]} — history diverged or was rewritten"
                )

    if actual_test_count is not None:
        checks["test_count_actual"] = actual_test_count
        if actual_test_count != snap.test_count_expected:
            problems.append(
                f"collected {actual_test_count} tests but the snapshot expects "
                f"{snap.test_count_expected}"
            )

    return VerifyReport(ok=not problems, problems=tuple(problems), checks=checks)
