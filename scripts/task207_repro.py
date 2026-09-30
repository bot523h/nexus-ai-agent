"""REPRODUCTION for task-207 — agent_board.py check fails OPEN.

The governance tool that decides whether an agent may touch a file reads only
the LOCAL .agents/board.json.  A claim that exists only on an unmerged PR branch
is therefore invisible, and `check` prints

    no overlap — safe to proceed.

and exits 0 for a file that is, in fact, under somebody else's live lease.

This is not a theoretical gap.  It was observed twice in one session:
  * task-202 (graph.py, PR #119) and task-203 (memory/, PR #121) were both live
    while `check` reported no overlap;
  * a scan whose input file list had been wiped reported "no lease found" from
    ZERO boards and looked exactly like a clean result.

This script builds the smallest world that shows the hole: a local board with no
claim, and a foreign board — reachable only if the tool looked — holding a live
lease on the file being checked.
"""

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "agent_board.py"
TARGET = "src/nexus_ai_agent/orchestration/graph.py"
FOREIGN_BRANCH = "arena/01a0e907-nexus-ai-agent"
MY_BRANCH = "arena/01a0eade-nexus-ai-agent"


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


def run_check(workdir: Path, extra: list[str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "check",
            "--files",
            TARGET,
            "--branch",
            MY_BRANCH,
            *(extra or []),
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "scripts").mkdir()
        (work / ".agents").mkdir()
        # Reuse the REAL tool; only the board it reads is synthetic.
        (work / "scripts" / "agent_board.py").write_text(TOOL.read_text(encoding="utf-8"))

        foreign = make_board([live_claim("task-202-memory-write-observability", FOREIGN_BRANCH)])
        (work / ".agents" / "board.json").write_text(
            json.dumps(make_board([]), indent=2), encoding="utf-8"
        )
        (work / "foreign_board.json").write_text(json.dumps(foreign, indent=2), encoding="utf-8")

        print("=" * 74)
        print("WORLD: a foreign PR branch holds a LIVE 7-day lease on")
        print(f"       {TARGET}")
        print("       The local board has no claims at all.")
        print("=" * 74)

        code, out = run_check(work)
        print("\n[1] check, no remote view available (the shipped behaviour)")
        print(f"    exit={code}")
        for line in out.splitlines():
            print(f"      {line}")
        verdict = "THE BUG" if code == 0 else "blocked"
        print(f"    -> {verdict}: a live foreign lease did not stop the check\n")

        code, out = run_check(work, ["--board-json", str(work / "foreign_board.json")])
        print("[2] check, with the foreign board visible")
        print(f"    exit={code}")
        for line in out.splitlines():
            print(f"      {line}")
        print(f"    -> {'BLOCKED correctly — task-207 fixed' if code == 1 else 'still leaking'}")

        print()
        print("VERDICT: case [1] is the pre-task-207 behaviour and case [2] is the fix.")
        print("The tool is the enforcement mechanism; it used to fail open, silently,")
        print("with the most reassuring wording it owns.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
