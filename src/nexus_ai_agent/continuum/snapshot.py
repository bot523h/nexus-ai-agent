"""Continuum snapshot: a committed, verifiable project-state record.

The recurring operational failure of this repository has been turn-to-turn
state loss: the agent arrives with an empty working tree and no memory of the
previous turn.  The remote branch is the *only* thing that survives between
turns.

``continuum`` fixes the *navigation* problem on top of that: a tiny JSON file
(`.nexus/continuum.json`) committed inside the repo records which phase is in
progress, the last known-good commit, the expected test count and the
environment fingerprint.  Next turn, `nexus continuum verify` compares the
checkout against that record and reports drift — so recovery is a single
deterministic command instead of a forensic investigation.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)

SNAPSHOT_SCHEMA_VERSION = 1
_REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_PATH = _REPO_ROOT / ".nexus" / "continuum.json"


@dataclass
class EnvFingerprint:
    """Versions the snapshot was produced with (verified on ``verify``)."""

    python: str
    alembic: str
    sqlalchemy: str


@dataclass
class ContinuumSnapshot:
    """The full snapshot schema (see ``.nexus/continuum.json``)."""

    schema_version: int
    phase: str
    phase_status: str
    last_good_commit: str
    last_good_branch: str
    test_count_expected: int
    env_fingerprint: EnvFingerprint
    next_phase: str

    @classmethod
    def from_dict(cls, d: dict) -> ContinuumSnapshot:
        return cls(
            schema_version=d["schema_version"],
            phase=d["phase"],
            phase_status=d["phase_status"],
            last_good_commit=d["last_good_commit"],
            last_good_branch=d["last_good_branch"],
            test_count_expected=d["test_count_expected"],
            env_fingerprint=EnvFingerprint(**d["env_fingerprint"]),
            next_phase=d["next_phase"],
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent) + "\n"


def current_commit() -> str:
    """Return the current HEAD commit hash (short or fails loudly)."""
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError("cannot resolve current git commit")
    return out.stdout.strip()


def read_snapshot() -> ContinuumSnapshot:
    """Read the committed snapshot; raises if missing or malformed."""
    if not SNAPSHOT_PATH.exists():
        raise FileNotFoundError(f"snapshot missing: {SNAPSHOT_PATH}")
    data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(f"unsupported snapshot schema_version {data.get('schema_version')!r}")
    return ContinuumSnapshot.from_dict(data)


def write_snapshot(snapshot: ContinuumSnapshot) -> Path:
    """Write (and create directories for) the snapshot file."""
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(snapshot.to_json(), encoding="utf-8")
    log.info("wrote continuum snapshot: %s", SNAPSHOT_PATH)
    return SNAPSHOT_PATH


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant`` (work preserved)."""
    out = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return out.returncode == 0


def verify_snapshot() -> list[str]:
    """Compare the checkout against the snapshot; return a list of problems.

    The anti-state-loss invariant: the recorded good commit must still be
    reachable from HEAD.  HEAD equalling or being *ahead of* the recorded
    commit is healthy (work preserved, possibly advanced); HEAD having lost
    the recorded commit means state was lost and must be recovered.
    """
    try:
        snap = read_snapshot()
    except (FileNotFoundError, ValueError) as exc:
        return [f"snapshot unreadable: {exc}"]

    head = current_commit()
    if snap.last_good_commit and not _is_ancestor(snap.last_good_commit, head):
        return [
            f"state loss detected: recorded good commit "
            f"{snap.last_good_commit} is not reachable from HEAD {head}"
        ]
    return []
