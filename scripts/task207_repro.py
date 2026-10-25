"""REPRODUCTION for task-207 — ``agent_board.py check`` must not fail OPEN.

Before task-207, ``check`` read only the LOCAL ``.agents/board.json``. A live
lease that lived only in another checkout — a sibling worktree, or a pushed PR
branch — was therefore invisible: ``check`` printed

    no overlap — safe to proceed.

and exited 0 for a file that was, in fact, under somebody else's live lease.
That was observed twice in one session (task-202 on ``graph.py``, PR #119, and
task-203 on ``memory/``, PR #121).

This script builds the smallest world that shows the hole is now closed, using
the REAL tool and a REAL ``git worktree`` so the only synthetic thing is the
lease itself:

  * ``local``   — a git repository whose ``.agents/board.json`` has NO claims;
  * ``foreign`` — a sibling worktree of ``local`` whose board holds a LIVE lease
                  on the file being checked.

``check`` must consult the sibling worktree (a source it reads via
``git worktree list``) and refuse: exit 1. A tool that read only the local board
would exit 0 — the shipped defect. The script exits non-zero if ``check`` does
not block, so it can never report success without asserting the outcome.
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "agent_board.py"
TARGET = "src/nexus_ai_agent/orchestration/graph.py"
MY_BRANCH = "arena/01a0eade-nexus-ai-agent"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "task207-repro",
    "GIT_AUTHOR_EMAIL": "task207-repro@example.invalid",
    "GIT_COMMITTER_NAME": "task207-repro",
    "GIT_COMMITTER_EMAIL": "task207-repro@example.invalid",
    "PATH": os.environ.get("PATH", ""),
}


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_board(claims: list[dict]) -> dict:
    return {
        "schema": 2,
        "updated_at": iso(datetime.now(timezone.utc)),
        "protocol": {"lease_ttl_hours": 24},
        "zones": [
            {
                "id": "conversation-memory-observability",
                "paths": [TARGET],
                "description": "memory observability",
            }
        ],
        "claims": claims,
        "deferred_log": [],
        "next_work": [],
    }


def live_claim(task: str, branch: str, hours: int = 24) -> dict:
    return {
        "task": task,
        "title": "A LIVE foreign lease on graph.py",
        "zone": "conversation-memory-observability",
        "status": "active",
        "agent_branch": branch,
        "claimed_at": iso(datetime.now(timezone.utc) - timedelta(hours=hours - 1)),
        "ttl_hours": hours,
        "exclusive_paths": [TARGET],
    }


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=_GIT_ENV)


def run_check(workdir: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [
            sys.executable,
            str(workdir / "scripts" / "agent_board.py"),
            "check",
            "--files",
            TARGET,
            "--branch",
            MY_BRANCH,
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local"
        foreign = Path(tmp) / "foreign"
        (local / "scripts").mkdir(parents=True)
        (local / ".agents").mkdir()
        # Reuse the REAL tool; only the boards are synthetic.
        (local / "scripts" / "agent_board.py").write_text(TOOL.read_text(encoding="utf-8"))
        (local / ".agents" / "board.json").write_text(
            json.dumps(make_board([]), indent=2), encoding="utf-8"
        )

        # A real repo with a real sibling worktree, so `git worktree list` (the
        # source `check` consults) actually sees the foreign checkout.
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
            git(cmd, local)
        git(["worktree", "add", "-q", str(foreign), "-b", "foreign-lease"], local)
        (foreign / ".agents").mkdir(exist_ok=True)
        foreign_claim = live_claim("task-202-memory-write-observability", "arena/foreign-lease")
        (foreign / ".agents" / "board.json").write_text(
            json.dumps(make_board([foreign_claim]), indent=2), encoding="utf-8"
        )

        print("=" * 74)
        print("WORLD: a sibling worktree holds a LIVE lease on")
        print(f"       {TARGET}")
        print("       The local board has no claims at all.")
        print("=" * 74)

        code, out = run_check(local)
        print(f"\ncheck exit={code}")
        for line in out.splitlines():
            print(f"      {line}")

        if code == 1:
            print("\n-> BLOCKED: the sibling worktree lease was consulted — task-207 holds.")
            return 0
        if code == 0:
            print("\n-> THE BUG: a live foreign lease did not stop the check.")
            return 1
        print(f"\n-> UNEXPECTED exit {code}: expected 1 (blocked) or 0 (the defect).")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
