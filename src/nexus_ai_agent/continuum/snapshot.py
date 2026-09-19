"""Read and verify the committed continuum project-state snapshot."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path

from nexus_ai_agent.observability.logging import get_logger

log = get_logger(__name__)
SNAPSHOT_SCHEMA_VERSION = 2
_REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_PATH = _REPO_ROOT / ".nexus" / "continuum.json"


@dataclass
class EnvFingerprint:
    python: str
    alembic: str
    sqlalchemy: str


@dataclass
class ContinuumSnapshot:
    schema_version: int
    plan: str
    step: str
    next: str
    ledger: list[dict]
    test_count_expected: int
    env_fingerprint: EnvFingerprint

    @classmethod
    def from_dict(cls, d: dict) -> ContinuumSnapshot:
        return cls(
            d["schema_version"],
            d["plan"],
            d["step"],
            d["next"],
            d["ledger"],
            d["test_count_expected"],
            EnvFingerprint(**d["env_fingerprint"]),
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent) + "\n"


def current_commit() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )
    if out.returncode:
        raise RuntimeError("cannot resolve current git commit")
    return out.stdout.strip()


def read_snapshot() -> ContinuumSnapshot:
    data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(f"unsupported snapshot schema_version {data.get('schema_version')!r}")
    return ContinuumSnapshot.from_dict(data)


def write_snapshot(snapshot: ContinuumSnapshot) -> Path:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(snapshot.to_json(), encoding="utf-8")
    return SNAPSHOT_PATH


def _is_ancestor(a: str, d: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", a, d], cwd=_REPO_ROOT, check=False
        ).returncode
        == 0
    )


def _environment() -> EnvFingerprint:
    return EnvFingerprint(
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        version("alembic"),
        version("sqlalchemy"),
    )


def _test_case_count() -> int:
    """Count test functions, rather than test files, for a useful invariant."""
    total = 0
    for path in (_REPO_ROOT / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        total += sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
            for node in ast.walk(tree)
        )
    return total


def verify_snapshot() -> list[str]:
    try:
        snap = read_snapshot()
    except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
        return [f"snapshot unreadable: {exc}"]
    problems = []
    head = current_commit()
    if snap.step and not _is_ancestor(snap.step, head):
        problems.append(
            f"state loss detected: recorded good commit {snap.step} is not "
            f"reachable from HEAD {head}"
        )
    actual_tests = _test_case_count()
    if snap.test_count_expected != actual_tests:
        problems.append(
            f"test count mismatch: expected {snap.test_count_expected}, found {actual_tests}"
        )
    expected = _environment()
    if asdict(snap.env_fingerprint) != asdict(expected):
        problems.append(
            f"environment fingerprint mismatch: expected {asdict(snap.env_fingerprint)}, "
            f"found {asdict(expected)}"
        )
    return problems
