"""RED probes for the two reviewer findings of PR#202 (hermetic, driven via cmd_check).

The LOCAL board is clean w.r.t. the checked file, so the exit code is decided by
the PR-source path under test — the flaw the first version of this probe had.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO = Path("/home/user/nexus-ai-agent")
spec = importlib.util.spec_from_file_location("probe2", REPO / "scripts" / "agent_board.py")
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def zone_board(zone, task="synthetic-gate"):
    return {
        "schema": 2,
        "protocol": {"protocol_version": 2},
        "claims": [
            {
                "task": task,
                "status": "active",
                "zone": "z",
                "agent_branch": "arena/111-other",
                "claimed_at": "2026-01-01T00:00:00Z",
                "ttl_hours": 24,
                "gates_owner": True,
                "exclusive_paths": [zone],
            }
        ],
        "zones": [{"id": "z", "paths": [zone]}],
        "deferred_log": [],
        "next_work": [],
        "history": {"waves_closed": [], "pull_requests": [], "incidents": []},
    }


CHECKED_FILE = "src/mine/engine.py"
tmp = Path("/tmp/probe")
tmp.mkdir(parents=True, exist_ok=True)
(tmp / "board2.json").write_text(
    json.dumps(zone_board("src/other/")), encoding="utf-8"
)  # clean for CHECKED_FILE
m.BOARD = tmp / "board2.json"
claimed = datetime.strptime("2026-01-01T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ").replace(
    tzinfo=timezone.utc
)
m._now = lambda: claimed + timedelta(seconds=1)

CONFLICT = zone_board("src/mine/")  # a lease on the checked file, living in the PR head
CLEAN = {"claims": []}
ORIGIN = "o/r"


def head(n, ref, repo=ORIGIN, clone=None):
    return {
        "number": n,
        "head": {
            "ref": ref,
            "repo": {"full_name": repo, "clone_url": clone or f"https://github.com/{repo}.git"},
        },
    }


def run(label, gh_get, git):
    m._gh_get = gh_get
    m._git = git
    m._worktree_boards = lambda exclude: ([], True)
    m._read_remote_board = lambda: ({"claims": []}, "")  # origin/main itself: clean
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = m.cmd_check(
            SimpleNamespace(
                files=CHECKED_FILE,
                branch="arena/b561a2be-nexus-ai-agent",
                no_remote=False,
                repo=ORIGIN,
                token="t",
            )
        )
    out = buf.getvalue()
    sources = next((line for line in out.splitlines() if "source(s) read" in line), "?")
    print(f"  {label}: exit={code} | {sources.strip()}")
    return code, out


FAILS: list[str] = []


def want(label, got, expected, note=""):
    ok = got == expected
    print(f"    want exit {expected} -> {'GREEN' if ok else 'RED — ' + note}")
    if not ok:
        FAILS.append(label)


# ── Finding A: pagination ───────────────────────────────────────────────────
def gh_paged(url, token):
    if "page=2" in url:
        return [head(101, "arena/999-pr-only")]
    return [head(i, f"arena/harmless-{i}") for i in range(1, 101)]


def git_paged(args, cwd):
    if args[:2] == ["worktree", "list"]:
        return subprocess.CompletedProcess(args, 0, "", "")
    if args[0] == "fetch":
        return subprocess.CompletedProcess(args, 0, "", "")
    if args[0] == "show":
        if "arena/999-pr-only" in args[1]:
            return subprocess.CompletedProcess(args, 0, json.dumps(CONFLICT), "")
        return subprocess.CompletedProcess(args, 0, json.dumps(CLEAN), "")
    return subprocess.CompletedProcess(args, 1, "", "")


print("FINDING A (pagination, 101 open PRs; the conflict is on PR #101)")
c, _ = run("cmd_check", gh_paged, git_paged)
want("A", c, 1, "page 2 is never requested, so PR #101 is invisible")

print("FINDING A2 (page 2 unreachable -> must be unverifiable)")


def gh_page2_fails(url, token):
    if "page=2" in url:
        raise OSError("connection reset")
    return [head(i, f"arena/harmless-{i}") for i in range(1, 101)]


c, _ = run("cmd_check", gh_page2_fails, git_paged)
want("A2", c, 2, "partial data accepted as a clean pass")

print("FINDING B (fork PR whose ref is 'main'; origin/main also exists)")


def gh_fork(url, token):
    return [head(7, "main", "attacker/nexus-ai-agent")]


fetched: list[list[str]] = []


def git_fork(args, cwd):
    if args[:2] == ["worktree", "list"]:
        return subprocess.CompletedProcess(args, 0, "", "")
    if args[0] == "fetch":
        fetched.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    if args[0] == "show":
        if "attacker" in args[1] or "nexus-referee" in args[1]:
            return subprocess.CompletedProcess(args, 0, json.dumps(CONFLICT), "")
        return subprocess.CompletedProcess(args, 0, json.dumps(CLEAN), "")  # origin/main is clean
    return subprocess.CompletedProcess(args, 1, "", "")


c, _ = run("cmd_check", gh_fork, git_fork)
want("B", c, 1, "the fork's own board was never read")
wrong = [f for f in fetched if "origin" in f and any("refs/heads/main:" in x for x in f)]
print(
    f"    origin refspec 'refs/heads/main' used as the PR's source: {bool(wrong)} -> "
    f"{'RED — wrong repo accepted' if wrong else 'ok'}"
)
if wrong:
    FAILS.append("B-wrong-source")

print("FINDING B2 (two forks, same ref name 'release' -> two distinct sources)")


def gh_two(url, token):
    return [head(8, "release", "forka/nexus-ai-agent"), head(9, "release", "forkb/nexus-ai-agent")]


seen: list[list[str]] = []


def git_two(args, cwd):
    if args[:2] == ["worktree", "list"]:
        return subprocess.CompletedProcess(args, 0, "", "")
    if args[0] == "fetch":
        seen.append(args)
        return subprocess.CompletedProcess(args, 0 if "forka" in " ".join(args) else 1, "", "")
    if args[0] == "show":
        return subprocess.CompletedProcess(args, 0, json.dumps(CLEAN), "")
    return subprocess.CompletedProcess(args, 1, "", "")


c, _ = run("cmd_check", gh_two, git_two)
foreign = [f for f in seen if "forka" in " ".join(f) or "forkb" in " ".join(f)]
ok = len(foreign) >= 2
print(
    f"    both forks fetched as separate sources: {ok} ({len(foreign)} foreign fetch(es)) -> "
    f"{'GREEN' if ok else 'RED — deduped by bare ref name'}"
)
if not ok:
    FAILS.append("B2")

print()
print("RED FINDINGS:", FAILS if FAILS else "none")
